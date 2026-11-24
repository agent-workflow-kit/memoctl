"""Child entry point of the kill tests: ``wrapper.py <home> <memoctl args...>``.

Like ``private_home_wrapper.py`` (the home comes from a replaced password entry) and, in
addition, it stops ``delete`` inside its write transaction: after the notes are deleted and
before the commit it prints ``READY`` on stdout and sleeps, so the parent can kill it exactly
there. Nothing of the production code is changed on disk.
"""

from __future__ import annotations

import os
import pwd
import sys
import time

HOME = sys.argv[1]
_real_uid = pwd.getpwuid
_OWN = _real_uid(os.getuid()).pw_name


def _private(r: pwd.struct_passwd) -> pwd.struct_passwd:
    return pwd.struct_passwd(
        (r.pw_name, r.pw_passwd, r.pw_uid, r.pw_gid, r.pw_gecos, HOME, r.pw_shell)
    )


pwd.getpwuid = lambda uid: _private(_real_uid(uid))  # type: ignore[assignment]
_real_nam = pwd.getpwnam
pwd.getpwnam = lambda name: _private(_real_nam(name)) if name == _OWN else _real_nam(name)  # type: ignore[assignment]

from memoctl import cmd_delete  # noqa: E402
from memoctl.cli import main  # noqa: E402

_apply = cmd_delete.apply_delete


def hold(store: object, plan: object) -> None:
    _apply(store, plan)  # type: ignore[arg-type]
    os.write(1, b"READY\n")
    time.sleep(120)


cmd_delete.apply_delete = hold  # type: ignore[assignment]
sys.exit(main(sys.argv[2:]))
