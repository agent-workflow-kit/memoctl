"""The state directory check and creation (interface.md, State).

Everything runs in temp dirs through a gateway whose home is private: the real gateway is
subclassed to point at a temp home, the fake covers a foreign owner.
"""

from __future__ import annotations

import errno
import os
import pickle
import stat
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from memoctl.config import ConfigError
from memoctl.gateway import DirHandle, GatewayError, RealGateway
from memoctl.statedir import SETTLE_TRIES, StateAccessError, UnsafeStateError, open_state_dir
from tests.fake_gateway import FakeGateway


class HomeGateway(RealGateway):
    """The real gateway with a private home (never the password-database one)."""

    def __init__(self, home: Path) -> None:
        super().__init__()
        self._h = home

    def home(self) -> Path:
        return self._h


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "h"
    h.mkdir()
    return h


@pytest.fixture
def gw(home: Path) -> HomeGateway:
    return HomeGateway(home)


@pytest.fixture
def umask_all() -> Iterator[None]:
    old = os.umask(0o777)
    try:
        yield
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _state(home: Path) -> Path:
    return home / ".local" / "state" / "memoctl"


# --- missing and creation ---------------------------------------------------------------------


def test_missing_is_none_and_nothing_is_created(gw: HomeGateway, home: Path) -> None:
    assert open_state_dir(gw, create=False) is None
    assert os.listdir(home) == []


def test_missing_leaf_with_existing_parents_creates_nothing(gw: HomeGateway, home: Path) -> None:
    (home / ".local" / "state").mkdir(parents=True)
    assert open_state_dir(gw, create=False) is None
    assert os.listdir(home / ".local" / "state") == []


def test_create_makes_every_missing_level_at_0700_despite_the_umask(
    gw: HomeGateway, home: Path, umask_all: None
) -> None:
    sd = open_state_dir(gw, create=True)
    assert sd is not None
    sd.close()
    for path in (home / ".local", home / ".local" / "state", _state(home)):
        assert path.is_dir() and not path.is_symlink()
        assert _mode(path) == 0o700


def test_create_returns_the_path_under_the_home(gw: HomeGateway, home: Path) -> None:
    with open_state_dir(gw, create=True) as sd:
        assert sd is not None
        assert sd.path == _state(home)


def test_existing_parents_are_not_checked_or_changed(gw: HomeGateway, home: Path) -> None:
    (home / ".local" / "state").mkdir(parents=True)
    (home / ".local").chmod(0o755)
    (home / ".local" / "state").chmod(0o775)
    with open_state_dir(gw, create=True) as sd:
        assert sd is not None
    assert _mode(home / ".local") == 0o755
    assert _mode(home / ".local" / "state") == 0o775
    assert _mode(_state(home)) == 0o700


def test_a_symlinked_parent_is_followed_not_checked(
    gw: HomeGateway, home: Path, tmp_path: Path
) -> None:
    # interface.md: existing parents are not checked (dotfile managers symlink .local).
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "state").mkdir(parents=True)
    (home / ".local").symlink_to(elsewhere)
    with open_state_dir(gw, create=True) as sd:
        assert sd is not None
    assert _mode(elsewhere / "state" / "memoctl") == 0o700


def test_repeated_open_is_idempotent(gw: HomeGateway, home: Path) -> None:
    for _ in range(3):
        with open_state_dir(gw, create=True) as sd:
            assert sd is not None
    sd = open_state_dir(gw, create=False)
    assert sd is not None
    sd.close()


def test_a_good_existing_dir_opens_without_create(gw: HomeGateway, home: Path) -> None:
    _state(home).mkdir(parents=True, mode=0o700)
    sd = open_state_dir(gw, create=False)
    assert sd is not None
    sd.close()


# --- refusals ---------------------------------------------------------------------------------


@pytest.mark.parametrize("create", [False, True])
@pytest.mark.parametrize("mode", [0o750, 0o770, 0o755, 0o707, 0o701, 0o740, 0o777, 0o710])
def test_group_or_other_permissions_are_refused_and_not_fixed(
    gw: HomeGateway, home: Path, mode: int, create: bool
) -> None:
    _state(home).mkdir(parents=True)
    _state(home).chmod(mode)
    with pytest.raises(UnsafeStateError):
        open_state_dir(gw, create=create)
    assert _mode(_state(home)) == mode


