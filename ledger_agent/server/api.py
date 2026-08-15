"""JSON API shaping for the dashboard poller and external integrations."""
from __future__ import annotations

import csv
import io
import json

from .. import db, evidence_levels, metering, pricing, savings
from ..prebind import validate_prebind


def default_org_id(conn) -> str | None:
    orgs = db.list_orgs(conn)
    return orgs[0]["id"] if orgs else None


def summary_json(conn, org_id: str) -> dict:
    """A flatter, poll-friendly view of an org's current state."""
    s = metering.org_summary(conn, org_id)
    return {
        "org_id": org_id,
        "org": s["org"]["name"] if s["org"] else None,
        "tier": s["tier"],
        "balance": s["balance"],
        "windows": s["windows"],
        "tracked_tokens_mtd": s["tracked_tokens_mtd"],
        "tracked_limit": s["tracked_limit"],
        "by_provider": s["by_provider"],
        "by_workspace": s["by_workspace"],
        "by_task_type": s["by_task_type"],
        "by_user": s["by_user"],
        "seats": s["seats"],
        "provider_health": s["provider_health"],
        "alerts": s["alerts"],
    }


def orgs_json(conn, orgs=None, limit=None, offset=0) -> list[dict]:
    if orgs is None:
        rows = db.list_orgs(conn, limit=limit, offset=offset)
    else:
        rows = orgs
        if limit is not None:
            rows = rows[offset:offset + limit]
    return [
        {"id": o["id"], "name": o["name"], "slug": o["slug"], "tier": o["tier"],
         "balance": db.get_balance(conn, o["id"])}
        for o in rows
    ]


# ----------------------------------------------------------- paged list views ---
def _page(items: list[dict], limit: int) -> dict:
    """Wrap a page of rows with a cursor. ``next_before`` is the ``_rowid`` to
    pass as ``before`` for the next page, or None when the page wasn't full."""
    next_before = items[-1]["_rowid"] if len(items) == limit and items else None
    return {"items": items, "next_before": next_before, "limit": limit}


def ledger_json(conn, org_id: str, limit: int = 50, before=None) -> dict:
    return _page(db.ledger_history(conn, org_id, limit=limit, before=before), limit)


def events_json(conn, org_id: str, limit: int = 50, before=None) -> dict:
    return _page(metering.recent_events(conn, org_id, limit=limit, before=before), limit)


def replay_receipt_prebind(conn, org_id: str, external_ref: str, **kwargs) -> dict:
    """Re-evaluate a stored prebind without mutating the usage history."""
    from ..prebind import replay_prebind

    rows = db.events_by_ref(conn, org_id, external_ref)
    if not rows:
        raise ValueError("receipt not found")
    payload = rows[0]["prebind_json"]
    if payload is None:
        raise ValueError("receipt has no prebind block")
    prior = json.loads(payload)
    valid, errors = validate_prebind(prior)
    if not valid:
        raise ValueError("stored prebind is invalid: " + ", ".join(errors))
    return replay_prebind(prior, **kwargs)


_EXPORT_COLUMNS = ["id", "ts", "provider", "model", "task_type", "workspace",
                   "input_tokens", "output_tokens", "cache_read_tokens",
                   "cache_write_tokens", "reasoning_tokens", "user_id",
                   "cost_usd", "baseline_usd",
                   "external_ref", "estimated", "source"]


