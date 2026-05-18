from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.common import ROOT, default_larry_config, load_json, read_csv
from agent.llm_client import GPTClient, LLMUnavailable


SEARCH_SPACE = {
    "ALPHA_BASE": [1000, 200000],
    "MIN_QUEUE": [4, 32],
    "CACHE_WEIGHT": [500, 20000],
    "CACHE_PROBE_INTERVAL": [1, 16],
    "SESSION_PROGRESS_WEIGHT": [0, 50000],
    "CONTINUITY_BONUS": [10000, 1000000],
    "CONTINUITY_DECAY": [10.0, 300.0],
    "ADAPTIVE_BASE_BONUS": [5000, 500000],
    "ADAPTIVE_REFERENCE_LEN": [5000, 100000],
    "ADAPTIVE_MIN_BONUS": [0, 100000],
    "ADAPTIVE_MAX_BONUS": [50000, 2000000],
    "PRESSURE_AMPLIFIER": [0.5, 10.0],
    "DECODE_PRESSURE_THRESHOLD": [1, 10],
    "SHORT_PREFILL_BOOST": [0, 500000],
    "SHORT_PREFILL_THRESHOLD": [512, 32768],
}

PLAN_SLOTS = ("exploit_a", "exploit_b", "explore_a", "explore_b")
ALLOWED_DIRECTIONS = {"increase", "decrease", "toggle", "high", "low", "keep"}


