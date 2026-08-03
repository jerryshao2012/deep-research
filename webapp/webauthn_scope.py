"""Shared WebAuthn relying-party scope normalization."""

from __future__ import annotations

import ipaddress

import idna
from publicsuffixlist import PublicSuffixList

_PUBLIC_SUFFIX_LIST = PublicSuffixList()
_ASCII_DECIMAL_DIGITS = frozenset("0123456789")


def normalize_dns_name(value: object, setting_name: str) -> str:
    """Return canonical IDNA ASCII DNS name or raise a bounded validation error."""
    if not isinstance(value, str):
        raise ValueError(f"{setting_name} contains an invalid RP ID")
    candidate = value.strip()
    if candidate.endswith("."):
        candidate = candidate[:-1]
    if not candidate:
        raise ValueError(f"{setting_name} contains an invalid RP ID")
    try:
        normalized = (
            idna.encode(candidate, uts46=False, std3_rules=True).decode("ascii").lower()
        )
    except (idna.IDNAError, UnicodeError) as exc:
        raise ValueError(f"{setting_name} contains an invalid RP ID") from exc
    labels = normalized.split(".")
    if len(normalized) > 253 or any(not label or len(label) > 63 for label in labels):
        raise ValueError(f"{setting_name} contains an invalid RP ID")
    return normalized


def ends_in_numeric_label(hostname: str) -> bool:
    """Reject decimal and hexadecimal-looking terminal labels."""
    candidate = hostname[:-1] if hostname.endswith(".") else hostname
    final_label = candidate.rsplit(".", 1)[-1].lower()
    return final_label.startswith("0x") or (
            bool(final_label)
            and all(character in _ASCII_DECIMAL_DIGITS for character in final_label)
    )


def normalize_rp_id(value: object, setting_name: str = "rp_id") -> str:
    """Apply one canonical IDNA/PSL/IP predicate to every stored RP ID."""
    candidate = value.strip() if isinstance(value, str) else ""
    if candidate.endswith("."):
        candidate = candidate[:-1]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        raise ValueError(f"{setting_name} entries must not be IP addresses")
    normalized = normalize_dns_name(value, setting_name)
    if ends_in_numeric_label(normalized):
        raise ValueError(
            f"{setting_name} entries must not end in a numeric IP-like label"
        )
    if normalized == "localhost":
        return normalized
    if _PUBLIC_SUFFIX_LIST.privatesuffix(normalized) is None:
        raise ValueError(
            f"{setting_name} entries must be registrable domains or tenant hosts"
        )
    return normalized
