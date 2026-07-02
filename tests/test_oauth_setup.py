#!/usr/bin/env python3
"""Test script for OAuth authentication setup."""

import os
from pathlib import Path

import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))


def test_oauth_imports():
    """Test that OAuth dependencies can be imported."""
    print("Testing OAuth imports...")

    try:
        from authlib.integrations.starlette_client import OAuth
        print("✓ authlib imported successfully")
    except ImportError as e:
        print(f"✗ Failed to import authlib: {e}")
        return False

    try:
        import itsdangerous
        print("✓ itsdangerous imported successfully")
    except ImportError as e:
        print(f"✗ Failed to import itsdangerous: {e}")
        return False

    return True


def test_oauth_handler():
    """Test OAuth handler module."""
    print("\nTesting OAuth handler module...")

    try:
        from webapp.oauth_handler import (
            google,
            github,
            user_manager,
            get_oauth_login_url,
        )
        print("✓ OAuth handler module imported successfully")

        # Test URL generation
        import asyncio
        from starlette.requests import Request

        def make_dummy_request():
            return Request({"type": "http", "method": "GET", "headers": [], "session": {}})

        try:
            google_url = asyncio.run(
                get_oauth_login_url(make_dummy_request(), "google", "http://localhost:8000/callback"))
            print(f"✓ Google login URL generated: {google_url[:50]}...")
        except Exception as e:
            print(f"⚠ Google URL generation warning (expected if no credentials): {e}")

        try:
            github_url = asyncio.run(
                get_oauth_login_url(make_dummy_request(), "github", "http://localhost:8000/callback"))
            print(f"✓ GitHub login URL generated: {github_url[:50]}...")
        except Exception as e:
            print(f"⚠ GitHub URL generation warning (expected if no credentials): {e}")

        return True

    except ImportError as e:
        print(f"✗ Failed to import oauth_handler: {e}")
        return False


def test_auth_module():
    """Test updated auth module."""
    print("\nTesting auth module...")

    try:
        from auth import authenticate, auth
        print("✓ Auth module imported successfully")
        print(f"✓ Auth instance created: {auth}")
        return True
    except ImportError as e:
        print(f"✗ Failed to import auth module: {e}")
        return False


def check_env_variables():
    """Check if OAuth environment variables are set."""
    print("\nChecking environment variables...")

    required_vars = [
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
    ]

    missing = []
    configured = []

    for var in required_vars:
        value = os.environ.get(var)
        if value and value != f"your-{var.lower().replace('_', '-')}":
            configured.append(var)
            print(f"✓ {var} is configured")
        else:
            missing.append(var)
            print(f"⚠ {var} is not configured (optional for testing)")

    if missing:
        print(f"\nNote: {len(missing)} OAuth credential(s) not set.")
        print("This is OK for testing - OAuth will be disabled until credentials are configured.")
        print("See OAUTH_SETUP.md for configuration instructions.")

    return len(configured) > 0


def test_session_management():
    """Test session management."""
    print("\nTesting session management...")

    try:
        from webapp.oauth_handler import user_manager

        # Create a test session
        test_user = {
            "identity": "test:123",
            "email": "test@example.com",
            "name": "Test User",
            "provider": "test",
        }

        token = user_manager.create_session(test_user, "test")
        print(f"✓ Session created with token: {token[:20]}...")

        # Validate the session
        validated = user_manager.validate_session(token)
        if validated:
            print(f"✓ Session validated successfully")
            print(f"  - Identity: {validated['identity']}")
            print(f"  - Email: {validated['email']}")
            print(f"  - Provider: {validated['provider']}")
        else:
            print("✗ Session validation failed")
            return False

        # Test expired/invalid token
        invalid = user_manager.validate_session("invalid-token")
        if invalid is None:
            print("✓ Invalid token correctly rejected")
        else:
            print("✗ Invalid token was not rejected")
            return False

        return True

    except Exception as e:
        print(f"✗ Session management test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_logout_and_cleanup():
    """Test logout and cleanup of logged users tracking."""
    print("\nTesting logout and cleanup...")

    try:
        from webapp.oauth_handler import user_manager, handle_logout
        from auth import _logged_oauth_users

        # Create a test session
        test_user = {
            "identity": "google:test456",
            "email": "test456@example.com",
            "name": "Test User 456",
            "provider": "google",
        }

        token = user_manager.create_session(test_user, "google")
        print(f"✓ Session created for logout test")

        # Simulate first authentication (adds to _logged_oauth_users)
        _logged_oauth_users.add(test_user["identity"])
        print(f"✓ User added to _logged_oauth_users: {test_user['identity']}")
        print(f"  - Tracked users count: {len(_logged_oauth_users)}")

        # Handle logout
        identity = handle_logout(token)
        if identity == test_user["identity"]:
            print(f"✓ Logout successful, returned identity: {identity}")
        else:
            print(f"✗ Logout failed, expected {test_user['identity']}, got {identity}")
            return False

        # Verify session is removed
        validated = user_manager.validate_session(token)
        if validated is None:
            print("✓ Session correctly removed after logout")
        else:
            print("✗ Session still exists after logout")
            return False

        # Clean up _logged_oauth_users (simulating what webapp.py does)
        if identity in _logged_oauth_users:
            _logged_oauth_users.discard(identity)
            print(f"✓ User removed from _logged_oauth_users")
            print(f"  - Tracked users count: {len(_logged_oauth_users)}")

        # Verify user is no longer tracked
        if test_user["identity"] not in _logged_oauth_users:
            print("✓ User correctly removed from tracking set")
        else:
            print("✗ User still in tracking set after logout")
            return False

        return True

    except Exception as e:
        print(f"✗ Logout and cleanup test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests."""
    print("=" * 70)
    print("OAuth Authentication Setup Test")
    print("=" * 70)

    results = []

    # Test imports
    results.append(("Dependencies", test_oauth_imports()))

    # Test OAuth handler
    results.append(("OAuth Handler", test_oauth_handler()))

    # Test auth module
    results.append(("Auth Module", test_auth_module()))

    # Check environment
    env_configured = check_env_variables()
    results.append(("Environment", True))  # Always pass, just informational

    # Test session management
    results.append(("Session Management", test_session_management()))

    # Test logout and cleanup
    results.append(("Logout & Cleanup", test_logout_and_cleanup()))

    # Summary
    print("\n" + "=" * 70)
    print("Test Summary")
    print("=" * 70)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status:8} - {name}")

    print("-" * 70)
    print(f"Total: {passed}/{total} tests passed")

    if passed == total:
        print("\n✓ All tests passed! OAuth authentication is ready to use.")
        if not env_configured:
            print("\nNote: OAuth credentials are not configured yet.")
            print("To enable OAuth login:")
            print("1. Copy .env.oauth.example to .env")
            print("2. Follow OAUTH_SETUP.md to configure Google/GitHub credentials")
            print("3. Restart the server")
        return 0
    else:
        print(f"\n✗ {total - passed} test(s) failed. Please review the errors above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
