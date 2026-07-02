"""Evaluation metrics tracking and logging utilities.

Compiles test manifests and logs execution run stats (including token counts,
durations, tool invocations, and failures) to files for evaluation comparison.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logger_utils import setup_logger

logger = setup_logger(__name__)

_TOOL_FAILURE_PREFIXES = (
    "Invalid JSON payload:",
    "Schema validation failed",
    "Unknown skill",
    "Error invoking tool",
    "ERROR:",
)


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO8601 format."""
    return datetime.now(timezone.utc).isoformat()


def build_manifest(
        *,
        subject: str,
        skill: str | None,
        doc_folder: str | None,
        no_web: bool,
        model_name: str,
        verify_ssl: str | bool,
) -> dict[str, Any]:
    """Build a canonical test-case manifest for comparability checks."""
    return {
        "subject": (subject or "").strip(),
        "skill": skill,
        "doc_folder": doc_folder,
        "no_web": bool(no_web),
        "model_name": model_name,
        "verify_ssl": str(verify_ssl),
    }


def manifest_hash(manifest: dict[str, Any]) -> str:
    """Return stable SHA256 hash for a manifest."""
    canonical = json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_git_sha(cwd: Path | None = None) -> str:
    """Return short git SHA if available, else 'unknown'."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _message_role_name_content(message: Any) -> tuple[str, str, str]:
    """Extract the role, name, and content text from a message object or dict.

    Handles both dict-style and object-style messages, normalizing content
    that may be a string, list of blocks, or mixed types.

    Args:
        message: A message as a dict or object with ``role``/``type``,
            ``name``, and ``content`` attributes.

    Returns:
        A tuple of ``(role, name, content_text)`` where all values are
        strings.
    """
    if isinstance(message, dict):
        role = str(message.get("role", ""))
        name = str(message.get("name", "") or "")
        content = message.get("content", "")
    else:
        role = str(getattr(message, "type", ""))
        name = str(getattr(message, "name", "") or "")
        content = getattr(message, "content", "")

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        content_text = "\n".join(part for part in parts if part)
    else:
        content_text = str(content)

    return role.lower(), name, content_text


def _extract_tool_call_count(message: Any) -> int:
    """Count the number of tool calls in a message.

    Args:
        message: A message as a dict or object with an optional
            ``tool_calls`` attribute.

    Returns:
        The number of tool calls, or 0 if none are present.
    """
    if isinstance(message, dict):
        tool_calls = message.get("tool_calls")
    else:
        tool_calls = getattr(message, "tool_calls", None)

    if isinstance(tool_calls, list):
        return len(tool_calls)
    return 0


def _extract_usage_metadata(message: Any) -> dict[str, Any]:
    """Extract token usage metadata from a message.
    
    Checks multiple possible locations where LLM providers store token counts:
    - usage_metadata (LangChain standard)
    - response_metadata.token_usage (OpenAI/Anthropic)
    - response_metadata.usage (some providers)
    - Direct attributes on message objects
    """
    if isinstance(message, dict):
        # Check usage_metadata first (LangChain standard)
        usage = message.get("usage_metadata")

        # Check response_metadata variants
        if not usage:
            response_metadata = message.get("response_metadata", {})
            if isinstance(response_metadata, dict):
                usage = (
                        response_metadata.get("token_usage") or
                        response_metadata.get("usage") or
                        response_metadata.get("usage_details")
                )

        # Check for direct token fields in the message
        if not usage:
            usage = {
                k: v for k, v in message.items()
                if k in ("input_tokens", "output_tokens", "total_tokens",
                         "prompt_tokens", "completion_tokens") and v is not None
            }
    else:
        # For message objects, check attributes
        usage = getattr(message, "usage_metadata", None)

        if usage is None:
            response_metadata = getattr(message, "response_metadata", None)
            if isinstance(response_metadata, dict):
                usage = (
                        response_metadata.get("token_usage") or
                        response_metadata.get("usage") or
                        response_metadata.get("usage_details")
                )

        # Check for direct token attributes
        if not usage:
            usage = {
                k: getattr(message, k, None)
                for k in ("input_tokens", "output_tokens", "total_tokens",
                          "prompt_tokens", "completion_tokens")
                if getattr(message, k, None) is not None
            }

    return usage if isinstance(usage, dict) else {}


def _analyze_tool_call_parameters(tool_call: dict[str, Any]) -> dict[str, Any]:
    """Analyze tool call parameters for completeness and correctness.
    
    Returns metadata about parameter quality including:
    - has_arguments: whether arguments were provided
    - argument_count: number of arguments passed
    - has_required_params: whether required params appear present
    - parameter_quality_score: heuristic score 0-1
    """
    args = tool_call.get("args", {})

    if not args:
        return {
            "has_arguments": False,
            "argument_count": 0,
            "has_required_params": False,
            "parameter_quality_score": 0.0,
        }

    # Count arguments
    arg_count = len(args) if isinstance(args, dict) else 0

    # Check for common required parameter patterns
    tool_name = tool_call.get("name", "").lower()

    # Heuristic checks based on tool type
    if "read" in tool_name or "write" in tool_name or "file" in tool_name:
        has_required = bool(args.get("path") or args.get("file_path"))
    elif "search" in tool_name:
        has_required = bool(args.get("query") or args.get("search_query"))
    elif "think" in tool_name:
        has_required = bool(args.get("thought") or args.get("reasoning"))
    else:
        # Generic check: at least one non-empty argument
        has_required = any(v for v in args.values()) if isinstance(args, dict) else False

    # Calculate quality score
    quality_score = 0.0
    if has_required:
        quality_score += 0.5
    if arg_count > 0:
        quality_score += min(0.5, arg_count * 0.1)  # Bonus for multiple params, capped at 0.5

    return {
        "has_arguments": True,
        "argument_count": arg_count,
        "has_required_params": has_required,
        "parameter_quality_score": round(quality_score, 2),
    }


def _detect_self_correction(messages: list[Any], current_index: int, tool_name: str) -> dict[str, Any]:
    """Detect if agent corrected itself after a tool failure.
    
    Analyzes message history to identify correction patterns:
    - Retry with different parameters
    - Alternative tool selection
    - Error acknowledgment and strategy change
    
    Args:
        messages: Full message history
        current_index: Index of current tool response message
        tool_name: Name of the tool that was called
        
    Returns:
        Dictionary with correction detection results
    """
    if current_index < 2:
        return {
            "self_corrected": False,
            "correction_type": None,
            "correction_details": {},
        }

    # Look back at previous messages to find the tool call
    previous_tool_response = None

    for i in range(current_index - 1, -1, -1):
        msg = messages[i]
        role, name, content = _message_role_name_content(msg)

        if role == "tool" and name == tool_name:
            previous_tool_response = content
            break

    # Find the next AI message after the previous tool response
    for i in range(current_index + 1, len(messages)):
        msg = messages[i]
        role, _, content = _message_role_name_content(msg)

        if role in {"ai", "assistant"}:
            # Check if this AI message contains a new tool call to same tool
            if isinstance(msg, dict):
                tool_calls = msg.get("tool_calls", [])
            else:
                tool_calls = getattr(msg, "tool_calls", [])

            for tc in tool_calls:
                tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                if tc_name == tool_name:
                    # Agent retried the same tool - potential correction
                    new_args = tc.get("args", {})

                    # Compare with previous call if available
                    correction_detected = previous_tool_response and (
                            previous_tool_response.startswith(_TOOL_FAILURE_PREFIXES) or
                            not previous_tool_response.strip()
                    )

                    return {
                        "self_corrected": correction_detected,
                        "correction_type": "retry_same_tool" if correction_detected else None,
                        "correction_details": {
                            "had_previous_failure": correction_detected,
                            "new_arguments_provided": bool(new_args),
                        },
                    }

            # Check if agent switched to alternative tool
            if tool_calls:
                return {
                    "self_corrected": True,
                    "correction_type": "alternative_tool",
                    "correction_details": {
                        "switched_from": tool_name,
                        "alternative_tools": [
                            tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                            for tc in tool_calls
                        ],
                    },
                }

            break

    return {
        "self_corrected": False,
        "correction_type": None,
        "correction_details": {},
    }


def collect_run_metrics(result: dict[str, Any], runtime_seconds: float, stream_fallback_used: bool) -> dict[str, Any]:
    """Collect golden-dataset metrics from a run result and runtime context.
    
    This is for CLI unit testing with baseline comparison. Includes completeness
    checks for /golden_dataset_metrics.md and /final_report.md files.
    """
    messages = result.get("messages", [])
    files = result.get("files", {}) if isinstance(result.get("files", {}), dict) else {}

    total_tool_calls = 0
    successful_tool_calls = 0
    failed_tool_calls = 0

    # Enhanced tracking
    tool_call_details = []
    parameter_validation_results = []
    self_correction_events = []
    retry_count = 0
    tools_with_errors = set()
    tools_corrected = set()

    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    saw_token_metadata = False

    for idx, message in enumerate(messages):
        role, _name, content = _message_role_name_content(message)

        if role in {"ai", "assistant"}:
            tool_calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", [])

            if isinstance(tool_calls, list):
                total_tool_calls += len(tool_calls)

                # Analyze each tool call's parameters
                for tc in tool_calls:
                    tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "unknown")
                    param_analysis = _analyze_tool_call_parameters(
                        tc if isinstance(tc, dict) else {"name": getattr(tc, "name", ""),
                                                         "args": getattr(tc, "args", {})})

                    tool_call_details.append({
                        "tool_name": tc_name,
                        "parameter_analysis": param_analysis,
                    })

                    parameter_validation_results.append({
                        "tool_name": tc_name,
                        "has_valid_parameters": param_analysis["has_required_params"],
                        "quality_score": param_analysis["parameter_quality_score"],
                    })

        if role == "tool":
            content_text = content.strip()
            tool_name = _name or "unknown"
            is_failure = not content_text or content_text.startswith(_TOOL_FAILURE_PREFIXES)

            if is_failure:
                failed_tool_calls += 1
                tools_with_errors.add(tool_name)

                # Check for self-correction
                correction = _detect_self_correction(messages, idx, tool_name)
                if correction["self_corrected"]:
                    self_correction_events.append({
                        "tool_name": tool_name,
                        "correction_type": correction["correction_type"],
                        "details": correction["correction_details"],
                    })
                    tools_corrected.add(tool_name)
            else:
                successful_tool_calls += 1

                # Check if this success came after a failure (recovery)
                if tool_name in tools_with_errors:
                    retry_count += 1

        usage = _extract_usage_metadata(message)
        if usage:
            saw_token_metadata = True
            prompt_tokens += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            completion_tokens += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            total_tokens += int(usage.get("total_tokens") or 0)

            # Debug: Log token extraction for first message with usage data
            if idx == 0 or not saw_token_metadata:
                logger.debug(f"Token metadata extracted: {usage}")

    if saw_token_metadata and total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens

    has_metrics_md = "/golden_dataset_metrics.md" in files
    has_final_report = "/final_report.md" in files
    completeness_pass = has_metrics_md and has_final_report

    intervention_required = bool(
        not completeness_pass
        or stream_fallback_used
        or failed_tool_calls > 0
    )

    # Calculate enhanced metrics
    avg_parameter_quality = (
        sum(p["quality_score"] for p in parameter_validation_results) / len(parameter_validation_results)
        if parameter_validation_results else 0.0
    )

    valid_parameter_rate = (
        sum(1 for p in parameter_validation_results if p["has_valid_parameters"]) / len(parameter_validation_results)
        if parameter_validation_results else 0.0
    )

    self_correction_rate = (
        len(tools_corrected) / len(tools_with_errors)
        if tools_with_errors else 0.0
    )

    return {
        "completeness": {
            "pass": completeness_pass,
            "has_golden_dataset_metrics_md": has_metrics_md,
            "has_final_report_md": has_final_report,
        },
        "tool_execution": {
            "total_tool_calls": total_tool_calls,
            "successful_tool_calls": successful_tool_calls,
            "failed_tool_calls": failed_tool_calls,
            "success_rate": (successful_tool_calls / total_tool_calls) if total_tool_calls > 0 else 1.0,
            "retry_count": retry_count,
            "unique_tools_with_errors": len(tools_with_errors),
            "tools_corrected_count": len(tools_corrected),
        },
        "parameter_validation": {
            "average_quality_score": round(avg_parameter_quality, 3),
            "valid_parameter_rate": round(valid_parameter_rate, 3),
            "total_calls_analyzed": len(parameter_validation_results),
            "calls_with_missing_params": sum(1 for p in parameter_validation_results if not p["has_valid_parameters"]),
        },
        "self_correction": {
            "correction_events": len(self_correction_events),
            "self_correction_rate": round(self_correction_rate, 3),
            "tools_attempted_correction": list(tools_corrected),
            "correction_types": list(set(e["correction_type"] for e in self_correction_events if e["correction_type"])),
        },
        "failure": {
            "intervention_required": intervention_required,
            "failure_rate": 1.0 if intervention_required else 0.0,
        },
        "token_efficiency": {
            "available": saw_token_metadata,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "tokens_per_successful_task": total_tokens if completeness_pass and saw_token_metadata else None,
        },
        "latency": {
            "runtime_seconds": round(runtime_seconds, 3),
        },
    }


def collect_server_metrics(
        messages: list[Any],
        runtime_seconds: float,
) -> dict[str, Any]:
    """Collect operational metrics for server/dev mode tracking.
    
    Simplified version focused on facts only (no baseline comparison).
    Unlike CLI golden dataset evaluation, this does NOT check for specific
    output files or compare against baselines since user inputs vary each time.
    
    Args:
        messages: List of conversation messages from agent state
        runtime_seconds: Total execution time in seconds
        
    Returns:
        Dictionary with operational metrics for tracking
    """
    total_tool_calls = 0
    successful_tool_calls = 0
    failed_tool_calls = 0

    # Enhanced tracking
    parameter_validation_results = []
    self_correction_events = []
    retry_count = 0
    tools_with_errors = set()
    tools_corrected = set()

    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    saw_token_metadata = False

    for idx, message in enumerate(messages):
        role, name, content = _message_role_name_content(message)

        if role in {"ai", "assistant"}:
            tool_calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", [])

            if isinstance(tool_calls, list):
                total_tool_calls += len(tool_calls)

                # Analyze each tool call's parameters
                for tc in tool_calls:
                    tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "unknown")
                    param_analysis = _analyze_tool_call_parameters(
                        tc if isinstance(tc, dict) else {"name": getattr(tc, "name", ""),
                                                         "args": getattr(tc, "args", {})})

                    parameter_validation_results.append({
                        "tool_name": tc_name,
                        "has_valid_parameters": param_analysis["has_required_params"],
                        "quality_score": param_analysis["parameter_quality_score"],
                    })

        if role == "tool":
            content_text = content.strip()
            tool_name = name or "unknown"
            is_failure = not content_text or content_text.startswith(_TOOL_FAILURE_PREFIXES)

            if is_failure:
                failed_tool_calls += 1
                tools_with_errors.add(tool_name)

                # Check for self-correction
                correction = _detect_self_correction(messages, idx, tool_name)
                if correction["self_corrected"]:
                    self_correction_events.append({
                        "tool_name": tool_name,
                        "correction_type": correction["correction_type"],
                        "details": correction["correction_details"],
                    })
                    tools_corrected.add(tool_name)
            else:
                successful_tool_calls += 1

                # Check if this success came after a failure (recovery)
                if tool_name in tools_with_errors:
                    retry_count += 1

        usage = _extract_usage_metadata(message)
        if usage:
            saw_token_metadata = True
            prompt_tokens += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            completion_tokens += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            total_tokens += int(usage.get("total_tokens") or 0)

    if saw_token_metadata and total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens

    # Calculate enhanced metrics
    avg_parameter_quality = (
        sum(p["quality_score"] for p in parameter_validation_results) / len(parameter_validation_results)
        if parameter_validation_results else 0.0
    )

    valid_parameter_rate = (
        sum(1 for p in parameter_validation_results if p["has_valid_parameters"]) / len(parameter_validation_results)
        if parameter_validation_results else 0.0
    )

    self_correction_rate = (
        len(tools_corrected) / len(tools_with_errors)
        if tools_with_errors else 0.0
    )

    return {
        "tool_execution": {
            "total_tool_calls": total_tool_calls,
            "successful_tool_calls": successful_tool_calls,
            "failed_tool_calls": failed_tool_calls,
            "success_rate": (successful_tool_calls / total_tool_calls) if total_tool_calls > 0 else 1.0,
            "retry_count": retry_count,
            "unique_tools_with_errors": len(tools_with_errors),
            "tools_corrected_count": len(tools_corrected),
        },
        "parameter_validation": {
            "average_quality_score": round(avg_parameter_quality, 3),
            "valid_parameter_rate": round(valid_parameter_rate, 3),
            "total_calls_analyzed": len(parameter_validation_results),
            "calls_with_missing_params": sum(1 for p in parameter_validation_results if not p["has_valid_parameters"]),
        },
        "self_correction": {
            "correction_events": len(self_correction_events),
            "self_correction_rate": round(self_correction_rate, 3),
            "tools_attempted_correction": list(tools_corrected),
            "correction_types": list(set(e["correction_type"] for e in self_correction_events if e["correction_type"])),
        },
        "token_efficiency": {
            "available": saw_token_metadata,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        "latency": {
            "runtime_seconds": round(runtime_seconds, 3),
        },
    }


def make_run_record(
        *,
        manifest: dict[str, Any],
        run_type: str,
        metrics: dict[str, Any],
        runtime_seconds: float,
        model_name: str,
        stream_fallback_used: bool,
        output_file: str,
        git_sha: str,
) -> dict[str, Any]:
    """Build one JSONL entry for an evaluation run."""
    return {
        "timestamp_utc": utc_now_iso(),
        "run_type": run_type,
        "manifest": manifest,
        "manifest_hash": manifest_hash(manifest),
        "model_name": model_name,
        "git_sha": git_sha,
        "runtime_seconds": runtime_seconds,
        "stream_fallback_used": stream_fallback_used,
        "output_file": output_file,
        "metrics": metrics,
    }


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON object as a line in a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load a JSONL file into records, skipping empty lines."""
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            payload = json.loads(text)
            if isinstance(payload, dict):
                records.append(payload)
    return records


