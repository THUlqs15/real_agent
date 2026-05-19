# LARRYSmith Agentic 调参系统

本文档解释当前 `real_agent` 项目的整体 workflow、子 agent 分工、核心代码文件逻辑，以及主要配置文件。系统目标是自动化调参 vLLM 中的 LARRY v6 调度算法。

## 1. 系统目标

这个项目做的是一个面向 LLM serving scheduler 的闭环 agentic auto-tuning 系统。

它要解决的问题是：

```text
给定 LARRY v6 调度算法的一组超参数搜索空间，
自动提出候选参数，
运行真实 vLLM benchmark，
收集 latency / throughput 指标，
和 FCFS baseline 比较，
再根据结果提出下一轮候选参数。
```

Optimizer 采用两阶段逻辑：LLM 根据历史结果判断下一轮应该 exploit/explore 哪些参数和方向；本地 Python 代码再把这个 search plan 转成具体候选配置。真正执行实验、解析指标、计算 score、写文件，全部由 Python/shell 工具完成。

## 2. 总体 Workflow

完整闭环如下：

```text
agent/main.py
  |
  |-- 读取 configs/runtime.json
  |-- 读取 configs/objective.json
  |-- 读取 configs/llm.json
  |
  |-- 可选：运行 FCFS baseline
  |-- 可选：运行 default LARRY config
  |
  |-- Round 1..N:
        |
        |-- optimizer_agent.py
        |     1. 从历史结果选择 current best
        |     2. 让 GPT 或 fallback 生成 exploit/explore search plan
        |     3. 本地生成一组候选 LARRY configs
        |
        |-- run_one.py
        |     对每个候选：
        |       1. 写入 larry_configs/active.json
        |       2. 等待 vLLM LARRY hook 加载
        |       3. 调用 vllm bench serve
        |       4. 保存 raw benchmark JSON
        |       5. 解析 metrics
        |       6. 追加 all_runs.csv
        |       7. 计算 score
        |
        |-- analyzer_agent.py
        |     分析本轮结果、trade-off 和下一轮搜索方向
        |
        |-- summarize.py
              更新 result.md
```

vLLM server 不需要每轮重启。`run_one.py` 每次只改：

```text
larry_configs/active.json
```

vLLM 内部的 LARRY hook 会定期检查这个文件，并加载新参数。

## 3. Long-lived Server 与切换机制

`agent/start_server.sh` 启动 vLLM server 时会设置：

```text
VLLM_USE_LARRY=1
LARRY_CONFIG_PATH=/workspace/lqs3/LLM_scheduling/real_agent/larry_configs/active.json
LARRY_RELOAD_EVERY_STEPS=20
```

含义是：

```text
VLLM_USE_LARRY=1
  开启 vLLM scheduler 中的 LARRY hook。

LARRY_CONFIG_PATH
  指向当前 live config 文件。

LARRY_RELOAD_EVERY_STEPS=20
  每 20 次 scheduler 调度检查一次 active.json 是否变化。
```

切换过程：

```text
run_one.py 写 active.json
  ↓
vLLM scheduler 调用 _larry_reorder_waiting()
  ↓
LarryRuntime.maybe_reload()
  ↓
发现 active.json mtime 变化
  ↓
重新加载 LarryConfig
  ↓
后续请求使用新参数调度
```

因此普通调参不需要重启 server。只有以下情况需要重启：

- 修改了 vLLM scheduler patch 或 `larry_hook.py`
- 修改了 server 启动参数，例如 model、`max_num_seqs`、`max_num_batched_tokens`
- 修改了 `VLLM_USE_LARRY`、`LARRY_CONFIG_PATH` 等启动环境变量
- server 崩溃、hang、OOM
- 要测试真正 `VLLM_USE_LARRY=0` 的 original vLLM

## 4. 子 Agent 分工

当前系统不是很多独立进程，而是一个 orchestrator 调用多个逻辑角色。主要角色如下。

### 4.1 Orchestrator

实现文件：

```text
agent/main.py
```

职责：

- 管理完整调参流程
- 加载 runtime 配置
- 确保目录存在
- 调用 baseline/default 实验
- 每轮调用 Optimizer Agent 生成候选
- 对每个候选调用 `run_one.py`
- 调用 Analyzer Agent 分析结果
- 调用 `summarize.py` 更新 `result.md`
- 从历史结果中保存 `best_config.json`

核心逻辑：

