from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from agent.analyzer_agent import analyze_round
from agent.common import ROOT, agent_dir, ensure_dirs, load_runtime, write_json
from agent.optimizer_agent import propose_candidates
from agent.run_one import run_one
from agent.summarize import summarize


def _copy_candidate_config(base_dir: Path, config_id: str, config: dict) -> Path:
    path = base_dir / "larry_configs" / f"config_{config_id}.json"
    write_json(path, config)
    return path


def _save_best(base_dir: Path) -> None:
    rows_path = base_dir / "larry_results" / "all_runs.csv"
    if not rows_path.exists():
        return
    import csv

    with rows_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    candidates = [
        r for r in rows
        if r.get("config_id") not in ("", "fcfs_baseline") and r.get("constraint_violation") != "true"
    ]
    if not candidates:
        return
    best = max(candidates, key=lambda r: float(r.get("composite_score") or r.get("score") or "-999"))
    cfg = json.loads(best.get("config_json") or "{}")
    write_json(base_dir / "larry_configs" / "best_config.json", cfg)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--candidates-per-round", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--skip-baseline", action="store_true")
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
