# AUR package

`winbridge` builds from the sdist that the release workflow publishes to PyPI.

## Files

- `PKGBUILD` — package recipe. `sha256sums` is a **placeholder** until the
  tagged sdist exists; `makepkg` fails on it by design.
- `.SRCINFO` — generated metadata, regenerated for every submission. Never edit
  it by hand.

## Release-time flow

The checksum cannot be committed before the tag, because the sdist is produced
by the release workflow after the tag is pushed.

1. Tag `v0.1.0` and let `.github/workflows/release.yml` publish the sdist to
   PyPI. Confirm the final response (after redirects) is 200 and carries a
   content length:

   ```bash
   url=https://files.pythonhosted.org/packages/source/w/winbridge/winbridge-0.1.0.tar.gz
   curl -fsSLI "$url"
   test "$(curl -fsSL -o /dev/null -w '%{http_code}' "$url")" = 200
   ```
2. Update `pkgver` and reset `pkgrel=1` in `PKGBUILD` if the version changed.
3. Replace the placeholder checksum with the published digest:

   ```bash
   cd packaging/aur
   updpkgsums          # part of pacman-contrib
   ```

   `updpkgsums` downloads the tagged sdist and writes `sha256sums` in place.
4. Regenerate the AUR metadata and do a clean build:

   ```bash
   makepkg --printsrcinfo > .SRCINFO
   makepkg --cleanbuild --syncdeps --rmdeps   # then install and run `winbridge --version`
   ```

   A clean chroot is a separate step; `makepkg` only rebuilds in place. With
   `devtools` installed, verify the recipe in a chroot:

   ```bash
   extra-x86_64-build        # wraps makechrootpkg for the x86_64 chroot
   ```
5. Commit the new `pkgver`, `sha256sums`, and `.SRCINFO` to this repository.
6. Submit to the AUR:

   ```bash
   git clone ssh://aur@aur.archlinux.org/winbridge.git
   cp PKGBUILD .SRCINFO winbridge/
   cd winbridge
   git add PKGBUILD .SRCINFO
   git commit -m "winbridge 0.1.0-1"
   git push
   ```

## Rules

- Build only from a tagged PyPI sdist; never from a git checkout or a local
  wheel.
- No vendored dependencies: the package installs only WinBridge's own code
  (modules, console script, pinned catalog) and depends on Arch's `python-*`
  packages.
- `depends` mirrors `pyproject.toml`; update both together.
- A checksum placeholder must never be pushed to the AUR.