def _clamp_config(cfg: dict[str, Any]) -> dict[str, Any]:
    base = default_larry_config()
    base.update({k: v for k, v in cfg.items() if k in base})

    for key, bounds in SEARCH_SPACE.items():
        lo, hi = bounds
        val = base[key]
        if isinstance(lo, int) and isinstance(hi, int):
            val = int(round(float(val)))
        else:
            val = float(val)
        base[key] = max(lo, min(hi, val))

    if base["ADAPTIVE_MIN_BONUS"] >= base["ADAPTIVE_BASE_BONUS"]:
        base["ADAPTIVE_MIN_BONUS"] = max(0, int(base["ADAPTIVE_BASE_BONUS"]) // 2)
    if base["ADAPTIVE_BASE_BONUS"] > base["ADAPTIVE_MAX_BONUS"]:
        base["ADAPTIVE_MAX_BONUS"] = base["ADAPTIVE_BASE_BONUS"]
    if base["CONTINUITY_DECAY"] <= 0:
        base["CONTINUITY_DECAY"] = 60.0
    return base


def _parse_float(raw: str | None, default: float = float("-inf")) -> float:
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _load_config_from_row(row: dict[str, str]) -> dict[str, Any] | None:
    raw = row.get("config_json") or ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed:
            return _clamp_config(parsed)
    except json.JSONDecodeError:
        pass

    cid = row.get("config_id", "")
    paths = []
    if cid == "default":
        paths.append(ROOT / "larry_configs" / "config_default.json")
    elif cid:
        paths.append(ROOT / "larry_configs" / f"config_{cid}.json")
    for path in paths:
        if path.exists():
            try:
                return _clamp_config(load_json(path))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
    return None


def _seed_config() -> dict[str, Any]:
    best_path = ROOT / "larry_configs" / "best_config.json"
    if best_path.exists():
        try:
            parsed = load_json(best_path)
            if isinstance(parsed, dict) and parsed:
                return _clamp_config(parsed)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    return default_larry_config()


def _history_group_key(row: dict[str, str]) -> str:
    cid = row.get("config_id", "")
    run_id = row.get("run_id", "")
    shifted_round_id = row.get("round_id", "")
    if not run_id and shifted_round_id.startswith("20") and "_" in shifted_round_id:
        run_id = shifted_round_id
    return f"{cid}:{run_id or shifted_round_id}"


def _best_config_from_history(rows: list[dict[str, str]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        cid = row.get("config_id", "")
        if not cid or cid in ("fcfs_baseline", "default"):
            continue
        if row.get("success", "").lower() not in ("true", "1"):
            continue
        grouped.setdefault(_history_group_key(row), []).append(row)

    if not grouped:
        return _seed_config()

    max_rate_count = max(len({row.get("rate", "") for row in group}) for group in grouped.values())
    require_multi_rate = max_rate_count >= 2
    best_score = float("-inf")
    best_cfg: dict[str, Any] | None = None
    for group in grouped.values():
        rates = {row.get("rate", "") for row in group}
        if require_multi_rate and len(rates) < 2:
            continue
        if any(row.get("constraint_violation", "").lower() == "true" for row in group):
            continue
        cfg = None
        for row in group:
            cfg = _load_config_from_row(row)
            if cfg is not None:
                break
        if cfg is None:
            continue
        composite_scores = [
            _parse_float(row.get("composite_score"), 0.0)
            for row in group
            if row.get("composite_score") not in (None, "")
        ]
        if composite_scores:
            score = max(composite_scores)
        else:
            point_scores = [_parse_float(row.get("score"), 0.0) for row in group]
            score = sum(point_scores) / max(1, len(point_scores))
        if score > best_score:
            best_score = score
            best_cfg = cfg
    return best_cfg or _seed_config()


def _history_summary(rows: list[dict[str, str]], limit: int = 24) -> list[dict[str, Any]]:
    summary = []
    for row in rows[-limit:]:
        cid = row.get("config_id", "")
        if not cid or cid == "fcfs_baseline":
            continue
        cfg = _load_config_from_row(row) or {}
        summary.append(
            {
                "config_id": cid,
                "rate": row.get("rate", ""),
                "score": _parse_float(row.get("composite_score") or row.get("score"), 0.0),
                "constraint_violation": row.get("constraint_violation", ""),
                "request_throughput": _parse_float(row.get("request_throughput"), 0.0),
                "mean_ttft_ms": _parse_float(row.get("mean_ttft_ms"), 0.0),
                "p99_ttft_ms": _parse_float(row.get("p99_ttft_ms"), 0.0),
                "mean_tpot_ms": _parse_float(row.get("mean_tpot_ms"), 0.0),
                "p99_tpot_ms": _parse_float(row.get("p99_tpot_ms"), 0.0),
                "key_params": {k: cfg.get(k) for k in SEARCH_SPACE if k in cfg},
            }
        )
    return summary


def _fallback_plan(rows: list[dict[str, str]]) -> dict[str, Any]:
    recent_violations = [r for r in rows[-12:] if r.get("constraint_violation", "").lower() == "true"]
    ttft_pressure = any(_parse_float(r.get("p99_ttft_ratio"), 0.0) > 1.1 for r in recent_violations)
    if ttft_pressure:
        return {
            "slots": {
                "exploit_a": {
                    "directions": {"MIN_QUEUE": "increase", "PRESSURE_AMPLIFIER": "decrease", "ALPHA_BASE": "increase"},
                    "rationale": "Recent candidates violated p99 TTFT, so exploit a more conservative queue and lower pressure response.",
                },
                "exploit_b": {
                    "directions": {"SHORT_PREFILL_BOOST": "decrease", "CACHE_WEIGHT": "increase"},
                    "rationale": "Reduce short-prefill bias while preserving cache locality.",
                },
                "explore_a": {
                    "directions": {"MIN_QUEUE": "high", "ALPHA_BASE": "high", "PRESSURE_AMPLIFIER": "low"},
                    "rationale": "Explore the conservative high-queue region that previously looked safer.",
                },
                "explore_b": {
                    "directions": {"CACHE_PROBE_INTERVAL": "decrease", "CACHE_WEIGHT": "high"},
                    "rationale": "Explore stronger cache probing without increasing admission pressure.",
                },
            }
        }
    return {
        "slots": {
            "exploit_a": {
                "directions": {"MIN_QUEUE": "increase", "ALPHA_BASE": "increase"},
                "rationale": "Local exploitation around the current best with slightly more batching.",
            },
            "exploit_b": {
                "directions": {"PRESSURE_AMPLIFIER": "decrease", "CACHE_WEIGHT": "increase"},
                "rationale": "Local exploitation with reduced pressure sensitivity and better cache locality.",
            },
            "explore_a": {
                "directions": {"SHORT_PREFILL_BOOST": "toggle", "SHORT_PREFILL_THRESHOLD": "low"},
                "rationale": "Broadly test whether short-prefill bias helps this workload.",
            },
            "explore_b": {
                "directions": {"MIN_QUEUE": "high", "ALPHA_BASE": "high", "PRESSURE_AMPLIFIER": "low"},
                "rationale": "Broadly test the stable high-batching region.",
            },
        }
    }


def _llm_plan(round_id: int, count: int, rows: list[dict[str, str]], best: dict[str, Any]) -> dict[str, Any]:
    objective = load_json(ROOT / "configs" / "objective.json")
    system = (
        "You are an LLM serving scheduling researcher. Analyze the tuning history and choose "
        "which LARRY parameters should be exploited or explored next. Return JSON only. "
        "Do not emit full candidate configs; emit a search plan with directions."
    )
    prompt = json.dumps(
        {
            "round_id": round_id,
            "candidate_count": count,
            "fixed_candidate_roles": [
                "c1 current best replay",
                "c2 exploitation around best A",
                "c3 exploitation around best B",
                "c4 exploration A",
                "c5 exploration B",
                "c6 safety anchor",
            ],
            "search_space": SEARCH_SPACE,
            "objective": objective,
            "current_best_config": best,
            "recent_history_summary": _history_summary(rows),
            "required_schema": {
                "slots": {
                    slot: {
                        "directions": {"PARAM_NAME": "increase|decrease|toggle|high|low|keep"},
                        "rationale": "short reason tied to observed metrics",
                    }
                    for slot in PLAN_SLOTS
                }
            },
            "guidance": [
                "Pick parameters based on observed score, throughput, TTFT, TPOT, and constraint violations.",
                "Use exploitation slots for small local changes around the current best.",
                "Use exploration slots for broader but still plausible moves.",
                "Prefer MIN_QUEUE, ALPHA_BASE, PRESSURE_AMPLIFIER, CACHE_WEIGHT, and SHORT_PREFILL_* unless history justifies other parameters.",
                "For single-turn workloads, keep session/continuity/adaptive bonuses low unless there is evidence they help.",
            ],
        },
        indent=2,
    )
    response = GPTClient().complete(system, prompt, json_mode=True)
    return _sanitize_plan(response)


def _sanitize_plan(raw: dict[str, Any]) -> dict[str, Any]:
    slots = raw.get("slots", {}) if isinstance(raw, dict) else {}
    clean_slots = {}
    for slot in PLAN_SLOTS:
        item = slots.get(slot, {}) if isinstance(slots, dict) else {}
        directions = item.get("directions", {}) if isinstance(item, dict) else {}
        clean_dirs = {
            key: value
            for key, value in directions.items()
            if key in SEARCH_SPACE and value in ALLOWED_DIRECTIONS
        }
        clean_slots[slot] = {
            "directions": clean_dirs,
            "rationale": str(item.get("rationale", "")) if isinstance(item, dict) else "",
        }
    return {"slots": clean_slots}


def _move_numeric(value: Any, key: str, direction: str, strength: str) -> Any:
    lo, hi = SEARCH_SPACE[key]
    numeric = float(value)
    span = float(hi) - float(lo)
    local_step = 0.12 if strength == "exploit" else 0.28
    mult_step = 0.18 if strength == "exploit" else 0.45
    if direction == "increase":
        numeric = numeric * (1.0 + mult_step) if numeric > 0 else numeric + span * local_step
    elif direction == "decrease":
        numeric = numeric * max(0.1, 1.0 - mult_step)
    elif direction == "high":
        numeric = float(lo) + span * (0.78 if strength == "exploit" else 0.9)
    elif direction == "low":
        numeric = float(lo) + span * (0.22 if strength == "exploit" else 0.1)
    elif direction == "toggle":
        midpoint = float(lo) + span * 0.5
        numeric = float(lo) if numeric > midpoint else float(hi) * (0.25 if strength == "exploit" else 0.5)
    if isinstance(lo, int) and isinstance(hi, int):
        return int(round(numeric))
    return numeric


def _apply_plan(best: dict[str, Any], slot_plan: dict[str, Any], strength: str) -> dict[str, Any]:
    cfg = dict(best)
    directions = slot_plan.get("directions", {})
    for key, direction in directions.items():
        if direction == "keep" or key not in cfg:
            continue
        cfg[key] = _move_numeric(cfg[key], key, direction, strength)
    if strength == "explore":
        cfg.update(
            {
                "SESSION_PROGRESS_WEIGHT": min(int(cfg.get("SESSION_PROGRESS_WEIGHT", 0)), 4096),
                "CONTINUITY_BONUS": min(int(cfg.get("CONTINUITY_BONUS", 0)), 10000),
                "ADAPTIVE_BASE_BONUS": min(int(cfg.get("ADAPTIVE_BASE_BONUS", 5000)), 20000),
                "ADAPTIVE_MIN_BONUS": 0,
                "ADAPTIVE_MAX_BONUS": min(int(cfg.get("ADAPTIVE_MAX_BONUS", 50000)), 100000),
            }
        )
    return _clamp_config(cfg)


def _safety_anchor(best: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(best)
    cfg.update(
        {
            "MIN_QUEUE": 32,
            "ALPHA_BASE": max(80000, min(140000, int(float(best.get("ALPHA_BASE", 120000))))),
            "PRESSURE_AMPLIFIER": min(1.0, float(best.get("PRESSURE_AMPLIFIER", 1.0))),
            "SHORT_PREFILL_BOOST": 0,
            "SESSION_PROGRESS_WEIGHT": 0,
            "CONTINUITY_BONUS": 10000,
            "ADAPTIVE_BASE_BONUS": 5000,
            "ADAPTIVE_MIN_BONUS": 0,
            "ADAPTIVE_MAX_BONUS": 50000,
        }
    )
    return _clamp_config(cfg)


def _signature(cfg: dict[str, Any]) -> str:
    return json.dumps(cfg, sort_keys=True, separators=(",", ":"))


def _structured_candidates(round_id: int, count: int, rows: list[dict[str, str]], plan: dict[str, Any]) -> list[dict[str, Any]]:
    best = _best_config_from_history(rows)
    slots = plan.get("slots", {})
    specs = [
        ("best_replay", "Current best replay.", best),
        ("exploit_a", slots.get("exploit_a", {}), _apply_plan(best, slots.get("exploit_a", {}), "exploit")),
        ("exploit_b", slots.get("exploit_b", {}), _apply_plan(best, slots.get("exploit_b", {}), "exploit")),
        ("explore_a", slots.get("explore_a", {}), _apply_plan(best, slots.get("explore_a", {}), "explore")),
        ("explore_b", slots.get("explore_b", {}), _apply_plan(best, slots.get("explore_b", {}), "explore")),
        ("safety_anchor", "Conservative high-queue anchor for regression control.", _safety_anchor(best)),
    ]

    out = []
    seen: set[str] = set()
    for idx, (slot, plan_item, cfg) in enumerate(specs, start=1):
        cfg = _clamp_config(cfg)
        sig = _signature(cfg)
        if sig in seen and slot != "best_replay":
            cfg["ALPHA_BASE"] = min(SEARCH_SPACE["ALPHA_BASE"][1], int(float(cfg["ALPHA_BASE"]) * (1.0 + 0.05 * idx)))
            cfg = _clamp_config(cfg)
            sig = _signature(cfg)
        seen.add(sig)
        rationale = plan_item if isinstance(plan_item, str) else plan_item.get("rationale", "")
        out.append(
            {
                "config_id": f"r{round_id}_c{idx}",
                "slot": slot,
                "rationale": rationale,
                "config": cfg,
            }
        )

    while len(out) < count:
        idx = len(out) + 1
        cfg = dict(best)
        cfg["ALPHA_BASE"] = int(float(cfg.get("ALPHA_BASE", 10000)) * (1.0 + 0.08 * idx))
        cfg["MIN_QUEUE"] = int(cfg.get("MIN_QUEUE", 4)) + (idx % 3) - 1
        out.append(
            {
                "config_id": f"r{round_id}_c{idx}",
                "slot": "extra_exploit",
                "rationale": "Extra local perturbation because candidates-per-round exceeds six.",
                "config": _clamp_config(cfg),
            }
        )
    return out[:count]


def propose_candidates(round_id: int, count: int, results_csv: Path, use_llm: bool = True) -> list[dict[str, Any]]:
    history = read_csv(results_csv)
    best = _best_config_from_history(history)
    plan = _fallback_plan(history)
    if use_llm:
        try:
            plan = _llm_plan(round_id, count, history, best)
        except (LLMUnavailable, ValueError, KeyError, TypeError, json.JSONDecodeError):
            plan = _fallback_plan(history)
    return _structured_candidates(round_id, count, history, plan)
