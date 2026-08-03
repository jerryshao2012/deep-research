#!/usr/bin/env python3
"""Check Clean Architecture dependency rules."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

FRAMEWORK_IMPORTS = (
    "azure",
    "boto3",
    "fastapi",
    "httpx",
    "langchain",
    "langgraph",
    "psycopg",
    "starlette",
)
LAYERS = {"domain", "application", "interfaces", "infrastructure"}


def _python_files(root_dir: Path) -> list[Path]:
    ignored = {".git", ".venv", ".worktrees", "__pycache__", "output", "tests"}
    return sorted(
        path
        for path in root_dir.rglob("*.py")
        if not ignored.intersection(path.relative_to(root_dir).parts)
    )


def _module_name(root_dir: Path, file_path: Path) -> str:
    relative = file_path.relative_to(root_dir).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _owner(root_dir: Path, file_path: Path) -> tuple[str, str, str] | None:
    parts = file_path.relative_to(root_dir).parts
    try:
        index = parts.index("features")
    except ValueError:
        return None
    if len(parts) <= index + 2 or parts[index + 2] not in LAYERS:
        return None
    package = ".".join(parts[:index])
    return package, parts[index + 1], parts[index + 2]


def _imports(tree: ast.AST) -> list[tuple[str, tuple[str, ...]]]:
    imports: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append((node.module, tuple(alias.name for alias in node.names)))
    return imports


def _cycle_violations(
        graph: dict[str, set[str]], module_paths: dict[str, Path], root_dir: Path
) -> list[dict[str, str]]:
    state: dict[str, str] = {}
    stack: list[str] = []
    cycles: set[str] = set()

    def visit(module: str) -> None:
        state[module] = "visiting"
        stack.append(module)
        for dependency in graph.get(module, set()):
            if state.get(dependency) == "visiting":
                start = stack.index(dependency)
                cycles.add(" -> ".join([*stack[start:], dependency]))
            elif dependency not in state:
                visit(dependency)
        stack.pop()
        state[module] = "visited"

    for module in graph:
        if module not in state:
            visit(module)
    return [
        {
            "rule": "dependency-cycle",
            "file": str(module_paths[cycle.split(" -> ", maxsplit=1)[0]].relative_to(root_dir)),
            "detail": cycle,
        }
        for cycle in sorted(cycles)
    ]


def check_architecture(root_dir: Path | None = None) -> list[dict[str, str]]:
    """Return architecture violations."""
    root = (root_dir or Path.cwd()).resolve()
    files = _python_files(root)
    module_paths = {_module_name(root, file_path): file_path for file_path in files}
    violations: list[dict[str, str]] = []
    graph: dict[str, set[str]] = {}

    for file_path in files:
        module = _module_name(root, file_path)
        owner = _owner(root, file_path)
        if owner is None:
            continue
        package, feature, layer = owner
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        dependencies: set[str] = set()

        for imported, names in _imports(tree):
            if layer == "domain" and imported.startswith(FRAMEWORK_IMPORTS):
                violations.append(
                    {
                        "rule": "domain-framework-import",
                        "file": str(file_path.relative_to(root)),
                        "detail": imported,
                    }
                )

            imported_parts = imported.split(".")
            if "features" in imported_parts:
                index = imported_parts.index("features")
                if len(imported_parts) > index + 2:
                    target_feature = imported_parts[index + 1]
                    target_layer = imported_parts[index + 2]
                    if target_feature != feature and target_layer in LAYERS:
                        violations.append(
                            {
                                "rule": "cross-feature-internal-import",
                                "file": str(file_path.relative_to(root)),
                                "detail": imported,
                            }
                        )
                    if (
                            target_feature == feature
                            and layer == "application"
                            and target_layer in {"interfaces", "infrastructure"}
                    ):
                        violations.append(
                            {
                                "rule": "application-outward-import",
                                "file": str(file_path.relative_to(root)),
                                "detail": imported,
                            }
                        )
                    if (
                            target_feature == feature
                            and layer == "domain"
                            and target_layer != "domain"
                    ):
                        violations.append(
                            {
                                "rule": "domain-outward-import",
                                "file": str(file_path.relative_to(root)),
                                "detail": imported,
                            }
                        )

            candidates = [imported, *(f"{imported}.{name}" for name in names)]
            dependencies.update(candidate for candidate in candidates if candidate in module_paths)

        graph[module] = dependencies

    return [*violations, *_cycle_violations(graph, module_paths, root)]


if __name__ == "__main__":
    found = check_architecture()
    for violation in found:
        sys.stderr.write(
            f"{violation['rule']}: {violation['file']}: {violation['detail']}\n"
        )
    raise SystemExit(1 if found else 0)
