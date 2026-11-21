"""Golden-file harness: bytes-exact comparison with explicit, CI-proof regeneration.

Convention: ``tests/golden/<area>/<case>.<ext>`` where ``area`` is a command or topic
(``add``, ``context``, ``errors``) and ``case`` names the scenario; each part matches
``[a-z0-9][a-z0-9_-]*``. Extensions: ``json`` (stdout of a ``--json`` run, validated against a
schema), ``txt`` (human output) and ``bin`` (anything else). Files are compared as raw bytes, so
CRLF, trailing newlines, NUL and non-UTF-8 bytes all count.

Regeneration happens only when ``MEMOCTL_UPDATE_GOLDEN=1`` is set, never when a CI variable
is set, and writes only below ``tests/golden``. The diff of the regenerated files is the review.
A JSON golden is validated before it is compared or written, so the helper cannot pass
unvalidated ``--json`` output: ``check_golden`` refuses a ``.json`` name without a schema.
"""

from __future__ import annotations

import difflib
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

from tests.schema_check import validate_json_output

GOLDEN_ROOT = Path(__file__).resolve().parent / "golden"
UPDATE_ENV = "MEMOCTL_UPDATE_GOLDEN"
CI_VARIABLES = ("CI", "GITHUB_ACTIONS", "BUILD_NUMBER", "TF_BUILD", "BUILDKITE", "GITLAB_CI")
_PART = r"[a-z0-9][a-z0-9_-]*"
NAME_RE = re.compile(rf"{_PART}(/{_PART})*\.(json|txt|bin)")


class GoldenError(AssertionError):
    pass


def golden_path(name: str, root: Path = GOLDEN_ROOT) -> Path:
    """The file of golden ``name``; refuses a name that breaks the convention or leaves ``root``."""
    if not NAME_RE.fullmatch(name) or "/" not in name:
        raise GoldenError(f"bad golden name {name!r}: expected <area>/<case>.(json|txt|bin)")
    real_root = root.resolve()
    path = (real_root / name).resolve()
    if real_root not in path.parents:
        raise GoldenError(f"golden {name!r} resolves outside {real_root}")
    return path


def _in_ci(environ: Mapping[str, str]) -> bool:
    return any(environ.get(v) for v in CI_VARIABLES)


def _volatile_markers() -> list[str]:
    markers = ["pytest-of-", tempfile.gettempdir(), str(Path(__file__).resolve().parent.parent)]
    home = str(Path.home())
    if home != "/":
        markers.append(home)
    return [m for m in markers if m and m != "/"]


def normalize(
    data: bytes, *, paths: Mapping[str, str] | None = None, pids: Iterable[int] = ()
) -> bytes:
    """Replace values that differ between runs: ``paths`` (longest first), ISO timestamps with
    ``<TS>`` and each of ``pids`` (as a whole number) with ``<PID>``."""
    for src in sorted((p for p in (paths or {}) if p), key=len, reverse=True):
        data = data.replace(os.fsencode(src), os.fsencode(paths[src]))  # type: ignore[index]
    data = re.sub(rb"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", b"<TS>", data)
    for pid in pids:
        data = re.sub(rb"(?<![0-9])" + str(pid).encode() + rb"(?![0-9])", b"<PID>", data)
    return data


def _diff(name: str, golden: bytes, actual: bytes) -> str:
    def lines(b: bytes) -> list[str]:
        return [repr(x) + "\n" for x in b.splitlines(keepends=True)] or ["<empty>\n"]

    diff = difflib.unified_diff(lines(golden), lines(actual), f"golden/{name}", "actual", n=2)
    return "".join(diff) or "(same lines; the bytes differ only at the end)\n"


def check_golden(
    name: str,
    actual: bytes,
    *,
    schema: str | None = None,
    root: Path = GOLDEN_ROOT,
    environ: Mapping[str, str] | None = None,
) -> None:
    if not isinstance(actual, bytes):
        raise TypeError("golden output must be bytes")
    env = os.environ if environ is None else environ
    path = golden_path(name, root)
    is_json = name.endswith(".json")
    if is_json and schema is None:
        raise GoldenError(f"{name}: a JSON golden needs the schema it is validated against")
    if schema is not None and not is_json:
        raise GoldenError(f"{name}: a schema applies only to a .json golden")
    if schema is not None:
        validate_json_output(schema, actual)
    for marker in _volatile_markers():
        if os.fsencode(marker) in actual:
            raise GoldenError(f"{name}: volatile value {marker!r} in the output; normalize() it")

    if env.get(UPDATE_ENV) == "1":
        if _in_ci(env):
            raise GoldenError(f"{name}: regeneration is refused in CI")
        _write(path, actual)
        return
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise GoldenError(f"{name}: the golden is a symlink or not a regular file")
    if not path.exists():
        raise GoldenError(f"{name}: missing golden; create it with {UPDATE_ENV}=1 and review it")
    golden = path.read_bytes()
    if golden != actual:
        raise GoldenError(
            f"{name}: output differs from the golden (lines shown as bytes literals)\n"
            f"{_diff(name, golden, actual)}"
            f"If the change is intended, regenerate with {UPDATE_ENV}=1 and review the diff."
        )


def _write(path: Path, data: bytes) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.resolve() != parent or path.is_symlink():
        raise GoldenError(f"{path}: refusing to write through a symlink")
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".golden-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
