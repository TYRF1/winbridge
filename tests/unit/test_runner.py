from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from winbridge.cli import app, main
from winbridge.core.runner import (
    DEFAULT_CATALOG_PATH,
    INSTALL_MANIFEST_NAME,
    Catalog,
    Runner,
    RunnerError,
    RunnerNotInstalled,
    UnknownRunner,
    UnsafeArchive,
    WebView2Payload,
    discover_runners,
    import_runner,
    install_runner,
    resolve_runner,
    runner_wine_path,
)
from winbridge.core.state import Paths
from winbridge.util.download import HashMismatch

FIXTURES = Path(__file__).parent.parent / "fixtures"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_tarball(
    directory: Path,
    members: dict[str, bytes],
    *,
    prefix: str = "",
    mode: int = 0o755,
) -> Path:
    archive = directory / f"archive-{len(list(directory.glob('archive-*')))}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(prefix + name)
            info.size = len(payload)
            info.mode = mode
            tar.addfile(info, io.BytesIO(payload))
    return archive


def make_special_tarball(directory: Path, info: tarfile.TarInfo) -> Path:
    archive = directory / f"archive-{len(list(directory.glob('archive-*')))}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.addfile(info)
    return archive


def make_runner(
    archive: Path,
    *,
    digest: str | None = None,
    archive_root: str = "",
    default: bool = False,
) -> Runner:
    return Runner(
        "soda",
        "11.0-10",
        "soda",
        archive.as_uri(),
        sha256_of(archive) if digest is None else digest,
        "bin/wine",
        archive_root=archive_root,
        default=default,
    )


# --- catalog -----------------------------------------------------------------


def test_catalog_default_and_lookup(catalog: Catalog) -> None:
    assert catalog.default_runner().id == "soda"
    assert catalog.default_runner().ref == "soda@11.0-10"
    assert catalog.get("soda@11.0-10").version == "11.0-10"
    assert catalog.get("example@1.0").kind == "wine"


def test_catalog_webview2_payload(catalog: Catalog) -> None:
    assert catalog.webview2.version == "156.0.4314.42"
    assert len(catalog.webview2.sha256) == 64
    assert catalog.latest_webview2_url.startswith("https://go.microsoft.com/")


def test_catalog_unknown_ref_is_a_usage_error(catalog: Catalog) -> None:
    with pytest.raises(UnknownRunner, match="missing@1.0") as exc_info:
        catalog.get("missing@1.0")

    assert exc_info.value.exit_code == 2


def test_catalog_rejects_path_refs(catalog: Catalog) -> None:
    with pytest.raises(UnknownRunner, match="path"):
        catalog.get("path:/tmp/runner")


