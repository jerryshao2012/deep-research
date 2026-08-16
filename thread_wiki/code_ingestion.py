"""AST-aware source-code ingestion for the thread wiki.

Tree-sitter produces concrete syntax trees. This module normalizes selected
nodes into a small, language-independent semantic model suitable for LLM wiki
ingestion. Uploaded code is parsed only; it is never imported or executed.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from tree_sitter import Language, Node, Parser

from .source_types import CODE_EXTENSION_LANGUAGES
from .source_types import SUPPORTED_CODE_SUFFIXES as SUPPORTED_CODE_SUFFIXES

_DEFAULT_MAX_BYTES = 2_097_152
_DEFAULT_MAX_CHARS = 40_000
_MANIFEST_NAME = ".code_ingest_manifest.json"
_ARTIFACT_DIR_NAME = "_code"
_PUBLIC_WARNING_LIMIT = 50


@dataclass(slots=True, frozen=True)
class CodeDetection:
    """Detected source language and the deterministic detection method."""

    language: str
    method: str


@dataclass(slots=True, frozen=True)
class EmbeddedCodeBlock:
    """Explicit language-tagged code fence inside an ordinary documents."""

    source_path: str
    language: str
    block_index: int
    start_line: int
    end_line: int
    code: str


@dataclass(slots=True, frozen=True)
class CodeWarning:
    """Safe, stable warning emitted while analyzing one source file."""

    source_path: str
    code: str
    message: str


@dataclass(slots=True)
class CodeImport:
    """Normalized import/include/use declaration."""

    module: str
    names: list[str] = field(default_factory=list)
    line: int = 1
    raw: str = ""
    resolved_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the import for manifests."""
        return asdict(self)


@dataclass(slots=True)
class CodeUnit:
    """One semantic code unit backed by an exact source range."""

    unit_id: str
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    code: str
    signature: str = ""
    documentation: str = ""
    parent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the unit without changing source text."""
        return asdict(self)


@dataclass(slots=True)
class CodeFileAnalysis:
    """Normalized code intelligence for one source file."""

    source_path: str
    language: str
    detection_method: str
    status: str
    sha256: str = ""
    size_bytes: int = 0
    parser_version: str | None = None
    grammar_version: str | None = None
    units: list[CodeUnit] = field(default_factory=list)
    imports: list[CodeImport] = field(default_factory=list)
    warnings: list[CodeWarning] = field(default_factory=list)
    origin_kind: str = "file"
    origin_line_start: int | None = None
    origin_line_end: int | None = None
    block_index: int | None = None

    @classmethod
    def fallback(
            cls,
            *,
            source_path: str,
            language: str,
            detection_method: str,
            warning_code: str,
            message: str,
            sha256: str = "",
            size_bytes: int = 0,
    ) -> CodeFileAnalysis:
        """Build a text-fallback result with one stable warning."""
        return cls(
            source_path=source_path,
            language=language,
            detection_method=detection_method,
            status="fallback",
            sha256=sha256,
            size_bytes=size_bytes,
            warnings=[
                CodeWarning(
                    source_path=source_path,
                    code=warning_code,
                    message=message,
                )
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize analysis for the persistent manifest."""
        return {
            "source_path": self.source_path,
            "language": self.language,
            "detection_method": self.detection_method,
            "status": self.status,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "parser_version": self.parser_version,
            "grammar_version": self.grammar_version,
            "units": [unit.to_dict() for unit in self.units],
            "imports": [item.to_dict() for item in self.imports],
            "warnings": [asdict(item) for item in self.warnings],
            "origin_kind": self.origin_kind,
            "origin_line_start": self.origin_line_start,
            "origin_line_end": self.origin_line_end,
            "block_index": self.block_index,
        }


@dataclass(slots=True)
class RepositoryIndex:
    """Deterministic repository-level symbol and import summary."""

    symbols: list[dict[str, Any]]
    internal_imports: list[dict[str, Any]]

    @property
    def symbol_count(self) -> int:
        """Return number of indexed semantic symbols."""
        return len(self.symbols)

    @property
    def internal_import_count(self) -> int:
        """Return number of imports resolved to uploaded code."""
        return len(self.internal_imports)

    def to_dict(self) -> dict[str, Any]:
        """Serialize repository index."""
        return {
            "symbol_count": self.symbol_count,
            "internal_import_count": self.internal_import_count,
            "symbols": self.symbols,
            "internal_imports": self.internal_imports,
        }


_FENCE_LANGUAGES: dict[str, str] = {
    "python": "python",
    "py": "python",
    "javascript": "javascript",
    "js": "javascript",
    "jsx": "javascript",
    "typescript": "typescript",
    "ts": "typescript",
    "tsx": "tsx",
    "java": "java",
    "go": "go",
    "golang": "go",
    "rust": "rust",
    "rs": "rust",
    "c": "c",
    "cpp": "cpp",
    "c++": "cpp",
    "csharp": "c_sharp",
    "c#": "c_sharp",
    "cs": "c_sharp",
    "ruby": "ruby",
    "rb": "ruby",
    "php": "php",
}

