"""Reconcile metered cost against a provider's authoritative billing.

The wired providers return token counts, not dollars, so :func:`record_usage`
prices each event from the static table in :mod:`plutus_agent.pricing` and flags
the row ``estimated = True``. That is fine for a live estimate and the prepaid
hard-stop, but a bill must reflect what the provider actually charged.

This module closes the gap. Given the provider's authoritative total for a
period (taken from the provider's own usage or cost export, e.g. the OpenAI
Usage/Costs API, the Anthropic usage report, or an AWS Cost and Usage Report),
it computes the difference between what Plutus debited and what was really billed
and writes one ``adjust`` ledger entry per provider so the ledger total matches
the invoice. The prepaid balance follows, because the balance is the sum of the
ledger deltas.

Design notes:

- Sign convention. Usage debits are stored as negative ledger deltas, so the
  ledger's contribution for a provider+period is ``-recorded``. To make it equal
  ``-authoritative`` we add ``recorded - authoritative``. Over-charging (recorded
  greater than authoritative) yields a positive adjust that credits the balance
  back; under-charging yields a negative adjust that debits the shortfall.
- Idempotent and restatement-safe. Each provider+period reconciliation is keyed
  by a deterministic ``stripe_ref`` (``reconcile:<period>:<provider>``). The new
  adjust is net of any prior adjust for that key, so re-running with the same
  authoritative total is a no-op, and re-running after the provider restates its
  invoice applies only the incremental correction.
- Never assumes missing data is zero. Providers with recorded usage but no
  authoritative total supplied are reported as unreconciled and left untouched,
  not refunded to zero.
- Dry-run by default. Nothing is written unless ``apply=True``; the writes for
  all providers happen in one serialized transaction.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import db


def _ref(period_label: str, provider: str) -> str:
    return f"reconcile:{period_label}:{provider}"


def recorded_cost_by_provider(conn, org_id: str,
                              start_ts: Optional[float] = None,
                              end_ts: Optional[float] = None) -> dict:
    """Sum recorded usage cost per provider (lowercased) over [start, end).

    Returns ``{provider: {recorded_micros, events, estimated_events,
    estimated_micros}}``. ``recorded_micros`` equals the usage debits already in
    the ledger, because a dropped (hard-stopped) event is never written to
    ``usage_events`` in the first place.
    """
    sql = ("SELECT LOWER(provider) AS provider, "
           "COALESCE(SUM(cost_micros),0) AS recorded_micros, "
           "COUNT(*) AS events, "
           "COALESCE(SUM(estimated),0) AS estimated_events, "
           "COALESCE(SUM(CASE WHEN estimated=1 THEN cost_micros ELSE 0 END),0) "
           "AS estimated_micros "
           "FROM usage_events WHERE org_id=?")
    params: list = [org_id]
    if start_ts is not None:
        sql += " AND ts >= ?"
        params.append(float(start_ts))
    if end_ts is not None:
        sql += " AND ts < ?"
        params.append(float(end_ts))
    sql += " GROUP BY LOWER(provider)"
    out = {}
    for r in conn.execute(sql, params).fetchall():
        out[r["provider"]] = {
            "recorded_micros": int(r["recorded_micros"]),
            "events": int(r["events"]),
            "estimated_events": int(r["estimated_events"]),
            "estimated_micros": int(r["estimated_micros"]),
        }
    return out


def _prior_adjust_micros(conn, org_id: str, ref: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(delta_micros),0) m FROM credit_ledger "
        "WHERE org_id=? AND kind='adjust' AND stripe_ref=?",
        (org_id, ref),
    ).fetchone()
    return int(row["m"])


@dataclass
class ReconItem:
    provider: str
    recorded_usd: float
    recorded_events: int
    estimated_events: int
    authoritative_usd: float
    prior_adjust_usd: float
    delta_usd: float          # new adjust to apply; + credits back, - charges more
    applied: bool = False
    ledger_id: Optional[str] = None
    note: str = ""


@dataclass
class ReconReport:
    org_id: str
    period_label: str
    applied: bool
    items: list = field(default_factory=list)
    unreconciled_providers: list = field(default_factory=list)
    total_adjust_usd: float = 0.0
    balance_after_usd: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "org_id": self.org_id,
            "period": self.period_label,
            "applied": self.applied,
            "total_adjust_usd": round(self.total_adjust_usd, 6),
            "balance_after_usd": (None if self.balance_after_usd is None
                                  else round(self.balance_after_usd, 6)),
            "unreconciled_providers": self.unreconciled_providers,
            "items": [vars(i) for i in self.items],
        }


def reconcile(conn, org_id: str, authoritative_usd: dict, *,
              period_label: str, start_ts: Optional[float] = None,
              end_ts: Optional[float] = None, apply: bool = False,
              ts: Optional[float] = None) -> ReconReport:
    """Reconcile recorded cost to ``authoritative_usd`` (``{provider: usd}``).

    Computes, per provider, the net ``adjust`` needed so the ledger reflects the
    authoritative total, and applies it when ``apply`` is True. Returns a
    :class:`ReconReport`. Providers with recorded usage but no authoritative
    total are reported in ``unreconciled_providers`` and left untouched.
    """
    recorded = recorded_cost_by_provider(conn, org_id, start_ts, end_ts)
    auth = {str(p).lower(): float(v) for p, v in authoritative_usd.items()}

    report = ReconReport(org_id=org_id, period_label=period_label, applied=apply)
    report.unreconciled_providers = sorted(set(recorded) - set(auth))

    plan = []
    for provider in sorted(auth):
        rec = recorded.get(provider, {"recorded_micros": 0, "events": 0,
                                       "estimated_events": 0})
        ref = _ref(period_label, provider)
        recorded_micros = rec["recorded_micros"]
        auth_micros = db.usd_to_micros(auth[provider])
        prior_micros = _prior_adjust_micros(conn, org_id, ref)
        # target ledger contribution = -auth; current = -recorded + prior.
        delta_micros = recorded_micros - auth_micros - prior_micros
        note = ""
        if rec["events"] == 0:
            note = "no metered usage; full provider cost booked as adjust"
        elif rec["estimated_events"] == rec["events"]:
            note = "all events were estimated from the price table"
        item = ReconItem(
            provider=provider,
            recorded_usd=db.micros_to_usd(recorded_micros),
            recorded_events=rec["events"],
            estimated_events=rec["estimated_events"],
            authoritative_usd=auth[provider],
            prior_adjust_usd=db.micros_to_usd(prior_micros),
            delta_usd=db.micros_to_usd(delta_micros),
            note=note,
        )
        plan.append((item, ref, delta_micros))

    def _emit():
        for item, ref, delta_micros in plan:
            if delta_micros == 0:
                item.note = (item.note + "; already reconciled").lstrip("; ")
                continue
            reason = (f"reconcile {period_label} {item.provider}: "
                      f"recorded ${item.recorded_usd:.6f} -> "
                      f"authoritative ${item.authoritative_usd:.6f}")
            if apply:
                row = db.add_ledger(conn, org_id, item.delta_usd, "adjust",
                                    reason=reason, stripe_ref=ref, ts=ts,
                                    commit=False)
                item.applied = True
                item.ledger_id = row["id"]
            report.total_adjust_usd += item.delta_usd
            report.items.append(item)
        # include the no-op items too, for a complete picture
        for item, _ref, delta_micros in plan:
            if delta_micros == 0:
                report.items.append(item)

    if apply:
        with db.immediate(conn):
            _emit()
        report.balance_after_usd = db.get_balance(conn, org_id)
    else:
        _emit()
        # projected balance if these adjusts were applied
        report.balance_after_usd = db.get_balance(conn, org_id) + report.total_adjust_usd

    report.total_adjust_usd = round(report.total_adjust_usd, 6)
    report.balance_after_usd = round(report.balance_after_usd, 6)
    return report


# --------------------------------------------------------------- input loading --
def load_authoritative(path: str | Path) -> dict:
    """Load provider authoritative totals from a normalized export file.

    Accepts either JSON or CSV:

    - JSON: ``{"totals": {"openai": 123.45, "anthropic": 67.89}}`` or a flat
      ``{"openai": 123.45}`` mapping.
    - CSV: a header with ``provider`` and one of ``cost_usd``/``cost``/``usd``.

    These are the provider's own numbers, normalized by the operator from the
    provider console or billing export. This module does not call provider APIs
    directly; see docs/reconciliation.md for how to produce this file per
    provider.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".csv":
        totals: dict = {}
        reader = csv.DictReader(text.splitlines())
        cost_key = None
        for row in reader:
            if cost_key is None:
                for k in ("cost_usd", "cost", "usd", "amount"):
                    if k in row:
                        cost_key = k
                        break
                if cost_key is None:
                    raise ValueError("CSV needs a cost_usd/cost/usd/amount column")
            prov = (row.get("provider") or "").strip()
            if prov:
                totals[prov] = totals.get(prov, 0.0) + float(row[cost_key] or 0)
        return totals
    data = json.loads(text)
    if isinstance(data, dict) and "totals" in data:
        data = data["totals"]
    if not isinstance(data, dict):
        raise ValueError("authoritative file must be a JSON object of {provider: usd}")
    return {str(k): float(v) for k, v in data.items()}