@pytest.mark.parametrize("create", [False, True])
def test_a_symlink_as_the_state_dir_is_refused(gw: HomeGateway, home: Path, create: bool) -> None:
    (home / ".local" / "state").mkdir(parents=True)
    real = home / "real"
    real.mkdir(mode=0o700)
    _state(home).symlink_to(real)
    with pytest.raises(UnsafeStateError):
        open_state_dir(gw, create=create)


@pytest.mark.parametrize("create", [False, True])
def test_a_dangling_symlink_as_the_state_dir_is_refused_and_not_created_through(
    gw: HomeGateway, home: Path, tmp_path: Path, create: bool
) -> None:
    (home / ".local" / "state").mkdir(parents=True)
    _state(home).symlink_to(tmp_path / "nowhere")
    with pytest.raises(UnsafeStateError) as info:
        open_state_dir(gw, create=create)
    assert info.value.reason == "not_a_directory"
    assert not (tmp_path / "nowhere").exists()


@pytest.mark.parametrize("create", [False, True])
def test_a_regular_file_as_the_state_dir_is_refused(
    gw: HomeGateway, home: Path, create: bool
) -> None:
    (home / ".local" / "state").mkdir(parents=True)
    _state(home).write_bytes(b"x")
    with pytest.raises(UnsafeStateError):
        open_state_dir(gw, create=create)


@pytest.mark.parametrize("create", [False, True])
@pytest.mark.parametrize("level", [".local", ".local/state"])
def test_a_file_where_a_parent_should_be_is_refused(
    gw: HomeGateway, home: Path, level: str, create: bool
) -> None:
    target = home / level
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    with pytest.raises(UnsafeStateError):
        open_state_dir(gw, create=create)
    assert target.read_bytes() == b"x"


@pytest.mark.parametrize("create", [False, True])
def test_a_dangling_symlink_as_a_parent_is_refused_on_create_and_missing_on_read(
    gw: HomeGateway, home: Path, tmp_path: Path, create: bool
) -> None:
    (home / ".local").symlink_to(tmp_path / "nowhere")
    if create:
        with pytest.raises(UnsafeStateError) as info:
            open_state_dir(gw, create=True)
        assert info.value.reason == "parent"
        assert not (tmp_path / "nowhere").exists()
    else:
        assert open_state_dir(gw, create=False) is None


def test_a_foreign_owner_is_refused(tmp_path: Path) -> None:
    fake = FakeGateway(tmp_path / "root")
    h = fake.home()
    (h / ".local" / "state" / "memoctl").mkdir(parents=True, mode=0o700)
    (h / ".local" / "state" / "memoctl").chmod(0o700)
    fake.uid_value = os.getuid() + 1
    for create in (False, True):
        with pytest.raises(UnsafeStateError) as info:
            open_state_dir(fake, create=create)
        assert info.value.reason == "owner"


def test_an_unreadable_own_dir_is_refused(gw: HomeGateway, home: Path) -> None:
    _state(home).mkdir(parents=True)
    _state(home).chmod(0)
    try:
        if os.access(_state(home), os.R_OK):
            pytest.skip("permissions are not enforced for this user")
        with pytest.raises(UnsafeStateError) as info:
            open_state_dir(gw, create=False)
        assert info.value.reason == "mode"
    finally:
        _state(home).chmod(0o700)


def test_the_error_is_fixed_text_without_paths_and_pickles(gw: HomeGateway, home: Path) -> None:
    _state(home).mkdir(parents=True)
    _state(home).chmod(0o755)
    with pytest.raises(UnsafeStateError) as info:
        open_state_dir(gw, create=False)
    exc = info.value
    assert exc.code == "unsafe_state"
    assert str(exc) == "unsafe state directory"
    assert str(home) not in str(exc) and str(home) not in repr(exc)
    again = pickle.loads(pickle.dumps(exc))
    assert isinstance(again, UnsafeStateError)
    assert again.reason == exc.reason == "mode"


def test_the_reasons_are_a_fixed_vocabulary(gw: HomeGateway, home: Path) -> None:
    (home / ".local" / "state").mkdir(parents=True)
    real = home / "real"
    real.mkdir(mode=0o700)
    _state(home).symlink_to(real)
    with pytest.raises(UnsafeStateError) as info:
        open_state_dir(gw, create=False)
    assert info.value.reason == "not_a_directory"


# --- creation errors keep their own mapping (R1) ----------------------------------------------


def _unwritable(path: Path) -> bool:
    return os.access(path, os.W_OK)