def latest_baseline(records: list[dict[str, Any]], manifest_hash_value: str) -> dict[str, Any] | None:
    """Get most recent baseline record with matching manifest hash."""
    for record in reversed(records):
        if record.get("run_type") == "baseline" and record.get("manifest_hash") == manifest_hash_value:
            return record
    return None


def _metric_verdict(candidate_value: float, baseline_value: float, tolerance: float) -> str:
    """Classify a candidate metric relative to a baseline within a tolerance band.

    Args:
        candidate_value: The value measured in the current run.
        baseline_value: The value from the baseline run.
        tolerance: Fractional tolerance (e.g., 0.20 for ±20%).

    Returns:
        ``"worse"`` if the candidate exceeds baseline + tolerance,
        ``"better"`` if it is below baseline − tolerance, or ``"same"``
        otherwise.
    """
    if candidate_value > baseline_value * (1 + tolerance):
        return "worse"
    if candidate_value < baseline_value * (1 - tolerance):
        return "better"
    return "same"


def compare_records(
        *,
        baseline: dict[str, Any] | None,
        candidate: dict[str, Any],
        tool_growth_threshold: float = 0.30,
        latency_regression_threshold: float = 0.15,
) -> dict[str, Any]:
    """Compare candidate against baseline for same manifest only."""
    candidate_hash = str(candidate.get("manifest_hash", ""))

    if baseline is None:
        return {
            "comparable": False,
            "overall_verdict": "non-comparable",
            "reason": "no baseline found for manifest",
            "per_metric": {},
        }

    baseline_hash = str(baseline.get("manifest_hash", ""))
    if candidate_hash != baseline_hash:
        return {
            "comparable": False,
            "overall_verdict": "non-comparable",
            "reason": "manifest hash mismatch",
            "per_metric": {},
        }

    base_metrics = baseline.get("metrics", {})
    cand_metrics = candidate.get("metrics", {})

    per_metric: dict[str, str] = {}

    base_complete = bool(base_metrics.get("completeness", {}).get("pass"))
    cand_complete = bool(cand_metrics.get("completeness", {}).get("pass"))
    if cand_complete and not base_complete:
        per_metric["completeness"] = "better"
    elif not cand_complete and base_complete:
        per_metric["completeness"] = "worse"
    else:
        per_metric["completeness"] = "same"

    base_total_tools = int(base_metrics.get("tool_execution", {}).get("total_tool_calls") or 0)
    cand_total_tools = int(cand_metrics.get("tool_execution", {}).get("total_tool_calls") or 0)
    tool_regression = False
    if base_total_tools > 0:
        tool_regression = cand_total_tools > base_total_tools * (1 + tool_growth_threshold)

    if tool_regression and not (cand_complete and not base_complete):
        per_metric["tool_execution"] = "worse"
    elif cand_total_tools < base_total_tools:
        per_metric["tool_execution"] = "better"
    else:
        per_metric["tool_execution"] = "same"

    base_failure = float(base_metrics.get("failure", {}).get("failure_rate") or 0.0)
    cand_failure = float(cand_metrics.get("failure", {}).get("failure_rate") or 0.0)
    if cand_failure > base_failure:
        per_metric["failure"] = "worse"
    elif cand_failure < base_failure:
        per_metric["failure"] = "better"
    else:
        per_metric["failure"] = "same"

    base_tokens_available = bool(base_metrics.get("token_efficiency", {}).get("available"))
    cand_tokens_available = bool(cand_metrics.get("token_efficiency", {}).get("available"))
    if base_tokens_available and cand_tokens_available:
        base_total_tokens = float(base_metrics.get("token_efficiency", {}).get("total_tokens") or 0.0)
        cand_total_tokens = float(cand_metrics.get("token_efficiency", {}).get("total_tokens") or 0.0)
        per_metric["token_efficiency"] = _metric_verdict(cand_total_tokens, base_total_tokens, 0.20)
    else:
        per_metric["token_efficiency"] = "unavailable"

    base_latency = float(base_metrics.get("latency", {}).get("runtime_seconds") or 0.0)
    cand_latency = float(cand_metrics.get("latency", {}).get("runtime_seconds") or 0.0)
    per_metric["latency"] = _metric_verdict(cand_latency, base_latency, latency_regression_threshold)

    # Compare parameter validation quality
    base_param_quality = float(base_metrics.get("parameter_validation", {}).get("average_quality_score") or 0.0)
    cand_param_quality = float(cand_metrics.get("parameter_validation", {}).get("average_quality_score") or 0.0)
    if cand_param_quality > base_param_quality * 1.1:
        per_metric["parameter_validation"] = "better"
    elif cand_param_quality < base_param_quality * 0.9:
        per_metric["parameter_validation"] = "worse"
    else:
        per_metric["parameter_validation"] = "same"

    # Compare self-correction capability
    base_correction_rate = float(base_metrics.get("self_correction", {}).get("self_correction_rate") or 0.0)
    cand_correction_rate = float(cand_metrics.get("self_correction", {}).get("self_correction_rate") or 0.0)
    if cand_correction_rate > base_correction_rate:
        per_metric["self_correction"] = "better"
    elif cand_correction_rate < base_correction_rate:
        per_metric["self_correction"] = "worse"
    else:
        per_metric["self_correction"] = "same"

    verdict_values = [v for v in per_metric.values() if v in {"better", "same", "worse"}]
    if any(v == "worse" for v in verdict_values):
        overall = "worse"
    elif any(v == "better" for v in verdict_values):
        overall = "better"
    else:
        overall = "same"

    return {
        "comparable": True,
        "overall_verdict": overall,
        "reason": None,
        "per_metric": per_metric,
    }


