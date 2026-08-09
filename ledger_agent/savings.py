"""Savings-share — the value-based revenue path (#7).

Ledger meters what an agent actually spends. When the caller also records a
**counterfactual** cost at meter time (``baseline_cost_usd`` — what the same call
would have cost without Perseus, i.e. the same token counts priced at the
customer's designated baseline model), the difference is a *provable* saving:

    saving(event) = max(0, baseline_micros - cost_micros)

This module aggregates that per-period and turns the agreed share of it (default
10%) into a bill. Three deliberate properties make the number defensible enough
to put on an invoice:

* **Only provable savings count.** Events with no baseline (``baseline_micros IS
  NULL``) contribute zero — never a blanket "you saved 40%". Coverage (how many
  events carried a baseline) is reported so a thin-coverage period is visible,
  not hidden.
* **Per-event clamp.** A single event where the baseline came in *below* actual
  cost contributes 0, not a negative that would silently erode a genuine saving
  elsewhere in the period.
* **Recomputable + tamper-evident.** ``baseline_micros`` is folded into the
  usage_events hash chain (see :mod:`ledger_agent.db`), and it is a deterministic
  function of the chained token counts and the published price table, so a
  customer can independently reconstruct and verify every dollar billed.

Billing mirrors :mod:`ledger_agent.reconcile`: **dry-run by default**, idempotent
per org+period (the ``savings_invoices`` UNIQUE(org_id, period_label) row), and
writes happen in one serialized transaction.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional

from . import db
from .reconcile import month_window, previous_month_label  # reuse the period math

# Savings-share rate as basis points. 1000 bps = 10%. The operator can override
# via billing.savings_share_pct in config; this is the shipped default.
DEFAULT_RATE_BPS = 1000

# #151: minimum coverage percentage (0-100) below which billing is blocked.
# When fewer than this fraction of events carry a baseline, the savings figure
# is too thin to bill on. Overridable via billing.min_coverage_pct in config.
DEFAULT_MIN_COVERAGE_PCT = 50.0

# #151: maximum estimated ratio (0-100) above which billing is provisional only.
# When more than this fraction of cost is estimated rather than authoritative,
# the invoice is marked provisional and Stripe billing is skipped.
DEFAULT_MAX_ESTIMATED_PCT = 20.0


def rate_bps_from_config(cfg: Optional[dict]) -> int:
    """Resolve the savings-share rate (basis points) from config, or the default.

    ``billing.savings_share_pct`` is a fraction (0.10 = 10%). Rejects values
    outside [0, 1] rather than silently billing a nonsensical share.
    """
    billing = (cfg or {}).get("billing", {}) if cfg else {}
    pct = billing.get("savings_share_pct")
    if pct is None:
        return DEFAULT_RATE_BPS
    pct = float(pct)
    if not (0.0 <= pct <= 1.0):
        raise ValueError(
            f"billing.savings_share_pct must be between 0 and 1, got {pct}"
        )
    return int(round(pct * 10_000))


def coverage_threshold_from_config(cfg: Optional[dict]) -> float:
    """Resolve the minimum coverage percentage from config, or the default."""
    billing = (cfg or {}).get("billing", {}) if cfg else {}
    return float(billing.get("min_coverage_pct", DEFAULT_MIN_COVERAGE_PCT))


def estimated_threshold_from_config(cfg: Optional[dict]) -> float:
    """Resolve the maximum estimated percentage from config, or the default."""
    billing = (cfg or {}).get("billing", {}) if cfg else {}
    return float(billing.get("max_estimated_pct", DEFAULT_MAX_ESTIMATED_PCT))


# ------------------------------------------------------------- aggregation ---
def period_savings(conn, org_id: str, start_ts: Optional[float] = None,
                   end_ts: Optional[float] = None) -> dict:
    """Gross savings over [start, end) for an org.

    Returns ``{gross_savings_micros, covered_events, total_events,
    baseline_micros, cost_on_covered_micros}``. ``gross_savings_micros`` is the
    sum of the per-event clamp ``max(0, baseline-cost)`` over events that carry a
    baseline; events without one are counted only in ``total_events``.
    """
    # Billable events: carry a baseline, the baseline exceeds the actual cost, and
    # the actual cost is POSITIVE. The cost>0 guard is a defensibility rule: an
    # event with no recorded cost (a data gap, or a free/local model) would
    # otherwise book its entire baseline as "savings" — an indefensible bill. We
    # under-count (a genuinely-free routed call records nothing) rather than bill a
    # saving we can't stand behind. ``covered``/``base``/``cost_cov`` count all
    # baseline-carrying events for coverage transparency, so a period where cost is
    # missing shows up as a gap between coverage and billable gross.
    billable = ("baseline_micros IS NOT NULL AND cost_micros > 0 "
                "AND baseline_micros > cost_micros")
    sql = (
        "SELECT "
        f"COALESCE(SUM(CASE WHEN {billable} "
        "          THEN baseline_micros - cost_micros ELSE 0 END),0) AS gross, "
        f"COALESCE(SUM(CASE WHEN {billable} THEN 1 ELSE 0 END),0) AS billable_events, "
        "COALESCE(SUM(CASE WHEN baseline_micros IS NOT NULL THEN 1 ELSE 0 END),0) AS covered, "
        "COUNT(*) AS total, "
        "COALESCE(SUM(CASE WHEN baseline_micros IS NOT NULL THEN baseline_micros ELSE 0 END),0) AS base, "
        "COALESCE(SUM(CASE WHEN baseline_micros IS NOT NULL THEN cost_micros ELSE 0 END),0) AS cost_cov "
        "FROM usage_events WHERE org_id=?"
    )
    params: list = [org_id]
    if start_ts is not None:
        sql += " AND ts >= ?"
        params.append(float(start_ts))
    if end_ts is not None:
        sql += " AND ts < ?"
        params.append(float(end_ts))
    r = conn.execute(sql, params).fetchone()
    return {
        "gross_savings_micros": int(r["gross"]),
        "billable_events": int(r["billable_events"]),
        "covered_events": int(r["covered"]),
        "total_events": int(r["total"]),
        "baseline_micros": int(r["base"]),
        "cost_on_covered_micros": int(r["cost_cov"]),
    }


def _share_micros(gross_micros: int, rate_bps: int) -> int:
    """Billable share of gross savings, in micros. Integer-exact: multiply then
    divide by 10_000, rounding half-up, so the invoiced amount is deterministic.
    """
    if gross_micros <= 0 or rate_bps <= 0:
        return 0
    # round(gross * rate_bps / 10000) without float error
    num = gross_micros * rate_bps
    return (num + 5_000) // 10_000


@dataclass
class SavingsShareReport:
    org_id: str
    period_label: str
    rate_bps: int
    gross_savings_usd: float
    billable_share_usd: float
    billable_events: int
    covered_events: int
    total_events: int
    baseline_usd: float
    cost_on_covered_usd: float
    already_invoiced: bool = False
    stripe_invoice_id: Optional[str] = None
    notes: list = field(default_factory=list)
    window: Optional[str] = None
    window_start_ts: Optional[float] = None
    window_end_ts: Optional[float] = None
    authoritative_events: int = 0
    estimated_events: int = 0
    reconciliation_status: str = "unknown"
    freshness_ts: Optional[float] = None
    # #151: reconciliation variance — the gap between estimated and
    # authoritative cost. Positive = estimated > authoritative (over-counted),
    # negative = estimated < authoritative (under-counted).
    reconciliation_variance_usd: float = 0.0
    # #151: whether billing was blocked due to low coverage (billing_blocked)
    # or issued as provisional only (billing_provisional).
    billing_blocked: bool = False
    billing_provisional: bool = False

    @property
    def coverage_pct(self) -> Optional[float]:
        if not self.total_events:
            return None
        return round(self.covered_events / self.total_events * 100.0, 1)

    @property
    def estimated_pct(self) -> Optional[float]:
        """Percentage of events that used estimated cost (#151)."""
        if not self.total_events:
            return None
        return round(self.estimated_events / self.total_events * 100.0, 1)

    def as_dict(self) -> dict:
        return {
            "org_id": self.org_id,
            "period": self.period_label,
            "rate_pct": self.rate_bps / 100.0,
            "gross_savings_usd": round(self.gross_savings_usd, 6),
            "billable_share_usd": round(self.billable_share_usd, 6),
            "billable_events": self.billable_events,
            "covered_events": self.covered_events,
            "total_events": self.total_events,
            "coverage_pct": self.coverage_pct,
            "baseline_usd": round(self.baseline_usd, 6),
            "cost_on_covered_usd": round(self.cost_on_covered_usd, 6),
            "already_invoiced": self.already_invoiced,
            "stripe_invoice_id": self.stripe_invoice_id,
            "notes": self.notes,
            "window": self.window,
            "window_start_ts": self.window_start_ts,
            "window_end_ts": self.window_end_ts,
            "authoritative_events": self.authoritative_events,
            "estimated_events": self.estimated_events,
            "reconciliation_status": self.reconciliation_status,
            "freshness_ts": self.freshness_ts,
            "reconciliation_variance_usd": round(self.reconciliation_variance_usd, 6),
            "estimated_pct": self.estimated_pct,
            "billing_blocked": self.billing_blocked,
            "billing_provisional": self.billing_provisional,
        }


def operational_window(window: str, now_ts: Optional[float] = None) -> tuple[str, float, float]:
    """Resolve an operational reporting window in UTC."""
    now = float(now_ts if now_ts is not None else time.time())
    if window == "24h":
        return "last-24h", now - 86400.0, now
    if window == "7d":
        return "last-7d", now - 7 * 86400.0, now
    import datetime as dt
    current = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    if window == "today":
        start = dt.datetime(current.year, current.month, current.day, tzinfo=dt.timezone.utc).timestamp()
        return "today", start, now
    if window == "mtd":
        start = dt.datetime(current.year, current.month, 1, tzinfo=dt.timezone.utc).timestamp()
        return "mtd", start, now
    if window == "billing":
        label = current.strftime("%Y-%m")
        start, end = month_window(label)
        return label, start, min(end, now)
    raise ValueError(f"unknown operational window: {window}")


def operational_savings_report(conn, org_id: str, window: str, *,
                               rate_bps: int = DEFAULT_RATE_BPS,
                               now_ts: Optional[float] = None) -> SavingsShareReport:
    """Evidence-aware report for live operational windows.

    This is read-only and never invoices. ``estimated_events`` are events whose
    cost was estimated rather than provider-authoritative; they remain visible
    and prevent the report from being presented as fully reconciled.
    """
    label, start_ts, end_ts = operational_window(window, now_ts)
    agg = period_savings(conn, org_id, start_ts, end_ts)
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN estimated=1 THEN 1 ELSE 0 END),0) estimated "
        "FROM usage_events WHERE org_id=? AND ts>=? AND ts<?",
        (org_id, start_ts, end_ts),
    ).fetchone()
    estimated = int(row["estimated"])
    gross = int(agg["gross_savings_micros"])
    report = SavingsShareReport(
        org_id=org_id, period_label=label, rate_bps=rate_bps,
        gross_savings_usd=db.micros_to_usd(gross),
        billable_share_usd=db.micros_to_usd(_share_micros(gross, rate_bps)),
        billable_events=agg["billable_events"],
        covered_events=agg["covered_events"], total_events=agg["total_events"],
        baseline_usd=db.micros_to_usd(agg["baseline_micros"]),
        cost_on_covered_usd=db.micros_to_usd(agg["cost_on_covered_micros"]),
        window=window, window_start_ts=start_ts, window_end_ts=end_ts,
        authoritative_events=max(0, agg["total_events"] - estimated),
        estimated_events=estimated,
        reconciliation_status="estimated" if estimated else "authoritative",
        freshness_ts=time.time(),
    )
    if estimated:
        report.notes.append(
            f"{estimated}/{agg['total_events']} events use estimated cost; "
            "provider reconciliation is required before billing."
        )
    if not agg["total_events"]:
        report.notes.append("no usage events were recorded in this window.")
    return report


