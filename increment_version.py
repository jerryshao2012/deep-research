#!/usr/bin/env python3
"""Auto-increment API version in webapp.py."""

import re
import sys
from pathlib import Path


def increment_version(file_path: Path) -> str:
    """Increment the sub-version (patch) in webapp/config.py and return new version."""
    content = file_path.read_text()

    # Find the API_VERSION line (supporting optional type hint like : str)
    match = re.search(
        r'API_VERSION(?:\s*:\s*\w+)?\s*=\s*"(\d+)\.(\d+)\.(\d+)"', content
    )
    if not match:
        raise ValueError("Could not find API_VERSION in webapp/config.py")

    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))

    # Increment patch version
    new_patch = patch + 1
    new_version = f"{major}.{minor}.{new_patch}"

    # Replace in content using the exact matched pattern
    old_match_str = match.group(0)
    new_match_str = old_match_str.replace(
        f'"{major}.{minor}.{patch}"', f'"{new_version}"'
    )
    new_content = content.replace(old_match_str, new_match_str)

    # Write back
    file_path.write_text(new_content)

    # Try to find and update contracts/custom-api.openapi.json
    openapi_paths = [
        file_path.parent.parent / "contracts" / "custom-api.openapi.json",
        Path(__file__).parent / "contracts" / "custom-api.openapi.json",
    ]
    openapi_file = None
    for p in openapi_paths:
        if p.exists() and p.is_file():
            openapi_file = p
            break

    if openapi_file:
        openapi_content = openapi_file.read_text(encoding="utf-8")
        openapi_match = re.search(
            r'"version"\s*:\s*"(\d+)\.(\d+)\.(\d+)"', openapi_content
        )
        if not openapi_match:
            raise ValueError(f"Could not find version string in {openapi_file}")

        old_version_str = f'"{openapi_match.group(1)}.{openapi_match.group(2)}.{openapi_match.group(3)}"'
        new_version_str = f'"{new_version}"'
        old_match_str = openapi_match.group(0)
        new_match_str = old_match_str.replace(old_version_str, new_version_str)
        new_openapi_content = openapi_content.replace(old_match_str, new_match_str)
        openapi_file.write_text(new_openapi_content, encoding="utf-8")

    return new_version


if __name__ == "__main__":
    webapp_path = (
        Path(sys.argv[1])
        if len(sys.argv) == 2
        else Path(__file__).parent / "webapp/config.py"
    )

    if len(sys.argv) > 2:
        print(  # noqa: T201
            "❌ Error: expected at most one config path", file=sys.stderr
        )
        sys.exit(2)

    try:
        print(webapp_path)  # noqa: T201
        new_version = increment_version(webapp_path)
        print(f"✅ Version incremented to {new_version}")  # noqa: T201
        sys.exit(0)
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)  # noqa: T201
        sys.exit(1)
