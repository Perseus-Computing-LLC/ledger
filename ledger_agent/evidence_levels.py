"""Evidence levels for receipts (#235).

Receipts must state what they prove. Four calibrated levels, borrowed from
arXiv:2608.11632 §3.3 / Table 4 — "A signature is not evidence that its
transaction committed":

  1. ``structural`` — canonical syntax, typed bindings, and a valid receipt
     signature under the declared key.
  2. ``attested`` — a trusted key attests the first terminal stage + reason.
     This does NOT independently establish correct evaluation.
  3. ``replay`` — retained inputs + pinned versions reproduce the stated
     decision/transition.
  4. ``inclusion`` — a certified snapshot / log proof places the receipt and
     outcome (and for Commit receipts, the head + lineage) in durable state.

The verifier reports the highest level actually achieved with the objects
currently retained. Watermark reclamation may downgrade Replay (inputs gone)
but never Inclusion (the anchor is durable). Higher levels are only claimed
when their verification objects exist (paper assumption A9).

Signatures are HMAC-SHA256 (the repo's stdlib-only signing scheme, shared
with chain checkpoints): a ``signature`` block covers the canonical receipt
content (including any ``attestation`` block), and an ``attestation`` block
covers the canonical receipt content (excluding the attestation itself) plus
the attested stage and reason.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
from typing import Any, Mapping, Optional

from . import db
from .prebind import (
    PREBIND_V2_SCHEMA,
    replay_prebind,
    replay_prebind_v2,
    validate_prebind,
)
from .receipts import validate_belief_context

# ── level ladder ────────────────────────────────────────────────────────────

EVIDENCE_LEVELS = ("structural", "attested", "replay", "inclusion")
_LEVEL_INDEX = {name: i for i, name in enumerate(EVIDENCE_LEVELS)}
TERMINAL_STATUSES = {"executed", "failed", "cancelled"}
RECEIPT_VERSIONS = {"perseus-evidence-receipt/v1"}
DEFAULT_KEY_ID = "default"
_SIG_EXCLUDED = frozenset({"signature", "verification"})
_ATTEST_EXCLUDED = frozenset({"signature", "verification", "attestation"})

# ── shared helpers ──────────────────────────────────────────────────────────


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value.lower().isalnum()
        and set(value.lower()) <= set("0123456789abcdef")
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def canonical_receipt(receipt: Mapping[str, Any], *,
                      exclude: frozenset[str] = _SIG_EXCLUDED) -> bytes:
    """Canonical byte form of a receipt, excluding non-content blocks.

    ``signature`` and ``verification`` are always excluded: the signature
    cannot cover itself and the verification block is computed at render
    time. The attestation is excluded only when signing/verifying the
    attestation's own payload.
    """
    body = {k: v for k, v in receipt.items() if k not in exclude}
    return _canonical(body)


def _hmac_hex(key: bytes, payload: bytes) -> str:
    return _hmac.new(key, payload, hashlib.sha256).hexdigest()


def resolve_key(key_registry: Optional[Mapping[str, bytes]],
                key_id: str, hmac_key: Optional[bytes]) -> Optional[bytes]:
    """Resolve declared signing key material, never the key value itself.

    A ``key_registry`` maps declared ``key_id`` strings to key bytes; the
    chain ``hmac_key`` stands in for the reserved ``"default"`` key id.
    """
    if key_registry and key_id in key_registry:
        return key_registry[key_id]
    if key_id == DEFAULT_KEY_ID and hmac_key:
        return hmac_key
    return None


# ── receipt signature (structural) ──────────────────────────────────────────


def sign_receipt(receipt: Mapping[str, Any], *, key_id: str,
                 key: bytes) -> dict[str, Any]:
    """Return the receipt with a content-binding signature attached.

    Covers the canonical receipt content including any ``attestation`` block,
    so content, attestation, and signature are bound together.
    """
    out = dict(receipt)
    out.pop("verification", None)
    out.pop("signature", None)
    sig = _hmac_hex(key, canonical_receipt(out))
    out["signature"] = {"key_id": key_id, "algo": "hmac-sha256", "sig": sig}
    return out


def verify_receipt_signature(receipt: Mapping[str, Any],
                             key_registry: Optional[Mapping[str, bytes]] = None,
                             hmac_key: Optional[bytes] = None) -> tuple[bool, str]:
    """Check the receipt's declared signature under the declared key."""
    signature = receipt.get("signature")
    if not isinstance(signature, dict):
        return False, "structural:signature_shape"
    key_id = signature.get("key_id")
    sig = signature.get("sig")
    if not isinstance(key_id, str) or not key_id.strip() \
            or not (isinstance(sig, str) and _is_sha256(sig)):
        return False, "structural:signature_shape"
    key = resolve_key(key_registry, key_id, hmac_key)
    if key is None:
        return False, "structural:signature_unknown_key"
    expected = _hmac_hex(key, canonical_receipt(receipt))
    if expected != sig.lower():
        return False, "structural:signature_invalid"
    return True, "structural:signature_ok"


