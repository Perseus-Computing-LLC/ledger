"""Independently-verifiable tamper-evidence: chain checkpoints (#120).

The usage_events hash chain (test_ledger_chain.py) is tamper-evident GIVEN a
trusted head, but nothing pins the head — an operator who rewrites history and
recomputes the whole chain from genesis passes verify_chain. A checkpoint
escrows a head the chain provably reached; the customer keeps a copy out-of-band
and verify_checkpoints requires the live DB to reproduce it. These tests prove:

* a checkpoint captures the live head + covered event count,
* an intact chain reproduces every retained checkpoint,
* the attack verify_chain alone MISSES — a full-chain recompute after editing an
  event — is CAUGHT by a retained checkpoint,
* deletion / shortening below the anchor is caught (count_mismatch / missing),
* the optional signature detects a forged/altered anchor,
* re-checkpointing the same rowid is idempotent.
"""
import pytest

from plutus_agent import config, db, metering


def _org(tmp_path, credit=10000.0):
    conn = db.connect(str(tmp_path / "plutus.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "Acme", tier="pro")["id"]
    if credit:
        db.add_ledger(conn, org_id, credit, "topup", reason="test seed")
    return conn, org_id


def _meter(conn, org_id, n=3, **kw):
    for i in range(n):
        metering.record_usage(conn, org_id, provider="openai", model="gpt-4o",
                              cost_usd=1.0 + i, input_tokens=100, output_tokens=50,
                              **kw)


# --------------------------------------------------------- schema / capture ---
def test_schema_version_bumped():
    assert db.SCHEMA_VERSION >= 9


def test_checkpoint_captures_live_head(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, n=3)
    cp = db.checkpoint_chain(conn, org)
    assert cp is not None
    assert cp["event_count"] == 3
    assert cp["mode"] == "sha256"
    assert cp["sig"] is None
    # head_hash equals the live highest-rowid row_hash
    live = conn.execute(
        "SELECT row_hash FROM usage_events WHERE org_id=? "
        "ORDER BY rowid DESC LIMIT 1", (org,)).fetchone()["row_hash"]
    assert cp["head_hash"] == live
    # and it was persisted
    assert len(db.list_checkpoints(conn, org)) == 1


def test_checkpoint_none_when_no_events(tmp_path):
    conn, org = _org(tmp_path)
    assert db.checkpoint_chain(conn, org) is None


def test_verify_ok_on_intact_chain(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, n=4)
    cp = db.checkpoint_chain(conn, org)
    _meter(conn, org, n=2)  # chain grows past the anchor — still fine
    rep = db.verify_checkpoints(conn, [cp])
    assert rep["ok"] is True
    assert rep["checkpoints"][0]["status"] == "ok"


# ----------------------------------- the attack verify_chain alone misses ---
def test_full_recompute_attack_caught_by_checkpoint(tmp_path):
    """Operator edits an event AND recomputes the whole chain from genesis.

    verify_chain passes (internally consistent), but the retained checkpoint's
    head no longer reproduces -> caught. This is the core value of the feature.
    """
    conn, org = _org(tmp_path)
    _meter(conn, org, n=3)
    cp = db.checkpoint_chain(conn, org)  # customer retains this out-of-band

    # --- operator rewrites history: halve the middle event's cost, then rebuild
    #     the entire chain so it is internally consistent again.
    rows = conn.execute(
        "SELECT rowid AS _rowid, * FROM usage_events WHERE org_id=? ORDER BY rowid",
        (org,)).fetchall()
    victim = rows[1]
    conn.execute("UPDATE usage_events SET cost_micros=? WHERE id=?",
                 (victim["cost_micros"] // 2, victim["id"]))
    # recompute chain from genesis
    prev = None
    for r in conn.execute(
            "SELECT rowid AS _rowid, * FROM usage_events WHERE org_id=? ORDER BY rowid",
            (org,)).fetchall():
        fields = {k: r[k] for k in db._CHAIN_FIELDS}
        for k in db._CHAIN_FIELDS_OPTIONAL:
            fields[k] = r[k]
        rh = db.compute_row_hash(prev, fields)
        conn.execute("UPDATE usage_events SET prev_hash=?, row_hash=? WHERE id=?",
                     (prev, rh, r["id"]))
        prev = rh
    conn.commit()

    # verify_chain is FOOLED — the rewritten chain is internally consistent.
    assert db.verify_chain(conn, org_id=org)["ok"] is True
    # the retained checkpoint is NOT — its head no longer reproduces.
    rep = db.verify_checkpoints(conn, [cp])
    assert rep["ok"] is False
    assert rep["checkpoints"][0]["status"] == "head_mismatch"


def test_deletion_below_anchor_caught(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, n=4)
    cp = db.checkpoint_chain(conn, org)  # covers 4 events
    # operator deletes the anchored head row entirely
    victim = conn.execute(
        "SELECT id FROM usage_events WHERE org_id=? ORDER BY rowid DESC LIMIT 1",
        (org,)).fetchone()["id"]
    conn.execute("DELETE FROM usage_events WHERE id=?", (victim,))
    conn.commit()
    rep = db.verify_checkpoints(conn, [cp])
    assert rep["ok"] is False
    assert rep["checkpoints"][0]["status"] == "missing"


def test_deletion_within_chain_caught(tmp_path):
    """Deleting an event BELOW the anchored head, without recomputing.

    The link from the next row is severed, so the chain no longer self-verifies
    up to the anchor -> chain_broken (a strictly stronger catch than the count
    guard, which is the fallback when a recompute keeps the chain consistent).
    """
    conn, org = _org(tmp_path)
    _meter(conn, org, n=4)
    cp = db.checkpoint_chain(conn, org)
    # delete the first event; the head row (highest rowid) is untouched.
    first = conn.execute(
        "SELECT id FROM usage_events WHERE org_id=? ORDER BY rowid ASC LIMIT 1",
        (org,)).fetchone()["id"]
    conn.execute("DELETE FROM usage_events WHERE id=?", (first,))
    conn.commit()
    rep = db.verify_checkpoints(conn, [cp])
    assert rep["ok"] is False
    assert rep["checkpoints"][0]["status"] in ("chain_broken", "count_mismatch", "head_mismatch")


def test_count_mismatch_caught(tmp_path):
    """The count guard: head reproduces and the chain self-verifies, but the
    anchored event_count disagrees with the live count.

    Reached directly by handing verify a checkpoint whose head_hash is correct
    but whose event_count was altered (an unsigned DB-stored anchor an operator
    tampered with, or padding claimed below the anchor). Deletion normally trips
    the stronger chain_broken/head_mismatch signals first; this isolates the
    count branch as the backstop.
    """
    conn, org = _org(tmp_path)
    _meter(conn, org, n=3)
    cp = db.checkpoint_chain(conn, org)  # correct: 3 events
    tampered = dict(cp, event_count=5, sig=None)  # claim more than really exist
    rep = db.verify_checkpoints(conn, [tampered])
    assert rep["ok"] is False
    assert rep["checkpoints"][0]["status"] == "count_mismatch"


# --------------------------------------------------------------- signature ---
def test_signed_checkpoint_authentic(tmp_path):
    conn, org = _org(tmp_path)
    key = b"customer-held-secret"
    _meter(conn, org, n=3, chain_hmac_key=key)
    cp = db.checkpoint_chain(conn, org, hmac_key=key)
    assert cp["mode"] == "hmac-sha256"
    assert cp["sig"]
    rep = db.verify_checkpoints(conn, [cp], hmac_key=key)
    assert rep["ok"] is True


def test_forged_checkpoint_signature_rejected(tmp_path):
    conn, org = _org(tmp_path)
    key = b"customer-held-secret"
    _meter(conn, org, n=3, chain_hmac_key=key)
    cp = db.checkpoint_chain(conn, org, hmac_key=key)
    # attacker alters the anchored count but cannot re-sign without the key
    forged = dict(cp, event_count=999)
    rep = db.verify_checkpoints(conn, [forged], hmac_key=key)
    assert rep["ok"] is False
    assert rep["checkpoints"][0]["status"] == "bad_signature"


def test_sign_checkpoint_deterministic_and_key_sensitive():
    cp = {"org_id": "o", "through_rowid": 5, "head_hash": "abc",
          "event_count": 5, "mode": "hmac-sha256"}
    a = db.sign_checkpoint(cp, b"k1")
    b = db.sign_checkpoint(dict(cp), b"k1")
    assert a == b and a is not None
    assert db.sign_checkpoint(cp, b"k2") != a
    assert db.sign_checkpoint(cp, None) is None


# ------------------------------------------------------------- idempotency ---
def test_recheckpoint_same_rowid_idempotent(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, n=3)
    cp1 = db.checkpoint_chain(conn, org)
    cp2 = db.checkpoint_chain(conn, org)  # same head, no new events
    assert cp1["through_rowid"] == cp2["through_rowid"]
    # UNIQUE(org_id, through_rowid) upserted, not duplicated
    assert len(db.list_checkpoints(conn, org)) == 1
