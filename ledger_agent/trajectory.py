"""Runtime-contract trajectory and evidence enforcement (#250).

This module is deliberately dependency-free.  It records an agent trajectory as
an append-only hash chain and provides deterministic, fail-closed evidence
verifiers for submission decisions.  The schema and evidence distinction are
based on *Agent Safety Should Be a Runtime Contract*, arXiv:2608.11274.

The module is additive to Ledger's prebind and receipt schemas: callers can use
:func:`trajectory_root_hash` as another value in an existing ``evidence_hashes``
collection without changing those schemas.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Optional


TRAJECTORY_SCHEMA = "perseus-ledger-trajectory/v1"
"""Versioned schema identifier for a serialized trajectory."""

GENESIS_HASH = hashlib.sha256(b"genesis").hexdigest()
"""The hash used as ``h_0`` for every trajectory."""

EVENT_KINDS = frozenset(
    {
        "tool_call",
        "tool_result",
        "file_read",
        "file_write",
        "shell_exec",
        "commit",
        "screenshot",
        "citation_lookup",
        "human_approval",
        "model_message",
    }
)
"""The closed event-kind vocabulary from Definition 1."""

# Friendly aliases make the schema easy to discover without duplicating the
# canonical constants.
TRAJECTORY_VERSION = TRAJECTORY_SCHEMA
ALLOWED_EVENT_KINDS = EVENT_KINDS
GENESIS = GENESIS_HASH
HARD = "accept"
REJECT = "reject"
SOFT = "soft"

_EVENT_FIELDS = frozenset({"kind", "timestamp_ms", "payload", "prev_hash", "hash"})
_HEX_DIGITS = frozenset("0123456789abcdef")


def canonical_json(value: Any) -> str:
    """Return Ledger's stable canonical JSON representation.

    Hashes intentionally use sorted keys and compact separators.  Unicode is
    kept as UTF-8 rather than escaped so the representation is unambiguous and
    compact while remaining deterministic.
    """

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value.lower()) <= _HEX_DIGITS
    )


def _event_material(event: Mapping[str, Any], prev_hash: str) -> dict[str, Any]:
    """Return the fields covered by an event hash, excluding ``hash`` itself."""

    return {
        "kind": event.get("kind"),
        "timestamp_ms": event.get("timestamp_ms"),
        "payload": event.get("payload"),
        "prev_hash": prev_hash,
    }


def event_hash(event: Mapping[str, Any], prev_hash: Optional[str] = None) -> str:
    """Hash an event's canonical fields and its predecessor hash.

    ``prev_hash`` defaults to the event's stored predecessor, which is useful
    for independently checking a serialized event.  The stored ``hash`` is
    never included in the digest.
    """

    predecessor = event.get("prev_hash") if prev_hash is None else prev_hash
    if not isinstance(predecessor, str):
        raise ValueError("prev_hash must be a string")
    return hashlib.sha256(_canonical_bytes(_event_material(event, predecessor))).hexdigest()


class Trajectory:
    """An append-only sequence of hash-chained agent events.

    Events are plain dictionaries with ``kind``, ``timestamp_ms``, ``payload``,
    ``prev_hash``, and ``hash`` fields.  The first event links to
    :data:`GENESIS_HASH`; the current ``head_hash`` is the last event hash, or
    the genesis hash for an empty trajectory.
    """

    def __init__(self, events: Optional[Iterable[Mapping[str, Any]]] = None):
        if events is None:
            copied: list[dict[str, Any]] = []
        else:
            if isinstance(events, (str, bytes)):
                raise TypeError("events must be an iterable of event mappings")
            copied = []
            for event in events:
                if not isinstance(event, Mapping):
                    raise TypeError("each event must be a mapping")
                copied.append(copy.deepcopy(dict(event)))
        self.events = copied
        self._schema = TRAJECTORY_SCHEMA
        self._declared_head_hash: Optional[str] = None

    def __iter__(self):
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    @property
    def head_hash(self) -> str:
        """Return the stored head hash, or the genesis hash when empty."""

        if not self.events:
            return GENESIS_HASH
        value = self.events[-1].get("hash")
        return value if isinstance(value, str) else ""

    def append(
        self,
        kind: str,
        payload: dict[str, Any],
        timestamp_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """Append one event and return its plain dictionary representation.

        Unknown kinds, non-dict payloads, invalid timestamps, and payloads that
        cannot be represented in canonical JSON are rejected before mutation.
        """

        if not isinstance(kind, str) or kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {kind!r}")
        if not isinstance(payload, dict):
            raise TypeError("event payload must be a dict")
        if timestamp_ms is None:
            timestamp_ms = time.time_ns() // 1_000_000
        if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
            raise TypeError("timestamp_ms must be an integer")

        payload_copy = copy.deepcopy(payload)
        # Validate serializability before changing the trajectory.
        try:
            _canonical_bytes(payload_copy)
        except (TypeError, ValueError) as exc:
            raise TypeError("event payload must be JSON serializable") from exc

        predecessor = self.head_hash
        event: dict[str, Any] = {
            "kind": kind,
            "timestamp_ms": timestamp_ms,
            "payload": payload_copy,
            "prev_hash": predecessor,
        }
        event["hash"] = event_hash(event, predecessor)
        self.events.append(event)
        self._declared_head_hash = event["hash"]
        return event

    def verify_chain(self) -> tuple[bool, str]:
        """Verify schema, predecessor links, and every event hash.

        The reason is a stable lowercase code so callers can make a deterministic
        fail-closed decision without parsing human prose.
        """

        if self._schema != TRAJECTORY_SCHEMA:
            return False, "schema_mismatch"
        predecessor = GENESIS_HASH
        for event in self.events:
            if not isinstance(event, Mapping):
                return False, "invalid_event"
            if set(event) != _EVENT_FIELDS:
                return False, "invalid_event"
            kind = event.get("kind")
            if not isinstance(kind, str) or kind not in EVENT_KINDS:
                return False, "unknown_kind"
            timestamp_ms = event.get("timestamp_ms")
            if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
                return False, "invalid_timestamp"
            if not isinstance(event.get("payload"), dict):
                return False, "invalid_payload"
            stored_prev = event.get("prev_hash")
            stored_hash = event.get("hash")
            if not _is_sha256(stored_prev) or not _is_sha256(stored_hash):
                return False, "invalid_hash"
            if stored_prev != predecessor:
                return False, "prev_hash_mismatch"
            try:
                expected = event_hash(event, predecessor)
            except (TypeError, ValueError):
                return False, "invalid_event"
            if stored_hash != expected:
                return False, "hash_mismatch"
            predecessor = stored_hash

        if self._declared_head_hash is not None and self._declared_head_hash != predecessor:
            return False, "head_hash_mismatch"
        return True, "ok"

    def to_dict(self) -> dict[str, Any]:
        """Serialize the trajectory to a deterministic plain dictionary."""

        return {
            "schema": TRAJECTORY_SCHEMA,
            "events": copy.deepcopy(self.events),
            "head_hash": self.head_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Trajectory":
        """Restore a trajectory without hiding later chain verification errors.

        A malformed top-level envelope raises immediately.  Event-level
        tampering is retained so :meth:`verify_chain` can report it rather than
        making it impossible to inspect a suspect trajectory.
        """

        if not isinstance(value, Mapping):
            raise TypeError("trajectory must be a mapping")
        if value.get("schema") != TRAJECTORY_SCHEMA:
            raise ValueError("invalid trajectory schema")
        events = value.get("events")
        if not isinstance(events, list):
            raise ValueError("trajectory events must be a list")
        trajectory = cls(events)
        declared = value.get("head_hash")
        if declared is not None and not isinstance(declared, str):
            raise ValueError("trajectory head_hash must be a string")
        trajectory._declared_head_hash = declared
        return trajectory


# ── Deterministic evidence verifiers ────────────────────────────────────────


def _event_payload(event: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    payload = event.get("payload") if isinstance(event, Mapping) else None
    return payload if isinstance(payload, Mapping) else None


def _common_verifier_guard(
    event: Mapping[str, Any], ref_state: Mapping[str, Any]
) -> Optional[str]:
    """Return a non-accepting classification for soft/non-deterministic input."""

    payload = _event_payload(event)
    if payload is None:
        return REJECT
    kind = event.get("kind")
    # A model's assertion is not execution evidence, even if it says "done" or
    # happens to contain fields that resemble a tool result.
    if kind == "model_message":
        return SOFT
    if (
        event.get("deterministic") is False
        or payload.get("deterministic") is False
        or payload.get("non_deterministic") is True
        or ref_state.get("deterministic") is False
        or ref_state.get("non_deterministic") is True
    ):
        return SOFT
    return None


def _test_run(event: Mapping[str, Any], property: str, ref_state: Mapping[str, Any]) -> str:
    if property != "test_suite_passes":
        return REJECT
    payload = _event_payload(event)
    exit_code = payload.get("exit_code") if payload is not None else None
    if (
        isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and exit_code == 0
        and ref_state.get("expected_pass") is True
    ):
        return HARD
    return REJECT


def _citation_lookup(event: Mapping[str, Any], property: str, ref_state: Mapping[str, Any]) -> str:
    if property != "citation_real":
        return REJECT
    payload = _event_payload(event)
    if payload is None:
        return REJECT
    url = payload.get("cited_url") or payload.get("url") or payload.get("source_url")
    source_urls = ref_state.get("source_urls")
    if isinstance(source_urls, str):
        allowed = {source_urls}
    elif isinstance(source_urls, Iterable):
        allowed = set(source_urls)
    else:
        allowed = set()
    return HARD if isinstance(url, str) and url in allowed else REJECT


def _diff_value(payload: Mapping[str, Any]) -> Any:
    for key in ("diff", "diff_text", "hunk", "patch"):
        if key in payload:
            return payload[key]
    return None


def _file_diff(event: Mapping[str, Any], property: str, ref_state: Mapping[str, Any]) -> str:
    if property not in {"diff_present", "diff_matches"}:
        return REJECT
    payload = _event_payload(event)
    expected = ref_state.get("expected_hunk")
    actual = _diff_value(payload) if payload is not None else None
    if property == "diff_matches":
        return HARD if expected is not None and actual == expected else REJECT
    if actual in (None, "", [], {}):
        return REJECT
    if expected is None:
        return HARD
    if isinstance(actual, str) and isinstance(expected, str):
        return HARD if expected in actual else REJECT
    return HARD if actual == expected else REJECT


def _log_capture(event: Mapping[str, Any], property: str, ref_state: Mapping[str, Any]) -> str:
    if property != "log_contains":
        return REJECT
    payload = _event_payload(event)
    marker = ref_state.get("marker")
    log_text = payload.get("log_text") if payload is not None else None
    return HARD if isinstance(marker, str) and marker and isinstance(log_text, str) and marker in log_text else REJECT


def _screenshot(event: Mapping[str, Any], property: str, ref_state: Mapping[str, Any]) -> str:
    if property not in {"screenshot", "screenshot_matches", "screenshot_real", "image_matches"}:
        return REJECT
    payload = _event_payload(event)
    expected = ref_state.get("expected_image_sha256")
    return HARD if payload is not None and payload.get("image_sha256") == expected and _is_sha256(expected) else REJECT


def _human_approval(event: Mapping[str, Any], property: str, ref_state: Mapping[str, Any]) -> str:
    if not isinstance(property, str) or not property:
        return REJECT
    payload = _event_payload(event)
    approved_by = payload.get("approved_by") if payload is not None else None
    if not isinstance(approved_by, str) or not approved_by.strip():
        return REJECT
    approval_ref = ref_state.get("approval_ref")
    if approval_ref is not None and approved_by != approval_ref:
        return REJECT
    return HARD


def _shell_exec(event: Mapping[str, Any], property: str, ref_state: Mapping[str, Any]) -> str:
    if not isinstance(property, str) or not property:
        return REJECT
    payload = _event_payload(event)
    exit_code = payload.get("exit_code") if payload is not None else None
    return HARD if isinstance(exit_code, int) and not isinstance(exit_code, bool) else REJECT


def _commit(event: Mapping[str, Any], property: str, ref_state: Mapping[str, Any]) -> str:
    if not isinstance(property, str) or not property:
        return REJECT
    payload = _event_payload(event)
    expected = ref_state.get("expected_commit_sha")
    return HARD if payload is not None and expected is not None and payload.get("commit_sha") == expected else REJECT


def _guarded(
    function: Callable[[Mapping[str, Any], str, Mapping[str, Any]], str]
) -> Callable[[Mapping[str, Any], str, Mapping[str, Any]], str]:
    def wrapped(event: Mapping[str, Any], property: str, ref_state: Mapping[str, Any]) -> str:
        guard = _common_verifier_guard(event, ref_state)
        return guard if guard is not None else function(event, property, ref_state)

    wrapped.__name__ = function.__name__
    wrapped.__doc__ = function.__doc__
    return wrapped


VERIFIER_REGISTRY: dict[str, Callable[[Mapping[str, Any], str, Mapping[str, Any]], str]] = {
    "test_run": _guarded(_test_run),
    "citation_lookup": _guarded(_citation_lookup),
    "file_diff": _guarded(_file_diff),
    "log_capture": _guarded(_log_capture),
    "screenshot": _guarded(_screenshot),
    "human_approval": _guarded(_human_approval),
    "shell_exec": _guarded(_shell_exec),
    "commit": _guarded(_commit),
}
# Public aliases used by integrations that call the registry directly.
EVIDENCE_VERIFIERS = VERIFIER_REGISTRY
VERIFIERS = VERIFIER_REGISTRY


def verify_evidence(
    event: Mapping[str, Any],
    property: Any,
    verifier: Any,
    ref_state: Optional[Mapping[str, Any]] = None,
) -> str:
    """Classify one evidence event as hard ``accept``, ``reject``, or ``soft``.

    ``verifier`` is normally a registry name.  A callable with the same
    ``(event, property, ref_state)`` signature is also accepted for local
    deterministic extensions.  Unknown or missing verifiers deliberately return
    ``soft`` rather than granting evidence.
    """

    # Be forgiving for callers that naturally write (event, verifier, property,
    # ref_state); the explicit names in the public signature remain canonical.
    if isinstance(property, str) and property in VERIFIER_REGISTRY and (
        not isinstance(verifier, str) or verifier not in VERIFIER_REGISTRY
    ):
        property, verifier = verifier, property

    state: Mapping[str, Any]
    if isinstance(ref_state, Mapping):
        state = ref_state
    else:
        state = {}
    if not isinstance(event, Mapping):
        return REJECT
    if verifier is None or verifier == "":
        return SOFT
    if isinstance(verifier, str):
        function = VERIFIER_REGISTRY.get(verifier)
        if function is None:
            return SOFT
    elif callable(verifier):
        function = verifier
    else:
        return SOFT
    try:
        result = function(event, property, state)
    except (KeyError, TypeError, ValueError):
        return REJECT
    return result if isinstance(result, str) and result in {HARD, REJECT, SOFT} else SOFT


# Names used by earlier/adjacent evidence integrations.
classify_evidence = verify_evidence
verify_event_evidence = verify_evidence


def _trajectory_events(trajectory: Any) -> list[Mapping[str, Any]]:
    if isinstance(trajectory, Trajectory):
        return list(trajectory.events)
    if isinstance(trajectory, Mapping):
        events = trajectory.get("events", [])
        return list(events) if isinstance(events, list) else []
    if isinstance(trajectory, Iterable) and not isinstance(trajectory, (str, bytes)):
        return list(trajectory)
    return []


def _requirement_state(
    requirement: Mapping[str, Any], global_ref_state: Optional[Mapping[str, Any]]
) -> Mapping[str, Any]:
    for key in ("ref_state", "reference_state", "ref"):
        value = requirement.get(key)
        if isinstance(value, Mapping):
            return value
    return global_ref_state if isinstance(global_ref_state, Mapping) else {}


def find_evidence_chain(
    trajectory: Any,
    requirements: Sequence[Mapping[str, Any]],
    *,
    ref_state: Optional[Mapping[str, Any]] = None,
) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]]]:
    """Find an evidence chain satisfying every requested property.

    A requirement is satisfied only by a verifier returning hard ``accept``.
    Unknown verifiers and model assertions therefore remain soft and cannot
    enter the returned chain.  One event may establish more than one explicitly
    requested property; the chain result deduplicates such an event.
    """

    events = _trajectory_events(trajectory)
    chain: list[dict[str, Any]] = []
    chain_indices: set[int] = set()
    unmet: list[dict[str, Any]] = []
    for raw_requirement in requirements or []:
        if not isinstance(raw_requirement, Mapping):
            unmet.append(copy.deepcopy(raw_requirement))
            continue
        requirement = dict(raw_requirement)
        property_name = requirement.get("property")
        verifier_name = requirement.get("verifier")
        state = _requirement_state(requirement, ref_state)
        match_index: Optional[int] = None
        for index, event in enumerate(events):
            if verify_evidence(event, property_name, verifier_name, state) == HARD:
                match_index = index
                break
        if match_index is None:
            unmet.append(copy.deepcopy(requirement))
        elif match_index not in chain_indices:
            matched = events[match_index]
            if isinstance(matched, Mapping):
                chain.append(copy.deepcopy(dict(matched)))
            chain_indices.add(match_index)
    return not unmet, chain, unmet


def _trajectory_is_valid(trajectory: Any) -> tuple[bool, str]:
    if isinstance(trajectory, Trajectory):
        return trajectory.verify_chain()
    if isinstance(trajectory, Mapping):
        try:
            return Trajectory.from_dict(trajectory).verify_chain()
        except (TypeError, ValueError):
            return False, "invalid_trajectory"
    return True, "ok"


def evaluate_submission(
    trajectory: Any,
    requirements: Sequence[Mapping[str, Any]],
    *,
    ref_state: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Apply the fail-closed evidence-gated submission contract."""

    valid, reason = _trajectory_is_valid(trajectory)
    if valid:
        found, chain, unmet = find_evidence_chain(
            trajectory, requirements, ref_state=ref_state
        )
    else:
        found, chain, unmet = False, [], [copy.deepcopy(r) for r in (requirements or [])]
        unmet.insert(
            0,
            {
                "property": "trajectory_integrity",
                "verifier": "trajectory_chain",
                "reason": reason,
            },
        )
    accepted = bool(valid and found)
    return {
        "accepted": accepted,
        "decision": "accepted_with_evidence" if accepted else "rejected_missing_evidence",
        "evidence_chain": chain if accepted else [],
        "unmet_requirements": [] if accepted else unmet,
    }


