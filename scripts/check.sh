#!/usr/bin/env bash
# CPU-only checks. Run the worker suites in their separate documented SDK environments;
# installing the full Runpod SDK into the app environment changes FastAPI dependencies.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
CHECK_PYTHON="${PULSEFEED_PYTHON:-python3}"
export PYTHONPATH="$ROOT:$ROOT/app${PYTHONPATH:+:$PYTHONPATH}"
case "${1:-app}" in
  app) exec "$CHECK_PYTHON" -m pytest -q app/tests ;;
  integration)
    # Evidence can contain downloaded upstream source trees and candidate copies.
    # Exercise our tests, not the third-party tests archived alongside their results.
    exec "$CHECK_PYTHON" -m pytest -q --ignore=integration/artifacts integration local
    ;;
  qwen) exec "$CHECK_PYTHON" -m unittest discover -s workers/qwen/tests -q ;;
  flux) exec "$CHECK_PYTHON" -m pytest -q workers/flux/tests ;;
  ui)
    for suite in integration/*.test.cjs; do node "$suite"; done
    ;;
  *) printf '%s\n' 'Usage: scripts/check.sh {app|integration|qwen|flux|ui}' >&2; exit 2 ;;
esac