```python
for round_id in range(1, rounds + 1):
    candidates = propose_candidates(...)
    for candidate in candidates:
        write config_rN_cM.json
        run_one(...)
    analysis = analyze_round(...)
    summarize(...)
```

### 4.2 Optimizer Agent

实现文件：

```text
agent/optimizer_agent.py
```

职责：

- 读取历史结果 `all_runs.csv`
- 读取 `objective.json`
- 读取默认 LARRY config
- 选择当前最好的有效 run 作为 `current best`
- 根据搜索空间生成下一轮候选参数

它有两种模式：

```text
LLM 模式：
  调 GPT，让模型根据历史结果生成 search plan。
  GPT 只决定 exploit/explore 哪些参数以及方向，不直接输出完整配置。

fallback 模式：
  当 --no-llm 或 GPT 不可用时，用 deterministic fallback 生成 search plan。
  后续仍由本地代码生成相同结构的候选。
```

### 4.2.1 Current Best 选择规则

Optimizer 不会简单地从 `all_runs.csv` 里拿单行最高分作为 best。原因是历史结果里可能混有：

- dry-run 结果
- smoke test 单 rate 结果
- 失败 run
- 同一个 `config_id` 的多次重复运行
- 违反 hard constraint 的候选

当前实现会按 `config_id + run_id` 分组。如果旧 CSV 里没有显式 `run_id`，会用 `round_id` 里的时间戳 run id 兜底。

选择规则是：

```text
1. 跳过 fcfs_baseline
2. 跳过 success=false 的 run
3. 如果存在完整多 rate run，优先只在多 rate run 中比较
4. 如果一个 run 的任意 rate 违反 constraint，则整个 run 不作为 best
5. 用 composite_score 选择当前 best
6. 如果 CSV 里的 config_json 因旧 header 错位不可解析，则回退读取 larry_configs/config_<config_id>.json
```

这样 `c1: current best replay` 会 replay 一个真实可比、没有违反约束的配置，而不是被早期 smoke test 的单点高分误导。

### 4.2.2 LLM Search Plan

LLM 输入包括：

- `SEARCH_SPACE`
- `objective.json`
- `current_best_config`
- 最近历史结果 summary
- 每个候选 slot 的固定含义

LLM 输出不是完整 LARRY config，而是如下 search plan：

```json
{
  "slots": {
    "exploit_a": {
      "directions": {
        "MIN_QUEUE": "increase",
        "PRESSURE_AMPLIFIER": "decrease"
      },
      "rationale": "根据最近 p99 TTFT 压力做更保守的局部搜索"
    },
    "exploit_b": {
      "directions": {
        "CACHE_WEIGHT": "increase"
      },
      "rationale": "增强 cache locality，但不改变 admission pressure"
    },
    "explore_a": {
      "directions": {
        "SHORT_PREFILL_BOOST": "toggle"
      },
      "rationale": "测试 short prefill bias 是否有收益"
    },
    "explore_b": {
      "directions": {
        "MIN_QUEUE": "high",
        "ALPHA_BASE": "high"
      },
      "rationale": "探索更激进的 high batching 区域"
    }
  }
}
```

允许的 direction：

```text
increase
decrease
toggle
high
low
keep
```

`_sanitize_plan()` 会过滤非法参数名和非法 direction，所以即使 GPT 输出不干净，也不会直接进入候选配置。

### 4.2.3 每轮 6 个固定候选角色

本地代码将 search plan 转成固定 6 个候选：

```text
c1: current best replay
c2: exploitation around best A
c3: exploitation around best B
c4: exploration A
c5: exploration B
c6: safety anchor
```

含义：

- `c1`：完全 replay 当前 best，用于检测 run-to-run variance。
- `c2/c3`：围绕 best 做小步 exploitation，例如 `increase` 会乘以较小倍率。
- `c4/c5`：做更大步 exploration，例如 `high/low/toggle` 会移动到搜索空间的更远区域。
- `c6`：保守 safety anchor，倾向 `MIN_QUEUE=32`、较低 `PRESSURE_AMPLIFIER`、关闭 `SHORT_PREFILL_BOOST`，用于防止整轮候选都太激进。

注意：不是固定只调三四个参数。GPT 可以从 `SEARCH_SPACE` 中选择任意合法参数，但 prompt 会建议优先关注：

```text
MIN_QUEUE
ALPHA_BASE
PRESSURE_AMPLIFIER
CACHE_WEIGHT
SHORT_PREFILL_*
```