# ── attestation (attested) ──────────────────────────────────────────────────


def attest_receipt(receipt: Mapping[str, Any], *, key_id: str, key: bytes,
                   stage: str, reason: str) -> dict[str, Any]:
    """Attach a trusted-key attestation of a terminal stage + reason.

    The attestation signature covers the receipt content (excluding the
    attestation itself, so it cannot self-cover) plus the attested stage and
    reason. Attesting does NOT establish inclusion — it is a claim about the
    terminal stage only.
    """
    if stage not in TERMINAL_STATUSES:
        raise ValueError(f"attestation stage must be one of {sorted(TERMINAL_STATUSES)}")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("attestation reason must be a non-empty string")
    out = dict(receipt)
    out.pop("verification", None)
    out.pop("attestation", None)
    content_digest = hashlib.sha256(
        canonical_receipt(out, exclude=_ATTEST_EXCLUDED)
    ).hexdigest()
    payload = f"{content_digest}\x1f{stage}\x1f{reason}".encode("utf-8")
    out["attestation"] = {
        "key_id": key_id, "algo": "hmac-sha256",
        "stage": stage, "reason": reason, "sig": _hmac_hex(key, payload),
    }
    return out


def verify_receipt_attestation(receipt: Mapping[str, Any],
                               key_registry: Optional[Mapping[str, bytes]] = None,
                               hmac_key: Optional[bytes] = None) -> tuple[bool, str]:
    """Check a trusted-key attestation of the receipt's terminal stage."""
    attestation = receipt.get("attestation")
    if not isinstance(attestation, dict):
        return False, "attested:missing_attestation"
    key_id = attestation.get("key_id")
    stage = attestation.get("stage")
    reason = attestation.get("reason")
    sig = attestation.get("sig")
    if not isinstance(key_id, str) or not key_id.strip() \
            or not (isinstance(sig, str) and _is_sha256(sig)):
        return False, "attested:signature_shape"
    if stage not in TERMINAL_STATUSES:
        return False, "attested:stage"
    if not isinstance(reason, str) or not reason.strip():
        return False, "attested:reason"
    terminal = first_terminal_status(receipt.get("events") or [])
    if terminal is None:
        return False, "attested:no_terminal_stage"
    if stage != terminal:
        return False, "attested:stage_mismatch"
    key = resolve_key(key_registry, key_id, hmac_key)
    if key is None:
        return False, "attested:unknown_key"
    content_digest = hashlib.sha256(
        canonical_receipt(receipt, exclude=_ATTEST_EXCLUDED)
    ).hexdigest()
    payload = f"{content_digest}\x1f{stage}\x1f{reason}".encode("utf-8")
    if _hmac_hex(key, payload) != sig.lower():
        return False, "attested:bad_signature"
    return True, "attested:ok"


# ── structural ──────────────────────────────────────────────────────────────


def first_terminal_status(events: list[dict[str, Any]]) -> Optional[str]:
    """The first terminal action status among receipt events, if any."""
    for event in events:
        if not isinstance(event, dict):
            continue
        auth = event.get("action_authorization")
        if isinstance(auth, dict) and auth.get("status") in TERMINAL_STATUSES:
            return auth["status"]
    return None


