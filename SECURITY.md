# Security

WinBridge runs untrusted Windows installers under Wine and downloads Wine
runners. Treat both as hostile input.

## Reporting a vulnerability

Do **not** open a public issue for a vulnerability. Use GitHub's private
vulnerability reporting on this repository. If that is unavailable, open a
minimal public issue asking for a private channel; do not include details.

Include: affected version, distribution, desktop, runner, the command, and a
minimal reproduction. Redact tokens, URLs, and vendor material.

## Scope and threat model

- **Installers** are untrusted executables. WinBridge never runs them as root
  and never passes their output through a shell. Wine's `Z:` host-root link is
  removed at prefix creation, so an installer cannot read the whole host by
  default; `--keep-z-drive` re-opens that boundary explicitly.
- **Runners** are downloaded from upstream release URLs and verified against a
  SHA-256 pinned in the packaged catalog. WinBridge does not mirror binaries.
- **WebView2** defaults to the checksum-pinned catalog payload for
  reproducible/offline runs. `--webview2=latest` opts into Microsoft's official
  Evergreen bootstrapper over HTTPS and is experimental; the installed version
  is recorded. Doctor warns when a pinned payload is stale.
- **Scheme handlers** accept URLs only for schemes recorded in the app's
  profile; logs redact query strings and tokens.
- **State** is written under XDG paths with `0700` evidence/log directories.
  `doctor --bundle` redacts URLs before writing an archive.
- **No elevation.** `sudo` is never used. UAC behavior is Wine's, not ours.

Out of scope: vulnerabilities in Wine, DXVK, WebView2, the tested app, or the
upstream runner builds. Report those upstream.

## Supported versions

Security fixes target the latest v0.1.x release. Pre-release tags and `main` are
supported on a best-effort basis.

## Disclosure

We aim to acknowledge a report within 7 days and to ship a fix or mitigation as
soon as practical. Credit is given unless you ask otherwise.
