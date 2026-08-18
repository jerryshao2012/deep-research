"""Pure, report-safe state transitions for strict citation failures."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from langchain_core.messages import AIMessage

CitationFailureCode = Literal[
    "missing_url",
    "placeholder_source",
    "unresolved_reference",
    "malformed_reference",
]

_CITATION_FAILURE_CODES = frozenset(
    {
        "missing_url",
        "placeholder_source",
        "unresolved_reference",
        "malformed_reference",
    }
)
_SAFE_DETAILS = frozenset({"web", "link", "number", "url", "marker", "range"})
_SAFE_SOURCE_DETAIL = re.compile(r"source:[1-9][0-9]{0,2}\Z")
MAX_CITATION_FAILURE_DEFECTS = 16
CITATION_FAILURE_CLEAR_UPDATE: dict[str, Any] = {
    "citation_failure_run_id": None,
    "citation_failure_report_fingerprint": None,
    "citation_failure_defects": [],
}


@dataclass(frozen=True, order=True, slots=True)
class CitationFailureDefect:
    """One serialized structural defect containing no report prose or URL."""

    code: CitationFailureCode
    detail: str


class ReportCitationError(RuntimeError):
    """Safe terminal error raised after strict citation correction exhaustion."""

    def __init__(self, defects: object = None) -> None:
        """Build a fixed error summary from allow-listed defect codes only."""
        codes = sorted(_defect_codes(defects))
        suffix = f" (defects={','.join(codes)})." if codes else "."
        super().__init__(
            "Report citations remain invalid after automatic correction limit"
            f"{suffix}"
        )


def serialize_citation_defects(defects: Iterable[object]) -> list[dict[str, str]]:
    """Return deterministic, bounded defects safe for checkpoint history."""
    serialized: set[tuple[str, str]] = set()
    for defect in defects:
        code = getattr(defect, "code", None)
        detail = getattr(defect, "detail", None)
        if code not in _CITATION_FAILURE_CODES:
            continue
        safe_detail = _safe_detail(detail)
        serialized.add((code, safe_detail))
        if len(serialized) >= MAX_CITATION_FAILURE_DEFECTS:
            break
    return [
        {"code": code, "detail": detail}
        for code, detail in sorted(serialized)
    ]


def build_citation_failure_update(
    *,
    run_id: str,
    report_fingerprint: str,
    defects: Iterable[object],
    terminal: AIMessage,
) -> dict[str, Any]:
    """Build terminal checkpoint metadata without exposing report content."""
    serialized = serialize_citation_defects(defects)
    if not run_id or not report_fingerprint or not serialized:
        raise ValueError("citation failure requires run, report, and defects")
    metadata = {**terminal.response_metadata, "resume_intermediate": True}
    tagged = terminal.model_copy(update={"response_metadata": metadata})
    return {
        "messages": [tagged],
        "citation_failure_run_id": run_id,
        "citation_failure_report_fingerprint": report_fingerprint,
        "citation_failure_defects": serialized,
        "jump_to": "end",
    }


def citation_failure_is_current(
    state: Mapping[str, Any],
    *,
    run_id: object,
    report_fingerprint: object,
) -> bool:
    """Return whether well-formed failure metadata owns this run/report pair."""
    normalized_defects = _normalize_serialized_defects(
        state.get("citation_failure_defects")
    )
    return (
        isinstance(run_id, str)
        and bool(run_id)
        and isinstance(report_fingerprint, str)
        and bool(report_fingerprint)
        and state.get("citation_failure_run_id") == run_id
        and state.get("citation_failure_report_fingerprint")
        == report_fingerprint
        and normalized_defects is not None
    )


def clear_stale_citation_failure(
    state: Mapping[str, Any],
    *,
    run_id: object,
    report_fingerprint: object,
) -> dict[str, Any] | None:
    """Clear malformed metadata or failure state from another run/report."""
    has_failure_metadata = (
        state.get("citation_failure_run_id") is not None
        or state.get("citation_failure_report_fingerprint") is not None
        or state.get("citation_failure_defects") not in (None, [])
    )
    if not has_failure_metadata:
        return None
    if citation_failure_is_current(
        state,
        run_id=run_id,
        report_fingerprint=report_fingerprint,
    ):
        return None
    return dict(CITATION_FAILURE_CLEAR_UPDATE)


def raise_current_citation_failure(
    state: Mapping[str, Any],
    *,
    run_id: object,
    report_fingerprint: object,
) -> None:
    """Raise only for current, well-formed checkpointed failure metadata."""
    if not citation_failure_is_current(
        state,
        run_id=run_id,
        report_fingerprint=report_fingerprint,
    ):
        return
    raise ReportCitationError(state.get("citation_failure_defects"))


def citation_failure_blocks_finalization(
    state: Mapping[str, Any],
    *,
    report_fingerprint: object,
) -> bool:
    """Return whether current report has a pending terminal citation failure."""
    return citation_failure_is_current(
        state,
        run_id=state.get("completion_current_run_id"),
        report_fingerprint=report_fingerprint,
    )


def citation_acceptance_ready(
    state: Mapping[str, Any],
    *,
    report_fingerprint: object,
    strict_required: bool,
) -> bool:
    """Return whether structural citation acceptance permits final exposure."""
    if citation_failure_blocks_finalization(
        state,
        report_fingerprint=report_fingerprint,
    ):
        return False
    if not strict_required:
        return True
    return (
        isinstance(report_fingerprint, str)
        and bool(report_fingerprint)
        and state.get("citation_accepted_report_fingerprint")
        == report_fingerprint
    )


def resolve_citation_run_id(
    config: Mapping[str, Any],
    runtime: object,
    *,
    fallback: object = None,
) -> str | None:
    """Resolve actual runtime run ID before config and checkpoint fallback."""
    execution_info = getattr(runtime, "execution_info", None)
    if isinstance(execution_info, Mapping):
        actual = execution_info.get("run_id")
    else:
        actual = getattr(execution_info, "run_id", None)
    for candidate in (actual, config.get("run_id"), fallback):
        if isinstance(candidate, UUID):
            return str(candidate)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _normalize_serialized_defects(value: object) -> tuple[CitationFailureDefect, ...] | None:
    if not isinstance(value, list) or not value or len(value) > MAX_CITATION_FAILURE_DEFECTS:
        return None
    defects: list[CitationFailureDefect] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            return None
        code = raw.get("code")
        detail = raw.get("detail")
        if code not in _CITATION_FAILURE_CODES or _safe_detail(detail) != detail:
            return None
        defects.append(CitationFailureDefect(code=code, detail=detail))
    return tuple(defects)


def _safe_detail(value: object) -> str:
    if isinstance(value, str) and (
        value in _SAFE_DETAILS or _SAFE_SOURCE_DETAIL.fullmatch(value)
    ):
        return value
    return "redacted"


def _defect_codes(value: object) -> set[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return set()
    codes: set[str] = set()
    for defect in value:
        if isinstance(defect, Mapping):
            code = defect.get("code")
        else:
            code = getattr(defect, "code", None)
        if code in _CITATION_FAILURE_CODES:
            codes.add(code)
    return codes
