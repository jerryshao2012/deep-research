#!/usr/bin/env python3
"""Check or remove deployment-owned passkey settings from a dotenv file."""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PROTECTED_KEYS = frozenset(
    {
        "FRONTEND_URLS",
        "PASSKEY_DERIVE_FROM_FRONTEND_URLS",
        "PASSKEY_ENABLED",
        "PASSKEY_ORIGINS",
        "PASSKEY_PROXY_ID",
        "PASSKEY_PROXY_SECRET",
        "PASSKEY_RP_ID",
        "PASSKEY_RP_IDS",
    }
)
ASSIGNMENT = re.compile(
    rb"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$"
)
MALFORMED_PROTECTED = re.compile(
    rb"^[ \t]*(?:export[ \t]+)?(PASSKEY_[A-Za-z0-9_]*|FRONTEND_URLS)(?:[ \t]|$)"
)


class SanitizeError(Exception):
    """Expected validation or safe-I/O failure."""


@dataclass(frozen=True)
class ParsedLine:
    """One original dotenv line plus parsed assignment fields when present."""

    raw: bytes
    key: str | None = None
    value: bytes | None = None


def _safe_regular_file(path: Path) -> tuple[int, os.stat_result]:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise SanitizeError(f"input file does not exist: {path}") from exc
    except OSError as exc:
        raise SanitizeError(f"cannot inspect input file: {path}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise SanitizeError(f"input must be a regular non-symlink file: {path}")
    if before.st_nlink != 1:
        raise SanitizeError(f"input file must not have hard links: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SanitizeError(f"cannot safely open input file: {path}: {exc}") from exc
    after = os.fstat(descriptor)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(descriptor)
        raise SanitizeError(f"input file changed while opening: {path}")
    return descriptor, after


def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _parse_value(raw: bytes, line_number: int) -> bytes:
    value = raw.strip(b" \t")
    if b"\x00" in value or b"`" in value or b"$(" in value or b"${" in value:
        raise SanitizeError(f"unsupported ambiguous syntax on line {line_number}")
    if value.endswith(b"\\"):
        raise SanitizeError(f"unsupported line continuation on line {line_number}")
    if not value:
        return b""
    if value[:1] in (b"'", b'"'):
        quote = value[:1]
        escaped = False
        closing = None
        for index in range(1, len(value)):
            byte = value[index : index + 1]
            if quote == b'"' and byte == b"\\" and not escaped:
                escaped = True
                continue
            if byte == quote and not escaped:
                closing = index
                break
            escaped = False
        if closing is None:
            raise SanitizeError(f"unterminated quoted value on line {line_number}")
        suffix = value[closing + 1 :].strip(b" \t")
        if suffix and not suffix.startswith(b"#"):
            raise SanitizeError(
                f"unsupported content after value on line {line_number}"
            )
        decoded = value[1:closing]
        if quote == b'"':
            decoded = re.sub(
                rb"\\([\\\"nrt])",
                lambda match: {
                    b"\\": b"\\",
                    b'"': b'"',
                    b"n": b"\n",
                    b"r": b"\r",
                    b"t": b"\t",
                }[match.group(1)],
                decoded,
            )
            if b"\\" in decoded:
                raise SanitizeError(
                    f"unsupported escape in value on line {line_number}"
                )
        return decoded
    if b"'" in value or b'"' in value:
        raise SanitizeError(
            f"unsupported quote in unquoted value on line {line_number}"
        )
    comment = re.search(rb"[ \t]+#", value)
    if comment:
        value = value[: comment.start()].rstrip(b" \t")
    if b"\\" in value:
        raise SanitizeError(
            f"unsupported escape in unquoted value on line {line_number}"
        )
    return value


def _parse(content: bytes) -> list[ParsedLine]:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SanitizeError("input is not valid UTF-8") from exc
    parsed: list[ParsedLine] = []
    assignment_seen: set[str] = set()
    for line_number, raw in enumerate(content.splitlines(keepends=True), 1):
        body = raw.rstrip(b"\r\n")
        if not body.strip(b" \t") or body.lstrip(b" \t").startswith(b"#"):
            parsed.append(ParsedLine(raw))
            continue
        match = ASSIGNMENT.fullmatch(body)
        if match is None:
            malformed = MALFORMED_PROTECTED.match(body)
            if malformed:
                key = malformed.group(1).decode("ascii")
                raise SanitizeError(f"malformed protected assignment for {key}")
            raise SanitizeError(f"unsupported dotenv syntax on line {line_number}")
        key = match.group(1).decode("ascii")
        value = _parse_value(match.group(2), line_number)
        if key in assignment_seen:
            raise SanitizeError(f"duplicate assignment for {key}")
        assignment_seen.add(key)
        parsed.append(ParsedLine(raw, key, value))
    return parsed


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _assert_input_unchanged(
    input_path: Path, expected_stat: os.stat_result, expected_content: bytes
) -> None:
    descriptor = -1
    try:
        descriptor, current_stat = _safe_regular_file(input_path)
        current_content = _read_all(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        current_stat.st_dev != expected_stat.st_dev
        or current_stat.st_ino != expected_stat.st_ino
        or current_stat.st_size != expected_stat.st_size
        or current_content != expected_content
    ):
        raise SanitizeError("input file changed during sanitize")


def _sanitize(
    input_path: Path,
    input_stat: os.stat_result,
    original_content: bytes,
    parsed: list[ParsedLine],
    capture_path: Path | None,
) -> None:
    sanitized = b"".join(item.raw for item in parsed if item.key not in PROTECTED_KEYS)
    secret = next(
        (
            item.value
            for item in parsed
            if item.key == "PASSKEY_PROXY_SECRET" and item.value is not None
        ),
        None,
    )
    input_temp_fd = -1
    input_temp_path: str | None = None
    capture_fd = -1
    capture_created = False
    try:
        input_temp_fd, input_temp_path = tempfile.mkstemp(
            prefix=f".{input_path.name}.sanitize.", dir=input_path.parent
        )
        os.fchmod(input_temp_fd, stat.S_IMODE(input_stat.st_mode))
        _write_all(input_temp_fd, sanitized)
        os.fsync(input_temp_fd)
        os.close(input_temp_fd)
        input_temp_fd = -1

        if capture_path is not None and secret is not None:
            capture_fd = os.open(
                capture_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            capture_created = True
            _write_all(capture_fd, secret)
            os.fsync(capture_fd)
            os.close(capture_fd)
            capture_fd = -1

        _assert_input_unchanged(input_path, input_stat, original_content)
        os.replace(input_temp_path, input_path)
        input_temp_path = None
    except OSError as exc:
        raise SanitizeError(f"safe file update failed: {exc}") from exc
    finally:
        if input_temp_fd >= 0:
            os.close(input_temp_fd)
        if capture_fd >= 0:
            os.close(capture_fd)
        if input_temp_path is not None:
            try:
                os.unlink(input_temp_path)
            except FileNotFoundError:
                pass
        if capture_created and input_temp_path is not None and capture_path is not None:
            try:
                os.unlink(capture_path)
            except FileNotFoundError:
                pass


def _arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--sanitize", action="store_true")
    parser.add_argument("--capture-secret-to", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.capture_secret_to is not None and not arguments.sanitize:
        parser.error("--capture-secret-to requires --sanitize")
    return arguments


def main(argv: list[str] | None = None) -> int:
    """Run strict check or atomic sanitize operation."""
    arguments = _arguments(sys.argv[1:] if argv is None else argv)
    descriptor = -1
    try:
        descriptor, input_stat = _safe_regular_file(arguments.input)
        content = _read_all(descriptor)
        os.close(descriptor)
        descriptor = -1
        parsed = _parse(content)
        protected = [item.key for item in parsed if item.key in PROTECTED_KEYS]
        if arguments.check:
            if protected:
                raise SanitizeError(
                    "deployment-owned keys are not allowed: "
                    + ", ".join(key for key in protected if key is not None)
                )
            return 0
        _sanitize(
            arguments.input,
            input_stat,
            content,
            parsed,
            arguments.capture_secret_to,
        )
        return 0
    except SanitizeError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    finally:
        if descriptor >= 0:
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
