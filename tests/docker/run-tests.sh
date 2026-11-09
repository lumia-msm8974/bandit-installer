#!/usr/bin/env bash
# Runs the whole test suite in a Debian container with python3-hivex.
#   tests/docker/run-tests.sh [unittest arguments]
# LUMIA_TEST_BCD=<phone BCD> also runs the boot chain tests (the file is mounted
# read-only, never copied into the repository).
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")" && pwd)
repo=$(cd -- "$here/../.." && pwd)
docker build -q -t lumia-installer-tests "$here" >/dev/null
args=(-v "$repo:/src:ro" -e PYTHONDONTWRITEBYTECODE=1)
if [ -n "${LUMIA_TEST_BCD:-}" ]; then
    args+=(-v "$(realpath "$LUMIA_TEST_BCD"):/bcd/BCD:ro" -e LUMIA_TEST_BCD=/bcd/BCD)
fi
exec docker run --rm "${args[@]}" --name "lumia-installer-tests-$$" lumia-installer-tests \
    python3 -m unittest discover -s tests "$@"
