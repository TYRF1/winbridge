# Publishing

The repository is designed to be public. The published history is a single
sanitized initial commit; the old development history is preserved only in a
private archive.

## Before the first public push

- [ ] Scan the entire tree for vendor names, app versions, screenshots,
      installers, tokens, and hardware IDs.
- [ ] Keep the public history to the single sanitized initial commit. Preserve
      any older history only in a private archive.
- [ ] Run a secret scan over the working tree and history.
- [ ] Confirm no `.exe`, `.msi`, `.deb`, screenshot, or app profile is tracked.
- [ ] Review every issue/PR in the private tracker before migrating it.
- [ ] Keep NDA material in the private archive, never in public issues or CI.

If the history is rewritten, update commit links in docs and the changelog.

## Repository settings

- [ ] Description, topics, and README match the support matrix.
- [ ] Private vulnerability reporting enabled (SECURITY.md depends on it).
- [ ] Branch protection/ruleset on `main`:
      - Require a pull request before merging; friends/fork PRs are allowed.
      - Require the `lint, types, unit tests` status check.
      - Block force pushes and branch deletion.
      - Restrict push/merge to `TYRF1`; do not grant public contributors write
        access.
      - Enforce the rule for admins too, so `main` is never pushed directly.
- [ ] Issues enabled; blank issues may be disabled once templates cover the
      common cases.
- [ ] Discussions optional; keep support in issues until volume justifies it.

## Package and name checks

- [ ] PyPI `winbridge`: free at time of writing (404).
- [ ] AUR `winbridge`: free at time of writing (0 results).
- [ ] GitHub org/user and repository name secured.
- [ ] PyPI trusted publisher configured for the release workflow.
- [ ] AUR `sha256sums` generated from the tagged sdist and verified in a clean
      chroot before submission.

## First release

- [ ] `docs/RELEASE-CHECKLIST.md` executed end to end.
- [ ] `uv build`, `twine check`, and a clean `uv tool install` pass.
- [ ] Tag `v0.1.0`; publish to PyPI; submit the AUR package.
- [ ] Announce only after the support matrix and known limitations are accurate.

## Release workflow gate

`.github/workflows/release.yml` runs the integration workflow as a reusable
job (`integration.yml`, which declares `workflow_call`) before the unit suite,
lint, and type checks. The publish job needs the build job, and the build job
needs that integration result, so a tag cannot reach PyPI unless the Windows
fixture build and the Arch/Wine pipeline for the same commit succeeded. Ruff and
mypy run in the release verify job; a lint or type regression blocks publishing
the same way a failing test does.