def savings_share_report(conn, org_id: str, period_label: str, *,
                         rate_bps: int = DEFAULT_RATE_BPS) -> SavingsShareReport:
    """Dry-run savings-share figure for an org+period. Reads only."""
    start_ts, end_ts = month_window(period_label)
    agg = period_savings(conn, org_id, start_ts, end_ts)
    gross_micros = agg["gross_savings_micros"]
    share_micros = _share_micros(gross_micros, rate_bps)

    # #151: count estimated events for reconciliation variance exposure
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN estimated=1 THEN 1 ELSE 0 END),0) estimated "
        "FROM usage_events WHERE org_id=? AND ts>=? AND ts<?",
        (org_id, start_ts, end_ts),
    ).fetchone()
    estimated = int(row["estimated"])
    authoritative = max(0, agg["total_events"] - estimated)

    existing = db.get_savings_invoice(conn, org_id, period_label)
    report = SavingsShareReport(
        org_id=org_id,
        period_label=period_label,
        rate_bps=rate_bps,
        gross_savings_usd=db.micros_to_usd(gross_micros),
        billable_share_usd=db.micros_to_usd(share_micros),
        billable_events=agg["billable_events"],
        covered_events=agg["covered_events"],
        total_events=agg["total_events"],
        baseline_usd=db.micros_to_usd(agg["baseline_micros"]),
        cost_on_covered_usd=db.micros_to_usd(agg["cost_on_covered_micros"]),
        already_invoiced=bool(existing and existing["status"] == "invoiced"),
        stripe_invoice_id=(existing or {}).get("stripe_invoice_id"),
        authoritative_events=authoritative,
        estimated_events=estimated,
        reconciliation_status="estimated" if estimated else "authoritative",
    )
    if agg["covered_events"] == 0 and agg["total_events"] > 0:
        report.notes.append(
            "no events in this period carried a baseline; nothing is billable. "
            "The metering caller must pass baseline_cost_usd to record savings."
        )
    elif agg["covered_events"] < agg["total_events"]:
        report.notes.append(
            f"{agg['covered_events']}/{agg['total_events']} events carried a "
            "baseline; only those contribute to billable savings."
        )
    excluded = agg["covered_events"] - agg["billable_events"]
    if excluded > 0:
        report.notes.append(
            f"{excluded} baseline-carrying event(s) excluded from billing "
            "(no positive recorded cost — a free/local model or a data gap; "
            "never billed on an unprovable saving)."
        )
    return report


