from __future__ import annotations

import pytest

from winbridge.util.slug import slugify


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Example App 2!", "example-app-2"),
        ("Example App", "example-app"),
        ("example_app", "example-app"),
        ("  Example   App  ", "example-app"),
        ("--Example--App--", "example-app"),
        ("Example...App", "example-app"),
        ("Example App 2", "example-app-2"),
        ("Café App", "caf-app"),
        ("A1", "a1"),
    ],
)
def test_slugify_normalizes_names(name: str, expected: str) -> None:
    assert slugify(name) == expected


@pytest.mark.parametrize("name", ["", "   ", "!!!", "---", "._."])
def test_slugify_refuses_names_without_alphanumerics(name: str) -> None:
    with pytest.raises(ValueError, match="app id"):
        slugify(name)


def test_slugify_is_idempotent() -> None:
    once = slugify("Example App 2!")
    assert slugify(once) == once
