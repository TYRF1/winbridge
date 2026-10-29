# Sandbox

Development runs inside a rootless Podman container, so an agent never touches
the host desktop, the host home directory, or the main checkout. Windows-app
behavior is still verified on a real host session (see
[Host-side exceptions](#host-side-exceptions)).

## Commands

```bash
bash scripts/sandbox-build       # once, and after Containerfile changes
scripts/sandbox uv sync --dev
scripts/sandbox uv run pytest -q
scripts/sandbox uv run ruff check .
scripts/sandbox uv run mypy src
scripts/sandbox bash             # interactive shell, worktree at /work
bash scripts/sandbox-selftest    # proves the guarantees below
```

`scripts/sandbox <command> [args...]` runs exactly one command. It refuses to run
outside a git worktree and refuses the main checkout, so the only writable source
tree is the current worktree.

| Variable | Default | Purpose |
|---|---|---|
| `WINBRIDGE_SANDBOX_IMAGE` | `winbridge-sandbox:local` | Image to run/build |
| `WINBRIDGE_SANDBOX_CPUS` | `2` | CPU limit |
| `WINBRIDGE_SANDBOX_MEMORY` | `4g` | Memory limit |

## Guarantees

The wrapper (`scripts/sandbox`) runs:

```
podman run --rm --userns=keep-id --read-only
  --tmpfs /tmp --volume <worktree>:/work --volume winbridge-sandbox-cache:/cache
  --cap-drop=all --security-opt=no-new-privileges --pids-limit=512
  --cpus 2 --memory 4g
  --env HOME=/tmp/home --env UV_CACHE_DIR=/cache/uv
  --env DISPLAY= --env WAYLAND_DISPLAY= --env WAYLAND_SOCKET=
```

What that means in practice:

- **No host home.** Only the worktree is bind-mounted, at `/work`. `/cache` is a
  named Podman volume, not a host path. `HOME` is `/tmp/home`, created per run
  and discarded with the container.
- **No GUI.** `DISPLAY`, `WAYLAND_DISPLAY`, and `WAYLAND_SOCKET` are empty. No
  X11 or Wayland socket is mounted, so nothing can open a window on your desktop.
- **No GPU or audio.** `/dev/dri` and `/dev/snd` are not passed through.
- **No privileges.** Root filesystem is read-only; only the tmpfs mounts
  (`/tmp`, `/var/tmp`, `/run`), `/work`, and `/cache` are writable. All
  capabilities dropped, `no-new-privileges` set, 512-pid limit, 2 CPUs, 4 GiB
  RAM.
- **Own network namespace.** The container keeps its own netns (not
  `--network=host`); outbound network is enabled because `uv` and package
  installs need it. The default rootless backend (pasta) mirrors host addresses
  into that namespace, so the LAN is reachable — the sandbox is an isolation
  boundary for the filesystem and desktop, not a network firewall.
- **Own PID/user namespace.** `--userns=keep-id` maps the caller's uid/gid into
  the container, so files created in `/work` stay owned by you. The container
  user is `dev`; the runtime uid is always the caller's.
- **Main checkout refused.** `git rev-parse --absolute-git-dir` and
  `--path-format=absolute --git-common-dir` are compared. A worktree has a
  separate admin dir; the main checkout has one, so it exits `2` with a clear
  message.
- **Git identity is forwarded.** `user.name` and `user.email` from the worktree
  config become `GIT_AUTHOR_*`/`GIT_COMMITTER_*`; no host `.gitconfig` is
  mounted.
- **Exit codes pass through.** `scripts/sandbox sh -c 'exit 7'` exits `7`.
- **No root.** Both scripts refuse to run as uid 0.

`bash scripts/sandbox-selftest` runs every check above through
`scripts/sandbox` itself — cwd, `HOME`, empty display variables, absent host
home, absent `/dev/dri` and `/dev/snd`, read-only root, Python >= 3.11, `uv`,
required QA tools, worktree write, and the main-checkout refusal.

## Image contents

`sandbox/Containerfile` is Arch-based and deliberately small:

- Python 3.11+ (Arch currently ships 3.14), `uv`, `git`, `curl`, `tar`, `xz`,
  `unzip`, `less`
- Rust and Cargo for generating the synthetic Tauri fixture's lockfile (the
  fixture itself is built on Windows CI, not here)
- ImageMagick (`convert`), Xvfb, `xdotool`, `wmctrl`, `desktop-file-utils`
  (`desktop-file-validate`, `update-desktop-database`) for the X11 adapter and
  launcher tests
- `shellcheck` and `procps-ng` for local checks

There is **no Wine, no browser, no desktop environment, and no Podman** in the
image. The sandbox is for unit tests, lint, types, packaging, and headless X11
adapter tests; Wine integration stays a host concern (`WINBRIDGE_INTEGRATION=1`).

## Host-side exceptions

Four things run on the host, never in the sandbox:

- **Image build.** `bash scripts/sandbox-build` — Podman cannot build the image
  it is about to run.
- **Git.** `git status`, `git diff`, and `git commit` run in the worktree on the
  host; the linked git dir is outside the mount (see [Limits](#limits)).
- **Wine/desktop integration tests.** `WINBRIDGE_INTEGRATION=1` against a real
  Wine prefix and a supported desktop.
- **Manual GUI QA.** Screenshots, cursor visibility, first paint, pin clicks, and
  login callbacks are judged by a human on a real host session, against a real
  Windows installer — the release-checklist path.

Unit tests, lint, types, packaging, and shell lint still go through
`scripts/sandbox` so the result is reproducible and the desktop is not disturbed.

## Limits

- Arch-only image; the pinned `archlinux:latest` base is re-resolved on rebuild,
  so `pacman -Syu` at build time decides exact versions. Rebuild deliberately.
- One command per invocation. State outside `/work` and `/cache` does not exist.
- `/tmp` is a 1 GiB tmpfs that dies with the container; large temporary test
  data belongs under the worktree.
- The named volume `winbridge-sandbox-cache` stores `uv` downloads and caches.
  Remove it with `podman volume rm winbridge-sandbox-cache` when it bloats.
- No Wine, no `flatpak`, no Hyprland socket, no D-Bus session, no notification
  bus. Anything that needs them is host manual QA.
- `--userns=keep-id` requires rootless Podman with subuid/subgid configured.
  Rootful Podman is not supported by design.
- **Git runs on the host.** A linked worktree keeps its real git dir at
  `<main-checkout>/.git/worktrees/<name>`, outside the mount. `git status`,
  `git diff`, and commits therefore run on the host in the worktree; inside the
  sandbox they fail with `gitfile does not point to a valid repository`. Mounting
  that directory would expose the main repo's refs and history, which is exactly
  what the worktree-only rule prevents. The wrapper still forwards the host git
  identity for tooling that runs git against repositories created inside `/work`
  (test fixtures).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `image ... is missing` | `bash scripts/sandbox-build` |
| `refusing to run in the main checkout` | `git worktree add ../winbridge-task -b feat/task`, then run from there |
| `refusing to run as root` | Run as your normal user; rootless Podman only |
| Build fails in `pacman` | Network/proxy issue, or stale mirror; retry `bash scripts/sandbox-build` |
| Permission errors writing `/work` | Check `podman info`, subuid/subgid, and that the worktree is owned by you |
| Disk pressure from builds | `podman system prune`, `podman volume rm winbridge-sandbox-cache` |

Do not add host mounts to work around a failure. If a test genuinely needs the
host, it is either an integration test (documented in `AGENTS.md`) or manual QA.

## Files

- `sandbox/Containerfile` — image definition
- `scripts/sandbox-build` — build the image (host-side exception)
- `scripts/sandbox` — run one command with the guarantees above
- `scripts/sandbox-selftest` — executable proof of those guarantees
