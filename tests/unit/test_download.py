from __future__ import annotations

import hashlib
import http.client
import io
import urllib.request
from pathlib import Path

import pytest

from winbridge.util.download import DownloadError, HashMismatch, download_verified

_BOOTSTRAPPER = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
# Verified 2026-10-27 with `curl -sSIL`: LinkId=2124703 301s here, then 200.
_CDN_FINAL = (
    "https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/"
    "4a41d80d-4493-4463-a466-d0988b97f1c4/MicrosoftEdgeWebview2Setup.exe"
)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RedirectingResponse(io.BytesIO):
    """Fake opener response whose final URL differs from the requested one."""

    def __init__(self, payload: bytes, final_url: str) -> None:
        super().__init__(payload)
        self._final_url = final_url

    def geturl(self) -> str:
        return self._final_url


def assert_no_output(dest: Path) -> None:
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_download_writes_destination_and_returns_it(tmp_path: Path) -> None:
    payload = b"runner archive bytes"
    src = tmp_path / "payload"
    src.write_bytes(payload)
    dest = tmp_path / "nested" / "runner.tar.xz"

    result = download_verified(src.as_uri(), sha256_of(payload), dest)

    assert result == dest
    assert dest.read_bytes() == payload
    assert not dest.with_name(dest.name + ".part").exists()


def test_download_rejects_hash_mismatch(tmp_path: Path) -> None:
    src = tmp_path / "payload"
    src.write_bytes(b"nope")
    dest = tmp_path / "out"

    with pytest.raises(HashMismatch, match="checksum mismatch"):
        download_verified(src.as_uri(), "0" * 64, dest)

    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_download_leaves_no_partial_file_on_stream_error(tmp_path: Path) -> None:
    class BrokenResponse:
        def __enter__(self) -> BrokenResponse:
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

        def read(self, size: int) -> bytes:
            raise OSError("connection reset")

    def opener(url: str) -> BrokenResponse:
        return BrokenResponse()

    dest = tmp_path / "out"

    with pytest.raises(DownloadError, match="connection reset"):
        download_verified("https://example.invalid/x", "0" * 64, dest, opener=opener)

    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid/runner.tar.xz",
        "ftp://example.invalid/runner.tar.xz",
        "runner.tar.xz",
    ],
)
def test_download_rejects_non_https_schemes(tmp_path: Path, url: str) -> None:
    with pytest.raises(DownloadError, match="HTTPS"):
        download_verified(url, "0" * 64, tmp_path / "out")


def test_download_requires_a_hash_for_file_urls(tmp_path: Path) -> None:
    src = tmp_path / "payload"
    src.write_bytes(b"bootstrapper")

    with pytest.raises(DownloadError, match="bootstrapper"):
        download_verified(src.as_uri(), None, tmp_path / "out")


def test_download_requires_bootstrapper_host_without_a_hash(tmp_path: Path) -> None:
    with pytest.raises(DownloadError, match="bootstrapper"):
        download_verified("https://example.invalid/setup.exe", None, tmp_path / "out")


def test_download_allows_hashless_https_bootstrapper(tmp_path: Path) -> None:
    payload = b"tiny bootstrapper"

    def opener(url: str) -> io.BytesIO:
        assert url == _BOOTSTRAPPER
        return io.BytesIO(payload)

    dest = tmp_path / "bootstrapper.exe"

    result = download_verified(_BOOTSTRAPPER, None, dest, opener=opener)

    assert result == dest
    assert dest.read_bytes() == payload


@pytest.mark.parametrize("digest", ["xyz", "0" * 63, "g" * 64, "0" * 65])
def test_download_rejects_malformed_hash(tmp_path: Path, digest: str) -> None:
    with pytest.raises(DownloadError, match="SHA-256"):
        download_verified("https://example.invalid/x", digest, tmp_path / "out")


def test_download_accepts_uppercase_hash(tmp_path: Path) -> None:
    payload = b"mixed case"
    src = tmp_path / "payload"
    src.write_bytes(payload)

    dest = download_verified(src.as_uri(), sha256_of(payload).upper(), tmp_path / "out")

    assert dest.read_bytes() == payload


def test_download_passes_a_timeout_to_a_cooperative_opener(tmp_path: Path) -> None:
    seen: list[float] = []
    payload = b"payload"

    def opener(url: str, *, timeout: float) -> io.BytesIO:
        seen.append(timeout)
        return io.BytesIO(payload)

    dest = tmp_path / "out"
    download_verified(
        "https://example.invalid/x",
        sha256_of(payload),
        dest,
        opener=opener,
        timeout_s=7.5,
    )

    assert seen == [7.5]
    assert dest.read_bytes() == payload


def test_download_passes_the_requested_url_to_the_opener(tmp_path: Path) -> None:
    """The helper does not resolve redirects itself; it delegates to the opener."""
    seen: list[str] = []

    def opener(url: str) -> io.BytesIO:
        seen.append(url)
        return io.BytesIO(b"payload")

    dest = tmp_path / "out"
    download_verified(
        "https://example.invalid/redirect", sha256_of(b"payload"), dest, opener=opener
    )

    assert seen == ["https://example.invalid/redirect"]
    assert dest.read_bytes() == b"payload"


