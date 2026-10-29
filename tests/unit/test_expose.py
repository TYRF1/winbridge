"""Host-folder exposure into the prefix's guest user profile.

Every test builds a realistic ``drive_c/users/<host-user>`` tree, so the guest
root selection, the Windows guest path, and the ownership checks are exercised
without Wine. The profile's ``exposed`` table is the only ownership record:
``unexpose`` removes a link only when it still resolves to the recorded host.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from winbridge.cli import app, main
from winbridge.core.errors import UsageError, WinBridgeError
from winbridge.core.expose import expose, guest_user_dir, unexpose
from winbridge.core.profile import ExposedPath, Profile
from winbridge.core.state import Paths

USER = "me"
OTHER_USER = "second"


def guest(user: str, name: str) -> str:
    """Return the Windows path WinBridge records for an exposed folder."""
    return f"C:\\users\\{user}\\{name}"


def make_prefix(
    paths: Paths,
    app_id: str,
    *,
    user: str = USER,
    extra_users: tuple[str, ...] = (),
) -> Path:
    """Create the parts of a Wine prefix ``expose`` depends on."""
    prefix = paths.prefix_for(app_id)
    for account in (user, *extra_users):
        home = prefix / "drive_c" / "users" / account
        (home / "AppData" / "Local").mkdir(parents=True)
        (home / "Desktop").mkdir()
    return prefix


@pytest.fixture
def profile(make_profile: Callable[..., Profile]) -> Profile:
    """A profile with no exposures; the shared fixture starts with one."""
    return make_profile(exposed=[])


@pytest.fixture
def prefix(paths: Paths, profile: Profile) -> Path:
    return make_prefix(paths, profile.id)


@pytest.fixture(autouse=True)
def _fixed_current_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin host-user preference; tests that need a match override this value."""
    monkeypatch.setattr("winbridge.core.expose.getpass.getuser", lambda: "current-user")


def invoke(*args: str, paths: Paths) -> Any:
    return CliRunner().invoke(app, list(args), obj={"paths": paths})


# --- guest user selection ----------------------------------------------------


def test_guest_user_dir_returns_the_single_user(prefix: Path) -> None:
    assert guest_user_dir(prefix) == prefix / "drive_c" / "users" / USER


def test_guest_user_dir_requires_a_users_tree(paths: Paths, profile: Profile) -> None:
    prefix = paths.prefix_for(profile.id)
    prefix.mkdir(parents=True)

    with pytest.raises(WinBridgeError, match=r"drive_c/users"):
        guest_user_dir(prefix)


def test_guest_user_dir_refuses_multiple_users(paths: Paths, profile: Profile) -> None:
    prefix = make_prefix(paths, profile.id, extra_users=(OTHER_USER,))

    with pytest.raises(WinBridgeError, match="multiple guest users") as error:
        guest_user_dir(prefix)

    message = str(error.value)
    assert USER in message
    assert OTHER_USER in message


def test_guest_user_dir_prefers_the_current_user(
    monkeypatch: pytest.MonkeyPatch, paths: Paths, profile: Profile
) -> None:
    prefix = make_prefix(paths, profile.id, extra_users=(OTHER_USER,))
    monkeypatch.setattr("winbridge.core.expose.getpass.getuser", lambda: OTHER_USER)

    assert guest_user_dir(prefix).name == OTHER_USER


def test_guest_user_dir_ignores_public_beside_the_user(prefix: Path) -> None:
    (prefix / "drive_c" / "users" / "Public").mkdir()

    assert guest_user_dir(prefix).name == USER


def test_guest_user_dir_refuses_when_only_builtins_exist(
    paths: Paths, profile: Profile
) -> None:
    users = paths.prefix_for(profile.id) / "drive_c" / "users"
    (users / "Public").mkdir(parents=True)

    with pytest.raises(WinBridgeError, match="only built-in accounts"):
        guest_user_dir(paths.prefix_for(profile.id))


def test_guest_user_dir_ignores_an_all_users_symlink(
    tmp_path: Path, prefix: Path
) -> None:
    target = tmp_path / "all-users"
    target.mkdir()
    (prefix / "drive_c" / "users" / "All Users").symlink_to(
        target, target_is_directory=True
    )

    assert guest_user_dir(prefix).name == USER