def test_catalog_requires_exactly_one_default(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.toml"
    catalog.write_text(
        (FIXTURES / "catalog.toml").read_text(encoding="utf-8").replace(
            "default = true", "default = false"
        ),
        encoding="utf-8",
    )

    from winbridge.core.runner import CatalogError

    with pytest.raises(CatalogError, match="default"):
        Catalog.load(catalog)


def test_catalog_rejects_http_urls(tmp_path: Path) -> None:
    from winbridge.core.runner import CatalogError

    catalog = tmp_path / "catalog.toml"
    catalog.write_text(
        (FIXTURES / "catalog.toml")
        .read_text(encoding="utf-8")
        .replace("https://example.invalid/example", "http://example.invalid/example"),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="HTTPS"):
        Catalog.load(catalog)


def test_catalog_rejects_duplicate_refs(tmp_path: Path) -> None:
    from winbridge.core.runner import CatalogError

    text = (FIXTURES / "catalog.toml").read_text(encoding="utf-8")
    entry = text.split("[[runners]]", maxsplit=1)[1]
    catalog = tmp_path / "catalog.toml"
    catalog.write_text(text + "[[runners]]" + entry, encoding="utf-8")

    with pytest.raises(CatalogError, match="duplicate"):
        Catalog.load(catalog)


def test_shipped_catalog_is_pinned_and_parsable() -> None:
    catalog = Catalog.load(DEFAULT_CATALOG_PATH)

    assert catalog.default_runner().ref == "soda@11.0-10"
    runner = catalog.default_runner()
    assert runner.url.startswith("https://")
    assert len(runner.sha256) == 64
    assert len(catalog.webview2.sha256) == 64
    assert catalog.latest_webview2_url.startswith("https://")


def test_catalog_missing_file_is_an_error(tmp_path: Path) -> None:
    from winbridge.core.runner import CatalogError

    with pytest.raises(CatalogError, match="not found"):
        Catalog.load(tmp_path / "missing.toml")


# --- runner validation -------------------------------------------------------


@pytest.mark.parametrize(
    ("runner_id", "version"),
    [("..", "1.0"), ("a/b", "1.0"), ("a\\b", "1.0"), ("soda", "../1.0"), ("soda", "")],
)
def test_runner_rejects_unsafe_components(runner_id: str, version: str) -> None:
    with pytest.raises(RunnerError):
        Runner(runner_id, version, "soda", "https://example.invalid/x", "0" * 64, "bin/wine")


def test_runner_rejects_non_https_url() -> None:
    with pytest.raises(RunnerError, match="HTTPS"):
        Runner("soda", "1.0", "soda", "http://example.invalid/x", "0" * 64, "bin/wine")


def test_runner_requires_a_hash_for_download_kinds() -> None:
    with pytest.raises(RunnerError, match="SHA-256"):
        Runner("soda", "1.0", "soda", "https://example.invalid/x", "", "bin/wine")


@pytest.mark.parametrize("relpath", ["/bin/wine", "../wine", "bin/../../wine", ""])
def test_runner_rejects_unsafe_wine_relpath(relpath: str) -> None:
    with pytest.raises(RunnerError):
        Runner("soda", "1.0", "soda", "https://example.invalid/x", "0" * 64, relpath)


@pytest.mark.parametrize("root", ["..", "../root", "/absolute/root"])
def test_runner_rejects_unsafe_archive_root(root: str) -> None:
    with pytest.raises(RunnerError):
        Runner(
            "soda",
            "1.0",
            "soda",
            "https://example.invalid/x",
            "0" * 64,
            "bin/wine",
            archive_root=root,
        )


def test_imported_runner_ref_uses_path_scheme(tmp_path: Path) -> None:
    root = tmp_path / "existing-runner"
    (root / "bin").mkdir(parents=True)

    imported = import_runner(root)

    assert imported.kind == "path"
    assert imported.ref == f"path:{root}"
    assert imported.url == str(root)
    assert imported.wine_relpath == "bin/wine"


def test_catalog_runner_ref_uses_id_at_version(catalog: Catalog) -> None:
    assert catalog.get("soda@11.0-10").ref == "soda@11.0-10"
    assert catalog.get("example@1.0").ref == "example@1.0"


# --- install -----------------------------------------------------------------


def test_install_runner_extracts_atomically(tmp_path: Path) -> None:
    archive = make_tarball(tmp_path, {"bin/wine": b"#!/bin/sh\n", "share/data": b"x"})
    runner = make_runner(archive, default=True)
    paths = Paths.from_root(tmp_path / "state")

    root = install_runner(runner, paths)

    assert root == paths.runners / "soda" / "11.0-10"
    assert (root / "bin/wine").read_bytes() == b"#!/bin/sh\n"
    assert (root / "share/data").read_bytes() == b"x"
    assert os.access(root / "bin/wine", os.X_OK)
    assert runner_wine_path(root, runner) == root / "bin/wine"
    assert not list((paths.runners / "soda").glob(".tmp-*"))
    assert not list((paths.runners / "soda").glob(".stale-*"))
    assert (paths.locks / "runner-soda.lock").is_file()


def test_install_runner_writes_a_pinned_install_manifest(tmp_path: Path) -> None:
    archive = make_tarball(tmp_path, {"bin/wine": b"#!/bin/sh\n"})
    runner = make_runner(archive)
    paths = Paths.from_root(tmp_path / "state")

    root = install_runner(runner, paths)

    manifest_path = root / INSTALL_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "schema": 1,
        "ref": "soda@11.0-10",
        "archive_sha256": runner.sha256,
        "wine_relpath": "bin/wine",
        "wine_sha256": sha256_of(root / "bin/wine"),
        "installed_at": manifest["installed_at"],
    }
    assert manifest["installed_at"].endswith("Z")
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600