# ---------------------------------------------------------------- billing ---
def bill_savings_share(conn, org_id: str, period_label: str, *,
                       rate_bps: int = DEFAULT_RATE_BPS,
                       stripe_client=None, apply: bool = False,
                       min_charge_usd: float = 0.50,
                       min_coverage_pct: float = DEFAULT_MIN_COVERAGE_PCT,
                       max_estimated_pct: float = DEFAULT_MAX_ESTIMATED_PCT,
                       ts: Optional[float] = None) -> dict:
    """Compute and (when ``apply``) raise a savings-share invoice for a period.

    Dry-run by default: returns the report plus ``would_invoice``. With
    ``apply=True`` it records the ``savings_invoices`` row and, if a Stripe client
    is supplied and available, raises the Stripe invoice. Idempotent per
    org+period — a second apply for an already-invoiced period is a no-op.

    ``min_charge_usd`` skips raising a Stripe invoice for a trivial amount (the
    row is still recorded so the period is closed), avoiding sub-dollar invoices
    that cost more to process than they collect.

    New in #151:
    - ``min_coverage_pct``: billing is blocked when coverage is below this
      threshold (too few events carry a baseline → savings figure is unreliable).
    - ``max_estimated_pct``: billing is provisional when estimated events exceed
      this threshold (unreconciled cost → no Stripe invoice raised).
    - Pricing version is captured from the price table at billing time so the
      exact rates used can be reconstructed from the invoice evidence.
    """
    from .pricing import PRICE_TABLE_AS_OF

    report = savings_share_report(conn, org_id, period_label, rate_bps=rate_bps)
    out = report.as_dict()

    if report.already_invoiced:
        out["status"] = "already_invoiced"
        out["applied"] = False
        return out

    # Derive the billed amount in integer micros straight from the aggregation
    # (not by round-tripping the rounded USD display value), so what is stored
    # and invoiced is exactly rate_bps of the summed integer savings.
    start_ts, end_ts = month_window(period_label)
    gross_micros = period_savings(conn, org_id, start_ts, end_ts)["gross_savings_micros"]
    share_micros = _share_micros(gross_micros, rate_bps)
    out["would_invoice"] = report.billable_share_usd

    # Compute coverage and reconciliation variance
    coverage = report.coverage_pct or 0.0
    estimated_pct = report.estimated_pct or 0.0

    # Compute reconciliation variance (#151): compare estimated vs authoritative
    # cost on covered events. Positive = over-counted, negative = under-counted.
    if report.authoritative_events > 0 and report.estimated_events > 0:
        # We need the authoritative vs estimated cost difference per provider
        # from the reconcile module. For now, derive it from the report:
        # authoritative cost = cost_on_covered - estimated portion
        pass
    est_ratio = estimated_pct / 100.0 if report.total_events else 0.0

    # --- Threshold checks (#151) ------------------------------------------
    billing_blocked = False
    billing_provisional = False

    if coverage < min_coverage_pct:
        billing_blocked = True
        out["notes"].append(
            f"coverage ({coverage:.1f}%) is below the minimum "
            f"({min_coverage_pct:.0f}%); billing is blocked. "
            "Events must carry a baseline for savings to be billable."
        )

    if not billing_blocked and est_ratio > (max_estimated_pct / 100.0):
        billing_provisional = True
        out["notes"].append(
            f"{report.estimated_events}/{report.total_events} events "
            f"({estimated_pct:.1f}%) use estimated cost, exceeding the "
            f"{max_estimated_pct:.0f}% threshold. "
            "Billing is provisional — reconcile before collecting."
        )

    below_min = report.billable_share_usd < float(min_charge_usd)

    if not apply:
        out["status"] = "dry_run"
        out["applied"] = False
        out["billing_blocked"] = billing_blocked
        out["billing_provisional"] = billing_provisional
        if billing_blocked:
            pass  # note already added
        elif billing_provisional:
            pass  # note already added
        elif below_min and report.billable_share_usd > 0:
            out["notes"].append(
                f"amount ${report.billable_share_usd:.2f} is below the "
                f"${float(min_charge_usd):.2f} minimum; --apply would record the "
                "period without raising a Stripe invoice."
            )
        return out

    # --- apply -----------------------------------------------------------
    if billing_blocked:
        out["status"] = "blocked"
        out["applied"] = False
        out["billing_blocked"] = True
        return out

    stripe_invoice_id = None
    status = "pending"
    min_charge_met = not below_min and share_micros > 0

    if share_micros <= 0:
        status = "void"  # nothing billable; record a closed, zero period
    elif billing_provisional:
        status = "provisional"  # recorded but not ripe for Stripe
        out["notes"].append(
            f"Provisional — estimated events exceed {max_estimated_pct:.0f}% "
            "threshold; no Stripe invoice raised."
        )
    elif below_min:
        status = "pending"  # recorded, but no Stripe invoice raised
        out["notes"].append(
            f"amount below ${float(min_charge_usd):.2f} minimum; recorded without "
            "a Stripe invoice."
        )
    elif stripe_client is not None and getattr(stripe_client, "available", False):
        inv = stripe_client.create_savings_invoice(
            conn, org_id, report.billable_share_usd, period_label,
            description=(
                f"Perseus savings-share {period_label}: "
                f"{report.rate_bps/100.0:.0f}% of ${report.gross_savings_usd:,.2f} "
                f"verified savings"
            ),
        )
        stripe_invoice_id = inv.get("id")
        status = "invoiced"
    else:
        # amount is billable but Stripe isn't wired: record it as pending so the
        # operator can raise it once Stripe is connected. Never silently drop it.
        status = "pending"
        out["notes"].append(
            "Stripe is not connected; recorded as pending (no invoice raised)."
        )

    with db.immediate(conn):
        row = db.record_savings_invoice(
            conn, org_id, period_label,
            gross_savings_micros=gross_micros, rate_bps=rate_bps,
            amount_micros=share_micros,
            covered_events=report.covered_events,
            total_events=report.total_events,
            stripe_invoice_id=stripe_invoice_id, status=status, ts=ts,
            pricing_version=PRICE_TABLE_AS_OF,
            min_charge_met=min_charge_met,
            commit=False,
        )
    out["status"] = status
    out["applied"] = True
    out["stripe_invoice_id"] = stripe_invoice_id
    out["savings_invoice_id"] = row["id"]
    out["pricing_version"] = PRICE_TABLE_AS_OF
    out["billing_blocked"] = billing_blocked
    out["billing_provisional"] = billing_provisional
    return out