# ── Deterministic monitors and compositional gates ───────────────────────────


def _approved_payload(payload: Mapping[str, Any]) -> bool:
    approved_by = payload.get("approved_by")
    if not isinstance(approved_by, str) or not approved_by.strip():
        return False
    return payload.get("approved", True) is not False


def monitor_no_shell_exec_without_prior_human_approval(
    trajectory: Any, ref_state: Optional[Mapping[str, Any]] = None
) -> tuple[bool, str]:
    """Require a preceding approved ``human_approval`` before every shell exec."""

    approved = False
    for event in _trajectory_events(trajectory):
        if not isinstance(event, Mapping):
            return False, "invalid_event"
        if event.get("kind") == "human_approval":
            payload = _event_payload(event)
            if payload is not None and _approved_payload(payload):
                approved = True
        elif event.get("kind") == "shell_exec" and not approved:
            return False, "shell_exec_without_prior_human_approval"
    return True, "ok"


def _path_is_within(path: str, allowed: str) -> bool:
    try:
        candidate = os.path.abspath(os.path.normpath(path))
        root = os.path.abspath(os.path.normpath(allowed))
        return os.path.commonpath([candidate, root]) == root
    except (TypeError, ValueError):
        return False


def monitor_no_file_write_outside_allowed_paths(
    trajectory: Any, ref_state: Optional[Mapping[str, Any]] = None
) -> tuple[bool, str]:
    """Reject every ``file_write`` whose path is outside ``allowed_paths``."""

    state = ref_state if isinstance(ref_state, Mapping) else {}
    allowed_raw = state.get("allowed_paths")
    if isinstance(allowed_raw, str):
        allowed_paths = [allowed_raw]
    elif isinstance(allowed_raw, Mapping):
        allowed_paths = allowed_raw.get("paths", [])
    elif isinstance(allowed_raw, Iterable):
        allowed_paths = list(allowed_raw)
    else:
        allowed_paths = []

    for event in _trajectory_events(trajectory):
        if not isinstance(event, Mapping):
            return False, "invalid_event"
        if event.get("kind") != "file_write":
            continue
        payload = _event_payload(event)
        path = payload.get("path") or payload.get("file_path") if payload is not None else None
        if not isinstance(path, str) or not path:
            return False, "file_write_path_missing"
        if not allowed_paths:
            return False, "allowed_paths_missing"
        if not any(isinstance(root, str) and _path_is_within(path, root) for root in allowed_paths):
            return False, "file_write_outside_allowed_paths"
    return True, "ok"


