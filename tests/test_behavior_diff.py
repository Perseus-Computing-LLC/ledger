"""Deterministic agent-run behavior diff gate (#238).

Success criteria:
- byte-identical snapshots across key-order/whitespace/CRLF variants;
- exit codes 2/1/0 verified (2 = regression, 1 = integrity, 0 = clean);
- a snapshot digest referenced in a receipt re-verifies against the
  snapshot (inclusion evidence);
- no network access (stdlib-only module: hashlib/json/os — nothing here
  opens a socket); bounded input cap with an explicit error.
"""
import json
import time

import pytest

from ledger_agent import behavior_diff, cli, db, metering
from ledger_agent.receipts import build_behavior_snapshot_pin
from ledger_agent.server.api import audit_json

BASE = {
    "kind": "latticeag.viscompile.transcript", "schema_version": 1,
    "cases": [
        {"id": "cap", "input": "Capital of France?",
         "events": [{"type": "tool_call", "name": "lookup",
                     "arguments": {"query": "capital of France", "limit": 5}},
                    {"type": "final", "output": "Paris"}]},
        {"id": "num", "input": "x",
         "events": [{"type": "final", "output": "1000.0"}]},
    ],
}


def _run_diff(argv):
    """Run the CLI in-process and return (exit_code, stdout)."""
    import io
    import contextlib
    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        try:
            cli.main(argv)
            code = 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, buf.getvalue() + err.getvalue()


# ── canonicalization invariance ─────────────────────────────────────────────


def test_byte_identical_snapshots_across_key_order_whitespace_crlf():
    compact = json.dumps(BASE, separators=(",", ":"))
    reordered = ('{ "schema_version": 1, '
                 '"cases": [ { "events": [ { "arguments": { "limit": 5, '
                 '"query": "capital of France" }, "name": "lookup", '
                 '"type": "tool_call" }, { "output": "Paris", '
                 '"type": "final" } ], "id": "cap", '
                 '"input": "Capital of France?" }, { "events": [ { "output": '
                 '"1000.0", "type": "final" } ], "id": "num", "input": "x" } ], '
                 '"kind": "latticeag.viscompile.transcript" }')
    pretty_crlf = json.dumps(BASE, indent=2).replace("\\n", "\\r\\n")

    d1 = behavior_diff.compile_snapshot(compact)
    d2 = behavior_diff.compile_snapshot(reordered)
    d3 = behavior_diff.compile_snapshot(pretty_crlf)
    assert d1["digest"] == d2["digest"] == d3["digest"]
    # the digest covers canonical bytes, not the raw input: a reordered
    # transcript hashes identically only because canonicalization runs first
    raw_digest = __import__("hashlib").sha256(compact.encode()).hexdigest()
    assert d1["digest"] != raw_digest
    report = behavior_diff.diff_snapshots(d1, d2)
    assert report["verdict"] == "clean"
    assert report["regressions"] == []


def test_file_and_inline_sources_agree(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps(BASE, indent=4) + "\n", encoding="utf-8")
    from_file = behavior_diff.compile_snapshot(str(p))
    from_obj = behavior_diff.compile_snapshot(BASE)
    assert from_file["digest"] == from_obj["digest"]


# ── conservative classification ─────────────────────────────────────────────


def test_removed_and_changed_are_regressions():
    target = json.loads(json.dumps(BASE))
    target["cases"] = [c for c in target["cases"] if c["id"] != "num"]
    report = behavior_diff.diff_sources(BASE, target)
    assert report["verdict"] == "regression"
    assert {"case": "num", "change": "removed"} in report["regressions"]

    changed = json.loads(json.dumps(BASE))
    changed["cases"][1]["events"][0]["output"] = "1001.0"
    report = behavior_diff.diff_sources(BASE, changed)
    assert report["verdict"] == "regression"
    assert {"case": "num", "change": "changed"} in report["regressions"]


def test_added_cases_are_not_regressions():
    target = json.loads(json.dumps(BASE))
    target["cases"].append({"id": "extra", "input": "y",
                            "events": [{"type": "final", "output": "z"}]})
    report = behavior_diff.diff_sources(BASE, target)
    assert report["verdict"] == "clean"
    assert report["additions"] == ["extra"]


def test_baseline_error_to_final_output_is_improvement():
    base = {"kind": "t", "cases": [
        {"id": "a", "input": "x", "events": [{"type": "error", "error": "boom"}]},
    ]}
    target = {"kind": "t", "cases": [
        {"id": "a", "input": "x", "events": [{"type": "final", "output": "ok"}]},
    ]}
    report = behavior_diff.diff_sources(base, target)
    assert report["verdict"] == "improvement_only"
    assert report["improvements"] == ["a"]


