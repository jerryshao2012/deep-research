#!/usr/bin/env python3
"""Test script for Document Upload API."""

from pathlib import Path

import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))


def test_upload_api():
    """Test the upload API endpoints."""
    from fastapi.testclient import TestClient
    from webapp import app
    from webapp import config as _cfg
    API_KEY = _cfg.API_KEY

    client = TestClient(app)

    print("🧪 Testing Document Upload API")
    print("=" * 60)

    # Test 1: Health endpoint (no auth required)
    print("\n1. Testing /health endpoint...")
    response = client.get("/health")
    assert response.status_code == 200, f"Health check failed: {response.status_code}"
    data = response.json()
    print(f"   ✅ Status: {data['status']}")
    print(f"   ✅ Free space: {data['free_space_human']}")

    # Test 2: Upload without API key (should fail)
    print("\n2. Testing upload without API key (should fail)...")
    response = client.post(
        "/documents/upload",
        data={"folder": "policy"},
        files=[("files", ("dummy.txt", b"x", "text/plain"))],
    )
    assert response.status_code == 401, "Should reject requests without API key"
    print(f"   ✅ Correctly rejected with 401")

    # Test 3: Upload with wrong API key (should fail)
    print("\n3. Testing upload with wrong API key (should fail)...")
    response = client.post(
        "/documents/upload",
        headers={"X-API-Key": "wrong_key"},
        data={"folder": "policy"},
        files=[("files", ("dummy.txt", b"x", "text/plain"))],
    )
    assert response.status_code == 401, "Should reject requests with wrong API key"
    print(f"   ✅ Correctly rejected with 401")

    # Test 4: Storage info without API key (should fail)
    print("\n4. Testing /storage/info without API key (should fail)...")
    response = client.get("/storage/info")
    assert response.status_code == 401, "Should reject storage info without API key"
    print(f"   ✅ Correctly rejected with 401")

    # Test 5: Storage info with API key (should succeed)
    print("\n5. Testing /storage/info with API key...")
    response = client.get(
        "/storage/info",
        headers={"X-API-Key": API_KEY}
    )
    assert response.status_code == 200, f"Storage info failed: {response.status_code}"
    data = response.json()
    storage = data["storage"]
    print(f"   ✅ Total space: {storage['total_space_human']}")
    print(f"   ✅ Used space: {storage['used_space_human']}")
    print(f"   ✅ Free space: {storage['free_space_human']}")
    print(f"   ✅ Usage: {storage['usage_percentage']}%")

    # Test 6: Create a test file and upload it
    print("\n6. Testing file upload...")
    test_file_path = Path("test_upload.txt")
    test_file_path.write_text("This is a test file for upload testing.")

    try:
        with open(test_file_path, "rb") as f:
            files = {"files": ("test_upload.txt", f, "text/plain")}
            response = client.post(
                "/documents/upload",
                headers={"X-API-Key": API_KEY},
                data={"folder": "policy"},
                files=files
            )

        assert response.status_code == 201, f"Upload failed: {response.status_code}"
        data = response.json()
        print(f"   ✅ Uploaded {data['count']} file(s)")
        print(f"   ✅ Folder: {data['folder']}")
        print(f"   ✅ File size: {data['saved'][0]['size']} bytes")
        print(f"   ✅ Free space after upload: {data['free_space_human']}")

        # Verify file was created
        uploaded_path = Path("docs/policy/test_upload.txt")
        assert uploaded_path.exists(), "Uploaded file should exist"
        print(f"   ✅ File exists at: {uploaded_path}")

        # Clean up
        uploaded_path.unlink()
        print(f"   ✅ Cleaned up test file")

    finally:
        # Clean up test file if it still exists
        if test_file_path.exists():
            test_file_path.unlink()

    print("\n" + "=" * 60)
    print("✅ All tests passed!")
    print("=" * 60)


if __name__ == "__main__":
    test_upload_api()
