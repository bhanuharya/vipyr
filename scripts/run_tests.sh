#!/bin/bash

set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Prefer the project virtualenv created by the README setup steps; fall
# back to whatever interpreter is already on PATH when no venv exists.
PYTHON=python
if [ -x .venv/bin/python ]; then
    PYTHON=.venv/bin/python
fi

for f in ./tests/*.py; do
    echo $f
    "$PYTHON" main.py $f -b64
    clang out.ll readline.c runtime.c -Wno-override-module
    ./a.out
done
