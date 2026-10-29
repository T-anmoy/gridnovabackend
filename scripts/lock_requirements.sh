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
# The image installs requirements-nemo.lock: the runtime lock exactly (constrained to it, so
# the shipped versions are the ones the suite ran against) plus NeMo Guardrails' own packages.
uv pip compile requirements-nemo.txt --universal --python-version 3.12 --generate-hashes \
  --no-header --output-file requirements-nemo.lock -c requirements.lock "$@"
echo "Wrote requirements.lock, requirements-dev.lock and requirements-nemo.lock"