def commit_receipt(events: list[dict[str, Any]]) -> bool:
    """True when the receipt records a Commit (an executed action)."""
    for event in events:
        if not isinstance(event, dict):
            continue
        auth = event.get("action_authorization")
        if isinstance(auth, dict) and auth.get("status") == "executed":
            return True
    return False


def verify_structural(receipt: Mapping[str, Any], *,
                      key_registry: Optional[Mapping[str, bytes]] = None,
                      hmac_key: Optional[bytes] = None) -> tuple[bool, str]:
    """Canonical syntax, typed bindings, and signature authenticity.

    Any malformed receipt fails here with a stable ``structural:<code>``
    reason — the same code for the same defect, call after call.
    """
    if not isinstance(receipt, Mapping):
        return False, "structural:not_object"
    if receipt.get("receipt_version") not in RECEIPT_VERSIONS:
        return False, "structural:receipt_version"
    org = receipt.get("organization")
    if not isinstance(org, Mapping) or not isinstance(org.get("id"), str) \
            or not org["id"].strip():
        return False, "structural:organization"
    if not isinstance(receipt.get("external_ref"), str) \
            or not receipt["external_ref"].strip():
        return False, "structural:external_ref"
    events = receipt.get("events")
    if not isinstance(events, list) or not events:
        return False, "structural:events"
    prev_row_hash: Optional[str] = None
    for i, event in enumerate(events):
        if not isinstance(event, Mapping):
            return False, f"structural:events[{i}]"
        if not isinstance(event.get("event_id"), str) \
                or not event["event_id"].strip():
            return False, f"structural:event_id[{i}]"
        if not isinstance(event.get("ts"), (int, float)):
            return False, f"structural:ts[{i}]"
        alloc = event.get("resource_allocation")
        if not isinstance(alloc, Mapping):
            return False, f"structural:resource_allocation[{i}]"
        for field in ("input_tokens", "output_tokens", "cost_usd"):
            if not isinstance(alloc.get(field), (int, float)):
                return False, f"structural:resource_allocation[{i}].{field}"
        row_hash = event.get("row_hash")
        prev_hash = event.get("prev_hash")
        if row_hash is not None and not _is_sha256(row_hash):
            return False, f"structural:row_hash[{i}]"
        if prev_hash is not None and not _is_sha256(prev_hash):
            return False, f"structural:prev_hash[{i}]"
        # Consecutive retained events must link; the first receipt event may
        # legitimately point at a predecessor outside the selected task.
        if prev_row_hash is not None and row_hash is not None \
                and prev_hash != prev_row_hash:
            return False, "structural:event_hash_link"
        if row_hash is not None:
            prev_row_hash = row_hash
        if event.get("prebind") is not None:
            valid, _ = validate_prebind(event["prebind"])
            if not valid:
                return False, f"structural:prebind[{i}]"
        if event.get("belief_context") is not None:
            valid, _ = validate_belief_context(event["belief_context"])
            if not valid:
                return False, f"structural:belief_context[{i}]"
    claimed = receipt.get("claimed_evidence_level")
    if claimed is not None and claimed not in EVIDENCE_LEVELS:
        return False, "structural:claimed_level"
    signature = receipt.get("signature")
    if signature is not None:
        if not isinstance(signature, Mapping) \
                or not isinstance(signature.get("key_id"), str) \
                or not _is_sha256(signature.get("sig")):
            return False, "structural:signature_shape"
    attestation = receipt.get("attestation")
    if attestation is not None:
        if not isinstance(attestation, Mapping) \
                or not isinstance(attestation.get("key_id"), str) \
                or not isinstance(attestation.get("stage"), str) \
                or not isinstance(attestation.get("reason"), str) \
                or not _is_sha256(attestation.get("sig")):
            return False, "structural:attestation_shape"
    if signature is not None:
        ok, reason = verify_receipt_signature(receipt, key_registry, hmac_key)
        if not ok:
            return False, reason
    return True, "structural:ok"


# ── attested ────────────────────────────────────────────────────────────────


def verify_attested(receipt: Mapping[str, Any], *,
                    key_registry: Optional[Mapping[str, bytes]] = None,
                    hmac_key: Optional[bytes] = None) -> tuple[bool, str]:
    """A trusted key attests the first terminal stage + reason."""
    return verify_receipt_attestation(receipt, key_registry, hmac_key)


