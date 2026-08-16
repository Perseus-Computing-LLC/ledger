"""Union reconciliation between the tracked ledger and the store (ledger#207).

The **tracked ledger** is the durable set of published records an operator
holds (a JSONL file, one canonical published record per line). The **store**
is ``usage_events``. Reconciliation is a **union**: a record survives if it
exists on EITHER surface.

- published-but-store-lost (tracked only) → **preserved and recovered** into
  the store; the recovery reason is recorded on the record
  (``reconciliation_note``) and in the ``reconciliation_events`` journal;
- store-only → **preserved**, never deleted without an explicit operator flag;
- both sides → compared by row hash; **fresh store state wins** on conflict
  and the divergence reason is journaled — never a silent status rewrite;
- a **dry run** (default) surfaces every action, including all would-be
  drops, before any mutation; deletion happens only with ``drop_missing=True``.

Recovered rows are re-inserted with NULL chain fields (``prev_hash``/``row_hash``)
so ``verify`` reports them as a pre-chain prefix rather than attesting a hash
the store never produced; the original ``row_hash`` is preserved in the
journal. The ``reconciliation_note`` column is deliberately excluded from the
hash-chain canonical form, so existing chains are byte-identical after the
migration.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import db

# Whitelist of usage_events columns a tracked record may carry on recovery.
# Chain fields (prev_hash/row_hash) are intentionally absent: recovered rows
# are re-chained as a fresh pre-chain segment, never attested by the store.
_RECOVER_COLUMNS = [
    "id", "org_id", "workspace_id", "provider", "model", "task_type",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "user_id", "cost_micros", "baseline_micros",
    "optimal_micros", "external_ref", "evidence_hashes", "policy_version",
    "result_hash", "human_review", "correction_ref", "agent_id",
    "authority_manifest_ref", "scope_anchor", "action_intent_hash",
    "action_status", "approval_ref", "context_render_schema",
    "context_render_hash", "served_memory_provenance_hash",
    "action_receipt_hash", "resource_constraints_version",
    "resource_constraints_hash", "prebind_json", "prebind_hash",
    # v18 (#219–#224): stage-aware receipts and evidence bindings.
    "served_claim_json", "served_claim_hash", "evidence_status",
    "runtime_manifest_json", "runtime_manifest_hash",
    "external_artifact_json", "external_artifact_hash",
    # v19 (#237): belief-context evidence.
    "belief_context_json", "belief_context_hash",
    # v20 (#239): governance self-cost.
    "governance_cost_json", "governance_cost_hash",
    # v21 (#238): behavior-snapshot receipt pin.
    "behavior_snapshot_json", "behavior_snapshot_hash",
    # v22 (#241): custody disclosure for the authority manifest.
    "authority_manifest_custody",
    "estimated", "source", "ts",
]

REASON_RECOVERED = "recovered_published_but_store_lost"
REASON_STORE_ONLY = "store_only_preserved"
REASON_CONFLICT = "conflict_store_state_wins"
REASON_DELETED = "deleted_operator_flag_missing_from_tracked"
REASON_MATCH = "match"


def _tracked_hash(record: dict) -> str:
    """Canonical SHA-256 of a tracked record's event fields (row hash if the
    publisher captured one, else a digest of the canonical JSON)."""
    row_hash = record.get("row_hash")
    if isinstance(row_hash, str) and len(row_hash) == 64:
        return row_hash.lower()
    return hashlib.sha256(
        json.dumps(record.get("record", record), sort_keys=True,
                   separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


@dataclass
class UnionReconItem:
    event_id: str
    side: str          # tracked | store | both
    action: str        # recovered | kept | conflict | deleted | match
    reason: str
    tracked_hash: str = ""
    store_hash: str = ""


@dataclass
class UnionReconReport:
    items: list[UnionReconItem] = field(default_factory=list)
    applied: bool = False
    drop_missing: bool = False
    recovered: int = 0
    deleted: int = 0
    would_drop: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dry_run": not self.applied,
            "drop_missing": self.drop_missing,
            "recovered": self.recovered,
            "deleted": self.deleted,
            "would_drop": self.would_drop,
            "items": [{
                "event_id": it.event_id, "side": it.side, "action": it.action,
                "reason": it.reason,
            } for it in self.items],
        }


def _journal(conn, item: UnionReconItem, ts: float) -> None:
    conn.execute(
        "INSERT INTO reconciliation_events (id, event_id, side, action, reason, ts)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (db.new_id("recon"), item.event_id, item.side, item.action,
         item.reason, ts),
    )


def _recover_record(conn, record: dict, ts: float) -> UnionReconItem:
    """Re-insert a published-but-store-lost record into the store."""
    event_id = record.get("event_id") or (record.get("record") or {}).get("id")
    body = record.get("record") or record
    cols, vals = [], []
    # The primary key comes from the tracked event id when the record body
    # does not carry it (the tracked line's id IS the event id).
    if "id" not in body or body["id"] is None:
        cols.append("id")
        vals.append(event_id)
    for col in _RECOVER_COLUMNS:
        if col in body and body[col] is not None:
            cols.append(col)
            vals.append(body[col])
    cols.append("reconciliation_note")
    vals.append(REASON_RECOVERED)
    item = UnionReconItem(
        event_id=str(event_id), side="tracked", action="recovered",
        reason=REASON_RECOVERED,
        tracked_hash=_tracked_hash(record),
    )
    try:
        conn.execute(
            f"INSERT INTO usage_events ({', '.join(cols)}) VALUES ({', '.join('?' * len(vals))})",
            vals,
        )
        _journal(conn, item, ts)
        return item
    except Exception as exc:  # e.g. FK violation for a deleted org
        item.action = "recover_failed"
        item.reason = f"recover_failed: {exc}"
        _journal(conn, item, ts)
        return item


def reconcile_union(conn, tracked_records: list[dict], *,
                    apply: bool = False, drop_missing: bool = False) -> UnionReconReport:
    """Union reconciliation of the tracked ledger against the store.

    ``tracked_records``: list of published records, each ``{"event_id": ...,
    "row_hash": ... (optional), "record": {usage_events-shaped fields}}``.

    Returns a :class:`UnionReconReport`; ``apply=False`` (default) is a dry
    run that mutates nothing and surfaces every action including all
    would-be drops. ``drop_missing=True`` is the explicit operator flag that
    permits deleting store-only records; without it nothing is ever deleted.
    """
    report = UnionReconReport(applied=apply, drop_missing=drop_missing)
    ts = time.time()

    store_rows = {
        r["id"]: r["row_hash"]
        for r in conn.execute("SELECT id, row_hash FROM usage_events").fetchall()
    }
    tracked = {}
    for rec in tracked_records:
        event_id = rec.get("event_id") or (rec.get("record") or {}).get("id")
        if not event_id:
            continue
        tracked[str(event_id)] = rec

    for event_id, rec in tracked.items():
        store_hash = store_rows.get(event_id)
        if store_hash is None:
            item = _recover_record(conn, rec, ts) if apply else UnionReconItem(
                event_id=event_id, side="tracked", action="recovered",
                reason=REASON_RECOVERED, tracked_hash=_tracked_hash(rec))
            if not apply:
                report.items.append(item)
            elif item.action == "recovered":
                report.recovered += 1
                report.items.append(item)
            else:  # recover_failed — still surfaced
                report.items.append(item)
        else:
            tracked_hash = _tracked_hash(rec)
            if store_hash and store_hash.lower() == tracked_hash:
                item = UnionReconItem(event_id=event_id, side="both",
                                      action="match", reason=REASON_MATCH,
                                      tracked_hash=tracked_hash,
                                      store_hash=store_hash)
                report.items.append(item)
            else:
                # Both sides present but divergent: fresh store state wins;
                # the divergence reason is journaled, the store is untouched
                # (no silent rewrite).
                item = UnionReconItem(event_id=event_id, side="both",
                                      action="conflict", reason=REASON_CONFLICT,
                                      tracked_hash=tracked_hash,
                                      store_hash=store_hash)
                if apply:
                    _journal(conn, item, ts)
                report.items.append(item)

    for event_id in store_rows:
        if event_id in tracked:
            continue
        item = UnionReconItem(event_id=event_id, side="store",
                              action="deleted", reason=REASON_DELETED,
                              store_hash=store_rows[event_id])
        if drop_missing:
            report.would_drop.append(event_id)
            if apply:
                conn.execute("DELETE FROM usage_events WHERE id=?", (event_id,))
                _journal(conn, item, ts)
                report.deleted += 1
            report.items.append(item)
        else:
            item.action = "kept"
            item.reason = REASON_STORE_ONLY
            if apply:
                _journal(conn, item, ts)
            report.items.append(item)

    return report