def test_guest_user_dir_refuses_a_symlinked_user_home(
    tmp_path: Path, paths: Paths, profile: Profile
) -> None:
    prefix = paths.prefix_for(profile.id)
    users = prefix / "drive_c" / "users"
    users.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (users / USER).symlink_to(outside, target_is_directory=True)

    with pytest.raises(WinBridgeError, match="symlink"):
        guest_user_dir(prefix)


def test_guest_user_dir_refuses_a_symlinked_users_directory(
    tmp_path: Path, paths: Paths, profile: Profile
) -> None:
    prefix = paths.prefix_for(profile.id)
    (prefix / "drive_c").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / USER).mkdir(parents=True)
    (prefix / "drive_c" / "users").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WinBridgeError, match="symlinked guest users"):
        guest_user_dir(prefix)


# --- expose ------------------------------------------------------------------


def test_expose_creates_symlink_and_records_it(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()

    item = expose(profile, paths, host)

    guest_dir = guest_user_dir(paths.prefix_for(profile.id))
    link = guest_dir / "repos"
    assert link.is_symlink() and link.resolve() == host.resolve()
    assert profile.exposed == [item]


def test_expose_records_the_windows_guest_path(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()

    item = expose(profile, paths, host)

    assert item == ExposedPath(host=str(host), guest=guest(USER, "repos"))


def test_expose_refuses_to_replace_existing_path(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    guest_dir = guest_user_dir(paths.prefix_for(profile.id))
    (guest_dir / "repos").mkdir(parents=True)

    with pytest.raises(WinBridgeError):
        expose(profile, paths, host)

    assert (guest_dir / "repos").is_dir()
    assert profile.exposed == []


def test_expose_refuses_to_replace_an_existing_file(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    link = guest_user_dir(paths.prefix_for(profile.id)) / "repos"
    link.write_text("user data", encoding="utf-8")

    with pytest.raises(UsageError, match="already exists"):
        expose(profile, paths, host)

    assert link.read_text(encoding="utf-8") == "user data"
    assert profile.exposed == []


def test_expose_refuses_a_symlink_to_another_folder(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    link = guest_user_dir(paths.prefix_for(profile.id)) / "repos"
    link.symlink_to(other, target_is_directory=True)

    with pytest.raises(UsageError, match="refusing to replace"):
        expose(profile, paths, host)

    assert link.resolve() == other.resolve()
    assert profile.exposed == []


def test_expose_is_idempotent_for_the_same_symlink(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()

    first = expose(profile, paths, host)
    second = expose(profile, paths, host)

    assert second == first
    assert profile.exposed == [first]


def test_expose_refuses_an_unrecorded_identical_symlink(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    link = guest_user_dir(paths.prefix_for(profile.id)) / "repos"
    link.symlink_to(host, target_is_directory=True)

    with pytest.raises(UsageError, match="not recorded"):
        expose(profile, paths, host)

    assert link.resolve() == host.resolve()
    assert profile.exposed == []
    with pytest.raises(WinBridgeError, match="not exposed"):
        unexpose(profile, paths, "repos")
    assert link.is_symlink()


@pytest.mark.parametrize(
    ("first", "second"),
    [("Repos", None), ("repos", "REPOS"), ("REPOS", "repos")],
)
def test_expose_reuses_the_recorded_guest_case(
    tmp_path: Path,
    profile: Profile,
    paths: Paths,
    prefix: Path,
    first: str,
    second: str | None,
) -> None:
    host = tmp_path / "repos"
    host.mkdir()

    item = expose(profile, paths, host, name=first)
    if second is None:
        repeat = expose(profile, paths, host)
    else:
        repeat = expose(profile, paths, host, name=second)

    user = guest_user_dir(paths.prefix_for(profile.id))
    links = sorted(entry.name for entry in user.iterdir() if entry.is_symlink())
    assert repeat == item
    assert profile.exposed == [item]
    assert links == [first]


def test_expose_refuses_a_case_variant_collision(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    user = guest_user_dir(paths.prefix_for(profile.id))
    (user / "Repos").symlink_to(other, target_is_directory=True)

    with pytest.raises(UsageError, match="case-insensitive"):
        expose(profile, paths, host, name="repos")

    assert profile.exposed == []
    assert not (user / "repos").exists() and not (user / "repos").is_symlink()


def test_expose_uses_as_name(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()

    item = expose(profile, paths, host, name="work")

    link = guest_user_dir(paths.prefix_for(profile.id)) / "work"
    assert link.is_symlink()
    assert item.guest == guest(USER, "work")


def test_expose_accepts_dot_underscore_and_hyphen_names(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()

    item = expose(profile, paths, host, name="my-repos_2.0")

    link = guest_user_dir(paths.prefix_for(profile.id)) / "my-repos_2.0"
    assert link.is_symlink()
    assert item.guest == guest(USER, "my-repos_2.0")


@pytest.mark.parametrize(
    "name",
    ["../escape", "a/b", "a\\b", "a b", ".", "..", "", "$bad", "café"],
)
def test_expose_rejects_invalid_names(
    tmp_path: Path,
    profile: Profile,
    paths: Paths,
    prefix: Path,
    name: str,
) -> None:
    host = tmp_path / "repos"
    host.mkdir()

    with pytest.raises(UsageError, match="invalid exposure name"):
        expose(profile, paths, host, name=name)

    assert profile.exposed == []


def test_expose_requires_an_absolute_existing_host(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    with pytest.raises(UsageError, match="absolute"):
        expose(profile, paths, "repos")
    with pytest.raises(UsageError, match="not found"):
        expose(profile, paths, tmp_path / "missing")


def test_expose_rejects_an_unresolvable_home_folder(
    profile: Profile, paths: Paths, prefix: Path
) -> None:
    with pytest.raises(UsageError, match="cannot resolve host folder"):
        expose(profile, paths, "~winbridge-no-such-user-9f3a/repos")


def test_host_paths_with_trailing_spaces_round_trip(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos "
    host.mkdir()

    item = expose(profile, paths, host, name="repos")
    removed = unexpose(profile, paths, str(host))

    assert item.host == str(host)
    assert removed == item
    assert profile.exposed == []


def test_expose_refuses_a_conflicting_guest_record(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    profile.exposed.append(ExposedPath(host=str(other), guest=guest(USER, "repos")))

    with pytest.raises(UsageError, match="already exposed"):
        expose(profile, paths, host)

    link = guest_user_dir(paths.prefix_for(profile.id)) / "repos"
    assert not link.exists() and not link.is_symlink()


def test_expose_allows_the_same_host_under_a_second_name(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()

    first = expose(profile, paths, host)
    second = expose(profile, paths, host, name="work")

    user = guest_user_dir(paths.prefix_for(profile.id))
    assert profile.exposed == [first, second]
    assert first.host == second.host == str(host)
    assert (user / "repos").is_symlink()
    assert (user / "work").is_symlink()


def test_expose_fails_before_creating_a_link_with_multiple_users(
    tmp_path: Path, profile: Profile, paths: Paths
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    prefix = make_prefix(paths, profile.id, extra_users=(OTHER_USER,))

    with pytest.raises(WinBridgeError, match="multiple guest users"):
        expose(profile, paths, host)

    assert not (prefix / "drive_c" / "users" / USER / "repos").exists()
    assert not (prefix / "drive_c" / "users" / USER / "repos").is_symlink()
    assert profile.exposed == []


# --- unexpose ----------------------------------------------------------------


def test_unexpose_by_host_removes_only_the_owned_link(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    item = expose(profile, paths, host)
    link = guest_user_dir(paths.prefix_for(profile.id)) / "repos"

    removed = unexpose(profile, paths, str(host))

    assert removed == item
    assert not link.is_symlink()
    assert profile.exposed == []
    assert host.is_dir()


def test_unexpose_by_host_requires_a_name_when_several_match(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    first = expose(profile, paths, host)
    second = expose(profile, paths, host, name="work")
    user = guest_user_dir(paths.prefix_for(profile.id))

    with pytest.raises(UsageError, match="guest name"):
        unexpose(profile, paths, str(host))

    assert profile.exposed == [first, second]
    assert (user / "repos").is_symlink()
    assert (user / "work").is_symlink()

    removed = unexpose(profile, paths, "work")

    assert removed == second
    assert profile.exposed == [first]
    assert not (user / "work").is_symlink()
    assert (user / "repos").is_symlink()


@pytest.mark.parametrize("ref", ["repos", "REPOS"])
def test_unexpose_by_guest_name(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path, ref: str
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    item = expose(profile, paths, host)

    removed = unexpose(profile, paths, ref)

    assert removed == item
    assert profile.exposed == []
    assert not (guest_user_dir(paths.prefix_for(profile.id)) / "repos").is_symlink()


def test_unexpose_drops_a_stale_record_whose_link_is_gone(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    item = expose(profile, paths, host)
    (guest_user_dir(paths.prefix_for(profile.id)) / "repos").unlink()

    removed = unexpose(profile, paths, "repos")

    assert removed == item
    assert profile.exposed == []


def test_unexpose_refuses_a_link_that_changed_target(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    item = expose(profile, paths, host)
    other = tmp_path / "other"
    other.mkdir()
    link = guest_user_dir(paths.prefix_for(profile.id)) / "repos"
    link.unlink()
    link.symlink_to(other, target_is_directory=True)

    with pytest.raises(UsageError, match="refusing to remove"):
        unexpose(profile, paths, str(host))

    assert link.is_symlink()
    assert profile.exposed == [item]


def test_unexpose_refuses_a_regular_file(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    item = expose(profile, paths, host)
    link = guest_user_dir(paths.prefix_for(profile.id)) / "repos"
    link.unlink()
    link.write_text("user data", encoding="utf-8")

    with pytest.raises(UsageError, match="not a symlink"):
        unexpose(profile, paths, str(host))

    assert link.read_text(encoding="utf-8") == "user data"
    assert profile.exposed == [item]


def test_unexpose_unknown_reference_changes_nothing(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    item = expose(profile, paths, host)

    with pytest.raises(WinBridgeError, match="not exposed"):
        unexpose(profile, paths, str(tmp_path / "missing"))

    assert profile.exposed == [item]
    assert (guest_user_dir(paths.prefix_for(profile.id)) / "repos").is_symlink()


def test_unexpose_rejects_a_relative_host_reference(
    profile: Profile, paths: Paths, prefix: Path
) -> None:
    with pytest.raises(UsageError, match="absolute"):
        unexpose(profile, paths, "some/relative/folder")


def test_unexpose_rejects_a_tampered_guest_record(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    profile.exposed.append(
        ExposedPath(host=str(host), guest="C:\\users\\me\\..\\escape")
    )

    with pytest.raises(WinBridgeError, match="invalid recorded guest"):
        unexpose(profile, paths, str(host))

    assert len(profile.exposed) == 1


def test_unexpose_refuses_a_record_for_another_guest_user(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()
    user = guest_user_dir(paths.prefix_for(profile.id))
    (user / "repos").symlink_to(host, target_is_directory=True)
    item = ExposedPath(host=str(host), guest=guest("other-user", "repos"))
    profile.exposed.append(item)

    with pytest.raises(WinBridgeError, match="does not match prefix user"):
        unexpose(profile, paths, str(host))

    assert (user / "repos").is_symlink()
    assert profile.exposed == [item]


def test_unexpose_keeps_other_exposures(
    tmp_path: Path, profile: Profile, paths: Paths, prefix: Path
) -> None:
    first = tmp_path / "repos"
    first.mkdir()
    second = tmp_path / "assets"
    second.mkdir()
    expose(profile, paths, first)
    kept = expose(profile, paths, second)

    unexpose(profile, paths, str(first))

    guest_dir = guest_user_dir(paths.prefix_for(profile.id))
    assert profile.exposed == [kept]
    assert not (guest_dir / "repos").is_symlink()
    assert (guest_dir / "assets").is_symlink()


# --- CLI ---------------------------------------------------------------------


def test_cli_expose_persists_the_record(
    tmp_path: Path,
    profile: Profile,
    paths: Paths,
    prefix: Path,
    write_profile: Callable[[Path, Profile], Path],
) -> None:
    write_profile(paths.root, profile)
    host = tmp_path / "repos"
    host.mkdir()

    result = invoke("expose", "example-app", str(host), paths=paths)

    assert result.exit_code == 0, result.output
    assert guest(USER, "repos") in result.output
    stored = Profile.load(paths.profiles / "example-app.toml")
    assert stored.exposed == [ExposedPath(host=str(host), guest=guest(USER, "repos"))]
    assert (guest_user_dir(prefix) / "repos").is_symlink()


def test_cli_expose_as_option(
    tmp_path: Path,
    profile: Profile,
    paths: Paths,
    prefix: Path,
    write_profile: Callable[[Path, Profile], Path],
) -> None:
    write_profile(paths.root, profile)
    host = tmp_path / "repos"
    host.mkdir()

    result = invoke("expose", "example-app", str(host), "--as", "work", paths=paths)

    assert result.exit_code == 0, result.output
    stored = Profile.load(paths.profiles / "example-app.toml")
    assert stored.exposed[0].guest == guest(USER, "work")
    assert (guest_user_dir(prefix) / "work").is_symlink()


def test_cli_unexpose_by_name(
    tmp_path: Path,
    profile: Profile,
    paths: Paths,
    prefix: Path,
    write_profile: Callable[[Path, Profile], Path],
) -> None:
    write_profile(paths.root, profile)
    host = tmp_path / "repos"
    host.mkdir()
    invoke("expose", "example-app", str(host), paths=paths)

    result = invoke("unexpose", "example-app", "repos", paths=paths)

    assert result.exit_code == 0, result.output
    assert guest(USER, "repos") in result.output
    assert not (guest_user_dir(prefix) / "repos").is_symlink()
    assert Profile.load(paths.profiles / "example-app.toml").exposed == []


def test_cli_unexpose_by_host_path(
    tmp_path: Path,
    profile: Profile,
    paths: Paths,
    prefix: Path,
    write_profile: Callable[[Path, Profile], Path],
) -> None:
    write_profile(paths.root, profile)
    host = tmp_path / "repos"
    host.mkdir()
    invoke("expose", "example-app", str(host), paths=paths)

    result = invoke("unexpose", "example-app", str(host), paths=paths)

    assert result.exit_code == 0, result.output
    assert not (guest_user_dir(prefix) / "repos").is_symlink()
    assert Profile.load(paths.profiles / "example-app.toml").exposed == []


def test_cli_expose_missing_profile_exits_1(
    tmp_path: Path, paths: Paths, prefix: Path
) -> None:
    host = tmp_path / "repos"
    host.mkdir()

    result = invoke("expose", "missing-app", str(host), paths=paths)

    assert result.exit_code == 1
    assert isinstance(result.exception, WinBridgeError)
    assert "missing-app" in str(result.exception)


def test_cli_expose_missing_prefix_leaves_the_profile_unchanged(
    tmp_path: Path,
    profile: Profile,
    paths: Paths,
    write_profile: Callable[[Path, Profile], Path],
) -> None:
    write_profile(paths.root, profile)
    host = tmp_path / "repos"
    host.mkdir()

    result = invoke("expose", "example-app", str(host), paths=paths)

    assert result.exit_code == 1
    assert isinstance(result.exception, WinBridgeError)
    assert Profile.load(paths.profiles / "example-app.toml").exposed == []


def test_cli_expose_invalid_name_is_a_usage_error(
    tmp_path: Path,
    profile: Profile,
    paths: Paths,
    prefix: Path,
    write_profile: Callable[[Path, Profile], Path],
) -> None:
    write_profile(paths.root, profile)
    host = tmp_path / "repos"
    host.mkdir()

    result = invoke("expose", "example-app", str(host), "--as", "../escape", paths=paths)

    assert isinstance(result.exception, UsageError)
    assert Profile.load(paths.profiles / "example-app.toml").exposed == []


def test_cli_unexpose_unknown_reference_exits_1(
    tmp_path: Path,
    profile: Profile,
    paths: Paths,
    prefix: Path,
    write_profile: Callable[[Path, Profile], Path],
) -> None:
    write_profile(paths.root, profile)
    host = tmp_path / "repos"
    host.mkdir()

    result = invoke("unexpose", "example-app", str(host), paths=paths)

    assert result.exit_code == 1
    assert isinstance(result.exception, WinBridgeError)
    assert "not exposed" in str(result.exception)


def test_cli_expose_invalid_name_exits_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: Profile,
    paths: Paths,
    prefix: Path,
    write_profile: Callable[[Path, Profile], Path],
) -> None:
    write_profile(paths.root, profile)
    host = tmp_path / "repos"
    host.mkdir()
    monkeypatch.setattr("winbridge.cli._paths", lambda ctx: paths)
    monkeypatch.setattr(
        sys,
        "argv",
        ["winbridge", "expose", "example-app", str(host), "--as", "../escape"],
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2
    assert "invalid exposure name" in capsys.readouterr().err