# ── replay ──────────────────────────────────────────────────────────────────


def _event_row(conn, org_id: str, event: Mapping[str, Any]):
    return conn.execute(
        "SELECT rowid AS _rowid, row_hash, prev_hash, prebind_json, prebind_hash "
        "FROM usage_events WHERE org_id=? AND id=?",
        (org_id, event.get("event_id")),
    ).fetchone()


_DECISION_OUTCOMES = {"allow", "hold", "deny"}


def _recorded_outcome(event: Mapping[str, Any]) -> Optional[str]:
    auth = event.get("action_authorization")
    if isinstance(auth, Mapping) and auth.get("status") in TERMINAL_STATUSES:
        return auth["status"]
    prebind = event.get("prebind")
    if isinstance(prebind, Mapping) and prebind.get("boundary_outcome") in _DECISION_OUTCOMES:
        return prebind["boundary_outcome"]
    return None


def _reproduction_matches(recorded: str, result: Mapping[str, Any]) -> bool:
    outcome = result.get("replayed_boundary_outcome")
    admission = result.get("admission")
    if recorded in ("executed", "allow"):
        return outcome == "allow" and admission == "admitted"
    if recorded in ("hold", "deny"):
        return admission == "not_admitted" and outcome in {"hold", "deny"}
    return False


def _replay_event(prior: Mapping[str, Any], event: Mapping[str, Any]) -> Mapping[str, Any]:
    auth_raw = event.get("action_authorization")
    auth = auth_raw if isinstance(auth_raw, Mapping) else {}
    decision_raw = event.get("decision_context")
    decision = decision_raw if isinstance(decision_raw, Mapping) else {}
    evidence_raw = event.get("evidence")
    evidence = evidence_raw if isinstance(evidence_raw, Mapping) else {}
    if prior.get("schema_version") == PREBIND_V2_SCHEMA:
        return replay_prebind_v2(
            prior,
            current_authority_ref=auth.get("authority_manifest_ref"),
            current_trusted_scope=auth.get("scope_anchor"),
            current_evidence_hashes=evidence.get("source_hashes"),
            current_policy_version=decision.get("policy_version"),
            current_context_hash=prior.get("context_hash"),
            current_policy_hash=prior.get("policy_hash"),
        )
    return replay_prebind(
        prior,
        current_authority_ref=auth.get("authority_manifest_ref"),
        current_trusted_scope=auth.get("scope_anchor"),
        current_evidence_hashes=evidence.get("source_hashes"),
        current_policy_version=decision.get("policy_version"),
    )


def verify_replay(conn, org_id: str, receipt: Mapping[str, Any]) -> tuple[bool, str]:
    """Retained inputs + pinned versions reproduce the stated transition.

    Every event that carries a prebind must (a) be valid, (b) still be the
    retained copy when a durable store is available, and (c) reproduce the
    event's recorded outcome when replayed against the event's own pinned
    values. Events without a terminal outcome are not replay claims.
    """
    events = receipt.get("events") or []
    replayable = [e for e in events if isinstance(e, Mapping) and e.get("prebind") is not None]
    if not replayable:
        # Distinguish explicit watermark reclamation (the durable row still
        # records that replay inputs once existed) from receipts that never
        # carried replay objects.
        if conn is not None:
            for event in events:
                if not isinstance(event, Mapping) \
                        or _recorded_outcome(event) is None:
                    continue
                row = _event_row(conn, org_id, event)
                if row is not None and row["prebind_hash"] is not None:
                    return False, "replay:inputs_reclaimed"
        return False, "replay:no_replayable_inputs"
    for i, event in enumerate(replayable):
        prior = event["prebind"]
        valid, _ = validate_prebind(prior)
        if not valid:
            return False, f"replay:invalid_prebind[{i}]"
        if conn is not None:
            row = _event_row(conn, org_id, event)
            if row is None:
                return False, "replay:inputs_reclaimed"
            stored = json.loads(row["prebind_json"]) if row["prebind_json"] else None
            if stored != prior:
                return False, "replay:inputs_mismatch"
        recorded = _recorded_outcome(event)
        if recorded is None:
            return False, f"replay:no_terminal_outcome[{i}]"
        result = _replay_event(prior, event)
        if result.get("changed_fields"):
            return False, f"replay:pinned_version_changed[{i}]"
        if not _reproduction_matches(recorded, result):
            return False, f"replay:reproduction_mismatch[{i}]"
    return True, "replay:ok"


