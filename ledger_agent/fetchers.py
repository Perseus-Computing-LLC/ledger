"""Provider cost fetchers — pull a period's authoritative spend per provider.

Follow-up to the reconciler (#107): `ledger reconcile` trues metered (estimated)
cost up to a provider's authoritative billing, but the operator had to supply the
per-provider total by hand (JSON/CSV from the provider console). These fetchers
pull that number directly from each provider's own cost API and normalize it to
the reconciler's input shape (`{provider: usd}`), so `ledger close` can run
fetch → reconcile unattended after month end.

Design rules (carried from #107):

- **Never assume missing == zero.** A fetcher either returns a real dollar figure
  from the provider, or raises :class:`FetchError`. It never returns 0.0 as a
  stand-in for "couldn't fetch" — the orchestrator reports that provider as
  *unreconciled* and leaves the ledger untouched, exactly like a missing manual
  total. Fabricating a zero would silently wipe a real charge.
- **Optional and offline-safe.** OpenAI/Anthropic use only stdlib ``urllib`` (no
  new dependency). AWS Bedrock uses ``boto3`` if installed (``pip install
  'ledger-agent[fetchers]'``); absent, its fetcher raises a clear FetchError and
  nothing else is affected. Import this module without any provider SDK present.
- **Auth via the environment**, the same place the meter's provider keys live.
  Cost/usage endpoints need an *admin/organization* key, which is distinct from a
  normal inference key — hence the ``*_ADMIN_KEY`` variables (with a fallback to
  the plain key for setups where they coincide).

The request-building and the response-parsing are split so the parsers are unit
tested against captured sample payloads and the HTTP layer is injectable
(``opener=``) — no network in tests. The endpoint shapes follow each provider's
documented cost-report API; a schema the parser doesn't recognize raises
FetchError rather than guessing a number.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import urllib.error
import urllib.request
from typing import Callable, Optional

# Match the meter client's UA so provider WAFs don't reject the default
# "Python-urllib/x.y" signature.
_UA = "ledger-agent/cost-fetcher"


class FetchError(Exception):
    """A provider total could not be fetched (missing creds, API error, or an
    unrecognized response). The provider is left unreconciled — never zeroed."""


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _http_get(url: str, headers: dict,
              opener: Optional[Callable] = None, timeout: float = 30.0) -> dict:
    """GET a JSON endpoint. ``opener`` (for tests) takes (url, headers) and returns
    a decoded dict; the default performs a real request via urllib."""
    if opener is not None:
        return opener(url, headers)
    req = urllib.request.Request(url, headers={**headers, "User-Agent": _UA}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise FetchError(f"HTTP {e.code} from {url.split('?')[0]}: {detail}") from e
    except urllib.error.URLError as e:
        raise FetchError(f"network error contacting {url.split('?')[0]}: {e.reason}") from e
    except (ValueError, json.JSONDecodeError) as e:
        raise FetchError(f"non-JSON response from {url.split('?')[0]}: {e}") from e


# --------------------------------------------------------------------- OpenAI --
def parse_openai_costs(payload: dict) -> float:
    """Sum USD from an OpenAI organization-costs page.

    Shape (``GET /v1/organization/costs``): ``{"data": [{"results": [{"amount":
    {"value": 1.23, "currency": "usd"}}]}], ...}``.
    """
    if not isinstance(payload, dict) or "data" not in payload:
        raise FetchError("OpenAI costs: response missing 'data' array")
    total = 0.0
    for bucket in payload.get("data") or []:
        for res in (bucket.get("results") or []):
            amt = res.get("amount") or {}
            cur = str(amt.get("currency", "usd")).lower()
            if cur not in ("usd", "$"):
                raise FetchError(f"OpenAI costs: non-USD currency {cur!r} — "
                                 "reconcile in the billed currency, not converted")
            total += float(amt.get("value", 0) or 0)
    return round(total, 6)


def fetch_openai(start_ts: float, end_ts: float, *, api_key: Optional[str] = None,
                 opener: Optional[Callable] = None) -> float:
    key = api_key or os.environ.get("OPENAI_ADMIN_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise FetchError("OpenAI: set OPENAI_ADMIN_KEY (organization/admin key) to "
                         "fetch the costs endpoint")
    total = 0.0
    page: Optional[str] = None
    for _ in range(200):  # safety cap on pagination
        url = ("https://api.openai.com/v1/organization/costs"
               f"?start_time={int(start_ts)}&end_time={int(end_ts)}&limit=180")
        if page:
            url += f"&page={page}"
        data = _http_get(url, {"Authorization": f"Bearer {key}"}, opener=opener)
        total += parse_openai_costs(data)
        if not data.get("has_more"):
            break
        page = data.get("next_page")
        if not page:
            break
    return round(total, 6)


# ------------------------------------------------------------------ Anthropic --
def parse_anthropic_cost_report(payload: dict) -> float:
    """Sum USD from an Anthropic cost-report page.

    Shape (``GET /v1/organizations/cost_report``): ``{"data": [{"results":
    [{"amount": "1.23", "currency": "USD"}]}], ...}``. Amounts may be strings.
    """
    if not isinstance(payload, dict) or "data" not in payload:
        raise FetchError("Anthropic cost_report: response missing 'data' array")
    total = 0.0
    for bucket in payload.get("data") or []:
        for res in (bucket.get("results") or []):
            cur = str(res.get("currency", "USD")).lower()
            if cur not in ("usd", "$"):
                raise FetchError(f"Anthropic cost_report: non-USD currency {cur!r}")
            total += float(res.get("amount", 0) or 0)
    return round(total, 6)


def fetch_anthropic(start_ts: float, end_ts: float, *, api_key: Optional[str] = None,
                    opener: Optional[Callable] = None) -> float:
    key = (api_key or os.environ.get("ANTHROPIC_ADMIN_KEY")
           or os.environ.get("ANTHROPIC_API_KEY"))
    if not key:
        raise FetchError("Anthropic: set ANTHROPIC_ADMIN_KEY (admin key) to fetch "
                         "the cost_report endpoint")
    total = 0.0
    page: Optional[str] = None
    for _ in range(200):
        url = ("https://api.anthropic.com/v1/organizations/cost_report"
               f"?starting_at={_iso(start_ts)}&ending_at={_iso(end_ts)}")
        if page:
            url += f"&page={page}"
        data = _http_get(url, {"x-api-key": key, "anthropic-version": "2023-06-01"},
                         opener=opener)
        total += parse_anthropic_cost_report(data)
        if not data.get("has_more"):
            break
        page = data.get("next_page")
        if not page:
            break
    return round(total, 6)


# ------------------------------------------------------------- AWS Bedrock (CE) --
def fetch_aws_bedrock(start_ts: float, end_ts: float, *, client=None) -> float:
    """Amazon Bedrock spend for the period via the AWS Cost Explorer API.

    Uses ``boto3`` (extra: ``ledger-agent[fetchers]``); credentials come from the
    standard AWS chain. ``client`` is injectable for tests. Filtered to the
    Bedrock service; monthly granularity over [start, end).
    """
    if client is None:
        try:
            import boto3  # optional dependency
        except ImportError as e:
            raise FetchError("AWS Bedrock: boto3 not installed — "
                             "pip install 'ledger-agent[fetchers]'") from e
        client = boto3.client("ce")
    start = _dt.datetime.fromtimestamp(start_ts, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    end = _dt.datetime.fromtimestamp(end_ts, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        resp = client.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "SERVICE",
                                   "Values": ["Amazon Bedrock"]}},
        )
    except Exception as e:  # boto3 raises botocore exceptions; keep it dependency-free
        raise FetchError(f"AWS Cost Explorer error: {e}") from e
    return parse_aws_cost_explorer(resp)


def parse_aws_cost_explorer(resp: dict) -> float:
    """Sum UnblendedCost (USD) across the Cost Explorer result-by-time buckets."""
    if not isinstance(resp, dict) or "ResultsByTime" not in resp:
        raise FetchError("AWS Cost Explorer: response missing 'ResultsByTime'")
    total = 0.0
    for bucket in resp.get("ResultsByTime") or []:
        cost = (bucket.get("Total") or {}).get("UnblendedCost") or {}
        unit = str(cost.get("Unit", "USD")).lower()
        if unit not in ("usd", "$"):
            raise FetchError(f"AWS Cost Explorer: non-USD unit {unit!r}")
        total += float(cost.get("Amount", 0) or 0)
    return round(total, 6)


# --------------------------------------------------------------- orchestration --
# provider name (as stored in usage_events.provider, lowercased) -> fetcher.
# "aws" and "bedrock" both map to the Bedrock Cost Explorer fetcher.
FETCHERS: dict = {
    "openai": fetch_openai,
    "anthropic": fetch_anthropic,
    "bedrock": fetch_aws_bedrock,
    "aws": fetch_aws_bedrock,
}


def available_providers() -> list:
    return sorted(FETCHERS)


def fetch_authoritative(providers, start_ts: float, end_ts: float,
                        *, fetchers: Optional[dict] = None) -> tuple[dict, dict]:
    """Fetch the authoritative USD total for each provider over [start, end).

    Returns ``(totals, errors)``: ``totals`` maps provider -> USD for every
    provider that returned a real figure; ``errors`` maps provider -> message for
    every one that could not be fetched. A provider is NEVER put in ``totals``
    with a fabricated 0.0 — an un-fetchable provider stays out of ``totals`` so
    the reconciler leaves it untouched (never zeroed).
    """
    reg = fetchers or FETCHERS
    totals: dict = {}
    errors: dict = {}
    for prov in providers:
        p = str(prov).lower()
        fn = reg.get(p)
        if fn is None:
            errors[p] = f"no fetcher for provider '{p}' (have: {', '.join(sorted(reg))})"
            continue
        try:
            totals[p] = float(fn(start_ts, end_ts))
        except FetchError as e:
            errors[p] = str(e)
        except Exception as e:  # never let one provider's surprise abort the rest
            errors[p] = f"unexpected error: {e}"
    return totals, errors
