"""JSON API shaping for the dashboard poller and external integrations."""
from __future__ import annotations

import csv
import io

from .. import db, metering, pricing, savings


def default_org_id(conn) -> str | None:
    orgs = db.list_orgs(conn)
    return orgs[0]["id"] if orgs else None


def summary_json(conn, org_id: str) -> dict:
    """A flatter, poll-friendly view of an org's current state."""
    s = metering.org_summary(conn, org_id)
    return {
        "org_id": org_id,
        "org": s["org"]["name"] if s["org"] else None,
        "tier": s["tier"],
        "balance": s["balance"],
        "windows": s["windows"],
        "tracked_tokens_mtd": s["tracked_tokens_mtd"],
        "tracked_limit": s["tracked_limit"],
        "by_provider": s["by_provider"],
        "by_workspace": s["by_workspace"],
        "by_task_type": s["by_task_type"],
        "by_user": s["by_user"],
        "seats": s["seats"],
        "provider_health": s["provider_health"],
        "alerts": s["alerts"],
    }


def orgs_json(conn, orgs=None, limit=None, offset=0) -> list[dict]:
    if orgs is None:
        rows = db.list_orgs(conn, limit=limit, offset=offset)
    else:
        rows = orgs
        if limit is not None:
            rows = rows[offset:offset + limit]
    return [
        {"id": o["id"], "name": o["name"], "slug": o["slug"], "tier": o["tier"],
         "balance": db.get_balance(conn, o["id"])}
        for o in rows
    ]


# ----------------------------------------------------------- paged list views ---
def _page(items: list[dict], limit: int) -> dict:
    """Wrap a page of rows with a cursor. ``next_before`` is the ``_rowid`` to
    pass as ``before`` for the next page, or None when the page wasn't full."""
    next_before = items[-1]["_rowid"] if len(items) == limit and items else None
    return {"items": items, "next_before": next_before, "limit": limit}


def ledger_json(conn, org_id: str, limit: int = 50, before=None) -> dict:
    return _page(db.ledger_history(conn, org_id, limit=limit, before=before), limit)


def events_json(conn, org_id: str, limit: int = 50, before=None) -> dict:
    return _page(metering.recent_events(conn, org_id, limit=limit, before=before), limit)


_EXPORT_COLUMNS = ["id", "ts", "provider", "model", "task_type", "workspace",
                   "input_tokens", "output_tokens", "cache_read_tokens",
                   "cache_write_tokens", "reasoning_tokens", "user_id",
                   "cost_usd", "baseline_usd",
                   "external_ref", "estimated", "source"]


def _csv_safe(value):
    """Neutralize spreadsheet formula injection: a cell whose text begins with
    ``= + - @`` (or a leading tab/CR that a spreadsheet strips to reach them) is
    prefixed with a single quote so Excel/Sheets treat it as text, not a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def audit_json(conn, org_id: str, *, hmac_key: str | None = None) -> dict:
    """Return a compact, tier-independent proof receipt for Plutus savings."""
    import time
    period = time.strftime("%Y-%m", time.gmtime())
    org = db.get_org(conn, org_id)
    tier_key = org["tier"] if org else "free"
    report = savings.savings_share_report(conn, org_id, period).as_dict()
    integrity = db.verify_chain(conn, org_id=org_id, hmac_key=hmac_key)
    checkpoints = db.list_checkpoints(conn, org_id)
    donation = pricing.recommended_donation_usd(
        tier_key, report.get("gross_savings_usd", 0.0)
    )
    return {
        "org_id": org_id,
        "tier": tier_key,
        "period": period,
        "savings": report,
        "recommended_donation_usd": donation,
        "recommended_donation_bps": pricing.tier(tier_key).donation_bps,
        "ledger_integrity": integrity,
        "latest_checkpoint": dict(checkpoints[-1]) if checkpoints else None,
        "audit_access": pricing.tier(tier_key).audit_access,
        "verification": {
            "method": "hash-chained usage ledger plus optional retained checkpoint",
            "authoritative_billing_required": report.get("billing_provisional") is not False,
        },
    }


def export_csv(conn, org_id: str, since=None, until=None) -> str:
    """Org-scoped usage events as CSV text (fix #66).

    Cells are formula-injection neutralized (2026-07-05 security review):
    ``provider``/``model``/``workspace``/``task_type`` are tenant-controlled at
    ingest, so a crafted value like ``=HYPERLINK(...)`` would otherwise execute
    when a teammate opens the export in a spreadsheet."""
    rows = db.export_events(conn, org_id, since=since, until=until)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_EXPORT_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: _csv_safe(v) for k, v in r.items()})
    return buf.getvalue()


def export_json(conn, org_id: str, since=None, until=None) -> dict:
    rows = db.export_events(conn, org_id, since=since, until=until)
    return {"org_id": org_id, "count": len(rows), "events": rows}