_SHEBANG_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bpython(?:[23](?:\.\d+)?)?\b", re.I), "python"),
    (re.compile(r"\b(?:node|nodejs|deno|bun)\b", re.I), "javascript"),
    (re.compile(r"\bruby\b", re.I), "ruby"),
    (re.compile(r"\bphp\b", re.I), "php"),
)

_GRAMMARS: dict[str, tuple[str, tuple[str, ...]]] = {
    "python": ("tree_sitter_python", ("language",)),
    "javascript": ("tree_sitter_javascript", ("language",)),
    "typescript": ("tree_sitter_typescript", ("language_typescript",)),
    "tsx": ("tree_sitter_typescript", ("language_tsx",)),
    "java": ("tree_sitter_java", ("language",)),
    "go": ("tree_sitter_go", ("language",)),
    "rust": ("tree_sitter_rust", ("language",)),
    "c": ("tree_sitter_c", ("language",)),
    "cpp": ("tree_sitter_cpp", ("language",)),
    "c_sharp": ("tree_sitter_c_sharp", ("language",)),
    "ruby": ("tree_sitter_ruby", ("language",)),
    "php": ("tree_sitter_php", ("language_php", "language_php_only")),
}

_NODE_KINDS: dict[str, str] = {
    # Classes and type containers.
    "class": "class",
    "class_declaration": "class",
    "class_definition": "class",
    "class_specifier": "class",
    "struct_item": "struct",
    "struct_declaration": "struct",
    "struct_specifier": "struct",
    "interface_declaration": "interface",
    "interface_type": "interface",
    "trait_declaration": "trait",
    "trait_item": "trait",
    "module": "module",
    "module_declaration": "module",
    "module_definition": "module",
    "namespace_definition": "namespace",
    "namespace_declaration": "namespace",
    "enum_declaration": "enum",
    "enum_item": "enum",
    "enum_specifier": "enum",
    "record_declaration": "record",
    "union_specifier": "union",
    # Callable units.
    "function_declaration": "function",
    "function_definition": "function",
    "function_item": "function",
    "generator_function_declaration": "function",
    "method": "method",
    "method_declaration": "method",
    "method_definition": "method",
    "singleton_method": "method",
    "constructor_declaration": "constructor",
    "init_declaration": "constructor",
    # Constants.
    "const_declaration": "constant",
    "const_item": "constant",
    "static_item": "constant",
}

_CONTAINER_KINDS = {
    "class",
    "struct",
    "interface",
    "trait",
    "record",
    "enum",
    "union",
    "module",
    "namespace",
}

_CALLABLE_KINDS = {"function", "method", "constructor"}

_IMPORT_NODE_TYPES = {
    "import_declaration",
    "import_from_statement",
    "import_statement",
    "namespace_use_declaration",
    "preproc_include",
    "use_declaration",
    "using_directive",
}


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return max(1, int(value))
    except ValueError:
        return default


def detect_code_language(path: Path, prefix: bytes | None = None) -> CodeDetection | None:
    """Detect supported source code by extension, then extensionless shebang."""
    suffix = path.suffix.lower()
    language = CODE_EXTENSION_LANGUAGES.get(suffix)
    if language is not None:
        return CodeDetection(language=language, method="extension")
    if suffix:
        return None

    if prefix is None:
        try:
            prefix = path.read_bytes()[:256]
        except OSError:
            return None
    first_line = prefix.splitlines()[0].decode("utf-8", errors="ignore") if prefix else ""
    if not first_line.startswith("#!"):
        return None
    for pattern, shebang_language in _SHEBANG_PATTERNS:
        if pattern.search(first_line):
            return CodeDetection(language=shebang_language, method="shebang")
    return None


def extract_embedded_code_blocks(
        path: Path,
        *,
        root: Path | None = None,
) -> list[EmbeddedCodeBlock]:
    """Extract only explicit supported language-tagged Markdown code fences."""
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return []
    source_path = _relative_source_paths([path], root)[path]
    lines = content.splitlines()
    opening_re = re.compile(
        r"^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+#.-]+)\s*$"
    )
    blocks: list[EmbeddedCodeBlock] = []
    index = 0
    cursor = 0
    while cursor < len(lines):
        opening = opening_re.match(lines[cursor])
        if opening is None:
            cursor += 1
            continue
        marker = opening.group(1)
        language = _FENCE_LANGUAGES.get(opening.group(2).lower())
        closing_re = re.compile(
            rf"^\s*{re.escape(marker[0])}{{{len(marker)},}}\s*$"
        )
        closing = cursor + 1
        while closing < len(lines) and closing_re.match(lines[closing]) is None:
            closing += 1
        if closing >= len(lines):
            break
        code_lines = lines[cursor + 1:closing]
        if language is not None and any(line.strip() for line in code_lines):
            index += 1
            blocks.append(
                EmbeddedCodeBlock(
                    source_path=source_path,
                    language=language,
                    block_index=index,
                    start_line=cursor + 2,
                    end_line=closing,
                    code="\n".join(code_lines),
                )
            )
        cursor = closing + 1
    return blocks


