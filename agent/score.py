from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from agent.common import ROOT, load_json, read_csv, rewrite_csv


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def score_row(cur: dict[str, Any], base: dict[str, Any], objective: dict[str, Any]) -> tuple[float, bool, str]:
    score = 0.0
    violations: list[str] = []
    for metric, spec in objective["metrics"].items():
        cur_v = _safe_float(cur.get(metric))
        base_v = _safe_float(base.get(metric))
        if cur_v is None or base_v in (None, 0):
            continue
        weight = float(spec["weight"])
        if spec["direction"] == "lower_better":
            score += weight * (base_v - cur_v) / base_v
        elif spec["direction"] == "higher_better":
            score += weight * (cur_v - base_v) / base_v
        else:
            raise ValueError(f"Unknown direction for {metric}: {spec['direction']}")

    for metric, spec in objective.get("constraints", {}).items():
        cur_v = _safe_float(cur.get(metric))
        base_v = _safe_float(base.get(metric))
        if cur_v is None or base_v in (None, 0):
            continue
        max_ratio = spec.get("max_ratio_vs_baseline")
        if max_ratio is not None and cur_v / base_v > float(max_ratio):
            violations.append(f"{metric} ratio {cur_v / base_v:.3f} > {float(max_ratio):.3f}")
    return score, bool(violations), "; ".join(violations)


def score_rows(rows: list[dict[str, Any]], objective: dict[str, Any]) -> list[dict[str, Any]]:
    baseline_id = objective["baseline_config_id"]
    baselines = {r["rate"]: r for r in rows if r.get("config_id") == baseline_id}
    updated: list[dict[str, Any]] = []
    by_config: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        row = dict(row)
        if row.get("config_id") == baseline_id:
            row["score"] = "0.0"
            row["constraint_violation"] = "false"
            row["violation_reason"] = ""
        elif row.get("rate") in baselines:
            score, violated, reason = score_row(row, baselines[row["rate"]], objective)
            row["score"] = f"{score:.8f}"
            row["constraint_violation"] = str(violated).lower()
            row["violation_reason"] = reason
        updated.append(row)
        if row.get("config_id"):
            by_config[row["config_id"]].append(row)

    rate_weights = objective.get("rate_weights", {})
    composites: dict[str, float] = {}
    for cid, cfg_rows in by_config.items():
        weighted_sum = 0.0
        total_weight = 0.0
        for row in cfg_rows:
            score = _safe_float(row.get("score"))
            if score is None:
                continue
            weight = float(rate_weights.get(row.get("rate"), 1.0))
            weighted_sum += score * weight
            total_weight += weight
        if total_weight > 0:
            composites[cid] = weighted_sum / total_weight

    for row in updated:
        cid = row.get("config_id")
        if cid in composites:
            row["composite_score"] = f"{composites[cid]:.8f}"
    return updated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-csv", default=str(ROOT / "larry_results" / "all_runs.csv"))
    parser.add_argument("--objective", default=str(ROOT / "configs" / "objective.json"))
    parser.add_argument("--rewrite", action="store_true")
    args = parser.parse_args()

    rows = read_csv(Path(args.results_csv))
    scored = score_rows(rows, load_json(Path(args.objective)))
    if args.rewrite:
        rewrite_csv(Path(args.results_csv), scored)
    print(json.dumps(scored[-20:], indent=2))


if __name__ == "__main__":
    main()
