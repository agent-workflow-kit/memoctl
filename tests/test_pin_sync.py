"""scripts/check-pin-sync.sh: the Makefile's check-jsonschema pin follows awkit-ci's."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from tests.child_env import child_path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-pin-sync.sh"
PIN = "check-jsonschema==0.38.2"
TOOLS = ["bash", "sed", "head", "grep", "sort", "dirname", "env"]  # what the script needs


def _run(tmp_path: Path, path: str, pin: str = PIN) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": path, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}
    return subprocess.run(
        [str(SCRIPT), pin], env=env, capture_output=True, text=True, timeout=30, check=False
    )


def _fake_gh(tmp_path: Path, body: str) -> str:
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    gh = fake / "gh"
    gh.write_text(f"#!/bin/sh\n{body}\n")
    gh.chmod(0o755)
    return str(fake) + os.pathsep + child_path(tmp_path, tools=TOOLS)


def test_a_matching_pin_passes(tmp_path: Path) -> None:
    path = _fake_gh(tmp_path, f"echo '  CHECK_JSONSCHEMA: {PIN}'")
    r = _run(tmp_path, path)
    assert r.returncode == 0, r.stderr
    assert "matches" in r.stdout


def test_a_different_pin_fails(tmp_path: Path) -> None:
    path = _fake_gh(tmp_path, "echo '  CHECK_JSONSCHEMA: check-jsonschema==0.99.0'")
    r = _run(tmp_path, path)
    assert r.returncode == 1
    assert "0.99.0" in r.stderr


def test_the_tag_comes_from_the_ci_workflow(tmp_path: Path) -> None:
    path = _fake_gh(tmp_path, f'case "$*" in *ref=v0.1.0*) echo "{PIN}";; *) exit 1;; esac')
    assert _run(tmp_path, path).returncode == 0


def test_an_offline_gh_is_a_notice_not_a_failure(tmp_path: Path) -> None:
    r = _run(tmp_path, _fake_gh(tmp_path, "exit 1"))
    assert r.returncode == 0
    assert "notice" in r.stdout


def test_a_missing_gh_is_a_notice_not_a_failure(tmp_path: Path) -> None:
    path = child_path(tmp_path, tools=TOOLS)  # no gh on it, whatever the machine has
    assert shutil.which("gh", path=path) is None
    r = _run(tmp_path, path)
    assert r.returncode == 0
    assert "gh not found" in r.stdout
    assert os.access(SCRIPT, os.X_OK)
