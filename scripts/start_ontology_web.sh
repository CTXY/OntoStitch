#!/usr/bin/env bash
set -euo pipefail
ONTOLOGY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORTAL_ROOT="${1:?Usage: scripts/start_ontology_web.sh /path/to/DataMosaic [port]}"
ONTOLOGY_PORT="${2:-8011}"
if [[ "$(uname -m)" == "arm64" && -x /opt/homebrew/bin/node ]]; then
  export PATH="/opt/homebrew/bin:$PATH"
fi
ONTOLOGY_PYTHON="${ONTOLOGY_PYTHON:-$ONTOLOGY_ROOT/.venv/bin/python}"
if [[ ! -f "$PORTAL_ROOT/web/package.json" ]]; then
  echo "Portal web/package.json not found: $PORTAL_ROOT" >&2
  exit 1
fi
if [[ ! -d "$PORTAL_ROOT/web/node_modules" ]]; then
  npm --prefix "$PORTAL_ROOT/web" ci
fi
npm --prefix "$PORTAL_ROOT/web" run build
cd "$ONTOLOGY_ROOT"
export CORTEX_PROJECT_ROOT="$ONTOLOGY_ROOT"
export PYTHONPATH="$ONTOLOGY_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export CORTEX_WEB_FRONTEND_DIST="$PORTAL_ROOT/web/dist"
exec "$ONTOLOGY_PYTHON" -m uvicorn cortex.web.app:app --host 127.0.0.1 --port "$ONTOLOGY_PORT"
