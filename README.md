# LARRYSmith Agentic Auto-Tuning System

This project implements an agentic tuning harness for LARRY v6, a priority-based
vLLM scheduling policy. The system separates three concerns:

- Scheduler parameters: JSON files consumed by the vLLM LARRY hook.
- Operator objective: JSON-defined metric weights, directions, rate weights, and hard constraints.
- Agent orchestration: Python tools that call GPT for proposal/analysis and deterministic scripts for experiments.

The core loop is:

```text
main.py
  -> load runtime/objective/LLM config
  -> establish FCFS baseline
  -> ask GPT for an exploit/explore search plan
  -> build fixed-role candidate LarryConfig values locally
  -> write each candidate to larry_configs/active.json
  -> run vLLM benchmark CLI
  -> parse metrics
  -> score vs FCFS using objective.json
  -> ask GPT to analyze results
  -> update result.md and continue
```

GPT does not directly control the scheduler or parse metrics. For optimization,
it analyzes the measured results and chooses which parameters to exploit or
explore next. Local deterministic code converts that search plan into concrete
candidate configurations, clamps values to the search space, deduplicates
candidates, and adds a safety anchor. Experiment execution, metric parsing,
scoring, and file updates are deterministic tools.

## Directory Layout

```text
.
  README.md
  design_doc_vllm.md
  result.md

  configs/
    runtime.json       # environment, model, rates, paths, benchmark/server flags
    objective.json     # operator-defined scoring weights and hard constraints
    llm.json           # GPT API config; api_key may reference an environment variable

  larry_configs/
    active.json        # live config read by the vLLM LARRY hook
    config_default.json
    config_r<N>_c<M>.json
    best_config.json

  larry_results/
    all_runs.csv       # flat experiment table
    *.json             # raw and normalized benchmark outputs

  server_logs/
    server.pid
    server_*.log

  agent/
    main.py            # orchestrator
    common.py          # shared config/path/subprocess helpers
    llm_client.py      # GPT API wrapper
    optimizer_agent.py # GPT-backed search planner plus local candidate builder
    analyzer_agent.py  # GPT-backed round analyzer with deterministic fallback
    run_one.py         # run one candidate across rates with a unique run_id
    parse_metrics.py   # normalize benchmark JSON into canonical metrics
    score.py           # score current metrics vs FCFS baseline using objective.json
    summarize.py       # rebuild result.md from all_runs.csv
    start_server.sh    # launch long-lived vLLM server
    stop_server.sh     # stop server
```

## Configuration Files

### `configs/runtime.json`

Defines fixed runtime details:

- conda environment
- vLLM project path
- model and dataset
- server host/port
- request rates and prompt count
- vLLM server flags

The default `agent_dir` is this repository root. If you want the layout from
`design_doc_vllm.md`, change it to `/workspace/lqs3/LLM_scheduling/LLM_agent`.

### `configs/objective.json`

Defines what the operator considers good. This replaces hard-coded scoring
coefficients.

Each metric has:

- `weight`: contribution to the per-rate score
- `direction`: `lower_better` or `higher_better`

The scoring rule is:

```text
lower_better:  weight * (baseline - current) / baseline
higher_better: weight * (current - baseline) / baseline
```

The file also supports hard constraints, for example:

```json
{
  "p99_ttft_ms": {
    "max_ratio_vs_baseline": 1.10
  }
}
```

This means a candidate is marked constrained/invalid if its p99 TTFT is more
than 10% worse than FCFS, even if its weighted score is high.

### `configs/llm.json`

Configures GPT calls:

```json
{
  "provider": "openai",
  "model": "gpt-4.1",
  "api_key": "${OPENAI_API_KEY}",
  "temperature": 0.2,
  "max_output_tokens": 4096
}
```

`api_key` can be a literal key or an environment reference such as
`${OPENAI_API_KEY}`. The environment variable form is preferred so secrets do not
need to be committed.

## Main Flow

Run a dry planning loop without launching vLLM:

```bash
python -m agent.main --dry-run --rounds 1 --candidates-per-round 3
```

Run the real harness:

```bash
python -m agent.main --rounds 10 --candidates-per-round 6
```

The real run expects:

- editable vLLM installed in the configured conda environment
- LARRY hook patched into vLLM as described in `design_doc_vllm.md`
- `vllm bench serve` available in the configured vLLM environment
- a running or launchable vLLM server
- OpenAI API credentials if GPT-backed search planning is desired

