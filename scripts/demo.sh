#!/usr/bin/env bash
# One-command demo: replay twin + dashboard seeded with the deliberately-bad run (gate FAIL).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -e .
fi
exec .venv/bin/rehearsal demo --port "${PORT:-8765}" --fixture "${FIXTURE:-fixtures/replay/demo}" --speed "${SPEED:-10}"
