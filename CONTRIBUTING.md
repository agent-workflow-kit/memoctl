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