If GPT is unavailable, the optimizer falls back to a deterministic search plan
and still generates the same fixed candidate roles around the current best
configuration.

## Execution Workflow

Use this section as the end-to-end checklist for testing and running the system.

### 1. Dry-run the agent harness

From this repository:

```bash
cd /workspace/lqs3/LLM_scheduling/real_agent

python -m agent.main --dry-run --no-llm --rounds 1 --candidates-per-round 3
```

This verifies the external harness only:

- orchestrator
- search-plan and candidate generation
- `active.json` writes
- metric parsing
- scoring
- `result.md` generation

It does not start vLLM and does not call GPT.

### 2. Configure GPT access

For GPT-backed search planning and analysis:

```bash
export OPENAI_API_KEY="your-key"
```

`configs/llm.json` reads this by default:

```json
"api_key": "${OPENAI_API_KEY}"
```

To run without GPT, pass:

```bash
--no-llm
```

The optimizer will use a deterministic fallback search plan, then generate the
same fixed-role candidates locally.

### 3. Verify the editable vLLM environment

Run this in a shell where `conda` is available:

```bash
/root/miniconda3/bin/conda run -n myvllm python -c "import vllm; print(vllm.__version__); print(vllm.__file__)"
```

The printed path should be under:

```text
/workspace/lqs3/LLM_scheduling/vllm
```

Then verify that LARRY is off by default:

```bash
/root/miniconda3/bin/conda run -n myvllm python -c "from vllm.v1.core.sched.larry_hook import LarryRuntime; print(LarryRuntime.get().enabled)"
```

Expected output:

```text
False
```

### 4. Start the LARRY-enabled vLLM server

```bash
cd /workspace/lqs3/LLM_scheduling/real_agent

bash agent/start_server.sh
```

The script sets:

```text
VLLM_USE_LARRY=1
LARRY_CONFIG_PATH=/workspace/lqs3/LLM_scheduling/real_agent/larry_configs/active.json
LARRY_RELOAD_EVERY_STEPS=20
```

Wait for readiness:

```bash
for i in $(seq 1 120); do
  if curl -fs http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
    echo "server up"
    break
  fi
  sleep 2
done
```

Check that the hook activated:

```bash
grep -R "\[LARRY\]" server_logs/
```

Expected log lines include:

```text
[LARRY] enabled
[LARRY] reload v1
```

### 5. Run one real candidate

For a first smoke test, consider setting `num_prompts` in `configs/runtime.json`
to a smaller value such as `32`. Then run:

```bash
python -m agent.run_one \
  --config-id default \
  --config-json larry_configs/config_default.json \
  --rates inf \
  --round-id 0 \
  --num-prompts 2
```

This will:

- copy the candidate into `larry_configs/active.json`
- wait briefly for hot reload
- run `vllm bench serve`
- write raw benchmark JSON under `larry_results/`
- append normalized rows to `larry_results/all_runs.csv`
- compute score fields using `configs/objective.json`

Each invocation gets a unique `run_id`, for example:

```text
20260511T153012Z_default_1a2b3c4d
```

Raw result files include that `run_id`:

```text
larry_results/20260511T153012Z_default_1a2b3c4d_rateinf.json
```

This means repeated runs with the same `config_id` and `rate` no longer
overwrite previous raw benchmark files. `all_runs.csv` records both `run_id` and
`config_id`.

After the run:

```bash
cat larry_results/all_runs.csv
grep -R "\[LARRY\]" server_logs/
```

### 6. Run a small tuning loop

By default, `agent.main` starts a fresh experiment session. Before it writes new
results, it archives the previous `all_runs.csv`, `agent_notes.md`, `result.md`,
and raw result JSON files under:

```text
larry_results/archive/<UTC timestamp>/
```

It does not archive or delete `larry_configs/best_config.json`; that file is
kept as the warm-start configuration for the next session. Pass
`--append-results` only if you intentionally want to keep appending into the
current `all_runs.csv`.

With the server already running, start with a small no-GPT loop:

```bash
python -m agent.main --no-llm --skip-baseline --rounds 1 --candidates-per-round 2
```

Then, after the benchmark path is stable, run GPT-backed tuning:

```bash
python -m agent.main --rounds 10 --candidates-per-round 6
```

Or run the full loop without GPT:

```bash
python -m agent.main --no-llm --rounds 10 --candidates-per-round 6
```

