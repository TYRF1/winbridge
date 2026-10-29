"""Command-line entry point and exit-code mapping."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import tomli_w
import typer

from winbridge import __version__
from winbridge.backends.base import Backend, BackendError
from winbridge.core.config import Config, ConfigError
from winbridge.core.context import Context, build_context
from winbridge.core.cursor import (
    CURSOR_MODES,
    require_force_supported,
    start_force,
)
from winbridge.core.detect import detect_app_version
from winbridge.core.doctor import (
    BundleIdentity,
    CheckResult,
    run_checks,
    write_bundle,
)
from winbridge.core.errors import (
    UnsupportedEnvironment,
    UsageError,
    VerifyFailed,
    WinBridgeError,
)
from winbridge.core.expose import expose as expose_path
from winbridge.core.expose import unexpose as unexpose_path
from winbridge.core.launcher import desktop_id
from winbridge.core.locking import file_lock
from winbridge.core.profile import LastVerified, Profile
from winbridge.core.run import run_app
from winbridge.core.runner import (
    DEFAULT_CATALOG_PATH,
    Catalog,
    Runner,
    RunnerNotInstalled,
    discover_runners,
    import_runner,
    install_runner,
    runner_wine_path,
)
from winbridge.core.setup import (
    SetupOptions,
    preflight_setup,
    setup_installer,
)
from winbridge.core.state import Paths
from winbridge.core.unbridge import NoopBackend, unbridge
from winbridge.core.update import pin_runner, update_app
from winbridge.core.urlbridge import (
    login_bridge_status,
    open_url,
    register_schemes,
)
from winbridge.core.verify import (
    BackgroundUnavailable,
    VerifyResult,
    capture_background,
    require_desktop,
    verify_app,
    watch_launch,
)
from winbridge.util.log import evidence_dir, redact_url, setup_logging

__all__ = ["app", "main"]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"winbridge {__version__}")
        raise typer.Exit()


app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Run Windows Tauri/WebView2 desktop builds on Linux.",
)


def _try_setup_logging(
    paths: Paths,
    *,
    command: str,
    app_id: str | None = None,
    version: str = "unknown",
    verbose: bool = False,
) -> bool:
    """Configure logging without making a read-only state tree fatal.

    ``list`` and ``doctor`` must still report when the log directory cannot be
    written; a broken log destination is a warning, not the command's result.
    """
    try:
        setup_logging(
            paths,
            command=command,
            app_id=app_id,
            version=version,
            verbose=verbose,
        )
    except (OSError, ValueError) as exc:
        typer.echo(
            f"warning: logging unavailable under {paths.logs}: {exc}",
            err=True,
        )
        return False
    return True


@app.callback()
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            expose_value=False,
            help="Show the version and exit.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Print detailed progress information."),
    ] = False,
) -> None:
    """WinBridge command group."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    command = ctx.invoked_subcommand or "winbridge"
    ctx.obj["command"] = command
    # Every invocation gets a command log and the rolling global log. Commands
    # that resolve an app rebind to its evidence directory once the id (and, if
    # known, version) are available. A read-only state tree must not hide a
    # doctor or list result.
    if _try_setup_logging(_paths(ctx), command=command, verbose=verbose):
        logging.getLogger("winbridge").info("[cli] %s", command)


def _log_version(value: str | None) -> str:
    """Return a version safe for an evidence path, or ``unknown``."""
    text = (value or "").strip()
    if not text or text in {".", ".."} or any(char in text for char in ("/", "\\", "\x00")):
        return "unknown"
    return text


def _bind_app_logging(
    ctx: typer.Context,
    paths: Paths,
    app_id: str,
    version: str | None = "unknown",
) -> None:
    """Move an app-scoped command's log under its evidence directory."""
    resolved = _log_version(version)
    obj = ctx.ensure_object(dict)
    key = (app_id, resolved)
    if obj.get("log_app") == key:
        return
    if _try_setup_logging(
        paths,
        command=str(obj.get("command") or "winbridge"),
        app_id=app_id,
        version=resolved,
        verbose=bool(obj.get("verbose", False)),
    ):
        logging.getLogger("winbridge").info(
            "[cli] %s app %s version %s",
            obj.get("command") or "winbridge",
            app_id,
            resolved,
        )
    obj["log_app"] = key


def _profile_log_version(profile: Profile) -> str:
    if profile.last_verified is not None:
        return _log_version(profile.last_verified.app_version)
    return "unknown"


def _paths(ctx: typer.Context) -> Paths:
    """Resolve state paths, allowing tests and callers to inject a root."""
    obj = ctx.ensure_object(dict)
    override = obj.get("paths")
    if isinstance(override, Paths):
        return override
    return Paths.default()


def _catalog(ctx: typer.Context) -> Catalog:
    """Load the embedded catalog, allowing callers to inject one."""
    obj = ctx.ensure_object(dict)
    override = obj.get("catalog")
    if isinstance(override, Catalog):
        return override
    return Catalog.load(DEFAULT_CATALOG_PATH)


def _runner_installed(paths: Paths, runner: Runner) -> bool:
    root = paths.runners / runner.id / runner.version
    return runner_wine_path(root, runner).is_file()


def _profile_path(paths: Paths, app_id: str) -> Path:
    if app_id in {"", ".", ".."} or any(char in app_id for char in ("/", "\\", "\x00")):
        raise UsageError(f"invalid app id: {app_id!r}")
    return paths.profiles / f"{app_id}.toml"


def _load_profiles(paths: Paths) -> tuple[list[Profile], list[tuple[Path, str]]]:
    """Load every profile, returning good ones plus corrupt paths and reasons."""
    profiles: list[Profile] = []
    corrupt: list[tuple[Path, str]] = []
    if paths.profiles.is_dir():
        for path in sorted(paths.profiles.glob("*.toml")):
            if path.name.startswith("."):
                continue
            try:
                profiles.append(Profile.load(path))
            except (OSError, WinBridgeError) as exc:
                corrupt.append((path, str(exc)))
    profiles.sort(key=lambda profile: profile.id)
    return profiles, corrupt