def _csv_safe(value):
    """Neutralize spreadsheet formula injection: a cell whose text begins with
    ``= + - @`` (or a leading tab/CR that a spreadsheet strips to reach them) is
    prefixed with a single quote so Excel/Sheets treat it as text, not a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def audit_json(conn, org_id: str, *, hmac_key: bytes | None = None,
               external_ref: str | None = None,
               key_registry: dict[str, bytes] | None = None,
               sign_key_id: str | None = None) -> dict:
    """Return either an organization audit summary or one task evidence receipt.

    ``external_ref`` selects the additive, task-scoped receipt path.  The ref is
    already an optional hash-covered field on usage events, so this turns the
    existing immutable chain into a portable evidence view without altering the
    stable ingest contract.

    ``key_registry`` maps declared signing key ids to key bytes. When
    ``sign_key_id`` names one of them (or ``"default"`` with ``hmac_key``), the
    receipt is signed and — when it records a terminal action stage — carries a
    trusted-key attestation. Receipts always include a
    ``verification.evidence`` block reporting the highest evidence level the
    retained objects actually verify (#235).
    """
    if external_ref is not None:
        org = db.get_org(conn, org_id)
        integrity = db.verify_chain(conn, org_id=org_id, hmac_key=hmac_key)
        org_chain = integrity["orgs"][0] if integrity["orgs"] else {}
        rows = list(reversed(db.events_by_ref(conn, org_id, external_ref)))
        events = []
        for row in rows:
            events.append({
                "event_id": row["id"],
                "ts": row["ts"],
                "actor": row["user_id"],
                "action": row["task_type"],
                "model_config": {"provider": row["provider"], "model": row["model"]},
                "external_ref": row["external_ref"],
                "evidence": {
                    "source_hashes": json.loads(row["evidence_hashes"])
                    if row["evidence_hashes"] else [],
                },
                "decision_context": {
                    "policy_version": row["policy_version"],
                    "result_hash": row["result_hash"],
                    "human_review": row["human_review"],
                    "correction_ref": row["correction_ref"],
                },
                "context_render_binding": {
                    "schema_version": row["context_render_schema"],
                    "render_hash": row["context_render_hash"],
                    "served_memory_provenance_hash": row["served_memory_provenance_hash"],
                    "action_receipt_hash": row["action_receipt_hash"],
                },
                "prebind": json.loads(row["prebind_json"])
                if row["prebind_json"] is not None else None,
                "served_claim": json.loads(row["served_claim_json"])
                if row["served_claim_json"] is not None else None,
                "evidence_status": row["evidence_status"],
                "runtime_manifest": json.loads(row["runtime_manifest_json"])
                if row["runtime_manifest_json"] is not None else None,
                "external_artifact_binding": json.loads(row["external_artifact_json"])
                if row["external_artifact_json"] is not None else None,
                "belief_context": json.loads(row["belief_context_json"])
                if row["belief_context_json"] is not None else None,
                "governance_cost": json.loads(row["governance_cost_json"])
                if row["governance_cost_json"] is not None else None,
                "behavior_snapshot": json.loads(row["behavior_snapshot_json"])
                if row["behavior_snapshot_json"] is not None else None,
                "action_authorization": {
                    "agent_id": row["agent_id"],
                    "authority_manifest_ref": row["authority_manifest_ref"],
                    "scope_anchor": row["scope_anchor"],
                    "action_intent_hash": row["action_intent_hash"],
                    "status": row["action_status"],
                    "approval_ref": row["approval_ref"],
                    **({"resource_constraints_version": row["resource_constraints_version"],
                        "resource_constraints_hash": row["resource_constraints_hash"]}
                       if row["resource_constraints_version"] is not None or row["resource_constraints_hash"] is not None else {}),
                    **({"authority_manifest_custody": row["authority_manifest_custody"]}
                       if row["authority_manifest_custody"] is not None else {}),
                },
                "resource_allocation": {
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "cache_read_tokens": row["cache_read_tokens"],
                    "cache_write_tokens": row["cache_write_tokens"],
                    "reasoning_tokens": row["reasoning_tokens"],
                    "cost_usd": db.micros_to_usd(row["cost_micros"]),
                    "estimated": bool(row["estimated"]),
                },
                "prev_hash": row["prev_hash"],
                "row_hash": row["row_hash"],
            })
        receipt = {
            "receipt_version": "perseus-evidence-receipt/v1",
            "organization": {"id": org_id, "name": org["name"] if org else None},
            "external_ref": external_ref,
            "events": events,
            "verification": {
                "chain_ok": org_chain.get("status") == "ok",
                "verified_events": org_chain.get("verified", 0),
                "pre_chain_events": org_chain.get("pre_chain", 0),
                "unverifiable_events": org_chain.get("unverifiable_events", 0),
                "coverage": org_chain.get("coverage", {}),
                "method": integrity.get("method", "sha256"),
                "hash_method": integrity.get("hash_method", integrity.get("method", "sha256")),
            },
        }
        # Optional receipt signature + trusted-key attestation (#235): the
        # attestation binds the first terminal stage + reason, the signature
        # binds the content (attestation included). Both exclude the
        # verification block from the signed bytes — it is re-attached below.
        verification = receipt.pop("verification")
        if sign_key_id is not None:
            key = evidence_levels.resolve_key(key_registry, sign_key_id, hmac_key)
            if key is not None:
                terminal = evidence_levels.first_terminal_status(events)
                if terminal is not None:
                    receipt = evidence_levels.attest_receipt(
                        receipt, key_id=sign_key_id, key=key,
                        stage=terminal, reason="ledger-audit-render",
                    )
                receipt = evidence_levels.sign_receipt(
                    receipt, key_id=sign_key_id, key=key,
                )
        receipt["verification"] = verification
        receipt["verification"]["evidence"] = evidence_levels.verify_receipt_evidence(
            conn, org_id, receipt, hmac_key=hmac_key,
            key_registry=key_registry,
            checkpoints=db.list_checkpoints(conn, org_id),
        )
        return receipt

    import time
    period = time.strftime("%Y-%m", time.gmtime())
    org = db.get_org(conn, org_id)
    tier_key = org["tier"] if org else "free"
    report = savings.savings_share_report(conn, org_id, period).as_dict()
    integrity = db.verify_chain(conn, org_id=org_id, hmac_key=hmac_key)
    checkpoints = db.list_checkpoints(conn, org_id)
    donation = pricing.recommended_donation_usd(
        tier_key, report.get("gross_savings_usd", 0.0)
    )
    return {
        "org_id": org_id,
        "tier": tier_key,
        "period": period,
        "savings": report,
        "recommended_donation_usd": donation,
        "recommended_donation_bps": pricing.tier(tier_key).donation_bps,
        "ledger_integrity": integrity,
        "latest_checkpoint": dict(checkpoints[-1]) if checkpoints else None,
        "audit_access": pricing.tier(tier_key).audit_access,
        "verification": {
            "chain_ok": integrity.get("ok", False),
            "method": integrity.get("method", "sha256"),
            "hash_method": integrity.get("hash_method", integrity.get("method", "sha256")),
            "verified_events": sum(o.get("verified", 0) for o in integrity.get("orgs", [])),
            "pre_chain_events": sum(o.get("pre_chain", 0) for o in integrity.get("orgs", [])),
            "unverifiable_events": sum(o.get("unverifiable_events", 0) for o in integrity.get("orgs", [])),
            "coverage": (
                integrity["orgs"][0].get("coverage", {})
                if len(integrity.get("orgs", [])) == 1 else {}
            ),
            "authoritative_billing_required": report.get("billing_provisional") is not False,
        },
    }


def export_csv(conn, org_id: str, since=None, until=None) -> str:
    """Org-scoped usage events as CSV text (fix #66).

    Cells are formula-injection neutralized (2026-07-05 security review):
    ``provider``/``model``/``workspace``/``task_type`` are tenant-controlled at
    ingest, so a crafted value like ``=HYPERLINK(...)`` would otherwise execute
    when a teammate opens the export in a spreadsheet."""
    rows = db.export_events(conn, org_id, since=since, until=until)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_EXPORT_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: _csv_safe(v) for k, v in r.items()})
    return buf.getvalue()


def export_json(conn, org_id: str, since=None, until=None) -> dict:
    rows = db.export_events(conn, org_id, since=since, until=until)
    return {"org_id": org_id, "count": len(rows), "events": rows}