### 7. Stop the server

```bash
bash agent/stop_server.sh
```

### Recommended Minimal Test Sequence

```bash
cd /workspace/lqs3/LLM_scheduling/real_agent

python -m agent.main --dry-run --no-llm --rounds 1 --candidates-per-round 3

/root/miniconda3/bin/conda run -n myvllm python -c "from vllm.v1.core.sched.larry_hook import LarryRuntime; print(LarryRuntime.get().enabled)"

bash agent/start_server.sh

for i in $(seq 1 120); do
  curl -fs http://127.0.0.1:8000/v1/models >/dev/null 2>&1 && echo "server up" && break
  sleep 2
done

python -m agent.run_one --config-id default --config-json larry_configs/config_default.json --rates inf --round-id 0 --num-prompts 2

grep -R "\[LARRY\]" server_logs/

bash agent/stop_server.sh
```

### Troubleshooting

If the server fails to start or benchmark runs fail:

```bash
tail -200 server_logs/server_*.log
cat larry_results/all_runs.csv
```

If the benchmark seems to run but parameters do not change, check for a fresh
reload line during the run window:

```bash
grep -R "\[LARRY\] reload" server_logs/
```

If no reload line appears, the benchmark likely ran with the previous config.
Check `LARRY_CONFIG_PATH`, `larry_configs/active.json`, and
`LARRY_RELOAD_EVERY_STEPS`.

## File Responsibilities

### `agent/main.py`

The orchestrator. It loads configs, ensures directories exist, creates the FCFS
baseline when requested, asks the optimizer for candidates, runs experiments,
scores results, asks the analyzer for interpretation, updates `result.md`, and
saves the final best config.

### `agent/llm_client.py`

A small GPT API wrapper using the OpenAI Chat Completions HTTP API. It supports
JSON-mode responses for optimizer search planning and plain-text responses for
analysis.

### `agent/optimizer_agent.py`

Selects the current best configuration, asks GPT for a search plan when enabled,
and builds concrete candidates locally. GPT does not emit full configs. Its
required output is a direction plan for the exploit/explore slots:

```json
{
  "slots": {
    "exploit_a": {
      "directions": {
        "MIN_QUEUE": "increase",
        "PRESSURE_AMPLIFIER": "decrease"
      },
      "rationale": "Recent p99 TTFT pressure suggests a more conservative local move."
    },
    "exploit_b": {
      "directions": {
        "CACHE_WEIGHT": "increase"
      },
      "rationale": "Improve cache locality without changing queue admission."
    },
    "explore_a": {
      "directions": {
        "SHORT_PREFILL_BOOST": "toggle"
      },
      "rationale": "Test whether short-prefill bias helps this workload."
    },
    "explore_b": {
      "directions": {
        "MIN_QUEUE": "high",
        "ALPHA_BASE": "high"
      },
      "rationale": "Explore a broader high-batching region."
    }
  }
}
```

Allowed directions are:

```text
increase, decrease, toggle, high, low, keep
```

The local candidate builder then creates the fixed six-role layout:

```text
c1: current best replay
c2: exploitation around best A
c3: exploitation around best B
c4: exploration A
c5: exploration B
c6: safety anchor
```

The final output consumed by `main.py` is still a list of candidate configs:

```json
[
  {
    "config_id": "r2_c1",
    "slot": "exploit_a",
    "rationale": "Raise MIN_QUEUE to reduce medium-load tail starvation.",
    "config": {
      "ALPHA_BASE": 80000,
      "MIN_QUEUE": 24
    }
  }
]
```

The module validates and fills missing LarryConfig fields from
`config_default.json`, clamps values to `SEARCH_SPACE`, deduplicates candidates,
and repairs bonus ordering constraints.

Current best selection is run-aware. The optimizer groups history by
`config_id + run_id`, ignores failed runs, prefers complete multi-rate results
when available, and excludes any run with a hard constraint violation. This
prevents old smoke tests or partial single-rate runs from becoming the replay
candidate by accident.

If `--no-llm` is passed or GPT is unavailable, the same six-role layout is used,
but the search plan comes from a deterministic fallback based on recent
constraint violations and latency pressure.

When a fresh session has no candidate history yet, or only contains the automatic
FCFS/default warmup rows, the optimizer uses `larry_configs/best_config.json` as
the current-best seed. If that file is missing or invalid, it falls back to
`config_default.json`.