def _load_profile(paths: Paths, app_id: str) -> Profile:
    """Load exactly one profile, refusing unknown ids with an actionable error."""
    path = _profile_path(paths, app_id)
    if not path.is_file():
        raise WinBridgeError(f"no profile for app {app_id!r}; run 'winbridge list'")
    return Profile.load(path)


def _optional_profile(paths: Paths, app_id: str) -> Profile | None:
    """Load one profile when present; unbridge treats a missing one as stale."""
    path = _profile_path(paths, app_id)
    if not path.is_file():
        return None
    return Profile.load(path)


def _profile_context(ctx: typer.Context, profile: Profile) -> Context:
    """Build the service context for one app, bound to its recorded runner.

    The profile's runner wins over the global default: a prefix is tied to the
    Wine build that created it, so ``run`` must not silently pick another one.
    """
    obj = ctx.ensure_object(dict)
    override = obj.get("context")
    if isinstance(override, Context):
        return override
    paths = _paths(ctx)
    config = replace(Config.load(paths.config), default_runner=profile.runner)
    return build_context(paths, config, _catalog(ctx), profile.backend)


def _setup_context(
    ctx: typer.Context,
    paths: Paths,
    catalog: Catalog,
    backend_name: str,
    runner: str | None,
    *,
    install_missing: bool = False,
) -> Context:
    """Resolve the backend, desktop, and runner setup will use.

    An explicit ``--runner`` overrides the global default before the runner is
    resolved, so setup and the profile agree on the exact ``id@version``. Setup
    asks for ``install_missing=True``: the catalog default is downloaded and
    verified on a clean machine instead of requiring a separate install step.
    """
    obj = ctx.ensure_object(dict)
    override = obj.get("context")
    if isinstance(override, Context):
        return override
    config = Config.load(paths.config)
    if config.backend != backend_name:
        config = replace(config, backend=backend_name)
    if runner is not None:
        config = replace(config, default_runner=runner)
    return build_context(
        paths,
        config,
        catalog,
        backend_name,
        install_missing_runner=install_missing,
        report=typer.echo,
    )


@dataclass(frozen=True, slots=True)
class _UnbridgeServices:
    """The backend and desktop adapter unbridge needs, and nothing else."""

    backend: Backend
    desktop: object | None


def _unbridge_context(
    ctx: typer.Context, paths: Paths, app_id: str, profile: Profile | None
) -> _UnbridgeServices:
    """Resolve the services unbridge needs, with or without a live prefix.

    A missing prefix needs no runner: cleanup still removes the profile, both
    entries, scheme associations, and the pin, so the metadata-only path gets a
    no-op backend instead of resolving a runner (or even loading the global
    config and catalog). A prefix that exists needs a usable runner to stop and
    remove it; that failure is reported before ``unbridge`` deletes anything.
    """
    obj = ctx.ensure_object(dict)
    override = obj.get("context")
    if isinstance(override, Context):
        return _UnbridgeServices(
            backend=override.backend,
            desktop=override.desktop,
        )
    if not _prefix_requires_backend(paths, app_id):
        return _UnbridgeServices(backend=NoopBackend(), desktop=None)

    try:
        if profile is not None:
            context = _profile_context(ctx, profile)
        else:
            context = build_context(paths, Config.load(paths.config), _catalog(ctx))
    except (BackendError, RunnerNotInstalled) as exc:
        raise WinBridgeError(
            f"runner missing for {app_id!r}: {exc}; "
            "install the runner and retry (nothing was removed)"
        ) from exc
    return _UnbridgeServices(backend=context.backend, desktop=context.desktop)


def _prefix_requires_backend(paths: Paths, app_id: str) -> bool:
    """Only a real prefix directory needs a runner to stop and delete it.

    A symlink or a non-directory is handled by ``unbridge`` itself, which
    refuses it before any backend call. Treating those as metadata-only keeps
    the safety error (the path is not ours) visible instead of masking it with a
    runner-resolution error.
    """
    prefix = paths.prefix_for(app_id)
    return prefix.is_dir() and not prefix.is_symlink()


def _split_silent_args(value: str) -> list[str]:
    r"""Split a Windows-style flag string without eating backslashes.

    POSIX ``shlex.split`` treats ``\`` as an escape, so ``/D=C:\App`` becomes
    ``/D=C:App``. This parser keeps every backslash, honours double or single
    quotes anywhere in a token (``/D="C:\Program Files\App"``), and removes the
    quote characters. An unterminated quote is a usage error.
    """
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for character in value:
        if quote is not None:
            if character == quote:
                quote = None
            else:
                current.append(character)
            continue
        if character in {'"', "'"}:
            quote = character
        elif character.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(character)
    if quote is not None:
        raise UsageError(f"--silent-args has an unterminated {quote} quote")
    if current:
        tokens.append("".join(current))
    return tokens


def _is_verified(profile: Profile) -> bool:
    return profile.last_verified is not None and profile.last_verified.result == "pass"


