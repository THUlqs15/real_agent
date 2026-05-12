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
        base["ADAPTIVE_MIN_BONUS"] = max(0, base["ADAPTIVE_BASE_BONUS"] // 2)
    if base["ADAPTIVE_BASE_BONUS"] > base["ADAPTIVE_MAX_BONUS"]:
        base["ADAPTIVE_MAX_BONUS"] = base["ADAPTIVE_BASE_BONUS"]
    if base["CONTINUITY_DECAY"] <= 0:
        base["CONTINUITY_DECAY"] = 60.0
    return base


def _best_config_from_history(rows: list[dict[str, str]]) -> dict[str, Any]:
    configs: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    for row in rows:
        cid = row.get("config_id", "")
        if not cid or cid == "fcfs_baseline":
            continue
        try:
            score = float(row.get("composite_score") or row.get("score") or "-inf")
        except ValueError:
            continue
        if cid not in scores or score > scores[cid]:
            scores[cid] = score
            try:
                configs[cid] = json.loads(row.get("config_json", "{}"))
            except json.JSONDecodeError:
                pass
    if not scores:
        return default_larry_config()
    return configs.get(max(scores, key=scores.get), default_larry_config())


def _heuristic_candidates(round_id: int, count: int, history: list[dict[str, str]]) -> list[dict[str, Any]]:
    best = _best_config_from_history(history)
    templates = [
        {"MIN_QUEUE": 16, "ALPHA_BASE": 60000, "PRESSURE_AMPLIFIER": 1.0},
        {"MIN_QUEUE": 24, "ALPHA_BASE": 90000, "PRESSURE_AMPLIFIER": 1.5},
        {"MIN_QUEUE": 32, "ALPHA_BASE": 120000, "PRESSURE_AMPLIFIER": 0.8},
        {"MIN_QUEUE": max(4, int(best["MIN_QUEUE"]) - 3), "ALPHA_BASE": int(best["ALPHA_BASE"] * 0.8)},
        {"MIN_QUEUE": min(32, int(best["MIN_QUEUE"]) + 3), "ALPHA_BASE": int(best["ALPHA_BASE"] * 1.2)},
        {"SHORT_PREFILL_BOOST": 50000, "SHORT_PREFILL_THRESHOLD": 4096},
    ]
    out = []
    for idx, patch in enumerate(templates[:count], start=1):
        cfg = dict(best)
        cfg.update(
            {
                "SESSION_PROGRESS_WEIGHT": 0,
                "CONTINUITY_BONUS": 0,
                "ADAPTIVE_BASE_BONUS": 5000,
                "ADAPTIVE_MIN_BONUS": 0,
                "ADAPTIVE_MAX_BONUS": 50000,
            }
        )
        cfg.update(patch)
        out.append(
            {
                "config_id": f"r{round_id}_c{idx}",
                "rationale": "Heuristic fallback candidate focused on MIN_QUEUE, ALPHA_BASE, and pressure sensitivity.",
                "config": _clamp_config(cfg),
            }
        )
    return out


def propose_candidates(round_id: int, count: int, results_csv: Path, use_llm: bool = True) -> list[dict[str, Any]]:
    history = read_csv(results_csv)
    objective = load_json(ROOT / "configs" / "objective.json")
    if not use_llm:
        return _heuristic_candidates(round_id, count, history)

    system = (
        "You are an LLM serving scheduling researcher. Propose valid LARRY v6 "
        "hyperparameter configs for the next tuning round. Return JSON only."
    )
    prompt = json.dumps(
        {
            "round_id": round_id,
            "candidate_count": count,
            "search_space": SEARCH_SPACE,
            "objective": objective,
            "default_config": default_larry_config(),
            "recent_history": history[-60:],
            "required_schema": {
                "candidates": [
                    {"config_id": "rN_cM", "rationale": "string", "config": "full LarryConfig object"}
                ]
            },
            "guidance": [
                "Prioritize MIN_QUEUE first.",
                "Use ALPHA_BASE >= 50000 during exploration unless testing a clear contrast.",
                "Zero continuity/session terms for single-turn workloads unless evidence suggests otherwise.",
                "Avoid violating MIN_BONUS < BASE_BONUS <= MAX_BONUS.",
            ],
        },
        indent=2,
    )
    try:
        response = GPTClient().complete(system, prompt, json_mode=True)
        raw_candidates = response.get("candidates", [])
        candidates = []
        for idx, item in enumerate(raw_candidates[:count], start=1):
            candidates.append(
                {
                    "config_id": item.get("config_id") or f"r{round_id}_c{idx}",
                    "rationale": item.get("rationale", ""),
                    "config": _clamp_config(item.get("config", {})),
                }
            )
        if candidates:
            return candidates
    except (LLMUnavailable, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass
    return _heuristic_candidates(round_id, count, history)
