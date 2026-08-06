"""Robust JSON parsing utilities with automatic repair.

Utilizes `json-repair` to automatically fix common formatting problems (like single
quotes, missing/trailing commas, or unquoted keys) before attempting standard parsing.
"""

from __future__ import annotations

import json
from typing import Any

from json_repair import repair_json


def robust_json_loads(json_string: str, **kwargs: Any) -> Any:
    """Parse JSON string with automatic repair for common formatting issues.
    
    This function first attempts standard json.loads(), and if that fails,
    uses json-repair to fix common issues like:
    - Trailing commas
    - Single quotes instead of double quotes
    - Unquoted keys
    - Missing commas
    - Comments
    
    Args:
        json_string: The JSON string to parse
        **kwargs: Additional keyword arguments passed to json.loads()
        
    Returns:
        The parsed JSON object (dict, list, etc.)
        
    Raises:
        ValueError: If the JSON cannot be parsed even after repair attempts
    """
    if not isinstance(json_string, str):
        raise TypeError(f"Expected string, got {type(json_string).__name__}")

    # First attempt: standard json.loads
    try:
        return json.loads(json_string, **kwargs)
    except json.JSONDecodeError:
        pass

    # Second attempt: repair and parse
    try:
        repaired = repair_json(json_string)
        return json.loads(repaired, **kwargs)
    except Exception as exc:
        raise ValueError(f"Failed to parse JSON even after repair: {exc}") from exc
