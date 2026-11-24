"""Child entry point of the ``add`` concurrency tests: ``wrapper.py <home> <memoctl args...>``.

Like ``private_home_wrapper.py`` (the home comes from a replaced password entry), plus a few
test-only controls read from the environment; nothing of the production code is changed on disk:

- ``ADDTEST_UMASK`` (octal): the umask the process runs under (a hostile one for the
  first-run race).
- ``ADDTEST_SYNC`` (a directory): the process records ``<pid>`` there when it is imported and
  ready, then waits (bounded at 60 s) for the file ``go`` in that directory, so the parent can
  release every child at the same moment.
- ``ADDTEST_BUSY_MS``: replaces the writer's busy timeout (5 s) so a ``db_locked`` run is quick.
- ``ADDTEST_STATEDIR_ONLY``: the process only creates the state directory (``open_state_dir``
  with ``create=True``) and exits 0; the first-run race under a hostile umask is about the
  directory levels, while SQLite creates its own ``-wal`` and ``-shm`` files with the umask
  applied (a window this tool cannot close).
- ``ADDTEST_CHMOD_DELAY_MS``: every ``chmod`` relative to a directory descriptor (the one that
  sets a just-made state directory level to 0700) sleeps this long first, so the window in which
  a level has only what ``mkdir`` left under the umask is wide and a rival reliably sees it.
- ``ADDTEST_ROTATE_BARRIER`` (a directory) and ``ADDTEST_ROTATE_COUNT`` (default 2): a writer
  that has decided to rotate the log (it is at the cap) records ``<pid>`` in the directory and
  waits (bounded at 30 s) until that many writers have, before it takes the rotation lock, so
  all of them decided to rotate before the first one did.
- ``ADDTEST_HOLD`` (``after_insert`` or ``after_commit``): ``add`` prints ``READY`` on stdout and
  sleeps, either inside the write transaction (after the insert, before the commit) or after the
  commit and before the log append, so the parent can kill it exactly there.
"""

from __future__ import annotations

import os
import pwd
import sys
import time

HOME = sys.argv[1]
_real_uid = pwd.getpwuid
_real_nam = pwd.getpwnam
_OWN = _real_uid(os.getuid()).pw_name


def _private(r: pwd.struct_passwd) -> pwd.struct_passwd:
    return pwd.struct_passwd(
        (r.pw_name, r.pw_passwd, r.pw_uid, r.pw_gid, r.pw_gecos, HOME, r.pw_shell)
    )


pwd.getpwuid = lambda uid: _private(_real_uid(uid))  # type: ignore[assignment]
pwd.getpwnam = lambda name: _private(_real_nam(name)) if name == _OWN else _real_nam(name)  # type: ignore[assignment]

from memoctl import cmd_add  # noqa: E402
from memoctl import store as store_mod  # noqa: E402
from memoctl.cli import main  # noqa: E402

if "ADDTEST_BUSY_MS" in os.environ:
    store_mod.WRITE_BUSY_MS = int(os.environ["ADDTEST_BUSY_MS"])

_hold = os.environ.get("ADDTEST_HOLD")
if _hold == "after_insert":
    _insert = cmd_add.step13_insert

    def _hold_insert(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = _insert(*args, **kwargs)
        os.write(1, b"READY\n")
        time.sleep(120)
        return result

    cmd_add.step13_insert = _hold_insert  # type: ignore[assignment]
elif _hold == "after_commit":
    _log = cmd_add.step14_append_log

    def _hold_log(*args, **kwargs):  # type: ignore[no-untyped-def]
        os.write(1, b"READY\n")
        time.sleep(120)
        return _log(*args, **kwargs)

    cmd_add.step14_append_log = _hold_log  # type: ignore[assignment]

_delay = os.environ.get("ADDTEST_CHMOD_DELAY_MS")
if _delay:
    _real_chmod = os.chmod
    _delay_s = int(_delay) / 1000

    def _slow_chmod(*args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("dir_fd") is not None:
            time.sleep(_delay_s)
        return _real_chmod(*args, **kwargs)

    os.chmod = _slow_chmod  # type: ignore[assignment]

_rotate_dir = os.environ.get("ADDTEST_ROTATE_BARRIER")
if _rotate_dir:
    from memoctl import logfile

    _dir: str = _rotate_dir
    _take = logfile._take_lock
    _want = int(os.environ.get("ADDTEST_ROTATE_COUNT", "2"))

    def _barrier_take(state):  # type: ignore[no-untyped-def]
        with open(os.path.join(_dir, str(os.getpid())), "w"):
            pass
        end = time.monotonic() + 30
        while len(os.listdir(_dir)) < _want and time.monotonic() < end:
            time.sleep(0.001)
        return _take(state)

    logfile._take_lock = _barrier_take  # type: ignore[assignment]

if "ADDTEST_UMASK" in os.environ:
    os.umask(int(os.environ["ADDTEST_UMASK"], 8))

_sync = os.environ.get("ADDTEST_SYNC")
if _sync:
    with open(os.path.join(_sync, str(os.getpid())), "w"):
        pass
    _end = time.monotonic() + 60
    while not os.path.exists(os.path.join(_sync, "go")) and time.monotonic() < _end:
        time.sleep(0.001)

if os.environ.get("ADDTEST_STATEDIR_ONLY"):
    from memoctl.gateway import RealGateway
    from memoctl.statedir import open_state_dir

    _state = open_state_dir(RealGateway(), create=True)
    _state.close()
    sys.exit(0)
sys.exit(main(sys.argv[2:]))