# ── inclusion ───────────────────────────────────────────────────────────────


def verify_inclusion(conn, org_id: str, receipt: Mapping[str, Any], *,
                     checkpoints: Optional[list] = None,
                     hmac_key: Optional[bytes] = None) -> tuple[bool, str, Optional[dict]]:
    """A certified snapshot / log proof places the receipt in durable state.

    Requires (1) every receipt event present and unaltered in the durable
    store, (2) the organization chain verifying clean, and (3) a retained
    checkpoint anchoring a chain head at or beyond the receipt's last event.
    For Commit receipts the anchored head + lineage is what makes the
    inclusion claim load-bearing.
    """
    if conn is None:
        return False, "inclusion:no_store", None
    events = receipt.get("events") or []
    if not events:
        return False, "inclusion:no_events", None
    max_rowid = 0
    for i, event in enumerate(events):
        if not isinstance(event, Mapping):
            return False, "inclusion:malformed_event", None
        row = _event_row(conn, org_id, event)
        if row is None or row["row_hash"] is None:
            return False, f"inclusion:event_missing:{event.get('event_id')}", None
        if row["row_hash"] != event.get("row_hash"):
            return False, "inclusion:row_hash_mismatch", None
        max_rowid = max(max_rowid, int(row["_rowid"]))
    chain = db.verify_chain(conn, org_id=org_id, hmac_key=hmac_key)
    org_chain = next((o for o in chain.get("orgs", [])
                      if o.get("org_id") == org_id), None)
    if org_chain is None or org_chain.get("status") != "ok":
        return False, "inclusion:chain_broken", None
    if not checkpoints:
        return False, "inclusion:anchor_missing", None
    vc = db.verify_checkpoints(conn, checkpoints, hmac_key=hmac_key)
    by_rowid = {int(c.get("through_rowid")): c for c in (checkpoints or [])}
    covering = [
        c for c in vc.get("checkpoints", [])
        if c.get("status") == "ok" and int(c.get("through_rowid", 0)) >= max_rowid
    ]
    if not covering:
        statuses = {c.get("status") for c in vc.get("checkpoints", [])}
        if statuses and statuses <= {"head_mismatch", "count_mismatch", "missing",
                                     "bad_signature", "chain_broken"}:
            return False, "inclusion:anchor_broken", None
        return False, "inclusion:anchor_missing", None
    best = max(covering, key=lambda c: int(c["through_rowid"]))
    source = by_rowid.get(int(best["through_rowid"]), {})
    anchor = {
        "checkpoint_id": source.get("id"),
        "through_rowid": int(best["through_rowid"]),
        "head_hash": source.get("head_hash"),
        "status": best["status"],
    }
    return True, "inclusion:ok", anchor


# ── #237: belief-context evidence ───────────────────────────────────────────