@pytest.mark.parametrize("level", ["", ".local", ".local/state"])
def test_a_read_only_directory_above_a_missing_level_raises_permission_error(
    gw: HomeGateway, home: Path, level: str
) -> None:
    holder = home / level if level else home
    holder.mkdir(parents=True, exist_ok=True)
    before = sorted(p.relative_to(home).as_posix() for p in home.rglob("*"))
    holder.chmod(0o500)
    try:
        if _unwritable(holder):
            pytest.skip("permissions are not enforced for this user")
        with pytest.raises(StateAccessError) as info:
            open_state_dir(gw, create=True)
        assert isinstance(info.value.__cause__, PermissionError)
        assert sorted(p.relative_to(home).as_posix() for p in home.rglob("*")) == before
    finally:
        holder.chmod(0o700)


class _Failing:
    """Wraps a handle so ``make_dir`` of one name fails with the given errno."""

    def __init__(self, inner: DirHandle, name: str, err: int) -> None:
        self._inner = inner
        self._name = name
        self._err = err

    def make_dir(self, name: str, mode: int) -> None:
        if name == self._name:
            raise OSError(self._err, os.strerror(self._err))
        self._inner.make_dir(name, mode)

    def open_dir(self, name: str, *, follow: bool) -> Any:
        sub = self._inner.open_dir(name, follow=follow)
        return None if sub is None else _Failing(sub, self._name, self._err)

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._inner, attr)


class FailingGateway(HomeGateway):
    def __init__(self, home: Path, name: str, err: int) -> None:
        super().__init__(home)
        self.args = (name, err)

    def open_dir(self, path: Path, *, follow: bool) -> Any:
        handle = super().open_dir(path, follow=follow)
        return None if handle is None else _Failing(handle, *self.args)


@pytest.mark.parametrize("name", [".local", "state", "memoctl"])
@pytest.mark.parametrize("err", [errno.EROFS, errno.EACCES, errno.EIO])
def test_a_failed_mkdir_propagates_its_own_error_at_every_level(
    home: Path, name: str, err: int
) -> None:
    g = FailingGateway(home, name, err)
    with pytest.raises(StateAccessError) as info:
        open_state_dir(g, create=True)
    cause = info.value.__cause__
    assert isinstance(cause, OSError) and cause.errno == err
    assert not isinstance(info.value, UnsafeStateError)


def test_an_unexplained_eacces_on_the_final_open_propagates(home: Path) -> None:
    class Denying(_Failing):
        def open_dir(self, name: str, *, follow: bool) -> Any:
            if name == "memoctl":
                raise OSError(errno.EACCES, "denied")
            sub = self._inner.open_dir(name, follow=follow)
            return None if sub is None else Denying(sub, "", 0)

    class G(HomeGateway):
        def open_dir(self, path: Path, *, follow: bool) -> Any:
            handle = super().open_dir(path, follow=follow)
            return None if handle is None else Denying(handle, "", 0)

    _state(home).mkdir(parents=True, mode=0o700)
    with pytest.raises(StateAccessError) as info:
        open_state_dir(G(home), create=False)
    cause = info.value.__cause__
    assert isinstance(cause, OSError) and cause.errno == errno.EACCES
    assert not isinstance(info.value, UnsafeStateError)


# --- home -------------------------------------------------------------------------------------


@pytest.mark.parametrize("create", [False, True])
def test_the_home_comes_from_resolve_home(tmp_path: Path, create: bool) -> None:
    class Missing(RealGateway):
        def home(self) -> Path:
            return tmp_path / "no-such-home"

    class Lookup(RealGateway):
        def home(self) -> Path:
            raise GatewayError("no pw entry")

    class Relative(RealGateway):
        def home(self) -> Path:
            return Path("rel")

    for g in (Missing(), Lookup(), Relative()):
        with pytest.raises(ConfigError) as info:
            open_state_dir(g, create=create)
        assert info.value.field == "home"
    assert not (tmp_path / "no-such-home").exists()


def test_the_home_variables_are_never_read(
    gw: HomeGateway, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("HOME", str(other))
    monkeypatch.setenv("XDG_STATE_HOME", str(other / "xdg"))
    with open_state_dir(gw, create=True) as sd:
        assert sd is not None
    assert _state(home).is_dir()
    assert os.listdir(other) == []


class _SwapAfterMake:
    """Wraps a handle so ``name`` becomes a symlink right after our mkdir of it."""

    def __init__(self, inner: DirHandle, name: str, target: Path, path: Path) -> None:
        self._inner = inner
        self._args = (name, target, path)
        self._path = path

    def make_dir(self, name: str, mode: int) -> None:
        self._inner.make_dir(name, mode)
        if name == self._args[0]:
            os.rmdir(self._path)
            os.symlink(self._args[1], self._path)

    def open_dir(self, name: str, *, follow: bool) -> Any:
        sub = self._inner.open_dir(name, follow=follow)
        return None if sub is None else _SwapAfterMake(sub, *self._args)

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._inner, attr)


