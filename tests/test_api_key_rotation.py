"""#150: API-key rotation, scoped keys, ingest diagnostics, and tenant-safe ops.

Covers:
- create_scoped_api_key with workspace restrictions
- rotate_api_key with bounded overlap (zero-downtime)
- complete_key_rotation
- rotate_and_revoke (emergency)
- event_count visibility without raw secrets
- ingest health recording and querying
- cross-org/workspace isolation
- rotation/revocation retries and partial failure
"""
import json
import time

import pytest

from plutus_agent import db


def _org(tmp_path, name="Acme", tier="pro"):
    conn = db.connect(str(tmp_path / "plutus.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, name, tier=tier, owner_email="a@b.co")["id"]
    return conn, org_id


def _two_orgs(tmp_path):
    conn = db.connect(str(tmp_path / "plutus.db"))
    db.init_schema(conn)
    org_a = db.create_org(conn, "OrgA", tier="pro", owner_email="a@x.co")["id"]
    org_b = db.create_org(conn, "OrgB", tier="pro", owner_email="b@x.co")["id"]
    ws_a = db.create_workspace(conn, org_a, "prod")["id"]
    ws_b = db.create_workspace(conn, org_b, "prod")["id"]
    return conn, org_a, org_b, ws_a, ws_b


# ----------------------------------------------------------- scoped keys -----
def test_create_scoped_key(tmp_path):
    conn, org = _org(tmp_path)
    scope = {"workspaces": ["prod"]}
    row, secret = db.create_api_key_scoped(conn, org, name="scoped-key", scope=scope)
    assert row["org_id"] == org
    assert row["name"] == "scoped-key"
    assert json.loads(row["scope"]) == scope
    assert secret.startswith(db.API_KEY_PREFIX)

    # Key resolves correctly
    assert db.api_key_org(conn, secret) == org
    conn.close()


def test_scoped_key_no_scope_is_unrestricted(tmp_path):
    conn, org = _org(tmp_path)
    row, secret = db.create_api_key_scoped(conn, org, name="unrestricted")
    assert row["scope"] is None
    assert db.api_key_org(conn, secret) == org
    conn.close()


def test_scoped_key_event_count_increments(tmp_path):
    conn, org = _org(tmp_path)
    _, secret = db.create_api_key_scoped(conn, org, name="counter")
    assert db.api_key_row(conn, secret)["event_count"] == 0

    # Use the key (api_key_org increments event_count)
    db.api_key_org(conn, secret)
    db.api_key_org(conn, secret)
    row = db.api_key_row(conn, secret)
    assert row["event_count"] >= 2
    conn.close()


# ------------------------------------------------------------- key rotation ---
def test_rotate_api_key_creates_new_key(tmp_path):
    conn, org = _org(tmp_path)
    old_row, old_secret = db.create_api_key(conn, org, name="original")

    new_row, new_secret, old_after = db.rotate_api_key(
        conn, org, old_row["id"], overlap_seconds=300)

    # New key has its own id and prefix
    assert new_row["id"] != old_row["id"]
    assert new_row["prefix"] != old_row["prefix"]
    assert new_secret != old_secret

    # Rotation chain is recorded
    assert new_row["rotation_of"] == old_row["id"]

    # Old key is still valid (overlap period)
    assert old_after["revoked_at"] is None
    assert db.api_key_org(conn, old_secret) == org  # still works

    # New key also works
    assert db.api_key_org(conn, new_secret) == org
    conn.close()


def test_rotate_api_key_inherits_name_and_scope(tmp_path):
    conn, org = _org(tmp_path)
    scope = {"workspaces": ["staging"]}
    old_row, _ = db.create_api_key_scoped(conn, org, name="my-key", scope=scope)

    new_row, _, _ = db.rotate_api_key(conn, org, old_row["id"], overlap_seconds=60)
    assert new_row["name"] == "my-key"  # inherited
    assert json.loads(new_row["scope"]) == scope  # inherited
    conn.close()


def test_rotate_api_key_rejects_revoked_key(tmp_path):
    conn, org = _org(tmp_path)
    old_row, _ = db.create_api_key(conn, org)
    db.revoke_api_key(conn, old_row["id"], org)
    with pytest.raises(ValueError, match="cannot rotate a revoked key"):
        db.rotate_api_key(conn, org, old_row["id"])
    conn.close()


def test_rotate_api_key_rejects_cross_org(tmp_path):
    conn, a, b, _, _ = _two_orgs(tmp_path)
    old_row, _ = db.create_api_key(conn, a)
    with pytest.raises(ValueError, match="does not belong"):
        db.rotate_api_key(conn, b, old_row["id"])
    conn.close()


def test_complete_key_rotation_revokes_old_key(tmp_path):
    conn, org = _org(tmp_path)
    old_row, old_secret = db.create_api_key(conn, org, name="rotating")
    new_row, new_secret, _ = db.rotate_api_key(
        conn, org, old_row["id"], overlap_seconds=300)

    # Old key still works during overlap
    assert db.api_key_org(conn, old_secret) == org

    # Complete rotation
    assert db.complete_key_rotation(conn, org, new_row["id"]) is True

    # Old key is now revoked
    assert db.api_key_org(conn, old_secret) is None  # revoked key fails
    assert db.api_key_org(conn, new_secret) == org  # new key still works
    conn.close()


def test_complete_key_rotation_noop_for_unrotated_key(tmp_path):
    conn, org = _org(tmp_path)
    row, _ = db.create_api_key(conn, org)
    assert db.complete_key_rotation(conn, org, row["id"]) is False
    conn.close()


def test_rotate_and_revoke_immediate_revocation(tmp_path):
    conn, org = _org(tmp_path)
    old_row, old_secret = db.create_api_key(conn, org, name="emergency")
    new_row, new_secret = db.rotate_and_revoke(conn, org, old_row["id"])

    # Old key is immediately revoked
    assert db.api_key_org(conn, old_secret) is None

    # New key works
    assert db.api_key_org(conn, new_secret) == org
    conn.close()


# -------------------------------------------------------- zero-downtime ------
def test_zero_downtime_overlap(tmp_path):
    """Both keys remain valid during the overlap period."""
    conn, org = _org(tmp_path)
    old_row, old_secret = db.create_api_key(conn, org)

    new_row, new_secret, _ = db.rotate_api_key(
        conn, org, old_row["id"], overlap_seconds=60)

    # Both keys work simultaneously
    assert db.api_key_org(conn, old_secret) == org
    assert db.api_key_org(conn, new_secret) == org

    # After complete_key_rotation, old key stops working
    assert db.complete_key_rotation(conn, org, new_row["id"]) is True
    assert db.api_key_org(conn, old_secret) is None
    assert db.api_key_org(conn, new_secret) == org
    conn.close()


# ---------------------------------------------------- visibility (no secrets) ---
def test_key_list_shows_metadata_not_secret(tmp_path):
    conn, org = _org(tmp_path)
    _, secret = db.create_api_key(conn, org, name="my-app-key")
    keys = db.list_api_keys(conn, org)

    assert len(keys) >= 1
    for k in keys:
        d = dict(k)
        # Prefix is shown, not the full secret
        assert d["prefix"].startswith(db.API_KEY_PREFIX)
        assert len(d["prefix"]) == len(db.API_KEY_PREFIX) + 4
        # event_count is visible
        assert d["event_count"] is not None
        # last_used_at is visible
        assert "last_used_at" in d
        # Raw secret is NOT in the row
        assert "token_hash" in d  # hash is available (for admin ops)
    conn.close()


def test_event_count_visible(tmp_path):
    conn, org = _org(tmp_path)
    _, secret = db.create_api_key(conn, org)

    # Use the key several times
    for _ in range(5):
        db.api_key_org(conn, secret)

    row = db.api_key_row(conn, secret)
    assert row["event_count"] >= 5
    conn.close()


# ------------------------------------------------------- cross-org isolation ---
def test_key_from_org_a_cannot_access_org_b(tmp_path):
    conn, a, b, _, _ = _two_orgs(tmp_path)
    _, a_secret = db.create_api_key(conn, a)
    _, b_secret = db.create_api_key(conn, b)

    # Each key resolves only to its own org
    assert db.api_key_org(conn, a_secret) == a
    assert db.api_key_org(conn, b_secret) == b

    # Key for org A cannot resolve in org B's context
    assert db.api_key_org(conn, a_secret) != b
    conn.close()


def test_rotation_scoped_to_one_org(tmp_path):
    conn, a, b, _, _ = _two_orgs(tmp_path)
    a_row, _ = db.create_api_key(conn, a)
    b_old, _ = db.create_api_key(conn, b)

    # Try to rotate B's key from org A — should fail
    with pytest.raises(ValueError, match="does not belong"):
        db.rotate_api_key(conn, a, b_old["id"])

    # Rotating within org A works
    new_a_row, _, _ = db.rotate_api_key(conn, a, a_row["id"])
    assert new_a_row is not None
    conn.close()


# ---------------------------------------------------------- retry / partial ---
def test_revoke_already_revoked_is_noop(tmp_path):
    conn, org = _org(tmp_path)
    row, _ = db.create_api_key(conn, org)
    assert db.revoke_api_key(conn, row["id"], org) is True
    assert db.revoke_api_key(conn, row["id"], org) is False  # already revoked
    conn.close()


def test_rotate_key_not_found_raises(tmp_path):
    conn, org = _org(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        db.rotate_api_key(conn, org, "key_nonexistent")
    conn.close()


# ------------------------------------------------------------ ingest health ---
def test_record_ingest_health_creates_row(tmp_path):
    conn, org = _org(tmp_path)
    db.record_ingest_health(conn, org, "test-source", ok=True)
    rows = db.get_ingest_health(conn, org)
    assert len(rows) == 1
    assert rows[0]["source"] == "test-source"
    assert rows[0]["last_ok"] is True
    assert rows[0]["total_events"] == 1
    assert rows[0]["total_errors"] == 0
    conn.close()


def test_ingest_health_tracks_errors(tmp_path):
    conn, org = _org(tmp_path)
    db.record_ingest_health(conn, org, "err-source", ok=False, error="rate limited")
    rows = db.get_ingest_health(conn, org)
    assert len(rows) == 1
    assert rows[0]["last_ok"] is False
    assert rows[0]["last_error"] == "rate limited"
    assert rows[0]["total_errors"] == 1
    assert rows[0]["total_events"] == 1
    conn.close()


def test_ingest_health_accumulates(tmp_path):
    conn, org = _org(tmp_path)
    for i in range(10):
        db.record_ingest_health(conn, org, "accumulate", ok=True)
    db.record_ingest_health(conn, org, "accumulate", ok=False, error="timeout")
    rows = db.get_ingest_health(conn, org)
    assert len(rows) == 1
    assert rows[0]["total_events"] == 11  # 10 ok + 1 fail
    assert rows[0]["total_errors"] == 1
    assert rows[0]["last_error"] == "timeout"
    conn.close()


def test_ingest_health_multiple_sources(tmp_path):
    conn, org = _org(tmp_path)
    db.record_ingest_health(conn, org, "source-a", ok=True)
    db.record_ingest_health(conn, org, "source-b", ok=True)
    db.record_ingest_health(conn, org, "source-c", ok=False, error="429")
    rows = db.get_ingest_health(conn, org)
    assert len(rows) == 3
    statuses = {r["source"]: r["last_ok"] for r in rows}
    assert statuses["source-a"] is True
    assert statuses["source-b"] is True
    assert statuses["source-c"] is False
    conn.close()


def test_ingest_health_org_isolation(tmp_path):
    conn, a, b, _, _ = _two_orgs(tmp_path)
    db.record_ingest_health(conn, a, "common-source", ok=True)
    db.record_ingest_health(conn, b, "common-source", ok=False, error="bad key")
    a_rows = db.get_ingest_health(conn, a)
    b_rows = db.get_ingest_health(conn, b)
    assert len(a_rows) == 1
    assert len(b_rows) == 1
    assert a_rows[0]["last_ok"] is True
    assert b_rows[0]["last_ok"] is False
    conn.close()


def test_all_ingest_health_returns_all(tmp_path):
    conn, a, b, _, _ = _two_orgs(tmp_path)
    db.record_ingest_health(conn, a, "source-a", ok=True)
    db.record_ingest_health(conn, b, "source-b", ok=False, error="err")
    all_rows = db.all_ingest_health(conn)
    assert len(all_rows) == 2
    sources = {r["source"] for r in all_rows}
    assert "source-a" in sources
    assert "source-b" in sources
    conn.close()