def _utc_now() -> str:
    """Return the profile timestamp format used by ``created_at``."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _profile_app_version(profile: Profile, paths: Paths, backend: Backend) -> str:
    """Resolve the version recorded with a verify result and its evidence path."""
    installer = profile.installer.path if profile.installer is not None else None
    return detect_app_version(
        backend,
        paths.prefix_for(profile.id),
        profile.exe or None,
        installer,
    )


def _last_verified(
    profile: Profile,
    *,
    app_version: str,
    result: str,
    first_paint_s: float | None,
) -> LastVerified:
    """Build the new record without dropping unknown nested keys from schema 1."""
    extra = dict(profile.last_verified.extra) if profile.last_verified is not None else {}
    return LastVerified(
        app_version=app_version,
        result=result,
        first_paint_s=first_paint_s,
        at=_utc_now(),
        extra=extra,
    )


runners_app = typer.Typer(
    no_args_is_help=True,
    help="List, install, import, and pin Wine runners.",
)
app.add_typer(runners_app, name="runners")


@runners_app.command("list")
def runners_list(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """List catalog runners, installed state, and reusable runners on disk."""
    paths = _paths(ctx)
    catalog = _catalog(ctx)
    discovered = discover_runners(paths)
    rows = [
        {
            "ref": runner.ref,
            "id": runner.id,
            "version": runner.version,
            "kind": runner.kind,
            "default": runner.default,
            "installed": _runner_installed(paths, runner),
            "url": runner.url,
            "sha256": runner.sha256,
        }
        for runner in catalog.runners
    ]

    if json_output:
        payload = {
            "runners": rows,
            "discovered": [{"path": str(path), "ref": f"path:{path}"} for path in discovered],
            "webview2": {
                "version": catalog.webview2.version,
                "pinned_url": catalog.webview2.url,
                "latest_url": catalog.latest_webview2_url,
            },
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    typer.echo("REF\tKIND\tSTATUS")
    for row in rows:
        status = "installed" if row["installed"] else "not installed"
        if row["default"]:
            status = f"{status} (default)"
        typer.echo(f"{row['ref']}\t{row['kind']}\t{status}")
    if discovered:
        typer.echo("Reusable runners (import by reference, never copied):")
        for path in discovered:
            typer.echo(f"  path:{path}")
    else:
        typer.echo("No reusable runners found.")
    typer.echo(
        f"WebView2: pinned {catalog.webview2.version}; "
        f"latest bootstrapper {catalog.latest_webview2_url}"
    )


@runners_app.command("install")
def runners_install(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Catalog reference, for example soda@11.0-10.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Install a catalog runner into the managed store."""
    paths = _paths(ctx)
    runner = _catalog(ctx).get(ref)
    root = install_runner(runner, paths)
    if json_output:
        payload = {"runner": runner.ref, "path": str(root), "installed": True}
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"installed {runner.ref} at {root}")


