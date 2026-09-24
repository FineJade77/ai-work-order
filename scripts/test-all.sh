#!/usr/bin/env bash
# Runs every service's unit and integration tests, each in its own virtualenv (services share no code).
# Integration tests need the stack running (`docker compose up -d --build`); without it they skip.
#
#   scripts/test-all.sh                  all services
#   scripts/test-all.sh app-service      just the ones named
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVICES=("$@")
[ ${#SERVICES[@]} -eq 0 ] && SERVICES=(knowledge-service ticket-service tool-gateway ai-gateway agent-runtime app-service)

setup_venv() {
  local dir="$1"
  if command -v uv >/dev/null; then
    [ -x "$dir/.venv/bin/python" ] || uv venv -q -p 3.11 "$dir/.venv"
    VIRTUAL_ENV="$dir/.venv" uv pip install -q -e "${dir}[test]"
  else
    [ -x "$dir/.venv/bin/python" ] || python3.11 -m venv "$dir/.venv"
    "$dir/.venv/bin/pip" install -q -e "${dir}[test]"
  fi
}

failed=()
for svc in "${SERVICES[@]}"; do
  dir="$ROOT/services/$svc"
  echo "==> $svc"
  setup_venv "$dir"
  (cd "$dir" && .venv/bin/python -m pytest -q -rs) || failed+=("$svc")
done

if [ ${#failed[@]} -gt 0 ]; then
  echo "FAILED: ${failed[*]}" >&2
  exit 1
fi
echo "all passed: ${SERVICES[*]}"