def test_a_symlink_swapped_in_right_after_the_final_mkdir_is_refused(
    home: Path, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)

    class G(HomeGateway):
        def open_dir(self, path: Path, *, follow: bool) -> Any:
            handle = super().open_dir(path, follow=follow)
            return (
                None if handle is None else _SwapAfterMake(handle, "memoctl", target, _state(home))
            )

    with pytest.raises(UnsafeStateError) as info:
        open_state_dir(G(home), create=True)
    assert info.value.reason == "not_a_directory"


# --- races ------------------------------------------------------------------------------------


class _Racer:
    """Wraps a handle so a rival creates ``name`` just before our mkdir of it."""

    def __init__(self, inner: DirHandle, rival: dict[str, int]) -> None:
        self._inner = inner
        self._rival = rival

    def make_dir(self, name: str, mode: int) -> None:
        if name in self._rival:
            self._inner.make_dir(name, self._rival.pop(name))
            sub = self._inner.open_dir(name, follow=False)
            assert sub is not None
            sub.set_mode(0o700 if name != "memoctl" else _RIVAL_MODE[0])
            sub.close()
        self._inner.make_dir(name, mode)

    def open_dir(self, name: str, *, follow: bool) -> Any:
        sub = self._inner.open_dir(name, follow=follow)
        return None if sub is None else _Racer(sub, self._rival)

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._inner, attr)


_RIVAL_MODE = [0o700]


class RacingGateway(HomeGateway):
    def __init__(self, home: Path, rival: dict[str, int]) -> None:
        super().__init__(home)
        self.rival = rival

    def open_dir(self, path: Path, *, follow: bool) -> Any:
        handle = super().open_dir(path, follow=follow)
        return None if handle is None else _Racer(handle, self.rival)


def test_a_rival_creating_the_dir_first_with_0700_is_accepted(home: Path) -> None:
    _RIVAL_MODE[0] = 0o700
    g = RacingGateway(home, {".local": 0o700, "state": 0o700, "memoctl": 0o700})
    with open_state_dir(g, create=True) as sd:
        assert sd is not None
    assert g.rival == {}
    assert _mode(_state(home)) == 0o700


def test_a_rival_creating_the_dir_first_with_a_wide_mode_is_refused(home: Path) -> None:
    _RIVAL_MODE[0] = 0o755
    g = RacingGateway(home, {"memoctl": 0o755})
    with pytest.raises(UnsafeStateError):
        open_state_dir(g, create=True)
    assert _mode(_state(home)) == 0o755


def _race_once(h: Path) -> list[object]:
    g = HomeGateway(h)
    barrier = threading.Barrier(6)
    results: list[object] = []

    def worker() -> None:
        barrier.wait()
        try:
            with open_state_dir(g, create=True) as sd:
                results.append(sd is not None)
        except BaseException as exc:  # noqa: BLE001 - collected and asserted by the caller
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def test_concurrent_creators_all_succeed_with_0700(tmp_path: Path) -> None:
    for round_ in range(20):
        h = tmp_path / f"h{round_}"
        h.mkdir()
        assert _race_once(h) == [True] * 6
        assert _mode(_state(h)) == 0o700
        assert _mode(h / ".local") == 0o700


# --- state.db ---------------------------------------------------------------------------------


def _open(gw: HomeGateway) -> Any:
    sd = open_state_dir(gw, create=True)
    assert sd is not None
    return sd


def test_db_path_and_missing_db(gw: HomeGateway, home: Path) -> None:
    with _open(gw) as sd:
        assert sd.db_path == _state(home) / "state.db"
        assert sd.check_db() is False
        assert os.listdir(_state(home)) == []


def test_create_db_is_exclusive_0600_and_empty(
    gw: HomeGateway, home: Path, umask_all: None
) -> None:
    with _open(gw) as sd:
        assert sd.create_db() is True
        assert sd.create_db() is False  # EEXIST: another process created it
        assert sd.check_db() is True
    assert _mode(_state(home) / "state.db") == 0o600
    assert (_state(home) / "state.db").stat().st_size == 0


