"""Shared physical source-file policy for Thread Wiki ingestion and lookup."""

from __future__ import annotations

from types import MappingProxyType

CODE_EXTENSION_LANGUAGES = MappingProxyType(
    {
        ".py": "python",
        ".pyw": "python",
        ".js": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".mts": "typescript",
        ".cts": "typescript",
        ".tsx": "tsx",
        ".java": "java",
        ".go": "go",
        ".rs": "rust",
        ".c": "c",
        ".h": "c_or_cpp",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".cxx": "cpp",
        ".hh": "cpp",
        ".hpp": "cpp",
        ".hxx": "cpp",
        ".cs": "c_sharp",
        ".rb": "ruby",
        ".php": "php",
    }
)
"""Canonical, lightweight source-code extension to language mapping."""

SUPPORTED_CODE_SUFFIXES = frozenset(CODE_EXTENSION_LANGUAGES)
"""Every source-code suffix supported by Thread Wiki AST ingestion."""

TEXT_SOURCE_SUFFIXES = frozenset({".md", ".txt", ".json", ".yaml", ".yml", ".csv"})
"""Text source suffixes read directly during wiki ingestion."""

BINARY_SOURCE_SUFFIXES = frozenset({".pdf", ".docx", ".pptx", ".xlsx"})
"""Binary source suffixes extracted before wiki ingestion."""

SUPPORTED_WIKI_SOURCE_SUFFIXES = frozenset(
    TEXT_SOURCE_SUFFIXES | BINARY_SOURCE_SUFFIXES | SUPPORTED_CODE_SUFFIXES
)
"""Every physical source suffix supported by Thread Wiki."""
