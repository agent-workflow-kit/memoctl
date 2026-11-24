"""Child entry point of ``tests/private_home.py``: ``wrapper.py <home> <memoctl args...>``."""

from __future__ import annotations

import os
import pwd
import runpy
import sys

HOME = sys.argv[1]
_real_uid = pwd.getpwuid
_real_nam = pwd.getpwnam
_OWN_NAME = _real_uid(os.getuid()).pw_name


def _private(r: pwd.struct_passwd) -> pwd.struct_passwd:
    return pwd.struct_passwd(
        (r.pw_name, r.pw_passwd, r.pw_uid, r.pw_gid, r.pw_gecos, HOME, r.pw_shell)
    )


def _getpwuid(uid: int) -> pwd.struct_passwd:
    return _private(_real_uid(uid))


def _getpwnam(name: str) -> pwd.struct_passwd:
    r = _real_nam(name)
    return _private(r) if name == _OWN_NAME else r


pwd.getpwuid = _getpwuid
pwd.getpwnam = _getpwnam
sys.argv = ["memoctl", *sys.argv[2:]]
runpy.run_module("memoctl", run_name="__main__", alter_sys=True)
