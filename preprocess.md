# Workload Preprocess 与 Warm-start 调参方案

本文档说明一个新增的前处理流程：在正式 agentic tuning 前，先分析 workload/dataset，得到结构化统计指标，再用这些指标生成第一轮候选 LARRY 参数。这个流程的目标是让搜索从 workload-aware 的初始点开始，而不是完全依赖默认值或盲目 sweep。

## 1. 核心观点

导师提出的想法是合理的。它本质上是在做：

```text
workload-aware initialization
```

也就是：

```text
先理解 workload 的形状
再决定初始 LARRY 参数候选
最后进入正常闭环搜索
```

推荐流程不是让 LLM 直接读取和分析原始 ShareGPT 数据集，而是：

```text
deterministic profiler
  负责计算事实和统计量

LLM / rule-based warm-start
  负责根据统计量做策略推理和候选参数设计
```

原因：

- 原始 dataset 很大，LLM 上下文放不下。
- token length、percentile、queue estimate 等统计量应该由确定性代码计算。
- LLM 对精确统计不可靠，但适合解释结构化结果和提出搜索策略。
- 原始对话文本可能有隐私和噪声，最好只把统计摘要给 LLM。

一句话：

```text
代码负责算事实，LLM 负责做策略推理。
```

## 2. 新增 Workflow

建议在现有 agent workflow 前新增一个 preprocess 阶段。

```text
Step 0: workload profiling
  agent/profile_workload.py
    -> workload_profiles/sharegpt_profile.json

Step 1: warm-start candidate generation
  agent/warm_start.py
    -> larry_configs/config_warm_c1.json
    -> larry_configs/config_warm_c2.json
    -> ...

Step 2: warm-start experiments
  agent/run_one.py
    -> 运行 config_warm_c*.json

Step 3: normal iterative search
  agent/main.py
    -> optimizer_agent.py
    -> run_one.py
    -> analyzer_agent.py
    -> summarize.py
```

整体链路：

```text
Dataset / trace
  ↓
profile_workload.py
  ↓
workload_profile.json
  ↓
warm_start.py or Optimizer Agent
  ↓
initial candidate configs
  ↓
run_one.py benchmark
  ↓
normal agentic tuning loop
```

## 3. 为什么 Preprocess 对 LARRY 有用

LARRY 的很多超参数和 workload 强相关：

```text
MIN_QUEUE
  取决于 arrival rate、server capacity、queue depth 分布。

ALPHA_BASE
  取决于 prompt length 分布、长短请求差异、长请求饥饿风险。

SHORT_PREFILL_THRESHOLD
  取决于 prompt token 分布。

SHORT_PREFILL_BOOST
  取决于短请求比例和短请求优先级策略。

CACHE_WEIGHT
  取决于 prefix cache 命中潜力。

SESSION_PROGRESS_WEIGHT / CONTINUITY_BONUS
  取决于 multi-turn session 占比和 session locality。

PRESSURE_AMPLIFIER
  取决于 decode pressure、prefill/decode 混合程度。
```

如果不看 workload，第一轮候选只能依赖经验值，例如：

```text
MIN_QUEUE in [8, 16, 24, 32]
ALPHA_BASE in [60000, 90000, 120000]
PRESSURE_AMPLIFIER in [0.8, 1.0, 1.5]
```

如果先看 workload，可以更有针对性地决定：

- 是否应该保守启用 LARRY
- 是否需要提高 aging 避免长请求饥饿
- 是否要探索 short prefill boost
- 是否值得打开 session/cache 相关参数
- 哪些 rate 更容易出现 queue pressure

## 4. 应该分析哪些指标

### 4.1 Prompt length 分布

需要统计：

```text
count
mean_prompt_tokens
median_prompt_tokens
p75_prompt_tokens
p90_prompt_tokens
p95_prompt_tokens
p99_prompt_tokens
max_prompt_tokens
```

用途：

- 判断请求长短差异。
- 判断是否存在强长尾。
- 决定 `SHORT_PREFILL_THRESHOLD` 的候选值。
- 决定 `ALPHA_BASE` 是否要偏高。

例子：

```text
p50 = 800
p90 = 6000
p99 = 20000
```

说明 prompt 长尾很强。如果 LARRY 过度偏短请求，长请求的 p99 TTFT 可能会明显恶化。

### 4.2 Output length 分布

需要统计：

```text
mean_output_tokens
median_output_tokens
p75_output_tokens
p90_output_tokens
p95_output_tokens
p99_output_tokens
max_output_tokens
```

