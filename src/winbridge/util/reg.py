"""Windows ``.reg`` parsing and ``reg export`` plumbing.

Registry exports are the only practical way to read an installed application's
Uninstall metadata and URL handlers from a Wine prefix. The format is Windows'
own: section headers in brackets, quote-wrapped string values with ``\\`` and
``\"`` escapes, and ``@`` for a key's default value. Both 32-bit and 64-bit
registry views are parsed as ordinary keys; callers decide which view matters.
Wine's own ``system.reg`` writes a last-write timestamp after each section
header (``[Key] 1761619200``) instead of the bare Windows header, so the parser
accepts and ignores trailing header tokens.

``reg export`` writes a guest-visible path. Wine's ``Z:`` host-root link is
stripped from every managed prefix, so the temporary file is created *inside*
``drive_c`` (``C:\\windows\\temp`` when it exists) and read back from the host
side before being removed.
"""

from __future__ import annotations

import codecs
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from winbridge.backends.base import Backend

__all__ = ["export_hive", "parse_reg"]

_LOGGER = logging.getLogger("winbridge")

#: A section header. Wine's ``system.reg`` appends the key's last-write
#: timestamp after the closing bracket (``[Key] 1761619200``), and a hand-edited
#: file may carry other trailing tokens. Windows' ``reg export`` emits no such
#: suffix, so everything after the bracket is accepted and ignored.
_SECTION = re.compile(r"^\[(?P<key>[^\]]+)\](?:\s.*)?$")
_VALUE = re.compile(
    r'^(?P<name>@|"(?:[^"\\]|\\.)*"|[^=\s][^=]*?)\s*=(?P<data>.*)$',
)
_HEX = re.compile(r"^hex(?:\((?P<kind>[0-9a-fA-F]+)\))?:(?P<data>.*)$")


def _decode_string(raw: str) -> str | None:
    """Decode a quoted .reg string; ``None`` means the closing quote is missing."""
    if not raw.startswith('"'):
        return None
    decoded: list[str] = []
    index = 1
    while index < len(raw):
        character = raw[index]
        if character == "\\" and index + 1 < len(raw):
            following = raw[index + 1]
            if following in ('"', "\\"):
                decoded.append(following)
                index += 2
                continue
        if character == '"':
            return "".join(decoded)
        decoded.append(character)
        index += 1
    return None


def _decode_name(raw: str) -> str | None:
    if raw == "@":
        return ""
    if raw.startswith('"'):
        return _decode_string(raw)
    name = raw.strip()
    return name or None


def _decode_hex_value(kind: str | None, data: str) -> str:
    """Return a readable value for the hex encodings ``reg export`` emits."""
    digits = re.sub(r"[^0-9a-fA-F]", "", data)
    try:
        payload = bytes.fromhex(digits)
    except ValueError:
        return data

    if kind in ("2", "7"):
        text = payload.decode("utf-16-le", errors="replace").rstrip("\x00")
        if kind == "7":
            return "\n".join(part for part in text.split("\x00") if part)
        return text
    if kind == "4" and len(payload) >= 4:
        return str(int.from_bytes(payload[:4], "little"))
    if kind == "b" and len(payload) >= 8:
        return str(int.from_bytes(payload[:8], "little"))
    return data


def _decode_value(raw: str) -> str | None:
    """Decode one value payload; ``None`` means a deletion or malformed string."""
    if raw == "-":
        return None
    if raw.startswith('"'):
        return _decode_string(raw)
    if raw.lower().startswith("dword:"):
        try:
            return str(int(raw.split(":", 1)[1], 16))
        except ValueError:
            return raw
    match = _HEX.match(raw)
    if match is not None:
        return _decode_hex_value(match.group("kind"), match.group("data"))
    return raw


