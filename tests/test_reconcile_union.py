"""Union reconciliation (ledger#207): tracked ledger vs store, union semantics.

Covers the dry-run-before-mutation rule, preservation of a published-but-
store-lost record with the recovery reason recorded, preservation of store-
only records, the both-sides conflict case (fresh store state wins, reason
journaled, no silent rewrite), and the explicit operator flag for deletion.
"""
import pytest

from ledger_agent import db, metering, reconcile_union


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "ledger.db"))
    db.init_schema(conn)
    return conn


def _org(conn, name="Acme"):
    return db.create_org(conn, name, tier="pro")["id"]


def _metered_ids(conn, org_id, n):
    ids = []
    for _ in range(n):
        event = metering.record_usage(
            conn, org_id, provider="openai", model="gpt-4o",
            cost_usd=0.10, source="api",
        )
        ids.append(event.event_id)
    return ids


def _tracked(conn, event_id):
    """Build the tracked-ledger record for a stored event (id + row_hash).

    Must be called while the row is still present; returns the row hash so a
    caller can delete the row afterwards to simulate store loss.
    """
    row = conn.execute(
        "SELECT row_hash FROM usage_events WHERE id=?", (event_id,)).fetchone()
    return {"event_id": event_id, "row_hash": row["row_hash"]}


def _store_row(conn, event_id):
    return conn.execute(
        "SELECT id, row_hash, reconciliation_note FROM usage_events WHERE id=?",
        (event_id,)).fetchone()


def _journal(conn, event_id=None):
    if event_id:
        return conn.execute(
            "SELECT event_id, side, action, reason FROM reconciliation_events"
            " WHERE event_id=? ORDER BY rowid", (event_id,)).fetchall()
    return conn.execute(
        "SELECT event_id, side, action, reason FROM reconciliation_events"
        " ORDER BY rowid").fetchall()


# ---------------------------------------------- dry-run before any mutation ---
def test_dry_run_surfaces_all_would_be_drops_before_mutation(tmp_path):
    conn = _conn(tmp_path)
    org = _org(conn)
    stored = _metered_ids(conn, org, 2)
    tracked = [_tracked(conn, stored[0])]          # stored[1] is store-only

    rep = reconcile_union.reconcile_union(
        conn, tracked, apply=False, drop_missing=True)

    # The dry run surfaces the would-be drop without touching the store.
    assert rep.applied is False
    assert rep.would_drop == [stored[1]]
    assert rep.deleted == 0
    actions = {it.event_id: it.action for it in rep.items}
    assert actions[stored[1]] == "deleted"
    assert conn.execute(
        "SELECT COUNT(*) FROM usage_events WHERE id=?", (stored[1],)
    ).fetchone()[0] == 1, "dry run must not mutate the store"
    assert _journal(conn) == [], "dry run must not write journal rows"


# ------------------------ published-but-store-lost: preserved + recovered ---
def test_tracked_only_record_is_preserved_and_recovered_with_reason(tmp_path):
    conn = _conn(tmp_path)
    org = _org(conn)
    stored = _metered_ids(conn, org, 1)
    # Capture the tracked record, then simulate store loss.
    tracked = [_tracked(conn, stored[0])]
    conn.execute("DELETE FROM usage_events WHERE id=?", (stored[0],))
    conn.commit()
    tracked[0]["record"] = {
        "org_id": org, "provider": "openai", "model": "gpt-4o",
        "task_type": "general", "input_tokens": 100, "output_tokens": 50,
        "cost_micros": 10000, "estimated": 1, "source": "api", "ts": 1.0,
    }

    rep = reconcile_union.reconcile_union(conn, tracked, apply=True)
    assert rep.recovered == 1
    got = _store_row(conn, stored[0])
    assert got is not None, "published-but-store-lost record must be recovered"
    assert got["reconciliation_note"] == "recovered_published_but_store_lost"
    # Recovery reason is recorded on the record and in the journal.
    entries = _journal(conn, stored[0])
    assert entries and entries[0]["action"] == "recovered"
    assert entries[0]["reason"] == "recovered_published_but_store_lost"
    # Recovered rows carry NULL chain fields: the store does not attest them.
    chain = conn.execute(
        "SELECT prev_hash, row_hash FROM usage_events WHERE id=?",
        (stored[0],)).fetchone()
    assert chain["prev_hash"] is None and chain["row_hash"] is None


# ------------------------------------------------- store-only: preserved ---
def test_store_only_record_is_preserved_without_flag(tmp_path):
    conn = _conn(tmp_path)
    org = _org(conn)
    stored = _metered_ids(conn, org, 2)
    tracked = [_tracked(conn, stored[0])]

    rep = reconcile_union.reconcile_union(conn, tracked, apply=True)
    assert rep.deleted == 0
    assert rep.would_drop == []
    assert _store_row(conn, stored[1]) is not None, "store-only record must survive"
    actions = {it.event_id: it.action for it in rep.items}
    assert actions[stored[1]] == "kept"
    # The preservation is itself journaled — no silent rewrite.
    entries = _journal(conn, stored[1])
    assert entries and entries[0]["action"] == "kept"
    assert entries[0]["reason"] == "store_only_preserved"


# --------------------------------- both-sides conflict: fresh store wins ---
def test_both_sides_conflict_fresh_store_state_wins_no_silent_rewrite(tmp_path):
    conn = _conn(tmp_path)
    org = _org(conn)
    stored = _metered_ids(conn, org, 1)
    before = _store_row(conn, stored[0])
    # Tracked copy diverges (stale row hash) while the record exists in both.
    tracked = [{"event_id": stored[0], "row_hash": "0" * 64}]

    rep = reconcile_union.reconcile_union(conn, tracked, apply=True)
    actions = {it.event_id: it.action for it in rep.items}
    assert actions[stored[0]] == "conflict"
    after = _store_row(conn, stored[0])
    assert after["id"] == before["id"] and after["row_hash"] == before["row_hash"]
    assert after["reconciliation_note"] == before["reconciliation_note"]
    # Divergence reason recorded in the journal; store state untouched.
    entries = _journal(conn, stored[0])
    assert entries and entries[0]["action"] == "conflict"
    assert entries[0]["reason"] == "conflict_store_state_wins"


# -------------------------------------------- explicit operator flag only ---
def test_no_deletion_without_explicit_operator_flag(tmp_path):
    conn = _conn(tmp_path)
    org = _org(conn)
    stored = _metered_ids(conn, org, 2)
    tracked = [_tracked(conn, stored[0])]

    rep = reconcile_union.reconcile_union(conn, tracked, apply=True)
    assert rep.deleted == 0
    assert _store_row(conn, stored[1]) is not None

    rep2 = reconcile_union.reconcile_union(
        conn, tracked, apply=True, drop_missing=True)
    assert rep2.deleted == 1
    assert rep2.would_drop == [stored[1]]
    assert _store_row(conn, stored[1]) is None
    # Journal accumulates history: the latest entry records the deletion.
    entries = _journal(conn, stored[1])
    assert entries[-1]["action"] == "deleted"
    assert "operator_flag" in entries[-1]["reason"]


def test_identical_records_match_and_journal_nothing(tmp_path):
    conn = _conn(tmp_path)
    org = _org(conn)
    stored = _metered_ids(conn, org, 1)
    tracked = [_tracked(conn, stored[0])]

    rep = reconcile_union.reconcile_union(conn, tracked, apply=True)
    actions = {it.event_id: it.action for it in rep.items}
    assert actions[stored[0]] == "match"
    assert _journal(conn) == [], "matching records need no resolution reason"
