"""#177: detect and replay Stripe webhook events the server missed.

Deploy windows, restarts, and proxy blips can drop Stripe webhook
deliveries. Stripe retries for ~3 days, but longer gaps (or a webhook
endpoint whose ``enabled_events`` changed after the fact) leave events
permanently unapplied — historically only caught by manual ledger audits.

This reconciler lists recent events from the Stripe API (the source of
truth), diffs them against the local ``stripe_events`` table, reports any
that were never applied, and can replay them through the app's own
:func:`handle_webhook_event` — which is idempotent by design (event-claim
inside one transaction + cumulative-reversal convergence keyed by charge),
so replays and re-runs are safe no-ops when there is nothing to do.

Dry-run by default; the CLI passes ``apply=True`` for ``--apply``.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

API_BASE = "https://api.stripe.com"

# Event types handle_webhook_event knows how to apply. Keep in sync with
# billing/stripe_client.py's dispatch and the dashboard webhook endpoint's
# enabled_events.
DEFAULT_TYPES = (
    "checkout.session.completed",
    "charge.refunded",
    "charge.dispute.created",
    "charge.dispute.funds_withdrawn",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_failed",
)


def _stripe_get(secret: str, path: str) -> dict:
    req = urllib.request.Request(
        API_BASE + path, headers={"Authorization": f"Bearer {secret}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_events(secret: str, types, since_ts: float,
                 max_pages_per_type: int = 10) -> list[dict]:
    """Events of ``types`` created at/after ``since_ts``, oldest first."""
    out: list[dict] = []
    for etype in types:
        starting_after = None
        for _ in range(max_pages_per_type):
            q = {"type": etype, "created[gte]": int(since_ts), "limit": 100}
            if starting_after:
                q["starting_after"] = starting_after
            page = _stripe_get(secret, "/v1/events?" + urllib.parse.urlencode(q))
            out.extend(page.get("data", []))
            if not page.get("has_more"):
                break
            starting_after = page["data"][-1]["id"]
    out.sort(key=lambda e: e.get("created", 0))
    return out


def processed_ids(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT event_id FROM stripe_events")}


def missing_events(conn, events: list[dict]) -> list[dict]:
    seen = processed_ids(conn)
    return [e for e in events if e.get("id") and e["id"] not in seen]


def replay(conn, events: list[dict]) -> list[dict]:
    """Apply events oldest-first via the app's own (idempotent) handler."""
    from .billing.stripe_client import handle_webhook_event
    results = []
    for e in sorted(events, key=lambda x: x.get("created", 0)):
        results.append(handle_webhook_event(conn, e))
    return results


def reconcile(conn, secret: str, *, days: float = 7.0, types=None,
              apply: bool = False) -> dict:
    """Diff Stripe's event log vs ``stripe_events``; optionally replay gaps."""
    types = tuple(types) if types else DEFAULT_TYPES
    since = time.time() - days * 86400
    events = fetch_events(secret, types, since)
    missing = missing_events(conn, events)
    report = {
        "window_days": days,
        "types": list(types),
        "stripe_events_found": len(events),
        "already_processed": len(events) - len(missing),
        "missing": [{"id": e["id"], "type": e["type"],
                     "created": e.get("created")} for e in missing],
        "applied": [],
    }
    if apply and missing:
        report["applied"] = replay(conn, missing)
        conn.commit()
    return report