def test_install_runner_strips_archive_root(tmp_path: Path) -> None:
    archive = make_tarball(
        tmp_path,
        {"bin/wine": b"x"},
        prefix="soda-11.0-10-x86_64/",
    )
    runner = make_runner(archive, archive_root="soda-11.0-10-x86_64")
    paths = Paths.from_root(tmp_path / "state")

    root = install_runner(runner, paths)

    assert (root / "bin/wine").is_file()
    assert not (root / "soda-11.0-10-x86_64").exists()


def test_install_runner_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_tarball(tmp_path, {"bin/wine": b"x"})
    runner = make_runner(archive)
    paths = Paths.from_root(tmp_path / "state")
    root = install_runner(runner, paths)

    def fail_download(*args: Any, **kwargs: Any) -> Path:
        raise AssertionError("already installed runner must not be downloaded again")

    monkeypatch.setattr("winbridge.core.runner.download_verified", fail_download)

    assert install_runner(runner, paths) == root


def test_install_runner_replaces_an_incomplete_store_entry(tmp_path: Path) -> None:
    payload = b"#!/bin/sh\n"
    archive = make_tarball(tmp_path, {"bin/wine": payload})
    runner = make_runner(archive)
    paths = Paths.from_root(tmp_path / "state")
    (paths.runners / "soda" / "11.0-10" / "bin").mkdir(parents=True)

    root = install_runner(runner, paths)

    assert (root / "bin/wine").read_bytes() == payload
    assert not list((paths.runners / "soda").glob(".stale-*"))
    assert not list((paths.runners / "soda").glob(".tmp-*"))



def test_install_runner_rejects_hash_mismatch(tmp_path: Path) -> None:
    archive = make_tarball(tmp_path, {"bin/wine": b"x"})
    runner = make_runner(archive, digest="0" * 64)
    paths = Paths.from_root(tmp_path / "state")

    with pytest.raises(HashMismatch):
        install_runner(runner, paths)

    assert not (paths.runners / "soda" / "11.0-10").exists()
    assert not list((paths.runners / "soda").glob(".tmp-*"))
    assert not list(paths.cache.iterdir())


def test_install_runner_requires_wine_in_the_archive(tmp_path: Path) -> None:
    archive = make_tarball(tmp_path, {"share/readme": b"x"})
    runner = make_runner(archive)
    paths = Paths.from_root(tmp_path / "state")

    with pytest.raises(RunnerError, match="bin/wine"):
        install_runner(runner, paths)

    assert not (paths.runners / "soda" / "11.0-10").exists()
    assert not list((paths.runners / "soda").glob(".tmp-*"))