用途：

- 判断 workload 是 decode-heavy 还是 prefill-heavy。
- 估计 running decode pressure。
- 决定 `PRESSURE_AMPLIFIER` 的候选范围。

如果 output 很长，decode pressure 更明显，`PRESSURE_AMPLIFIER` 需要谨慎。

### 4.3 Prompt / output ratio

统计：

```text
mean_prompt_tokens / mean_output_tokens
total_prompt_tokens / total_output_tokens
```

用途：

```text
prefill-heavy workload:
  更关注 prompt length、short boost、cache reuse。

decode-heavy workload:
  更关注 running requests、decode pressure、TPOT。
```

### 4.4 Multi-turn session 指标

如果 dataset 或 trace 保留 session/conversation 信息，可以统计：

```text
num_sessions
turns_per_session_mean
turns_per_session_p50
turns_per_session_p90
turns_per_session_p99
multi_turn_session_ratio
messages_per_session
session_reuse_potential
```

用途：

- 判断 `SESSION_PROGRESS_WEIGHT` 是否有意义。
- 判断 `CONTINUITY_BONUS` 是否值得打开。
- 判断同一 session 的 prefix cache 是否可能被复用。

注意：

```text
如果 benchmark 把 ShareGPT 样本当作独立 single request 随机发出，
session-aware 参数影响通常很小。

如果 benchmark 构造了 multi-turn trace，并按 session 顺序发送请求，
session-aware 参数才会成为关键调参对象。
```

### 4.5 Prefix/cache 复用潜力

可以估计：

```text
exact_prefix_duplicate_ratio
common_prefix_token_ratio
same_session_prefix_overlap
conversation_history_growth
```

用途：

- 判断 `CACHE_WEIGHT` 是否应该提高。
- 判断 `CONTINUITY_BONUS` 是否有效。
- 判断 prefix caching 是否是主要优化空间。

规则：

```text
prefix overlap 很低:
  CACHE_WEIGHT 不应太大。
  continuity/session bonus 可以弱化或置零。

same-session overlap 很高:
  CACHE_WEIGHT、CONTINUITY_BONUS、ADAPTIVE_* 值得探索。
```

### 4.6 预估队列长度 / load pressure

队列长度不能只从 dataset 得到，还需要结合：

```text
request rates
server capacity
mean service time
max_num_seqs
max_num_batched_tokens
```

粗略估计：

```text
arrival_rate = λ
mean_service_time = E[S]
utilization ρ = λ * E[S]
```

也可以从一次短 FCFS warmup 得到：

```text
capacity_req_per_s ≈ request_throughput at rate=inf
```

然后判断：

```text
λ >= capacity:
  queue 会持续加深。

λ << capacity:
  queue 多数时间较浅。

λ 接近 capacity:
  queue 会波动，容易出现中等负载 tail latency 问题。
```

对 `MIN_QUEUE` 的启发：

```text
高负载 inf:
  queue 深，LARRY 激活通常合理。

中负载 4 req/s:
  如果接近 capacity，MIN_QUEUE 太小可能频繁重排，伤 p99 TTFT。

低负载 2 req/s:
  如果低于 capacity，MIN_QUEUE 应偏高，让 LARRY 多数时间不干预 FCFS。
```

## 5. Workload Profile 输出格式

建议新增目录：

```text
workload_profiles/
```

profile 输出示例：

```json
{
  "dataset": "~/.etc/ShareGPT_V3_unfiltered_cleaned_split.json",
  "model": "/workspace/LLM/Butter_L3_8B_RPMaster_v2",
  "num_samples": 5000,
  "prompt_tokens": {
    "mean": 1234,
    "p50": 800,
    "p75": 1800,
    "p90": 4200,
    "p95": 7000,
    "p99": 16000,
    "max": 30000
  },
  "output_tokens": {
    "mean": 240,
    "p50": 180,
    "p75": 320,
    "p90": 600,
    "p95": 900,
    "p99": 1600,
    "max": 4096
  },
  "ratios": {
    "prompt_to_output_mean_ratio": 5.14,
    "prompt_long_tail_ratio_p99_p50": 20.0
  },
  "multi_turn": {
    "session_count": 3000,
    "multi_turn_session_ratio": 0.42,
    "turns_per_session_mean": 2.8,
    "turns_per_session_p90": 6
  },
  "cache_reuse": {
    "exact_prefix_duplicate_ratio": 0.03,
    "estimated_same_session_prefix_overlap": 0.35
  },
  "load_estimates": {
    "rates": {
      "inf": {
        "expected_queue": "deep"
      },
      "4": {
        "expected_queue": "medium"
      },
      "2": {
        "expected_queue": "shallow"
      }
    }
  }
}
```

