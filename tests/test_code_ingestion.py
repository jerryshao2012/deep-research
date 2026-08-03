from __future__ import annotations

import json
from pathlib import Path

import pytest

from thread_wiki.code_ingestion import (
    CodeFileAnalysis,
    analyze_code_sources,
    analyze_embedded_code_sources,
    build_repository_index,
    detect_code_language,
    extract_embedded_code_blocks,
    load_code_manifest,
    write_code_artifacts,
)


@pytest.mark.parametrize(
    ("filename", "source", "language"),
    [
        ("sample.py", "def greet():\n    return 'hello'\n", "python"),
        ("sample.jsx", "export function Greet() { return <p>Hello</p>; }\n", "javascript"),
        ("sample.tsx", "export function Greet(): JSX.Element { return <p>Hello</p>; }\n", "tsx"),
        ("Sample.java", "class Sample { void greet() {} }\n", "java"),
        ("sample.go", "package sample\nfunc Greet() string { return \"hello\" }\n", "go"),
        ("sample.rs", "pub fn greet() -> &'static str { \"hello\" }\n", "rust"),
        ("sample.c", "const char *greet(void) { return \"hello\"; }\n", "c"),
        ("sample.cpp", "class Sample { public: void greet() {} };\n", "cpp"),
        ("Sample.cs", "class Sample { void Greet() {} }\n", "c_sharp"),
        ("sample.rb", "def greet\n  'hello'\nend\n", "ruby"),
        ("sample.php", "<?php function greet() { return 'hello'; }\n", "php"),
    ],
)
def test_detects_and_parses_supported_languages(
        tmp_path: Path, filename: str, source: str, language: str
) -> None:
    path = tmp_path / filename
    path.write_text(source, encoding="utf-8")

    detection = detect_code_language(path)
    assert detection is not None
    assert detection.language == language
    assert detection.method == "extension"

    [analysis] = analyze_code_sources([path])
    assert analysis.language == language
    assert analysis.status in {"parsed", "partial"}
    assert analysis.units
    assert all(unit.start_line >= 1 for unit in analysis.units)
    assert all(unit.end_line >= unit.start_line for unit in analysis.units)


def test_extensionless_shebang_detection_does_not_classify_markdown(
        tmp_path: Path,
) -> None:
    script = tmp_path / "build"
    script.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
    markdown = tmp_path / "notes.md"
    markdown.write_text("```python\nprint('not a code documents')\n```\n", encoding="utf-8")

    detected = detect_code_language(script)
    assert detected is not None
    assert detected.language == "python"
    assert detected.method == "shebang"
    assert detect_code_language(markdown) is None


def test_nested_units_have_stable_parent_and_line_ranges(tmp_path: Path) -> None:
    path = tmp_path / "nested.py"
    path.write_text(
        '"""Module docs."""\n'
        "import os\n\n"
        "class Greeter:\n"
        '    """Greets users."""\n'
        "    def greet(self, name: str) -> str:\n"
        '        return f"Hello {name}"\n',
        encoding="utf-8",
    )

    [analysis] = analyze_code_sources([path])
    greeter = next(unit for unit in analysis.units if unit.name == "Greeter")
    greet = next(unit for unit in analysis.units if unit.name == "greet")

    assert greeter.kind == "class"
    assert greet.kind == "method"
    assert greet.parent_id == greeter.unit_id
    assert greet.qualified_name == "Greeter.greet"
    assert greet.start_line == 6
    assert "def greet" in greet.code
    assert analysis.imports[0].module == "os"


def test_recoverable_syntax_error_keeps_usable_units(tmp_path: Path) -> None:
    path = tmp_path / "partial.py"
    path.write_text("def good():\n    return 1\n\ndef broken(:\n", encoding="utf-8")

    [analysis] = analyze_code_sources([path])

    assert analysis.status == "partial"
    assert any(unit.name == "good" for unit in analysis.units)
    assert any(warning.code == "syntax_error" for warning in analysis.warnings)


