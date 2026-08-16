"""Deterministic agent-run behavior diff gate (#238).

Compile an agent-run transcript into a byte-stable canonical snapshot, diff
it against a pinned baseline, and gate on the result with an exit-code
taxonomy:

    2 — regression (removed/changed behavior, with --fail-on-regression)
    1 — integrity mismatch (pinned digest mismatch, oversized input, parse
        failure — no partial --out write)
    0 — clean

Canonicalization is key-order / whitespace / CRLF invariant: JSON input is
parsed and re-serialized with sorted keys and minimal separators, so two
differently-formatted transcripts of the same run produce identical
snapshots and identical sha256 digests. The digest covers the canonical
snapshot bytes — never the raw file. Fully offline; input is bounded with an
explicit error.

Borrows the pattern proven by LatticeAG/viscompile (verified locally
2026-08-15): byte-stable canonicalization, conservative classification
(removed/changed = regression; baseline error -> final output =
improvement), and digest-of-canonical-bytes semantics.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

SNAPSHOT_SCHEMA = "perseus-ledger-behavior-snapshot/v1"
DIFF_REPORT_SCHEMA = "perseus-ledger-behavior-diff/v1"
MAX_INPUT_BYTES = 16 * 1024 * 1024  # bounded input cap, explicit error past this

EXIT_CLEAN = 0
EXIT_INTEGRITY = 1
EXIT_REGRESSION = 2

DIGEST_PREFIX = "sha256:"

_VERDICTS = ("clean", "regression", "improvement_only")
_ERROR_MARKERS = ("error", "failure", "failed")


class IntegrityError(ValueError):
    """A pinned-digest mismatch, oversized input, or unparseable transcript.

    Maps to exit code 1 — distinct from a behavior regression (exit 2).
    """


def _bounded_read(source: str) -> str:
    """Read a transcript path with the bounded-input cap (#238)."""
    size = os.path.getsize(source)
    if size > MAX_INPUT_BYTES:
        raise IntegrityError(
            f"transcript exceeds the {MAX_INPUT_BYTES}-byte input cap: "
            f"{source} is {size} bytes"
        )
    with open(source, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _parse_transcript(text: str) -> Any:
    """Parse a transcript as JSON, JSONL, or plain text.

    JSON and JSONL parsing makes the snapshot key-order / whitespace /
    CRLF invariant by construction. Plain text normalizes CRLF and strips
    trailing per-line whitespace — deterministic but less structural than
    parsed JSON, and reported as such in the snapshot kind.
    """
    stripped = text.lstrip("\ufeff \t\r\n")
    try:
        return ("json", json.loads(stripped))
    except ValueError:
        pass
    lines = [line for line in stripped.splitlines() if line.strip()]
    if not lines:
        return ("text", "")
    # JSONL: every non-empty line parses as JSON.
    try:
        parsed = [json.loads(line) for line in lines]
        if parsed:
            return ("jsonl", parsed)
    except ValueError:
        pass
    # Plain text: CRLF/whitespace normalization only.
    normalized = "\n".join(line.rstrip().replace("\r", "") for line in lines)
    return ("text", normalized)


def compile_snapshot(source: Any, *, source_ref: Optional[str] = None,
                     max_bytes: int = MAX_INPUT_BYTES) -> dict[str, Any]:
    """Compile a transcript into a canonical snapshot with a sha256 digest.

    ``source`` is a file path, a transcript string, or an already-parsed
    JSON object/list. The digest covers the canonical snapshot bytes
    (never the raw file). ``source_ref`` is an optional caller label kept
    for traceability only — it never changes the digest.
    """
    if isinstance(source, (dict, list)):
        kind, parsed = "json", source
        raw_bytes = len(json.dumps(source, ensure_ascii=False))
    elif isinstance(source, (str, os.PathLike)):
        path = str(source)
        if os.path.exists(path):
            text = _bounded_read(path)
            raw_bytes = len(text.encode("utf-8"))
            kind, parsed = _parse_transcript(text)
        elif "\n" in path or path.lstrip().startswith(("{", "[")):
            # Inline transcript content rather than a path.
            raw_bytes = len(path.encode("utf-8"))
            kind, parsed = _parse_transcript(path)
        else:
            raise IntegrityError(f"transcript not found: {path}")
    else:
        raise IntegrityError(
            "source must be a path, a transcript string, or parsed JSON"
        )
    if raw_bytes > max_bytes:
        raise IntegrityError(
            f"transcript exceeds the {max_bytes}-byte input cap ({raw_bytes} bytes)"
        )
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False) if kind in ("json", "jsonl") \
        else (parsed if isinstance(parsed, str) else json.dumps(parsed, sort_keys=True))
    canonical_bytes = canonical.encode("utf-8") if isinstance(canonical, str) \
        else _canonical_json_bytes(parsed)
    digest = hashlib.sha256(canonical_bytes).hexdigest()
    return {
        "schema": SNAPSHOT_SCHEMA,
        "kind": kind,
        "digest": digest,
        "canonical": canonical,
        "source_ref": source_ref,
    }


def parse_pinned_digest(pinned: str) -> str:
    """Accept ``sha256:<hex>`` or a bare 64-hex digest; return lowercase hex."""
    value = pinned.strip()
    if value.startswith(DIGEST_PREFIX):
        value = value[len(DIGEST_PREFIX):]
    if len(value) != 64 or not all(c in "0123456789abcdef" for c in value.lower()):
        raise IntegrityError(
            f"pinned digest must be sha256:<64-hex>, got: {pinned!r}"
        )
    return value.lower()


def check_pinned_digest(snapshot: dict[str, Any], pinned: Optional[str],
                        role: str) -> None:
    """Raise IntegrityError when the pinned digest does not match (#238)."""
    if pinned is None:
        return
    expected = parse_pinned_digest(pinned)
    if snapshot["digest"] != expected:
        raise IntegrityError(
            f"{role} digest mismatch: pinned {expected}, snapshot is "
            f"{snapshot['digest']}"
        )


def _case_map(parsed: Any, kind: str) -> Optional[dict[str, Any]]:
    """Extract a stable case-id -> case map from structured transcripts."""
    if kind not in ("json", "jsonl") or not isinstance(parsed, (dict, list)):
        return None
    root = parsed
    if isinstance(root, dict):
        cases = root.get("cases", root.get("events"))
    else:
        cases = root
    if not isinstance(cases, list):
        return None
    mapping: dict[str, Any] = {}
    for item in cases:
        if isinstance(item, dict) and isinstance(item.get("id"), str) \
                and item["id"].strip():
            mapping[item["id"]] = item
    return mapping or None


def _is_error_case(case: Any) -> bool:
    if not isinstance(case, dict):
        return False
    if any(marker in str(case.get("type", "")).lower() for marker in _ERROR_MARKERS):
        return True
    if isinstance(case.get("error"), (str, dict, list)) and case["error"]:
        return True
    if case.get("status") == "error" or case.get("outcome") in ("failed", "error"):
        return True
    events = case.get("events")
    if isinstance(events, list):
        for e in events:
            if not isinstance(e, dict):
                continue
            if any(marker in str(e.get("type", "")).lower() for marker in _ERROR_MARKERS):
                return True
            if isinstance(e.get("error"), (str, dict, list)) and e["error"]:
                return True
    return False


def _has_final_output(case: Any) -> bool:
    if not isinstance(case, dict):
        return False
    for key in ("output", "final", "final_output", "result"):
        if case.get(key) not in (None, ""):
            return True
    events = case.get("events")
    if isinstance(events, list):
        return any(
            isinstance(e, dict) and e.get("type") == "final" for e in events
        )
    return False


def diff_snapshots(baseline: dict[str, Any],
                   target: dict[str, Any]) -> dict[str, Any]:
    """Conservative diff of two compiled snapshots (#238).

    Classification rules (conservative by construction):
    - removed or changed cases  -> regression
    - added cases               -> addition (not a regression)
    - baseline errored and the target produced final output -> improvement
    - identical canonical bytes -> clean

    Text-kind transcripts are compared line-wise: removed/changed lines are
    regressions, added lines are additions.
    """
    if baseline["kind"] in ("json", "jsonl") and target["kind"] in ("json", "jsonl"):
        base_map = _case_map(json.loads(baseline["canonical"]), baseline["kind"])
        tgt_map = _case_map(json.loads(target["canonical"]), target["kind"])
        if base_map is not None and tgt_map is not None:
            return _diff_case_maps(base_map, tgt_map)
    if baseline["kind"] == "text" and target["kind"] == "text":
        return _diff_text(
            baseline["canonical"].splitlines(), target["canonical"].splitlines()
        )
    # Structured-but-opaque transcripts: any change is a regression unless
    # the baseline is an error shape and the target carries an output.
    if baseline["canonical"] == target["canonical"]:
        return _report([], [], 0, [], False)
    return _diff_whole_object(
        json.loads(baseline["canonical"]) if baseline["kind"] != "text" else baseline["canonical"],
        json.loads(target["canonical"]) if target["kind"] != "text" else target["canonical"],
    )


def _diff_case_maps(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    regressions: list[dict[str, Any]] = []
    improvements: list[str] = []
    additions: list[str] = []
    base_ids = set(base)
    tgt_ids = set(target)
    for case_id in sorted(base_ids - tgt_ids):
        regressions.append({"case": case_id, "change": "removed"})
    for case_id in sorted(base_ids & tgt_ids):
        if json.dumps(base[case_id], sort_keys=True, separators=(",", ":")) \
                == json.dumps(target[case_id], sort_keys=True, separators=(",", ":")):
            continue
        if _is_error_case(base[case_id]) and _has_final_output(target[case_id]):
            improvements.append(case_id)
            continue
        regressions.append({"case": case_id, "change": "changed"})
    additions = sorted(tgt_ids - base_ids)
    unchanged = sum(
        1 for cid in (base_ids & tgt_ids)
        if json.dumps(base[cid], sort_keys=True, separators=(",", ":"))
        == json.dumps(target[cid], sort_keys=True, separators=(",", ":"))
    )
    return _report(regressions, additions, unchanged,
                   improvements, bool(improvements) and not regressions)


def _diff_text(base_lines: list[str], target_lines: list[str]) -> dict[str, Any]:
    regressions: list[dict[str, Any]] = []
    additions: list[str] = []
    base_map = {}
    for i, line in enumerate(base_lines):
        base_map.setdefault(line, []).append(i)
    consumed = {line: 0 for line in base_map}
    target_counts: dict[str, int] = {}
    for line in target_lines:
        target_counts[line] = target_counts.get(line, 0) + 1
    base_counts = {line: len(idxs) for line, idxs in base_map.items()}
    for line, tcount in target_counts.items():
        bcount = base_counts.get(line, 0)
        if tcount > bcount:
            additions.extend([line] * (tcount - bcount))
    for line, bcount in base_counts.items():
        tcount = target_counts.get(line, 0)
        if tcount < bcount:
            regressions.append({"line": line, "change": "removed",
                                "count": bcount - tcount})
    unchanged = sum(min(base_counts.get(line, 0), target_counts.get(line, 0))
                    for line in set(base_counts) | set(target_counts))
    return _report(regressions, additions, unchanged, [], False)


def _diff_whole_object(base: Any, target: Any) -> dict[str, Any]:
    base_err = _is_error_case(base) or (
        isinstance(base, dict) and base.get("error") not in (None, "")
    )
    improved = base_err and _has_final_output(target)
    if improved:
        return _report([], [], 0, ["(whole transcript)"], True)
    return _report([{"change": "changed", "scope": "(whole transcript)"}],
                   [], 0, 0, [], False)


def _report(regressions, additions, unchanged, improved_ids,
            improvement_only: bool) -> dict[str, Any]:
    verdict = "regression" if regressions else (
        "improvement_only" if improvement_only else "clean"
    )
    return {
        "schema": DIFF_REPORT_SCHEMA,
        "verdict": verdict,
        "regressions": regressions,
        "additions": additions,
        "improvements": improved_ids,
        "unchanged_cases": unchanged,
    }


def diff_sources(baseline_source: Any, target_source: Any, *,
                 require_baseline_digest: Optional[str] = None,
                 require_target_digest: Optional[str] = None,
                 baseline_ref: Optional[str] = None,
                 target_ref: Optional[str] = None) -> dict[str, Any]:
    """Compile + digest-check + diff two transcripts in one pass (#238).

    Raises IntegrityError (exit 1) on digest mismatch, oversized input, or
    parse failure. The returned report carries both snapshots' digests so a
    clean diff is receipt-anchorable.
    """
    baseline = compile_snapshot(baseline_source, source_ref=baseline_ref)
    target = compile_snapshot(target_source, source_ref=target_ref)
    check_pinned_digest(baseline, require_baseline_digest, "baseline")
    check_pinned_digest(target, require_target_digest, "target")
    report = diff_snapshots(baseline, target)
    report["baseline_digest"] = baseline["digest"]
    report["target_digest"] = target["digest"]
    report["baseline_kind"] = baseline["kind"]
    report["target_kind"] = target["kind"]
    return report


__all__ = [
    "SNAPSHOT_SCHEMA", "DIFF_REPORT_SCHEMA", "MAX_INPUT_BYTES",
    "EXIT_CLEAN", "EXIT_INTEGRITY", "EXIT_REGRESSION", "DIGEST_PREFIX",
    "IntegrityError",
    "compile_snapshot", "parse_pinned_digest", "check_pinned_digest",
    "diff_snapshots", "diff_sources",
]
