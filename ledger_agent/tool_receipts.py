"""Signed tool-execution receipts and Nyaya (pramana) claim verification.

This module implements the additive tool-receipt bridge described by
arXiv:2603.10060 (NabaOS).  A receipt commits to a tool call and its output,
while the in-memory ledger lets a verifier cross-check the claims an agent
makes about that call.

The paper's illustrative signature covers ``id|tool_name|input_hash|
output_hash|timestamp_ms``.  This implementation deliberately extends that
payload with ``result_count`` and canonical ``facts``: those fields are the
cross-check ground truth, so leaving them unsigned would make count and fact
mismatch detection meaningless.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Mapping
from typing import Any, Optional

from .evidence_levels import resolve_key

TOOL_RECEIPT_SCHEMA = "perseus-ledger-tool-receipt/v1"

PRAMANA_TYPES = (
    "pratyaksha",
    "anumana",
    "upamana",
    "shabda",
    "abhava",
    "ungrounded",
)

HALLUCINATION_TYPES = (
    "fabricated_call",
    "count_mismatch",
    "fact_mismatch",
    "inference_as_fact",
    "false_absence",
    "source_fabrication",
)

_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "id",
        "tool_name",
        "input_hash",
        "output_hash",
        "result_count",
        "facts",
        "timestamp_ms",
        "duration_ms",
        "key_id",
        "signature",
    }
)


# ---------------------------------------------------------------------------
# Canonical hashing and receipt construction


def _canonical_json(value: Any) -> str:
    """Return the package's stable, compact JSON representation."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _output_bytes(raw_output: Any) -> bytes:
    if isinstance(raw_output, str):
        return raw_output.encode("utf-8")
    if isinstance(raw_output, (bytes, bytearray)):
        return bytes(raw_output)
    raise TypeError("raw_output must be str or bytes")


def _nonnegative_int(value: Any, field: str) -> None:
    if type(value) is not int or value < 0:  # bool is intentionally not an int here.
        raise ValueError(f"{field} must be a non-negative integer")


