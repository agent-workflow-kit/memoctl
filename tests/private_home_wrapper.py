"""Child entry point of ``tests/private_home.py``: ``wrapper.py <home> <memoctl args...>``."""

from __future__ import annotations

import pwd
import runpy
import sys

HOME = sys.argv[1]
_real = pwd.getpwuid


def _private(uid: int) -> pwd.struct_passwd:
    r = _real(uid)
    return pwd.struct_passwd(
        (r.pw_name, r.pw_passwd, r.pw_uid, r.pw_gid, r.pw_gecos, HOME, r.pw_shell)
    )


pwd.getpwuid = _private
sys.argv = ["memoctl", *sys.argv[2:]]
runpy.run_module("memoctl", run_name="__main__", alter_sys=True)