def _package_version(module_name: str) -> str | None:
    distribution = module_name.replace("_", "-")
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def _load_parser(language_name: str) -> tuple[Parser, str | None]:
    module_name, factories = _GRAMMARS[language_name]
    module = importlib.import_module(module_name)
    factory = next(
        (getattr(module, name) for name in factories if hasattr(module, name)),
        None,
    )
    if factory is None:
        raise RuntimeError(f"No grammar factory found for {language_name}")
    language = Language(factory())
    return Parser(language), _package_version(module_name)


def _node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _walk(node: Node) -> Iterable[Node]:
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _error_score(root: Node) -> tuple[int, int]:
    errors = 0
    missing = 0
    for node in _walk(root):
        errors += int(node.is_error)
        missing += int(node.is_missing)
    return errors, missing


def _first_named_descendant(
        node: Node, allowed_types: set[str], *, skip_root: bool = True
) -> Node | None:
    for candidate in _walk(node):
        if skip_root and candidate.id == node.id:
            continue
        if candidate.type in allowed_types:
            return candidate
    return None


def _name_node(node: Node) -> Node | None:
    for field_name in ("name", "declarator", "type"):
        candidate = node.child_by_field_name(field_name)
        if candidate is None:
            continue
        if field_name == "declarator" and candidate.type not in {
            "identifier",
            "field_identifier",
            "property_identifier",
            "type_identifier",
            "constant",
        }:
            nested = _first_named_descendant(
                candidate,
                {
                    "identifier",
                    "field_identifier",
                    "property_identifier",
                    "type_identifier",
                    "constant",
                },
                skip_root=False,
            )
            if nested is not None:
                return nested
        return candidate
    return _first_named_descendant(
        node,
        {
            "constant",
            "field_identifier",
            "identifier",
            "namespace_identifier",
            "property_identifier",
            "type_identifier",
        },
    )


def _semantic_kind(node: Node, *, inside_callable: bool) -> str | None:
    if node.parent is None:
        return None
    kind = _NODE_KINDS.get(node.type)
    if kind in _CALLABLE_KINDS and inside_callable:
        return None
    return kind


def _unit_name(node: Node, source: bytes, kind: str) -> str:
    candidate = _name_node(node)
    if candidate is not None:
        name = _node_text(candidate, source).strip()
        if name:
            return re.sub(r"\s+", " ", name)
    if kind == "constructor":
        return "__init__"
    return f"<anonymous-{kind}>"


def _unit_signature(node: Node, source: bytes) -> str:
    body = node.child_by_field_name("body")
    end_byte = body.start_byte if body is not None else node.end_byte
    prefix = source[node.start_byte:end_byte].decode("utf-8", errors="replace").strip()
    if not prefix:
        prefix = _node_text(node, source).splitlines()[0].strip()
    return re.sub(r"\s+", " ", prefix).rstrip("{:").strip()


def _unit_documentation(node: Node, source: bytes) -> str:
    body = node.child_by_field_name("body")
    if body is None:
        return ""
    for child in body.named_children[:3]:
        if child.type == "comment":
            return _node_text(child, source).strip()
        if child.type == "expression_statement":
            string_node = _first_named_descendant(
                child,
                {"string", "string_content", "string_literal"},
                skip_root=False,
            )
            if string_node is not None:
                return _node_text(string_node, source).strip().strip("\"'")
    return ""


