# Contributing

Read [CLAUDE.md](CLAUDE.md) first; the rules there apply to every change.

## Before every commit

Run `make verify` (ruff, format check, pyright, pytest and markdown lint). It is offline.

## Before every push

Run `make schemas`. It is the CI `schemas` job: `check-jsonschema --check-metaschema` at CI's pinned version over every file below `schemas/`, plus `config.example.yaml` against `schemas/config.json`. It needs network access (it runs through `uvx`), so it is not part of `make verify`. The `$schema` declaration, the `$id`, the offline `$ref` resolution and the `regress` compilation of every pattern are also covered by `tests/test_schema_check.py`, which `make verify` runs.

## Tests: schemas and golden files

- Validate every `--json` output with `tests/schema_check.py`: `validate("cli/add", document)` for a parsed document, or `validate_json_output("cli/add", raw_stdout)` for raw bytes (strict UTF-8 JSON, no duplicate keys or NaN). A failure lists every violation with its path in the document.
- Compare outputs with `check_golden(name, actual_bytes)` from `tests/golden_files.py`. Goldens live at `tests/golden/<area>/<case>.<ext>` (`json` for `--json` stdout, `txt` for human output, `bin` otherwise; lowercase letters, digits, `_` and `-`). A `.json` golden must be given its schema (`check_golden(..., schema="cli/add")`), so output cannot be compared without being validated. Comparison is bytes-exact.
- A missing or different golden fails. To regenerate, run `make golden-update` (it sets `MEMOCTL_UPDATE_GOLDEN=1`), then review `git diff tests/golden` line by line. Regeneration is refused when a CI variable (`CI`, `GITHUB_ACTIONS`, ...) is set and never writes outside `tests/golden`.
- Keep goldens deterministic: pass the output through `normalize()` to replace paths, timestamps and process ids. The harness refuses a golden that contains the home or temporary directory.
