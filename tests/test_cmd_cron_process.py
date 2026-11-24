"""``memoctl cron`` as a real process against ``tests/fakebin/crontab`` (never the real one):
exact bytes on the pipe, the call sequence, failures, a missing ``crontab``, closed descriptors.
Private home throughout (see ``tests/private_home.py``)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from memoctl.cron_block import block_bytes
from tests import private_home
from tests.schema_check import validate

SECRET = "SECRET-TABLE-TEXT"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def cron(
    home: Path,
    bindir: Path | None,
    *args: str,
    extra: dict[str, str] | None = None,
    pass_fds: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    import os

    # Never "/usr/bin:/bin" here: a real crontab lives there. Without a fake, PATH is an empty
    # directory, so no ``crontab`` can be found at all.
    path = f"{bindir}:/usr/bin:/bin" if bindir is not None else str(home / "empty-path")
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": path,
        "LC_ALL": "en_US.UTF-8",  # the gateway must force C for crontab itself
        "CRONTAB_NOHEADER": "N",  # and must not pass this on
        **({"FAKE_CRONTAB_DIR": os.environ["FAKE_CRONTAB_DIR"]} if bindir is not None else {}),
        **(extra or {}),
    }
    return subprocess.run(
        private_home.argv(home, "cron", *args),
        input=b"", capture_output=True, env=env, timeout=60,
    )  # fmt: skip


def calls(state: Path) -> list[str]:
    return (state / "calls.log").read_text().splitlines()


def test_install_and_uninstall_roundtrip_with_exact_bytes(
    home: Path, fake_crontab: tuple[str, Path]
) -> None:
    script, state = fake_crontab
    bindir = Path(script).parent
    original = b"\xff\x00weird\r\n0 * * * * x\n@reboot y"  # no trailing LF, CR, NUL, non-UTF-8
    (state / "crontab").write_bytes(original)
    block = block_bytes(str(home))

    done = cron(home, bindir, "install", "--json")
    assert done.returncode == 0, done.stderr
    validate("cli/cron-install", json.loads(done.stdout))
    assert (state / "input.2").read_bytes() == original + b"\n" + block
    assert calls(state) == ["1 -l exit=0", "2 - exit=0"]
    assert (state / "env.1").read_text() == "LC_ALL=C\nCRONTAB_NOHEADER=<unset>\n"
    assert (state / "env.2").read_text() == "LC_ALL=C\nCRONTAB_NOHEADER=<unset>\n"

    again = cron(home, bindir, "install")  # idempotent: reads, does not write
    assert (again.returncode, again.stdout) == (0, b"cron block unchanged\n")
    assert calls(state)[2:] == ["3 -l exit=0"]

    gone = cron(home, bindir, "uninstall", "--json")
    assert gone.returncode == 0
    assert calls(state)[3:] == ["4 -l exit=0", "5 - exit=0"]
    assert (state / "input.5").read_bytes() == original + b"\n"
    assert (state / "crontab").read_bytes() == original + b"\n"


def test_no_crontab_yet_is_empty_not_an_error(home: Path, fake_crontab: tuple[str, Path]) -> None:
    script, state = fake_crontab
    done = cron(home, Path(script).parent, "install")
    assert (done.returncode, done.stdout) == (0, b"cron block installed\n")
    assert (state / "input.2").read_bytes() == block_bytes(str(home))


def test_dry_run_makes_exactly_one_read_call(home: Path, fake_crontab: tuple[str, Path]) -> None:
    script, state = fake_crontab
    done = cron(home, Path(script).parent, "install", "--dry-run")
    assert (done.returncode, done.stdout) == (0, b"dry run: cron block would be installed\n")
    assert calls(state) == ["1 -l exit=1"]
    assert not (state / "crontab").exists()


@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_a_crontab_that_fails_is_exit_12_and_stores_nothing(
    home: Path, fake_crontab: tuple[str, Path], command: str
) -> None:
    script, state = fake_crontab
    done = cron(home, Path(script).parent, command, "--json", extra={"FAKE_CRONTAB_EXIT": "5"})
    assert done.returncode == 12
    error = json.loads(done.stdout)["error"]
    assert (error["code"], error["reason"]) == ("failed", "crontab_failed")
    assert not (state / "crontab").exists()
    assert b"outcome" not in done.stdout


def test_a_failing_write_after_a_good_read_is_exit_12(
    home: Path, fake_crontab: tuple[str, Path]
) -> None:
    script, state = fake_crontab
    wrapper = Path(script).parent / "wrapper"
    wrapper.mkdir()
    # a crontab that lists fine and refuses to install, with a message that must not be echoed
    fail = wrapper / "crontab"
    fail.write_text(
        f"#!/bin/sh\nif [ \"$1\" = -l ]; then echo '0 * * * * x'; exit 0; fi\n"
        f"cat >/dev/null; echo {SECRET} >&2; exit 1\n"
    )
    fail.chmod(0o755)
    done = cron(home, wrapper, "install")
    assert done.returncode == 12
    assert SECRET.encode() not in done.stdout + done.stderr
    assert done.stderr.startswith(b"failed (crontab_failed)")
    assert done.stdout == b""


def test_a_crontab_missing_from_path_is_exit_12(home: Path) -> None:
    (home / "empty-path").mkdir()
    done = cron(home, None, "install", "--json")
    assert done.returncode == 12
    assert json.loads(done.stdout)["error"]["reason"] == "crontab_failed"


def test_a_malformed_block_is_exit_20_and_nothing_is_written(
    home: Path, fake_crontab: tuple[str, Path]
) -> None:
    script, state = fake_crontab
    (state / "crontab").write_bytes(b"# memoctl managed\n" + SECRET.encode() + b"\n")
    done = cron(home, Path(script).parent, "uninstall")
    assert done.returncode == 20
    assert SECRET.encode() not in done.stderr + done.stdout
    assert calls(state) == ["1 -l exit=0"]


def test_an_unsafe_home_is_exit_2_and_the_crontab_is_never_called(
    tmp_path: Path, fake_crontab: tuple[str, Path]
) -> None:
    script, state = fake_crontab
    home = tmp_path / "ho me"  # a space cannot be written into a crontab line
    home.mkdir()
    done = cron(home, Path(script).parent, "install", "--json")
    assert done.returncode == 2
    assert json.loads(done.stdout)["error"]["details"] == {"field": "home"}
    assert not (state / "calls.log").exists()


def test_closed_stdout_does_not_leave_a_half_written_crontab(
    home: Path, fake_crontab: tuple[str, Path]
) -> None:
    script, state = fake_crontab
    import os

    env = {
        "HOME": str(home), "PATH": f"{Path(script).parent}:/usr/bin:/bin",
        "FAKE_CRONTAB_DIR": os.environ["FAKE_CRONTAB_DIR"],
    }  # fmt: skip
    done = subprocess.run(
        private_home.argv(home, "cron", "install"),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, timeout=60,
        preexec_fn=lambda: os.close(1),
    )  # fmt: skip
    # With fd 1 closed the tool opens /dev/null there (interface.md), so it still succeeds, and
    # the table was written whole.
    assert done.returncode == 0
    assert (state / "crontab").read_bytes() == block_bytes(str(home))
