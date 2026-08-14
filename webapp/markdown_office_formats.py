"""Filename-based classification for Microsoft Office markdown attachments."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

OFFICE_CONTENT_TYPE = "application/octet-stream"

OFFICE_EXTENSIONS_BY_FAMILY: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "word": frozenset(
            {".doc", ".docx", ".docm", ".dot", ".dotx", ".dotm", ".rtf", ".wbk"}
        ),
        "excel": frozenset(
            {
                ".xls",
                ".xlsx",
                ".xlsm",
                ".xlsb",
                ".xlt",
                ".xltx",
                ".xltm",
                ".xla",
                ".xlam",
                ".xll",
                ".xlm",
                ".xlw",
            }
        ),
        "powerpoint": frozenset(
            {
                ".ppt",
                ".pptx",
                ".pptm",
                ".pot",
                ".potx",
                ".potm",
                ".pps",
                ".ppsx",
                ".ppsm",
                ".ppa",
                ".ppam",
                ".sldx",
                ".sldm",
                ".thmx",
            }
        ),
        "access": frozenset(
            {
                ".accdb",
                ".accde",
                ".accdr",
                ".accdt",
                ".accdc",
                ".mdb",
                ".mde",
                ".mda",
                ".mdw",
                ".ade",
                ".adp",
            }
        ),
        "visio": frozenset(
            {
                ".vsd",
                ".vsdx",
                ".vsdm",
                ".vss",
                ".vssx",
                ".vssm",
                ".vst",
                ".vstx",
                ".vstm",
                ".vdw",
                ".vdx",
                ".vsx",
                ".vtx",
            }
        ),
        "onenote": frozenset({".one", ".onepkg", ".onetoc2"}),
        "project": frozenset({".mpp", ".mpt", ".mpd", ".mpx"}),
        "outlook": frozenset({".pst", ".ost", ".msg", ".oft"}),
        "publisher": frozenset({".pub"}),
        "infopath": frozenset({".xsn"}),
    }
)


def _build_office_family_by_extension(
    extensions_by_family: Mapping[str, frozenset[str]],
) -> Mapping[str, str]:
    family_by_extension: dict[str, str] = {}
    for family, extensions in extensions_by_family.items():
        for extension in extensions:
            normalized_extension = extension.lower()
            if normalized_extension in family_by_extension:
                raise ValueError(f"Duplicate Office extension: {normalized_extension}")
            family_by_extension[normalized_extension] = family
    return MappingProxyType(family_by_extension)


OFFICE_FAMILY_BY_EXTENSION: Mapping[str, str] = _build_office_family_by_extension(
    OFFICE_EXTENSIONS_BY_FAMILY
)


def office_family_for_filename(filename: str) -> str | None:
    """Return Office family for filename's final suffix, if supported."""
    if "/" in filename or "\\" in filename:
        return None
    return OFFICE_FAMILY_BY_EXTENSION.get(Path(filename).suffix.lower())


def is_office_upload(filename: str) -> bool:
    """Return whether filename has a supported Microsoft Office suffix."""
    return office_family_for_filename(filename) is not None
