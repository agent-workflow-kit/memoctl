#!/usr/bin/env bash
# Fail when the check-jsonschema pin in the Makefile differs from the one in awkit-ci's
# python-ci.yml at the tag this repository's .github/workflows/ci.yml uses. Best effort: with no
# `gh` or no network it prints a notice and succeeds.
# Usage: scripts/check-pin-sync.sh check-jsonschema==X.Y.Z
set -u
pin=${1:?usage: check-pin-sync.sh check-jsonschema==X.Y.Z}
cd "$(dirname "$0")/.." || exit 1

tag=$(sed -n 's|.*awkit-ci/\.github/workflows/python-ci\.yml@\([^ ]*\).*|\1|p' .github/workflows/ci.yml | head -n 1)
if [ -z "$tag" ]; then
	echo "error: no awkit-ci python-ci.yml tag found in .github/workflows/ci.yml" >&2
	exit 1
fi
if ! command -v gh >/dev/null 2>&1; then
	echo "notice: gh not found; skipped the check-jsonschema pin sync check against awkit-ci@$tag"
	exit 0
fi
if ! upstream=$(gh api -H "Accept: application/vnd.github.raw" \
	"repos/agent-workflow-kit/awkit-ci/contents/.github/workflows/python-ci.yml?ref=$tag" 2>/dev/null); then
	echo "notice: could not fetch awkit-ci@$tag (offline or not signed in); skipped the pin sync check"
	exit 0
fi
theirs=$(printf '%s\n' "$upstream" | grep -o 'check-jsonschema==[0-9][0-9A-Za-z.]*' | sort -u)
if [ "$theirs" != "$pin" ]; then
	echo "error: awkit-ci@$tag pins '${theirs:-nothing}' but the Makefile pins '$pin'; update CHECK_JSONSCHEMA" >&2
	exit 1
fi
echo "check-jsonschema pin matches awkit-ci@$tag ($pin)"
