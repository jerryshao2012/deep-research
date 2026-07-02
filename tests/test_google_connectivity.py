#!/usr/bin/env python3
"""Test reachability of google.com."""

import httpx
import sys


def test_google_reachable():
    """Verify that google.com is reachable from the current environment."""
    url = "https://www.google.com"
    timeout_seconds = 10

    with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
        response = client.get(url)

    assert 200 <= response.status_code < 400, (
        f"Unable to reach {url}. Status code: {response.status_code}"
    )


if __name__ == "__main__":
    try:
        test_google_reachable()
        print("✓ SUCCESS: google.com is reachable")
        sys.exit(0)
    except AssertionError as exc:
        print(f"✗ FAILED: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"✗ FAILED: Error reaching google.com: {type(exc).__name__}: {exc}")
        sys.exit(1)
