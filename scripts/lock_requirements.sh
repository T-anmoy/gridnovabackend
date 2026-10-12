#!/bin/sh
# Regenerate the hash-pinned lockfiles from requirements*.txt (needs uv:
# https://docs.astral.sh/uv/). The lockfiles target Python 3.12 — the Docker image's
# and CI's interpreter — on every platform (--universal), so the same file installs on
# Linux (image, CI) and macOS (laptops).
#
# An existing lockfile's pins are kept unless a requirements*.txt change forces a new
# version; pass --upgrade to move every transitive package to its newest allowed version.
set -eu
cd "$(dirname "$0")/.."
for name in requirements requirements-dev; do
  uv pip compile "$name.txt" --universal --python-version 3.12 --generate-hashes \
    --no-header --output-file "$name.lock" "$@"
done
echo "Wrote requirements.lock and requirements-dev.lock"
