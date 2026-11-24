"""The password-database guard and the session backstop (``tests/conftest.py``)."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from memoctl.gateway import RealGateway
from tests import passwd_guard
from tests.passwd_guard import GuardedPasswd, RealHomeError


def test_the_real_gateway_home_is_refused_inside_a_test() -> None:
    with pytest.raises(AssertionError, match="real home"):
        RealGateway().home()


def test_other_fields_still_read() -> None:
    entry = pwd.getpwuid(os.getuid())
    assert entry.pw_name and entry.pw_uid == os.getuid()
    assert RealGateway().login_name() == entry.pw_name


def test_getpwnam_is_guarded_too() -> None:
    name = pwd.getpwuid(os.getuid()).pw_name
    with pytest.raises(RealHomeError):
        _ = pwd.getpwnam(name).pw_dir


def test_tuple_access_to_the_home_is_refused() -> None:
    entry = pwd.getpwuid(os.getuid())
    with pytest.raises(RealHomeError):
        _ = entry[5]
    with pytest.raises(RealHomeError):
        _ = entry[-2]
    with pytest.raises(RealHomeError):
        list(entry)
    assert len(entry) == 7 and entry[0] == entry.pw_name


def test_a_home_other_than_the_real_one_passes_through() -> None:
    real = pwd.getpwuid  # the guarded lookup; build an entry with another home
    assert isinstance(real(os.getuid()), GuardedPasswd)
    other = pwd.struct_passwd(("n", "x", 1, 2, "g", "/somewhere/else", "/bin/sh"))
    entry = GuardedPasswd(other, "/the/real/home")
    assert entry.pw_dir == "/somewhere/else" and entry[5] == "/somewhere/else"
    assert tuple(entry) == tuple(other)


@pytest.mark.real_passwd
def test_the_marker_opts_out_of_the_guard() -> None:
    assert not isinstance(pwd.getpwuid(os.getuid()), GuardedPasswd)
    assert pwd.getpwuid(os.getuid()).pw_dir == passwd_guard.REAL_HOME


def test_the_backstop_reports_a_directory_that_appeared_or_changed(tmp_path: Path) -> None:
    before = passwd_guard.real_state_snapshot(tmp_path)
    assert before == {rel: None for rel in passwd_guard.WATCHED}
    assert passwd_guard.changed_paths(before, passwd_guard.real_state_snapshot(tmp_path)) == []
    state = tmp_path / ".local/state/memoctl"
    state.mkdir(parents=True)
    appeared = passwd_guard.real_state_snapshot(tmp_path)
    assert passwd_guard.changed_paths(before, appeared) == [".local/state/memoctl"]
    os.utime(state, ns=(1, 1))
    touched = passwd_guard.real_state_snapshot(tmp_path)
    assert passwd_guard.changed_paths(appeared, touched) == [".local/state/memoctl"]
    state.rmdir()
    assert passwd_guard.changed_paths(touched, passwd_guard.real_state_snapshot(tmp_path)) == [
        ".local/state/memoctl"
    ]
