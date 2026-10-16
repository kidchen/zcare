#!/usr/bin/env bash
# Enables the repo's git hooks (.githooks/pre-commit: privacy check + ruff). Run once per clone.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath .githooks
echo "pre-commit hook enabled (privacy check + ruff)"
[ -f .privacy-patterns ] || echo "note: create .privacy-patterns (git-ignored) with one regex per line — e.g. family names, your neighbourhood"