async def log_server_metrics(
        *,
        messages: list[Any],
        files: dict[str, Any],
        runtime_seconds: float,
        model_name: str,
        context: dict[str, Any] | None = None,
        history_file: str | Path = "./output/eval_history/dev_server_runs.jsonl",
) -> dict[str, Any] | None:
    """Log operational metrics for langgraph dev/server mode (async, non-blocking).
    
    This function collects facts (tools called, tokens used, runtime, etc.)
    for general tracking purposes. Unlike CLI regression testing, this does NOT
    compare against baselines since user inputs vary each time.
    
    This async version runs in the background and catches all exceptions to avoid
    interrupting the main chat response flow.
    
    Args:
        messages: List of conversation messages from agent state
        files: Dictionary of files from agent state
        runtime_seconds: Total execution time in seconds
        model_name: Name of the LLM model used
        context: Optional context metadata (subject, skill, doc_folder, no_web)
        history_file: Path to JSONL history file
        
    Returns:
        Dictionary with logged metrics summary for console output, or None on error
    """
    try:
        # Collect metrics using server-specific collector (facts only, no baseline comparison)
        run_metrics = collect_server_metrics(
            messages=messages,
            runtime_seconds=runtime_seconds,
        )

        # Extract summary stats for console output
        tool_calls = run_metrics.get("tool_execution", {}).get("total_tool_calls", 0)
        success_rate = run_metrics.get("tool_execution", {}).get("success_rate", 0)
        total_tokens = run_metrics.get("token_efficiency", {}).get("total_tokens", 0)
        param_quality = run_metrics.get("parameter_validation", {}).get("average_quality_score", 0)
        corrections = run_metrics.get("self_correction", {}).get("correction_events", 0)

        summary = {
            "runtime_seconds": round(runtime_seconds, 3),
            "tool_calls": tool_calls,
            "success_rate": success_rate,
            "total_tokens": total_tokens,
            "param_quality": param_quality,
            "corrections": corrections,
        }

        # Create simple run record with timestamp and facts
        record = {
            "timestamp_utc": utc_now_iso(),
            "model_name": model_name,
            "context": context or {},
            "runtime_seconds": round(runtime_seconds, 3),
            "metrics": run_metrics,
            "summary": summary,
            "files": list(files.keys()) if files else [],
        }

        # Append to history file (use async file I/O if available, otherwise sync)
        history_path = Path(history_file)
        if not history_path.parent.exists():
            history_path.parent.mkdir(parents=True, exist_ok=True)

        # Use asyncio.to_thread for file I/O to avoid blocking the event loop
        await _write_metrics_to_file(history_path, record)

        return summary

    except Exception as e:
        # Log error but never propagate - this must not interrupt main chat response
        logger.error(f"⚠️  Metrics logging failed (non-critical): {type(e).__name__}: {e}")
        return None


async def _write_metrics_to_file(history_path: Path, record: dict) -> None:
    """Write a metrics record to a JSONL file using async I/O.

    Offloads the actual file write to a thread-pool executor so the event
    loop is never blocked by synchronous filesystem operations.

    Args:
        history_path: Path to the JSONL history file.
        record: Metrics record dict to append as a single JSON line.
    """
    import json
    import asyncio

    # Run file I/O in a thread pool to avoid blocking the event loop
    def _write_sync():
        with open(history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    await asyncio.to_thread(_write_sync)