def _stable_unit_id(
        source_path: str,
        kind: str,
        qualified_name: str,
        start_line: int,
        end_line: int,
) -> str:
    payload = f"{source_path}\0{kind}\0{qualified_name}\0{start_line}\0{end_line}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _extract_units(root: Node, source: bytes, source_path: str) -> list[CodeUnit]:
    units: list[CodeUnit] = []

    def visit(
            node: Node,
            parent_unit: CodeUnit | None,
            *,
            inside_callable: bool,
    ) -> None:
        kind = _semantic_kind(node, inside_callable=inside_callable)
        current_parent = parent_unit
        next_inside_callable = inside_callable
        if kind is not None:
            name = _unit_name(node, source, kind)
            qualified = (
                f"{parent_unit.qualified_name}.{name}" if parent_unit else name
            )
            if kind == "function" and parent_unit and parent_unit.kind in _CONTAINER_KINDS:
                kind = "method"
            start_line = node.start_point.row + 1
            end_line = max(start_line, node.end_point.row + 1)
            unit = CodeUnit(
                unit_id=_stable_unit_id(
                    source_path, kind, qualified, start_line, end_line
                ),
                kind=kind,
                name=name,
                qualified_name=qualified,
                start_line=start_line,
                end_line=end_line,
                code=_node_text(node, source).rstrip(),
                signature=_unit_signature(node, source),
                documentation=_unit_documentation(node, source),
                parent_id=parent_unit.unit_id if parent_unit else None,
            )
            units.append(unit)
            current_parent = unit
            next_inside_callable = kind in _CALLABLE_KINDS

        for child in node.named_children:
            visit(
                child,
                current_parent,
                inside_callable=next_inside_callable,
            )

    visit(root, None, inside_callable=False)
    if not units and source.strip():
        text = source.decode("utf-8", errors="replace").rstrip()
        end_line = max(1, text.count("\n") + 1)
        units.append(
            CodeUnit(
                unit_id=_stable_unit_id(source_path, "module", source_path, 1, end_line),
                kind="module",
                name=PurePosixPath(source_path).name,
                qualified_name=PurePosixPath(source_path).name,
                start_line=1,
                end_line=end_line,
                code=text,
                signature=f"module {PurePosixPath(source_path).name}",
            )
        )
    return sorted(units, key=lambda unit: (unit.start_line, unit.end_line, unit.unit_id))


def _import_modules(language: str, raw: str) -> list[tuple[str, list[str]]]:
    stripped = raw.strip()
    modules: list[tuple[str, list[str]]] = []
    if language == "python":
        match = re.match(r"from\s+([\w.]+)\s+import\s+(.+)", stripped)
        if match:
            names = [
                item.strip().split(" as ", 1)[0]
                for item in match.group(2).strip("()").split(",")
            ]
            return [(match.group(1), [item for item in names if item])]
        match = re.match(r"import\s+(.+)", stripped)
        if match:
            return [
                (item.strip().split(" as ", 1)[0], [])
                for item in match.group(1).split(",")
                if item.strip()
            ]
    elif language in {"javascript", "typescript", "tsx"}:
        match = re.search(r"\bfrom\s+['\"]([^'\"]+)['\"]", stripped)
        if match:
            modules.append((match.group(1), []))
        else:
            match = re.search(r"\bimport\s*['\"]([^'\"]+)['\"]", stripped)
            if match:
                modules.append((match.group(1), []))
    elif language == "java":
        match = re.search(r"\bimport\s+(?:static\s+)?([\w.*]+)", stripped)
        if match:
            modules.append((match.group(1), []))
    elif language == "go":
        for module in re.findall(r'"([^"]+)"', stripped):
            modules.append((module, []))
    elif language == "rust":
        match = re.search(r"\buse\s+([^;]+)", stripped)
        if match:
            modules.append((match.group(1).strip(), []))
    elif language in {"c", "cpp"}:
        match = re.search(r"#\s*include\s*[<\"]([^>\"]+)[>\"]", stripped)
        if match:
            modules.append((match.group(1), []))
    elif language == "c_sharp":
        match = re.search(r"\busing\s+([\w.]+)", stripped)
        if match:
            modules.append((match.group(1), []))
    elif language == "php":
        match = re.search(r"\buse\s+([^;]+)", stripped)
        if match:
            modules.append((match.group(1).strip(), []))
    return modules


def _extract_imports(root: Node, source: bytes, language: str) -> list[CodeImport]:
    imports: list[CodeImport] = []
    seen: set[tuple[str, int]] = set()
    for node in _walk(root):
        if node.type not in _IMPORT_NODE_TYPES:
            continue
        raw = _node_text(node, source).strip()
        for module, names in _import_modules(language, raw):
            key = (module, node.start_point.row + 1)
            if key in seen:
                continue
            seen.add(key)
            imports.append(
                CodeImport(
                    module=module,
                    names=names,
                    line=node.start_point.row + 1,
                    raw=raw,
                )
            )

    text = source.decode("utf-8", errors="replace")
    if language == "ruby":
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = re.match(r"\s*require(?:_relative)?\s*[\(\s]['\"]([^'\"]+)", line)
            if match and (match.group(1), line_number) not in seen:
                imports.append(
                    CodeImport(
                        module=match.group(1),
                        line=line_number,
                        raw=line.strip(),
                    )
                )
    if language in {"javascript", "typescript", "tsx", "php"}:
        pattern = (
            r"\brequire(?:_once)?\s*\(?\s*['\"]([^'\"]+)['\"]"
            if language == "php"
            else r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
        )
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = re.search(pattern, line)
            if match and (match.group(1), line_number) not in seen:
                imports.append(
                    CodeImport(
                        module=match.group(1),
                        line=line_number,
                        raw=line.strip(),
                    )
                )
    return sorted(imports, key=lambda item: (item.line, item.module, item.raw))


