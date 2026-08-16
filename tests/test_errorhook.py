"""Tests for ledger_agent.server.errorhook (optional exception capture)."""
from __future__ import annotations

import json
import sys
import types
import urllib.request

import pytest

from ledger_agent.server import errorhook


@pytest.fixture(autouse=True)
def reset_hook_state(monkeypatch):
    monkeypatch.delenv("LEDGER_SENTRY_DSN", raising=False)
    monkeypatch.delenv("LEDGER_ERROR_WEBHOOK", raising=False)
    errorhook._sentry = None
    errorhook._sentry_init_attempted = False
    errorhook._last_signature.clear()
    errorhook._recent_times.clear()
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)
    yield


def test_capture_is_noop_when_unconfigured():
    # Must not raise and must not touch the network.
    errorhook.capture_exception(ValueError("boom"))
    assert errorhook._sentry is None
    assert errorhook._sentry_init_attempted is True


def test_webhook_posts_json_record(monkeypatch):
    posted = []

    def fake_post(url, record):
        posted.append((url, record))

    monkeypatch.setenv("LEDGER_ERROR_WEBHOOK", "http://127.0.0.1:9/err")
    monkeypatch.setattr(errorhook, "_post", fake_post)
    errorhook.capture_exception(KeyError("missing"), context="GET /v1/usage")
    assert len(posted) == 1
    url, record = posted[0]
    assert url == "http://127.0.0.1:9/err"
    assert record["service"] == "ledger"
    assert record["type"] == "KeyError"
    assert "missing" in record["message"]
    assert "GET /v1/usage" in record["message"]
    json.dumps(record)  # serializable


def test_webhook_throttles_duplicate_signature(monkeypatch):
    posted = []

    monkeypatch.setenv("LEDGER_ERROR_WEBHOOK", "http://127.0.0.1:9/err")
    monkeypatch.setattr(errorhook, "_post", lambda url, record: posted.append(record))
    errorhook.capture_exception(ValueError("same"))
    errorhook.capture_exception(ValueError("same"))
    errorhook.capture_exception(ValueError("different"))
    assert len(posted) == 2  # second identical signature throttled


def test_sentry_lazy_init(monkeypatch):
    calls = {}
    fake = types.SimpleNamespace(
        init=lambda **kw: calls.update(kw),
        capture_exception=lambda exc: calls.setdefault("captured", exc),
    )
    monkeypatch.setenv("LEDGER_SENTRY_DSN", "https://x@example.invalid/1")
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    err = RuntimeError("prod failure")
    errorhook.capture_exception(err)
    assert calls.get("dsn") == "https://x@example.invalid/1"
    assert calls.get("server_name") == "ledger"
    assert calls.get("captured") is err


def test_sentry_import_failure_is_swallowed(monkeypatch):
    monkeypatch.setenv("LEDGER_SENTRY_DSN", "https://x@example.invalid/1")
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)  # ImportError path
    errorhook.capture_exception(ValueError("x"))  # must not raise


def test_handler_captures_request_exceptions(monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer

    from ledger_agent.server import app as appmod

    captured = []
    monkeypatch.setattr(appmod.errorhook, "capture_exception",
                        lambda exc, context=None: captured.append((exc, context)))

    class Boom(appmod.Handler):
        def do_GET(self):
            raise RuntimeError("handler boom")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Boom)
    httpd.ctx = None
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/")
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError:
            pass
        except Exception:
            pass  # the raised RuntimeError closes the connection
        assert len(captured) == 1
        assert isinstance(captured[0][0], RuntimeError)
        assert captured[0][1] == "GET /"
    finally:
        httpd.shutdown()
