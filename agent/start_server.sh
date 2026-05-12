#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="${ROOT_DIR}/configs/runtime.json"
AGENT_DIR="$(ROOT_DIR="${ROOT_DIR}" python - <<'PY'
import json
import os
from pathlib import Path
root = Path(os.environ["ROOT_DIR"])
runtime = json.loads((root / "configs" / "runtime.json").read_text())
print(runtime.get("agent_dir", str(root)))
PY
)"

CONDA_ENV="$(python -c "import json; print(json.load(open('${RUNTIME}'))['conda_env'])")"
CONDA_EXE="$(python -c "import json; print(json.load(open('${RUNTIME}')).get('conda_executable', 'conda'))")"
CONDA_SH="$(python -c "import json; print(json.load(open('${RUNTIME}')).get('conda_sh', ''))")"
MODEL="$(python -c "import json; print(json.load(open('${RUNTIME}'))['model'])")"
HOST="$(python -c "import json; print(json.load(open('${RUNTIME}'))['server_host'])")"
PORT="$(python -c "import json; print(json.load(open('${RUNTIME}'))['server_port'])")"
TP="$(python -c "import json; print(json.load(open('${RUNTIME}'))['server_flags']['tensor_parallel_size'])")"
PP="$(python -c "import json; print(json.load(open('${RUNTIME}'))['server_flags']['pipeline_parallel_size'])")"
MAX_SEQS="$(python -c "import json; print(json.load(open('${RUNTIME}'))['server_flags']['max_num_seqs'])")"
MAX_TOKENS="$(python -c "import json; print(json.load(open('${RUNTIME}'))['server_flags']['max_num_batched_tokens'])")"

mkdir -p "${AGENT_DIR}/server_logs" "${AGENT_DIR}/larry_configs" "${AGENT_DIR}/larry_results"
LARRY_CFG="${AGENT_DIR}/larry_configs/active.json"
if [ ! -f "${LARRY_CFG}" ]; then
  cp "${ROOT_DIR}/larry_configs/config_default.json" "${LARRY_CFG}"
fi

LOG="${AGENT_DIR}/server_logs/server_$(date +%Y%m%d_%H%M%S).log"
export VLLM_USE_LARRY="${VLLM_USE_LARRY:-1}"
export LARRY_CONFIG_PATH="${LARRY_CFG}"
export LARRY_RELOAD_EVERY_STEPS="${LARRY_RELOAD_EVERY_STEPS:-20}"

env | grep -E '^(VLLM_USE_LARRY|LARRY_)' >> "${LOG}" || true

LAUNCH_SCRIPT="${AGENT_DIR}/server_logs/launch_server.sh"
cat > "${LAUNCH_SCRIPT}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export VLLM_USE_LARRY="${VLLM_USE_LARRY}"
export LARRY_CONFIG_PATH="${LARRY_CONFIG_PATH}"
export LARRY_RELOAD_EVERY_STEPS="${LARRY_RELOAD_EVERY_STEPS}"
if [ -n "${CONDA_SH}" ] && [ -f "${CONDA_SH}" ]; then
  source "${CONDA_SH}"
  conda activate "${CONDA_ENV}"
else
  export PATH="$(dirname "${CONDA_EXE}"):\${PATH}"
fi
exec vllm serve "${MODEL}" \
    --host "${HOST}" --port "${PORT}" \
    --tensor-parallel-size "${TP}" \
    --pipeline-parallel-size "${PP}" \
    --max-num-seqs "${MAX_SEQS}" \
    --max-num-batched-tokens "${MAX_TOKENS}" \
    --enable-prefix-caching
EOF
chmod +x "${LAUNCH_SCRIPT}"

setsid "${LAUNCH_SCRIPT}" >> "${LOG}" 2>&1 < /dev/null &

echo $! > "${AGENT_DIR}/server_logs/server.pid"
echo "Server PID $(cat "${AGENT_DIR}/server_logs/server.pid"), logging to ${LOG}"
