"""Custody disclosure labels for key registry entries (#241).

Borrows the 1f916-ai/protocol custody taxonomy (SPEC §3,
``identity.custody-disclosure``): custody is a DISCLOSURE, not a ladder — a
signature proves exactly what its disclosed tier permits it to prove. The
label is the mechanism: ``self_held`` and ``household_held`` keys are
different products, and a signature made under either must be priced
differently by anyone relying on a receipt.

Rule: missing or unknown custody is rendered as labeled uncertainty
(``"unknown"``) — never silently as the strongest case. Verifiers surface
custody alongside every signature-verification result.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

#: The 1f916 custody taxonomy, extensible: literal tiers plus parameterized
#: ``threshold(k,n)`` and ``kms(provider)`` forms.
CUSTODY_TIERS = (
    "self_held",
    "platform_held",
    "household_held",
    "threshold(k,n)",
    "kms",
    "hsm",
    "session_delegated",
)
CUSTODY_UNKNOWN = "unknown"

_THRESHOLD_RE = re.compile(r"^threshold\(\d+,\s*\d+\)$")
_KMS_RE = re.compile(r"^kms(\([^)]*\))?$")


def is_known_custody(value: Any) -> bool:
    """True when ``value`` is a recognized custody tier (extensible forms)."""
    if not isinstance(value, str) or not value.strip():
        return False
    v = value.strip()
    return (v in CUSTODY_TIERS
            or bool(_THRESHOLD_RE.fullmatch(v))
            or bool(_KMS_RE.fullmatch(v)))


def custody_label(value: Any) -> dict[str, Any]:
    """Label custody honestly; missing/unknown → labeled uncertainty."""
    if value is None:
        return {"custody": CUSTODY_UNKNOWN, "known": False}
    v = str(value).strip()
    if not v:
        return {"custody": CUSTODY_UNKNOWN, "known": False}
    return {"custody": v, "known": is_known_custody(v)}


def _copy_binding_metadata(source: Mapping[str, Any], target: dict[str, Any]) -> None:
    """Preserve optional CVA identity/revocation metadata during normalization."""
    for field in ("agent_id", "agent_binding", "revoked", "revoked_at", "status"):
        if field in source:
            target[field] = source[field]


def normalize_key_registry(registry: Optional[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Normalize a key registry into labeled entries.

    Accepts both legacy ``{key_id: bytes}`` registries (entries carry no
    custody — rendered as labeled uncertainty) and labeled
    ``{key_id: {key_material: bytes, custody: str, label: str}}`` entries.
    Optional ``agent_id``/``agent_binding`` and revocation fields are preserved
    for CVA principal binding; existing signature consumers continue to use
    only ``key_material``.
    """
    out: dict[str, dict[str, Any]] = {}
    for key_id, entry in (registry or {}).items():
        if isinstance(entry, (bytes, bytearray)):
            out[key_id] = {
                "key_material": bytes(entry),
                "custody": CUSTODY_UNKNOWN,
                "known": False,
                "label": None,
            }
        elif isinstance(entry, Mapping) and isinstance(
                entry.get("key_material"), (bytes, bytearray)):
            label = custody_label(entry.get("custody"))
            entry_label = entry.get("label")
            normalized = {
                "key_material": bytes(entry["key_material"]),
                "custody": label["custody"],
                "known": label["known"],
                "label": entry_label if isinstance(entry_label, str) else None,
            }
            _copy_binding_metadata(entry, normalized)
            out[key_id] = normalized
        else:
            raise ValueError(
                f"key_registry entry {key_id!r} must be bytes or "
                "{key_material: bytes, custody: str}"
            )
    return out


def custody_for_key(registry: Optional[Mapping[str, Any]],
                    key_id: str) -> dict[str, Any]:
    """The disclosed custody label for a registry key — never the key bytes."""
    entry = normalize_key_registry(registry).get(key_id)
    if entry is None:
        return {"custody": CUSTODY_UNKNOWN, "known": False}
    return {"custody": entry["custody"], "known": entry["known"]}


__all__ = [
    "CUSTODY_TIERS", "CUSTODY_UNKNOWN",
    "is_known_custody", "custody_label",
    "normalize_key_registry", "custody_for_key",
]