def _relative_source_paths(
        paths: list[Path],
        root: Path | None = None,
) -> dict[Path, str]:
    if not paths:
        return {}
    if root is not None:
        common = root.resolve()
    else:
        parents = [str(path.parent.resolve()) for path in paths]
        try:
            common = Path(os.path.commonpath(parents))
        except ValueError:
            common = paths[0].parent.resolve()
    result: dict[Path, str] = {}
    for path in paths:
        try:
            result[path] = path.resolve().relative_to(common).as_posix()
        except ValueError:
            result[path] = path.name
    return result


def _analyze_with_language(
        *,
        source_path: str,
        detection: CodeDetection,
        language_name: str,
        source: bytes,
        sha256: str,
) -> tuple[CodeFileAnalysis, tuple[int, int]]:
    parser, grammar_version = _load_parser(language_name)
    tree = parser.parse(source)
    if tree is None:
        raise RuntimeError("Parser returned no syntax tree")
    score = _error_score(tree.root_node)
    units = _extract_units(tree.root_node, source, source_path)
    if not units:
        return (
            CodeFileAnalysis.fallback(
                source_path=source_path,
                language=language_name,
                detection_method=detection.method,
                warning_code="no_usable_units",
                message="AST parser produced no usable semantic units; using text chunks.",
                sha256=sha256,
                size_bytes=len(source),
            ),
            score,
        )
    warnings: list[CodeWarning] = []
    status = "parsed"
    if tree.root_node.has_error:
        status = "partial"
        warnings.append(
            CodeWarning(
                source_path=source_path,
                code="syntax_error",
                message=(
                    "Parser recovered from syntax errors; usable semantic units "
                    "were retained."
                ),
            )
        )
    return (
        CodeFileAnalysis(
            source_path=source_path,
            language=language_name,
            detection_method=detection.method,
            status=status,
            sha256=sha256,
            size_bytes=len(source),
            parser_version=_package_version("tree_sitter"),
            grammar_version=grammar_version,
            units=units,
            imports=_extract_imports(tree.root_node, source, language_name),
            warnings=warnings,
        ),
        score,
    )


def analyze_code_sources(
        paths: Iterable[Path],
        *,
        root: Path | None = None,
        enabled: bool | None = None,
        max_bytes: int | None = None,
) -> list[CodeFileAnalysis]:
    """Analyze recognized code paths without executing their contents."""
    path_list = sorted((Path(path) for path in paths), key=lambda item: item.as_posix())
    relative_paths = _relative_source_paths(path_list, root)
    ast_enabled = (
        _env_flag("WIKI_CODE_AST_ENABLED", True) if enabled is None else enabled
    )
    byte_limit = (
        _env_int("WIKI_CODE_PARSE_MAX_BYTES", _DEFAULT_MAX_BYTES)
        if max_bytes is None
        else max(1, max_bytes)
    )
    analyses: list[CodeFileAnalysis] = []

    for path in path_list:
        try:
            source = path.read_bytes()
        except OSError:
            continue
        detection = detect_code_language(path, source[:256])
        if detection is None:
            continue
        source_path = relative_paths[path]
        sha256 = hashlib.sha256(source).hexdigest()
        if not ast_enabled:
            analyses.append(
                CodeFileAnalysis.fallback(
                    source_path=source_path,
                    language=detection.language,
                    detection_method=detection.method,
                    warning_code="ast_disabled",
                    message="AST code ingestion is disabled; using text chunks.",
                    sha256=sha256,
                    size_bytes=len(source),
                )
            )
            continue
        if len(source) > byte_limit:
            analyses.append(
                CodeFileAnalysis.fallback(
                    source_path=source_path,
                    language=detection.language,
                    detection_method=detection.method,
                    warning_code="source_too_large",
                    message=(
                        f"Source exceeds AST parse limit of {byte_limit} bytes; "
                        "using text chunks."
                    ),
                    sha256=sha256,
                    size_bytes=len(source),
                )
            )
            continue
        if source.startswith(b"\xef\xbb\xbf"):
            source = source[3:]
        try:
            source.decode("utf-8")
        except UnicodeDecodeError:
            analyses.append(
                CodeFileAnalysis.fallback(
                    source_path=source_path,
                    language=detection.language,
                    detection_method=detection.method,
                    warning_code="unsupported_encoding",
                    message="Source is not UTF-8; using text ingestion fallback.",
                    sha256=sha256,
                    size_bytes=len(source),
                )
            )
            continue

        try:
            if detection.language == "c_or_cpp":
                c_analysis, c_score = _analyze_with_language(
                    source_path=source_path,
                    detection=detection,
                    language_name="c",
                    source=source,
                    sha256=sha256,
                )
                cpp_analysis, cpp_score = _analyze_with_language(
                    source_path=source_path,
                    detection=detection,
                    language_name="cpp",
                    source=source,
                    sha256=sha256,
                )
                analyses.append(c_analysis if c_score <= cpp_score else cpp_analysis)
            else:
                analysis, _score = _analyze_with_language(
                    source_path=source_path,
                    detection=detection,
                    language_name=detection.language,
                    source=source,
                    sha256=sha256,
                )
                analyses.append(analysis)
        except Exception as exc:
            analyses.append(
                CodeFileAnalysis.fallback(
                    source_path=source_path,
                    language=(
                        "c" if detection.language == "c_or_cpp" else detection.language
                    ),
                    detection_method=detection.method,
                    warning_code="parser_failure",
                    message=f"AST parser unavailable or failed ({type(exc).__name__}); using text chunks.",
                    sha256=sha256,
                    size_bytes=len(source),
                )
            )

    return sorted(analyses, key=lambda item: item.source_path)