def test_download_rejects_bootstrapper_redirect_to_http(tmp_path: Path) -> None:
    final = "http://downloads.example.invalid/MicrosoftEdgeWebview2Setup.exe"

    def opener(url: str) -> RedirectingResponse:
        return RedirectingResponse(b"payload", final)

    dest = tmp_path / "bootstrapper.exe"

    with pytest.raises(DownloadError, match="non-HTTPS"):
        download_verified(_BOOTSTRAPPER, None, dest, opener=opener)

    assert_no_output(dest)


def test_download_rejects_hashed_redirect_to_http(tmp_path: Path) -> None:
    payload = b"runner archive"
    final = "http://downloads.example.invalid/runner.tar.xz"

    def opener(url: str) -> RedirectingResponse:
        return RedirectingResponse(payload, final)

    dest = tmp_path / "runner.tar.xz"

    with pytest.raises(DownloadError, match="non-HTTPS"):
        download_verified(
            "https://example.invalid/runner.tar.xz", sha256_of(payload), dest, opener=opener
        )

    assert_no_output(dest)


def test_download_rejects_redirect_to_file(tmp_path: Path) -> None:
    local = tmp_path / "local-payload"
    local.write_bytes(b"payload")

    def opener(url: str) -> RedirectingResponse:
        return RedirectingResponse(b"payload", local.as_uri())

    dest = tmp_path / "out"

    with pytest.raises(DownloadError, match="non-HTTPS"):
        download_verified(
            "https://example.invalid/runner.tar.xz", sha256_of(b"payload"), dest, opener=opener
        )

    assert_no_output(dest)


def test_download_rejects_bootstrapper_redirect_to_other_host(tmp_path: Path) -> None:
    final = "https://downloads.example.invalid/MicrosoftEdgeWebview2Setup.exe"

    def opener(url: str) -> RedirectingResponse:
        return RedirectingResponse(b"payload", final)

    dest = tmp_path / "bootstrapper.exe"

    with pytest.raises(DownloadError, match="bootstrapper"):
        download_verified(_BOOTSTRAPPER, None, dest, opener=opener)

    assert_no_output(dest)


def test_download_allows_bootstrapper_redirect_to_microsoft_cdn(tmp_path: Path) -> None:
    payload = b"bootstrapper"

    def opener(url: str) -> RedirectingResponse:
        return RedirectingResponse(payload, _CDN_FINAL)

    dest = tmp_path / "bootstrapper.exe"

    result = download_verified(_BOOTSTRAPPER, None, dest, opener=opener)

    assert result == dest
    assert dest.read_bytes() == payload


def test_download_allows_hashed_redirect_to_another_https_host(tmp_path: Path) -> None:
    payload = b"runner archive"
    final = "https://cdn.example.invalid/runner.tar.xz"

    def opener(url: str) -> RedirectingResponse:
        return RedirectingResponse(payload, final)

    dest = tmp_path / "runner.tar.xz"

    result = download_verified(
        "https://example.invalid/runner.tar.xz", sha256_of(payload), dest, opener=opener
    )

    assert result == dest
    assert dest.read_bytes() == payload


def test_redirect_handler_refuses_non_https_before_following() -> None:
    from winbridge.util.download import _HttpsOnlyRedirectHandler

    handler = _HttpsOnlyRedirectHandler()
    request = urllib.request.Request(_BOOTSTRAPPER)

    with pytest.raises(DownloadError, match="non-HTTPS"):
        handler.redirect_request(
            request,
            io.BytesIO(),
            302,
            "Found",
            http.client.HTTPMessage(),
            "http://example.invalid/setup.exe",
        )


def test_redirect_handler_rejects_malformed_redirect_url() -> None:
    from winbridge.util.download import _HttpsOnlyRedirectHandler

    handler = _HttpsOnlyRedirectHandler()
    request = urllib.request.Request(_BOOTSTRAPPER)

    with pytest.raises(DownloadError, match="redirect URL"):
        handler.redirect_request(
            request,
            io.BytesIO(),
            302,
            "Found",
            http.client.HTTPMessage(),
            "https://[::1",
        )


def test_download_rejects_malformed_final_url(tmp_path: Path) -> None:
    def opener(url: str) -> RedirectingResponse:
        return RedirectingResponse(b"payload", "https://[::1")

    dest = tmp_path / "bootstrapper.exe"

    with pytest.raises(DownloadError, match="final URL"):
        download_verified(_BOOTSTRAPPER, None, dest, opener=opener)

    assert_no_output(dest)


def test_default_opener_installs_https_only_redirect_handler() -> None:
    from winbridge.util.download import _DEFAULT_OPENER

    names = [type(handler).__name__ for handler in _DEFAULT_OPENER.handlers]

    assert "_HttpsOnlyRedirectHandler" in names
    assert "HTTPRedirectHandler" not in names
