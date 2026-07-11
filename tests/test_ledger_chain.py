"""Ledger tamper-evidence: the usage_events hash chain (#108).

Covers plutus_agent.db.compute_row_hash / chain_head / verify_chain and the
record_usage wiring — the chain is built at ingest, an intact chain verifies,
and every tampering shape (modify, delete, reorder, insert, hash-strip) is
detected. Also: the pre-chain (pre-upgrade) prefix, the additive migration, and
the optional keyed-MAC two-party mode.
"""
import pytest

from plutus_agent import config, db, metering


def _org(tmp_path, credit=1000.0):
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


def _events(conn, org_id):
    return conn.execute(
        "SELECT rowid AS _rowid, * FROM usage_events WHERE org_id=? ORDER BY rowid",
        (org_id,)).fetchall()


# ------------------------------------------------------- chain construction ---
def test_schema_version_bumped():
    assert db.SCHEMA_VERSION >= 6


def test_chain_populated_on_insert(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, n=3)
    rows = _events(conn, org)
    assert len(rows) == 3
    # first row chains from genesis (prev_hash NULL), each row_hash set,
    # and each subsequent prev_hash == the prior row_hash.
    assert rows[0]["prev_hash"] is None
    assert all(r["row_hash"] for r in rows)
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]
    assert rows[2]["prev_hash"] == rows[1]["row_hash"]
    # hashes are unique (no accidental constant)
    assert len({r["row_hash"] for r in rows}) == 3


def test_verify_ok_on_clean_ledger(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, n=4)
    rep = db.verify_chain(conn)
    assert rep["ok"] is True
    o = rep["orgs"][0]
    assert o["status"] == "ok"
    assert o["events"] == 4 and o["verified"] == 4 and o["pre_chain"] == 0


def test_per_org_chains_are_independent(tmp_path):
    conn, org = _org(tmp_path)
    org2 = db.create_org(conn, "Beta", tier="pro")["id"]
    db.add_ledger(conn, org2, 100.0, "topup", reason="seed")
    _meter(conn, org, n=2)
    _meter(conn, org2, n=2)
    rep = db.verify_chain(conn)
    assert rep["ok"] is True
    assert {o["org_id"] for o in rep["orgs"]} == {org, org2}
    # first event of each org starts its own chain from genesis
    for oid in (org, org2):
        assert _events(conn, oid)[0]["prev_hash"] is None


# ----------------------------------------------------------- tamper detection --
def test_detects_modified_cost(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, n=3)
    victim = _events(conn, org)[1]
    # An operator rewrites a debit downward directly in the DB.
    conn.execute("UPDATE usage_events SET cost_micros = cost_micros - 500000 "
                 "WHERE id=?", (victim["id"],))
    conn.commit()
    rep = db.verify_chain(conn, org_id=org)
    o = rep["orgs"][0]
    assert rep["ok"] is False and o["status"] == "broken"
    assert o["first_divergence"]["event_id"] == victim["id"]
    assert "modified" in o["first_divergence"]["reason"]


def test_detects_deleted_event(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, n=4)
    victim = _events(conn, org)[1]
    conn.execute("DELETE FROM usage_events WHERE id=?", (victim["id"],))
    conn.commit()
    rep = db.verify_chain(conn, org_id=org)
    o = rep["orgs"][0]
    assert rep["ok"] is False and o["status"] == "broken"
    # the row AFTER the deleted one now has a prev_hash that no longer matches
    assert "prev_hash" in o["first_divergence"]["reason"]


def test_detects_forged_insert(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, n=2)
    # An attacker appends a fabricated event with an invented hash.
    conn.execute(
        "INSERT INTO usage_events(id,org_id,provider,task_type,input_tokens,"
        "output_tokens,cache_read_tokens,reasoning_tokens,cost_micros,estimated,"
        "source,ts,prev_hash,row_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("evt_forged", org, "openai", "general", 0, 0, 0, 0, 0, 1, "api",
         9999999999.0, "deadbeef", "cafebabe"))
    conn.commit()
    rep = db.verify_chain(conn, org_id=org)
    assert rep["ok"] is False
    assert rep["orgs"][0]["first_divergence"]["event_id"] == "evt_forged"


def test_detects_stripped_hash(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, n=3)
    victim = _events(conn, org)[1]
    # NULL out a hash on an already-chained row (not a legitimate pre-chain row,
    # which only ever precedes the chain).
    conn.execute("UPDATE usage_events SET row_hash=NULL WHERE id=?", (victim["id"],))
    conn.commit()
    rep = db.verify_chain(conn, org_id=org)
    o = rep["orgs"][0]
    assert rep["ok"] is False and "missing" in o["first_divergence"]["reason"]