@runners_app.command("import")
def runners_import(
    directory: Annotated[
        str,
        typer.Argument(help="Existing runner root, for example from Bottles or Steam."),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Record an existing runner by reference; nothing is copied or changed."""
    runner = import_runner(directory)
    if json_output:
        typer.echo(json.dumps({"runner": runner.ref, "path": runner.url}, indent=2))
        return
    typer.echo(f"imported by reference: {runner.ref}")
    typer.echo("Use this reference with 'winbridge setup --runner'; nothing was copied.")


@runners_app.command("pin")
def runners_pin(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Argument(help="App id from 'winbridge list'.")],
    ref: Annotated[
        str,
        typer.Argument(help="Runner 'id@version' or 'path:/absolute/root'."),
    ],
    rebuild: Annotated[
        bool,
        typer.Option(
            "--rebuild",
            help="Recreate the prefix under the new runner, reinstalling the app.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Do not ask for confirmation."),
    ] = False,
) -> None:
    """Record a runner for one app, or rebuild its prefix under it."""
    paths = _paths(ctx)
    profile = _load_profile(paths, app_id)
    _bind_app_logging(ctx, paths, profile.id, _profile_log_version(profile))
    if not rebuild:
        updated = pin_runner(profile, ref, paths=paths, confirm=False)
        typer.echo(
            f"recorded {updated.runner} for {updated.id} "
            "(record-only; the prefix did not change)"
        )
        return

    catalog = _catalog(ctx)
    context = _setup_context(ctx, paths, catalog, profile.backend, ref)
    if not yes:
        typer.confirm(
            f"Recreate the Windows prefix for {profile.name!r} ({profile.id}) "
            f"under {ref}? Its installed state is deleted.",
            abort=True,
        )
    updated = pin_runner(
        profile,
        ref,
        rebuild=True,
        backend=context.backend,
        paths=paths,
        catalog=context.catalog,
        config=context.config,
        desktop=context.desktop,
        confirm=False,
    )
    typer.echo(f"rebuilt {updated.id} under {updated.runner}")


@app.command("setup")
def setup_command(
    ctx: typer.Context,
    installer: Annotated[
        Path,
        typer.Argument(help="Windows installer (.exe or .msi) to bridge."),
    ],
    app_id: Annotated[
        str | None,
        typer.Option("--id", help="App id; defaults to a slug of the installer name."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an existing profile after backing it up."),
    ] = False,
    runner: Annotated[
        str | None,
        typer.Option("--runner", help="Runner 'id@version' or 'path:/absolute/root'."),
    ] = None,
    exe: Annotated[
        str | None,
        typer.Option("--exe", help="Guest path of the main executable."),
    ] = None,
    scheme: Annotated[
        list[str] | None,
        typer.Option("--scheme", help="Record an additional URL scheme; repeatable."),
    ] = None,
    silent_args: Annotated[
        str | None,
        typer.Option(
            "--silent-args",
            help="Override the detected silent flags, for example '/S /norestart'.",
        ),
    ] = None,
    backend: Annotated[
        str,
        typer.Option("--backend", help="Execution backend (wine in v0.1)."),
    ] = "wine",
    webview2: Annotated[
        str,
        typer.Option(
            "--webview2",
            help=(
                "Runtime source: catalog 'pinned' (default) or 'latest' "
                "Evergreen (experimental)."
            ),
        ),
    ] = "pinned",
    keep_z_drive: Annotated[
        bool,
        typer.Option("--keep-z-drive", help="Keep Wine's Z: host-root link."),
    ] = False,
    manual: Annotated[
        bool,
        typer.Option(
            "--manual",
            help="Fall back to the GUI installer when silent install fails.",
        ),
    ] = False,
    verify: Annotated[
        bool | None,
        typer.Option(
            "--verify/--no-verify",
            help="Force or skip the render check; the default is automatic.",
        ),
    ] = None,
    no_shell_restart: Annotated[
        bool,
        typer.Option(
            "--no-shell-restart",
            help="Do not restart omarchy-shell after pinning.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Do not ask for input."),
    ] = False,
) -> None:
    """Install an app with zero required input."""
    parsed_silent_args = (
        _split_silent_args(silent_args) if silent_args is not None else None
    )

    options = SetupOptions(
        app_id=app_id,
        runner=runner,
        exe=exe,
        schemes=scheme,
        silent_args=parsed_silent_args,
        backend=backend,
        webview2=webview2,
        keep_z_drive=keep_z_drive,
        manual=manual,
        verify=verify,
        shell_restart=not no_shell_restart,
        yes=yes,
        force=force,
    )
    paths = _paths(ctx)
    catalog = _catalog(ctx)
    app_id = preflight_setup(installer, paths, options)
    _bind_app_logging(ctx, paths, app_id)
    context = _setup_context(
        ctx, paths, catalog, backend, runner, install_missing=True
    )
    profile = setup_installer(
        installer,
        paths,
        context.config,
        catalog,
        context.backend,
        context.desktop,
        options,
    )
    typer.echo(f"setup: {profile.name} ({profile.id}) is ready")


@app.command("update")
def update_command(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Argument(help="App id from 'winbridge list'.")],
    installer: Annotated[
        Path | None,
        typer.Option(
            "--installer",
            help="Use this installer instead of scanning the recorded directory.",
        ),
    ] = None,
    verify: Annotated[
        bool | None,
        typer.Option(
            "--verify/--no-verify",
            help="Force or skip the render check; the default is automatic.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Accept a zero-exit reinstall with no executable change when "
                "the recorded exe still exists (same-build reinstall)."
            ),
        ),
    ] = False,
) -> None:
    """Reinstall an app from its newest matching installer."""
    paths = _paths(ctx)
    profile = _load_profile(paths, app_id)
    _bind_app_logging(ctx, paths, profile.id, _profile_log_version(profile))
    context = _profile_context(ctx, profile)
    updated = update_app(
        profile,
        paths,
        context.backend,
        context.desktop,
        installer=installer,
        verify=verify,
        force=force,
    )
    typer.echo(f"update: {updated.id} ({updated.name}) is ready on {updated.runner}")


@app.command("list")
def list_apps(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """List installed apps."""
    profiles, corrupt = _load_profiles(_paths(ctx))
    if not json_output:
        for path, error in corrupt:
            typer.echo(f"warning: skipping corrupt profile {path}: {error}", err=True)
    if json_output:
        apps = [
            {
                "id": profile.id,
                "name": profile.name,
                "runner": profile.runner,
                "verified": _is_verified(profile),
            }
            for profile in profiles
        ]
        payload: dict[str, object] = {"apps": apps}
        if corrupt:
            payload["corrupt"] = [
                {"path": str(path), "error": error} for path, error in corrupt
            ]
        typer.echo(json.dumps(payload, indent=2))
        return
    if not profiles:
        typer.echo("No apps installed.")
        return
    typer.echo("ID\tNAME\tRUNNER\tSTATUS")
    for profile in profiles:
        status = "verified" if _is_verified(profile) else "not verified"
        typer.echo(f"{profile.id}\t{profile.name}\t{profile.runner}\t{status}")


@app.command("inspect")
def inspect_app(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Argument(help="App id from 'winbridge list'.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Print one app profile."""
    data = _load_profile(_paths(ctx), app_id).to_dict()
    if json_output:
        typer.echo(json.dumps(data, indent=2, default=str))
    else:
        typer.echo(tomli_w.dumps(data).rstrip())


@app.command("unbridge")
def unbridge_command(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Argument(help="App id from 'winbridge list'.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Do not ask for confirmation."),
    ] = False,
) -> None:
    """Remove an app and every artifact WinBridge created for it."""
    paths = _paths(ctx)
    profile: Profile | None = None
    try:
        profile = _optional_profile(paths, app_id)
    except UsageError:
        raise
    except (OSError, WinBridgeError) as exc:
        typer.echo(f"warning: {exc}", err=True)
    _bind_app_logging(
        ctx,
        paths,
        app_id,
        _profile_log_version(profile) if profile is not None else "unknown",
    )

    if not yes:
        label = f"{profile.name!r} ({app_id})" if profile is not None else repr(app_id)
        typer.confirm(f"Remove {label} and every WinBridge artifact?", abort=True)

    services = _unbridge_context(ctx, paths, app_id, profile)
    unbridge(
        app_id,
        paths,
        services.backend,
        desktop=services.desktop,
        confirm=False,
    )
    typer.echo(f"unbridged {app_id}")


@app.command("expose")
def expose_command(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Argument(help="App id from 'winbridge list'.")],
    host_folder: Annotated[
        str,
        typer.Argument(help="Absolute host folder to link into the app's profile."),
    ],
    name: Annotated[
        str | None,
        typer.Option(
            "--as",
            help="Guest folder name; defaults to the host folder name.",
        ),
    ] = None,
) -> None:
    """Link a host folder into the app's Windows user profile."""
    paths = _paths(ctx)
    profile = _load_profile(paths, app_id)
    _bind_app_logging(ctx, paths, profile.id, _profile_log_version(profile))
    item = expose_path(profile, paths, host_folder, name=name)
    profile.dump(_profile_path(paths, app_id))
    typer.echo(f"exposed {item.host} -> {item.guest}")


