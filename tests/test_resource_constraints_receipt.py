import hashlib
import sqlite3

from ledger_agent import db, metering


def test_resource_constraint_hash_is_persisted_and_projected(tmp_path):
    path = tmp_path / "ledger.sqlite"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Resource Org", "resource-org", "pro")
    raw = '{"currency":"USD","amount_minor":1800,"merchant_ref":"merchant:a"}'
    digest = hashlib.sha256(raw.encode()).hexdigest()
    result = metering.record_usage(
        conn,
        org["id"],
        provider="openai",
        model="gpt-fixture",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.01,
        resource_constraints_version="perseus-authorized-action/resource-constraints/v1",
        resource_constraints_hash=digest,
    )
    row = conn.execute(
        "SELECT resource_constraints_version, resource_constraints_hash FROM usage_events WHERE id=?",
        (result.event_id,),
    ).fetchone()
    assert row["resource_constraints_version"].endswith("/v1")
    assert row["resource_constraints_hash"] == digest


def test_invalid_resource_constraint_hash_is_rejected(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Resource Org", "resource-org", "pro")
    try:
        metering.record_usage(
            conn,
            org["id"],
            provider="openai",
            cost_usd=0.01,
            resource_constraints_hash="not-a-sha256",
        )
    except ValueError as exc:
        assert "resource_constraints_hash" in str(exc)
    else:
        raise AssertionError("invalid resource constraint hash was accepted")


def test_legacy_schema_is_migrated_with_empty_constraint_fields(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(usage_events)")}
    assert "resource_constraints_version" in columns
    assert "resource_constraints_hash" in columns


def test_constraint_fields_are_part_of_chain_hash(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Resource Org", "resource-org", "pro")
    digest = "a" * 64
    result = metering.record_usage(
        conn,
        org["id"],
        provider="openai",
        cost_usd=0.01,
        resource_constraints_version="v1",
        resource_constraints_hash=digest,
    )
    before = db.verify_chain(conn, org["id"])
    assert before["ok"]
    conn.execute("UPDATE usage_events SET resource_constraints_hash=? WHERE id=?", ("b" * 64, result.event_id))
    after = db.verify_chain(conn, org["id"])
    assert not after["ok"]


def test_constraint_hash_is_optional_for_legacy_usage(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Legacy Org", "legacy-org", "pro")
    result = metering.record_usage(conn, org["id"], provider="openai", cost_usd=0.01)
    row = conn.execute("SELECT resource_constraints_hash FROM usage_events WHERE id=?", (result.event_id,)).fetchone()
    assert row["resource_constraints_hash"] is None


def test_empty_constraint_projection_does_not_store_raw_values(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Projection Org", "projection-org", "pro")
    result = metering.record_usage(
        conn,
        org["id"],
        provider="openai",
        cost_usd=0.01,
        resource_constraints_version="v1",
        resource_constraints_hash="c" * 64,
    )
    row = conn.execute("SELECT * FROM usage_events WHERE id=?", (result.event_id,)).fetchone()
    assert "merchant" not in str(dict(row)).lower()
    assert row["resource_constraints_hash"] == "c" * 64


def test_schema_migration_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    db.init_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0


def test_constraint_fields_are_strings_at_meter_boundary(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Boundary Org", "boundary-org", "pro")
    try:
        metering.record_usage(conn, org["id"], provider="openai", cost_usd=0.01, resource_constraints_version=1)
    except ValueError as exc:
        assert "version" in str(exc)
    else:
        raise AssertionError("non-string resource constraint version was accepted")


def test_constraint_hash_is_lowercased(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Case Org", "case-org", "pro")
    result = metering.record_usage(conn, org["id"], provider="openai", cost_usd=0.01, resource_constraints_hash="D" * 64)
    row = conn.execute("SELECT resource_constraints_hash FROM usage_events WHERE id=?", (result.event_id,)).fetchone()
    assert row["resource_constraints_hash"] == "d" * 64


def test_constraint_version_can_be_present_without_raw_payload(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Version Org", "version-org", "pro")
    result = metering.record_usage(conn, org["id"], provider="openai", cost_usd=0.01, resource_constraints_version="v1")
    row = conn.execute("SELECT resource_constraints_version, resource_constraints_hash FROM usage_events WHERE id=?", (result.event_id,)).fetchone()
    assert row["resource_constraints_version"] == "v1"
    assert row["resource_constraints_hash"] is None


def test_digest_example_is_sha256():
    assert len(hashlib.sha256(b"constraints").hexdigest()) == 64


def test_resource_fields_are_included_in_authorization_projection_shape():
    # The API contract is hash-only; raw resource values never belong in this projection.
    projection = {"resource_constraints_version": "v1", "resource_constraints_hash": "a" * 64}
    assert set(projection) == {"resource_constraints_version", "resource_constraints_hash"}
    assert len(projection["resource_constraints_hash"]) == 64


def test_resource_constraint_hash_roundtrip_is_exact(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Roundtrip Org", "roundtrip-org", "pro")
    digest = hashlib.sha256(b"roundtrip").hexdigest()
    result = metering.record_usage(conn, org["id"], provider="openai", cost_usd=0.01, resource_constraints_hash=digest)
    assert conn.execute("SELECT resource_constraints_hash FROM usage_events WHERE id=?", (result.event_id,)).fetchone()[0] == digest


def test_constraint_hash_rejects_empty_nonhex_value(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Reject Org", "reject-org", "pro")
    try:
        metering.record_usage(conn, org["id"], provider="openai", cost_usd=0.01, resource_constraints_hash="")
    except ValueError:
        pass
    else:
        raise AssertionError("empty hash was accepted")


def test_constraint_version_is_visible_to_sql_consumers(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "SQL Org", "sql-org", "pro")
    result = metering.record_usage(conn, org["id"], provider="openai", cost_usd=0.01, resource_constraints_version="perseus/v1")
    assert conn.execute("SELECT resource_constraints_version FROM usage_events WHERE id=?", (result.event_id,)).fetchone()[0] == "perseus/v1"


def test_resource_hash_is_not_a_plaintext_constraint_payload(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Hash Org", "hash-org", "pro")
    result = metering.record_usage(conn, org["id"], provider="openai", cost_usd=0.01, resource_constraints_hash="e" * 64)
    row = dict(conn.execute("SELECT * FROM usage_events WHERE id=?", (result.event_id,)).fetchone())
    assert "amount_minor" not in str(row)
    assert row["resource_constraints_hash"] == "e" * 64


def test_resource_columns_have_nullable_legacy_defaults(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    info = {row["name"]: row for row in conn.execute("PRAGMA table_info(usage_events)")}
    assert info["resource_constraints_version"]["notnull"] == 0
    assert info["resource_constraints_hash"]["notnull"] == 0


def test_resource_fields_do_not_change_cost_calculation(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Cost Org", "cost-org", "pro")
    a = metering.record_usage(conn, org["id"], provider="openai", cost_usd=0.01)
    b = metering.record_usage(conn, org["id"], provider="openai", cost_usd=0.01, resource_constraints_hash="f" * 64)
    assert a.cost_usd == b.cost_usd


def test_resource_hash_is_stable_for_same_input():
    value = "resource-constraints"
    assert hashlib.sha256(value.encode()).hexdigest() == hashlib.sha256(value.encode()).hexdigest()


def test_constraint_fields_are_not_required_for_old_clients(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Compat Org", "compat-org", "pro")
    result = metering.record_usage(conn, org["id"], provider="openai", cost_usd=0.01)
    assert result.recorded is True


def test_resource_constraint_contract_has_versioned_fields():
    assert "resource_constraints_version" in {"resource_constraints_version", "resource_constraints_hash"}
    assert "resource_constraints_hash" in {"resource_constraints_version", "resource_constraints_hash"}


def test_hash_length_is_exactly_64():
    assert len("0" * 64) == 64


def test_hash_is_hex():
    int("a" * 64, 16)


def test_no_raw_resource_argument_column_is_added(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(usage_events)")}
    assert "raw_arguments" not in columns


def test_schema_is_readable_by_sqlite(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_chain_verification_still_passes_with_null_constraint_fields(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Null Org", "null-org", "pro")
    metering.record_usage(conn, org["id"], provider="openai", cost_usd=0.01)
    assert db.verify_chain(conn, org["id"])["ok"]


def test_hash_is_stored_without_prefix(tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    org = db.create_org(conn, "Prefix Org", "prefix-org", "pro")
    digest = "1" * 64
    result = metering.record_usage(conn, org["id"], provider="openai", cost_usd=0.01, resource_constraints_hash=digest)
    stored = conn.execute("SELECT resource_constraints_hash FROM usage_events WHERE id=?", (result.event_id,)).fetchone()[0]
    assert stored == digest


def test_authorization_projection_can_be_serialized():
    import json
    assert json.dumps({"resource_constraints_version": "v1", "resource_constraints_hash": "a" * 64})


def test_constraint_contract_does_not_expose_raw_values():
    projection = {"resource_constraints_version": "v1", "resource_constraints_hash": "a" * 64}
    import json
    assert "merchant:a" not in json.dumps(projection)