# ---------------------------------------------------------- corrections (#151) ---
CORRECTION_KEY_PREFIX = "save-correction:"


def record_savings_correction(
    conn, org_id: str, period_label: str, *,
    previous_amount_micros: int, corrected_amount_micros: int,
    reason: str, ts: Optional[float] = None,
) -> dict:
    """Record an auditable correction to a previously invoiced savings-share period.

    Corrections create a credit_ledger entry for the difference (positive = we
    under-billed and are now billing more; negative = we over-billed and are
    returning credit). The correction is idempotent per org+period+reason via a
    deterministic stripe_ref, so re-runs never double-apply.

    Returns ``{stripe_ref, delta_micros, delta_usd, ledger_entry, already_applied}``.
    """
    delta = corrected_amount_micros - previous_amount_micros
    # Use hashlib (not Python's built-in hash(), which is salted and
    # non-deterministic across interpreter restarts) so the same correction
    # reason always produces the same stripe_ref — essential for idempotency.
    reason_digest = hashlib.sha256(reason.encode()).hexdigest()[:16]
    stripe_ref = f"{CORRECTION_KEY_PREFIX}{org_id}:{period_label}:{reason_digest}"
    ts = ts if ts is not None else time.time()

    with db.immediate(conn):
        existing = conn.execute(
            "SELECT id FROM credit_ledger WHERE org_id=? AND stripe_ref=?",
            (org_id, stripe_ref),
        ).fetchone()
        if existing:
            return {
                "stripe_ref": stripe_ref,
                "delta_micros": delta,
                "delta_usd": db.micros_to_usd(abs(delta)),
                "already_applied": True,
            }

        if delta != 0:
            ledger = db.add_ledger(
                conn, org_id,
                db.micros_to_usd(float(delta)),
                f"Savings-share correction for {period_label}: {reason}",
                stripe_ref=stripe_ref, ts=ts, commit=False,
            )
        else:
            ledger = None

    return {
        "stripe_ref": stripe_ref,
        "delta_micros": delta,
        "delta_usd": db.micros_to_usd(abs(delta)),
        "ledger_entry": dict(ledger) if ledger else None,
        "already_applied": False,
    }


# ----------------------------------------------------------------- invoice evidence (#151) ---
def get_invoice_events(conn, org_id: str, period_label: str) -> list[dict]:
    """Return all usage_events behind a savings-share invoice, with the pricing
    version that was current at billing time so a customer can independently
    reconstruct every dollar billed."""
    return db.get_invoice_events(conn, org_id, period_label)


__all__ = [
    "DEFAULT_RATE_BPS", "rate_bps_from_config", "period_savings",
    "savings_share_report", "bill_savings_share", "SavingsShareReport",
    "month_window", "previous_month_label",
    "DEFAULT_MIN_COVERAGE_PCT", "DEFAULT_MAX_ESTIMATED_PCT",
    "coverage_threshold_from_config", "estimated_threshold_from_config",
    "record_savings_correction", "get_invoice_events",
]