def parse_reg(text: str) -> dict[str, dict[str, str]]:
    """Parse ``.reg`` export text into ``{key: {value_name: value}}``.

    Default values are stored under the empty name. String values are
    unescaped; numeric and hex values are converted to decimal text or decoded
    UTF-16 strings. Deletions are applied to the result. Unknown lines are
    ignored so one odd value cannot lose the rest of the export.
    """
    parsed: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    text = text.lstrip("\ufeff")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or line.startswith(";") or line.startswith("#"):
            # ``;`` is a Windows .reg comment; ``#time=`` and friends are
            # Wine's own metadata comments in ``system.reg``.
            continue

        section = _SECTION.match(line)
        if section is not None:
            key = section.group("key").strip()
            if key.startswith("-"):
                parsed.pop(key[1:], None)
                current = None
            else:
                current = parsed.setdefault(key, {})
            continue

        if current is None:
            continue
        value_match = _VALUE.match(line)
        if value_match is None:
            continue
        name = _decode_name(value_match.group("name"))
        if name is None:
            continue

        data = value_match.group("data").strip()
        if data.lower().startswith("hex") and data.endswith("\\"):
            parts = [data]
            while parts[-1].endswith("\\") and index < len(lines):
                parts.append(lines[index].strip())
                index += 1
            data = "".join(part[:-1] if part.endswith("\\") else part for part in parts)

        value = _decode_value(data)
        if value is None:
            current.pop(name, None)
        else:
            current[name] = value
    return parsed


def _decode_reg_bytes(data: bytes) -> str:
    if data.startswith(codecs.BOM_UTF16_LE):
        return data[len(codecs.BOM_UTF16_LE) :].decode("utf-16-le", errors="replace")
    if data.startswith(codecs.BOM_UTF16_BE):
        return data[len(codecs.BOM_UTF16_BE) :].decode("utf-16-be", errors="replace")
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace")
    # Wine's exporter is normally UTF-16LE with a BOM, but a missing BOM must
    # not turn every value into mojibake.
    sample = data[:4096]
    if len(sample) >= 4 and sample[1::2].count(0) > len(sample) // 4:
        return data.decode("utf-16-le", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _temp_directory(prefix: Path) -> Path | None:
    drive_c = prefix / "drive_c"
    if drive_c.is_symlink() or not drive_c.is_dir():
        return None
    windows_temp = drive_c / "windows" / "temp"
    if windows_temp.is_dir() and not windows_temp.is_symlink():
        try:
            if windows_temp.resolve().is_relative_to(prefix.resolve()):
                return windows_temp
        except OSError:
            return drive_c
    return drive_c


def _guest_path(drive_c: Path, host: Path) -> str:
    relative = host.relative_to(drive_c)
    return "C:\\" + "\\".join(relative.parts)


def export_hive(backend: Backend, prefix: Path, hive: str) -> dict[str, dict[str, str]]:
    """Export one registry hive/subtree through ``reg export`` and parse it.

    The temporary file is guest-visible (inside ``drive_c``) and is removed on
    every path, including backend failures. A missing prefix or a failed export
    returns an empty mapping: detection falls back to installer metadata rather
    than failing setup.
    """
    prefix = Path(prefix)
    drive_c = prefix / "drive_c"
    temp_directory = _temp_directory(prefix)
    if temp_directory is None:
        _LOGGER.debug("[detect] cannot export %s: %s has no drive_c", hive, prefix)
        return {}

    try:
        descriptor, raw_host = tempfile.mkstemp(
            prefix="winbridge-reg-", suffix=".reg", dir=temp_directory
        )
    except OSError as exc:
        _LOGGER.warning("[detect] cannot create a registry export file in %s: %s", prefix, exc)
        return {}

    os.close(descriptor)
    host = Path(raw_host)
    try:
        # Let reg create the file itself; an existing placeholder can confuse
        # exporters that check for an existing target before treating /y as
        # force.
        host.unlink(missing_ok=True)
        guest = _guest_path(drive_c, host)
        completed = backend.wine(prefix, "reg", "export", hive, guest, "/y")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            _LOGGER.debug(
                "[detect] reg export %s exited %s%s",
                hive,
                completed.returncode,
                f": {detail}" if detail else "",
            )
            return {}
        try:
            data = host.read_bytes()
        except OSError as exc:
            _LOGGER.warning("[detect] cannot read the registry export for %s: %s", hive, exc)
            return {}
        parsed = parse_reg(_decode_reg_bytes(data))
        _LOGGER.debug("[detect] exported %s (%d keys)", hive, len(parsed))
        return parsed
    finally:
        host.unlink(missing_ok=True)