def test_install_runner_rejects_path_kind(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    (root / "bin").mkdir(parents=True)

    with pytest.raises(RunnerError, match="path"):
        install_runner(import_runner(root), Paths.from_root(tmp_path / "state"))


@pytest.mark.parametrize(
    "name",
    [
        "/etc/passwd",
        "../escape",
        "bin/../../escape",
        "C:/windows/evil",
        "bin\\..\\escape",
    ],
)
def test_extract_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    archive = make_tarball(tmp_path, {name: b"payload"})
    runner = make_runner(archive)
    paths = Paths.from_root(tmp_path / "state")

    with pytest.raises(UnsafeArchive):
        install_runner(runner, paths)

    assert not (tmp_path / "escape").exists()
    assert not (paths.runners / "soda" / "11.0-10").exists()


@pytest.mark.parametrize(
    "member_type",
    [tarfile.LNKTYPE, tarfile.FIFOTYPE],
)
def test_extract_rejects_links_and_special_members(
    tmp_path: Path, member_type: bytes
) -> None:
    info = tarfile.TarInfo("bin/wine")
    info.type = member_type
    info.linkname = "/bin/sh"
    info.size = 0
    archive = make_special_tarball(tmp_path, info)
    runner = make_runner(archive)
    paths = Paths.from_root(tmp_path / "state")

    with pytest.raises(UnsafeArchive):
        install_runner(runner, paths)

    assert not (paths.runners / "soda" / "11.0-10").exists()


@pytest.mark.parametrize(
    "linkname",
    [
        "/bin/sh",
        "../outside",
        "bin/../../outside",
        "C:/windows/evil",
        "bin\\..\\outside",
        ".",
    ],
)
def test_extract_rejects_escaping_symlinks(tmp_path: Path, linkname: str) -> None:
    info = tarfile.TarInfo("bin/regedit")
    info.type = tarfile.SYMTYPE
    info.linkname = linkname
    info.size = 0
    archive = make_special_tarball(tmp_path, info)
    runner = make_runner(archive)
    paths = Paths.from_root(tmp_path / "state")

    with pytest.raises(UnsafeArchive):
        install_runner(runner, paths)

    assert not (tmp_path / "outside").exists()
    assert not (paths.runners / "soda" / "11.0-10").exists()


def test_extract_allows_in_tree_symlinks(tmp_path: Path) -> None:
    payload = b"#!/bin/sh\n"
    archive = tmp_path / "archive-0.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        wine = tarfile.TarInfo("bin/wine")
        wine.size = len(payload)
        wine.mode = 0o755
        tar.addfile(wine, io.BytesIO(payload))
        for alias in ("bin/regedit", "bin/winemine"):
            link = tarfile.TarInfo(alias)
            link.type = tarfile.SYMTYPE
            link.linkname = "wine"
            link.size = 0
            tar.addfile(link)
    runner = make_runner(archive)
    paths = Paths.from_root(tmp_path / "state")

    root = install_runner(runner, paths)

    assert (root / "bin/wine").read_bytes() == payload
    for alias in ("regedit", "winemine"):
        link = root / "bin" / alias
        assert link.is_symlink()
        assert os.readlink(link) == "wine"
        assert link.read_bytes() == payload


def test_extract_rejects_corrupt_archive(tmp_path: Path) -> None:
    archive = tmp_path / "corrupt.tar.gz"
    archive.write_bytes(b"this is not a tarball")
    runner = make_runner(archive)
    paths = Paths.from_root(tmp_path / "state")

    with pytest.raises(RunnerError):
        install_runner(runner, paths)


def test_runner_wine_path_stays_inside_the_root(tmp_path: Path) -> None:
    runner = Runner("soda", "1.0", "soda", "https://example.invalid/x", "0" * 64, "bin/wine")

    assert runner_wine_path(tmp_path, runner) == tmp_path / "bin/wine"


# --- resolve -----------------------------------------------------------------


def _fake_installed(paths: Paths, runner: Runner) -> Path:
    root = paths.runners / runner.id / runner.version
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "wine").write_text("", encoding="utf-8")
    return root


def test_resolve_runner_returns_installed_store_path(tmp_path: Path, catalog: Catalog) -> None:
    paths = Paths.from_root(tmp_path / "state")
    root = _fake_installed(paths, catalog.default_runner())

    assert resolve_runner("soda@11.0-10", paths, catalog) == root
    assert resolve_runner("", paths, catalog) == root


def test_resolve_runner_reports_missing_runner(tmp_path: Path, catalog: Catalog) -> None:
    paths = Paths.from_root(tmp_path / "state")

    with pytest.raises(RunnerNotInstalled, match="runners install soda@11.0-10"):
        resolve_runner("soda@11.0-10", paths, catalog)


def test_resolve_runner_unknown_ref(tmp_path: Path, catalog: Catalog) -> None:
    paths = Paths.from_root(tmp_path / "state")

    with pytest.raises(UnknownRunner) as exc_info:
        resolve_runner("missing@1.0", paths, catalog)

    assert exc_info.value.exit_code == 2


def test_resolve_runner_path_ref(tmp_path: Path, catalog: Catalog) -> None:
    root = tmp_path / "existing-runner"
    root.mkdir()
    paths = Paths.from_root(tmp_path / "state")

    assert resolve_runner(f"path:{root}", paths, catalog) == root


def test_resolve_runner_path_ref_must_be_absolute(tmp_path: Path, catalog: Catalog) -> None:
    paths = Paths.from_root(tmp_path / "state")

    with pytest.raises(RunnerError, match="absolute"):
        resolve_runner("path:relative/runner", paths, catalog)


