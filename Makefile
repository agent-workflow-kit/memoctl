PYTEST_ARGS ?=

.PHONY: verify perf test lint fmt fmt-check typecheck mdlint schemas golden-update

verify: lint fmt-check typecheck test mdlint

test:
	uv run pytest

# The heavy flood and performance fixtures (tests marked `perf`): about 15 s, up to about 70 MB of
# scratch under pytest's temporary directory, never part of `verify` or CI. It prints a run
# report ([perf] lines) for each; read the numbers with `-s`.
perf:
	uv run pytest -m perf -s tests/test_context_flood.py $(PYTEST_ARGS)

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

typecheck:
	uv run pyright

mdlint:
	uv run pymarkdown --config .pymarkdown.json scan -r --respect-gitignore .

# Replicates two steps of the CI `schemas` job at CI's pinned version (needs network for uvx):
# `check-jsonschema --check-metaschema` over every file below `schemas/` and `config.example.yaml`
# against `schemas/config.json`. It does not run CI's `$schema` == draft 2020-12 jq check; `make
# verify` enforces that (tests/test_schema_check.py::test_schema_is_draft_2020_12_and_valid).
# Keep CHECK_JSONSCHEMA in step with agent-workflow-kit/awkit-ci's python-ci.yml at the tag
# .github/workflows/ci.yml uses (v0.1.0); the first step below fails when they differ (it is
# skipped with a notice when `gh` is missing or offline). Not part of `verify`: run it before
# every push.
CHECK_JSONSCHEMA := check-jsonschema==0.38.2
SCHEMA_FILES := $(shell find schemas -name '*.json' | LC_ALL=C sort)

schemas:
	scripts/check-pin-sync.sh $(CHECK_JSONSCHEMA)
	uvx --from $(CHECK_JSONSCHEMA) check-jsonschema --check-metaschema $(SCHEMA_FILES)
	uvx --from $(CHECK_JSONSCHEMA) check-jsonschema --schemafile schemas/config.json config.example.yaml

# Regenerate the golden files (never automatic, refused when CI is set); review `git diff tests/golden`.
# Narrow it with PYTEST_ARGS="tests/test_x.py -k case".
golden-update:
	MEMOCTL_UPDATE_GOLDEN=1 uv run pytest $(PYTEST_ARGS)