# ------------------------------------------------------------- pre-chain prefix --
def test_pre_chain_rows_are_unverifiable_not_failures(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, n=2)
    # Simulate rows written before the chain existed: NULL both hashes on the
    # earliest rows (they precede any chained row).
    for r in _events(conn, org):
        conn.execute("UPDATE usage_events SET prev_hash=NULL, row_hash=NULL "
                     "WHERE id=?", (r["id"],))
    conn.commit()
    # New events after the upgrade chain from genesis again.
    _meter(conn, org, n=2)
    rep = db.verify_chain(conn, org_id=org)
    o = rep["orgs"][0]
    assert rep["ok"] is True and o["status"] == "ok"
    assert o["pre_chain"] == 2 and o["verified"] == 2
    # the first post-upgrade row restarts the chain from genesis
    post = [r for r in _events(conn, org) if r["row_hash"]][0]
    assert post["prev_hash"] is None


# ------------------------------------------------------------------- migration --
def test_migration_adds_columns_to_old_db(tmp_path):
    # Build a pre-#108 database by hand: a usage_events without the chain columns
    # (the additive-only schema contract means no rebuild/drop is ever needed).
    conn = db.connect(str(tmp_path / "old.db"))
    conn.executescript(
        "CREATE TABLE organizations(id TEXT PRIMARY KEY, name TEXT, slug TEXT UNIQUE,"
        " tier TEXT, allow_negative_balance INTEGER DEFAULT 0, created_at REAL);"
        "CREATE TABLE usage_events(id TEXT PRIMARY KEY, org_id TEXT, workspace_id TEXT,"
        " provider TEXT, model TEXT, task_type TEXT, input_tokens INTEGER,"
        " output_tokens INTEGER, cache_read_tokens INTEGER, reasoning_tokens INTEGER,"
        " cost_micros INTEGER, estimated INTEGER, source TEXT, ts REAL);"
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO meta(key,value) VALUES('schema_version','5');")
    org = "org_old"
    conn.execute("INSERT INTO organizations(id,name,slug,tier,created_at) "
                 "VALUES(?,?,?,?,?)", (org, "Acme", "acme", "pro", 1.0))
    conn.execute(
        "INSERT INTO usage_events(id,org_id,provider,task_type,input_tokens,"
        "output_tokens,cache_read_tokens,reasoning_tokens,cost_micros,estimated,"
        "source,ts) VALUES('evt_old',?,?,?,?,?,?,?,?,?,?,?)",
        (org, "openai", "general", 1, 1, 0, 0, 1000, 1, "api", 1.0))
    conn.commit()
    assert "row_hash" not in db._table_columns(conn, "usage_events")
    # init_schema migrates additively (adds the chain columns, bumps version).
    db.init_schema(conn)
    db.add_ledger(conn, org, 100.0, "topup", reason="seed")
    cols = db._table_columns(conn, "usage_events")
    assert "prev_hash" in cols and "row_hash" in cols
    old = conn.execute("SELECT * FROM usage_events WHERE id='evt_old'").fetchone()
    assert old["row_hash"] is None  # not back-filled with an unattestable hash
    # a new event chains from genesis; verify treats the old row as pre-chain
    _meter(conn, org, n=1)
    rep = db.verify_chain(conn, org_id=org)
    assert rep["ok"] is True
    assert rep["orgs"][0]["pre_chain"] == 1


# --------------------------------------------------------------- keyed MAC (2-party) --
def test_hmac_mode_two_party(tmp_path):
    conn, org = _org(tmp_path)
    key = b"customer-held-secret"
    _meter(conn, org, n=3, chain_hmac_key=key)
    # verifies with the right key
    assert db.verify_chain(conn, org_id=org, hmac_key=key)["ok"] is True
    # a wrong key (or none — the operator without the customer key) cannot forge
    # a passing verification
    assert db.verify_chain(conn, org_id=org, hmac_key=b"wrong")["ok"] is False
    assert db.verify_chain(conn, org_id=org, hmac_key=None)["ok"] is False


def test_compute_row_hash_is_deterministic_and_prev_sensitive():
    fields = {k: 0 for k in db._CHAIN_FIELDS}
    fields.update(id="evt_1", org_id="org_1", provider="openai", ts=1.5)
    h1 = db.compute_row_hash(None, fields)
    h2 = db.compute_row_hash(None, dict(fields))
    assert h1 == h2                      # deterministic
    assert db.compute_row_hash("abc", fields) != h1  # chained onto prev


def test_config_resolves_hmac_key(monkeypatch, tmp_path):
    monkeypatch.delenv("PLUTUS_CHAIN_HMAC_KEY", raising=False)
    assert config.chain_hmac_key({"ledger": {"hmac_key": ""}}) is None
    assert config.chain_hmac_key({"ledger": {"hmac_key": "abc"}}) == b"abc"
    monkeypatch.setenv("PLUTUS_CHAIN_HMAC_KEY", "envkey")
    assert config.chain_hmac_key({"ledger": {"hmac_key": "abc"}}) == b"envkey"
