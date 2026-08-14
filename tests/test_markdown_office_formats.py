from __future__ import annotations

import inspect
from types import MappingProxyType

import pytest

from webapp.markdown_office_formats import (
    OFFICE_CONTENT_TYPE,
    OFFICE_EXTENSIONS_BY_FAMILY,
    OFFICE_FAMILY_BY_EXTENSION,
    _build_office_family_by_extension,
    is_office_upload,
    office_family_for_filename,
)

EXPECTED_EXTENSIONS_BY_FAMILY = {
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


def _mixed_case(extension: str) -> str:
    return "".join(
        character.upper() if index % 2 else character.lower()
        for index, character in enumerate(extension)
    )


def test_office_catalog_is_exact_stable_and_immutable() -> None:
    assert tuple(OFFICE_EXTENSIONS_BY_FAMILY) == tuple(EXPECTED_EXTENSIONS_BY_FAMILY)
    assert OFFICE_EXTENSIONS_BY_FAMILY == EXPECTED_EXTENSIONS_BY_FAMILY
    assert isinstance(OFFICE_EXTENSIONS_BY_FAMILY, MappingProxyType)
    assert all(
        isinstance(extensions, frozenset)
        for extensions in OFFICE_EXTENSIONS_BY_FAMILY.values()
    )

    with pytest.raises(TypeError):
        OFFICE_EXTENSIONS_BY_FAMILY["word"] = frozenset()  # type: ignore[index]


def test_reverse_catalog_is_exact_unique_and_immutable() -> None:
    expected_reverse = {
        extension: family
        for family, extensions in EXPECTED_EXTENSIONS_BY_FAMILY.items()
        for extension in extensions
    }

    assert OFFICE_FAMILY_BY_EXTENSION == expected_reverse
    assert len(OFFICE_FAMILY_BY_EXTENSION) == sum(
        len(extensions) for extensions in EXPECTED_EXTENSIONS_BY_FAMILY.values()
    )
    assert isinstance(OFFICE_FAMILY_BY_EXTENSION, MappingProxyType)

    with pytest.raises(TypeError):
        OFFICE_FAMILY_BY_EXTENSION[".new"] = "word"  # type: ignore[index]


def test_reverse_catalog_builder_fails_fast_on_duplicate_extensions() -> None:
    duplicate_catalog = {
        "word": frozenset({".duplicate"}),
        "excel": frozenset({".duplicate"}),
    }

    with pytest.raises(ValueError, match=r"Duplicate Office extension: \.duplicate"):
        _build_office_family_by_extension(duplicate_catalog)


@pytest.mark.parametrize(
    ("family", "extension"),
    [
        (family, extension)
        for family, extensions in EXPECTED_EXTENSIONS_BY_FAMILY.items()
        for extension in sorted(extensions)
    ],
)
def test_every_office_extension_maps_to_exact_family(
    family: str, extension: str
) -> None:
    assert office_family_for_filename(f"report{extension}") == family
    assert is_office_upload(f"report{extension}") is True


@pytest.mark.parametrize(
    ("family", "extension"),
    [
        (family, extension)
        for family, extensions in EXPECTED_EXTENSIONS_BY_FAMILY.items()
        for extension in sorted(extensions)
    ],
)
def test_office_extensions_are_case_insensitive(family: str, extension: str) -> None:
    assert office_family_for_filename(f"report{extension.upper()}") == family
    assert office_family_for_filename(f"report{_mixed_case(extension)}") == family
    assert is_office_upload(f"report{extension.upper()}") is True
    assert is_office_upload(f"report{_mixed_case(extension)}") is True


@pytest.mark.parametrize(
    "filename",
    [
        "",
        "report",
        ".docx",
        ".xlsx",
        "report.",
        "report.docx.exe",
        "report.xlsx.txt",
    ],
)
def test_only_final_real_suffix_is_considered(filename: str) -> None:
    assert office_family_for_filename(filename) is None
    assert is_office_upload(filename) is False


@pytest.mark.parametrize(
    "filename",
    [
        "report.pdf",
        "report.csv",
        "report.xml",
        "report.html",
        "report.odt",
        "report.ods",
        "report.odp",
        "report.txt",
        "report.md",
        "report.png",
        "report.jpg",
        "report.gif",
        "report.zip",
        "report.tar",
        "report.gz",
        "report.7z",
    ],
)
def test_generic_non_office_formats_are_rejected(filename: str) -> None:
    assert office_family_for_filename(filename) is None
    assert is_office_upload(filename) is False


def test_public_functions_accept_filename_only() -> None:
    for function in (office_family_for_filename, is_office_upload):
        signature = inspect.signature(function)
        assert tuple(signature.parameters) == ("filename",)
        assert (
            signature.parameters["filename"].kind
            is inspect.Parameter.POSITIONAL_OR_KEYWORD
        )


@pytest.mark.parametrize(
    "irrelevant_second_argument",
    [
        b"\x00\xffarbitrary opaque bytes",
        "",
        "application/octet-stream",
        "text/plain",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ],
)
def test_payload_and_mime_values_cannot_influence_filename_only_api(
    irrelevant_second_argument: object,
) -> None:
    with pytest.raises(TypeError):
        is_office_upload("report.docx", irrelevant_second_argument)  # type: ignore[call-arg]

    assert is_office_upload("report.docx") is True
    assert is_office_upload("report.pdf") is False


def test_office_uploads_use_normalized_stored_content_type() -> None:
    assert OFFICE_CONTENT_TYPE == "application/octet-stream"
