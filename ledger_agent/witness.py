"""External witness countersignatures over ledger heads (#240).

Borrows 1f916-ai/protocol fail-closed witness semantics (SPEC §8 "anchor
rule" + witness-absent verdicts, verified locally 2026-08-15):

- witness keys must be pinned out-of-band — a key shipped in the same file
  as the countersignature proves self-consistency only, never witnessed;
- unsigned witness copies corroborate, never raise the verdict;
- witness refusal lines are evidence AGAINST the head;
- asked-and-empty is a distinct verdict/exit code from never-asked;
- every run prints what a passing run does NOT prove.

Ledger heads are the per-org chain heads (``db.chain_head``): the row_hash
of the newest event. A countersignature is an HMAC-SHA256 (the repo's
stdlib-only signing scheme) over the canonical head binding
{org_id, head_hash, through_rowid, prev_head_hash}. Binding the witness's
previous head makes continuity checkable: a witness that saw only an older
head cannot attest a newer one without a continuity chain of prior
countersignatures.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
from typing import Any, Mapping, Optional

HEAD_SCHEMA = "perseus-ledger-witness-head/v1"
COUNTERSIG_SCHEMA = "perseus-ledger-witness-countersignature/v1"
VERDICT_SCHEMA = "perseus-ledger-witness-verdict/v1"

VERDICTS = (
    "witnessed",
    "consistent-unwitnessed",
    "witness-unusable",
    "diverged",
    "asked-and-empty",
)
EXIT_CODES = {
    "witnessed": 0,
    "consistent-unwitnessed": 1,
    "witness-unusable": 2,
    "diverged": 3,
    "asked-and-empty": 4,
}

_DOES_NOT_PROVE = [
    "witnesses attest ledger heads, not the contents of individual events",
    "a witnessed head proves pinned witnesses saw this exact head — events "
    "withheld from witnesses before this head are still undetectable",
    "consistent-unwitnessed proves internal hash-chain coherence only; an "
    "operator can rewrite and re-sign their own chain",
    "witness keys are pinned out-of-band at verification time; keys from the "
    "artifact under test prove self-consistency only",
    "witness refusal lines are evidence against the head, not proof of tampering",
]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _is_sha256(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and set(value.lower()) <= set("0123456789abcdef"))


def build_head(*, org_id: str, head_hash: str,
               through_rowid: int, prev_head_hash: Optional[str] = None) -> dict[str, Any]:
    """Canonical head object a witness countersigns (#240)."""
    if not isinstance(org_id, str) or not org_id.strip():
        raise ValueError("org_id must be a non-empty string")
    if not _is_sha256(head_hash):
        raise ValueError("head_hash must be a 64-char SHA-256 hex digest")
    if not isinstance(through_rowid, int) or through_rowid < 0:
        raise ValueError("through_rowid must be a non-negative integer")
    if prev_head_hash is not None and not _is_sha256(prev_head_hash):
        raise ValueError("prev_head_hash must be a 64-char SHA-256 hex digest or None")
    return {
        "schema": HEAD_SCHEMA,
        "org_id": org_id,
        "head_hash": head_hash.lower(),
        "through_rowid": through_rowid,
        "prev_head_hash": prev_head_hash.lower() if prev_head_hash else None,
    }


def countersign_head(head: Mapping[str, Any], *, witness_id: str,
                     key: bytes, prev_head_hash: Optional[str] = None) -> dict[str, Any]:
    """Countersign a head with a witness HMAC key (#240).

    The signature covers {witness_id, org_id, head_hash, through_rowid,
    prev_head_hash} — the witness's previous head is bound into the new
    countersignature so continuity is checkable offline.
    """
    org_id = head.get("org_id")
    head_hash = head.get("head_hash")
    through_rowid = head.get("through_rowid")
    if not isinstance(org_id, str) or not _is_sha256(head_hash) \
            or not isinstance(through_rowid, int):
        raise ValueError("head must carry org_id, head_hash, through_rowid")
    head_hash = str(head_hash).lower()
    if prev_head_hash is not None and not _is_sha256(prev_head_hash):
        raise ValueError("prev_head_hash must be a 64-char SHA-256 hex digest or None")
    if not isinstance(witness_id, str) or not witness_id.strip():
        raise ValueError("witness_id must be a non-empty string")
    payload = {
        "witness_id": witness_id,
        "org_id": org_id,
        "head_hash": head_hash,
        "through_rowid": through_rowid,
        "prev_head_hash": prev_head_hash.lower() if prev_head_hash else None,
    }
    sig = _hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
    return {
        "schema": COUNTERSIG_SCHEMA,
        "witness_id": witness_id,
        "org_id": org_id,
        "head_hash": head_hash,
        "through_rowid": through_rowid,
        "prev_head_hash": prev_head_hash.lower() if prev_head_hash else None,
        "algo": "hmac-sha256",
        "sig": sig,
    }


def verify_countersignature(copy: Mapping[str, Any], key: bytes) -> tuple[bool, str]:
    """Check one countersignature against an already-pinned key."""
    if not isinstance(copy, Mapping):
        return False, "bad_copy_shape"
    sig = copy.get("sig")
    if not (isinstance(sig, str) and _is_sha256(sig)):
        return False, "bad_signature_shape"
    payload = {
        "witness_id": copy.get("witness_id"),
        "org_id": copy.get("org_id"),
        "head_hash": copy.get("head_hash"),
        "through_rowid": copy.get("through_rowid"),
        "prev_head_hash": copy.get("prev_head_hash"),
    }
    expected = _hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(expected, sig.lower()):
        return False, "bad_signature"
    return True, "ok"


def _prior_heads(prior_copies: Optional[list], pinned_keys: Mapping[str, bytes],
                 witness_id: str) -> set:
    """Head hashes this witness previously countersigned (verified)."""
    heads: set = set()
    for prior in prior_copies or []:
        if not isinstance(prior, Mapping) or prior.get("witness_id") != witness_id:
            continue
        key = pinned_keys.get(witness_id)
        if key is None:
            continue
        ok, _ = verify_countersignature(prior, key)
        if ok and _is_sha256(prior.get("head_hash")):
            heads.add(prior["head_hash"].lower())
    return heads


def verify_witnesses(*, head: Mapping[str, Any], copies: Optional[list] = None,
                     pinned_keys: Optional[Mapping[str, bytes]] = None,
                     asked: bool = False,
                     prior_copies: Optional[list] = None,
                     chain_ok: bool = True) -> dict[str, Any]:
    """Grade witness evidence for one ledger head (#240).

    Fail-closed grading, verdicts per 1f916 SPEC §8 semantics:

    - ``witnessed`` — >=1 countersignature from a PINNED witness covers this
      exact head with continuity;
    - ``consistent-unwitnessed`` — no pinned attestation (never asked, or
      only corroborating/unsigned copies); the chain checks internally;
    - ``witness-unusable`` — copies were supplied but carried no line the
      run could apply (unknown witness, bad signature, unsigned);
    - ``diverged`` — chain broken, a pinned witness refused, or a witness
      attested a conflicting head at the same position;
    - ``asked-and-empty`` — witnesses were asked and nothing came back.
      Distinct from never-asked by verdict AND exit code.

    ``prior_copies`` supplies the continuity chain (the witness's previous
    countersignatures, verified under the same pinned keys). A copy attesting
    this head with a non-null ``prev_head_hash`` attests only when the pinned
    witness previously countersigned exactly that previous head.
    """
    pinned = dict(pinned_keys or {})
    copies = [c for c in (copies or []) if isinstance(c, Mapping)]
    org_id = head.get("org_id")
    head_hash = (head.get("head_hash") or "").lower()
    through = head.get("through_rowid")

    does_not_prove = list(_DOES_NOT_PROVE)
    witness_results: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []

    def verdict(verdict: str, reason: str) -> dict[str, Any]:
        return {
            "schema": VERDICT_SCHEMA,
            "verdict": verdict,
            "exit_code": EXIT_CODES[verdict],
            "reason": reason,
            "head": {"org_id": org_id, "head_hash": head_hash,
                     "through_rowid": through},
            "witnesses": witness_results,
            "refusals": refusals,
            "does_not_prove": does_not_prove,
        }

    if not chain_ok:
        # A fabricated head never earns witnessed — the chain is the anchor.
        return verdict("diverged", "chain_broken")

    if not _is_sha256(head_hash):
        return verdict("diverged", "head_malformed")

    applicable = 0
    attested = 0
    conflicting = []
    refusal_hits = []
    prior_heads_cache: dict[str, set] = {}

    for copy in copies:
        witness_id = copy.get("witness_id")
        if not isinstance(witness_id, str) or not witness_id.strip():
            witness_results.append({"witness_id": None, "status": "unusable",
                                    "detail": "missing_witness_id"})
            continue
        if copy.get("refusal"):
            detail = copy.get("reason") or copy.get("refusal")
            refusals.append({"witness_id": witness_id,
                             "reason": detail if isinstance(detail, str) else "refused"})
            if witness_id in pinned:
                refusal_hits.append(witness_id)
                witness_results.append({"witness_id": witness_id, "status": "refusal",
                                        "detail": "evidence_against_head"})
            else:
                witness_results.append({"witness_id": witness_id,
                                        "status": "refusal_unpinned",
                                        "detail": "refusal from an unpinned witness — "
                                                  "recorded, not graded"})
            continue
        key = pinned.get(witness_id)
        if key is None:
            witness_results.append({"witness_id": witness_id,
                                    "status": "unknown_witness",
                                    "detail": "no pinned key — corroboration only"})
            continue
        if not isinstance(copy.get("sig"), str):
            # Unsigned witness copy: corroboration only, never the top
            # verdict (#240). It is applicable when the pinned witness echoed
            # this exact head; otherwise it is a line the run cannot apply.
            if (copy.get("head_hash") or "").lower() == head_hash:
                applicable += 1
                witness_results.append({
                    "witness_id": witness_id,
                    "status": "unsigned_corroboration_only",
                    "detail": "echoes this head without a countersignature"})
                does_not_prove.append(
                    f"copy from {witness_id} is unsigned — corroboration only, "
                    "never the top verdict")
                continue
            witness_results.append({"witness_id": witness_id, "status": "unusable",
                                    "detail": "unsigned_copy"})
            continue
        ok, reason = verify_countersignature(copy, key)
        if not ok:
            witness_results.append({"witness_id": witness_id, "status": "unusable",
                                    "detail": reason})
            continue
        applicable += 1
        copy_head = (copy.get("head_hash") or "").lower()
        if copy_head != head_hash:
            if copy.get("through_rowid") == through:
                conflicting.append(witness_id)
                witness_results.append({"witness_id": witness_id,
                                        "status": "conflicting_head",
                                        "detail": "attests a different head_hash at the "
                                                  "same through_rowid"})
            else:
                witness_results.append({"witness_id": witness_id,
                                        "status": "stale_head",
                                        "detail": "countersigns an older head only"})
            continue
        prev = copy.get("prev_head_hash")
        if prev:
            if witness_id not in prior_heads_cache:
                prior_heads_cache[witness_id] = _prior_heads(
                    prior_copies, pinned, witness_id)
            if prev.lower() not in prior_heads_cache[witness_id]:
                witness_results.append({
                    "witness_id": witness_id, "status": "continuity_unproven",
                    "detail": "witness saw only an older head — cannot attest a "
                              "newer one without a continuity proof"})
                does_not_prove.append(
                    f"countersignature from {witness_id} lacks a continuity "
                    "proof; a witness that saw only an older head cannot attest "
                    "a newer one")
                continue
        attested += 1
        witness_results.append({"witness_id": witness_id, "status": "attests",
                                "detail": "pinned countersignature covers this head"})

    if refusal_hits:
        # Refusal lines are evidence AGAINST the head.
        return verdict("diverged",
                       "witness_refusal:" + ",".join(sorted(refusal_hits)))
    if conflicting:
        return verdict("diverged", "conflicting_head:" + ",".join(sorted(conflicting)))
    if attested:
        for wid in sorted(pinned):
            if wid not in {c.get("witness_id") for c in copies}:
                does_not_prove.append(f"pinned witness {wid} has no countersignature "
                                      "in this file")
        return verdict("witnessed", f"{attested} pinned witness(es) attest this head")
    if not copies:
        if asked:
            return verdict("asked-and-empty",
                           "witnesses were asked and nothing came back")
        return verdict("consistent-unwitnessed",
                       "no witness copies presented (never asked)")
    if applicable:
        return verdict("consistent-unwitnessed",
                       "witness copies supplied but none attests this head")
    return verdict("witness-unusable",
                   "witness input supplied but carried no line the run could apply")


__all__ = [
    "HEAD_SCHEMA", "COUNTERSIG_SCHEMA", "VERDICT_SCHEMA",
    "VERDICTS", "EXIT_CODES",
    "build_head", "countersign_head", "verify_countersignature",
    "verify_witnesses",
]