### `agent/analyzer_agent.py`

Summarizes a round: best configs, constraint violations, likely parameter
effects, suspicious results, and the next search direction.

### `agent/run_one.py`

Runs one candidate across one or more request rates:

1. Writes the candidate to `larry_configs/active.json` atomically.
2. Waits briefly for hot reload.
3. Invokes `vllm bench serve`.
4. Saves raw benchmark JSON under a unique `run_id`.
5. Normalizes metrics.
6. Appends rows to `larry_results/all_runs.csv`.

The generated `run_id` has this shape:

```text
<UTC timestamp>_<config_id>_<random suffix>
```

Example:

```text
20260511T153012Z_default_smoke_1a2b3c4d
```

You can override it for reproducible naming:

```bash
python -m agent.run_one \
  --config-id default \
  --config-json larry_configs/config_default.json \
  --rates inf \
  --run-id manual_default_test_001 \
  --num-prompts 2
```

For FCFS baseline with a long-lived LARRY-enabled server, it writes a config with
`MIN_QUEUE` set very high so reordering is a no-op.

### `agent/parse_metrics.py`

Converts vLLM benchmark JSON into canonical fields:

```text
duration
request_throughput
output_throughput
mean_ttft_ms
p99_ttft_ms
mean_tpot_ms
p99_tpot_ms
```

### `agent/score.py`

Reads `objective.json` and compares each current row to the FCFS baseline at the
same rate. It writes:

- per-rate score
- constraint violation flag
- violation reason
- composite score across rates

### `agent/summarize.py`

Rebuilds `result.md` from `all_runs.csv`, including environment, baseline,
ranked candidates, and the latest analyzer notes.

## Important Operational Rule

Always verify that a candidate actually took effect. The vLLM server log should
contain a fresh line like:

```text
[LARRY] reload v<N>
```

for the candidate run window. If not, the benchmark probably used the previous
configuration and the result should be marked invalid.

## Remarks

### Long-lived server and hot switching

The vLLM server is intended to stay alive across many tuning runs. Normal
hyperparameter tuning does not require restarting the server.

At server start, `agent/start_server.sh` sets:

```text
VLLM_USE_LARRY=1
LARRY_CONFIG_PATH=/workspace/lqs3/LLM_scheduling/real_agent/larry_configs/active.json
LARRY_RELOAD_EVERY_STEPS=20
```

The switching mechanism is:

```text
agent.run_one
  -> writes a candidate config into larry_configs/active.json
  -> vLLM's LARRY hook periodically checks the file mtime
  -> if active.json changed, LarryRuntime reloads it
  -> subsequent schedule() calls use the new LARRY parameters
```

So these changes are hot-swapped without restarting vLLM:

- `ALPHA_BASE`
- `MIN_QUEUE`
- `CACHE_WEIGHT`
- `CACHE_PROBE_INTERVAL`
- `SESSION_PROGRESS_WEIGHT`
- `CONTINUITY_BONUS`
- `CONTINUITY_DECAY`
- `ADAPTIVE_*`
- `PRESSURE_AMPLIFIER`
- `DECODE_PRESSURE_THRESHOLD`
- `SHORT_PREFILL_BOOST`
- `SHORT_PREFILL_THRESHOLD`

### FCFS baseline without restart

`run_one.py` supports:

```bash
python -m agent.run_one \
  --config-id fcfs_baseline \
  --config-json fcfs \
  --rates inf \
  --round-id 0
```

This does not disable the hook process-wide. Instead, it writes a no-op LARRY
config with:

```text
MIN_QUEUE = 1000000000
```

The scheduler hook returns before reordering when:

```python
q_len <= cfg.MIN_QUEUE
```

Since the real waiting queue will not exceed that huge value, the behavior is
effectively FCFS for the benchmark run.

### When to restart the server

Restart vLLM only when one of these changes:

- vLLM scheduler code or `larry_hook.py`
- server startup parameters, such as `max_num_seqs`, `max_num_batched_tokens`,
  model path, tensor parallel size, or prefix caching
- startup environment variables, such as `VLLM_USE_LARRY`, `LARRY_CONFIG_PATH`,
  or `LARRY_RELOAD_EVERY_STEPS`
- the server crashed, hung, or hit GPU OOM
- you need to test truly stock vLLM with `VLLM_USE_LARRY=0`

For normal candidate search, leave the server running and let `run_one.py`
hot-swap `active.json`.