@app.command("unexpose")
def unexpose_command(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Argument(help="App id from 'winbridge list'.")],
    ref: Annotated[
        str,
        typer.Argument(help="Host folder path or guest folder name to remove."),
    ],
) -> None:
    """Remove one host-folder link owned by WinBridge."""
    paths = _paths(ctx)
    profile = _load_profile(paths, app_id)
    _bind_app_logging(ctx, paths, profile.id, _profile_log_version(profile))
    item = unexpose_path(profile, paths, ref)
    profile.dump(_profile_path(paths, app_id))
    typer.echo(f"removed {item.guest} -> {item.host}")


def _format_pids(pids: set[int]) -> str:
    """Render blocking PIDs deterministically for human-readable output."""
    return ", ".join(str(pid) for pid in sorted(pids))


class _RunReporter:
    """Progress for ``run``: human ``[run +Xs]`` lines or NDJSON events.

    Every state transition goes through this one object so the human and the
    NDJSON mode cannot drift. ``started`` is the display clock for stage lines;
    it deliberately includes lock wait and pre-launch work. The wait budget and
    ``first_paint_s`` use a separate launch-relative origin (see
    ``_wait_for_render``), so pre-launch time is never charged to ``--timeout``.
    ``json_output`` replaces every human line with one flushed JSON object per
    line, so stdout is pure NDJSON and parses line by line while streaming.
    Exactly one terminal result is emitted per run, even when a launch raises.
    """

    def __init__(
        self,
        *,
        json_output: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._json = json_output
        self._clock = clock or time.monotonic
        self._finished = False
        self.started = self._clock()

    def _elapsed(self) -> float:
        return max(self._clock() - self.started, 0.0)

    def _finish(self) -> bool:
        """Claim the single terminal result slot for this run."""
        if self._finished:
            return False
        self._finished = True
        return True

    def _emit(self, payload: dict[str, object]) -> None:
        # ``typer.echo`` takes no ``flush``; streaming taps need each line now.
        print(json.dumps(payload), flush=True)

    def _human(self, label: str, text: str, elapsed: float) -> None:
        typer.echo(f"[run +{elapsed:.2f}s] {label}: {text}")

    def stage(self, stage: str, status: str) -> None:
        """Report one lifecycle transition (stop/launch/window/paint)."""
        elapsed = self._elapsed()
        if self._json:
            self._emit(
                {
                    "event": "stage",
                    "stage": stage,
                    "status": status,
                    "elapsed_s": elapsed,
                }
            )
            return
        self._human(stage, status, elapsed)

    def _result(
        self,
        status: str,
        reason: str,
        elapsed: float,
        *,
        first_paint_s: float | None = None,
        evidence: Path | None = None,
    ) -> None:
        self._emit(
            {
                "event": "result",
                "status": status,
                "reason": reason,
                "elapsed_s": elapsed,
                "first_paint_s": first_paint_s,
                "evidence": None if evidence is None else str(evidence),
            }
        )

    def launched(self) -> None:
        """Finish a launch that did not wait for paint."""
        if not self._finish():
            return
        elapsed = self._elapsed()
        if self._json:
            self._result("launched", "no-wait", elapsed)
            return
        self._human("result", "launched (no wait)", elapsed)

    def already_running(self, pids: set[int] | None = None) -> None:
        """Finish the default path for a live instance without spawning."""
        if not self._finish():
            return
        elapsed = self._elapsed()
        if self._json:
            self._result("already-running", "already-running", elapsed)
            return
        detail = "already running; use --restart"
        if pids:
            detail = f"already running (pids {_format_pids(pids)}); use --restart"
        self._human("result", detail, elapsed)

    def passed(self, result: VerifyResult, evidence: Path | None) -> None:
        """Finish a waiting launch that observed two visible frames."""
        if not self._finish():
            return
        elapsed = self._elapsed()
        if self._json:
            self._result(
                "pass",
                result.reason,
                elapsed,
                first_paint_s=result.first_paint_s,
                evidence=evidence,
            )
            return
        # The parenthetical is total wait time, not first paint; it shares the
        # stage prefix's clock so the two numbers always agree.
        typer.echo(f"[run +{elapsed:.2f}s] result: PASS ({elapsed:.2f}s)")
        self._evidence(evidence)

    def failed(
        self,
        reason: str,
        evidence: Path | None = None,
        *,
        first_paint_s: float | None = None,
    ) -> None:
        """Finish a waiting launch with the state that stopped it."""
        if not self._finish():
            return
        elapsed = self._elapsed()
        if self._json:
            self._result(
                "fail",
                reason,
                elapsed,
                first_paint_s=first_paint_s,
                evidence=evidence,
            )
            return
        self._human("result", f"FAIL ({reason})", elapsed)
        self._evidence(evidence)

    def unsupported(self) -> None:
        """Finish a wait the session cannot observe; JSON mode only.

        The human path stays silent so ``main`` can print the unsupported
        error to stderr and exit 3 exactly as before. Both modes claim the
        terminal slot so the error wrapper cannot add a bogus second result.
        """
        if not self._finish():
            return
        if self._json:
            self._result("fail", "unsupported", self._elapsed())

    def failed_if_unfinished(
        self,
        reason: str,
        evidence: Path | None = None,
    ) -> None:
        """Emit a terminal failure unless a result was already reported."""
        if not self._finished:
            self.failed(reason, evidence=evidence)

    def _evidence(self, evidence: Path | None) -> None:
        if evidence is not None:
            self._human("evidence", str(evidence), self._elapsed())


def _wait_for_render(
    profile: Profile,
    paths: Paths,
    context: Context,
    reporter: _RunReporter,
    *,
    argv: list[str],
    url: str | None,
    restart: bool,
    timeout: float,
    cursor_force: bool = False,
) -> None:
    """Launch, then prove the app painted before returning.

    The desktop is validated before the lock and before any kill, so an
    unsupported session never destroys a running app. Version detection, the
    evidence directory, and the background capture all run inside
    ``before_start``: after a restart's kill and the live-instance probe, but
    before anything is spawned. That ordering keeps an already-running wait
    free of empty evidence directories and stale registry reads, and it keeps
    ``--timeout`` from being consumed by lock wait or setup work.

    Observation shares ``watch_launch`` with verify and runs under the same
    per-app lock for the whole wait. The launch-relative clock is captured last
    in ``before_start``, immediately before the spawn, and is the only origin
    passed to the watcher; ``reporter.started`` stays the display clock.
    """
    try:
        desktop = require_desktop(context.desktop)
    except UnsupportedEnvironment:
        # The human path lets ``main`` report the error on stderr; NDJSON needs
        # a terminal event because the only other stdout would be empty.
        reporter.unsupported()
        raise

    background: Path | None = None
    run_dir: Path | None = None
    launched_at: float | None = None

    def before_start() -> None:
        nonlocal background, run_dir, launched_at
        version = _profile_app_version(profile, paths, context.backend)
        run_dir = evidence_dir(paths, profile.id, version)
        background = capture_background(desktop, run_dir)
        # The observation origin: everything before this point (lock wait,
        # version detection, background capture) is excluded from both the
        # timeout budget and ``first_paint_s``.
        launched_at = time.monotonic()

    lock = paths.locks / f"app-{profile.id}.lock"
    with file_lock(lock):
        try:
            launch = run_app(
                profile,
                paths,
                context.backend,
                argv=argv,
                url=url,
                restart=restart,
                before_start=before_start,
                on_stage=reporter.stage,
            )
        except BackgroundUnavailable as exc:
            # The missing signal is fatal: wallpaper must never count as paint.
            reporter.failed("background", evidence=run_dir)
            raise VerifyFailed(
                f"run failed: background: {exc}; evidence: {run_dir}"
            ) from exc

        if launch.popen is None:
            # A live instance was reused or the URL was forwarded into it. The
            # wait only passes for a launch it owns, so never claim its paint.
            reporter.failed("already-running")
            blocked = (
                f" (pids {_format_pids(launch.pid_tree)})"
                if launch.pid_tree
                else ""
            )
            if url is not None:
                raise VerifyFailed(
                    f"run failed: already-running{blocked}; the URL was "
                    "forwarded to the running instance, so there is nothing new "
                    "to observe"
                )
            raise VerifyFailed(
                f"run failed: already-running{blocked}; "
                "use 'run --restart --wait'"
            )

        if background is None or run_dir is None or launched_at is None:
            # Fail closed under ``-O`` too: a spawned launch always ran the
            # pre-start hook, so missing state means observation is unsafe.
            reporter.failed("background", evidence=run_dir)
            raise VerifyFailed(
                "run failed: background: launch ran without a background capture"
            )

        if cursor_force:
            # The helper is independent: a spawn failure is logged there and
            # never turns a successful launch into a failed wait.
            start_force(profile, paths, launch.pid_tree)

        result = watch_launch(
            launch,
            paths.prefix_for(profile.id),
            desktop,
            run_dir,
            background,
            started=launched_at,
            timeout=timeout,
            on_stage=reporter.stage,
        )

    evidence = result.evidence_dir or run_dir
    if not result.passed:
        reporter.failed(
            result.reason,
            evidence=evidence,
            first_paint_s=result.first_paint_s,
        )
        raise VerifyFailed(f"run failed: {result.reason}; evidence: {evidence}")
    reporter.passed(result, evidence)


@app.command("run")
def run_command(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Argument(help="App id from 'winbridge list'.")],
    url: Annotated[
        str | None,
        typer.Option("--url", help="Forward a URL to the app instead of only launching it."),
    ] = None,
    restart: Annotated[
        bool,
        typer.Option("--restart", help="Stop a running instance first; the only kill path."),
    ] = False,
    wait: Annotated[
        bool,
        typer.Option(
            "--wait",
            help="Wait until two visible frames prove the app rendered.",
        ),
    ] = False,
    timeout: Annotated[
        int | None,
        typer.Option(
            "--timeout",
            help="Seconds to wait for a visible frame; requires --wait (default 300).",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit one JSON object per line (NDJSON); suppresses human lines.",
        ),
    ] = False,
    cursor: Annotated[
        str,
        typer.Option(
            "--cursor",
            help=(
                "Experimental X11/XWayland arrow fallback for a missing cursor "
                "(off|force; default off)."
            ),
        ),
    ] = "off",
    app_args: Annotated[
        list[str] | None,
        typer.Argument(help="Arguments for the app; put them after --.", show_default=False),
    ] = None,
) -> None:
    """Launch an installed app without disturbing a running instance."""
    if cursor not in CURSOR_MODES:
        # Validate before the profile or prefix is touched: this is a usage
        # error, not a failed launch.
        raise UsageError(
            f"--cursor must be one of {', '.join(sorted(CURSOR_MODES))}; got {cursor!r}"
        )
    if url is not None and restart:
        # Forwarding must never restart; reject before touching any state.
        raise UsageError("--url cannot be combined with --restart")
    if timeout is not None and timeout < 0:
        raise UsageError("--timeout must not be negative")
    if timeout is not None and not wait:
        # Validate before the profile or prefix is touched: this is a usage
        # error, not a failed launch.
        raise UsageError("--timeout requires --wait")
    paths = _paths(ctx)
    profile = _load_profile(paths, app_id)
    _bind_app_logging(ctx, paths, profile.id, _profile_log_version(profile))
    context = _profile_context(ctx, profile)
    reporter = _RunReporter(json_output=json_output)
    if cursor == "force":
        # Before run_app: an unsupported session must never kill a running app
        # (``--restart``) only to discover it cannot force the cursor. NDJSON
        # callers still get one terminal event when this preflight refuses.
        try:
            require_force_supported()
        except UnsupportedEnvironment:
            reporter.unsupported()
            raise
    argv = list(app_args or [])

    if not wait:
        try:
            launch = run_app(
                profile,
                paths,
                context.backend,
                argv=argv,
                url=url,
                restart=restart,
                on_stage=reporter.stage,
            )
        except WinBridgeError:
            # JSON callers still need one terminal event when the launch dies.
            reporter.failed_if_unfinished("launch")
            raise
        if cursor == "force":
            # Even a reused live instance can be forced; do not wait for it.
            start_force(profile, paths, launch.pid_tree)
        if launch.popen is None and url is None:
            reporter.already_running(launch.pid_tree)
        else:
            reporter.launched()
        return

    try:
        _wait_for_render(
            profile,
            paths,
            context,
            reporter,
            argv=argv,
            url=url,
            restart=restart,
            timeout=300.0 if timeout is None else float(timeout),
            cursor_force=cursor == "force",
        )
    except WinBridgeError:
        # A launch/backend error can escape before the watcher reports its own
        # result; the reporter emits at most one terminal event.
        reporter.failed_if_unfinished("launch")
        raise


