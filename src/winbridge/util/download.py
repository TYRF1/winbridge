"""Checksum-verified streaming downloads.

The catalog links upstream artifacts; this module is the only place that
fetches them. Every runner download must carry a SHA-256. A ``None`` digest is
accepted only for Microsoft's WebView2 Evergreen bootstrapper, which is
explicitly "latest" and therefore cannot be pinned.

Redirects are constrained twice: the default opener refuses every redirect
whose target is not HTTPS, and the final response URL is re-validated for the
scheme (and, for the unpinned bootstrapper, the host) before any body bytes are
read or written.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import inspect
import os
import re
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any
from urllib.parse import SplitResult, urlsplit

from winbridge.core.errors import WinBridgeError
from winbridge.util.log import redact_url

__all__ = ["DownloadError", "HashMismatch", "download_verified"]

_CHUNK_SIZE = 1024 * 1024
_DEFAULT_TIMEOUT_S = 60.0
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}\Z")
# The Evergreen bootstrapper URL is a go.microsoft.com fwlink. Anything else
# without a digest is refused: the catalog is the only trust channel.
_UNPINNED_INITIAL_HOSTS = frozenset({"go.microsoft.com"})
# Verified 2026-10-27 with `curl -sSIL` against LinkId=2124703: the fwlink
# 301s to this exact Microsoft delivery host and then returns 200. No other
# host appears in the chain; wildcards are deliberately not allowed.
_UNPINNED_FINAL_HOSTS = frozenset(
    {
        "go.microsoft.com",
        "msedge.sf.dl.delivery.mp.microsoft.com",
    }
)


class DownloadError(WinBridgeError):
    """The artifact could not be fetched or the request was refused."""


class HashMismatch(WinBridgeError):
    """The downloaded bytes do not match the pinned SHA-256."""


def _parse_url(url: str, context: str) -> SplitResult:
    """Parse a URL, turning urllib's ``ValueError`` into a clean failure."""
    try:
        return urlsplit(url)
    except ValueError as exc:
        raise DownloadError(f"invalid {context}: {exc}") from exc


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect before urllib follows it to a non-HTTPS target."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        scheme = _parse_url(newurl, "redirect URL").scheme
        if scheme != "https":
            raise DownloadError(
                f"refusing redirect to a non-HTTPS URL: {scheme or 'no scheme'!r}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_DEFAULT_OPENER = urllib.request.build_opener(_HttpsOnlyRedirectHandler())


def _validate_url(url: str, digest: str | None) -> None:
    parsed = _parse_url(url, "URL")
    if parsed.scheme == "file":
        if digest is None:
            raise DownloadError(
                "refusing a hashless file:// download; only the HTTPS WebView2 "
                "bootstrapper may omit its SHA-256"
            )
        if parsed.netloc not in {"", "localhost"} or not parsed.path:
            raise DownloadError(f"unsupported file URL: {url!r}")
        return
    if parsed.scheme != "https":
        raise DownloadError(
            f"refusing to download over {parsed.scheme or 'no scheme'!r}; HTTPS is required"
        )
    if not parsed.hostname:
        raise DownloadError(f"invalid HTTPS URL: {url!r}")
    if digest is None and parsed.hostname not in _UNPINNED_INITIAL_HOSTS:
        raise DownloadError(
            "a SHA-256 is required; only the HTTPS WebView2 bootstrapper "
            f"(hosts: {', '.join(sorted(_UNPINNED_INITIAL_HOSTS))}) may omit it"
        )


def _check_final_url(response: Any, requested: str, digest: str | None) -> None:
    """Validate the URL the opener actually landed on, before reading bytes."""
    geturl = getattr(response, "geturl", None)
    final = geturl() if callable(geturl) else requested
    if not isinstance(final, str) or not final:
        raise DownloadError("download response did not expose a final URL")

    final_parts = _parse_url(final, "final URL after redirect")
    if _parse_url(requested, "URL").scheme == "file":
        if final_parts.scheme != "file":
            raise DownloadError(
                "refusing redirect from a file URL to "
                f"{final_parts.scheme or 'no scheme'!r}"
            )
        return

    if final_parts.scheme != "https":
        raise DownloadError(
            "refusing redirect to a non-HTTPS URL: "
            f"{final_parts.scheme or 'no scheme'!r} ({redact_url(final)})"
        )
    host = final_parts.hostname
    if not host:
        raise DownloadError(f"invalid final URL after redirect: {redact_url(final)}")
    if digest is None and host not in _UNPINNED_FINAL_HOSTS:
        raise DownloadError(
            "refusing redirect away from the Microsoft WebView2 bootstrapper hosts; "
            f"got {host!r}"
        )


def _validate_digest(digest: str) -> str:
    value = digest.strip().lower()
    if not _SHA256_HEX.fullmatch(value):
        raise DownloadError(f"SHA-256 must be 64 hexadecimal characters; got {digest!r}")
    return value


def _private_dir(directory: Path) -> None:
    existed = directory.exists()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not existed:
        directory.chmod(0o700)


def _is_default_opener(opener: Callable[..., Any]) -> bool:
    return getattr(opener, "__self__", None) is _DEFAULT_OPENER


def _accepts_timeout(opener: Callable[..., Any]) -> bool:
    """Return whether ``opener`` accepts a ``timeout`` keyword."""
    try:
        parameters = inspect.signature(opener).parameters
    except (TypeError, ValueError):
        return False
    return "timeout" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def download_verified(
    url: str,
    sha256: str | None,
    dest: Path | str,
    opener: Callable[..., Any] = _DEFAULT_OPENER.open,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> Path:
    """Download ``url`` to ``dest`` after verifying its SHA-256.

    The payload streams into ``<dest>.part`` while hashing, is flushed and
    ``fsync``-ed, then renamed with :func:`os.replace` so a reader never sees a
    partial file. ``sha256=None`` is accepted only for the HTTPS WebView2
    bootstrapper. ``file://`` inputs are accepted for tests and local mirrors;
    the shipped catalog contains HTTPS URLs only. Redirects must stay on HTTPS,
    and an unpinned bootstrapper must stay on its allowed host.

    ``timeout_s`` bounds each network operation. The default opener receives it
    as urllib's socket timeout; an injected opener that accepts ``timeout``
    receives it too, while a simple test opener keeps working unchanged.
    """
    _validate_url(url, sha256)
    expected = None if sha256 is None else _validate_digest(sha256)

    target = Path(dest)
    _private_dir(target.parent)
    part = target.with_name(f"{target.name}.part")
    timeout = max(float(timeout_s), 0.0)

    digest = hashlib.sha256()
    try:
        if _is_default_opener(opener) or _accepts_timeout(opener):
            response = opener(url, timeout=timeout)
        else:
            response = opener(url)
        with response as opened:
            _check_final_url(opened, url, sha256)
            with part.open("wb") as handle:
                while chunk := opened.read(_CHUNK_SIZE):
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
    except (OSError, http.client.HTTPException) as exc:
        part.unlink(missing_ok=True)
        raise DownloadError(f"download failed for {redact_url(url)}: {exc}") from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise

    actual = digest.hexdigest()
    if expected is not None and not hmac.compare_digest(actual, expected):
        part.unlink(missing_ok=True)
        raise HashMismatch(
            f"checksum mismatch for {redact_url(url)}: expected {expected}, got {actual}"
        )

    try:
        os.replace(part, target)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise DownloadError(f"cannot store download at {target}: {exc}") from exc
    return target
