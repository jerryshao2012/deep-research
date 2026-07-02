#!/usr/bin/env python3
"""Auto-increment API version in webapp.py"""
import re
import sys
from pathlib import Path


def increment_version(file_path: Path) -> str:
    """Increment the sub-version (patch) in webapp/config.py and return new version."""
    content = file_path.read_text()

    # Find the API_VERSION line (supporting optional type hint like : str)
    match = re.search(r'API_VERSION(?:\s*:\s*\w+)?\s*=\s*"(\d+)\.(\d+)\.(\d+)"', content)
    if not match:
        raise ValueError("Could not find API_VERSION in webapp/config.py")

    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))

    # Increment patch version
    new_patch = patch + 1
    new_version = f"{major}.{minor}.{new_patch}"

    # Replace in content using the exact matched pattern
    old_match_str = match.group(0)
    new_match_str = old_match_str.replace(f'"{major}.{minor}.{patch}"', f'"{new_version}"')
    new_content = content.replace(old_match_str, new_match_str)

    # Write back
    file_path.write_text(new_content)

    return new_version


if __name__ == "__main__":
    webapp_path = Path(__file__).parent / "webapp/config.py"

    try:
        print(webapp_path)
        new_version = increment_version(webapp_path)
        print(f"✅ Version incremented to {new_version}")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)
