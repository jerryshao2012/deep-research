from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from thread_wiki.git_import import (
    GitImportError,
    import_public_git_repository,
    validate_git_ref,
    validate_git_repo_url,
)
from thread_wiki.routes import WikiGitImportRequest, WikiGitImportResponse


def test_validates_supported_public_https_repository_urls() -> None:
    parsed = validate_git_repo_url("https://github.com/openai/openai-python.git")

    assert parsed.host == "github.com"
    assert parsed.slug == "github.com-openai-openai-python"
    assert parsed.url == "https://github.com/openai/openai-python.git"


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:openai/openai-python.git",
        "http://github.com/openai/openai-python.git",
        "https://user:token@github.com/openai/openai-python.git",
        "https://example.com/owner/repo.git",
        "https://github.com/owner/../repo.git",
        "https://github.com/owner/repo.git?token=secret",
        "https://github.com:8443/owner/repo.git",
    ],
)
def test_rejects_unsafe_or_unsupported_repository_urls(url: str) -> None:
    with pytest.raises(GitImportError):
        validate_git_repo_url(url)


@pytest.mark.parametrize("ref", ["main", "release/v1.2", "feature_ast-1"])
def test_validates_branch_or_tag_ref(ref: str) -> None:
    assert validate_git_ref(ref) == ref


@pytest.mark.parametrize("ref", ["--upload-pack=evil", "../main", "main@{1}", "a b"])
def test_rejects_unsafe_git_ref(ref: str) -> None:
    with pytest.raises(GitImportError):
        validate_git_ref(ref)


def test_import_clones_without_execution_and_removes_git_metadata(
        tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    def fake_runner(
            command: list[str],
            *,
            capture_output: bool,
            text: bool,
            timeout: int,
            env: dict[str, str],
            check: bool,
    ) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed["env"] = env
        clone_dir = Path(command[-1])
        (clone_dir / ".git").mkdir(parents=True)
        (clone_dir / "src").mkdir()
        (clone_dir / "src" / "app.py").write_text(
            "def run():\n    return 1\n",
            encoding="utf-8",
        )
        (clone_dir / "README.md").write_text("# Example\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = import_public_git_repository(
        tmp_path,
        "https://github.com/example/project.git",
        ref="main",
        runner=fake_runner,
    )

    command = observed["command"]
    assert isinstance(command, list)
    assert command[:3] == ["git", "-c", "protocol.file.allow=never"]
    assert "--depth" in command
    assert "--no-recurse-submodules" in command
    assert "--branch" in command
    assert observed["env"]["GIT_TERMINAL_PROMPT"] == "0"  # type: ignore[index]
    assert observed["env"]["GIT_LFS_SKIP_SMUDGE"] == "1"  # type: ignore[index]
    assert result.file_count == 2
    assert result.destination == (
            tmp_path / "repositories" / "github.com-example-project"
    )
    assert (result.destination / "src" / "app.py").is_file()
    assert not (result.destination / ".git").exists()


def test_import_enforces_repository_limits_before_install(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIKI_GIT_IMPORT_MAX_FILES", "1")

    def fake_runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        clone_dir = Path(command[-1])
        clone_dir.mkdir(parents=True)
        (clone_dir / "one.py").write_text("one", encoding="utf-8")
        (clone_dir / "two.py").write_text("two", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(GitImportError, match="file limit"):
        import_public_git_repository(
            tmp_path,
            "https://gitlab.com/example/project.git",
            runner=fake_runner,
        )

    assert not (tmp_path / "repositories" / "gitlab.com-example-project").exists()


def test_git_import_api_models_are_additive() -> None:
    request = WikiGitImportRequest(url="https://github.com/example/project.git")
    response = WikiGitImportResponse(
        thread_id="thread-1",
        status="started",
        message="queued",
        repository_url=request.url,
        ref=None,
    )

    assert request.ref is None
    assert response.repository_url == request.url
