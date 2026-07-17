"""Import and freshness helpers for the maintained LiteLLM price catalog.

The checked-in :mod:`pricing` table remains the conservative runtime default.
This module provides an explicit, reviewable import path for operators who want
to refresh prices from LiteLLM's community catalog and then pass the resulting
mapping through ``pricing.overrides``.
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import date
from pathlib import Path
from typing import Optional

LITELLM_CATALOG_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
DEFAULT_MAX_AGE_DAYS = 45


def _per_million(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value) * 1_000_000
    except (TypeError, ValueError):
        return None


def _provider(model: str, entry: dict) -> str:
    provider = str(entry.get("litellm_provider") or "").lower()
    if provider:
        return provider
    prefix = model.split("/", 1)[0].lower() if "/" in model else ""
    return {"gemini": "google", "vertex_ai": "google"}.get(prefix, prefix or "_default")


def import_catalog(payload: dict, *, imported_at: Optional[str] = None) -> dict:
    """Map LiteLLM's catalog JSON into Plutus pricing override shape.

    Entries without a usable input or output rate are skipped. Values remain
    floats in USD per million tokens, matching :class:`pricing.ModelPrice`.
    """
    out: dict[str, dict[str, dict[str, float]]] = {}
    for model, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        inp = _per_million(entry.get("input_cost_per_token"))
        outp = _per_million(entry.get("output_cost_per_token"))
        if inp is None or outp is None:
            continue
        provider = _provider(str(model), entry)
        item = {"input": inp, "output": outp}
        for src, dst in (
            ("cache_read_input_token_cost", "cache_read"),
            ("cache_creation_input_token_cost", "cache_write"),
        ):
            value = _per_million(entry.get(src))
            if value is not None:
                item[dst] = value
        out.setdefault(provider, {})[str(model)] = item
    return {"as_of": imported_at or date.today().isoformat(), "models": out}


def fetch_catalog(url: str = LITELLM_CATALOG_URL, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "plutus-price-catalog"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return import_catalog(json.loads(response.read().decode("utf-8")))


def catalog_is_fresh(as_of: str, max_age_days: int = DEFAULT_MAX_AGE_DAYS,
                     now: Optional[float] = None) -> bool:
    """Return whether an ISO date is no older than ``max_age_days``."""
    try:
        age = (date.fromtimestamp(now or time.time()) - date.fromisoformat(as_of)).days
    except (TypeError, ValueError):
        return False
    return 0 <= age <= int(max_age_days)


def load_catalog(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_catalog(catalog: dict, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target
