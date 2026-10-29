"""Reserved URL schemes that an app may never claim.

A Windows installer can register a protocol as a side effect of bundling an
embedded browser or a shell helper. Recording ``https`` or ``file`` would let a
malicious or misconfigured package capture links and local files from every
other application, so detection, setup validation, registration, and URL
forwarding all refuse the same closed set.
"""

from __future__ import annotations

from winbridge.core.errors import UsageError

__all__ = ["RESERVED_SCHEMES", "is_reserved_scheme", "reject_reserved_scheme"]

#: Browser, mail, operating-system, and file-transfer schemes. A custom
#: application scheme must not shadow any of these.
RESERVED_SCHEMES = frozenset(
    {
        "about",
        "blob",
        "chrome",
        "chrome-extension",
        "data",
        "file",
        "filesystem",
        "ftp",
        "ftps",
        "http",
        "https",
        "javascript",
        "mailto",
        "ms-appinstaller",
        "ms-appx",
        "ms-appx-web",
        "resource",
        "shell",
        "sms",
        "ssh",
        "tel",
        "view-source",
        "ws",
        "wss",
    }
)


def is_reserved_scheme(scheme: str) -> bool:
    """Return whether ``scheme`` is reserved and must not be claimed."""
    return scheme.strip().casefold() in RESERVED_SCHEMES


def reject_reserved_scheme(scheme: str, *, context: str) -> None:
    """Raise :class:`UsageError` when ``scheme`` is reserved."""
    if is_reserved_scheme(scheme):
        raise UsageError(
            f"{context}: URL scheme {scheme!r} is reserved and cannot be claimed "
            "by an app"
        )