def analyze_embedded_code_sources(
        paths: Iterable[Path],
        *,
        root: Path | None = None,
        enabled: bool | None = None,
        max_bytes: int | None = None,
) -> list[CodeFileAnalysis]:
    """Analyze supported explicit code fences while retaining documents origins."""
    embedded_enabled = (
        _env_flag("WIKI_CODE_AST_ENABLED", True)
        and _env_flag("WIKI_EMBEDDED_CODE_AST_ENABLED", True)
        if enabled is None
        else enabled
    )
    byte_limit = (
        _env_int("WIKI_CODE_PARSE_MAX_BYTES", _DEFAULT_MAX_BYTES)
        if max_bytes is None
        else max(1, max_bytes)
    )
    blocks = [
        block
        for path in sorted(
            (Path(item) for item in paths),
            key=lambda item: item.as_posix(),
        )
        for block in extract_embedded_code_blocks(path, root=root)
    ]
    analyses: list[CodeFileAnalysis] = []
    for block in blocks:
        source = block.code.encode("utf-8")
        sha256 = hashlib.sha256(source).hexdigest()
        detection = CodeDetection(
            language=block.language,
            method="embedded_fence",
        )
        if not embedded_enabled:
            analysis = CodeFileAnalysis.fallback(
                source_path=block.source_path,
                language=block.language,
                detection_method=detection.method,
                warning_code="embedded_ast_disabled",
                message=(
                    "Embedded-code AST processing is disabled; documents text "
                    "remains available."
                ),
                sha256=sha256,
                size_bytes=len(source),
            )
        elif len(source) > byte_limit:
            analysis = CodeFileAnalysis.fallback(
                source_path=block.source_path,
                language=block.language,
                detection_method=detection.method,
                warning_code="embedded_source_too_large",
                message=(
                    f"Embedded code exceeds AST parse limit of {byte_limit} "
                    "bytes; documents text remains available."
                ),
                sha256=sha256,
                size_bytes=len(source),
            )
        else:
            try:
                analysis, _score = _analyze_with_language(
                    source_path=(
                        f"{block.source_path}#embedded-{block.block_index}"
                    ),
                    detection=detection,
                    language_name=block.language,
                    source=source,
                    sha256=sha256,
                )
            except Exception as exc:
                analysis = CodeFileAnalysis.fallback(
                    source_path=block.source_path,
                    language=block.language,
                    detection_method=detection.method,
                    warning_code="embedded_parser_failure",
                    message=(
                        "Embedded-code parser unavailable or failed "
                        f"({type(exc).__name__}); documents text remains available."
                    ),
                    sha256=sha256,
                    size_bytes=len(source),
                )

        offset = block.start_line - 1
        analysis.source_path = block.source_path
        analysis.origin_kind = "embedded"
        analysis.origin_line_start = block.start_line
        analysis.origin_line_end = block.end_line
        analysis.block_index = block.block_index
        for unit in analysis.units:
            unit.start_line += offset
            unit.end_line += offset
        for item in analysis.imports:
            item.line += offset
        analysis.warnings = [
            CodeWarning(
                source_path=block.source_path,
                code=warning.code,
                message=warning.message,
            )
            for warning in analysis.warnings
        ]
        analyses.append(analysis)
    return sorted(
        analyses,
        key=lambda item: (
            item.source_path,
            item.block_index or 0,
        ),
    )


def _module_candidates(module: str) -> set[str]:
    normalized = module.strip().strip("\"'<>")
    normalized = re.sub(r"^(?:crate|self|super)::", "", normalized)
    normalized = normalized.replace("::", ".").replace("\\", ".").replace("/", ".")
    normalized = re.sub(r"^\.+", "", normalized)
    normalized = re.sub(r"\.(?:\*|\{.*})$", "", normalized)
    candidates = {normalized, normalized.rsplit(".", 1)[-1]}
    return {item for item in candidates if item}


