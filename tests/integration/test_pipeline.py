"""Full Wine pipeline against the in-repo synthetic Tauri fixture.

The test runs the installed ``winbridge`` console script, not an in-process
CLI, so real exit codes and real XDG state are exercised. It is skipped unless
``WINBRIDGE_INTEGRATION=1``; CI sets that and ``WINBRIDGE_FIXTURE_INSTALLER``
to the NSIS artifact built from ``fixtures/synthetic-tauri-app``. Setup uses
``--webview2 pinned`` so the checksummed payload is cached under the state root
and can be restored by CI instead of downloading it again. Setup runs with
``--no-verify``; the explicit ``verify`` below is the first app launch, which
avoids depending on a WebView2 second launch in the headless X11 container.

Every path the CLI writes lives under one disposable root. ``HOME`` and all
XDG variables point there, and the environment is scrubbed of Wayland and Wine
overrides so the test always selects the X11 adapter under ``xvfb-run``. The
runner store may be kept across CI runs with ``WINBRIDGE_INTEGRATION_HOME``;
per-app state is removed before the loop so the test still starts from nothing.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("WINBRIDGE_INTEGRATION") != "1",
        reason="needs Wine and WINBRIDGE_INTEGRATION=1",
    ),
]

#: A deliberately slow first paint; the fixture reads this through its Rust
#: command and delays the visible UI by this many milliseconds.
_FIXTURE_DELAY_MS = "3000"
_APP_ID = "fixture"
_SCHEME = "winbridge-fixture"

#: Root-level directories that belong to one run. ``runners`` is intentionally
#: absent: a cached runner store may be reused, but no profile, prefix, lock,
#: evidence, launcher, or config may survive into the next run.
_PER_RUN_STATE = (
    "config",
    "home",
    "runtime",
    "cache",
    "state",
    "data/applications",
    "data/winbridge/profiles",
    "data/winbridge/prefixes",
    "data/winbridge/evidence",
    "data/winbridge/logs",
    "data/winbridge/locks",
    "data/winbridge/bundles",
    "host-folder",
)


def run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    capture: bool = False,
    timeout: float = 1800,
) -> subprocess.CompletedProcess[str]:
    """Run one command and fail with the full command line and both streams.

    ``capture=True`` keeps stdout/stderr out of the CI log for callers that
    parse JSON. Otherwise output is replayed after completion so a long Wine
    phase still yields readable logs without a streaming wrapper.
    """
    display = shlex.join(str(part) for part in argv)
    try:
        completed = subprocess.run(
            argv,
            env=env,
            text=True,
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AssertionError(f"command not found: {display}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"command timed out after {timeout:g}s: {display}"
        ) from exc

    if not capture:
        sys.stdout.write(completed.stdout)
        sys.stdout.flush()
        sys.stderr.write(completed.stderr)
        sys.stderr.flush()

    if completed.returncode != 0:
        streams = []
        if completed.stdout:
            streams.append(f"stdout:\n{completed.stdout.rstrip()}")
        if completed.stderr:
            streams.append(f"stderr:\n{completed.stderr.rstrip()}")
        detail = "\n".join(streams) or "(no output)"
        raise AssertionError(
            f"command failed with exit {completed.returncode}: {display}\n{detail}"
        )
    return completed


def _winbridge_command() -> list[str]:
    """Resolve the console script so the test exercises the installed entry point."""
    script = shutil.which("winbridge")
    if script is None:
        candidate = Path(sys.executable).with_name("winbridge")
        if candidate.is_file():
            script = str(candidate)
    if script is None:
        raise AssertionError(
            "winbridge console script not found; run the test through 'uv run pytest'"
        )
    return [script]


def _state_root(tmp_path: Path) -> Path:
    """Pick the disposable root and clear everything that belongs to one run."""
    override = os.environ.get("WINBRIDGE_INTEGRATION_HOME")
    root = Path(override).expanduser().resolve() if override else tmp_path
    root.mkdir(parents=True, exist_ok=True)
    for relative in _PER_RUN_STATE:
        shutil.rmtree(root / relative, ignore_errors=True)
    return root


def _isolated_env(root: Path) -> dict[str, str]:
    """Build a subprocess environment that cannot see the real user session."""
    for relative in ("home", "config", "data", "cache", "state", "runtime"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "runtime").chmod(0o700)

    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "WAYLAND_DISPLAY",
            "WAYLAND_SOCKET",
            "HYPRLAND_INSTANCE_SIGNATURE",
            "WINEARCH",
            "WINEPREFIX",
        }
    }
    env.update(
        {
            "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
            "WINBRIDGE_FIXTURE_DELAY_MS": _FIXTURE_DELAY_MS,
        }
    )
    return env


def _inspect(winbridge: list[str], env: dict[str, str]) -> dict[str, object]:
    completed = run(
        [*winbridge, "inspect", _APP_ID, "--json"],
        env=env,
        capture=True,
        timeout=120,
    )
    return json.loads(completed.stdout)


def test_full_loop(tmp_path: Path) -> None:
    """setup -> inspect -> verify -> update -> expose -> unbridge, twice-checked."""
    installer_value = os.environ.get("WINBRIDGE_FIXTURE_INSTALLER")
    if not installer_value:
        raise AssertionError(
            "WINBRIDGE_FIXTURE_INSTALLER must point at winbridge-fixture-setup.exe"
        )
    installer = Path(installer_value).expanduser().resolve()
    assert installer.is_file(), f"fixture installer not found: {installer}"

    root = _state_root(tmp_path)
    env = _isolated_env(root)
    winbridge = _winbridge_command()
    profile_path = root / "data" / "winbridge" / "profiles" / f"{_APP_ID}.toml"
    prefix_path = root / "data" / "winbridge" / "prefixes" / _APP_ID

    run(
        [
            *winbridge,
            "setup",
            str(installer),
            "--id",
            _APP_ID,
            "--webview2",
            "pinned",
            "--no-verify",
            "--yes",
        ],
        env=env,
        timeout=1800,
    )
    assert profile_path.is_file(), f"setup did not write {profile_path}"
    payloads = list(
        (root / "data" / "winbridge" / "cache" / "downloads").glob("webview2-*.exe")
    )
    assert payloads, "pinned WebView2 payload was not cached under the state root"

    inspected = _inspect(winbridge, env)
    assert inspected["id"] == _APP_ID
    assert inspected["schemes"] == [_SCHEME], inspected["schemes"]
    assert inspected["installer"]["type"] == "nsis"  # type: ignore[index]
    assert str(inspected["exe"]).lower().endswith("winbridge-fixture.exe"), inspected["exe"]

    run(
        [*winbridge, "verify", _APP_ID, "--timeout", "300"],
        env=env,
        timeout=900,
    )
    verified = tomllib.loads(profile_path.read_text(encoding="utf-8"))
    assert verified["last_verified"]["result"] == "pass"
    assert verified["schemes"] == [_SCHEME]

    run(
        [*winbridge, "update", _APP_ID, "--no-verify"],
        env=env,
        timeout=1800,
    )
    updated = _inspect(winbridge, env)
    assert updated["schemes"] == [_SCHEME]
    assert updated["exe"] == inspected["exe"]
    assert profile_path.is_file(), "update removed the profile it was updating"

    host_folder = root / "host-folder"
    host_folder.mkdir()
    run(
        [*winbridge, "expose", _APP_ID, str(host_folder)],
        env=env,
        timeout=120,
    )
    exposed = _inspect(winbridge, env)["exposed"]
    assert [item["host"] for item in exposed] == [str(host_folder)]  # type: ignore[union-attr]

    run(
        [*winbridge, "unbridge", _APP_ID, "--yes"],
        env=env,
        timeout=900,
    )
    assert not profile_path.exists(), f"profile residue: {profile_path}"
    assert not prefix_path.exists(), f"prefix residue: {prefix_path}"
    assert not list((root / "data" / "winbridge" / "profiles").glob("fixture*"))
    assert not list((root / "data" / "applications").glob("winbridge-fixture*"))
    assert host_folder.is_dir(), "unbridge deleted a host folder it did not own"