## 6. 从 Profile 到初始候选参数的映射逻辑

### 6.1 `MIN_QUEUE`

输入：

```text
estimated_queue_by_rate
max_num_seqs
capacity estimate
```

规则：

```text
中低负载 queue 较浅:
  MIN_QUEUE 取 16-32，避免不必要重排。

高负载 queue 很深:
  MIN_QUEUE 取 8-16，允许 LARRY 介入。

中负载接近 capacity:
  MIN_QUEUE 不宜太小，优先试 20-32。
```

候选：

```text
[8, 16, 24, 32]
```

### 6.2 `ALPHA_BASE`

输入：

```text
prompt length p99 / p50
prompt length CV
长请求比例
```

规则：

```text
如果 p99_prompt / p50_prompt > 10:
  使用较高 aging，避免长请求饥饿。
  candidates: 80000, 120000, 160000

否则:
  candidates: 40000, 80000, 120000
```

### 6.3 `SHORT_PREFILL_THRESHOLD`

输入：

```text
prompt token percentiles
```

规则：

```text
SHORT_PREFILL_THRESHOLD = p50 / p75 / p90 prompt tokens
```

候选：

```text
[p50_prompt, p75_prompt, p90_prompt]
```

需要 clamp 到合法范围：

```text
[512, 32768]
```

### 6.4 `SHORT_PREFILL_BOOST`

输入：

```text
short request ratio
tail latency objective
```

规则：

```text
如果短请求很多，但 p99 TTFT 约束很严格:
  boost 应保守。

如果目标偏 throughput / mean latency:
  可探索中等 boost。
```

候选：

```text
[0, 25000, 50000, 100000]
```

### 6.5 `PRESSURE_AMPLIFIER`

输入：

```text
output length distribution
decode pressure estimate
prefill/decode ratio
```

规则：

```text
decode-heavy:
  candidates: 0.5, 1.0, 2.0

prefill-heavy:
  candidates: 1.0, 2.0, 4.0

如果 medium load p99 TTFT 是关键:
  避免过大的 pressure amplifier。
```

### 6.6 `CACHE_WEIGHT`

输入：

```text
prefix duplicate ratio
same-session prefix overlap
prefix caching enabled or not
```

规则：

```text
cache reuse low:
  CACHE_WEIGHT 接近默认或更低，例如 1024-4096。

cache reuse high:
  CACHE_WEIGHT 可探索 4096-20000。
```

### 6.7 Session / continuity 参数

参数：

```text
SESSION_PROGRESS_WEIGHT
CONTINUITY_BONUS
CONTINUITY_DECAY
ADAPTIVE_BASE_BONUS
ADAPTIVE_REFERENCE_LEN
ADAPTIVE_MIN_BONUS
ADAPTIVE_MAX_BONUS
```

规则：

```text
single-turn workload:
  SESSION_PROGRESS_WEIGHT = 0
  CONTINUITY_BONUS = 0 或很低
  ADAPTIVE_* 保守

multi-turn session-aware workload:
  打开 SESSION_PROGRESS_WEIGHT
  打开 continuity/adaptive bonus
  配合 CACHE_WEIGHT 搜索
```

## 7. LLM 应该如何介入

不建议：

```text
LLM 直接读取原始 ShareGPT JSON。
LLM 直接估算 token length。
LLM 直接估算 queue length。
LLM 直接替代 benchmark 结果。
```

建议：

```text
profile_workload.py:
  读取 dataset，调用 tokenizer，计算统计量。

warm_start.py:
  rule-based 生成候选，或把 workload_profile.json 给 GPT。

Optimizer Agent:
  基于 workload profile + 历史结果生成候选。

Analyzer Agent:
  解释 profile 和 benchmark 结果之间的关系。
```

LLM 输入应是结构化摘要，例如：

```json
{
  "prompt_tokens": {
    "p50": 800,
    "p99": 16000
  },
  "multi_turn": {
    "multi_turn_session_ratio": 0.42
  },
  "cache_reuse": {
    "estimated_same_session_prefix_overlap": 0.35
  },
  "objective": {
    "p99_ttft_ms": "strict"
  }
}
```

