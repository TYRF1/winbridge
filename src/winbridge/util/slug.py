"""App-id slugs."""

from __future__ import annotations

import re

__all__ = ["slugify"]

_SEPARATORS = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Lowercase ``name`` into an app id, or raise if nothing remains."""
    slug = _SEPARATORS.sub("-", name.lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot derive an app id from {name!r}")
    return slug
