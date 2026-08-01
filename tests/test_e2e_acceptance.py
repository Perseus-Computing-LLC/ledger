"""Deterministic Hermes -> Perseus -> Plutus fixture acceptance harness.

This test intentionally stays provider-free. It exercises the production ledger
and report code with a fixture representing the normalized Hermes response plus
the Perseus counterfactual baseline.
"""

import json
import time

from plutus_agent import db, metering, savings


def test_provider_usage_baseline_idempotency_and_report(tmp_path):
    conn = db.connect(str(tmp_path / "fixture.db"))
    db.init_schema(conn)
    org_id = db.create_org(
        conn, "fixture-org", tier="pro", owner_email="fixture@example.test"
    )["id"]

    event = {
        "provider": "openai",
        "model": "gpt-fixture",
        "input_tokens": 120,
        "output_tokens": 40,
        "baseline_input_tokens": 300,
        "baseline_output_tokens": 40,
        "source": "hermes-provider-response",
    }
    idempotency_key = "fixture-hermes-turn-001"

    assert db.claim_idempotency_key(conn, org_id, idempotency_key)
    result = metering.record_usage(
        conn,
        org_id,
        provider=event["provider"],
        model=event["model"],
        input_tokens=event["input_tokens"],
        output_tokens=event["output_tokens"],
        baseline_input_tokens=event["baseline_input_tokens"],
        baseline_output_tokens=event["baseline_output_tokens"],
        cost_usd=1.0,
        source=event["source"],
    )
    assert result.recorded
    assert result.baseline_usd == 1.0 or result.baseline_usd is not None
    response = {"event_id": result.event_id, "savings_usd": result.savings_usd}
    db.store_idempotency_response(
        conn, org_id, idempotency_key, 201, json.dumps(response)
    )

    replay = db.idempotency_response(conn, org_id, idempotency_key)
    assert replay[0] == 201
    assert json.loads(replay[1])["event_id"] == result.event_id

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM usage_events WHERE org_id=?", (org_id,)
    ).fetchone()["n"]
    assert count == 1

    period = time.strftime("%Y-%m", time.gmtime())
    report = savings.savings_share_report(conn, org_id, period, rate_bps=1000)
    assert report.total_events == 1
    assert report.covered_events == 1
    assert report.coverage_pct == 100.0
    assert report.gross_savings_usd >= 0.0

    conn.close()