def build_repository_index(analyses: list[CodeFileAnalysis]) -> RepositoryIndex:
    """Index symbols and resolve exact, unambiguous internal imports."""
    symbols: list[dict[str, Any]] = []
    path_keys: dict[str, set[str]] = {}
    for analysis in sorted(analyses, key=lambda item: item.source_path):
        if analysis.origin_kind != "file":
            continue
        pure = PurePosixPath(analysis.source_path)
        no_suffix = pure.with_suffix("").as_posix()
        keys = {
            no_suffix,
            no_suffix.replace("/", "."),
            pure.stem,
            pure.name,
        }
        for key in keys:
            path_keys.setdefault(key, set()).add(analysis.source_path)
        for unit in analysis.units:
            if unit.kind == "module":
                continue
            symbols.append(
                {
                    "source_path": analysis.source_path,
                    "unit_id": unit.unit_id,
                    "kind": unit.kind,
                    "name": unit.name,
                    "qualified_name": unit.qualified_name,
                    "start_line": unit.start_line,
                    "end_line": unit.end_line,
                }
            )

    internal_imports: list[dict[str, Any]] = []
    for analysis in sorted(analyses, key=lambda item: item.source_path):
        if analysis.origin_kind != "file":
            continue
        for item in analysis.imports:
            matches: set[str] = set()
            for candidate in _module_candidates(item.module):
                matches.update(path_keys.get(candidate, set()))
            matches.discard(analysis.source_path)
            if len(matches) != 1:
                item.resolved_source = None
                continue
            item.resolved_source = next(iter(matches))
            internal_imports.append(
                {
                    "source_path": analysis.source_path,
                    "line": item.line,
                    "module": item.module,
                    "resolved_source": item.resolved_source,
                }
            )

    return RepositoryIndex(
        symbols=sorted(
            symbols,
            key=lambda item: (
                item["source_path"],
                item["start_line"],
                item["qualified_name"],
            ),
        ),
        internal_imports=sorted(
            internal_imports,
            key=lambda item: (
                item["source_path"],
                item["line"],
                item["module"],
            ),
        ),
    )


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("._-")
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:80] or 'source'}-{digest}"


def _split_code_unit(unit: CodeUnit, max_chars: int) -> list[tuple[int, int, str]]:
    if len(unit.code) <= max_chars:
        return [(unit.start_line, unit.end_line, unit.code)]
    lines = unit.code.splitlines(keepends=True)
    chunks: list[tuple[int, int, str]] = []
    current: list[str] = []
    current_size = 0
    chunk_start = unit.start_line
    line_number = unit.start_line
    for line in lines:
        if current and current_size + len(line) > max_chars:
            chunks.append((chunk_start, line_number - 1, "".join(current).rstrip()))
            current = []
            current_size = 0
            chunk_start = line_number
        current.append(line)
        current_size += len(line)
        line_number += 1
    if current:
        chunks.append((chunk_start, max(chunk_start, line_number - 1), "".join(current).rstrip()))
    return chunks


def _render_unit_artifact(
        analysis: CodeFileAnalysis,
        unit: CodeUnit,
        *,
        start_line: int,
        end_line: int,
        code: str,
        part: int,
        total_parts: int,
) -> str:
    part_text = f"\n- Part: {part}/{total_parts}" if total_parts > 1 else ""
    parent = f"\n- Parent unit: `{unit.parent_id}`" if unit.parent_id else ""
    origin = (
        f"\n- Origin: embedded code block {analysis.block_index}"
        if analysis.origin_kind == "embedded"
        else ""
    )
    documentation = (
        f"\n\n## Documentation\n\n{unit.documentation}" if unit.documentation else ""
    )
    return (
        f"# {unit.qualified_name}\n\n"
        f"- Original source: `/raw/{analysis.source_path}`\n"
        f"- Language: `{analysis.language}`\n"
        f"- Kind: `{unit.kind}`\n"
        f"- Lines: {start_line}-{end_line}\n"
        f"- Unit ID: `{unit.unit_id}`"
        f"{parent}{origin}{part_text}\n"
        f"- Signature: `{unit.signature}`"
        f"{documentation}\n\n"
        f"## Original Code\n\n"
        f"```{analysis.language}\n{code}\n```\n"
    )


def _render_repository_index(index: RepositoryIndex) -> str:
    lines = [
        "# Code Repository Index",
        "",
        "Derived navigation aid. Cite original `/raw/` sources, never this file.",
        "",
        "## Symbols",
        "",
    ]
    if not index.symbols:
        lines.append("- None")
    for symbol in index.symbols:
        lines.append(
            f"- `{symbol['qualified_name']}` ({symbol['kind']}) — "
            f"`/raw/{symbol['source_path']}` lines "
            f"{symbol['start_line']}-{symbol['end_line']}"
        )
    lines.extend(["", "## Internal Imports", ""])
    if not index.internal_imports:
        lines.append("- None")
    for item in index.internal_imports:
        lines.append(
            f"- `/raw/{item['source_path']}` line {item['line']}: "
            f"`{item['module']}` → `/raw/{item['resolved_source']}`"
        )
    lines.append("")
    return "\n".join(lines)