def test_ambiguous_header_selects_best_tree_and_ties_to_c(tmp_path: Path) -> None:
    c_header = tmp_path / "plain.h"
    c_header.write_text("int add(int left, int right);\n", encoding="utf-8")
    cpp_header = tmp_path / "typed.h"
    cpp_header.write_text(
        "template <typename T>\nclass Box { public: T value; };\n",
        encoding="utf-8",
    )

    analyses = analyze_code_sources([c_header, cpp_header], root=tmp_path)
    by_path = {item.source_path: item for item in analyses}

    assert by_path["plain.h"].language == "c"
    assert by_path["typed.h"].language == "cpp"


def test_disabled_and_oversized_code_fall_back(tmp_path: Path) -> None:
    path = tmp_path / "large.py"
    path.write_text("def large():\n    return 1\n", encoding="utf-8")

    [disabled] = analyze_code_sources([path], enabled=False)
    [oversized] = analyze_code_sources([path], max_bytes=1)

    assert disabled.status == "fallback"
    assert disabled.warnings[0].code == "ast_disabled"
    assert oversized.status == "fallback"
    assert oversized.warnings[0].code == "source_too_large"


def test_repository_index_resolves_only_unambiguous_internal_links(
        tmp_path: Path,
) -> None:
    util = tmp_path / "util.py"
    util.write_text("def helper():\n    return 1\n", encoding="utf-8")
    app = tmp_path / "app.py"
    app.write_text("from util import helper\n\ndef run():\n    return helper()\n", encoding="utf-8")

    analyses = analyze_code_sources([app, util])
    index = build_repository_index(analyses)

    assert index.symbol_count >= 2
    assert index.internal_import_count == 1
    app_analysis = next(item for item in analyses if item.source_path == "app.py")
    assert app_analysis.imports[0].resolved_source == "util.py"


def test_repository_index_omits_ambiguous_internal_imports(tmp_path: Path) -> None:
    first = tmp_path / "one" / "util.py"
    second = tmp_path / "two" / "util.py"
    app = tmp_path / "app.py"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("def helper():\n    return 1\n", encoding="utf-8")
    second.write_text("def helper():\n    return 2\n", encoding="utf-8")
    app.write_text("import util\n", encoding="utf-8")

    analyses = analyze_code_sources([app, first, second], root=tmp_path)
    index = build_repository_index(analyses)
    app_analysis = next(item for item in analyses if item.source_path == "app.py")

    assert app_analysis.imports[0].resolved_source is None
    assert index.internal_import_count == 0


