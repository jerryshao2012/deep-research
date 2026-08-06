"""Deterministic policy helpers for resuming incomplete research work."""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

BASE_RESUME_PHRASES = frozenset(
    {
        "continue",
        "go on",
        "keep going",
        "resume",
        "proceed",
        "finish the remaining tasks",
        "complete the remaining tasks",
    }
)


def _accepted_resume_phrases() -> frozenset[str]:
    phrases: set[str] = set()
    for base in BASE_RESUME_PHRASES:
        phrases.update(
            {
                base,
                f"please {base}",
                f"{base} please",
                f"{base}, please",
            }
        )
    return frozenset(phrases)


ACCEPTED_RESUME_PHRASES = _accepted_resume_phrases()

INCOMPLETE_TODO_STATUSES = frozenset({"pending", "in_progress"})
DEFAULT_MAX_RESUME_ROUNDS = 3


@dataclass(frozen=True)
class TodoInspection:
    """Incomplete todo items plus count of malformed entries."""

    incomplete: tuple[dict[str, Any], ...]
    malformed_count: int = 0

    @property
    def has_incomplete(self) -> bool:
        """Return whether any known incomplete todos remain."""
        return bool(self.incomplete)


def normalize_resume_text(text: str) -> str:
    """Normalize text without broadening the supported phrase grammar."""
    normalized = unicodedata.normalize("NFKC", str(text)).casefold().strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.rstrip(".!").rstrip()


def is_resume_intent(text: str) -> bool:
    """Return whether text exactly matches the explicit resume allowlist."""
    return normalize_resume_text(text) in ACCEPTED_RESUME_PHRASES


def inspect_todos(value: Any) -> TodoInspection:
    """Return known incomplete todos and count malformed list entries."""
    if not isinstance(value, list):
        return TodoInspection(())

    incomplete: list[dict[str, Any]] = []
    malformed_count = 0
    for item in value:
        if not isinstance(item, dict):
            malformed_count += 1
            continue

        status = str(item.get("status", "")).strip().casefold()
        if status in INCOMPLETE_TODO_STATUSES:
            incomplete.append(item)
        elif status != "completed":
            malformed_count += 1

    return TodoInspection(tuple(incomplete), malformed_count)


def get_max_resume_rounds() -> int:
    """Read a positive resume-round limit, falling back safely."""
    raw = os.getenv("MAX_RESUME_ROUNDS")
    if raw is None:
        return DEFAULT_MAX_RESUME_ROUNDS

    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0

    if value <= 0:
        logger.warning(
            "Invalid MAX_RESUME_ROUNDS setting; using fallback value %d.",
            DEFAULT_MAX_RESUME_ROUNDS,
        )
        return DEFAULT_MAX_RESUME_ROUNDS
    return value


def build_round_limit_message(
        inspection: TodoInspection,
        rounds: int,
) -> str:
    """Build user-visible output when automatic resume rounds are exhausted."""
    lines = [
        f"Resume safety limit reached after {rounds} rounds.",
        "Remaining tasks:",
    ]
    for item in inspection.incomplete:
        label = str(item.get("content") or item.get("task") or "Unnamed task")
        lines.append(f"- [{item.get('status')}] {label}")
    lines.append("Send another resume phrase to continue.")
    return "\n".join(lines)


def is_resume_intermediate_message(message: Any) -> bool:
    """Return whether a mapping or LangChain message is strictly hidden."""
    if isinstance(message, Mapping):
        metadata = message.get("response_metadata")
    else:
        metadata = getattr(message, "response_metadata", None)
    return (
            isinstance(metadata, Mapping)
            and metadata.get("resume_intermediate") is True
    )


def visible_messages(messages: Iterable[Any]) -> list[Any]:
    """Preserve message order while removing tagged resume intermediates."""
    return [
        message
        for message in messages
        if not is_resume_intermediate_message(message)
    ]
