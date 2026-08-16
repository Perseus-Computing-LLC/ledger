"""A lightweight CVA authorization relation and replay gateway.

This module implements the non-zero-knowledge Ledger adaptation of the formal
model in arXiv:2607.21325.  A statement is public, hash-bound data; request and
context payloads are supplied to the verifier so the four CVA conjuncts can be
checked without pretending that a Python predicate is a ZK proof.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from typing import Any, Optional

from .keys import normalize_key_registry

CVA_STATEMENT_SCHEMA = "perseus-ledger-cva-statement/v1"


def _canonical(value: Any) -> bytes:
    """Serialize a JSON value using Ledger's canonical hash encoding."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value.lower())
    )


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _timestamp(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timestamp_ms must be a finite number")
    if not math.isfinite(value):
        raise ValueError("timestamp_ms must be a finite number")
    return value


def build_cva_statement(*, agent_id: str, request_hash: str,
                        context_hash: str, policy_id: str, nonce: str,
                        timestamp_ms: int | float) -> dict[str, Any]:
    """Build the public CVA statement ``x`` from paper Equation (16).

    The returned ``statement_hash`` commits to every field except itself.  The
    hash is deliberately over the same canonical JSON representation used by
    the rest of Ledger; it is not a proof of a private witness.
    """
    body: dict[str, Any] = {
        "schema": CVA_STATEMENT_SCHEMA,
        "agent_id": _require_text(agent_id, "agent_id"),
        "request_hash": request_hash.lower() if _is_sha256(request_hash)
        else (_raise_hash("request_hash")),
        "context_hash": context_hash.lower() if _is_sha256(context_hash)
        else (_raise_hash("context_hash")),
        "policy_id": _require_text(policy_id, "policy_id"),
        "nonce": _require_text(nonce, "nonce"),
        "timestamp_ms": _timestamp(timestamp_ms),
    }
    body["statement_hash"] = _sha(body)
    return body


def _raise_hash(field: str) -> str:
    raise ValueError(f"{field} must be a 64-character SHA-256 hex digest")


def _policy_parts(policy: Any) -> tuple[Optional[str], Optional[Callable[..., Any]]]:
    """Return an optional policy identifier and its deterministic predicate.

    The issue-level API is a callable.  A callable may advertise its bound
    policy identifier through ``policy_id``; this gives the non-ZK adapter a
    way to detect a cross-policy presentation.  A mapping with ``predicate``
    and ``policy_id`` is accepted as a convenience for callers that carry
    policy metadata separately.  A plain callable remains fully supported.
    """
    if isinstance(policy, Mapping):
        policy_id = policy.get("policy_id")
        predicate = policy.get("predicate", policy.get("evaluate"))
        return (policy_id if isinstance(policy_id, str) else None,
                predicate if callable(predicate) else None)
    policy_id = getattr(policy, "policy_id", None)
    return (policy_id if isinstance(policy_id, str) else None,
            policy if callable(policy) else None)


def _statement_hash_valid(statement: Mapping[str, Any]) -> bool:
    supplied = statement.get("statement_hash")
    if not _is_sha256(supplied):
        return False
    body = {key: value for key, value in statement.items()
            if key != "statement_hash"}
    try:
        return supplied.lower() == _sha(body)
    except (TypeError, ValueError):
        return False


def _safe_payload_hash(payload: Any) -> Optional[str]:
    try:
        return _sha(payload)
    except (TypeError, ValueError, OverflowError):
        return None


def cva_relation_holds(statement: Mapping[str, Any], *,
                       principal_key_id: str,
                       key_registry: Optional[Mapping[str, Any]],
                       request_payload: Any,
                       context_payload: Any,
                       attrs: Any,
                       policy: Any) -> tuple[bool, list[str]]:
    """Evaluate the four CVA conjuncts from Equations (22)--(24).

    This is intentionally an honest Ledger adaptation: the registry and
    payloads are verifier inputs and ``policy`` is a caller-supplied
    deterministic predicate, not a zero-knowledge proof verifier.  Every
    violated conjunct is reported in formal order rather than short-circuiting.
    """
    errors: list[str] = []
    if not isinstance(statement, Mapping):
        return False, ["statement"]

    if statement.get("schema") != CVA_STATEMENT_SCHEMA:
        errors.append("statement_schema")
    if not _statement_hash_valid(statement):
        errors.append("statement_hash")

    # BindPrincipal (Equation 23).  ``normalize_key_registry`` retains the
    # Ledger custody shape and the optional agent/revocation metadata.
    principal_ok = False
    try:
        entry = normalize_key_registry(key_registry).get(principal_key_id)
        if entry is not None:
            binding = entry.get("agent_id", entry.get("agent_binding"))
            revoked = bool(entry.get("revoked", False))
            if entry.get("revoked_at") is not None:
                revoked = True
            if entry.get("status") in {"revoked", "disabled", "inactive"}:
                revoked = True
            principal_ok = (
                isinstance(statement.get("agent_id"), str)
                and binding == statement.get("agent_id")
                and not revoked
            )
    except (TypeError, ValueError):
        principal_ok = False
    if not principal_ok:
        errors.append("bind_principal")

    # BindRequest (Equation 19) and BindContext (Equation 20).
    request_digest = _safe_payload_hash(request_payload)
    if not (_is_sha256(statement.get("request_hash"))
            and request_digest == statement.get("request_hash", "").lower()):
        errors.append("bind_request")

    context_digest = _safe_payload_hash(context_payload)
    if not (_is_sha256(statement.get("context_hash"))
            and context_digest == statement.get("context_hash", "").lower()):
        errors.append("bind_context")

    # SatisfyPolicy (Equation 21).  A bound policy identifier is checked when
    # the callable exposes one; a plain predicate is still valid and must be
    # selected by the caller according to statement["policy_id"].
    policy_id, predicate = _policy_parts(policy)
    policy_ok = predicate is not None
    if policy_id is not None and policy_id != statement.get("policy_id"):
        policy_ok = False
    if predicate is not None:
        try:
            policy_ok = policy_ok and (predicate(attrs, request_payload, context_payload) is True)
        except Exception:
            # A policy failure is an authorization failure, not an exception
            # escape that could accidentally admit the request.
            policy_ok = False
    if not policy_ok:
        errors.append("satisfy_policy")

    return not errors, errors


def is_fresh(nonce: str, timestamp_ms: int | float,
             consumed_nonces: set[str], t_min: int | float,
             t_max: int | float) -> bool:
    """Return the inclusive freshness predicate from Equations (26), (38)--(40)."""
    try:
        return nonce not in consumed_nonces and t_min <= timestamp_ms <= t_max
    except (TypeError, ValueError):
        return False


class CvaGateway:
    """Stateful freshness gateway around the stateless CVA relation.

    ``consumed_nonces`` is the trusted replay-control state.  A nonce is added
    only after the relation and timestamp checks have both accepted, matching
    the state transition in Equation (37).
    """

    def __init__(self, consumed_nonces: Optional[set[str]] = None, *,
                 t_min: int | float | None = None,
                 t_max: int | float | None = None) -> None:
        self.consumed_nonces = consumed_nonces if consumed_nonces is not None else set()
        self.t_min = t_min
        self.t_max = t_max

    def consume(self, statement: Mapping[str, Any], *,
                consumed: Optional[set[str]] = None) -> None:
        """Commit an accepted statement's nonce to replay state."""
        nonce = statement.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise ValueError("statement nonce must be a non-empty string")
        target = self.consumed_nonces if consumed is None else consumed
        target.add(nonce)

    def accept(self, statement: Mapping[str, Any],
               witness: Optional[Mapping[str, Any]] = None, *,
               principal_key_id: Optional[str] = None,
               key_registry: Optional[Mapping[str, Any]] = None,
               request_payload: Any = None,
               context_payload: Any = None,
               attrs: Any = None,
               policy: Any = None,
               consumed: Optional[set[str]] = None,
               t_min: int | float | None = None,
               t_max: int | float | None = None) -> dict[str, Any]:
        """Accept a CVA statement or return a precise rejection reason.

        ``witness`` is an optional convenience mapping for callers that carry
        all relation inputs together.  Explicit keyword arguments take
        precedence over values in that mapping.
        """
        supplied = dict(witness or {})
        values = {
            "principal_key_id": principal_key_id,
            "key_registry": key_registry,
            "request_payload": request_payload,
            "context_payload": context_payload,
            "attrs": attrs,
            "policy": policy,
        }
        for name, value in list(values.items()):
            if value is None and name in supplied:
                values[name] = supplied[name]

        target = self.consumed_nonces if consumed is None else consumed
        nonce = statement.get("nonce") if isinstance(statement, Mapping) else None
        if isinstance(nonce, str) and nonce in target:
            return {"accepted": False, "reason": "replay"}

        lower = self.t_min if t_min is None else t_min
        upper = self.t_max if t_max is None else t_max
        if lower is None:
            lower = float("-inf")
        if upper is None:
            upper = float("inf")
        timestamp = statement.get("timestamp_ms") if isinstance(statement, Mapping) else None
        try:
            if timestamp < lower:
                return {"accepted": False, "reason": "stale_timestamp"}
            if timestamp > upper:
                return {"accepted": False, "reason": "future_timestamp"}
        except (TypeError, ValueError):
            # Let the relation report malformed statements consistently.
            pass

        relation_ok, relation_errors = cva_relation_holds(
            statement,
            principal_key_id=values["principal_key_id"],
            key_registry=values["key_registry"],
            request_payload=values["request_payload"],
            context_payload=values["context_payload"],
            attrs=values["attrs"],
            policy=values["policy"],
        )
        if not relation_ok:
            return {
                "accepted": False,
                "reason": "relation_not_satisfied",
                "relation_errors": relation_errors,
            }

        if not is_fresh(nonce, timestamp, target, lower, upper):
            # The explicit boundary reasons above handle normal timestamps;
            # this branch covers malformed/non-string freshness inputs.
            if isinstance(nonce, str) and nonce in target:
                reason = "replay"
            elif timestamp < lower:
                reason = "stale_timestamp"
            elif timestamp > upper:
                reason = "future_timestamp"
            else:
                reason = "relation_not_satisfied"
            return {"accepted": False, "reason": reason}

        self.consume(statement, consumed=target)
        return {"accepted": True, "reason": "accepted"}


PROPERTIES: list[dict[str, str]] = [
    {
        "name": "authorization_soundness",
        "paper_eq": "27-28",
        "attack_class": "proof forgery or invalid-witness acceptance",
        "ledger_mechanism": "hash-bound statements plus fail-closed evaluation of all four CVA conjuncts",
    },
    {
        "name": "principal_binding",
        "paper_eq": "29",
        "attack_class": "cross-principal proof transfer",
        "ledger_mechanism": "normalized key-registry agent binding with revocation awareness",
    },
    {
        "name": "request_binding",
        "paper_eq": "30",
        "attack_class": "cross-request proof transfer",
        "ledger_mechanism": "SHA-256 canonical request commitment in the CVA statement and AAR prebind",
    },
    {
        "name": "policy_binding",
        "paper_eq": "31",
        "attack_class": "cross-policy proof transfer",
        "ledger_mechanism": "policy identifier committed in statement; supplied predicate must satisfy that binding",
    },
    {
        "name": "context_binding",
        "paper_eq": "32-36",
        "attack_class": "context substitution at authorization time",
        "ledger_mechanism": "SHA-256 canonical context commitment and selected-context receipt hashes",
    },
    {
        "name": "replay_resistance",
        "paper_eq": "37-40",
        "attack_class": "nonce reuse and deferred presentation outside the validity window",
        "ledger_mechanism": "trusted gateway nonce set with inclusive timestamp window",
    },
]

# Descriptive alias for callers that prefer an explicit constant name.
CVA_PROPERTIES = PROPERTIES

__all__ = [
    "CVA_STATEMENT_SCHEMA",
    "PROPERTIES",
    "CVA_PROPERTIES",
    "build_cva_statement",
    "cva_relation_holds",
    "is_fresh",
    "CvaGateway",
]
