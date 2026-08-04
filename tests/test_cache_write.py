"""Anthropic cache-creation metering (#135)."""
import importlib.util
import json
from types import SimpleNamespace

from plutus_agent import db, metering, pricing
from plutus_agent.integrations.adapters import track_anthropic


def _org(tmp_path):
    conn = db.connect(str(tmp_path / "plutus.db"))
    db.init_schema(conn)
    org = db.create_org(conn, "Cache Test", tier="pro")["id"]
    return conn, org


def test_schema_v11_and_cache_write_is_stored_and_chained(tmp_path):
    conn, org = _org(tmp_path)
    assert db.get_schema_version(conn) == 17
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(usage_events)")}
    assert "cache_write_tokens" in cols

    result = metering.record_usage(
        conn, org, provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=100, output_tokens=50, cache_write_tokens=1000,
    )
    row = conn.execute(
        "SELECT cache_write_tokens, cost_micros FROM usage_events WHERE id=?",
        (result.event_id,),
    ).fetchone()
    assert row["cache_write_tokens"] == 1000
    expected = pricing.PRICE_TABLE["anthropic"]["claude-sonnet-4-6"].cost_with_cache_write(
        100, 50, 0, 0, 1000)
    assert row["cost_micros"] == db.usd_to_micros(expected)
    assert db.verify_chain(conn, org_id=org)["ok"] is True


def test_anthropic_adapter_passes_cache_creation_tokens(tmp_path):
    conn, org = _org(tmp_path)
    class Meter:
        def track(self, **kwargs):
            return kwargs

    response = SimpleNamespace(
        model="claude-sonnet-4-6",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                              cache_read_input_tokens=20,
                              cache_creation_input_tokens=30),
    )
    event = track_anthropic(Meter(), response)
    assert event["cache_read_tokens"] == 20
    assert event["cache_write_tokens"] == 30


def test_claude_code_hook_keeps_cache_creation_separate():
    path = "integrations/claude-code-plugin/scripts/plutus-meter.py"
    spec = importlib.util.spec_from_file_location("plutus_cc_hook", path)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    lines = [json.dumps({"type": "assistant", "message": {
        "model": "claude-sonnet-4-6",
        "usage": {"input_tokens": 10, "output_tokens": 5,
                  "cache_read_input_tokens": 20,
                  "cache_creation_input_tokens": 30},
    }})]
    assert hook._sum_usage(lines) == {
        "claude-sonnet-4-6": {
            "input": 10, "output": 5, "cache_read": 20, "cache_write": 30,
        }
    }