def test_resolve_runner_path_ref_must_exist(tmp_path: Path, catalog: Catalog) -> None:
    paths = Paths.from_root(tmp_path / "state")

    with pytest.raises(RunnerNotInstalled, match="not found"):
        resolve_runner(f"path:{tmp_path / 'missing'}", paths, catalog)


# --- reuse scan --------------------------------------------------------------


def _make_wine_dir(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "wine").write_text("", encoding="utf-8")
    return root


def test_discover_runners_scans_bottles_and_steam(tmp_path: Path) -> None:
    home = Path.home()
    bottles = home / ".var/app/com.usebottles.bottles/data/bottles/runners"
    steam = home / ".local/share/Steam/compatibilitytools.d"
    soda = _make_wine_dir(bottles / "soda-11.0-10")
    proton = steam / "GE-Proton9-20"
    (proton / "files" / "bin").mkdir(parents=True)
    (proton / "files" / "bin" / "wine").write_text("", encoding="utf-8")
    (bottles / "not-a-runner").mkdir()
    hidden = _make_wine_dir(steam / ".hidden-runner")

    found = discover_runners(Paths.from_root(tmp_path / "state"))

    assert found == sorted([soda, proton])
    assert hidden not in found


def test_discover_runners_skips_the_winbridge_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths.from_root(tmp_path / "state")
    inside = _make_wine_dir(paths.runners / "soda-11.0-10")
    outside = _make_wine_dir(tmp_path / "outside" / "runner")
    monkeypatch.setattr(
        "winbridge.core.runner._reuse_roots",
        lambda home: [paths.runners, tmp_path / "outside"],
    )

    assert discover_runners(paths) == [outside]
    assert inside not in discover_runners(paths)


def test_discover_runners_returns_empty_without_roots(tmp_path: Path) -> None:
    assert discover_runners(Paths.from_root(tmp_path / "state")) == []


def test_import_runner_by_reference(tmp_path: Path, catalog: Catalog) -> None:
    root = tmp_path / "existing-runner"
    (root / "bin").mkdir(parents=True)
    imported = import_runner(root)

    assert imported.kind == "path"
    assert imported.ref == f"path:{root}"
    assert resolve_runner(imported.ref, Paths.from_root(tmp_path / "state"), catalog) == root
    assert not (tmp_path / "state").exists()


def test_import_runner_detects_proton_layout(tmp_path: Path) -> None:
    root = tmp_path / "GE-Proton9-20"
    (root / "files" / "bin").mkdir(parents=True)
    (root / "files" / "bin" / "wine").write_text("", encoding="utf-8")

    imported = import_runner(root)

    assert imported.wine_relpath == "files/bin/wine"


def test_import_runner_requires_an_existing_absolute_directory(tmp_path: Path) -> None:
    with pytest.raises(RunnerError, match="absolute"):
        import_runner(Path("relative/runner"))

    with pytest.raises(RunnerNotInstalled, match="not found"):
        import_runner(tmp_path / "missing")


def test_import_runner_does_not_copy_anything(tmp_path: Path) -> None:
    root = tmp_path / "existing-runner"
    (root / "bin").mkdir(parents=True)
    before = sorted(path.name for path in root.iterdir())

    import_runner(root)

    assert sorted(path.name for path in root.iterdir()) == before
    assert not (tmp_path / "state").exists()


# --- webview2 payload --------------------------------------------------------


def test_webview2_payload_rejects_non_https_url() -> None:
    with pytest.raises(RunnerError, match="HTTPS"):
        WebView2Payload("1.0", "http://example.invalid/x.exe", "0" * 64)


# --- CLI ---------------------------------------------------------------------


def invoke(*args: str, paths: Paths, catalog: Catalog) -> Any:
    return CliRunner().invoke(app, list(args), obj={"paths": paths, "catalog": catalog})


def test_cli_runners_list_json(tmp_path: Path, catalog: Catalog) -> None:
    paths = Paths.from_root(tmp_path / "state")
    _fake_installed(paths, catalog.default_runner())

    result = invoke("runners", "list", "--json", paths=paths, catalog=catalog)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    rows = {row["ref"]: row for row in payload["runners"]}
    assert rows["soda@11.0-10"]["installed"] is True
    assert rows["soda@11.0-10"]["default"] is True
    assert rows["example@1.0"]["installed"] is False
    assert payload["discovered"] == []


