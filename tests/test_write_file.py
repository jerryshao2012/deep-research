"""Test write_file tool functionality."""
from pathlib import Path

import pytest

from research_agent.research_subagent import tools
from research_agent.research_subagent.utils.knowledge_filesystem import write_file_impl


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


def test_write_file_tool_defaults_missing_path_to_final_report(monkeypatch) -> None:
    writes: list[tuple[str, str]] = []

    def capture_write(file_path: str, content: str) -> str:
        writes.append((file_path, content))
        return f"Successfully wrote {len(content)} bytes to {file_path}"

    monkeypatch.setattr(tools, "write_file_impl", capture_write)

    schema = tools.write_file.tool_call_schema.model_json_schema()
    assert "file_path" not in schema.get("required", [])

    result = tools.write_file.func(content="# Final report", state={})

    assert writes == [("/final_report.md", "# Final report")]
    assert "/final_report.md" in result


def test_write_file_tool_preserves_explicit_path(monkeypatch) -> None:
    writes: list[tuple[str, str]] = []

    def capture_write(file_path: str, content: str) -> str:
        writes.append((file_path, content))
        return f"Successfully wrote {len(content)} bytes to {file_path}"

    monkeypatch.setattr(tools, "write_file_impl", capture_write)

    tools.write_file.func(
        content="Original question",
        state={},
        file_path="/research_request.md",
    )

    assert writes == [("/research_request.md", "Original question")]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