def test_artifacts_and_manifest_are_deterministic_and_cite_original(
        tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    source = raw_dir / "sample.py"
    source.write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    analyses = analyze_code_sources([source])
    index = build_repository_index(analyses)

    summary = write_code_artifacts(raw_dir, tmp_path, analyses, index)
    manifest = load_code_manifest(tmp_path)

    assert summary["detected_files"] == 1
    assert manifest is not None
    assert manifest["schema_version"] == 1
    assert manifest["files"][0]["source_path"] == "sample.py"
    assert manifest["files"][0]["sha256"]
    artifact_path = raw_dir / manifest["files"][0]["artifacts"][0]
    artifact = artifact_path.read_text(encoding="utf-8")
    assert "Original source: `/raw/sample.py`" in artifact
    assert "Lines: 1-2" in artifact
    assert (raw_dir / "_code" / "repository-index.md").is_file()

    stale = raw_dir / "_code" / "stale.md"
    stale.write_text("old", encoding="utf-8")
    write_code_artifacts(raw_dir, tmp_path, analyses, index)
    assert not stale.exists()


def test_public_warning_summary_is_bounded(tmp_path: Path) -> None:
    analyses = [
        CodeFileAnalysis.fallback(
            source_path=f"source-{index:03d}.py",
            language="python",
            detection_method="extension",
            warning_code="parser_failure",
            message="failed",
        )
        for index in range(60)
    ]

    summary = write_code_artifacts(
        tmp_path / "raw",
        tmp_path / "wiki",
        analyses,
        build_repository_index(analyses),
    )

    assert summary["fallback_files"] == 60
    assert len(summary["warnings"]) == 50
    assert summary["warnings"][0]["source_path"] == "source-000.py"


def test_manifest_is_json_serializable(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    source = raw_dir / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    analyses = analyze_code_sources([source])

    write_code_artifacts(raw_dir, tmp_path, analyses, build_repository_index(analyses))

    payload = json.loads((tmp_path / ".code_ingest_manifest.json").read_text())
    assert payload["files"][0]["units"]


def test_utf8_bom_and_artifact_names_are_safe(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    source = raw_dir / "odd folder" / "module name.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\xef\xbb\xbfdef greet():\n    return 1\n")
    analyses = analyze_code_sources([source], root=raw_dir)

    write_code_artifacts(
        raw_dir,
        tmp_path / "wiki",
        analyses,
        build_repository_index(analyses),
    )
    artifact_names = [
        path.name
        for path in (raw_dir / "_code").glob("*.md")
        if path.name != "repository-index.md"
    ]

    assert analyses[0].status == "parsed"
    assert artifact_names
    assert all("/" not in name and " " not in name for name in artifact_names)


def test_embedded_code_parses_only_explicit_supported_fences(tmp_path: Path) -> None:
    document = tmp_path / "guide.md"
    document.write_text(
        "# Guide\n\n"
        "```python\n"
        "def greet():\n"
        "    return 'hello'\n"
        "```\n\n"
        "```\n"
        "not explicitly typed\n"
        "```\n\n"
        "```brainfuck\n"
        "+++\n"
        "```\n",
        encoding="utf-8",
    )

    blocks = extract_embedded_code_blocks(document, root=tmp_path)
    analyses = analyze_embedded_code_sources([document], root=tmp_path)

    assert len(blocks) == 1
    assert blocks[0].source_path == "guide.md"
    assert blocks[0].language == "python"
    assert blocks[0].start_line == 4
    assert blocks[0].end_line == 5
    assert len(analyses) == 1
    assert analyses[0].origin_kind == "embedded"
    assert analyses[0].units[0].start_line == 4
    assert analyses[0].units[0].end_line == 5


def test_embedded_symbols_do_not_enter_repository_resolution(
        tmp_path: Path,
) -> None:
    source = tmp_path / "app.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    guide = tmp_path / "guide.md"
    guide.write_text(
        "```python\nfrom app import run\n\ndef example():\n    return run()\n```\n",
        encoding="utf-8",
    )

    file_analyses = analyze_code_sources([source], root=tmp_path)
    embedded = analyze_embedded_code_sources([guide], root=tmp_path)
    index = build_repository_index([*file_analyses, *embedded])

    assert all(item["source_path"] != "guide.md" for item in index.symbols)
    assert all(
        item["source_path"] != "guide.md" for item in index.internal_imports
    )


def test_embedded_code_has_separate_summary_counts(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text("```javascript\nfunction run() { return 1; }\n```\n", encoding="utf-8")
    analyses = analyze_embedded_code_sources([guide], root=tmp_path)

    summary = write_code_artifacts(
        tmp_path / "raw",
        tmp_path / "wiki",
        analyses,
        build_repository_index(analyses),
    )

    assert summary["detected_files"] == 0
    assert summary["embedded_blocks"] == 1
    assert summary["parsed_embedded_blocks"] == 1


def test_embedded_code_uses_independent_safe_fallback(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text("```python\ndef run():\n    return 1\n```\n", encoding="utf-8")

    [analysis] = analyze_embedded_code_sources(
        [guide],
        root=tmp_path,
        enabled=False,
    )

    assert analysis.status == "fallback"
    assert analysis.warnings[0].code == "embedded_ast_disabled"
