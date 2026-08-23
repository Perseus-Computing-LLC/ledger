"""Session/task-lineage action-composition admission for Perseus Ledger.

The module is deliberately small and runtime-neutral.  It gives a Ledger caller a
trusted taxonomy lookup, a versioned policy, and a durable serialized admission
transition.  The taxonomy and policy are configuration owned by the application;
request data can select an entry but cannot replace its impact, classification, or
budget cost.  Only hash-only action projections are persisted.

This is a composition contract, not a semantic-intent detector.  Vault/AAR remains
the authority for manifests and approvals; Ledger binds the resulting authority
reference, policy/profile digests, lineage state, and verdict to its evidence
surfaces.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import posixpath
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from . import db

ACTION_PROFILE_SCHEMA = "perseus-ledger-action-profile/v1"
ACTION_TAXONOMY_SCHEMA = "perseus-ledger-action-taxonomy/v1"
COMPOSITION_POLICY_SCHEMA = "perseus-ledger-composition-policy/v1"
COMPOSITION_STATE_SCHEMA = "perseus-ledger-composition-state/v1"
COMPOSITION_VERDICT_SCHEMA = "perseus-ledger-composition-verdict/v1"
COMPOSITION_BINDING_SCHEMA = "perseus-ledger-composition-binding/v1"
AUTHORIZATION_SCHEMA = "perseus-ledger-composition-authorization/v1"

CLASSIFICATIONS = frozenset({"public", "internal", "confidential", "restricted"})
IMPACTS = frozenset({"low", "medium", "high", "critical"})
OUTCOMES = frozenset({"allow", "deny", "hold", "review", "abstain"})
POLICY_SCOPES = frozenset({"task", "session"})
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_IDENT = re.compile(r"^[a-z][a-z0-9_.:/-]{0,127}$")
_OPAQUE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_CREDENTIAL_REF = re.compile(
    r"(?:api[_-]?key|secret|password|passwd|token|credential|authorization|bearer|private[_-]?key)",
    re.IGNORECASE,
)
_COMPOSITION_BINDING_FIELDS = frozenset({
    "schema", "policy_version", "policy_hash", "taxonomy_version", "taxonomy_hash",
    "state_hash", "profile_digest", "action_digest", "action_id", "task_lineage_id",
    "authority_action_id", "authority_ref", "context_head_digest", "workspace_scope",
    "verdict", "composition_hash",
})
_SAFE_TEXT_MAX = 512


class CompositionError(ValueError):
    """A fail-closed composition or taxonomy contract error."""

    def __init__(self, code: str, message: Optional[str] = None):
        self.code = code
        super().__init__(message or code)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise CompositionError("malformed_value", "value is not canonical JSON") from exc


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def _text(value: Any, field: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > _SAFE_TEXT_MAX:
        raise CompositionError("malformed_" + field, f"{field} must be a bounded non-empty string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CompositionError("malformed_" + field, f"{field} contains a control character")
    if identifier and not _IDENT.fullmatch(value):
        raise CompositionError("malformed_" + field, f"{field} is not canonical")
    return value


def _opaque_ref(value: Any, field: str) -> str:
    """Accept only bounded printable references, never credential-like text."""
    if (
        not isinstance(value, str)
        or not _OPAQUE_REF.fullmatch(value)
        or _CREDENTIAL_REF.search(value)
    ):
        raise CompositionError("malformed_" + field, f"{field} must be an opaque reference")
    return value


def _public_ref(value: Any) -> str:
    """Return a safe identity for a review projection or an unbound sentinel."""
    if isinstance(value, str) and _OPAQUE_REF.fullmatch(value) and not _CREDENTIAL_REF.search(value):
        return value
    return "unbound"


def _finite_nonnegative(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CompositionError("invalid_" + field, f"{field} must be a finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise CompositionError("invalid_" + field, f"{field} must be a finite non-negative number")
    return round(number, 6)


def _units(value: float) -> int:
    return int(round(value * 1_000_000))


def _validate_json_value(value: Any, *, field: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CompositionError("malformed_" + field, f"{field} contains a non-finite number")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise CompositionError("malformed_" + field, f"{field} keys must be strings")
            _validate_json_value(child, field=field)
        return
    if isinstance(value, list):
        for child in value:
            _validate_json_value(child, field=field)
        return
    raise CompositionError("malformed_" + field, f"{field} is not canonical JSON")


def _normalize_resource(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CompositionError("invalid_resource", "resource argument must be a canonical string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CompositionError("invalid_resource", "resource contains a control character")
    if ".." in value.split("/"):
        raise CompositionError("invalid_resource", "resource traversal is not permitted")
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise CompositionError("invalid_resource", "resource URL is not supported")
        if parsed.username is not None or parsed.password is not None:
            raise CompositionError("invalid_resource", "resource URL userinfo is not permitted")
        path = posixpath.normpath(parsed.path or "/")
        if path == "." or path.startswith("../"):
            path = "/"
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path,
                           parsed.query, parsed.fragment))
    return value


def _names(values: Sequence[str], field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise CompositionError("malformed_" + field, f"{field} must be a sequence")
    try:
        result = tuple(_text(item, field, identifier=True) for item in values)
    except TypeError as exc:
        raise CompositionError("malformed_" + field, f"{field} must be a sequence") from exc
    if len(set(result)) != len(result):
        raise CompositionError("duplicate_" + field, f"{field} contains duplicates")
    return result


@dataclass(frozen=True, init=False)
class ActionProfile:
    """Trusted metadata for one exact tool endpoint.

    ``budget_cost`` is owned by the registry, never by an admission request.
    ``allowed_arguments``/``required_arguments`` are a deliberately bounded
    validation contract; raw argument values are hashed but never retained in a
    resolved action projection.
    """

    schema = ACTION_PROFILE_SCHEMA

    tool_endpoint: str
    action_class: str
    resource: str
    data_classification: str
    impact: str
    budget_cost: float
    allowed_arguments: tuple[str, ...]
    required_arguments: tuple[str, ...]
    resource_argument: Optional[str]

    def __init__(self, tool_endpoint: str, action_class: str, resource: str,
                 data_classification: str, impact: str, budget_cost: Any,
                 allowed_arguments: Sequence[str] = (),
                 required_arguments: Sequence[str] = (),
                 resource_argument: Optional[str] = None):
        object.__setattr__(self, "tool_endpoint", _text(tool_endpoint, "tool_endpoint", identifier=True))
        object.__setattr__(self, "action_class", _text(action_class, "action_class", identifier=True))
        object.__setattr__(self, "resource", _text(resource, "resource", identifier=True))
        if data_classification not in CLASSIFICATIONS:
            raise CompositionError("invalid_classification", "data_classification is not trusted")
        if impact not in IMPACTS:
            raise CompositionError("invalid_impact", "impact is not trusted")
        object.__setattr__(self, "data_classification", data_classification)
        object.__setattr__(self, "impact", impact)
        object.__setattr__(self, "budget_cost", _finite_nonnegative(budget_cost, "budget_cost"))
        allowed = _names(allowed_arguments, "allowed_arguments")
        required = _names(required_arguments, "required_arguments")
        if not set(required).issubset(allowed):
            raise CompositionError("required_argument_not_allowed",
                                   "required arguments must be allowed")
        if resource_argument is not None:
            resource_argument = _text(resource_argument, "resource_argument", identifier=True)
            if resource_argument not in allowed:
                raise CompositionError("resource_argument_not_allowed",
                                       "resource_argument must be allowed")
        object.__setattr__(self, "allowed_arguments", allowed)
        object.__setattr__(self, "required_arguments", required)
        object.__setattr__(self, "resource_argument", resource_argument)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, tool_endpoint: Optional[str] = None) -> "ActionProfile":
        if not isinstance(value, Mapping):
            raise CompositionError("malformed_profile", "profile must be an object")
        endpoint = tool_endpoint or value.get("tool_endpoint", value.get("tool"))
        classification = value.get("data_classification", value.get("classification"))
        budget = value.get("budget_cost", value.get("cost"))
        if endpoint is None or classification is None or budget is None:
            raise CompositionError("incomplete_profile", "profile is missing trusted fields")
        return cls(
            endpoint, value.get("action_class"), value.get("resource"),
            classification, value.get("impact"), budget,
            value.get("allowed_arguments", ()), value.get("required_arguments", ()),
            value.get("resource_argument"),
        )

    @property
    def tool(self) -> str:
        return self.tool_endpoint

    @property
    def classification(self) -> str:
        return self.data_classification

    @property
    def cost(self) -> float:
        return self.budget_cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ACTION_PROFILE_SCHEMA,
            "tool_endpoint": self.tool_endpoint,
            "action_class": self.action_class,
            "resource": self.resource,
            "data_classification": self.data_classification,
            "impact": self.impact,
            "budget_cost": self.budget_cost,
            "allowed_arguments": list(self.allowed_arguments),
            "required_arguments": list(self.required_arguments),
            "resource_argument": self.resource_argument,
        }


@dataclass(frozen=True)
class ResolvedAction:
    """A safe, hash-only projection of a validated tool invocation."""

    tool_endpoint: str
    action_class: str
    resource: str
    data_classification: str
    impact: str
    budget_cost: float
    arguments_hash: str
    profile_digest: str
    action_digest: str
    taxonomy_version: str

    @property
    def profile_hash(self) -> str:
        return self.profile_digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "perseus-ledger-resolved-action/v1",
            "tool_endpoint": self.tool_endpoint,
            "action_class": self.action_class,
            "resource_hash": _sha(self.resource),
            "data_classification": self.data_classification,
            "impact": self.impact,
            "budget_cost": self.budget_cost,
            "arguments_hash": self.arguments_hash,
            "profile_digest": self.profile_digest,
            "action_digest": self.action_digest,
            "taxonomy_version": self.taxonomy_version,
        }


class TrustedActionRegistry:
    """Immutable-by-convention trusted endpoint-to-profile taxonomy.

    Endpoint aliases are not silently canonicalized.  A caller using an alias
    receives an explicit ``aliased_tool``/``ambiguous_tool`` review outcome and
    must retry with the exact registered endpoint.
    """

    schema = ACTION_TAXONOMY_SCHEMA

    def __init__(self, profiles: Any = (), *, version: str = "taxonomy/v1",
                 aliases: Optional[Mapping[str, Any]] = None):
        self.version = _text(version, "taxonomy_version")
        entries: dict[str, ActionProfile] = {}
        if isinstance(profiles, Mapping):
            iterable = [ActionProfile.from_mapping(profile, tool_endpoint=endpoint)
                        for endpoint, profile in profiles.items()]
        else:
            iterable = list(profiles)
        for item in iterable:
            profile = item if isinstance(item, ActionProfile) else ActionProfile.from_mapping(item)
            if profile.tool_endpoint in entries:
                raise CompositionError("duplicate_tool", "taxonomy contains a duplicate endpoint")
            entries[profile.tool_endpoint] = profile
        self._profiles = dict(entries)
        self._aliases: dict[str, tuple[str, ...]] = {}
        for alias, targets in (aliases or {}).items():
            alias = _text(alias, "alias", identifier=True)
            if isinstance(targets, str):
                targets = (targets,)
            targets = tuple(targets)
            if not targets:
                raise CompositionError("ambiguous_tool", "alias has no target")
            self._aliases[alias] = tuple(targets)

    @property
    def profiles(self) -> Mapping[str, ActionProfile]:
        return dict(self._profiles)

    @property
    def taxonomy_hash(self) -> str:
        return _sha(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ACTION_TAXONOMY_SCHEMA,
            "version": self.version,
            "profiles": [self._profiles[key].to_dict() for key in sorted(self._profiles)],
        }

    def resolve(self, tool_endpoint: str, arguments: Optional[Mapping[str, Any]] = None) -> ResolvedAction:
        if not isinstance(tool_endpoint, str) or not tool_endpoint:
            raise CompositionError("malformed_tool", "tool endpoint is required")
        if tool_endpoint in self._aliases:
            targets = self._aliases[tool_endpoint]
            if len(targets) != 1:
                raise CompositionError("ambiguous_tool", "tool alias resolves ambiguously")
            raise CompositionError("aliased_tool", "exact canonical tool endpoint is required")
        profile = self._profiles.get(tool_endpoint)
        if profile is None:
            raise CompositionError("unknown_tool", "tool endpoint is not in the trusted taxonomy")
        if tool_endpoint != tool_endpoint.strip() or not _IDENT.fullmatch(tool_endpoint):
            raise CompositionError("malformed_tool", "tool endpoint is not canonical")
        if not isinstance(arguments, Mapping):
            raise CompositionError("malformed_arguments", "arguments must be an object")
        _validate_json_value(dict(arguments), field="arguments")
        keys = set(arguments)
        unknown = keys - set(profile.allowed_arguments)
        if unknown:
            raise CompositionError("unknown_argument", "argument is not allowed by the trusted profile")
        missing = set(profile.required_arguments) - keys
        if missing:
            raise CompositionError("missing_argument", "required argument is missing")
        if any(not isinstance(key, str) for key in keys):
            raise CompositionError("malformed_arguments", "argument keys must be strings")
        arguments_hash = _sha(dict(arguments))
        resource = profile.resource
        if profile.resource_argument is not None and profile.resource_argument in arguments:
            resource = f"{resource}:{_normalize_resource(arguments[profile.resource_argument])}"
        profile_digest = _sha({"taxonomy_version": self.version, "profile": profile.to_dict()})
        action_digest = _sha({
            "taxonomy_version": self.version,
            "profile_digest": profile_digest,
            "tool_endpoint": profile.tool_endpoint,
            "action_class": profile.action_class,
            "resource": resource,
            "data_classification": profile.data_classification,
            "impact": profile.impact,
            "budget_cost": profile.budget_cost,
            "arguments_hash": arguments_hash,
        })
        return ResolvedAction(
            tool_endpoint=profile.tool_endpoint,
            action_class=profile.action_class,
            resource=resource,
            data_classification=profile.data_classification,
            impact=profile.impact,
            budget_cost=profile.budget_cost,
            arguments_hash=arguments_hash,
            profile_digest=profile_digest,
            action_digest=action_digest,
            taxonomy_version=self.version,
        )


ActionTaxonomy = TrustedActionRegistry
TrustedActionTaxonomy = TrustedActionRegistry


@dataclass(frozen=True, init=False)
class CompositionPolicy:
    """Versioned unordered-pair and ordered-sequence restrictions."""

    schema = COMPOSITION_POLICY_SCHEMA

    version: str
    prohibited_pairs: tuple[tuple[str, str], ...]
    prohibited_sequences: tuple[tuple[str, ...], ...]
    budget_limit: Optional[float]
    scope: str

    def __init__(self, version: str, prohibited_pairs: Sequence[Sequence[str]] = (),
                 prohibited_sequences: Sequence[Sequence[str]] = (),
                 budget_limit: Any = None, scope: str = "task",
                 policy_scope: Optional[str] = None):
        if policy_scope is not None:
            scope = policy_scope
        object.__setattr__(self, "version", _text(version, "policy_version"))
        if scope not in POLICY_SCOPES:
            raise CompositionError("invalid_policy_scope", "scope must be task or session")
        object.__setattr__(self, "scope", scope)
        pairs: list[tuple[str, str]] = []
        for pair in prohibited_pairs:
            if isinstance(pair, (str, bytes)) or len(pair) != 2:
                raise CompositionError("invalid_pair", "prohibited pairs must contain two classes")
            values = tuple(_text(item, "action_class", identifier=True) for item in pair)
            pairs.append(tuple(sorted(values)))
        if len(set(pairs)) != len(pairs):
            raise CompositionError("duplicate_pair", "prohibited pairs contain duplicates")
        sequences: list[tuple[str, ...]] = []
        for sequence in prohibited_sequences:
            if isinstance(sequence, (str, bytes)) or len(sequence) < 2:
                raise CompositionError("invalid_sequence", "prohibited sequences need at least two classes")
            values = tuple(_text(item, "action_class", identifier=True) for item in sequence)
            sequences.append(values)
        if len(set(sequences)) != len(sequences):
            raise CompositionError("duplicate_sequence", "prohibited sequences contain duplicates")
        object.__setattr__(self, "prohibited_pairs", tuple(sorted(pairs)))
        object.__setattr__(self, "prohibited_sequences", tuple(sorted(sequences)))
        if budget_limit is None:
            object.__setattr__(self, "budget_limit", None)
        else:
            object.__setattr__(self, "budget_limit", _finite_nonnegative(budget_limit, "budget_limit"))

    @property
    def policy_scope(self) -> str:
        return self.scope

    @property
    def policy_hash(self) -> str:
        return _sha(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": COMPOSITION_POLICY_SCHEMA,
            "version": self.version,
            "scope": self.scope,
            "prohibited_pairs": [list(pair) for pair in self.prohibited_pairs],
            "prohibited_sequences": [list(sequence) for sequence in self.prohibited_sequences],
            "budget_limit": self.budget_limit,
        }


def _safe_digest_field(value: Any, field: str) -> str:
    if not _is_hash(value):
        raise CompositionError("invalid_" + field, f"{field} must be a SHA-256 digest")
    return value.lower()


def _binding_body(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": COMPOSITION_BINDING_SCHEMA,
        "policy_version": value["policy_version"],
        "policy_hash": value["policy_hash"],
        "taxonomy_version": value["taxonomy_version"],
        "taxonomy_hash": value["taxonomy_hash"],
        "state_hash": value["state_hash"],
        "profile_digest": value["profile_digest"],
        "action_digest": value["action_digest"],
        "action_id": value["action_id"],
        "task_lineage_id": value["task_lineage_id"],
        "authority_action_id": value["authority_action_id"],
        "authority_ref": value["authority_ref"],
        "context_head_digest": value["context_head_digest"],
        "workspace_scope": value["workspace_scope"],
        "verdict": value["outcome"] if "outcome" in value else value["verdict"],
    }


def _binding_hash(value: Mapping[str, Any]) -> str:
    return _sha(_binding_body(value))


def _safe_binding(verdict: Mapping[str, Any]) -> dict[str, Any]:
    binding = _binding_body(verdict)
    binding["composition_hash"] = verdict["composition_hash"]
    return binding


def composition_binding(verdict: Mapping[str, Any]) -> dict[str, Any]:
    """Return the hash-only projection safe for an AAR/evidence receipt."""
    valid, errors = validate_verdict(verdict)
    if not valid:
        raise CompositionError("invalid_verdict", ",".join(errors))
    return _safe_binding(verdict)


build_composition_binding = composition_binding


def _is_ordered_subsequence(sequence: Sequence[str], classes: Sequence[str]) -> bool:
    """Return whether ``sequence`` occurs in order, allowing interleaved actions."""
    position = 0
    for action_class in classes:
        if position < len(sequence) and action_class == sequence[position]:
            position += 1
    return position == len(sequence)


def _verdict_hash(body: Mapping[str, Any]) -> str:
    """Commit the public-safe binding projection, not transient verdict metadata."""
    return _binding_hash(body)


def validate_verdict(verdict: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Validate a persisted or caller-returned verdict without admitting it."""
    errors: list[str] = []
    if not isinstance(verdict, Mapping):
        return False, ["not_object"]
    allowed = {
        "schema", "outcome", "reason_code", "action_id", "task_lineage_id", "session_id",
        "workspace_scope", "authority_action_id", "authority_ref", "context_head_digest",
        "policy_version", "policy_hash", "policy_scope", "taxonomy_version", "taxonomy_hash",
        "profile_digest", "action_digest", "action_class", "resource_hash", "data_classification",
        "impact", "budget_cost", "budget_used_before", "budget_used_after", "prior_action_classes",
        "prior_state_hash", "state_hash", "state_version", "sequence_no", "matched_sequence",
        "override_ref", "composition_hash", "idempotent_replay", "state_mutated",
    }
    if set(verdict) - allowed:
        errors.append("unknown_field")
    if verdict.get("schema") != COMPOSITION_VERDICT_SCHEMA:
        errors.append("schema")
    for field in ("action_id", "task_lineage_id", "session_id", "workspace_scope",
                  "authority_action_id", "authority_ref", "policy_version", "policy_scope",
                  "taxonomy_version", "action_class", "resource_hash", "data_classification",
                  "impact", "reason_code"):
        try:
            _opaque_ref(verdict.get(field), field)
        except CompositionError:
            errors.append(field)
    for field in ("policy_hash", "taxonomy_hash", "profile_digest", "action_digest",
                  "context_head_digest", "resource_hash", "prior_state_hash", "state_hash"):
        if not _is_hash(verdict.get(field)):
            errors.append(field)
    if verdict.get("outcome") not in OUTCOMES:
        errors.append("outcome")
    if verdict.get("policy_scope") not in POLICY_SCOPES:
        errors.append("policy_scope")
    if verdict.get("data_classification") not in CLASSIFICATIONS:
        errors.append("data_classification")
    if verdict.get("impact") not in IMPACTS:
        errors.append("impact")
    for field in ("budget_cost", "budget_used_before", "budget_used_after"):
        value = verdict.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
            errors.append(field)
    for field in ("state_version", "sequence_no"):
        value = verdict.get(field)
        if type(value) is not int or value < 0:
            errors.append(field)
    if not isinstance(verdict.get("prior_action_classes"), list):
        errors.append("prior_action_classes")
    if not isinstance(verdict.get("matched_sequence"), list):
        errors.append("matched_sequence")
    if verdict.get("idempotent_replay") is not None and type(verdict["idempotent_replay"]) is not bool:
        errors.append("idempotent_replay")
    if verdict.get("state_mutated") is not None and type(verdict["state_mutated"]) is not bool:
        errors.append("state_mutated")
    supplied = verdict.get("composition_hash")
    if not isinstance(supplied, str) or not _is_hash(supplied) \
            or supplied.lower() != _verdict_hash(verdict):
        errors.append("composition_hash")
    return not errors, sorted(set(errors))


