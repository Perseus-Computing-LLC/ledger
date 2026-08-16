"""Optional exception capture for server deployments. No-op by default.

Perseus Ledger is local-first: nothing leaves the process unless the
deployment opts in through one of:

- ``LEDGER_SENTRY_DSN`` — lazily initialises ``sentry-sdk`` on first capture
  (requires the optional extra: ``pip install ledger-agent[sentry]``).
- ``LEDGER_ERROR_WEBHOOK`` — POSTs a compact JSON record to the given URL on
  every (throttled) capture. The payload is ntfy-compatible
  (``title``/``message``/``priority``), so a self-hosted ntfy topic works with
  no additional service.

Captures are throttled per-signature (default cooldown 120 s) and capped at
``MAX_CAPTURES_PER_MINUTE`` process-wide so an error storm cannot become an
alert storm. ``capture_exception`` never raises: monitoring failure must not
take the server down.

Scope is the HTTP server (``ledger_agent/server/app.py``); library errors keep
surfacing to the embedding application as they always have.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

COOLDOWN_SECONDS = 120.0
MAX_CAPTURES_PER_MINUTE = 10
_WEBHOOK_TIMEOUT = 5.0

_sentry = None
_sentry_init_attempted = False
_lock = threading.Lock()
_last_signature: dict = {}
_recent_times: list = []


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _sentry_dsn() -> str:
    return _env("LEDGER_SENTRY_DSN")


def _webhook_url() -> str:
    return _env("LEDGER_ERROR_WEBHOOK")


def _init_sentry() -> None:
    """Import + init sentry_sdk once, lazily, only when a DSN is configured."""
    global _sentry, _sentry_init_attempted
    if _sentry_init_attempted:
        return
    _sentry_init_attempted = True
    if not _sentry_dsn():
        return
    try:
        import sentry_sdk  # type: ignore[import-not-found]

        sentry_sdk.init(dsn=_sentry_dsn(), server_name="ledger")
        _sentry = sentry_sdk
    except Exception:
        _sentry = None


def _throttled(signature: str) -> bool:
    now = time.monotonic()
    with _lock:
        _recent_times[:] = [t for t in _recent_times if now - t < 60.0]
        if len(_recent_times) >= MAX_CAPTURES_PER_MINUTE:
            return True
        if signature in _last_signature and now - _last_signature[signature] < COOLDOWN_SECONDS:
            return True
        _recent_times.append(now)
        _last_signature[signature] = now
    return False


def _signature(exc: BaseException) -> str:
    return f"{type(exc).__name__}:{str(exc)[:160]}"


def _record(exc: BaseException, context: str | None) -> dict:
    tb = exc.__traceback__
    frame = "?"
    line = 0
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    if tb is not None:
        frame = tb.tb_frame.f_code.co_filename
        line = tb.tb_lineno
    message = f"{type(exc).__name__}: {str(exc)[:200]}"
    if context:
        message += f" ({context})"
    return {
        "service": "ledger",
        "title": message[:160],
        "message": message,
        "type": type(exc).__name__,
        "file": frame,
        "line": line,
        "context": context or "",
        "priority": 4,
        "tags": ["ledger", "error"],
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _post(url: str, record: dict) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(record).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT):
        pass


def capture_exception(exc: BaseException, context: str | None = None) -> None:
    """Capture an exception if (and only if) a backend is configured."""
    try:
        _init_sentry()
        if _sentry is not None:
            _sentry.capture_exception(exc)
        hook = _webhook_url()
        if hook and not _throttled(_signature(exc)):
            _post(hook, _record(exc, context))
    except Exception:
        pass