def _verify_payload(
    profile: Profile,
    *,
    version: str,
    desktop_name: str,
    passed: bool,
    result: str,
    reason: str,
    first_paint_s: float | None = None,
    window_class: str | None = None,
    frames: list[Path] | None = None,
    evidence: Path | None = None,
    transparent_frames: int = 0,
) -> dict[str, object]:
    items = list(frames or [])
    return {
        "app": profile.id,
        "app_version": version,
        "runner": profile.runner,
        "desktop": desktop_name,
        "passed": passed,
        "result": result,
        "reason": reason,
        "first_paint_s": first_paint_s,
        "window_class": window_class,
        "frames": [str(frame) for frame in items],
        "evidence": None if evidence is None else str(evidence),
        "cursor_checked": False,
        "transparent_frames": transparent_frames,
    }


@app.command("verify")
def verify_command(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Argument(help="App id from 'winbridge list'.")],
    timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            help="Seconds to wait for a visible (non-blank, non-background) window.",
        ),
    ] = 300,
    keep_all: Annotated[
        bool,
        typer.Option("--keep-all", help="Keep every captured frame, not just the key frames."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Launch an app, prove it rendered, and keep evidence frames."""
    paths = _paths(ctx)
    profile = _load_profile(paths, app_id)
    context = _profile_context(ctx, profile)
    version = _profile_app_version(profile, paths, context.backend)
    _bind_app_logging(ctx, paths, profile.id, version)
    desktop_name = context.desktop.name if context.desktop is not None else "unsupported"
    try:
        result = verify_app(
            profile,
            paths,
            context.backend,
            context.desktop,
            timeout=timeout,
            keep_all=keep_all,
        )
    except UnsupportedEnvironment as exc:
        # A desktop that cannot observe or capture is not a failed render.
        profile.last_verified = _last_verified(
            profile,
            app_version=version,
            result="skipped",
            first_paint_s=None,
        )
        profile.dump(_profile_path(paths, app_id))
        if json_output:
            typer.echo(
                json.dumps(
                    _verify_payload(
                        profile,
                        version=version,
                        desktop_name=desktop_name,
                        passed=False,
                        result="skipped",
                        reason=str(exc),
                    ),
                    indent=2,
                )
            )
        raise
    except UsageError:
        raise
    except WinBridgeError as exc:
        # A backend, compositor, or capture tool failed while proving the
        # render. That is a failed verify (exit 4), not a generic failure.
        profile.last_verified = _last_verified(
            profile,
            app_version=version,
            result="fail",
            first_paint_s=None,
        )
        profile.dump(_profile_path(paths, app_id))
        detail = f"verify failed: {exc}"
        if json_output:
            typer.echo(
                json.dumps(
                    _verify_payload(
                        profile,
                        version=version,
                        desktop_name=desktop_name,
                        passed=False,
                        result="fail",
                        reason=str(exc),
                    ),
                    indent=2,
                )
            )
        else:
            typer.echo(f"[verify] app: {profile.id}")
            typer.echo(f"[verify] app version: {version}")
            typer.echo(f"[verify] runner: {profile.runner}")
            typer.echo(f"[verify] desktop: {desktop_name}")
            typer.echo(f"[verify] result: FAIL ({exc})")
            typer.echo("[verify] cursor not checked")
        raise VerifyFailed(detail) from exc

    profile.last_verified = _last_verified(
        profile,
        app_version=version,
        result="pass" if result.passed else "fail",
        first_paint_s=result.first_paint_s,
    )
    profile.dump(_profile_path(paths, app_id))

    evidence = result.evidence_dir or (
        result.frames[0].parent if result.frames else None
    )
    outcome = "pass" if result.passed else "fail"
    if json_output:
        payload = _verify_payload(
            profile,
            version=version,
            desktop_name=desktop_name,
            passed=result.passed,
            result=outcome,
            reason=result.reason,
            first_paint_s=result.first_paint_s,
            window_class=result.window_class,
            frames=result.frames,
            evidence=evidence,
            transparent_frames=result.transparent_frames,
        )
        # Machine output stays pure JSON; ``cursor_checked`` carries the
        # limitation instead of a human sentence that would break json.loads.
        typer.echo(json.dumps(payload, indent=2))
    else:
        paint = (
            "not observed"
            if result.first_paint_s is None
            else f"{result.first_paint_s:.2f}s"
        )
        typer.echo(f"[verify] app: {profile.id}")
        typer.echo(f"[verify] app version: {version}")
        typer.echo(f"[verify] runner: {profile.runner}")
        typer.echo(f"[verify] desktop: {desktop_name}")
        typer.echo(f"[verify] window class: {result.window_class or 'not found'}")
        typer.echo(f"[verify] first paint: {paint}")
        typer.echo(f"[verify] evidence: {evidence or 'none (no frame captured)'}")
        typer.echo(f"[verify] frames: {len(result.frames)}")
        for frame in result.frames:
            typer.echo(f"[verify] frame: {frame}")
        typer.echo(f"[verify] result: {'PASS' if result.passed else 'FAIL'} ({result.reason})")
        typer.echo("[verify] cursor not checked")

    if not result.passed:
        detail = f"verify failed: {result.reason}"
        if evidence is not None:
            detail = f"{detail}; evidence: {evidence}"
        raise VerifyFailed(detail)


@app.command("open-url")
def open_url_command(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Argument(help="App id from 'winbridge list'.")],
    url: Annotated[
        str,
        typer.Argument(help="Callback URL handed over by the desktop handler."),
    ],
) -> None:
    """Forward a callback URL to an app without stopping a running instance."""
    paths = _paths(ctx)
    profile = _load_profile(paths, app_id)
    _bind_app_logging(ctx, paths, profile.id, _profile_log_version(profile))
    context = _profile_context(ctx, profile)
    open_url(profile, context.backend, url, paths=paths)
    typer.echo(f"forwarded {redact_url(url)} to {app_id}")


login_bridge_app = typer.Typer(
    no_args_is_help=True,
    help="Register and inspect URL scheme handlers.",
)
app.add_typer(login_bridge_app, name="login-bridge")


@login_bridge_app.command("install")
def login_bridge_install(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Argument(help="App id from 'winbridge list'.")],
) -> None:
    """Write the NoDisplay handler and claim every recorded scheme."""
    paths = _paths(ctx)
    profile = _load_profile(paths, app_id)
    _bind_app_logging(ctx, paths, profile.id, _profile_log_version(profile))
    schemes = register_schemes(profile, paths)
    handler = f"{desktop_id(profile.id)}-handler.desktop"
    for scheme in schemes:
        typer.echo(f"x-scheme-handler/{scheme} -> {handler}")
    typer.echo("experimental: forwarding under Wine is unproven")


@login_bridge_app.command("status")
def login_bridge_status_command(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Argument(help="App id from 'winbridge list'.")],
) -> None:
    """Show the default handler xdg-mime reports for each recorded scheme."""
    paths = _paths(ctx)
    profile = _load_profile(paths, app_id)
    _bind_app_logging(ctx, paths, profile.id, _profile_log_version(profile))
    status = login_bridge_status(profile, paths)
    if not status:
        typer.echo(f"{profile.id}: no schemes recorded")
        return
    handler = f"{desktop_id(profile.id)}-handler.desktop"
    typer.echo("SCHEME\tDEFAULT HANDLER\tSTATUS")
    for scheme, current in status.items():
        if not current:
            state = "not registered"
        elif current == handler:
            state = "ok"
        else:
            state = "other app"
        typer.echo(f"{scheme}\t{current or '-'}\t{state}")


@app.command("doctor")
def doctor_command(
    ctx: typer.Context,
    app_id: Annotated[
        str | None,
        typer.Argument(help="App id from 'winbridge list'; omit for environment checks."),
    ] = None,
    bundle: Annotated[
        bool,
        typer.Option(
            "--bundle",
            help="Write a redacted diagnostic archive under the WinBridge state directory.",
        ),
    ] = False,
) -> None:
    """Check the environment, the runner store, and one app's prefix."""
    paths = _paths(ctx)
    catalog = _catalog(ctx)

    config_error: str | None = None
    try:
        config = Config.load(paths.config)
    except (OSError, ConfigError) as exc:
        config = Config()
        config_error = str(exc)

    profile: Profile | None = None
    profile_error: str | None = None
    if app_id is not None:
        try:
            profile = _load_profile(paths, app_id)
        except UsageError:
            raise
        except (OSError, WinBridgeError) as exc:
            profile_error = str(exc)

    checks = run_checks(paths, config, catalog, profile)
    if config_error is not None:
        checks = [
            CheckResult("config", "fail", config_error) if check.name == "config" else check
            for check in checks
        ]
    if profile_error is not None:
        checks.insert(0, CheckResult("profile", "fail", profile_error))
    if app_id is not None:
        _bind_app_logging(
            ctx,
            paths,
            app_id,
            _profile_log_version(profile) if profile is not None else "unknown",
        )

    for check in checks:
        typer.echo(f"[doctor] {check.status:4} {check.name}: {check.detail}")
    counts = Counter(check.status for check in checks)
    typer.echo(
        f"[doctor] summary: {counts['ok']} ok, {counts['warn']} warn, "
        f"{counts['fail']} fail"
    )

    if bundle:
        identity = None
        if app_id is not None:
            identity = BundleIdentity(
                app_id=app_id,
                app_name=profile.name if profile is not None else "",
                app_version=(
                    _profile_log_version(profile) if profile is not None else ""
                ),
            )
            if identity.app_version == "unknown":
                identity = BundleIdentity(
                    app_id=app_id, app_name=identity.app_name, app_version=""
                )
        target = write_bundle(paths, checks, identity=identity)
        typer.echo(f"[doctor] bundle: {target}")

    if counts["fail"]:
        raise WinBridgeError(f"doctor found {counts['fail']} failed check(s)")


def main() -> None:
    """Run the CLI, mapping expected failures to documented exit codes."""
    try:
        try:
            app()
        except WinBridgeError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=exc.exit_code) from exc
    except typer.Exit as exit_exc:
        # Typer only turns Exit into a process status inside a Click command.
        # At the console-script boundary it must become SystemExit, otherwise
        # the interpreter reports a traceback and exits 1.
        raise SystemExit(exit_exc.exit_code) from exit_exc