def month_window(period_label: str) -> tuple[float, float]:
    """[start, end) epoch seconds for a ``YYYY-MM`` label (UTC)."""
    year, month = (int(x) for x in period_label.split("-"))
    start = _dt.datetime(year, month, 1, tzinfo=_dt.timezone.utc)
    end = (_dt.datetime(year + 1, 1, 1, tzinfo=_dt.timezone.utc) if month == 12
           else _dt.datetime(year, month + 1, 1, tzinfo=_dt.timezone.utc))
    return start.timestamp(), end.timestamp()


def previous_month_label(now_ts: Optional[float] = None) -> str:
    """``YYYY-MM`` for the month before ``now`` (UTC). The cron default: a close
    run shortly after month end trues up the month that just ended."""
    now = _dt.datetime.fromtimestamp(now_ts if now_ts is not None else time.time(),
                                     tz=_dt.timezone.utc)
    first_of_this = now.replace(day=1)
    last_of_prev = first_of_this - _dt.timedelta(days=1)
    return f"{last_of_prev.year:04d}-{last_of_prev.month:02d}"


# ------------------------------------------------------ scheduled period close --
def close_period(conn, org_id: str, period_label: str, *,
                 providers: Optional[list] = None, apply: bool = False,
                 fetchers: Optional[dict] = None,
                 ts: Optional[float] = None,
                 checkpoint: bool = True,
                 hmac_key: Optional[bytes] = None) -> dict:
    """Fetch each provider's authoritative total for ``period_label`` and
    reconcile the org's ledger to it — the unattended fetch → reconcile step
    (#109) behind the cron close.

    ``providers`` defaults to the providers that actually have recorded usage in
    the period (so a routine close targets exactly what was metered). Pass an
    explicit list to also true-up a provider that billed but wasn't metered.

    A provider whose fetch fails is reported in ``fetch_errors`` and left OUT of
    the reconcile input, so the ledger is never zeroed on a failed fetch. Dry-run
    by default; nothing is written unless ``apply`` is True.

    #121: an APPLIED close also records a tamper-evidence checkpoint for the
    org (``checkpoint=False`` opts out), so every billing period ends with a
    fresh anchor the customer can retain out of band — a checkpoint that is
    never taken protects nothing. The anchor is returned as ``out["checkpoint"]``
    (``None`` on dry runs, opt-out, or an org with no chained events).
    """
    from . import fetchers as _fetchers  # lazy: keep provider SDKs off the import path
    start_ts, end_ts = month_window(period_label)
    if providers is None:
        providers = sorted(recorded_cost_by_provider(conn, org_id, start_ts, end_ts))
    totals, fetch_errors = _fetchers.fetch_authoritative(
        providers, start_ts, end_ts, fetchers=fetchers)
    report = reconcile(conn, org_id, totals, period_label=period_label,
                       start_ts=start_ts, end_ts=end_ts, apply=apply, ts=ts)
    out = report.as_dict()
    out["providers_requested"] = list(providers)
    out["fetch_errors"] = fetch_errors
    out["fetched"] = {k: round(v, 6) for k, v in totals.items()}
    out["checkpoint"] = None
    if apply and checkpoint:
        from . import db as _db
        out["checkpoint"] = _db.checkpoint_chain(conn, org_id, hmac_key=hmac_key, ts=ts)
    return out
