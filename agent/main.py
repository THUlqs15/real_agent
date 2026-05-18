from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from agent.analyzer_agent import analyze_round
from agent.common import ROOT, agent_dir, ensure_dirs, load_json, load_runtime, write_json
from agent.optimizer_agent import propose_candidates
from agent.run_one import run_one
from agent.summarize import summarize


def _copy_candidate_config(base_dir: Path, config_id: str, config: dict) -> Path:
    path = base_dir / "larry_configs" / f"config_{config_id}.json"
    write_json(path, config)
    return path


def _archive_previous_results(base_dir: Path) -> Path | None:
    result_files = [
        base_dir / "larry_results" / "all_runs.csv",
        base_dir / "larry_results" / "agent_notes.md",
        base_dir / "result.md",
    ]
    result_files.extend(sorted((base_dir / "larry_results").glob("*.json")))
    existing = [path for path in result_files if path.exists()]
    if not existing:
        return None

    archive_dir = (
        base_dir
        / "larry_results"
        / "archive"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    archive_dir.mkdir(parents=True, exist_ok=True)
    for path in existing:
        target = archive_dir / path.name
        if target.exists():
            target = archive_dir / f"{path.stem}_{datetime.now(timezone.utc).strftime('%H%M%S')}{path.suffix}"
        shutil.move(str(path), str(target))
    return archive_dir


def _parse_float(raw: str | None, default: float = float("-inf")) -> float:
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _history_group_key(row: dict[str, str]) -> str:
    cid = row.get("config_id", "")
    run_id = row.get("run_id", "")
    shifted_round_id = row.get("round_id", "")
    if not run_id and shifted_round_id.startswith("20") and "_" in shifted_round_id:
        run_id = shifted_round_id
    return f"{cid}:{run_id or shifted_round_id}"


def _load_config_from_row(base_dir: Path, row: dict[str, str]) -> dict | None:
    raw = row.get("config_json") or ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed:
            return parsed
    except json.JSONDecodeError:
        pass

    cid = row.get("config_id", "")
    paths = []
    if cid == "default":
        paths.append(base_dir / "larry_configs" / "config_default.json")
    elif cid:
        paths.append(base_dir / "larry_configs" / f"config_{cid}.json")
    for path in paths:
        if path.exists():
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict) and parsed:
                    return parsed
            except (OSError, json.JSONDecodeError):
                continue
    return None


def _weighted_run_score(group: list[dict[str, str]], objective: dict) -> float:
    rate_weights = objective.get("rate_weights", {})
    weighted_sum = 0.0
    total_weight = 0.0
    for row in group:
        score = _parse_float(row.get("score"))
        if score == float("-inf"):
            continue
        weight = float(rate_weights.get(row.get("rate"), 1.0))
        weighted_sum += score * weight
        total_weight += weight
    if total_weight <= 0:
        return float("-inf")
    return weighted_sum / total_weight


def _save_best(base_dir: Path) -> None:
    rows_path = base_dir / "larry_results" / "all_runs.csv"
    if not rows_path.exists():
        return
    import csv

    with rows_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    objective_path = base_dir / "configs" / "objective.json"
    objective = load_json(objective_path if objective_path.exists() else ROOT / "configs" / "objective.json")
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        cid = row.get("config_id", "")
        if not cid or cid in ("fcfs_baseline", "default"):
            continue
        if row.get("success", "").lower() not in ("true", "1"):
            continue
        grouped.setdefault(_history_group_key(row), []).append(row)

    if not grouped:
        return

    max_rate_count = max(len({row.get("rate", "") for row in group}) for group in grouped.values())
    require_multi_rate = max_rate_count >= 2
    best_cfg: dict | None = None
    best_score = float("-inf")
    for group in grouped.values():
        rates = {row.get("rate", "") for row in group}
        if require_multi_rate and len(rates) < 2:
            continue
        if any(row.get("constraint_violation", "").lower() == "true" for row in group):
            continue
        cfg = None
        for row in group:
            cfg = _load_config_from_row(base_dir, row)
            if cfg is not None:
                break
        if cfg is None:
            continue
        score = _weighted_run_score(group, objective)
        if score > best_score:
            best_score = score
            best_cfg = cfg

    if best_cfg is not None:
        write_json(base_dir / "larry_configs" / "best_config.json", best_cfg)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--candidates-per-round", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--append-results", action="store_true")
    args = parser.parse_args()

    runtime = load_runtime()
    base_dir = agent_dir(runtime)
    ensure_dirs(runtime)

    if base_dir != ROOT:
        for rel in ("configs", "agent", "README.md", "design_doc_vllm.md"):
            src = ROOT / rel
            dst = base_dir / rel
            if src.is_file() and not dst.exists():
                shutil.copy2(src, dst)

    results_csv = base_dir / "larry_results" / "all_runs.csv"
    notes_path = base_dir / "larry_results" / "agent_notes.md"

    if not args.append_results:
        archive_dir = _archive_previous_results(base_dir)
        if archive_dir is not None:
            print(f"Archived previous results to {archive_dir}")

    if not args.skip_baseline:
        run_one("fcfs_baseline", "fcfs", runtime["rates"], dry_run=args.dry_run, round_id="0")

    default_cfg = ROOT / "larry_configs" / "config_default.json"
    if default_cfg.exists():
        run_one("default", str(default_cfg), runtime["rates"], dry_run=args.dry_run, round_id="0")

    for round_id in range(1, args.rounds + 1):
        candidates = propose_candidates(
            round_id,
            args.candidates_per_round,
            results_csv,
            use_llm=not args.no_llm,
        )
        for candidate in candidates:
            cfg_path = _copy_candidate_config(base_dir, candidate["config_id"], candidate["config"])
            run_one(
                candidate["config_id"],
                str(cfg_path),
                runtime["rates"],
                dry_run=args.dry_run,
                round_id=str(round_id),
            )
        analysis = analyze_round(round_id, results_csv, use_llm=not args.no_llm)
        with notes_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## Round {round_id}\n\n{analysis}\n")
        summarize(results_csv, base_dir / "result.md", notes_path)

    _save_best(base_dir)
    summarize(results_csv, base_dir / "result.md", notes_path)
    print(f"Results written to {base_dir / 'result.md'}")


if __name__ == "__main__":
    main()