MONITOR_REGISTRY: dict[str, Callable[[Any, Optional[Mapping[str, Any]]], tuple[bool, str]]] = {
    "no_shell_exec_without_prior_human_approval": monitor_no_shell_exec_without_prior_human_approval,
    "no_shell_exec_without_approval": monitor_no_shell_exec_without_prior_human_approval,
    "no_file_write_outside_allowed_paths": monitor_no_file_write_outside_allowed_paths,
}
MONITORS = MONITOR_REGISTRY


def _normalise_monitor_name(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(" ", "_")


def _resolve_monitor(spec: Any) -> tuple[str, Callable[..., Any], Mapping[str, Any]]:
    state: Mapping[str, Any] = {}
    candidate = spec
    if isinstance(spec, Mapping):
        state_value = spec.get("ref_state", spec.get("reference_state", spec.get("ref", {})))
        if isinstance(state_value, Mapping):
            state = state_value
        candidate = spec.get("predicate", spec.get("monitor", spec.get("name")))
    if isinstance(candidate, str):
        name = _normalise_monitor_name(candidate)
        function = MONITOR_REGISTRY.get(name)
        if function is None:
            raise KeyError(name)
        return name, function, state
    if callable(candidate):
        return getattr(candidate, "__name__", "custom_monitor"), candidate, state
    raise KeyError("missing_monitor")


class EvidenceGate:
    """Small callable wrapper for one requirement set."""

    def __init__(self, requirements: Sequence[Mapping[str, Any]]):
        self.requirements = list(requirements or [])

    def evaluate(self, trajectory: Any, *, ref_state: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return evaluate_submission(trajectory, self.requirements, ref_state=ref_state)

    def __call__(self, trajectory: Any, *, ref_state: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return self.evaluate(trajectory, ref_state=ref_state)


def _looks_like_pair(value: Any) -> bool:
    return isinstance(value, (tuple, list)) and len(value) == 2


def _requirements_for_gate(gate: Any) -> Optional[list[Mapping[str, Any]]]:
    if isinstance(gate, EvidenceGate):
        return list(gate.requirements)
    if isinstance(gate, Mapping):
        if "requirements" in gate and isinstance(gate["requirements"], Sequence):
            return list(gate["requirements"])
        if "property" in gate or "verifier" in gate:
            return [gate]
    if isinstance(gate, Sequence) and not isinstance(gate, (str, bytes)):
        if all(isinstance(item, Mapping) for item in gate):
            return list(gate)
    return None


class ComposedGate(list):
    """Compose deterministic monitors with disjoint evidence gates.

    The preferred form is ``ComposedGate(monitors=[...], gates=[...])``.  For
    compact declarative use, ``ComposedGate([(monitor_spec, gate), ...])`` and
    ``ComposedGate(monitors, gates)`` are also accepted.  Each monitor returns a
    boolean/reason pair and each evidence gate is an ordinary requirement list
    or :class:`EvidenceGate`.
    """

    def __init__(self, *args: Any, monitors: Any = None, gates: Any = None):
        if len(args) > 2:
            raise TypeError("ComposedGate accepts at most monitors and gates")
        if len(args) == 2:
            if monitors is not None or gates is not None:
                raise TypeError("do not mix positional and keyword monitor/gate lists")
            monitors, gates = args
        elif len(args) == 1:
            if monitors is not None or gates is not None:
                raise TypeError("do not mix positional and keyword monitor/gate lists")
            candidate = args[0]
            if _looks_like_pair(candidate):
                candidate = [candidate]
            if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)) and candidate and all(_looks_like_pair(item) for item in candidate):
                pairs = list(candidate)
                monitors = [pair[0] for pair in pairs]
                gates = [pair[1] for pair in pairs]
            else:
                monitors = candidate
                gates = []
        self.monitors = list(monitors or [])
        self.gates = list(gates or [])
        super().__init__([(monitor, None) for monitor in self.monitors] + [(None, gate) for gate in self.gates])

    def add_monitor(self, monitor_spec: Any) -> "ComposedGate":
        self.monitors.append(monitor_spec)
        self.append((monitor_spec, None))
        return self

    def add_gate(self, gate: Any) -> "ComposedGate":
        self.gates.append(gate)
        self.append((None, gate))
        return self

    def _requirements_are_disjoint(self) -> bool:
        seen: set[tuple[Any, Any]] = set()
        for gate in self.gates:
            requirements = _requirements_for_gate(gate)
            if requirements is None:
                continue
            for requirement in requirements:
                if not isinstance(requirement, Mapping):
                    continue
                key = (requirement.get("property"), requirement.get("verifier"))
                if key in seen:
                    return False
                seen.add(key)
        return True

    @staticmethod
    def _monitor_result(result: Any) -> tuple[bool, str]:
        if isinstance(result, tuple) and len(result) >= 2:
            return bool(result[0]), str(result[1])
        if isinstance(result, Mapping):
            return bool(result.get("held", result.get("ok", False))), str(result.get("reason", "monitor_violated"))
        return (bool(result), "ok" if result else "monitor_violated")

    def _evaluate_gate(
        self, gate: Any, trajectory: Any, ref_state: Optional[Mapping[str, Any]]
    ) -> dict[str, Any]:
        requirements = _requirements_for_gate(gate)
        if requirements is not None:
            return evaluate_submission(trajectory, requirements, ref_state=ref_state)
        try:
            if hasattr(gate, "evaluate") and callable(gate.evaluate):
                result = gate.evaluate(trajectory, ref_state=ref_state)
            elif callable(gate):
                result = gate(trajectory)
            else:
                result = False
        except Exception as exc:  # custom gates fail closed
            return {
                "accepted": False,
                "decision": "rejected_missing_evidence",
                "evidence_chain": [],
                "unmet_requirements": [{"reason": "gate_error", "detail": str(exc)}],
            }
        if isinstance(result, Mapping):
            report = dict(result)
            report.setdefault("accepted", False)
            report.setdefault("evidence_chain", [])
            report.setdefault("unmet_requirements", [])
            return report
        accepted = bool(result)
        return {
            "accepted": accepted,
            "decision": "accepted_with_evidence" if accepted else "rejected_missing_evidence",
            "evidence_chain": [],
            "unmet_requirements": [] if accepted else [{"reason": "gate_rejected"}],
        }

    def evaluate(
        self, trajectory: Any, *, ref_state: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        """Evaluate all monitors and all evidence gates fail-closed."""

        monitor_reports: list[dict[str, Any]] = []
        all_monitors_hold = True
        for spec in self.monitors:
            try:
                name, function, monitor_state = _resolve_monitor(spec)
                if isinstance(ref_state, Mapping):
                    merged_state = dict(ref_state)
                    merged_state.update(monitor_state)
                else:
                    merged_state = monitor_state
                result = function(trajectory, merged_state)
                held, reason = self._monitor_result(result)
            except Exception as exc:
                name = getattr(spec, "__name__", "unknown_monitor")
                held, reason = False, "monitor_error"
            monitor_reports.append({"monitor": name, "held": held, "reason": reason})
            all_monitors_hold = all_monitors_hold and held

        gate_reports = [self._evaluate_gate(gate, trajectory, ref_state) for gate in self.gates]
        all_gates_hold = all(bool(report.get("accepted")) for report in gate_reports)
        disjoint = self._requirements_are_disjoint()
        accepted = bool(all_monitors_hold and all_gates_hold and disjoint)

        evidence_chain: list[Any] = []
        unmet: list[Any] = []
        for report in gate_reports:
            evidence_chain.extend(report.get("evidence_chain", []))
            unmet.extend(report.get("unmet_requirements", []))
        if not disjoint:
            unmet.insert(0, {"reason": "overlapping_requirements"})
        return {
            "accepted": accepted,
            "decision": "accepted_with_evidence" if accepted else "rejected_composition",
            "monitors": monitor_reports,
            "gates": gate_reports,
            "evidence_chain": evidence_chain if accepted else evidence_chain,
            "unmet_requirements": unmet,
        }

    def verify(self, trajectory: Any, *, ref_state: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return self.evaluate(trajectory, ref_state=ref_state)

    def check(self, trajectory: Any, *, ref_state: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return self.evaluate(trajectory, ref_state=ref_state)

    def accepts(self, trajectory: Any, *, ref_state: Optional[Mapping[str, Any]] = None) -> bool:
        return bool(self.evaluate(trajectory, ref_state=ref_state)["accepted"])

    def __call__(self, trajectory: Any, *, ref_state: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return self.evaluate(trajectory, ref_state=ref_state)


# Friendly function aliases for monitor names used in prose and integrations.
no_shell_exec_without_prior_human_approval = monitor_no_shell_exec_without_prior_human_approval
no_file_write_outside_allowed_paths = monitor_no_file_write_outside_allowed_paths


def trajectory_root_hash(trajectory: Any) -> str:
    """Return ``sha256(head_hash)`` as a hexadecimal string.

    The returned value is suitable for adding to an existing prebind or receipt
    ``evidence_hashes`` list.  This helper does not alter those existing
    signature schemas, preserving additive AAR integration.
    """

    if isinstance(trajectory, Trajectory):
        head = trajectory.head_hash
    elif isinstance(trajectory, Mapping):
        try:
            head = Trajectory.from_dict(trajectory).head_hash
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid trajectory") from exc
    else:
        head = Trajectory(trajectory).head_hash
    if not _is_sha256(head):
        raise ValueError("trajectory head_hash is invalid")
    return hashlib.sha256(head.encode("ascii")).hexdigest()


__all__ = [
    "ALLOWED_EVENT_KINDS",
    "ComposedGate",
    "EVIDENCE_VERIFIERS",
    "EVENT_KINDS",
    "GENESIS",
    "GENESIS_HASH",
    "HARD",
    "MONITORS",
    "MONITOR_REGISTRY",
    "REJECT",
    "SOFT",
    "TRAJECTORY_SCHEMA",
    "TRAJECTORY_VERSION",
    "VERIFIERS",
    "VERIFIER_REGISTRY",
    "EvidenceGate",
    "Trajectory",
    "canonical_json",
    "classify_evidence",
    "evaluate_submission",
    "event_hash",
    "find_evidence_chain",
    "monitor_no_file_write_outside_allowed_paths",
    "monitor_no_shell_exec_without_prior_human_approval",
    "no_file_write_outside_allowed_paths",
    "no_shell_exec_without_prior_human_approval",
    "trajectory_root_hash",
    "verify_evidence",
    "verify_event_evidence",
]