def _nonempty_string(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")


def _signature_payload(receipt: Mapping[str, Any]) -> bytes:
    """Build the pipe-delimited, non-JSON signature payload.

    ``result_count`` and ``facts`` are the deliberate extension to the paper's
    field list.  The facts object is canonicalized before it is inserted into
    the pipe-delimited payload; no separator is added around that JSON value.
    """
    return "|".join(
        (
            str(receipt["id"]),
            str(receipt["tool_name"]),
            str(receipt["input_hash"]),
            str(receipt["output_hash"]),
            str(receipt["result_count"]),
            _canonical_json(receipt["facts"]),
            str(receipt["timestamp_ms"]),
        )
    ).encode("utf-8")


def _sign_tool_receipt(receipt: Mapping[str, Any], key: bytes) -> str:
    return hmac.new(key, _signature_payload(receipt), hashlib.sha256).hexdigest()


def build_tool_receipt(
    *,
    tool_name: str,
    input_params: Any,
    raw_output: Any,
    result_count: int,
    facts: dict[str, Any],
    duration_ms: int,
    key_id: str,
    key: bytes,
    timestamp_ms: Optional[int] = None,
    id: Optional[str] = None,
) -> dict[str, Any]:
    """Build and sign one tool-execution receipt.

    ``input_params`` and ``raw_output`` are intentionally committed as hashes;
    the receipt does not disclose the potentially sensitive preimages.  The
    signature is HMAC-SHA256 over ``id|tool_name|input_hash|output_hash|
    result_count|canonical(facts)|timestamp_ms``.  Including the last two
    ground-truth fields is a deliberate extension of the paper's example
    payload so a verifier can trust count and fact cross-checks.
    """
    _nonempty_string(tool_name, "tool_name")
    _nonempty_string(key_id, "key_id")
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise ValueError("key must be non-empty bytes")
    if not isinstance(facts, dict):
        raise TypeError("facts must be a dict")
    _nonnegative_int(result_count, "result_count")
    _nonnegative_int(duration_ms, "duration_ms")

    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    _nonnegative_int(timestamp_ms, "timestamp_ms")

    if id is None:
        id = uuid.uuid4().hex
    _nonempty_string(id, "id")

    # Force JSON serialization now so a malformed input/facts value cannot
    # produce a receipt whose commitment cannot be reproduced later.
    input_hash = _sha256_json(input_params)
    output_hash = _sha256_bytes(_output_bytes(raw_output))
    facts_copy = copy.deepcopy(facts)
    _canonical_json(facts_copy)

    receipt: dict[str, Any] = {
        "schema": TOOL_RECEIPT_SCHEMA,
        "id": id,
        "tool_name": tool_name,
        "input_hash": input_hash,
        "output_hash": output_hash,
        "result_count": result_count,
        "facts": facts_copy,
        "timestamp_ms": timestamp_ms,
        "duration_ms": duration_ms,
        "key_id": key_id,
    }
    receipt["signature"] = _sign_tool_receipt(receipt, bytes(key))
    return receipt


# ---------------------------------------------------------------------------
# Receipt validation and session ledger


def _is_sha256_hex(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def verify_tool_receipt(
    receipt: Mapping[str, Any],
    key_registry: Optional[Mapping[str, Any]] = None,
) -> tuple[bool, list[str]]:
    """Validate receipt shape and its HMAC under the declared registry key.

    A receipt contains commitments rather than the raw input/output, so a
    standalone verifier can validate their shape and the signature binding;
    the preimages remain with the tool adapter.  If a caller retains the
    preimages, it can independently recompute the two hashes before passing
    the receipt to this function.  This preserves the paper's privacy
    property while making every exposed field cryptographically tamper-evident.
    """
    errors: list[str] = []

    def add(code: str) -> None:
        if code not in errors:
            errors.append(code)

    if not isinstance(receipt, Mapping):
        return False, ["not_object"]

    unknown = set(receipt) - _RECEIPT_FIELDS
    if unknown:
        add("unknown_field")

    if receipt.get("schema") != TOOL_RECEIPT_SCHEMA:
        add("schema")
    if not isinstance(receipt.get("id"), str) or not receipt.get("id"):
        add("id")
    if not isinstance(receipt.get("tool_name"), str) or not receipt.get("tool_name"):
        add("tool_name")
    for field in ("input_hash", "output_hash"):
        if not _is_sha256_hex(receipt.get(field)):
            add(field)
    if type(receipt.get("result_count")) is not int or receipt.get("result_count", -1) < 0:
        add("result_count")
    if not isinstance(receipt.get("facts"), dict):
        add("facts")
    for field in ("timestamp_ms", "duration_ms"):
        if type(receipt.get(field)) is not int or receipt.get(field, -1) < 0:
            add(field)
    if not isinstance(receipt.get("key_id"), str) or not receipt.get("key_id"):
        add("key_id")
    if not _is_sha256_hex(receipt.get("signature")):
        add("signature")

    key_id = receipt.get("key_id")
    key = None
    if isinstance(key_id, str) and key_id:
        # resolve_key is the canonical key-registry adapter for this package.
        # There is intentionally no fallback key argument in this API.
        key = resolve_key(key_registry, key_id, None)
        if key is None:
            add("unknown_key")

    if key is not None and _is_sha256_hex(receipt.get("signature")):
        try:
            expected = _sign_tool_receipt(receipt, key)
        except (KeyError, TypeError, ValueError):
            expected = None
        if expected is None or not hmac.compare_digest(
            expected, str(receipt["signature"]).lower()
        ):
            add("signature_invalid")

    return not errors, errors


class ToolReceiptLedger:
    """An in-memory, insertion-ordered registry for one agent session."""

    def __init__(self, *, key_registry: Optional[Mapping[str, Any]] = None):
        self._receipts: dict[str, dict[str, Any]] = {}
        self._key_registry: dict[str, Any] = dict(key_registry or {})

    def issue(self, **receipt_kwargs: Any) -> dict[str, Any]:
        """Build, remember, and return exactly one new receipt."""
        receipt = build_tool_receipt(**receipt_kwargs)
        key_id = receipt["key_id"]
        if key_id not in self._key_registry:
            self._key_registry[key_id] = bytes(receipt_kwargs["key"])
        self.register(receipt)
        return copy.deepcopy(receipt)

    def register(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Register an already-built receipt, preserving session order.

        This is useful when a runtime persists receipts before constructing a
        verifier.  The caller supplies the corresponding key registry at
        ledger construction time.
        """
        if not isinstance(receipt, Mapping) or not isinstance(receipt.get("id"), str):
            raise ValueError("receipt must contain a string id")
        receipt_id = receipt["id"]
        if receipt_id in self._receipts:
            raise ValueError("duplicate_receipt_id")
        self._receipts[receipt_id] = copy.deepcopy(dict(receipt))
        return copy.deepcopy(self._receipts[receipt_id])

    # A small alias keeps adapter code readable without changing the required
    # issue/verify/get/all_ids/unreferenced interface.
    add = register

    def verify(self, receipt_id: str) -> tuple[bool, list[str]]:
        if receipt_id not in self._receipts:
            return False, ["unknown_receipt"]
        return verify_tool_receipt(self._receipts[receipt_id], self._key_registry)

    def get(self, receipt_id: str) -> Optional[dict[str, Any]]:
        receipt = self._receipts.get(receipt_id)
        return copy.deepcopy(receipt) if receipt is not None else None

    def all_ids(self) -> list[str]:
        return list(self._receipts)

    def unreferenced(self, referenced_ids: set[str]) -> list[str]:
        referenced = set(referenced_ids or set())
        return [receipt_id for receipt_id in self._receipts if receipt_id not in referenced]


# ---------------------------------------------------------------------------
# Pramana claim verification


def _verdict(
    claim: Mapping[str, Any],
    *,
    status: str,
    hallucination_type: Optional[str],
    trust_level: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "claim_text": claim.get("text") if isinstance(claim.get("text"), str) else "",
        "pramana": claim.get("pramana") if isinstance(claim.get("pramana"), str) else "",
        "status": status,
        "hallucination_type": hallucination_type,
        "trust_level": trust_level,
        "reason": reason,
        "receipt_id": claim.get("receipt_id") if isinstance(claim.get("receipt_id"), str) else None,
    }


def _missing_receipt(ledger: ToolReceiptLedger, receipt_id: Any) -> bool:
    return not isinstance(receipt_id, str) or not receipt_id or ledger.get(receipt_id) is None


def _receipt_or_flag(
    claim: Mapping[str, Any], ledger: ToolReceiptLedger
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    receipt_id = claim.get("receipt_id")
    if _missing_receipt(ledger, receipt_id):
        return None, _verdict(
            claim,
            status="flagged",
            hallucination_type="fabricated_call",
            trust_level="unreliable",
            reason="receipt_id does not exist in the session ledger",
        )
    ok, errors = ledger.verify(receipt_id)
    if not ok:
        return None, _verdict(
            claim,
            status="flagged",
            hallucination_type=None,
            trust_level="unreliable",
            reason="receipt verification failed: " + ",".join(errors),
        )
    return ledger.get(receipt_id), None


def _fact_present(facts: Mapping[str, Any], premise: Any) -> bool:
    if isinstance(premise, Mapping):
        return all(key in facts and facts[key] == value for key, value in premise.items())
    if isinstance(premise, str):
        return premise in facts or premise in facts.values()
    return False


def _looks_inferential(text: str) -> bool:
    lower = text.lower()
    markers = (
        " seems ",
        " appears ",
        " likely ",
        " probably ",
        " may be ",
        " might be ",
        " suggests ",
        " parece ",
        " semble ",
    )
    padded = f" {lower} "
    return any(marker in padded for marker in markers)


def _is_fetch_tool(tool_name: Any) -> bool:
    if not isinstance(tool_name, str):
        return False
    name = tool_name.lower()
    return name in {"fetch", "web_fetch", "http_fetch", "url_fetch", "browser_fetch"} or "fetch" in name


def _url_in_facts(facts: Mapping[str, Any], url: str) -> bool:
    if facts.get("source_url") == url:
        return True
    fetched = facts.get("fetched_urls")
    if isinstance(fetched, str):
        return fetched == url
    if isinstance(fetched, (list, tuple, set)):
        return url in fetched
    return False


def verify_claim(claim: Mapping[str, Any], ledger: ToolReceiptLedger) -> dict[str, Any]:
    """Cross-check one self-tagged claim against the session receipt ledger."""
    if not isinstance(claim, Mapping):
        return {
            "claim_text": "",
            "pramana": "",
            "status": "unverifiable",
            "hallucination_type": None,
            "trust_level": "ungrounded",
            "reason": "claim is not an object",
            "receipt_id": None,
        }

    pramana = claim.get("pramana")
    if not isinstance(pramana, str):
        return _verdict(
            claim,
            status="unverifiable",
            hallucination_type=None,
            trust_level="ungrounded",
            reason="missing pramana label",
        )
    pramana = pramana.lower()

    if pramana == "ungrounded":
        return _verdict(
            claim,
            status="unverifiable",
            hallucination_type=None,
            trust_level="ungrounded",
            reason="no tool or source evidence was declared",
        )
    if pramana not in PRAMANA_TYPES:
        return _verdict(
            claim,
            status="unverifiable",
            hallucination_type=None,
            trust_level="ungrounded",
            reason="unknown pramana label",
        )

    if pramana == "shabda":
        receipt_id = claim.get("receipt_id")
        if receipt_id is not None and _missing_receipt(ledger, receipt_id):
            return _verdict(
                claim,
                status="flagged",
                hallucination_type="fabricated_call",
                trust_level="unreliable",
                reason="receipt_id does not exist in the session ledger",
            )
        if receipt_id is not None:
            ok, errors = ledger.verify(receipt_id)
            if not ok:
                return _verdict(
                    claim,
                    status="flagged",
                    hallucination_type=None,
                    trust_level="unreliable",
                    reason="receipt verification failed: " + ",".join(errors),
                )
        cited_url = claim.get("cited_source_url")
        if isinstance(cited_url, str) and cited_url:
            for candidate_id in ledger.all_ids():
                candidate = ledger.get(candidate_id)
                if candidate is None or not _is_fetch_tool(candidate.get("tool_name")):
                    continue
                valid, _ = ledger.verify(candidate_id)
                if valid and _url_in_facts(candidate.get("facts", {}), cited_url):
                    return _verdict(
                        claim,
                        status="verified",
                        hallucination_type=None,
                        trust_level="mostly_verified",
                        reason="cited URL is present in a verified fetch receipt",
                    )
        return _verdict(
            claim,
            status="flagged",
            hallucination_type="source_fabrication",
            trust_level="unreliable",
            reason="no verified fetch receipt contains the cited source URL",
        )

    receipt, early = _receipt_or_flag(claim, ledger)
    if early is not None:
        return early
    assert receipt is not None

    if pramana == "pratyaksha":
        premise_facts = claim.get("premise_facts")
        if (isinstance(premise_facts, list) and premise_facts) or _looks_inferential(
            claim.get("text", "")
        ):
            return _verdict(
                claim,
                status="flagged",
                hallucination_type="inference_as_fact",
                trust_level="unreliable",
                reason="an inferential claim was labelled as direct tool output",
            )
        expected_count = claim.get("expected_count")
        if expected_count is not None and (
            type(expected_count) is not int or expected_count != receipt.get("result_count")
        ):
            return _verdict(
                claim,
                status="flagged",
                hallucination_type="count_mismatch",
                trust_level="unreliable",
                reason="expected_count differs from receipt result_count",
            )
        expected_facts = claim.get("expected_facts")
        if expected_facts is not None and (
            not isinstance(expected_facts, Mapping)
            or not all(
                key in receipt.get("facts", {}) and receipt["facts"][key] == value
                for key, value in expected_facts.items()
            )
        ):
            return _verdict(
                claim,
                status="flagged",
                hallucination_type="fact_mismatch",
                trust_level="unreliable",
                reason="expected_facts are not a subset of receipt facts",
            )
        return _verdict(
            claim,
            status="verified",
            hallucination_type=None,
            trust_level="fully_verified",
            reason="claim count and facts agree with a verified receipt",
        )

    if pramana == "anumana":
        premises = claim.get("premise_facts")
        if not isinstance(premises, list) or not premises:
            return _verdict(
                claim,
                status="flagged",
                hallucination_type="inference_as_fact",
                trust_level="unreliable",
                reason="inference has no declared premises in receipt facts",
            )
        facts = receipt.get("facts", {})
        if all(_fact_present(facts, premise) for premise in premises):
            return _verdict(
                claim,
                status="verified",
                hallucination_type=None,
                trust_level="mostly_verified",
                reason="all inference premises are present in receipt facts",
            )
        return _verdict(
            claim,
            status="flagged",
            hallucination_type="inference_as_fact",
            trust_level="unreliable",
            reason="one or more inference premises are absent from receipt facts",
        )

    if pramana == "upamana":
        subjects = claim.get("premise_facts")
        if not isinstance(subjects, list) or not subjects:
            subjects = list((claim.get("expected_facts") or {}).keys()) if isinstance(
                claim.get("expected_facts"), Mapping
            ) else []
        if subjects and all(_fact_present(receipt.get("facts", {}), subject) for subject in subjects):
            return _verdict(
                claim,
                status="verified",
                hallucination_type=None,
                trust_level="partial",
                reason="comparison subjects are present in receipt facts",
            )
        return _verdict(
            claim,
            status="unverifiable",
            hallucination_type=None,
            trust_level="ungrounded",
            reason="comparison subjects are not grounded in receipt facts",
        )

    # The only remaining pramana is abhava.
    if receipt.get("result_count") == 0:
        return _verdict(
            claim,
            status="verified",
            hallucination_type=None,
            trust_level="mostly_verified",
            reason="verified receipt records an empty result set",
        )
    return _verdict(
        claim,
        status="flagged",
        hallucination_type="false_absence",
        trust_level="unreliable",
        reason="receipt records a non-empty result set",
    )


def verify_response(
    claims: list[Mapping[str, Any]],
    ledger: ToolReceiptLedger,
    referenced_receipt_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Verify all claims and report status counts plus omitted receipts."""
    verdicts = [verify_claim(claim, ledger) for claim in claims]
    by_type = {kind: 0 for kind in HALLUCINATION_TYPES}
    for verdict in verdicts:
        kind = verdict.get("hallucination_type")
        if kind in by_type:
            by_type[kind] += 1

    if referenced_receipt_ids is None:
        referenced = {
            claim.get("receipt_id")
            for claim in claims
            if isinstance(claim, Mapping) and isinstance(claim.get("receipt_id"), str)
        }
    else:
        referenced = set(referenced_receipt_ids)

    summary = {
        "total": len(verdicts),
        "verified": sum(v["status"] == "verified" for v in verdicts),
        "flagged": sum(v["status"] == "flagged" for v in verdicts),
        "unverifiable": sum(v["status"] == "unverifiable" for v in verdicts),
        "by_type": by_type,
    }
    return {
        "claims": verdicts,
        "summary": summary,
        "omitted_receipts": ledger.unreferenced(referenced),
    }


# ---------------------------------------------------------------------------
# Markdown and evidence-hash bridges


def render_claim(verdict: Mapping[str, Any]) -> str:
    """Render one verdict as the single-line trust annotation used in replies."""
    return (
        f"— {verdict.get('claim_text', '')} "
        f"[{verdict.get('pramana', '')} · {verdict.get('trust_level', 'ungrounded')}]"
    )


def render_verification_block(result: Mapping[str, Any]) -> str:
    """Render a complete verification result as a compact Markdown block."""
    summary = result.get("summary", {})
    lines = ["### Verification", ""]
    claims = result.get("claims", [])
    lines.extend(render_claim(claim) for claim in claims)
    lines.extend(
        [
            "",
            (
                "**Summary:** "
                f"{summary.get('total', 0)} total · "
                f"{summary.get('verified', 0)} verified · "
                f"{summary.get('flagged', 0)} flagged · "
                f"{summary.get('unverifiable', 0)} unverifiable"
            ),
        ]
    )
    omitted = result.get("omitted_receipts", [])
    if omitted:
        lines.append("**Omitted receipts:** " + ", ".join(str(item) for item in omitted))
    return "\n".join(lines)


def receipt_to_evidence_hash(receipt: Mapping[str, Any]) -> str:
    """Hash the complete canonical receipt for ``evidence_hashes`` bridges."""
    return _sha256_json(dict(receipt))


# Kept as a lazy compatibility hook for callers that used the first draft of
# the benchmark API.  The executable harness owns the generator implementation
# so importing this core module never imports benchmark code.
def generate_benchmark_scenarios(seed: int = 251) -> list[dict[str, Any]]:
    from benchmark.nyaya_verify_bench import generate_benchmark_scenarios as _generate

    return _generate(seed=seed)


__all__ = [
    "TOOL_RECEIPT_SCHEMA",
    "PRAMANA_TYPES",
    "HALLUCINATION_TYPES",
    "build_tool_receipt",
    "verify_tool_receipt",
    "ToolReceiptLedger",
    "verify_claim",
    "verify_response",
    "render_claim",
    "render_verification_block",
    "receipt_to_evidence_hash",
]