因为这些参数对当前 single-turn serving benchmark 更直接。`SESSION_PROGRESS_WEIGHT`、`CONTINUITY_BONUS`、`ADAPTIVE_*` 等 session-aware 参数不是禁止调，而是只有当历史结果或数据预处理显示 multi-turn/session-aware 明显相关时，才更值得打开。

搜索空间在 `SEARCH_SPACE` 中定义，例如：

```text
ALPHA_BASE: [1000, 200000]
MIN_QUEUE: [4, 32]
CACHE_WEIGHT: [500, 20000]
PRESSURE_AMPLIFIER: [0.5, 10.0]
SHORT_PREFILL_BOOST: [0, 500000]
```

候选输出格式：

```json
{
  "config_id": "r1_c1",
  "slot": "exploit_a",
  "rationale": "why this config is proposed",
  "config": {
    "ALPHA_BASE": 60000,
    "MIN_QUEUE": 16,
    "PRESSURE_AMPLIFIER": 1.0
  }
}
```

`_clamp_config()` 会做基本保护：

- 补齐缺失参数
- 将参数限制在搜索范围内
- 修正 `ADAPTIVE_MIN_BONUS < ADAPTIVE_BASE_BONUS <= ADAPTIVE_MAX_BONUS`
- 修正非法 `CONTINUITY_DECAY`

`_apply_plan()` 会根据 direction 和 slot 类型决定步长：

- exploitation：小步调整，主要围绕 best 做局部搜索
- exploration：大步调整，允许跳到更远区域

`_structured_candidates()` 会做 dedupe。如果两个 slot 生成了完全相同的 config，会轻微扰动 `ALPHA_BASE`，避免浪费一次 benchmark。

### 4.3 Executor / Experiment Runner

实现文件：

```text
agent/run_one.py
```

职责：

- 跑单个 candidate config
- 将候选参数写入 `larry_configs/active.json`
- 调用 `vllm bench serve`
- 保存 raw benchmark JSON
- 调用 `parse_metrics.py`
- 追加 `all_runs.csv`
- 调用 `score.py`

核心步骤：

```text
1. _load_candidate()
   - 如果 config-json 是 "fcfs"，生成 no-op config
   - 否则读取 JSON 并和 config_default.json 合并

2. atomic_write_json(active.json)
   - 原子替换 active.json

3. sleep reload_wait_seconds
   - 给 vLLM hook 热加载时间

4. vllm bench serve
   - backend=vllm
   - endpoint=/v1/completions
   - dataset=sharegpt
   - rates=inf/4/2

5. parse_metrics()
   - 提取 duration、throughput、TTFT、TPOT

6. score_rows()
   - 与 FCFS baseline 比较并写 score
```

每次 `run_one.py` 都会生成唯一 `run_id`：

```text
20260511T213846Z_default_full_8412eda1
```

raw result 文件名会包含这个 `run_id`：

```text
larry_results/20260511T213846Z_default_full_8412eda1_rateinf.json
```

这样重复跑同一个 `config_id` 不会覆盖之前 raw JSON。

### 4.4 Metrics Parser

实现文件：

```text
agent/parse_metrics.py
```

职责：

- 读取 `vllm bench serve` 输出 JSON
- 提取统一字段
- 兼容不同字段别名

标准字段：

```text
duration
request_throughput
output_throughput
mean_ttft_ms
p99_ttft_ms
mean_tpot_ms
p99_tpot_ms
```

如果找不到 `duration`，会标记：

```text
success = false
error = "missing duration; benchmark JSON shape may have changed"
```

### 4.5 Scoring Tool

实现文件：

```text
agent/score.py
```

职责：

- 读取 `configs/objective.json`
- 找到同 rate 的 FCFS baseline
- 计算每个 candidate 的 per-rate score
- 检查 hard constraints
- 计算跨 rate 的 composite score

打分规则：

```text
lower_better:
  weight * (baseline - current) / baseline

higher_better:
  weight * (current - baseline) / baseline
```

约束示例：

```json
"p99_ttft_ms": {
  "max_ratio_vs_baseline": 1.10
}
```

含义是：

```text
如果 candidate 的 p99_ttft_ms 超过 FCFS baseline 的 1.10 倍，
则标记 constraint_violation=true。
```

### 4.6 Analyzer Agent

实现文件：

```text
agent/analyzer_agent.py
```

职责：

- 读取本轮结果
- 按 score 排序
- 调 GPT 分析 trade-off
- 给出下一轮搜索建议

它会关注：

