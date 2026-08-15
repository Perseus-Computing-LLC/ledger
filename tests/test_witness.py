"""External witness countersignatures over ledger heads (#240).

Success criteria (1f916 SPEC §8 semantics):
- a fabricated head cannot earn ``witnessed``;
- a witness that saw only an older head cannot attest a newer one without
  a continuity proof;
- witness keys pinned out-of-band; unsigned copies corroborate only;
- refusal lines are evidence against the head;
- asked-and-empty is a distinct verdict/exit code from never-asked;
- every run prints what passing does NOT prove.
"""
import json
import os
import time

import pytest

from ledger_agent import cli, db, metering, witness

WKEY = b"witness-a-32-byte-key-material!!"
WKEY2 = b"witness-b-32-byte-key-material!!"
ORG = "witness-org"


def _head(head_hash="a" * 64, through=3, prev=None):
    return witness.build_head(org_id=ORG, head_hash=head_hash,
                              through_rowid=through, prev_head_hash=prev)


def _copy(head, wid="witness-a", key=WKEY, prev=None):
    return witness.countersign_head(head, witness_id=wid, key=key,
                                    prev_head_hash=prev)


def _verify(head, copies, pinned, *, asked=False, chain_ok=True, prior=None):
    return witness.verify_witnesses(
        head=head, copies=copies, pinned_keys=pinned, asked=asked,
        prior_copies=prior, chain_ok=chain_ok)


# ── verdict grading ─────────────────────────────────────────────────────────


def test_real_head_with_pinned_countersignature_is_witnessed():
    head = _head()
    copy = _copy(head)
    result = _verify(head, [copy], {"witness-a": WKEY})
    assert result["verdict"] == "witnessed"
    assert result["exit_code"] == 0
    assert result["does_not_prove"]  # every run prints what passing does NOT prove


def test_fabricated_head_never_earns_witnessed():
    head = _head(head_hash="f" * 64)
    copy = _copy(head)
    # a fabricated head fails the chain gate regardless of signatures
    result = _verify(head, [copy], {"witness-a": WKEY}, chain_ok=False)
    assert result["verdict"] == "diverged"
    assert result["reason"] == "chain_broken"
    # even with a self-signed copy and no chain verification, an unpinned key
    # can never raise the verdict
    result2 = _verify(head, [copy], {})
    assert result2["verdict"] != "witnessed"


def test_unpinned_witness_key_never_raises_verdict():
    head = _head()
    copy = _copy(head)  # valid signature, but the key is not pinned
    result = _verify(head, [copy], {})
    assert result["verdict"] == "witness-unusable"
    assert any(w["status"] == "unknown_witness" for w in result["witnesses"])


def test_unsigned_copy_corroborates_but_never_witnesses():
    head = _head()
    copy = {
        "schema": witness.COUNTERSIG_SCHEMA, "witness_id": "witness-a",
        "org_id": ORG, "head_hash": "a" * 64, "through_rowid": 3,
        "prev_head_hash": None, "algo": "hmac-sha256",
    }
    result = _verify(head, [copy], {"witness-a": WKEY})
    assert result["verdict"] == "consistent-unwitnessed"


def test_refusal_line_is_evidence_against_the_head():
    head = _head()
    copy = {"witness_id": "witness-a", "refusal": True, "reason": "chain diverged"}
    result = _verify(head, [copy], {"witness-a": WKEY})
    assert result["verdict"] == "diverged"
    assert result["reason"].startswith("witness_refusal")
    assert result["refusals"][0]["reason"] == "chain diverged"


def test_asked_and_empty_is_distinct_from_never_asked():
    head = _head()
    asked = _verify(head, [], {"witness-a": WKEY}, asked=True)
    assert asked["verdict"] == "asked-and-empty"
    assert asked["exit_code"] == 4
    never = _verify(head, [], {"witness-a": WKEY}, asked=False)
    assert never["verdict"] == "consistent-unwitnessed"
    assert never["exit_code"] == 1
    assert asked["exit_code"] != never["exit_code"]  # distinct verdict/exit code


def test_witness_that_saw_only_older_head_cannot_attest_newer_one():
    head_n = _head(head_hash="b" * 64, through=5)
    # the witness countersigns head N claiming it last saw N-2...
    copy = _copy(head_n, prev="a" * 64)
    # ...but the only prior copy we hold from it covers N-3 (no continuity)
    prior_head = _head(head_hash="c" * 64, through=3)
    prior = _copy(prior_head, prev=None)
    result = _verify(head_n, [copy], {"witness-a": WKEY}, prior=[prior])
    assert result["verdict"] == "consistent-unwitnessed"
    statuses = {w["witness_id"]: w["status"] for w in result["witnesses"]}
    assert statuses["witness-a"] == "continuity_unproven"


