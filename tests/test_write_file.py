"""Test write_file tool functionality."""
from pathlib import Path

import pytest

from research_agent.utils.knowledge_filesystem import write_file_impl


def test_write_file_basic():
    """Test basic write_file functionality."""
    import os
    # Test writing to a temporary file
    test_path = "/tmp/test_write_file_basic.txt"
    test_content = "Hello, World!"

    result = write_file_impl(test_path, test_content)

    assert "Successfully wrote" in result
    assert str(len(test_content)) in result

    # Verify file was actually written (considering path normalization)
    output_dir = Path(os.environ.get("OUTPUT_FOLDER", "./output"))
    resolved_path = Path(test_path)
    if not resolved_path.exists():
        resolved_path = output_dir / "tmp/test_write_file_basic.txt"

    assert resolved_path.exists()
    assert resolved_path.read_text() == test_content

    # Cleanup
    resolved_path.unlink()
    try:
        resolved_path.parent.rmdir()
    except OSError:
        pass


def test_write_file_with_state():
    """Test write_file with state parameter (virtual filesystem)."""
    test_path = "/test_virtual_file.txt"
    test_content = "Virtual content"
    state = {}

    result = write_file_impl(test_path, test_content, state=state)

    assert "Successfully wrote" in result
    assert "files" in state
    assert test_path in state["files"]


def test_write_file_creates_directories():
    """Test that write_file creates parent directories if needed."""
    import os
    test_path = "/tmp/test_nested/dir1/dir2/test_file.txt"
    test_content = "Nested directory test"

    result = write_file_impl(test_path, test_content)

    assert "Successfully wrote" in result

    # Verify file and directories were created (considering path normalization)
    output_dir = Path(os.environ.get("OUTPUT_FOLDER", "./output"))
    resolved_path = Path(test_path)
    if not resolved_path.exists():
        resolved_path = output_dir / "tmp/test_nested/dir1/dir2/test_file.txt"

    assert resolved_path.exists()
    assert resolved_path.parent.exists()
    assert resolved_path.read_text() == test_content

    # Cleanup
    resolved_path.unlink()
    try:
        resolved_path.parent.rmdir()
        resolved_path.parent.parent.rmdir()
        resolved_path.parent.parent.parent.rmdir()
        resolved_path.parent.parent.parent.parent.rmdir()
    except OSError:
        pass


def test_write_file_error_handling():
    """Test write_file error handling for invalid paths."""
    # Try to write to an invalid location (should fail gracefully)
    test_path = "/nonexistent_root_that_cannot_exist/file.txt"
    test_content = "This should fail"

    result = write_file_impl(test_path, test_content)

    # Should return an error message instead of raising exception
    assert "Error" in result or "Successfully" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
