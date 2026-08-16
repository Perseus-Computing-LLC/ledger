import copy
import hashlib
import json
from collections import Counter

from ledger_agent.tool_receipts import (
    HALLUCINATION_TYPES,
    ToolReceiptLedger,
    build_tool_receipt,
    generate_benchmark_scenarios,
    receipt_to_evidence_hash,
    render_claim,
    render_verification_block,
    verify_claim,
    verify_response,
    verify_tool_receipt,
)


KEY = b"tool-receipt-test-secret"
REGISTRY = {"tool-key": KEY}


def make_receipt(**overrides):
    values = {
        "tool_name": "email_search",
        "input_params": {"query": "Alice"},
        "raw_output": '{"sender":"Alice","subject":"Deadline update"}',
        "result_count": 2,
        "facts": {"sender": "Alice", "subject": "Deadline update", "count": 2},
        "duration_ms": 12,
        "key_id": "tool-key",
        "key": KEY,
        "timestamp_ms": 1708300000000,
        "id": "receipt-1",
    }
    values.update(overrides)
    return build_tool_receipt(**values)


def make_ledger(*receipts):
    ledger = ToolReceiptLedger(key_registry=REGISTRY)
    for receipt in receipts:
        ledger.register(receipt)
    return ledger


def test_build_and_verify_tool_receipt_signature():
    receipt = make_receipt()
    assert list(receipt) == [
        "schema",
        "id",
        "tool_name",
        "input_hash",
        "output_hash",
        "result_count",
        "facts",
        "timestamp_ms",
        "duration_ms",
        "key_id",
        "signature",
    ]
    ok, errors = verify_tool_receipt(receipt, REGISTRY)
    assert ok is True
    assert errors == []
    facts_json = json.dumps(receipt["facts"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload = "|".join(
        [
            receipt["id"],
            receipt["tool_name"],
            receipt["input_hash"],
            receipt["output_hash"],
            str(receipt["result_count"]),
            facts_json,
            str(receipt["timestamp_ms"]),
        ]
    ).encode()
    assert receipt["signature"] == __import__("hmac").new(KEY, payload, hashlib.sha256).hexdigest()


def test_wrong_key_fails_verification():
    ok, errors = verify_tool_receipt(make_receipt(), {"tool-key": b"wrong"})
    assert ok is False
    assert "signature_invalid" in errors


def test_tampering_any_signed_field_fails_verification():
    for field, value in (
        ("tool_name", "calendar_search"),
        ("input_hash", "0" * 64),
        ("output_hash", "1" * 64),
        ("timestamp_ms", 1708300000001),
        ("result_count", 99),
        ("facts", {"sender": "Mallory"}),
    ):
        tampered = copy.deepcopy(make_receipt())
        tampered[field] = value
        ok, errors = verify_tool_receipt(tampered, REGISTRY)
        assert ok is False, field
        assert "signature_invalid" in errors, (field, errors)


def test_ledger_issue_get_ids_and_unknown_verification():
    ledger = ToolReceiptLedger(key_registry=REGISTRY)
    receipt = ledger.issue(
        tool_name="email_search",
        input_params={"query": "Alice"},
        raw_output="[]",
        result_count=0,
        facts={},
        duration_ms=1,
        key_id="tool-key",
        key=KEY,
        timestamp_ms=1,
        id="issue-1",
    )
    assert ledger.get("issue-1") == receipt
    assert ledger.all_ids() == ["issue-1"]
    assert ledger.verify("issue-1") == (True, [])
    assert ledger.verify("missing") == (False, ["unknown_receipt"])


def test_pratyaksha_claim_is_verified():
    receipt = make_receipt()
    verdict = verify_claim(
        {
            "text": "Alice sent two emails.",
            "pramana": "pratyaksha",
            "receipt_id": receipt["id"],
            "expected_count": 2,
            "expected_facts": {"sender": "Alice"},
        },
        make_ledger(receipt),
    )
    assert verdict["status"] == "verified"
    assert verdict["trust_level"] == "fully_verified"
    assert verdict["hallucination_type"] is None


def test_fabricated_call_detection():
    verdict = verify_claim(
        {"text": "The tool returned results.", "pramana": "pratyaksha", "receipt_id": "does-not-exist"},
        make_ledger(make_receipt()),
    )
    assert verdict["status"] == "flagged"
    assert verdict["hallucination_type"] == "fabricated_call"
    assert verdict["trust_level"] == "unreliable"


def test_count_mismatch_detection():
    receipt = make_receipt()
    verdict = verify_claim(
        {"text": "Alice sent five emails.", "pramana": "pratyaksha", "receipt_id": receipt["id"], "expected_count": 5},
        make_ledger(receipt),
    )
    assert verdict["hallucination_type"] == "count_mismatch"
    assert verdict["status"] == "flagged"


def test_fact_mismatch_detection():
    receipt = make_receipt()
    verdict = verify_claim(
        {"text": "Bob sent the email.", "pramana": "pratyaksha", "receipt_id": receipt["id"], "expected_facts": {"sender": "Bob"}},
        make_ledger(receipt),
    )
    assert verdict["hallucination_type"] == "fact_mismatch"
    assert verdict["status"] == "flagged"


def test_inference_as_fact_detection():
    receipt = make_receipt()
    verdict = verify_claim(
        {
            "text": "Alice seems worried.",
            "pramana": "pratyaksha",
            "receipt_id": receipt["id"],
            "premise_facts": ["sender", "subject"],
        },
        make_ledger(receipt),
    )
    assert verdict["hallucination_type"] == "inference_as_fact"
    assert verdict["status"] == "flagged"


def test_false_absence_detection():
    receipt = make_receipt()
    verdict = verify_claim(
        {"text": "No emails were found.", "pramana": "abhava", "receipt_id": receipt["id"]},
        make_ledger(receipt),
    )
    assert verdict["hallucination_type"] == "false_absence"
    assert verdict["status"] == "flagged"


def test_shabda_source_fabrication_and_fetched_source():
    receipt = make_receipt(
        tool_name="web_fetch",
        input_params={"url": "https://example.com/article"},
        raw_output="article",
        result_count=1,
        facts={"source_url": "https://example.com/article"},
        id="web-1",
    )
    ledger = make_ledger(receipt)
    good = verify_claim(
        {
            "text": "According to the article, ...",
            "pramana": "shabda",
            "receipt_id": receipt["id"],
            "cited_source_url": "https://example.com/article",
        },
        ledger,
    )
    bad = verify_claim(
        {
            "text": "According to a fabricated article, ...",
            "pramana": "shabda",
            "receipt_id": receipt["id"],
            "cited_source_url": "https://example.com/missing",
        },
        ledger,
    )
    assert good["status"] == "verified"
    assert bad["hallucination_type"] == "source_fabrication"
    assert bad["status"] == "flagged"


def test_anumana_premises_and_upamana_comparison():
    receipt = make_receipt()
    ledger = make_ledger(receipt)
    inference = verify_claim(
        {
            "text": "Alice appears to be discussing a deadline.",
            "pramana": "anumana",
            "receipt_id": receipt["id"],
            "premise_facts": [{"sender": "Alice"}, {"subject": "Deadline update"}],
        },
        ledger,
    )
    comparison = verify_claim(
        {
            "text": "Alice's message is like the deadline update.",
            "pramana": "upamana",
            "receipt_id": receipt["id"],
            "premise_facts": ["sender", "subject"],
        },
        ledger,
    )
    missing = verify_claim(
        {"text": "This inference has no basis.", "pramana": "anumana", "receipt_id": receipt["id"], "premise_facts": ["missing"]},
        ledger,
    )
    assert inference["status"] == "verified"
    assert inference["trust_level"] == "mostly_verified"
    assert comparison["status"] == "verified"
    assert comparison["trust_level"] == "partial"
    assert missing["hallucination_type"] == "inference_as_fact"


def test_ungrounded_claim_is_unverifiable():
    verdict = verify_claim({"text": "It will rain tomorrow.", "pramana": "ungrounded", "receipt_id": None}, make_ledger())
    assert verdict["status"] == "unverifiable"
    assert verdict["trust_level"] == "ungrounded"
    assert verdict["hallucination_type"] is None


def test_verify_response_summary_and_omitted_receipts():
    used = make_receipt(id="used")
    omitted = make_receipt(id="omitted", input_params={"query": "Bob"})
    ledger = make_ledger(used, omitted)
    result = verify_response(
        [{"text": "Alice sent two emails.", "pramana": "pratyaksha", "receipt_id": "used", "expected_count": 2}],
        ledger,
    )
    assert result["summary"] == {
        "total": 1,
        "verified": 1,
        "flagged": 0,
        "unverifiable": 0,
        "by_type": {key: 0 for key in HALLUCINATION_TYPES},
    }
    assert result["omitted_receipts"] == ["omitted"]


def test_rendering_and_evidence_hash_are_stable():
    receipt = make_receipt()
    verdict = verify_claim(
        {"text": "Alice sent two emails.", "pramana": "pratyaksha", "receipt_id": receipt["id"], "expected_count": 2},
        make_ledger(receipt),
    )
    line = render_claim(verdict)
    block = render_verification_block({"claims": [verdict], "summary": {"total": 1, "verified": 1, "flagged": 0, "unverifiable": 0, "by_type": {}}, "omitted_receipts": []})
    assert line == "— Alice sent two emails. [pratyaksha · fully_verified]"
    assert "Verification" in block
    assert receipt_to_evidence_hash(receipt) == receipt_to_evidence_hash(copy.deepcopy(receipt))
    assert len(receipt_to_evidence_hash(receipt)) == 64


def test_benchmark_generator_is_deterministic_and_balanced():
    first = generate_benchmark_scenarios(seed=251)
    second = generate_benchmark_scenarios(seed=251)
    assert first == second
    assert len(first) == 1800
    counts = Counter(item["hallucination_type"] for item in first)
    assert counts["clean"] == 600
    for kind in HALLUCINATION_TYPES:
        assert counts[kind] == 200
    assert Counter(item["language"] for item in first) == {"en": 450, "es": 450, "fr": 450, "hi": 450}
