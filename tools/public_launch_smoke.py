#!/usr/bin/env python3
"""Public launch contract smoke test for Ledger."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlencode


class SmokeFailure(RuntimeError):
    pass


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: object


def _request(base: str, method: str, path: str, *, payload=None,
             token: str | None = None, idem: str | None = None) -> Response:
    url = base.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json, text/html", "User-Agent": "ledger-public-smoke/1"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if idem:
        headers["Idempotency-Key"] = idem
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(256_000)
            status = resp.status
            response_headers = dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read(16_000)
        status = exc.code
        response_headers = dict(exc.headers.items())
    except Exception as exc:
        raise SmokeFailure(f"{method} {path}: transport error ({type(exc).__name__})") from exc
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text)
    except ValueError:
        body = text
    return Response(status, response_headers, body)


def _fail(path: str, detail: str) -> None:
    raise SmokeFailure(f"{path}: {detail}")


def _expect_status(resp: Response, path: str, *expected: int) -> None:
    if resp.status not in expected:
        _fail(path, f"HTTP {resp.status}, expected {'/'.join(map(str, expected))}")


def _expect_text(body: object, path: str, *terms: str) -> None:
    text = body if isinstance(body, str) else json.dumps(body, sort_keys=True)
    folded = text.casefold()
    missing = [term for term in terms if term.casefold() not in folded]
    if missing:
        _fail(path, "missing contract terms: " + ", ".join(missing))


def _pricing_contract(base: str) -> None:
    resp = _request(base, "GET", "/pricing")
    _expect_status(resp, "/pricing", 200)
    _expect_text(resp.body, "/pricing", "Free", "10", "5%", "Team", "$20", "Enterprise")
    print("PASS | /pricing | Free/Team/Enterprise contract present")


def _health_contract(base: str) -> None:
    resp = _request(base, "GET", "/healthz")
    _expect_status(resp, "/healthz", 200)
    if not isinstance(resp.body, dict) or resp.body.get("ok") is not True:
        _fail("/healthz", "response does not report ok=true")
    print("PASS | /healthz | service healthy")


def _authenticated_contract(base: str, admin_token: str) -> None:
    stamp = str(int(time.time()))
    org_resp = _request(base, "POST", "/v1/admin/orgs",
                        payload={"name": f"public-smoke-{stamp}", "tier": "free"},
                        token=admin_token)
    _expect_status(org_resp, "/v1/admin/orgs", 201)
    if not isinstance(org_resp.body, dict) or not org_resp.body.get("id"):
        _fail("/v1/admin/orgs", "response omitted org id")
    org_id = str(org_resp.body["id"])

    key_resp = _request(base, "POST", "/v1/admin/keys",
                        payload={"org_id": org_id, "name": "public-smoke"},
                        token=admin_token)
    _expect_status(key_resp, "/v1/admin/keys", 201)
    secret = key_resp.body.get("secret") if isinstance(key_resp.body, dict) else None
    if not secret:
        _fail("/v1/admin/keys", "response omitted one-time API secret")

    event = {"provider": "smoke", "model": "fixture", "input_tokens": 1,
             "output_tokens": 1, "cost_usd": 0.01, "source": "public-smoke"}
    usage_resp = _request(base, "POST", "/v1/usage", payload=event, token=secret,
                          idem=f"public-smoke-{stamp}")
    _expect_status(usage_resp, "/v1/usage", 200)
    if not isinstance(usage_resp.body, dict) or usage_resp.body.get("recorded") is False:
        _fail("/v1/usage", "usage event was not recorded")
    print("PASS | /v1/usage | disposable Free usage recorded")

    audit_query = "/api/audit?" + urlencode({"org": org_id})
    audit_resp = _request(base, "GET", audit_query)
    if audit_resp.status == 200:
        if not isinstance(audit_resp.body, dict):
            _fail(audit_query, "receipt was not JSON")
        for key in ("ledger_integrity", "savings", "verification"):
            if key not in audit_resp.body:
                _fail(audit_query, f"receipt missing {key}")
        print("PASS | /api/audit | Free receipt contains integrity/savings/verification")
    elif audit_resp.status in (401, 403):
        print("WARN | /api/audit | customer session required; admin chain check follows")
    else:
        _fail(audit_query, f"HTTP {audit_resp.status}, expected 200 or auth boundary")

    verify_query = "/v1/admin/verify?" + urlencode({"org": org_id})
    verify_resp = _request(base, "GET", verify_query, token=admin_token)
    _expect_status(verify_resp, verify_query, 200)
    if not isinstance(verify_resp.body, dict) or verify_resp.body.get("ok") is not True:
        _fail(verify_query, "ledger verification did not report ok=true")
    print("PASS | /v1/admin/verify | disposable ledger integrity verified")


def main() -> int:
    base = os.environ.get("LEDGER_SMOKE_BASE_URL", "").strip()
    if not base:
        print("SKIP | public smoke | set LEDGER_SMOKE_BASE_URL to enable")
        return 0
    try:
        _health_contract(base)
        _pricing_contract(base)
        admin_token = os.environ.get("LEDGER_SMOKE_ADMIN_TOKEN", "").strip()
        if admin_token:
            _authenticated_contract(base, admin_token)
        else:
            print("SKIP | authenticated contract | LEDGER_SMOKE_ADMIN_TOKEN not set")
        print("RESULT=PASS")
        return 0
    except SmokeFailure as exc:
        print(f"FAIL | {exc}")
        print("RESULT=FAIL")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
