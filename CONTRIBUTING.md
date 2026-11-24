# Contributing

Read [CLAUDE.md](CLAUDE.md) first; the rules there apply to every change.

## Before every commit

Run `make verify` (ruff, format check, pyright, pytest and markdown lint). It is offline.

## Before every push

Run `make schemas`. It replicates two steps of the CI `schemas` job at CI's pinned version: `check-jsonschema --check-metaschema` over every file below `schemas/`, and `config.example.yaml` against `schemas/config.json`. It first compares its `check-jsonschema` pin with the one in awkit-ci's `python-ci.yml` at the tag `.github/workflows/ci.yml` uses and fails when they differ (it only prints a notice when `gh` is missing or offline). It needs network access (it runs through `uvx`), so it is not part of `make verify`. It does not run CI's `$schema` check (every schema declares draft 2020-12): `make verify` enforces that in `tests/test_schema_check.py::test_schema_is_draft_2020_12_and_valid`, together with the `$id`, the offline `$ref` resolution and the `regress` compilation of every pattern.

## Tests: schemas and golden files

- Validate every `--json` output with `tests/schema_check.py`: `validate("cli/add", document)` for a parsed document, or `validate_json_output("cli/add", raw_stdout)` for raw bytes (strict UTF-8 JSON, no duplicate keys or NaN, one line ending in a single LF, as `docs/interface.md` requires of the output). Patterns are matched with `regress` (check-jsonschema's engine, `u` flag), so `$` does not accept a trailing newline. A failure lists every violation with its path in the document.
- Compare outputs with `check_golden(name, actual_bytes)` from `tests/golden_files.py`. Goldens live at `tests/golden/<area>/<case>.<ext>` (`json` for `--json` stdout, `txt` for human output, `bin` otherwise; lowercase letters, digits, `_` and `-`). A `.json` golden must be given its schema (`check_golden(..., schema="cli/add")`), so output cannot be compared without being validated. Comparison is bytes-exact (`.gitattributes` marks `tests/golden/**` as `-text` so git never converts line endings). A `.txt` or `.bin` golden whose bytes are one JSON object or array is refused: JSON output needs a `.json` golden and a schema, and the schema must describe a document (not `defs`).
- A missing or different golden fails. To regenerate, run `make golden-update` (it sets `MEMOCTL_UPDATE_GOLDEN=1`; narrow it with `make golden-update PYTEST_ARGS="tests/test_x.py -k case"`), then review `git diff tests/golden` line by line. Regeneration warns for each golden it rewrites, is refused when a CI variable (`CI`, `GITHUB_ACTIONS`, `JENKINS_URL`, ...) is set and never writes outside `tests/golden`.
- Keep goldens deterministic: pass the output through `normalize()` to replace paths, timestamps and process ids. The harness refuses a golden that contains the home, temporary or repository directory as a path prefix; a longer name that merely starts with one (`/tmpfoo`, `/help` for a home of `/h`) is not a leak.

## Tests: the real home

- The tool takes its home from the password database, not from `HOME`, so a private `HOME` does not protect `~/.local/state/memoctl`. An autouse fixture in `tests/conftest.py` makes `pwd.getpwuid` and `pwd.getpwnam` raise `AssertionError` when a test reads the real `pw_dir` (other fields still work). This works only because code reads `pwd.getpwuid` as a module attribute; a `from pwd import getpwuid` bypasses it (the session backstop still catches the write afterwards). A test that only compares the real value takes `@pytest.mark.real_passwd`.
- A test that runs the real entry point in a child process builds its command with `tests/private_home.py` (`argv(home, *args)`): a wrapper gives `pwd.getpwuid` the private `pw_dir`, then runs `python -m memoctl`. Production code is unchanged.
- As a backstop the session fails (exit status 1) when the real `~/.local/state/memoctl` or `~/.config/memoctl` was absent when it started and exists when it finished. Never run a test or probe without these guards.

## Tests: the real crontab

- Rule: no test may start a process, directly or through a child, whose `PATH` can resolve `crontab` to a real one. The in-process guard cannot see a lookup that happens inside a child (`memoctl cron` in a child process runs `crontab` itself), so the rule is enforced on the child's environment, before it starts.
- Incident class: a process test built its child environment with `PATH=/usr/bin:/bin`, where the real setgid `crontab` lives. The child, not covered by the guard around the test process, could run the real `crontab -l` and `crontab -`, and install a managed block into the user's real crontab.
- Guard: `tests/conftest.py` computes the effective `PATH` of every process a test starts (the `env=` given, or `os.environ` when none; an empty or relative entry means the child's working directory) in `subprocess.Popen`, `subprocess._fork_exec` and the `posix_spawn` paths `subprocess` takes, and raises `CrontabGuardError` before exec unless `shutil.which("crontab", path=...)` finds nothing or a fake below the pytest base temporary directory. It does not look at the program being started, so it covers every child, and it has no switch to turn it off. A session tripwire (a `crontab` that exits 97) is first on `PATH` for the whole run, and each test adds its own.
- Helper: build the `PATH` of every child with `tests/child_env.py` (`child_path(root, fake_bin=..., tools=[...])`): the fake-bin directory (when the test has a fake `crontab`), an empty directory and, for a child that needs a system program such as `sh`, a directory with a link to just that program. It asserts that `crontab` resolves to the fake (or to nothing). Never write `/usr/bin:/bin` into a test environment.
- A test that uses `RealGateway.crontab_*` passes `crontab_bin=` as an explicit path to a fake below the tmp directory (`fake_crontab`), or expects the guard to refuse the default `crontab`; the guard refuses a bare `crontab` program in-process.