def _belief_context_evidence(receipt: Mapping[str, Any],
                             key_registry: Optional[Mapping[str, bytes]],
                             hmac_key: Optional[bytes]) -> dict[str, Any]:
    """Attested-tier reporting for the optional belief-context block (#237).

    The block is inside the signed bytes, so a receipt whose HMAC signature
    verifies has its belief context bound to the signer — that is attested-tier
    evidence. Absent blocks leave the section with ``present: false`` and
    existing receipts byte-unchanged.
    """
    blocks = []
    for event in receipt.get("events") or []:
        bc = event.get("belief_context") if isinstance(event, Mapping) else None
        if isinstance(bc, Mapping):
            blocks.append(bc)
    if not blocks:
        return {
            "present": False, "covered": None, "level": None,
            "reason": "belief:absent", "entries": None, "digest": None,
        }
    entries = {
        kind: sum(1 for b in blocks for e in (b.get(kind) or []) if isinstance(e, Mapping))
        for kind in ("believed", "assumed", "ignored")
    }
    if any(not validate_belief_context(b)[0] for b in blocks):
        return {
            "present": True, "covered": False, "level": None,
            "reason": "belief:malformed_block", "entries": entries,
            "digest": None,
        }
    sig_ok = False
    if isinstance(receipt.get("signature"), Mapping):
        sig_ok, _ = verify_receipt_signature(receipt, key_registry, hmac_key)
    if not sig_ok:
        return {
            "present": True, "covered": False, "level": None,
            "reason": ("belief:signature_missing" if not isinstance(receipt.get("signature"), Mapping)
                       else "belief:signature_invalid"),
            "entries": entries, "digest": None,
        }
    digest = hashlib.sha256(
        json.dumps([b.get("belief_digest") for b in blocks],
                   sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "present": True, "covered": True, "level": "attested",
        "reason": "belief:ok_attested", "entries": entries, "digest": digest,
    }


# ── composite verifier ──────────────────────────────────────────────────────


def verify_receipt_evidence(conn, org_id: str, receipt: Mapping[str, Any], *,
                            hmac_key: Optional[bytes] = None,
                            key_registry: Optional[Mapping[str, bytes]] = None,
                            checkpoints: Optional[list] = None) -> dict[str, Any]:
    """Verify a receipt at every level and report the highest achieved.

    Returns a ``verification.evidence`` block: per-level booleans and stable
    reason codes, the highest verified level (or ``None`` below structural),
    any downgrade from a claimed level, the inclusion anchor when verified,
    and whether the receipt records a Commit (executed action) — which
    REQUIRES an inclusion anchor to reach the inclusion level.
    """
    levels: dict[str, bool] = {}
    reasons: dict[str, str] = {}
    levels["structural"], reasons["structural"] = verify_structural(
        receipt, key_registry=key_registry, hmac_key=hmac_key,
    )
    if levels["structural"]:
        levels["attested"], reasons["attested"] = verify_attested(
            receipt, key_registry=key_registry, hmac_key=hmac_key,
        )
        levels["replay"], reasons["replay"] = verify_replay(conn, org_id, receipt)
        levels["inclusion"], reasons["inclusion"], anchor = verify_inclusion(
            conn, org_id, receipt, checkpoints=checkpoints, hmac_key=hmac_key,
        )
    else:
        skipped = "skipped:structural_failed"
        levels.update(attested=False, replay=False, inclusion=False)
        reasons.update(attested=skipped, replay=skipped, inclusion=skipped)
        anchor = None

    level: Optional[str] = None
    for name in EVIDENCE_LEVELS:
        if levels[name]:
            level = name
    claimed = receipt.get("claimed_evidence_level")
    if claimed is not None and claimed not in EVIDENCE_LEVELS:
        claimed = None  # structural already failed on this; keep the block tidy
    downgrades: list[dict[str, Any]] = []
    if claimed is not None:
        claimed_index = _LEVEL_INDEX[claimed]
        verified_index = _LEVEL_INDEX[level] if level is not None else -1
        if claimed_index > verified_index:
            for name in EVIDENCE_LEVELS:
                idx = _LEVEL_INDEX[name]
                if verified_index < idx <= claimed_index and not levels.get(name):
                    downgrades.append({
                        "from": name,
                        "to": level or "none",
                        "reason": reasons.get(name, "skipped"),
                    })
    events = receipt.get("events") or []
    return {
        "levels": levels,
        "level": level,
        "claimed": claimed if claimed in EVIDENCE_LEVELS else None,
        "reasons": reasons,
        "downgrades": downgrades,
        "inclusion_anchor": anchor,
        "commit_receipt": commit_receipt(events),
        "inclusion_required": commit_receipt(events),
        "belief_context": _belief_context_evidence(receipt, key_registry, hmac_key),
    }


__all__ = [
    "EVIDENCE_LEVELS", "TERMINAL_STATUSES", "RECEIPT_VERSIONS",
    "canonical_receipt", "resolve_key",
    "sign_receipt", "verify_receipt_signature",
    "attest_receipt", "verify_receipt_attestation",
    "first_terminal_status", "commit_receipt",
    "verify_structural", "verify_attested", "verify_replay", "verify_inclusion",
    "verify_receipt_evidence",
]
