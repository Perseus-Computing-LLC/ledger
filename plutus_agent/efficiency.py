"""Efficiency view — the value-vs-actual story, generalized (#8).

Savings-share (:mod:`plutus_agent.savings`) measures ONE kind of efficiency:
routing to a cheaper model than the flagship. But the dogfood reconciliation
showed the bigger story is *sourcing* — running open models locally or on a
subscription for a fraction of the API price. DeepSeek: 3.6B tokens worth ~$227
at API list prices, actually billed $2.56. Claude Code: ~$310 of API-equivalent
usage for ~$59 of credits.

This module computes, per org + period, three token-derived figures from the
published price table plus one ground-truth figure:

* **flagship_value** — every event's tokens priced at its provider's *flagship*
  model. "What this capability would have cost on the best API model." The
  headline value number.
* **list_value** — every event's tokens priced at the *model actually used*'s
  list price. "What your usage would cost at API list prices" (ignores that you
  may have run it locally / on a subscription).
* **metered_cost** — the cost Plutus actually recorded (``cost_micros``). For
  local/free models this is unreliable, which is exactly why we reconcile.
* **actual_paid** — the real amount billed, from the provider console
  (reconciled monthly; passed in here). The truth anchor.

Efficiency = value − actual_paid; the multiple = value / actual_paid. All the
token-derived figures are reconstructable from the chained token counts and the
published table, so the number is verifiable, not asserted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import db, pricing
from .reconcile import month_window, previous_month_label  # noqa: F401 (re-export)

# Provider family -> the flagship a customer would run WITHOUT routing (mirrors
# config ``savings.baseline_models`` and the Hermes sync's built-in map).
DEFAULT_BASELINE_MODELS = {
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-5",
    "google": "gemini-3.1-pro",
    "xai": "grok-4",
    "deepseek": "deepseek-v4-pro",
    "mistral": "mistral-large-2",
    "cohere": "command-a",
    "meta": "llama-4-maverick",
}

_FAMILY_PREFIXES = (
    ("claude", "anthropic"), ("gpt", "openai"), ("o4", "openai"),
    ("gemini", "google"), ("gemma", "google"), ("deepseek", "deepseek"),
    ("grok", "xai"), ("mistral", "mistral"), ("command", "cohere"),
    ("llama", "meta"),
)


def _norm_model(model: Optional[str]) -> str:
    return (model or "").rsplit("/", 1)[-1].strip().lower()


def family_of(provider: Optional[str], model: Optional[str]) -> Optional[str]:
    """Infer the provider family from the MODEL NAME (reliable), falling back to
    the ``provider`` string. Returns a DEFAULT_BASELINE_MODELS key or None."""
    m = _norm_model(model)
    for pre, fam in _FAMILY_PREFIXES:
        if m.startswith(pre):
            return fam
    p = (provider or "").strip().lower()
    return p if p in DEFAULT_BASELINE_MODELS else None


def baseline_models_from_config(cfg: Optional[dict]) -> dict:
    bm = ((cfg or {}).get("savings") or {}).get("baseline_models") if cfg else None
    return {str(k).lower(): str(v) for k, v in (bm or DEFAULT_BASELINE_MODELS).items()}


@dataclass
class EfficiencyReport:
    org_id: str
    period_label: str
    events: int
    tokens: int
    flagship_value_usd: float
    list_value_usd: float
    metered_cost_usd: float
    actual_paid_usd: Optional[float] = None  # from console reconcile, if known
    by_family: dict = field(default_factory=dict)

    @property
    def basis_usd(self) -> float:
        """Actual paid when known, else the metered cost as a fallback basis."""
        return self.actual_paid_usd if self.actual_paid_usd is not None \
            else self.metered_cost_usd

    @property
    def efficiency_usd(self) -> float:
        """Flagship-equivalent value minus what was actually paid (>= 0)."""
        return round(max(0.0, self.flagship_value_usd - self.basis_usd), 6)

    @property
    def multiple(self) -> Optional[float]:
        b = self.basis_usd
        return round(self.flagship_value_usd / b, 2) if b > 0 else None

    def as_dict(self) -> dict:
        return {
            "org_id": self.org_id,
            "period": self.period_label,
            "events": self.events,
            "tokens": self.tokens,
            "flagship_value_usd": round(self.flagship_value_usd, 4),
            "list_value_usd": round(self.list_value_usd, 4),
            "metered_cost_usd": round(self.metered_cost_usd, 4),
            "actual_paid_usd": (None if self.actual_paid_usd is None
                                else round(self.actual_paid_usd, 4)),
            "basis_usd": round(self.basis_usd, 4),
            "efficiency_usd": self.efficiency_usd,
            "multiple": self.multiple,
            "by_family": self.by_family,
        }


def org_efficiency(conn, org_id: str, *, period_label: Optional[str] = None,
                   start_ts: Optional[float] = None, end_ts: Optional[float] = None,
                   baseline_models: Optional[dict] = None,
                   pricing_overrides: Optional[dict] = None,
                   actual_paid_usd: Optional[float] = None) -> EfficiencyReport:
    """Compute the efficiency figures for an org over a period.

    Pass ``period_label`` ('YYYY-MM') OR an explicit [start_ts, end_ts) window
    (period_label wins if both given). ``actual_paid_usd`` is the reconciled
    console total for the period, when known — it becomes the truth basis for the
    efficiency/multiple; otherwise the metered cost is used as a fallback basis.
    """
    if period_label:
        start_ts, end_ts = month_window(period_label)
    bm = {str(k).lower(): str(v) for k, v in
          (baseline_models or DEFAULT_BASELINE_MODELS).items()}

    sql = ("SELECT provider, model, input_tokens, output_tokens, "
           "cache_read_tokens, reasoning_tokens, cost_micros "
           "FROM usage_events WHERE org_id=?")
    params: list = [org_id]
    if start_ts is not None:
        sql += " AND ts >= ?"
        params.append(float(start_ts))
    if end_ts is not None:
        sql += " AND ts < ?"
        params.append(float(end_ts))

    events = tokens = 0
    flag_micros = list_micros = metered_micros = 0
    by_family: dict = {}
    for r in conn.execute(sql, params):
        events += 1
        toks = (int(r["input_tokens"]) + int(r["output_tokens"])
                + int(r["cache_read_tokens"]) + int(r["reasoning_tokens"]))
        tokens += toks
        metered_micros += int(r["cost_micros"] or 0)

        fam = family_of(r["provider"], r["model"])
        # list value: the model actually used, at its published price
        lp, _ = pricing.resolve_price(r["provider"], r["model"], pricing_overrides)
        lv = lp.cost(r["input_tokens"], r["output_tokens"],
                     r["cache_read_tokens"], r["reasoning_tokens"])
        # flagship value: same tokens at the family flagship's price
        flag_model = bm.get(fam) if fam else None
        if flag_model:
            fp, _ = pricing.resolve_price(fam or r["provider"], flag_model,
                                          pricing_overrides)
            fv = fp.cost(r["input_tokens"], r["output_tokens"],
                         r["cache_read_tokens"], r["reasoning_tokens"])
        else:
            fv = lv  # unknown family: no flagship uplift, fall back to list value
        list_micros += db.usd_to_micros(lv)
        flag_micros += db.usd_to_micros(fv)

        key = fam or (r["provider"] or "unknown")
        agg = by_family.setdefault(key, {"events": 0, "tokens": 0,
                                         "flagship_value_usd": 0.0,
                                         "list_value_usd": 0.0})
        agg["events"] += 1
        agg["tokens"] += toks
        agg["flagship_value_usd"] = round(agg["flagship_value_usd"] + fv, 6)
        agg["list_value_usd"] = round(agg["list_value_usd"] + lv, 6)

    return EfficiencyReport(
        org_id=org_id,
        period_label=period_label or "all",
        events=events,
        tokens=tokens,
        flagship_value_usd=db.micros_to_usd(flag_micros),
        list_value_usd=db.micros_to_usd(list_micros),
        metered_cost_usd=db.micros_to_usd(metered_micros),
        actual_paid_usd=actual_paid_usd,
        by_family=by_family,
    )


__all__ = ["EfficiencyReport", "org_efficiency", "family_of",
           "baseline_models_from_config", "DEFAULT_BASELINE_MODELS",
           "month_window", "previous_month_label"]