def _public_summary(
        analyses: list[CodeFileAnalysis], index: RepositoryIndex
) -> dict[str, Any]:
    file_analyses = [
        item for item in analyses if item.origin_kind == "file"
    ]
    embedded_analyses = [
        item for item in analyses if item.origin_kind == "embedded"
    ]
    warnings = sorted(
        (
            asdict(warning)
            for analysis in analyses
            for warning in analysis.warnings
        ),
        key=lambda item: (item["source_path"], item["code"], item["message"]),
    )
    return {
        "detected_files": len(file_analyses),
        "parsed_files": sum(
            item.status == "parsed" for item in file_analyses
        ),
        "partially_parsed_files": sum(
            item.status == "partial" for item in file_analyses
        ),
        "fallback_files": sum(
            item.status == "fallback" for item in file_analyses
        ),
        "embedded_blocks": len(embedded_analyses),
        "parsed_embedded_blocks": sum(
            item.status in {"parsed", "partial"} for item in embedded_analyses
        ),
        "fallback_embedded_blocks": sum(
            item.status == "fallback" for item in embedded_analyses
        ),
        "symbol_count": index.symbol_count,
        "internal_import_count": index.internal_import_count,
        "warnings": warnings[:_PUBLIC_WARNING_LIMIT],
    }


def write_code_artifacts(
        raw_dir: Path,
        wiki_dir: Path,
        analyses: list[CodeFileAnalysis],
        index: RepositoryIndex,
        *,
        max_chars: int | None = None,
) -> dict[str, Any]:
    """Atomically regenerate code artifacts and persist the ingest manifest."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    wiki_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = raw_dir / _ARTIFACT_DIR_NAME
    staging_dir = raw_dir / f".{_ARTIFACT_DIR_NAME}.staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)
    chunk_limit = (
        _env_int("WIKI_MAX_CHUNK_CHARS", _DEFAULT_MAX_CHARS)
        if max_chars is None
        else max(1, max_chars)
    )

    manifest_files: list[dict[str, Any]] = []
    artifact_sequence = 0
    for analysis in sorted(analyses, key=lambda item: item.source_path):
        payload = analysis.to_dict()
        artifacts: list[str] = []
        if analysis.status != "fallback":
            for unit in analysis.units:
                parts = _split_code_unit(unit, chunk_limit)
                for part_number, (start_line, end_line, code) in enumerate(parts, start=1):
                    artifact_sequence += 1
                    filename = (
                        f"{artifact_sequence:04d}-"
                        f"{_safe_slug(analysis.source_path)}-"
                        f"{_safe_slug(unit.qualified_name)}"
                    )
                    if len(parts) > 1:
                        filename += f"-part{part_number:03d}"
                    filename += ".md"
                    (staging_dir / filename).write_text(
                        _render_unit_artifact(
                            analysis,
                            unit,
                            start_line=start_line,
                            end_line=end_line,
                            code=code,
                            part=part_number,
                            total_parts=len(parts),
                        ),
                        encoding="utf-8",
                    )
                    artifacts.append(f"{_ARTIFACT_DIR_NAME}/{filename}")
        payload["artifacts"] = artifacts
        manifest_files.append(payload)

    (staging_dir / "repository-index.md").write_text(
        _render_repository_index(index),
        encoding="utf-8",
    )
    if artifact_dir.exists():
        shutil.rmtree(artifact_dir)
    staging_dir.replace(artifact_dir)

    summary = _public_summary(analyses, index)
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": summary,
        "repository": index.to_dict(),
        "files": manifest_files,
    }
    manifest_path = wiki_dir / _MANIFEST_NAME
    temp_manifest = wiki_dir / f"{_MANIFEST_NAME}.tmp"
    temp_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_manifest.replace(manifest_path)
    return summary


def load_code_manifest(wiki_dir: Path) -> dict[str, Any] | None:
    """Load a valid schema-v1 code manifest, returning ``None`` on corruption."""
    path = wiki_dir / _MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    return payload


def load_code_analysis_summary(wiki_dir: Path) -> dict[str, Any] | None:
    """Load the persisted bounded public code-analysis summary."""
    manifest = load_code_manifest(wiki_dir)
    if manifest is None:
        return None
    summary = manifest.get("summary")
    if not isinstance(summary, dict):
        return None
    if not summary.get("detected_files") and not summary.get("embedded_blocks"):
        return None
    return summary