LLM 输出应是：

```json
{
  "strategy": "High prompt length skew; use higher aging and conservative MIN_QUEUE.",
  "candidates": [
    {
      "config_id": "warm_c1",
      "rationale": "Protect long prompts with high aging and avoid medium-load reordering.",
      "config": {
        "MIN_QUEUE": 24,
        "ALPHA_BASE": 120000,
        "PRESSURE_AMPLIFIER": 1.0
      }
    }
  ]
}
```

## 8. 建议新增代码文件

### 8.1 `agent/profile_workload.py`

职责：

- 读取 dataset
- 加载 tokenizer
- 计算 prompt/output token 分布
- 统计 multi-turn/session 指标
- 估计 prefix/cache reuse 潜力
- 根据 rates 和初始 benchmark 估计 load pressure
- 输出 `workload_profiles/*.json`

建议命令：

```bash
python -m agent.profile_workload \
  --dataset ~/.etc/ShareGPT_V3_unfiltered_cleaned_split.json \
  --model /workspace/LLM/Butter_L3_8B_RPMaster_v2 \
  --out workload_profiles/sharegpt_profile.json \
  --sample-size 5000
```

### 8.2 `agent/warm_start.py`

职责：

- 读取 `workload_profile.json`
- 读取 `objective.json`
- 根据 rule-based mapping 生成初始候选
- 可选调用 GPT 生成 rationale 或补充候选
- 输出 `larry_configs/config_warm_c*.json`

建议命令：

```bash
python -m agent.warm_start \
  --profile workload_profiles/sharegpt_profile.json \
  --out-dir larry_configs \
  --count 6
```

### 8.3 `configs/warm_start.json`

可选配置：

```json
{
  "enabled": true,
  "candidate_count": 6,
  "use_llm": true,
  "profile_path": "workload_profiles/sharegpt_profile.json",
  "rules": {
    "min_queue_candidates": [8, 16, 24, 32],
    "alpha_high_tail_candidates": [80000, 120000, 160000],
    "alpha_normal_candidates": [40000, 80000, 120000],
    "pressure_decode_heavy_candidates": [0.5, 1.0, 2.0],
    "pressure_prefill_heavy_candidates": [1.0, 2.0, 4.0]
  }
}
```

## 9. 如何接入现有 `agent/main.py`

可以给 `main.py` 增加参数：

```bash
python -m agent.main \
  --use-workload-profile workload_profiles/sharegpt_profile.json \
  --rounds 10 \
  --candidates-per-round 6
```

逻辑：

```text
if workload_profile exists and round_id == 1:
  candidates = warm_start_candidates(profile)
else:
  candidates = optimizer_agent.propose_candidates(history)
```

也可以更保守：

```text
先手动运行 warm_start.py 生成 config_warm_c*.json
再用 run_one.py 跑这些候选
然后再启动 main.py 正常搜索
```

这种方式更容易 debug。

## 10. 与现有 Workflow 的关系

现有 workflow：

```text
baseline/default
  -> optimizer_agent
  -> run_one
  -> analyzer_agent
  -> summarize
```

新增 preprocess 后：

```text
profile_workload
  -> warm_start
  -> warm-start experiments
  -> baseline/default
  -> optimizer_agent
  -> run_one
  -> analyzer_agent
  -> summarize
```

preprocess 只影响“初始候选从哪里来”，不改变后续 benchmark、score、analysis 的主闭环。

## 11. 推荐落地顺序

建议分三步实现，避免一次做太复杂。

### Phase 1: deterministic profile

只实现：

```text
agent/profile_workload.py
```

输出 token length、session、prefix reuse 的 JSON。

### Phase 2: rule-based warm start

实现：

```text
agent/warm_start.py
```

不调用 LLM，先按规则生成 4-6 个候选。

### Phase 3: LLM-assisted warm start

把 `workload_profile.json` 输入给 GPT，让 GPT：

- 解释 workload 特征
- 生成 rationale
- 对 rule-based candidates 做补充
- 给出下一轮搜索策略

## 12. 总结

这个 preprocess 流程是值得做的，因为它能把搜索从“经验起点”改成“workload-aware 起点”。

最重要的原则是：

```text
不要让 LLM 直接分析原始数据。
先用确定性代码把 workload 变成统计摘要。
再让 LLM 或规则系统基于摘要生成 warm-start candidates。
```

这会让第一轮候选更有针对性，也更容易解释为什么某些参数组合值得优先尝试。
