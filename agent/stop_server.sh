#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="$(ROOT_DIR="${ROOT_DIR}" python - <<'PY'
import json
import os
from pathlib import Path
root = Path(os.environ["ROOT_DIR"])
runtime = json.loads((root / "configs" / "runtime.json").read_text())
print(runtime.get("agent_dir", str(root)))
PY
)"
PIDFILE="${AGENT_DIR}/server_logs/server.pid"

if [ -f "${PIDFILE}" ]; then
  kill "$(cat "${PIDFILE}")" 2>/dev/null || true
  rm -f "${PIDFILE}"
fi
