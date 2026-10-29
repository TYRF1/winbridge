## Summary

<!-- What changed and why. Link the spec section or issue. -->

## Tests

- [ ] New/updated failing test added before the fix
- [ ] `scripts/sandbox uv run pytest -q`
- [ ] `scripts/sandbox uv run ruff check .`
- [ ] `scripts/sandbox uv run mypy src`
- [ ] Integration checked (if it touches Wine/desktop behavior)

## Checklist

- [ ] No vendor names, binaries, installers, screenshots, or app-specific hacks
- [ ] No `shell=True`; no root; no mutation of shared Bottles/Steam runners
- [ ] Profile changes are additive; unknown keys still survive
- [ ] Docs updated (README/support matrix/troubleshooting as applicable)
- [ ] Commit message follows `feat:`, `fix:`, `docs:`, `test:`, `build:`
