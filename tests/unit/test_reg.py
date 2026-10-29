"""Registry export parsing and ``reg export`` plumbing."""

from __future__ import annotations

import codecs
import subprocess
from pathlib import Path

import pytest

from winbridge.util.reg import export_hive, parse_reg

FIXTURES = Path(__file__).parents[1] / "fixtures"
REGISTRY_FIXTURE = FIXTURES / "registry.reg"

UNINSTALL_64 = r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


class ExportBackend:
    """A backend that writes a canned .reg payload where ``reg export`` would."""

    name = "fake-reg"

    def __init__(
        self,
        payload: str | bytes | None = None,
        *,
        returncode: int = 0,
        raises: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.returncode = returncode
        self.raises = raises
        self.calls: list[tuple[Path, tuple[str, ...]]] = []
        self.written: list[Path] = []

    def wine(
        self, prefix: Path, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((prefix, args))
        if self.raises is not None:
            raise self.raises
        if self.returncode != 0:
            return subprocess.CompletedProcess(
                list(args), self.returncode, stdout="", stderr="export failed\n"
            )
        assert self.payload is not None
        guest = args[3]
        host = Path(prefix).joinpath("drive_c", *guest.split("\\")[1:])
        host.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(self.payload, bytes):
            host.write_bytes(self.payload)
        else:
            host.write_bytes(codecs.BOM_UTF16_LE + self.payload.encode("utf-16-le"))
        self.written.append(host)
        return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")


def _prefix(tmp_path: Path) -> Path:
    prefix = tmp_path / "prefix"
    (prefix / "drive_c" / "windows" / "temp").mkdir(parents=True)
    return prefix


def test_parse_reg_handles_quotes_and_escaped_backslashes():
    parsed = parse_reg(REGISTRY_FIXTURE.read_text(encoding="utf-8"))
    key = r"HKEY_CLASSES_ROOT\example-app\shell\open\command"
    assert parsed[key][""] == r'"C:\Program Files\Example\Example.exe" "%1"'


def test_parse_reg_reads_default_and_named_values():
    parsed = parse_reg(
        "Windows Registry Editor Version 5.00\n"
        "\n"
        "[HKEY_CLASSES_ROOT\\example-app]\n"
        '@="URL:Example Protocol"\n'
        '"URL Protocol"=""\n'
    )
    values = parsed[r"HKEY_CLASSES_ROOT\example-app"]
    assert values[""] == "URL:Example Protocol"
    assert values["URL Protocol"] == ""


def test_parse_reg_keeps_equals_signs_inside_quoted_names_and_values():
    parsed = parse_reg(REGISTRY_FIXTURE.read_text(encoding="utf-8"))
    values = parsed[r"HKEY_CURRENT_USER\Software\Example"]
    assert values["equals=name"] == r"C:\tmp\x"


def test_parse_reg_unescapes_quotes_in_value_names_and_data():
    parsed = parse_reg(
        "Windows Registry Editor Version 5.00\n"
        "\n"
        "[HKEY_CURRENT_USER\\Software\\Example]\n"
        '"quoted \\"name\\""="a \\"b\\" c\\\\d"\n'
    )
    values = parsed[r"HKEY_CURRENT_USER\Software\Example"]
    assert values['quoted "name"'] == 'a "b" c\\d'


def test_parse_reg_handles_comments_crlf_and_bom():
    text = (
        "\ufeffWindows Registry Editor Version 5.00\r\n"
        "\r\n"
        "; a comment\r\n"
        "[HKEY_LOCAL_MACHINE\\SOFTWARE\\WOW6432Node\\Example]\r\n"
        '"DisplayName"="Example"\r\n'
    )
    parsed = parse_reg(text)
    assert parsed == {
        r"HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Example": {"DisplayName": "Example"}
    }


def test_parse_reg_accepts_wine_system_reg_timestamped_sections():
    """Wine's ``system.reg`` appends a timestamp after every section header."""
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    key = rf"Software\\Microsoft\\EdgeUpdate\\Clients\\{guid}"
    parsed = parse_reg(
        "WINE REGISTRY Version 2\n"
        ";; All keys relative to \\\\Machine\n"
        "\n"
        f"[{key}] 1761619200\n"
        "#time=1dc4c0a1b2c3d4e\n"
        '"pv"="156.0.4314.42"\n'
        '"location"="C:\\\\Program Files (x86)\\\\Microsoft\\\\EdgeWebView"\n'
    )

    assert parsed[key] == {
        "pv": "156.0.4314.42",
        "location": r"C:\Program Files (x86)\Microsoft\EdgeWebView",
    }


def test_parse_reg_accepts_other_trailing_header_tokens():
    parsed = parse_reg(
        "WINE REGISTRY Version 2\n"
        "\n"
        "[Software\\Example] 1761619200 extra-token\n"
        '"pv"="1.2.3"\n'
    )

    assert parsed == {r"Software\Example": {"pv": "1.2.3"}}


def test_parse_reg_handles_a_bom_directly_before_a_section():
    parsed = parse_reg('\ufeff[HKEY_CLASSES_ROOT\\example-app]\n"URL Protocol"=""\n')
    assert parsed[r"HKEY_CLASSES_ROOT\example-app"] == {"URL Protocol": ""}


def test_parse_reg_merges_duplicate_sections_and_last_value_wins():
    parsed = parse_reg(
        "Windows Registry Editor Version 5.00\n"
        "\n"
        "[HKEY_CLASSES_ROOT\\example-app]\n"
        '"DisplayName"="First"\n'
        '"Version"="1"\n'
        "\n"
        "[HKEY_CLASSES_ROOT\\example-app]\n"
        '"DisplayName"="Second"\n'
    )
    assert parsed[r"HKEY_CLASSES_ROOT\example-app"] == {
        "DisplayName": "Second",
        "Version": "1",
    }


def test_parse_reg_ignores_deletions_and_malformed_lines():
    parsed = parse_reg(
        "Windows Registry Editor Version 5.00\n"
        "\n"
        "[HKEY_CLASSES_ROOT\\example-app]\n"
        '"Keep"="yes"\n'
        '"Remove"=-\n'
        "not a value line\n"
        '"Broken\n'
    )
    assert parsed[r"HKEY_CLASSES_ROOT\example-app"] == {"Keep": "yes"}


def test_parse_reg_decodes_reg_expand_sz_hex_and_dword_values():
    parsed = parse_reg(
        "Windows Registry Editor Version 5.00\n"
        "\n"
        "[HKEY_LOCAL_MACHINE\\SOFTWARE\\Example]\n"
        '"DisplayVersion"=hex(2):31,00,2e,00,32,00,\\\n'
        "  00,00\n"
        '"Build"=dword:0000002a\n'
    )
    assert parsed[r"HKEY_LOCAL_MACHINE\SOFTWARE\Example"] == {
        "DisplayVersion": "1.2",
        "Build": "42",
    }


def test_parse_reg_empty_text_returns_empty_mapping():
    assert parse_reg("") == {}
    assert parse_reg("Windows Registry Editor Version 5.00\n") == {}


def test_parse_reg_keeps_both_registry_views_as_distinct_keys():
    parsed = parse_reg(REGISTRY_FIXTURE.read_text(encoding="utf-8"))
    assert (
        parsed[
            r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion"
            r"\Uninstall\Example"
        ]["DisplayVersion"]
        == "1.2.3"
    )
    assert (
        parsed[
            r"HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\Windows"
            r"\CurrentVersion\Uninstall\ExampleOld32"
        ]["DisplayVersion"]
        == "0.9.0"
    )


def test_export_hive_uses_reg_export_into_the_prefix(tmp_path: Path):
    prefix = _prefix(tmp_path)
    payload = REGISTRY_FIXTURE.read_text(encoding="utf-8")
    backend = ExportBackend(payload)

    parsed = export_hive(backend, prefix, UNINSTALL_64)

    assert parsed[
        r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion"
        r"\Uninstall\Example"
    ]["DisplayVersion"] == "1.2.3"

    assert len(backend.calls) == 1
    _, args = backend.calls[0]
    assert args[0:3] == ("reg", "export", UNINSTALL_64)
    assert args[4] == "/y"
    assert args[3].startswith("C:\\windows\\temp\\")
    assert args[3].endswith(".reg")

    written = backend.written[0]
    assert written.is_relative_to(prefix)
    assert not written.exists()
    assert list((prefix / "drive_c").rglob("*.reg")) == []


def test_export_hive_reads_utf8_payloads(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = ExportBackend(
        b"Windows Registry Editor Version 5.00\n"
        b"\n"
        b"[HKEY_CLASSES_ROOT\\example-app]\n"
        b'"URL Protocol"=""\n'
    )

    parsed = export_hive(backend, prefix, r"HKEY_CLASSES_ROOT")

    assert parsed[r"HKEY_CLASSES_ROOT\example-app"] == {"URL Protocol": ""}


def test_export_hive_returns_empty_mapping_on_failure(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = ExportBackend(None, returncode=1)

    assert export_hive(backend, prefix, UNINSTALL_64) == {}
    assert list((prefix / "drive_c").rglob("*.reg")) == []


def test_export_hive_cleans_up_when_the_backend_raises(tmp_path: Path):
    prefix = _prefix(tmp_path)
    backend = ExportBackend(raises=RuntimeError("wine vanished"))

    with pytest.raises(RuntimeError, match="wine vanished"):
        export_hive(backend, prefix, UNINSTALL_64)

    assert list((prefix / "drive_c").rglob("*.reg")) == []


def test_export_hive_does_not_touch_a_prefix_without_drive_c(tmp_path: Path):
    prefix = tmp_path / "missing"
    backend = ExportBackend(None)

    assert export_hive(backend, prefix, UNINSTALL_64) == {}
    assert backend.calls == []


def test_export_hive_uses_drive_c_when_windows_temp_is_absent(tmp_path: Path):
    prefix = tmp_path / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    backend = ExportBackend(
        "Windows Registry Editor Version 5.00\n"
        "\n"
        "[HKEY_CLASSES_ROOT\\example-app]\n"
        '"URL Protocol"=""\n'
    )

    parsed = export_hive(backend, prefix, r"HKEY_CLASSES_ROOT")

    assert parsed[r"HKEY_CLASSES_ROOT\example-app"] == {"URL Protocol": ""}
    _, args = backend.calls[0]
    assert args[3].startswith("C:\\winbridge-reg-")
    assert args[3].endswith(".reg")
    assert backend.written[0].parent == prefix / "drive_c"


def test_export_hive_does_not_follow_a_symlinked_windows_temp(tmp_path: Path):
    prefix = tmp_path / "prefix"
    outside = tmp_path / "outside"
    outside.mkdir()
    (prefix / "drive_c" / "windows").mkdir(parents=True)
    (prefix / "drive_c" / "windows" / "temp").symlink_to(
        outside, target_is_directory=True
    )
    backend = ExportBackend(
        "Windows Registry Editor Version 5.00\n"
        "\n"
        "[HKEY_CLASSES_ROOT\\example-app]\n"
        '"URL Protocol"=""\n'
    )

    assert export_hive(backend, prefix, r"HKEY_CLASSES_ROOT") != {}
    assert list(outside.iterdir()) == []
    assert backend.written[0].parent == prefix / "drive_c"