def test_create_db_never_touches_an_existing_file(gw: HomeGateway, home: Path) -> None:
    with _open(gw) as sd:
        (_state(home) / "state.db").write_bytes(b"SQLite format 3\0")
        assert sd.create_db() is False
    assert (_state(home) / "state.db").read_bytes() == b"SQLite format 3\0"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o666, 0o644])
def test_check_db_refuses_group_or_other_access(gw: HomeGateway, home: Path, mode: int) -> None:
    with _open(gw) as sd:
        (_state(home) / "state.db").write_bytes(b"")
        (_state(home) / "state.db").chmod(mode)
        with pytest.raises(UnsafeStateError):
            sd.check_db()


def test_check_db_refuses_a_symlink_and_create_does_not_write_through(
    gw: HomeGateway, home: Path, tmp_path: Path
) -> None:
    with _open(gw) as sd:
        (_state(home) / "state.db").symlink_to(tmp_path / "victim")
        with pytest.raises(UnsafeStateError):
            sd.check_db()
        assert sd.create_db() is False
        with pytest.raises(UnsafeStateError):
            sd.check_db()
    assert not (tmp_path / "victim").exists()


def test_check_db_refuses_a_directory_and_a_fifo(gw: HomeGateway, home: Path) -> None:
    with _open(gw) as sd:
        (_state(home) / "state.db").mkdir()
        with pytest.raises(UnsafeStateError):
            sd.check_db()
        (_state(home) / "state.db").rmdir()
        os.mkfifo(_state(home) / "state.db", 0o600)
        with pytest.raises(UnsafeStateError):
            sd.check_db()


def test_check_db_refuses_a_foreign_owner(tmp_path: Path) -> None:
    fake = FakeGateway(tmp_path / "root")
    with open_state_dir(fake, create=True) as sd:
        assert sd is not None
        assert sd.create_db() is True
        fake.uid_value = os.getuid() + 1
        with pytest.raises(UnsafeStateError):
            sd.check_db()


def test_check_db_checks_the_file_inside_the_checked_directory(
    gw: HomeGateway, home: Path, tmp_path: Path
) -> None:
    # The directory is held by descriptor: swapping the path afterwards cannot redirect it.
    with _open(gw) as sd:
        assert sd.create_db() is True
        moved = tmp_path / "moved"
        _state(home).rename(moved)
        _state(home).mkdir(mode=0o700)
        (_state(home) / "state.db").write_bytes(b"")
        (_state(home) / "state.db").chmod(0o666)
        assert sd.check_db() is True  # the held directory's own, safe, file


# --- a creator that has not yet set the mode (first-run race, #20) ---------------------------


class _SlowCreator(HomeGateway):
    """The first ``pause`` plays the creator finishing: it sets 0700 on the named levels."""

    def __init__(self, home: Path, levels: list[Path]) -> None:
        super().__init__(home)
        self.levels = levels
        self.pauses: list[float] = []

    def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        for level in self.levels:
            level.chmod(0o700)
        self.levels = []


@pytest.mark.parametrize("mode", [0o000, 0o100, 0o500, 0o300])
def test_a_level_a_creator_has_not_yet_chmodded_is_waited_for_not_refused(
    home: Path, mode: int
) -> None:
    levels = [home / ".local", home / ".local" / "state", _state(home)]
    for level in levels:
        level.mkdir()
    for level in reversed(levels):  # the leaf first, so the parents stay reachable meanwhile
        level.chmod(mode)
    gw = _SlowCreator(home, levels)
    with open_state_dir(gw, create=True) as sd:
        assert sd is not None
    assert gw.pauses  # it waited
    for level in levels:
        assert _mode(level) == 0o700


def test_a_final_level_that_never_settles_is_still_handled_as_before(home: Path) -> None:
    for level in (home / ".local", home / ".local" / "state", _state(home)):
        level.mkdir()
    _state(home).chmod(0o000)
    gw = HomeGateway(home)
    pauses: list[float] = []
    gw.pause = pauses.append  # type: ignore[method-assign]
    try:
        with pytest.raises(UnsafeStateError) as info:
            open_state_dir(gw, create=True)
    finally:
        _state(home).chmod(0o700)
    assert info.value.reason == "mode"
    assert len(pauses) == 2 * SETTLE_TRIES  # bounded: the wait runs twice