- 哪个 config 最好
- 哪个 rate 出现退化
- 是否违反 hard constraints
- 是否用 tail latency 换了 throughput
- 下一轮应该调哪些参数

如果 GPT 不可用，会返回 fallback 文本，例如当前 round 最优 row 和检查建议。

### 4.7 Reporter / Summarizer

实现文件：

```text
agent/summarize.py
```

职责：

- 从 `all_runs.csv` 重建 `result.md`
- 写入环境信息
- 写入 FCFS baseline 表
- 写入 ranked candidates 表
- 附加 `agent_notes.md`

注意：

```text
result.md 是生成文件，会被 summarize.py 覆盖。
真实历史记录主要在 larry_results/all_runs.csv。
```

### 4.8 LLM Client

实现文件：

```text
agent/llm_client.py
```

职责：

- 读取 `configs/llm.json`
- 支持 `${OPENAI_API_KEY}` 环境变量展开
- 调 OpenAI Chat Completions API
- 支持 JSON mode

主要接口：

```python
GPTClient().complete(system_prompt, user_prompt, json_mode=True)
```

注意：

```text
llm.json 可以写明文 key，但不建议提交到 GitHub。
更推荐使用 "api_key": "${OPENAI_API_KEY}"。
```

## 5. 每个代码文件核心逻辑

### `agent/common.py`

通用工具函数：

- `load_json()` / `write_json()`
- `atomic_write_json()`
- `load_runtime()`
- `ensure_dirs()`
- `conda_prefix()`
- `run_command()`
- CSV 读写
- `default_larry_config()`
- `fcfs_noop_config()`

其中 `fcfs_noop_config()` 会生成：

```text
MIN_QUEUE = 1000000000
```

用于模拟 FCFS baseline。

### `agent/main.py`

主入口和 orchestrator。负责串联整个调参流程。

常用命令：

```bash
python -m agent.main --dry-run --no-llm --rounds 1 --candidates-per-round 3
python -m agent.main --rounds 10 --candidates-per-round 6
```

默认行为：

```text
每次运行 agent.main 都会开启一个新的 experiment session。
旧的 all_runs.csv、agent_notes.md、result.md 和 raw result JSON 会被归档到：

larry_results/archive/<UTC timestamp>/

larry_configs/best_config.json 会保留，用作本次 session 的 warm-start/current-best seed。
```

参数：

```text
--rounds
  搜索轮数。

--candidates-per-round
  每轮候选 config 数量。

--dry-run
  不跑真实 vLLM benchmark，生成 synthetic metrics。

--no-llm
  不调用 GPT，使用 deterministic fallback search plan。

--skip-baseline
  跳过自动跑 fcfs_baseline。

--append-results
  不归档旧结果，继续向当前 all_runs.csv 追加。只有需要跨 session 混合分析时才建议使用。
```

### `agent/run_one.py`

单个候选实验执行器。

常用命令：

```bash
python -m agent.run_one \
  --config-id default_full \
  --config-json larry_configs/config_default.json \
  --rates inf \
  --round-id 0
```

快速 smoke test：

```bash
python -m agent.run_one \
  --config-id default_smoke \
  --config-json larry_configs/config_default.json \
  --rates inf \
  --round-id 0 \
  --num-prompts 2
```

### `agent/optimizer_agent.py`

候选参数生成器。优先用 GPT 生成 search plan；失败时用 deterministic fallback plan。

它不是让 GPT 直接黑盒输出完整 config，而是：

```text
1. 读取 all_runs.csv
2. 按 config_id + run_id 选择 current best
3. 让 GPT 判断 exploit/explore 哪些参数以及方向
4. 本地生成 c1..c6 固定角色候选
5. clamp / dedupe / 修正 bonus 约束
```

固定角色：

```text
c1 best_replay
c2 exploit_a
c3 exploit_b
c4 explore_a
c5 explore_b
c6 safety_anchor
```

### `agent/analyzer_agent.py`

实验结果分析器。给 GPT 的输入包括：

- 本轮 rows
- top rows
- objective
- 关键问题列表

输出写入：

```text
larry_results/agent_notes.md
```

### `agent/parse_metrics.py`

benchmark JSON 标准化工具。

输入：

```text
raw benchmark JSON
config_id
rate
```

输出：

```text
统一 metrics row
```

### `agent/score.py`

打分和约束检查工具。

输入：

```text
larry_results/all_runs.csv
configs/objective.json
```

输出字段：

```text
score
constraint_violation
violation_reason
composite_score
```

### `agent/summarize.py`