def test_cli_runners_list_human_output(tmp_path: Path, catalog: Catalog) -> None:
    paths = Paths.from_root(tmp_path / "state")

    result = invoke("runners", "list", paths=paths, catalog=catalog)

    assert result.exit_code == 0
    assert "soda@11.0-10" in result.output
    assert "default" in result.output
    assert "not installed" in result.output
    assert "WebView2" in result.output


def test_cli_runners_list_includes_reusable_runners(tmp_path: Path, catalog: Catalog) -> None:
    home = Path.home()
    bottles = home / ".var/app/com.usebottles.bottles/data/bottles/runners"
    _make_wine_dir(bottles / "soda-11.0-10")
    paths = Paths.from_root(tmp_path / "state")

    result = invoke("runners", "list", "--json", paths=paths, catalog=catalog)

    assert result.exit_code == 0
    discovered = json.loads(result.output)["discovered"]
    assert discovered == [
        {
            "path": str(bottles / "soda-11.0-10"),
            "ref": f"path:{bottles / 'soda-11.0-10'}",
        }
    ]


def test_cli_runners_install_does_not_touch_profiles(tmp_path: Path, catalog: Catalog) -> None:
    archive = make_tarball(tmp_path, {"bin/wine": b"x"})
    runner = make_runner(archive)
    custom = Catalog((runner,), catalog.webview2, catalog.latest_webview2_url)
    paths = Paths.from_root(tmp_path / "state")

    result = invoke("runners", "install", "soda@11.0-10", paths=paths, catalog=custom)

    assert result.exit_code == 0
    assert (paths.runners / "soda" / "11.0-10" / "bin/wine").is_file()
    assert not paths.profiles.exists()
    assert "installed soda@11.0-10" in result.output


def test_cli_runners_install_json(tmp_path: Path, catalog: Catalog) -> None:
    archive = make_tarball(tmp_path, {"bin/wine": b"x"})
    runner = make_runner(archive)
    custom = Catalog((runner,), catalog.webview2, catalog.latest_webview2_url)
    paths = Paths.from_root(tmp_path / "state")

    result = invoke(
        "runners", "install", "soda@11.0-10", "--json", paths=paths, catalog=custom
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "runner": "soda@11.0-10",
        "path": str(paths.runners / "soda" / "11.0-10"),
        "installed": True,
    }


def test_cli_runners_unknown_ref_exits_2(
    tmp_path: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = Paths.from_root(tmp_path / "state")
    monkeypatch.setattr("winbridge.cli._paths", lambda ctx: paths)
    monkeypatch.setattr("winbridge.cli._catalog", lambda ctx: catalog)
    monkeypatch.setattr(sys, "argv", ["winbridge", "runners", "install", "missing@1.0"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2
    assert "missing@1.0" in capsys.readouterr().err


def test_cli_runners_import_prints_reference(tmp_path: Path, catalog: Catalog) -> None:
    root = tmp_path / "existing-runner"
    (root / "bin").mkdir(parents=True)
    paths = Paths.from_root(tmp_path / "state")

    result = invoke("runners", "import", str(root), "--json", paths=paths, catalog=catalog)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["runner"] == f"path:{root}"
    assert payload["path"] == str(root)
    # Command logging is the only state a read-only import creates.
    assert not paths.runners.exists()
    assert not paths.profiles.exists()
    assert list(paths.logs.glob("*-runners.log"))


def test_cli_runners_import_missing_path_exits_1(
    tmp_path: Path,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = Paths.from_root(tmp_path / "state")
    monkeypatch.setattr("winbridge.cli._paths", lambda ctx: paths)
    monkeypatch.setattr("winbridge.cli._catalog", lambda ctx: catalog)
    monkeypatch.setattr(
        sys, "argv", ["winbridge", "runners", "import", str(tmp_path / "missing")]
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_cli_list_still_works_without_a_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing app-level ``list`` command must not load the catalog."""
    monkeypatch.setattr("winbridge.cli._paths", lambda ctx: Paths.from_root(tmp_path))

    result = CliRunner().invoke(app, ["list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {"apps": []}
