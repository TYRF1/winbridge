"""Composition root: resolve the runner, detect the desktop, build the context.

Desktop detection is session-based and side-effect free; callers that already
have an adapter (tests, embedders) pass it through and skip detection.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from winbridge.backends.base import Backend
from winbridge.backends.wine import WineBackend
from winbridge.core.config import Config
from winbridge.core.errors import UnsupportedEnvironment, UsageError
from winbridge.core.runner import (
    Catalog,
    RunnerNotInstalled,
    install_runner,
    resolve_runner,
)
from winbridge.core.state import Paths
from winbridge.desktops import Desktop, detect_desktop

__all__ = ["Context", "build_context"]


@dataclass(frozen=True, slots=True)
class Context:
    """Everything a command needs, assembled once at the CLI boundary."""

    paths: Paths
    config: Config
    catalog: Catalog
    backend: Backend
    desktop: Desktop | None = None


def build_context(
    paths: Paths,
    config: Config,
    catalog: Catalog,
    backend_name: str = "wine",
    desktop: Desktop | None = None,
    *,
    install_missing_runner: bool = False,
    report: Callable[[str], object] | None = None,
) -> Context:
    """Resolve the configured runner, construct its backend, detect the desktop.

    The runner reference comes from the global config when set, otherwise from
    the catalog's marked default. By default a missing runner is an error rather
    than a silent download: commands that are not ``setup`` tell the user which
    install command to run. ``setup`` opts into
    ``install_missing_runner=True`` so a clean machine can install the catalog
    default without a separate step; ``report`` receives the download/install
    lines to print. An injected desktop wins over detection.
    """
    if backend_name == "wine":
        try:
            runner_root = resolve_runner(config.default_runner or "", paths, catalog)
        except RunnerNotInstalled:
            if not install_missing_runner:
                raise
            reference = config.default_runner or catalog.default_runner().ref
            if reference.startswith("path:"):
                raise
            runner = catalog.get(reference)
            if report is not None:
                report(
                    f"[setup] runner: {reference} is not installed; downloading "
                    "and verifying the catalog payload"
                )
            runner_root = install_runner(runner, paths)
            if report is not None:
                report(f"[setup] runner: installed at {runner_root}")
        backend: Backend = WineBackend(runner_root, paths)
    elif backend_name == "bottles":
        raise UnsupportedEnvironment(
            "the bottles backend is not available in v0.1; it arrives in v0.2"
        )
    else:
        raise UsageError(f"unknown backend: {backend_name!r}")

    return Context(
        paths=paths,
        config=config,
        catalog=catalog,
        backend=backend,
        desktop=desktop if desktop is not None else detect_desktop(),
    )
