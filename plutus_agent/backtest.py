"""Replay the append-only usage ledger against current pricing logic (#145)."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from . import db, pricing


@dataclass(frozen=True)
class EventCheck:
    event_id: str
    recorded_micros: int
    predicted_micros: int
    delta_micros: int
    estimated: bool
    ok: bool


@dataclass(frozen=True)
class BacktestReport:
    org_id: str
    events: int
    checked: int
    mismatches: int
    tolerance_micros: int
    passed: bool
    checks: tuple[EventCheck, ...]

    def as_dict(self) -> dict:
        out = asdict(self)
        out["checks"] = [asdict(c) for c in self.checks]
        return out


def replay(conn, org_id: str, *, pricing_overrides: Optional[dict] = None,
           tolerance_micros: int = 0) -> BacktestReport:
    """Replay all historical events for ``org_id`` without writing to the DB.

    Exact-cost events are accepted as authoritative outcomes. Estimated events
    are re-priced from the current catalog and every stored token dimension,
    including cache reads/writes and reasoning tokens. A mismatch means the
    current billing logic would change a known historical outcome.
    """
    rows = conn.execute(
        "SELECT * FROM usage_events WHERE org_id=? ORDER BY rowid", (org_id,)
    ).fetchall()
    checks = []
    tol = max(0, int(tolerance_micros))
    columns = set(rows[0].keys()) if rows else set()
    for row in rows:
        recorded = int(row["cost_micros"] or 0)
        if row["estimated"]:
            price, _ = pricing.resolve_price(
                row["provider"], row["model"], pricing_overrides)
            predicted = db.usd_to_micros(price.cost_with_cache_write(
                int(row["input_tokens"] or 0), int(row["output_tokens"] or 0),
                int(row["cache_read_tokens"] or 0), int(row["reasoning_tokens"] or 0),
                int(row["cache_write_tokens"] or 0) if "cache_write_tokens" in columns else 0,
            ))
        else:
            predicted = recorded
        delta = predicted - recorded
        checks.append(EventCheck(
            event_id=row["id"], recorded_micros=recorded,
            predicted_micros=predicted, delta_micros=delta,
            estimated=bool(row["estimated"]), ok=abs(delta) <= tol,
        ))
    mismatches = sum(not c.ok for c in checks)
    return BacktestReport(
        org_id=org_id, events=len(rows), checked=len(checks),
        mismatches=mismatches, tolerance_micros=tol,
        passed=mismatches == 0, checks=tuple(checks),
    )


def assert_clean(conn, org_id: str, **kwargs) -> BacktestReport:
    """Run ``replay`` and raise before a deployment/write workflow proceeds."""
    report = replay(conn, org_id, **kwargs)
    if not report.passed:
        bad = next(c for c in report.checks if not c.ok)
        raise ValueError(
            f"metering backtest failed at {bad.event_id}: "
            f"recorded={bad.recorded_micros} predicted={bad.predicted_micros}"
        )
    return report