结果汇总工具。根据 `all_runs.csv` 和 `agent_notes.md` 生成 `result.md`。

### `agent/start_server.sh`

启动长驻 vLLM server。

核心逻辑：

- 读取 `configs/runtime.json`
- 设置 LARRY 环境变量
- source conda
- activate `myvllm`
- 使用 `setsid` detached 启动 `vllm serve`
- 写入 `server_logs/server.pid`

### `agent/stop_server.sh`

根据 `server_logs/server.pid` 停止 server。

## 6. 配置文件说明

### 6.1 `configs/runtime.json`

定义实验环境和 server/benchmark 参数。

关键字段：

```json
{
  "conda_env": "myvllm",
  "conda_executable": "/root/miniconda3/bin/conda",
  "conda_sh": "/root/miniconda3/etc/profile.d/conda.sh",
  "vllm_project": "/workspace/lqs3/LLM_scheduling/vllm",
  "agent_dir": "/workspace/lqs3/LLM_scheduling/real_agent",
  "dataset": "~/.etc/ShareGPT_V3_unfiltered_cleaned_split.json",
  "model": "/workspace/LLM/Butter_L3_8B_RPMaster_v2",
  "server_host": "127.0.0.1",
  "server_port": 8000,
  "rates": ["inf", "4", "2"],
  "num_prompts": 512
}
```

含义：

```text
conda_env / conda_executable / conda_sh
  指定 vLLM 运行环境。

vllm_project
  editable vLLM 源码路径。

agent_dir
  agent 产生结果、配置、日志的位置。

dataset
  ShareGPT 数据集路径。

model
  vLLM serve 使用的本地模型路径。

rates
  benchmark request rates。

num_prompts
  每个 rate 下 benchmark 请求数。
```

### 6.2 `configs/objective.json`

定义 operator 认为“什么是好”。

主要包括：

- baseline config id
- rate 权重
- metric 权重
- metric 方向
- hard constraints

默认 rate 权重：

```json
"rate_weights": {
  "inf": 0.4,
  "4": 0.35,
  "2": 0.25
}
```

默认 metrics：

```text
duration              lower_better
request_throughput    higher_better
mean_ttft_ms          lower_better
p99_ttft_ms           lower_better
mean_tpot_ms          lower_better
p99_tpot_ms           lower_better
```

这个文件是 operator policy，不是 scheduler 参数。改它不会影响 vLLM 调度，只会影响 score 和 candidate ranking。

### 6.3 `configs/llm.json`

定义 GPT 调用配置。

字段：

```json
{
  "provider": "openai",
  "model": "gpt-5.5",
  "api_key": "...",
  "temperature": 0.2,
  "max_output_tokens": 4096,
  "timeout_seconds": 120
}
```

建议：

```text
本地可写明文 key 方便运行。
提交到 GitHub 前应改成 "${OPENAI_API_KEY}"。
```

### 6.4 `larry_configs/config_default.json`

默认 LARRY v6 参数。

它只包含 scheduler hook 使用的参数，不包含 objective 权重、API key 或 benchmark 参数。

### 6.5 `larry_configs/active.json`

vLLM LARRY hook 实际读取的 live config。

`run_one.py` 每次实验都会覆盖它：

```text
candidate config -> active.json -> vLLM hot reload
```

这个文件是运行时文件，不建议提交。

### 6.6 `larry_configs/config_rN_cM.json`

optimizer 生成的候选参数文件。

例如：

```text
config_r1_c1.json
config_r1_c2.json
```

这些是实验中间产物，当前 `.gitignore` 默认排除。

### 6.7 `larry_configs/best_config.json`

`main.py` 根据 `composite_score` 和 constraints 保存的当前最佳配置。

也是运行产物，默认不提交。

## 7. LARRY v6 参数说明

LARRY v6 分数公式在概念上是：

```text
score =
  aging
  - work_penalty
  + cache_bonus
  + progress_bonus
  + continuity_bonus
  + short_prefill_boost
```

主要参数：