def validate_composition_binding(binding: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Validate a persisted hash-only binding and recompute its commitment."""
    if not isinstance(binding, Mapping):
        return False, ["not_object"]
    errors: list[str] = []
    if set(binding) != _COMPOSITION_BINDING_FIELDS:
        errors.append("fields")
    if binding.get("schema") != COMPOSITION_BINDING_SCHEMA:
        errors.append("schema")
    for field in ("policy_version", "taxonomy_version", "action_id", "task_lineage_id",
                  "authority_action_id", "authority_ref", "workspace_scope", "verdict"):
        try:
            _opaque_ref(binding.get(field), field)
        except CompositionError:
            errors.append(field)
    for field in ("policy_hash", "taxonomy_hash", "state_hash", "profile_digest",
                  "action_digest", "context_head_digest", "composition_hash"):
        if not _is_hash(binding.get(field)):
            errors.append(field)
    if binding.get("verdict") not in OUTCOMES:
        errors.append("verdict")
    if not errors:
        supplied = binding["composition_hash"]
        if supplied.lower() != _binding_hash(binding):
            errors.append("composition_hash")
    return not errors, sorted(set(errors))


def safe_composition_projection(row: Mapping[str, Any], *, chain_ok: bool = True) -> Optional[dict[str, Any]]:
    """Return a composition projection only when its row-level evidence verifies."""
    if not chain_ok:
        return None
    raw = row.get("composition_json")
    if raw is None:
        return None
    try:
        binding = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    valid, _ = validate_composition_binding(binding)
    if not valid:
        return None
    columns = {
        "schema": "composition_schema",
        "policy_version": "composition_policy_version",
        "policy_hash": "composition_policy_hash",
        "state_hash": "composition_state_hash",
        "profile_digest": "composition_profile_hash",
        "action_id": "composition_action_id",
        "task_lineage_id": "composition_lineage_id",
        "verdict": "composition_verdict",
        "composition_hash": "composition_hash",
    }
    if any(row.get(column) != binding[field] for field, column in columns.items()):
        return None
    return dict(binding)


def verify_persisted_admission(conn: sqlite3.Connection, org_id: str,
                               verdict: Mapping[str, Any]) -> bool:
    """Confirm that an allow verdict is the exact durable admission result.

    Shape validation alone is insufficient: a caller could recompute a valid
    hash over a forged verdict.  Effects recorded by ``record_usage`` therefore
    require a matching row written by the serialized admission transaction.
    Retry metadata is transport-only and is excluded from the comparison.
    """
    if not isinstance(verdict, Mapping) or verdict.get("outcome") != "allow":
        return False
    try:
        row = conn.execute(
            "SELECT action_digest, state_hash, verdict_json FROM composition_admissions "
            "WHERE org_id=? AND task_lineage_id=? AND action_id=?",
            (org_id, verdict.get("task_lineage_id"), verdict.get("action_id")),
        ).fetchone()
        if row is None:
            return False
        stored = json.loads(row["verdict_json"])
        if not isinstance(stored, Mapping):
            return False
        if row["action_digest"] != verdict.get("action_digest") \
                or row["state_hash"] != verdict.get("state_hash"):
            return False
        candidate = {key: value for key, value in verdict.items()
                     if key != "idempotent_replay"}
        original = {key: value for key, value in stored.items()
                    if key != "idempotent_replay"}
        return _canonical(candidate) == _canonical(original)
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return False


class CompositionEngine:
    """Durable serialized admission engine for one trusted taxonomy/policy pair."""

    def __init__(self, taxonomy: TrustedActionRegistry, policy: CompositionPolicy,
                 *, authority_key: Optional[bytes] = None,
                 reset_key: Optional[bytes] = None):
        if not isinstance(taxonomy, TrustedActionRegistry):
            raise TypeError("taxonomy must be a TrustedActionRegistry")
        if not isinstance(policy, CompositionPolicy):
            raise TypeError("policy must be a CompositionPolicy")
        if authority_key is not None and (not isinstance(authority_key, (bytes, bytearray)) or not authority_key):
            raise ValueError("authority_key must be non-empty bytes")
        if reset_key is not None and (not isinstance(reset_key, (bytes, bytearray)) or not reset_key):
            raise ValueError("reset_key must be non-empty bytes")
        self.taxonomy = taxonomy
        self.policy = policy
        self.authority_key = bytes(authority_key) if authority_key is not None else None
        self.reset_key = bytes(reset_key) if reset_key is not None else self.authority_key
        self._ensure_tables_sql = (
            "CREATE TABLE IF NOT EXISTS composition_lineages ("
            "org_id TEXT NOT NULL, task_lineage_id TEXT NOT NULL, session_id TEXT NOT NULL, "
            "workspace_scope TEXT NOT NULL, policy_scope TEXT NOT NULL, policy_version TEXT NOT NULL, "
            "policy_hash TEXT NOT NULL, taxonomy_version TEXT NOT NULL, taxonomy_hash TEXT NOT NULL, "
            "authority_action_id TEXT NOT NULL, authority_ref TEXT NOT NULL, context_head_digest TEXT NOT NULL, "
            "state_version INTEGER NOT NULL DEFAULT 0, budget_used_micros INTEGER NOT NULL DEFAULT 0, "
            "admitted_actions_json TEXT NOT NULL DEFAULT '[]', state_hash TEXT NOT NULL, "
            "parent_lineage_id TEXT, reset_authorization_hash TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
            "PRIMARY KEY(org_id, task_lineage_id));"
            "CREATE TABLE IF NOT EXISTS composition_admissions ("
            "id TEXT PRIMARY KEY, org_id TEXT NOT NULL, task_lineage_id TEXT NOT NULL, action_id TEXT NOT NULL, "
            "idempotency_key TEXT, action_digest TEXT NOT NULL, verdict_json TEXT NOT NULL, outcome TEXT NOT NULL, "
            "reason_code TEXT NOT NULL, state_version INTEGER NOT NULL, state_hash TEXT NOT NULL, created_at REAL NOT NULL, "
            "UNIQUE(org_id, task_lineage_id, action_id), UNIQUE(org_id, task_lineage_id, idempotency_key));"
            "CREATE INDEX IF NOT EXISTS ix_composition_lineage ON composition_admissions(org_id, task_lineage_id);"
        )

    # ---- authenticated authority decisions ---------------------------------
    def _authorization(self, body: dict[str, Any], *, key: Optional[bytes]) -> dict[str, Any]:
        if not key:
            raise CompositionError("authorization_required", "authenticated authority authorization is required")
        payload = dict(body)
        payload["schema"] = AUTHORIZATION_SCHEMA
        payload["policy_version"] = self.policy.version
        payload["policy_hash"] = self.policy.policy_hash
        payload["taxonomy_version"] = self.taxonomy.version
        payload["taxonomy_hash"] = self.taxonomy.taxonomy_hash
        payload["signature"] = hmac.new(key, _canonical(payload).encode(), hashlib.sha256).hexdigest()
        return payload

    def issue_lineage_authorization(self, *, org_id: str, task_lineage_id: str,
                                    session_id: str, workspace_scope: str,
                                    authority_action_id: str, authority_ref: str,
                                    context_head_digest: str) -> dict[str, Any]:
        return self._authorization({
            "kind": "lineage_start", "org_id": _opaque_ref(org_id, "org_id"),
            "task_lineage_id": _opaque_ref(task_lineage_id, "task_lineage_id"),
            "session_id": _opaque_ref(session_id, "session_id"),
            "workspace_scope": _opaque_ref(workspace_scope, "workspace_scope"),
            "authority_action_id": _opaque_ref(authority_action_id, "authority_action_id"),
            "authority_ref": _opaque_ref(authority_ref, "authority_ref"),
            "context_head_digest": _safe_digest_field(context_head_digest, "context_head_digest"),
        }, key=self.authority_key)

    def issue_reset_authorization(self, *, org_id: str, prior_lineage_id: str,
                                   successor_lineage_id: str, session_id: str,
                                   workspace_scope: str, authority_action_id: str,
                                   authority_ref: str, context_head_digest: str) -> dict[str, Any]:
        return self._authorization({
            "kind": "lineage_reset", "org_id": _opaque_ref(org_id, "org_id"),
            "prior_lineage_id": _opaque_ref(prior_lineage_id, "prior_lineage_id"),
            "successor_lineage_id": _opaque_ref(successor_lineage_id, "successor_lineage_id"),
            "session_id": _opaque_ref(session_id, "session_id"),
            "workspace_scope": _opaque_ref(workspace_scope, "workspace_scope"),
            "authority_action_id": _opaque_ref(authority_action_id, "authority_action_id"),
            "authority_ref": _opaque_ref(authority_ref, "authority_ref"),
            "context_head_digest": _safe_digest_field(context_head_digest, "context_head_digest"),
        }, key=self.reset_key)

    def issue_override(self, *, org_id: str, task_lineage_id: str, action_id: str,
                       action_digest: str, authority_action_id: str, authority_ref: str,
                       workspace_scope: str, context_head_digest: str,
                       approval_ref: str) -> dict[str, Any]:
        return self._authorization({
            "kind": "composition_override", "org_id": _opaque_ref(org_id, "org_id"),
            "task_lineage_id": _opaque_ref(task_lineage_id, "task_lineage_id"),
            "action_id": _opaque_ref(action_id, "action_id"),
            "action_digest": _safe_digest_field(action_digest, "action_digest"),
            "authority_action_id": _opaque_ref(authority_action_id, "authority_action_id"),
            "authority_ref": _opaque_ref(authority_ref, "authority_ref"),
            "workspace_scope": _opaque_ref(workspace_scope, "workspace_scope"),
            "context_head_digest": _safe_digest_field(context_head_digest, "context_head_digest"),
            "approval_ref": _opaque_ref(approval_ref, "approval_ref"),
            "decision": "allow",
        }, key=self.authority_key)

    def _verify_authorization(self, supplied: Any, *, expected: Mapping[str, Any], key: Optional[bytes]) -> dict[str, Any]:
        if not key or not isinstance(supplied, Mapping):
            raise CompositionError("authorization_required", "authenticated authority authorization is required")
        payload = dict(supplied)
        signature = payload.pop("signature", None)
        if not isinstance(signature, str) or not _HEX64.fullmatch(signature):
            raise CompositionError("invalid_authorization", "authorization signature is invalid")
        expected_body = dict(expected)
        expected_body.update({
            "schema": AUTHORIZATION_SCHEMA,
            "policy_version": self.policy.version,
            "policy_hash": self.policy.policy_hash,
            "taxonomy_version": self.taxonomy.version,
            "taxonomy_hash": self.taxonomy.taxonomy_hash,
        })
        if payload != expected_body:
            raise CompositionError("invalid_authorization", "authorization does not match the requested transition")
        actual = hmac.new(key, _canonical(payload).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(actual, signature.lower()):
            raise CompositionError("invalid_authorization", "authorization signature is invalid")
        payload["signature"] = signature.lower()
        return payload

    # ---- durable state ------------------------------------------------------
    def _ensure_tables(self, conn: sqlite3.Connection) -> None:
        # ``executescript`` commits any active transaction before running. Use
        # individual DDL statements so a caller's enclosing admission/effect
        # transaction cannot be committed as a side effect of table discovery.
        was_in_transaction = conn.in_transaction
        for statement in self._ensure_tables_sql.split(";"):
            if statement.strip():
                conn.execute(statement)
        if not was_in_transaction:
            conn.commit()

    def _state_hash(self, *, org_id: str, task_lineage_id: str, session_id: str,
                    workspace_scope: str, policy_scope: str, policy_version: str,
                    policy_hash: str, taxonomy_version: str, taxonomy_hash: str,
                    authority_action_id: str, authority_ref: str, context_head_digest: str, state_version: int,
                    budget_used_micros: int, admitted_actions: list[dict[str, Any]],
                    parent_lineage_id: Optional[str] = None,
                    reset_authorization_hash: Optional[str] = None) -> str:
        return _sha({
            "schema": COMPOSITION_STATE_SCHEMA,
            "org_id": org_id,
            "task_lineage_id": task_lineage_id,
            "session_id": session_id,
            "workspace_scope": workspace_scope,
            "policy_scope": policy_scope,
            "policy_version": policy_version,
            "policy_hash": policy_hash,
            "taxonomy_version": taxonomy_version,
            "taxonomy_hash": taxonomy_hash,
            "authority_action_id": authority_action_id,
            "authority_ref": authority_ref,
            "context_head_digest": context_head_digest,
            "state_version": state_version,
            "budget_used_micros": budget_used_micros,
            "admitted_actions": admitted_actions,
            "parent_lineage_id": parent_lineage_id,
            "reset_authorization_hash": reset_authorization_hash,
        })

    def _row_state(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            actions = json.loads(row["admitted_actions_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CompositionError("invalid_state", "stored composition history is not JSON") from exc
        if not isinstance(actions, list):
            raise CompositionError("invalid_state", "stored composition history is not a list")
        state = {
            "schema": COMPOSITION_STATE_SCHEMA,
            "org_id": row["org_id"],
            "task_lineage_id": row["task_lineage_id"],
            "session_id": row["session_id"],
            "workspace_scope": row["workspace_scope"],
            "policy_scope": row["policy_scope"],
            "policy_version": row["policy_version"],
            "policy_hash": row["policy_hash"],
            "taxonomy_version": row["taxonomy_version"],
            "taxonomy_hash": row["taxonomy_hash"],
            "authority_action_id": row["authority_action_id"],
            "authority_ref": row["authority_ref"],
            "context_head_digest": row["context_head_digest"],
            "state_version": int(row["state_version"]),
            "budget_used_micros": int(row["budget_used_micros"]),
            "admitted_actions": actions,
            "state_hash": row["state_hash"],
            "parent_lineage_id": row["parent_lineage_id"],
            "reset_authorization_hash": row["reset_authorization_hash"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        expected = self._state_hash(
            org_id=state["org_id"], task_lineage_id=state["task_lineage_id"],
            session_id=state["session_id"], workspace_scope=state["workspace_scope"],
            policy_scope=state["policy_scope"], policy_version=state["policy_version"],
            policy_hash=state["policy_hash"], taxonomy_version=state["taxonomy_version"],
            taxonomy_hash=state["taxonomy_hash"], authority_action_id=state["authority_action_id"], authority_ref=state["authority_ref"],
            context_head_digest=state["context_head_digest"], state_version=state["state_version"],
            budget_used_micros=state["budget_used_micros"], admitted_actions=actions,
            parent_lineage_id=state["parent_lineage_id"],
            reset_authorization_hash=state["reset_authorization_hash"],
        )
        if expected != state["state_hash"]:
            raise CompositionError("invalid_state", "stored composition state hash does not verify")
        return state

    def get_lineage(self, conn: sqlite3.Connection, org_id: str, task_lineage_id: str) -> Optional[dict[str, Any]]:
        self._ensure_tables(conn)
        row = conn.execute(
            "SELECT * FROM composition_lineages WHERE org_id=? AND task_lineage_id=?",
            (org_id, task_lineage_id),
        ).fetchone()
        return self._row_state(row) if row is not None else None

    def start_lineage(self, conn: sqlite3.Connection, *, org_id: str,
                      task_lineage_id: str, session_id: str, workspace_scope: str,
                      authority_action_id: str, authority_ref: str,
                      context_head_digest: str, authorization: Optional[Mapping[str, Any]] = None,
                      parent_lineage_id: Optional[str] = None,
                      reset_authorization_hash: Optional[str] = None) -> dict[str, Any]:
        org_id = _opaque_ref(org_id, "org_id")
        task_lineage_id = _opaque_ref(task_lineage_id, "task_lineage_id")
        session_id = _opaque_ref(session_id, "session_id")
        workspace_scope = _opaque_ref(workspace_scope, "workspace_scope")
        authority_action_id = _opaque_ref(authority_action_id, "authority_action_id")
        authority_ref = _opaque_ref(authority_ref, "authority_ref")
        context_head_digest = _safe_digest_field(context_head_digest, "context_head_digest")
        if parent_lineage_id is not None or reset_authorization_hash is not None:
            raise CompositionError("reset_requires_authorization",
                                   "successor lineage creation requires reset_lineage")
        self._verify_authorization(authorization, expected={
            "kind": "lineage_start", "org_id": org_id, "task_lineage_id": task_lineage_id,
            "session_id": session_id, "workspace_scope": workspace_scope,
            "authority_action_id": authority_action_id, "authority_ref": authority_ref,
            "context_head_digest": context_head_digest,
        }, key=self.authority_key)
        self._ensure_tables(conn)
        with db.immediate(conn):
            existing = conn.execute(
                "SELECT * FROM composition_lineages WHERE org_id=? AND task_lineage_id=?",
                (org_id, task_lineage_id),
            ).fetchone()
            if existing is not None:
                state = self._row_state(existing)
                if (state["session_id"], state["workspace_scope"], state["authority_action_id"],
                    state["authority_ref"], state["context_head_digest"]) != (
                        session_id, workspace_scope, authority_action_id, authority_ref,
                        context_head_digest):
                    raise CompositionError("lineage_conflict", "lineage already has different bindings")
                return state
            actions: list[dict[str, Any]] = []
            state_hash = self._state_hash(
                org_id=org_id, task_lineage_id=task_lineage_id, session_id=session_id,
                workspace_scope=workspace_scope, policy_scope=self.policy.scope,
                policy_version=self.policy.version, policy_hash=self.policy.policy_hash,
                taxonomy_version=self.taxonomy.version, taxonomy_hash=self.taxonomy.taxonomy_hash,
                authority_action_id=authority_action_id, authority_ref=authority_ref, context_head_digest=context_head_digest,
                state_version=0, budget_used_micros=0, admitted_actions=actions,
                parent_lineage_id=parent_lineage_id, reset_authorization_hash=reset_authorization_hash,
            )
            now = time.time()
            conn.execute(
                "INSERT INTO composition_lineages(org_id,task_lineage_id,session_id,workspace_scope,"
                "policy_scope,policy_version,policy_hash,taxonomy_version,taxonomy_hash,authority_action_id,"
                "authority_ref,context_head_digest,state_version,budget_used_micros,admitted_actions_json,state_hash,"
                "parent_lineage_id,reset_authorization_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (org_id, task_lineage_id, session_id, workspace_scope, self.policy.scope,
                 self.policy.version, self.policy.policy_hash, self.taxonomy.version,
                 self.taxonomy.taxonomy_hash, authority_action_id, authority_ref, context_head_digest,
                 0, 0, "[]", state_hash, parent_lineage_id, reset_authorization_hash, now, now),
            )
            return self._row_state(conn.execute(
                "SELECT * FROM composition_lineages WHERE org_id=? AND task_lineage_id=?",
                (org_id, task_lineage_id),
            ).fetchone())

    def reset_lineage(self, conn: sqlite3.Connection, *, org_id: str,
                      task_lineage_id: str, successor_lineage_id: str, session_id: str,
                      workspace_scope: str, authority_action_id: str, authority_ref: str,
                      context_head_digest: str, authorization: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        org_id = _opaque_ref(org_id, "org_id")
        task_lineage_id = _opaque_ref(task_lineage_id, "task_lineage_id")
        successor_lineage_id = _opaque_ref(successor_lineage_id, "successor_lineage_id")
        session_id = _opaque_ref(session_id, "session_id")
        workspace_scope = _opaque_ref(workspace_scope, "workspace_scope")
        authority_action_id = _opaque_ref(authority_action_id, "authority_action_id")
        authority_ref = _opaque_ref(authority_ref, "authority_ref")
        context_head_digest = _safe_digest_field(context_head_digest, "context_head_digest")
        auth = self._verify_authorization(authorization, expected={
            "kind": "lineage_reset", "org_id": org_id, "prior_lineage_id": task_lineage_id,
            "successor_lineage_id": successor_lineage_id, "session_id": session_id,
            "workspace_scope": workspace_scope, "authority_action_id": authority_action_id,
            "authority_ref": authority_ref, "context_head_digest": context_head_digest,
        }, key=self.reset_key)
        self._ensure_tables(conn)
        reset_hash = _sha({key: value for key, value in auth.items() if key != "signature"})
        with db.immediate(conn):
            prior = conn.execute(
                "SELECT 1 FROM composition_lineages WHERE org_id=? AND task_lineage_id=?",
                (org_id, task_lineage_id),
            ).fetchone()
            if prior is None:
                raise CompositionError("unknown_lineage", "prior lineage does not exist")
            existing = conn.execute(
                "SELECT * FROM composition_lineages WHERE org_id=? AND task_lineage_id=?",
                (org_id, successor_lineage_id),
            ).fetchone()
            if existing is not None:
                state = self._row_state(existing)
                if state["reset_authorization_hash"] != reset_hash:
                    raise CompositionError("lineage_conflict", "successor lineage already exists")
                return state
            actions: list[dict[str, Any]] = []
            state_hash = self._state_hash(
                org_id=org_id, task_lineage_id=successor_lineage_id, session_id=session_id,
                workspace_scope=workspace_scope, policy_scope=self.policy.scope,
                policy_version=self.policy.version, policy_hash=self.policy.policy_hash,
                taxonomy_version=self.taxonomy.version, taxonomy_hash=self.taxonomy.taxonomy_hash,
                authority_action_id=authority_action_id, authority_ref=authority_ref, context_head_digest=context_head_digest,
                state_version=0, budget_used_micros=0, admitted_actions=actions,
                parent_lineage_id=task_lineage_id, reset_authorization_hash=reset_hash,
            )
            now = time.time()
            conn.execute(
                "INSERT INTO composition_lineages(org_id,task_lineage_id,session_id,workspace_scope,"
                "policy_scope,policy_version,policy_hash,taxonomy_version,taxonomy_hash,authority_action_id,"
                "authority_ref,context_head_digest,state_version,budget_used_micros,admitted_actions_json,state_hash,"
                "parent_lineage_id,reset_authorization_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (org_id, successor_lineage_id, session_id, workspace_scope, self.policy.scope,
                 self.policy.version, self.policy.policy_hash, self.taxonomy.version,
                 self.taxonomy.taxonomy_hash, authority_action_id, authority_ref, context_head_digest,
                 0, 0, "[]", state_hash, task_lineage_id, reset_hash, now, now),
            )
            return self._row_state(conn.execute(
                "SELECT * FROM composition_lineages WHERE org_id=? AND task_lineage_id=?",
                (org_id, successor_lineage_id),
            ).fetchone())

    # ---- admission ----------------------------------------------------------
    def _review(self, *, action_id: str, task_lineage_id: str, session_id: str,
                workspace_scope: str, reason_code: str) -> dict[str, Any]:
        return {
            "schema": COMPOSITION_VERDICT_SCHEMA,
            "outcome": "review",
            "reason_code": _public_ref(reason_code),
            "action_id": _public_ref(action_id),
            "task_lineage_id": _public_ref(task_lineage_id),
            "session_id": _public_ref(session_id),
            "workspace_scope": _public_ref(workspace_scope),
            "state_mutated": False,
        }

    def _base_verdict(self, *, outcome: str, reason_code: str, action_id: str,
                      task_lineage_id: str, session_id: str, workspace_scope: str,
                      authority_action_id: str, authority_ref: str, context_head_digest: str,
                      resolved: ResolvedAction, state_hash: str,
                      state_version: int, budget_before: int, budget_after: int,
                      prior_classes: list[str], matched_sequence: list[str],
                      prior_state_hash: str, sequence_no: int,
                      override_ref: Optional[str] = None, state_mutated: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": COMPOSITION_VERDICT_SCHEMA,
            "outcome": outcome,
            "reason_code": reason_code,
            "action_id": action_id,
            "task_lineage_id": task_lineage_id,
            "session_id": session_id,
            "workspace_scope": workspace_scope,
            "authority_action_id": authority_action_id,
            "authority_ref": authority_ref,
            "context_head_digest": context_head_digest,
            "policy_version": self.policy.version,
            "policy_hash": self.policy.policy_hash,
            "policy_scope": self.policy.scope,
            "taxonomy_version": self.taxonomy.version,
            "taxonomy_hash": self.taxonomy.taxonomy_hash,
            "profile_digest": resolved.profile_digest,
            "action_digest": resolved.action_digest,
            "action_class": resolved.action_class,
            "resource_hash": _sha(resolved.resource),
            "data_classification": resolved.data_classification,
            "impact": resolved.impact,
            "budget_cost": resolved.budget_cost,
            "budget_used_before": budget_before / 1_000_000,
            "budget_used_after": budget_after / 1_000_000,
            "prior_action_classes": prior_classes,
            "matched_sequence": matched_sequence,
            "prior_state_hash": prior_state_hash,
            "state_hash": state_hash,
            "state_version": state_version,
            "sequence_no": sequence_no,
            "state_mutated": state_mutated,
        }
        if override_ref is not None:
            body["override_ref"] = override_ref
        body["composition_hash"] = _verdict_hash(body)
        return body

    def _verify_override(self, override: Any, *, org_id: str, task_lineage_id: str,
                         action_id: str, action_digest: str, policy_hash: str,
                         taxonomy_hash: str, authority_ref: str,
                         workspace_scope: str, context_head_digest: str) -> dict[str, Any]:
        if not self.authority_key or not isinstance(override, Mapping):
            raise CompositionError("invalid_override", "override is not authenticated")
        try:
            override_authority_action_id = _opaque_ref(
                override.get("authority_action_id"), "authority_action_id")
            approval_ref = _opaque_ref(override.get("approval_ref"), "approval_ref")
        except CompositionError as exc:
            raise CompositionError("invalid_override", "override references are invalid") from exc
        expected = {
            "kind": "composition_override", "org_id": org_id,
            "task_lineage_id": task_lineage_id, "action_id": action_id,
            "action_digest": action_digest,
            "authority_action_id": override_authority_action_id,
            "authority_ref": authority_ref,
            "workspace_scope": workspace_scope,
            "context_head_digest": context_head_digest,
            "approval_ref": approval_ref, "decision": "allow",
        }
        body = dict(override)
        signature = body.pop("signature", None)
        if not isinstance(signature, str) or not _HEX64.fullmatch(signature):
            raise CompositionError("invalid_override", "override signature is invalid")
        full = dict(expected)
        full.update({
            "schema": AUTHORIZATION_SCHEMA,
            "policy_version": self.policy.version,
            "policy_hash": policy_hash,
            "taxonomy_version": self.taxonomy.version,
            "taxonomy_hash": taxonomy_hash,
        })
        if body != full:
            raise CompositionError("invalid_override", "override is not bound to this action")
        actual = hmac.new(self.authority_key, _canonical(body).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(actual, signature.lower()):
            raise CompositionError("invalid_override", "override signature is invalid")
        return {"approval_ref": body["approval_ref"], "authority_action_id": body["authority_action_id"]}

    def admit(self, conn: sqlite3.Connection, *, org_id: str,
              task_lineage_id: str, session_id: str, workspace_scope: str,
              authority_action_id: str, authority_ref: str, context_head_digest: str,
              action_id: str, tool_endpoint: str, arguments: Mapping[str, Any],
              idempotency_key: Optional[str] = None, override: Optional[Mapping[str, Any]] = None,
              claimed_profile: Optional[Mapping[str, Any]] = None,
              claimed_impact: Any = None, claimed_cost: Any = None,
              claimed_classification: Any = None, risk: Any = None,
              cost: Any = None) -> dict[str, Any]:
        """Admit one action against authoritative durable lineage state.

        The first lineage state must be created by :meth:`start_lineage` with a
        signed authority decision.  A missing/unknown action is a review result,
        never an allow.  Only ``allow`` transitions append to admitted history;
        denied/review/hold attempts cannot consume budget or alter composition.
        """
        raw_ids = {
            "action_id": action_id, "task_lineage_id": task_lineage_id,
            "session_id": session_id, "workspace_scope": workspace_scope,
        }
        try:
            org_id = _opaque_ref(org_id, "org_id")
            task_lineage_id = _opaque_ref(task_lineage_id, "task_lineage_id")
            session_id = _opaque_ref(session_id, "session_id")
            workspace_scope = _opaque_ref(workspace_scope, "workspace_scope")
            authority_action_id = _opaque_ref(authority_action_id, "authority_action_id")
            authority_ref = _opaque_ref(authority_ref, "authority_ref")
            action_id = _opaque_ref(action_id, "action_id")
            context_head_digest = _safe_digest_field(context_head_digest, "context_head_digest")
            if idempotency_key is not None:
                idempotency_key = _opaque_ref(idempotency_key, "idempotency_key")
        except CompositionError as exc:
            return self._review(**raw_ids, reason_code=exc.code)
        if any(value is not None for value in (claimed_profile, claimed_impact,
                                                claimed_cost, claimed_classification, risk, cost)):
            return self._review(action_id=action_id, task_lineage_id=task_lineage_id,
                                session_id=session_id, workspace_scope=workspace_scope,
                                reason_code="caller_profile_not_authoritative")
        try:
            resolved = self.taxonomy.resolve(tool_endpoint, arguments)
        except CompositionError as exc:
            return self._review(action_id=action_id, task_lineage_id=task_lineage_id,
                                session_id=session_id, workspace_scope=workspace_scope,
                                reason_code=exc.code)
        verified_override = None
        if override is not None:
            try:
                verified_override = self._verify_override(
                    override, org_id=org_id, task_lineage_id=task_lineage_id,
                    action_id=action_id, action_digest=resolved.action_digest,
                    policy_hash=self.policy.policy_hash, taxonomy_hash=self.taxonomy.taxonomy_hash,
                    authority_ref=authority_ref, workspace_scope=workspace_scope,
                    context_head_digest=context_head_digest,
                )
            except CompositionError:
                return self._review(action_id=action_id, task_lineage_id=task_lineage_id,
                                    session_id=session_id, workspace_scope=workspace_scope,
                                    reason_code="invalid_override")
            if verified_override["authority_action_id"] != authority_action_id:
                return self._review(action_id=action_id, task_lineage_id=task_lineage_id,
                                    session_id=session_id, workspace_scope=workspace_scope,
                                    reason_code="invalid_override")
        self._ensure_tables(conn)
        with db.immediate(conn):
            row = conn.execute(
                "SELECT * FROM composition_lineages WHERE org_id=? AND task_lineage_id=?",
                (org_id, task_lineage_id),
            ).fetchone()
            if row is None:
                return self._review(action_id=action_id, task_lineage_id=task_lineage_id,
                                    session_id=session_id, workspace_scope=workspace_scope,
                                    reason_code="lineage_not_started") | {"outcome": "hold"}
            state = self._row_state(row)
            if state["policy_version"] != self.policy.version or state["policy_hash"] != self.policy.policy_hash \
                    or state["taxonomy_version"] != self.taxonomy.version or state["taxonomy_hash"] != self.taxonomy.taxonomy_hash:
                return self._review(action_id=action_id, task_lineage_id=task_lineage_id,
                                    session_id=session_id, workspace_scope=workspace_scope,
                                    reason_code="policy_binding_mismatch") | {"outcome": "hold"}
            if state["workspace_scope"] != workspace_scope or state["authority_ref"] != authority_ref \
                    or state["context_head_digest"] != context_head_digest \
                    or (override is None and state["authority_action_id"] != authority_action_id):
                return self._review(action_id=action_id, task_lineage_id=task_lineage_id,
                                    session_id=session_id, workspace_scope=workspace_scope,
                                    reason_code="lineage_binding_mismatch") | {"outcome": "hold"}
            if state["policy_scope"] == "session" and state["session_id"] != session_id:
                return self._review(action_id=action_id, task_lineage_id=task_lineage_id,
                                    session_id=session_id, workspace_scope=workspace_scope,
                                    reason_code="session_binding_mismatch") | {"outcome": "hold"}

            existing = conn.execute(
                "SELECT verdict_json,action_digest FROM composition_admissions "
                "WHERE org_id=? AND task_lineage_id=? AND action_id=?",
                (org_id, task_lineage_id, action_id),
            ).fetchone()
            if existing is None and idempotency_key is not None:
                existing = conn.execute(
                    "SELECT verdict_json,action_digest FROM composition_admissions "
                    "WHERE org_id=? AND task_lineage_id=? AND idempotency_key=?",
                    (org_id, task_lineage_id, idempotency_key),
                ).fetchone()
            if existing is not None:
                try:
                    replay = json.loads(existing["verdict_json"])
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise CompositionError("invalid_admission", "stored admission is invalid") from exc
                if (existing["action_digest"] != resolved.action_digest
                        or replay.get("action_id") != action_id
                        or replay.get("authority_action_id") != authority_action_id):
                    return self._review(action_id=action_id, task_lineage_id=task_lineage_id,
                                        session_id=session_id, workspace_scope=workspace_scope,
                                        reason_code="idempotency_conflict") | {"outcome": "deny"}
                valid, errors = validate_verdict(replay)
                if not valid:
                    raise CompositionError("invalid_admission", "stored admission is invalid")
                replay["idempotent_replay"] = True
                return replay

            history = state["admitted_actions"]
            prior_classes = [item["action_class"] for item in history]
            matched_pair = False
            for prior_class in prior_classes:
                if tuple(sorted((resolved.action_class, prior_class))) in self.policy.prohibited_pairs:
                    matched_pair = True
                    break
            matched_sequence: list[str] = []
            classes_with_candidate = prior_classes + [resolved.action_class]
            for sequence in self.policy.prohibited_sequences:
                if (sequence[-1] == resolved.action_class
                        and _is_ordered_subsequence(sequence, classes_with_candidate)):
                    matched_sequence = list(sequence)
                    break
            budget_before = state["budget_used_micros"]
            cost_units = _units(resolved.budget_cost)
            budget_after = budget_before + cost_units
            budget_exceeded = self.policy.budget_limit is not None \
                and budget_after > _units(self.policy.budget_limit)
            reason_code = "admitted"
            outcome = "allow"
            if matched_pair:
                outcome, reason_code = "deny", "prohibited_pair"
            elif matched_sequence:
                outcome, reason_code = "deny", "prohibited_sequence"
            elif budget_exceeded:
                outcome, reason_code = "deny", "budget_exceeded"

            override_ref = None
            if outcome != "allow" and verified_override is not None:
                outcome, reason_code = "allow", "override_authorized"
                override_ref = verified_override["approval_ref"]

            prior_state_hash = state["state_hash"]
            if outcome == "allow":
                next_version = state["state_version"] + 1
                next_history = history + [{
                    "sequence_no": next_version,
                    "action_id": action_id,
                    "authority_action_id": authority_action_id,
                    "action_digest": resolved.action_digest,
                    "profile_digest": resolved.profile_digest,
                    "action_class": resolved.action_class,
                    "resource_hash": _sha(resolved.resource),
                    "data_classification": resolved.data_classification,
                    "impact": resolved.impact,
                    "budget_cost": resolved.budget_cost,
                }]
                next_budget = budget_after
                next_hash = self._state_hash(
                    org_id=org_id, task_lineage_id=task_lineage_id, session_id=state["session_id"],
                    workspace_scope=state["workspace_scope"], policy_scope=state["policy_scope"],
                    policy_version=state["policy_version"], policy_hash=state["policy_hash"],
                    taxonomy_version=state["taxonomy_version"], taxonomy_hash=state["taxonomy_hash"],
                    authority_action_id=state["authority_action_id"], authority_ref=state["authority_ref"], context_head_digest=state["context_head_digest"],
                    state_version=next_version, budget_used_micros=next_budget,
                    admitted_actions=next_history, parent_lineage_id=state["parent_lineage_id"],
                    reset_authorization_hash=state["reset_authorization_hash"],
                )
                verdict = self._base_verdict(
                    outcome=outcome, reason_code=reason_code, action_id=action_id,
                    task_lineage_id=task_lineage_id, session_id=session_id,
                    workspace_scope=workspace_scope, authority_action_id=authority_action_id,
                    authority_ref=authority_ref, context_head_digest=context_head_digest,
                    resolved=resolved, state_hash=next_hash,
                    state_version=next_version, budget_before=budget_before,
                    budget_after=next_budget, prior_classes=prior_classes,
                    matched_sequence=matched_sequence, prior_state_hash=prior_state_hash,
                    sequence_no=next_version, override_ref=override_ref, state_mutated=True,
                )
                conn.execute(
                    "UPDATE composition_lineages SET state_version=?,budget_used_micros=?,"
                    "admitted_actions_json=?,state_hash=?,updated_at=? WHERE org_id=? AND task_lineage_id=?",
                    (next_version, next_budget,
                     json.dumps(next_history, sort_keys=True, separators=(",", ":")),
                     next_hash, time.time(), org_id, task_lineage_id),
                )
                state_version, state_hash = next_version, next_hash
            else:
                verdict = self._base_verdict(
                    outcome=outcome, reason_code=reason_code, action_id=action_id,
                    task_lineage_id=task_lineage_id, session_id=session_id,
                    workspace_scope=workspace_scope, authority_action_id=authority_action_id,
                    authority_ref=authority_ref, context_head_digest=context_head_digest,
                    resolved=resolved, state_hash=state["state_hash"],
                    state_version=state["state_version"], budget_before=budget_before,
                    budget_after=budget_before, prior_classes=prior_classes,
                    matched_sequence=matched_sequence, prior_state_hash=prior_state_hash,
                    sequence_no=state["state_version"], state_mutated=False,
                )
                state_version, state_hash = state["state_version"], state["state_hash"]
            conn.execute(
                "INSERT INTO composition_admissions(id,org_id,task_lineage_id,action_id,idempotency_key,"
                "action_digest,verdict_json,outcome,reason_code,state_version,state_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (db.new_id("cmp"), org_id, task_lineage_id, action_id, idempotency_key,
                 resolved.action_digest, json.dumps(verdict, sort_keys=True, separators=(",", ":")),
                 verdict["outcome"], verdict["reason_code"], state_version, state_hash, time.time()),
            )
            return verdict


CompositionChecker = CompositionEngine

__all__ = [
    "ACTION_PROFILE_SCHEMA", "ACTION_TAXONOMY_SCHEMA", "COMPOSITION_POLICY_SCHEMA",
    "COMPOSITION_STATE_SCHEMA", "COMPOSITION_VERDICT_SCHEMA", "COMPOSITION_BINDING_SCHEMA",
    "AUTHORIZATION_SCHEMA", "CLASSIFICATIONS", "IMPACTS", "OUTCOMES", "POLICY_SCOPES",
    "CompositionError", "ActionProfile", "ResolvedAction", "TrustedActionRegistry",
    "TrustedActionTaxonomy", "ActionTaxonomy", "CompositionPolicy", "CompositionEngine",
    "CompositionChecker", "composition_binding", "build_composition_binding",
    "validate_verdict", "validate_composition_binding", "safe_composition_projection",
    "verify_persisted_admission",
]