def test_continuity_proof_upgrades_to_witnessed():
    head_n1 = _head(head_hash="a" * 64, through=4)
    prior = _copy(head_n1, prev=None)          # witness attested head N-1
    head_n = _head(head_hash="b" * 64, through=5)
    copy = _copy(head_n, prev="a" * 64)        # and binds N-1 as prev
    result = _verify(head_n, [copy], {"witness-a": WKEY}, prior=[prior])
    assert result["verdict"] == "witnessed"
    assert any(w["status"] == "attests" for w in result["witnesses"])


def test_conflicting_head_at_same_position_diverges():
    head = _head(head_hash="a" * 64, through=5)
    other = _head(head_hash="d" * 64, through=5)
    copy = _copy(other)  # witness attests a DIFFERENT hash at rowid 5
    result = _verify(head, [copy], {"witness-a": WKEY})
    assert result["verdict"] == "diverged"
    assert result["reason"].startswith("conflicting_head")


def test_witness_unusable_when_no_line_applies():
    head = _head()
    copy = _copy(head, wid="witness-a", key=WKEY2)  # wrong key
    result = _verify(head, [copy], {"witness-a": WKEY})
    assert result["verdict"] == "witness-unusable"
    assert result["exit_code"] == 2


def test_bad_signature_never_witnesses():
    head = _head()
    copy = _copy(head)
    copy["sig"] = "e" * 64
    result = _verify(head, [copy], {"witness-a": WKEY})
    assert result["verdict"] == "witness-unusable"


# ── CLI round trip ──────────────────────────────────────────────────────────


def _run_cli(argv, monkeypatch, tmp_path):
    import io
    import contextlib
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "w.db"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            cli.main(argv)
            code = 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, buf.getvalue()


def test_cli_sign_then_verify_round_trip(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "w.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "witness-org", tier="free")["id"]
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="deploy", input_tokens=10, output_tokens=5, cost_usd=0.1,
        ts=time.time(),
    )
    conn.close()

    state = tmp_path / "wstate"
    publish = tmp_path / "publish"
    key_file = tmp_path / "wkey"
    key_file.write_bytes(WKEY)

    # sign the live head
    code, out = _run_cli(["witness", "sign", "--org", "witness-org",
                          "--witness-id", "witness-a", "--key-file", str(key_file),
                          "--state", str(state), "--publish", str(publish)],
                         monkeypatch, tmp_path)
    assert code == 0
    signed = json.loads(out)
    assert signed["head_hash"]

    # verify it against the pinned key file
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"witness-a": WKEY.hex()}))
    copies = tmp_path / "copies.jsonl"
    copies.write_text(json.dumps(signed["countersignature"]) + "\n")
    code, out = _run_cli(["witness", "verify", "--org", "witness-org",
                          "--copies", str(copies), "--keys", str(keys),
                          "--asked", "--json"], monkeypatch, tmp_path)
    assert code == 0
    verdict = json.loads(out)
    assert verdict["verdict"] == "witnessed"
    assert verdict["does_not_prove"]

    # asked-and-empty has its own exit code
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    code, out = _run_cli(["witness", "verify", "--org", "witness-org",
                          "--copies", str(empty), "--keys", str(keys),
                          "--asked", "--json"], monkeypatch, tmp_path)
    assert code == 4
    assert json.loads(out)["verdict"] == "asked-and-empty"


def test_cli_verify_offline_head_is_fail_closed(tmp_path, monkeypatch):
    # A fabricated head file cannot earn witnessed: chain_ok defaults false.
    fabricated = _head(head_hash="9" * 64, through=99)
    head_file = tmp_path / "head.json"
    head_file.write_text(json.dumps(fabricated))
    copy = _copy(fabricated)
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"witness-a": WKEY.hex()}))
    copies = tmp_path / "copies.jsonl"
    copies.write_text(json.dumps(copy) + "\n")
    code, out = _run_cli(["witness", "verify", "--head-file", str(head_file),
                          "--copies", str(copies), "--keys", str(keys),
                          "--json"], monkeypatch, tmp_path)
    assert code == 3
    assert json.loads(out)["verdict"] == "diverged"

    # ...and only --assume-chain-ok (offline verification of a real head)
    # upgrades the run.
    code, out = _run_cli(["witness", "verify", "--head-file", str(head_file),
                          "--assume-chain-ok", "--copies", str(copies),
                          "--keys", str(keys), "--json"], monkeypatch, tmp_path)
    assert code == 0
    assert json.loads(out)["verdict"] == "witnessed"