```text
ALPHA_BASE
  aging 权重。越大，等待时间越容易抬高请求优先级。

MIN_QUEUE
  队列长度小于等于该值时不重排。它是最重要的 load-adaptive knob。

CACHE_WEIGHT
  cached prefix token 的加分权重。

CACHE_PROBE_INTERVAL
  每隔多少个 scheduler step 探测一次 prefix cache。

SESSION_PROGRESS_WEIGHT
  多轮 session 进度加分。单轮 ShareGPT workload 通常影响较小。

CONTINUITY_BONUS / CONTINUITY_DECAY
  连续性 bonus 及衰减时间。

ADAPTIVE_BASE_BONUS / ADAPTIVE_REFERENCE_LEN / ADAPTIVE_MIN_BONUS / ADAPTIVE_MAX_BONUS
  自适应 continuity bonus 相关参数。

PRESSURE_AMPLIFIER
  decode pressure 下对大 prefill 的惩罚放大系数。

DECODE_PRESSURE_THRESHOLD
  running requests 达到该值时 decode pressure 视为满。

SHORT_PREFILL_BOOST
  short prefill 请求的固定加分。

SHORT_PREFILL_THRESHOLD
  判断 short prefill 的 token 阈值。
```

调参优先级建议：

```text
1. MIN_QUEUE
2. ALPHA_BASE
3. PRESSURE_AMPLIFIER
4. CACHE_WEIGHT / SHORT_PREFILL_BOOST
5. continuity/session bonus group
```

## 8. FCFS Baseline 的实现方式

系统里的 FCFS baseline 不需要重启 server。

命令：

```bash
python -m agent.run_one \
  --config-id fcfs_baseline \
  --config-json fcfs \
  --rates inf,4,2 \
  --round-id 0
```

当 `config-json` 是特殊值 `fcfs` 时，`run_one.py` 会写入：

```text
MIN_QUEUE = 1000000000
```

LARRY hook 中有：

```python
if q_len <= cfg.MIN_QUEUE:
    return
```

所以实际队列永远不会触发 LARRY reorder，效果近似 FCFS。

## 9. 输出文件说明

### `larry_results/*.json`

每次 benchmark 的 raw JSON。

文件名包含唯一 `run_id`：

```text
20260511T213846Z_default_full_8412eda1_rateinf.json
```

### `larry_results/all_runs.csv`

所有实验的主结果表。

典型字段：

```text
run_id
config_id
rate
success
error
raw_result_path
duration
request_throughput
output_throughput
mean_ttft_ms
p99_ttft_ms
mean_tpot_ms
p99_tpot_ms
score
constraint_violation
violation_reason
composite_score
config_json
```

### `larry_results/agent_notes.md`

Analyzer Agent 的每轮分析。

### `result.md`

由 `summarize.py` 生成的人类可读总结。

### `server_logs/`

vLLM server 日志。

检查 LARRY 是否生效：

```bash
grep -R "\[LARRY\]" server_logs/
```

应看到：

```text
[LARRY] enabled
[LARRY] reload v<N>
```

## 10. 推荐使用顺序

### 10.1 启动 server

```bash
bash agent/start_server.sh
```

确认 ready：

```bash
curl -fs http://127.0.0.1:8000/v1/models
```

### 10.2 smoke test

```bash
python -m agent.run_one \
  --config-id default_smoke \
  --config-json larry_configs/config_default.json \
  --rates inf \
  --round-id 0 \
  --num-prompts 2
```

### 10.3 baseline

```bash
python -m agent.run_one \
  --config-id fcfs_baseline \
  --config-json fcfs \
  --rates inf,4,2 \
  --round-id 0
```

### 10.4 default LARRY

```bash
python -m agent.run_one \
  --config-id default_larry \
  --config-json larry_configs/config_default.json \
  --rates inf,4,2 \
  --round-id 0
```

### 10.5 自动搜索

不用 GPT：

```bash
python -m agent.main --no-llm --skip-baseline --rounds 1 --candidates-per-round 2
```

使用 GPT：

```bash
python -m agent.main --rounds 10 --candidates-per-round 6
```

### 10.6 停止 server

```bash
bash agent/stop_server.sh
```

## 11. 关键注意事项

1. 不要把明文 API key commit 到 GitHub。

2. `result.md`、`larry_results/`、`server_logs/` 是运行产物，默认不提交。

3. `active.json` 是 live config，会被 `run_one.py` 覆盖。

4. 判断配置是否真的生效，应检查 server log：

```bash
grep -R "\[LARRY\] reload" server_logs/
```

5. `rate=inf` 是突发高压场景，TTFT 很大是正常的；判断 LARRY 是否更好必须和同 rate 的 FCFS baseline 对比。

6. `objective.json` 的权重是 operator policy。不同业务目标应修改这里，而不是改 `score.py`。

7. 如果改了 vLLM 内部 hook 代码，需要重启 server；如果只是改 LARRY 参数，不需要重启。