# ── exit-code taxonomy ──────────────────────────────────────────────────────


def test_exit_codes_2_1_0(tmp_path):
    # 0 = clean
    code, _ = _run_diff(["diff", json.dumps(BASE), json.dumps(BASE)])
    assert code == 0

    # 2 = regression (with --fail-on-regression)
    target = json.loads(json.dumps(BASE))
    target["cases"].pop()
    code, _ = _run_diff(["diff", json.dumps(BASE), json.dumps(target),
                         "--fail-on-regression"])
    assert code == 2

    # 1 = integrity mismatch (pinned digest does not match the snapshot)
    code, out = _run_diff(["diff", json.dumps(BASE), json.dumps(BASE),
                           "--require-baseline-digest", "sha256:" + "0" * 64])
    assert code == 1
    assert "integrity failure" in out

    # 1 = oversized input (explicit bounded-input error)
    big = json.dumps({"cases": [{"id": f"c{i}", "input": "x"} for i in range(10**6)]})
    small_cap = 1024
    with pytest.raises(behavior_diff.IntegrityError):
        behavior_diff.compile_snapshot(big, max_bytes=small_cap)


def test_no_partial_out_write_on_integrity_failure(tmp_path):
    out = tmp_path / "report.json"
    target = json.loads(json.dumps(BASE))
    target["cases"].pop()
    _run_diff(["diff", json.dumps(BASE), json.dumps(target),
               "--require-target-digest", "sha256:" + "f" * 64, "--out", str(out)])
    assert not out.exists()

    # a clean run writes the report atomically
    _run_diff(["diff", json.dumps(BASE), json.dumps(BASE), "--out", str(out)])
    assert out.exists()
    written = json.loads(out.read_text())
    assert written["verdict"] == "clean"
    assert written["baseline_digest"] == written["target_digest"]


def test_regression_still_writes_out_report(tmp_path):
    out = tmp_path / "report.json"
    target = json.loads(json.dumps(BASE))
    target["cases"].pop()
    code, _ = _run_diff(["diff", json.dumps(BASE), json.dumps(target),
                         "--fail-on-regression", "--out", str(out)])
    assert code == 2
    assert out.exists()
    assert json.loads(out.read_text())["verdict"] == "regression"


# ── receipt pinning / inclusion evidence ────────────────────────────────────


def test_snapshot_digest_pinned_in_receipt_reverifies(tmp_path):
    conn = db.connect(str(tmp_path / "pin.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "pin-org", tier="free")["id"]
    snap = behavior_diff.compile_snapshot(BASE)
    pin = build_behavior_snapshot_pin(digest=snap["digest"], cases=2,
                                      source_ref="ci/run-1")
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="deploy", external_ref="task-pin",
        input_tokens=10, output_tokens=5, cost_usd=0.1, ts=time.time(),
        behavior_snapshot=pin,
    )
    receipt = audit_json(conn, org_id, external_ref="task-pin")
    ev = receipt["events"][0]
    assert ev["behavior_snapshot"]["digest"] == snap["digest"]
    evidence = receipt["verification"]["evidence"]["behavior_snapshot"]
    assert evidence["present"] is True
    assert evidence["level"] == "inclusion"
    assert snap["digest"] in evidence["digests"]
    assert evidence["reverify"][0].startswith("ledger diff --require-target-digest")

    # the pinned digest re-verifies against the retained snapshot...
    same_file = tmp_path / "snap.json"
    same_file.write_text(json.dumps(BASE), encoding="utf-8")
    code, _ = _run_diff(["diff", str(same_file), str(same_file),
                         "--require-target-digest", "sha256:" + snap["digest"]])
    assert code == 0

    # ...and fails (exit 1) against a different snapshot
    other_file = tmp_path / "other.json"
    other_file.write_text(json.dumps({"cases": [{"id": "z", "input": "nope"}]}),
                          encoding="utf-8")
    code, _ = _run_diff(["diff", str(same_file), str(other_file),
                         "--require-target-digest", "sha256:" + snap["digest"]])
    assert code == 1
    conn.close()


def test_unpinned_receipt_reports_snapshot_absent(tmp_path):
    conn = db.connect(str(tmp_path / "nopin.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "nopin-org", tier="free")["id"]
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="deploy", external_ref="task-nopin",
        input_tokens=10, output_tokens=5, cost_usd=0.1, ts=time.time(),
    )
    receipt = audit_json(conn, org_id, external_ref="task-nopin")
    evidence = receipt["verification"]["evidence"]["behavior_snapshot"]
    assert evidence["present"] is False
    assert evidence["level"] is None
    conn.close()
