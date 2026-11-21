.PHONY: verify test lint fmt fmt-check typecheck mdlint schemas golden-update

verify: lint fmt-check typecheck test mdlint

test:
	uv run pytest

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

# The CI `schemas` job, run locally at CI's pinned version (needs network for uvx). Keep
# CHECK_JSONSCHEMA in step with agent-workflow-kit/awkit-ci's python-ci.yml. Not part of
# `verify`: run it before every push.
CHECK_JSONSCHEMA := check-jsonschema==0.38.2
SCHEMA_FILES := $(shell find schemas -name '*.json' | LC_ALL=C sort)

schemas:
	uvx --from $(CHECK_JSONSCHEMA) check-jsonschema --check-metaschema $(SCHEMA_FILES)
	uvx --from $(CHECK_JSONSCHEMA) check-jsonschema --schemafile schemas/config.json config.example.yaml

# Regenerate the golden files (never automatic, refused when CI is set); review `git diff tests/golden`.
golden-update:
	MEMOCTL_UPDATE_GOLDEN=1 uv run pytest
