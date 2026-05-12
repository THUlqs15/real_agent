from __future__ import annotations

import argparse
from pathlib import Path

from agent.common import ROOT, load_runtime, read_csv


def summarize(results_csv: Path, out_path: Path, notes_path: Path | None = None) -> None:
    runtime = load_runtime()
    rows = read_csv(results_csv)
    baselines = [r for r in rows if r.get("config_id") == "fcfs_baseline"]
    ranked = sorted(
        [r for r in rows if r.get("config_id") != "fcfs_baseline"],
        key=lambda r: float(r.get("composite_score") or r.get("score") or "-999"),
        reverse=True,
    )
    lines = [
        "# LARRYSmith Real-vLLM Optimization Results",
        "",
        "## Environment",
        f"- Dataset: {runtime['dataset']}",
        f"- vLLM project: {runtime['vllm_project']}",
        f"- Model: {runtime['model']}",
        f"- Rates: {', '.join(runtime['rates'])}",
        f"- Num prompts: {runtime['num_prompts']}",
        "",
        "## FCFS Baseline",
        "| Rate | duration | mean_ttft_ms | p99_ttft_ms | mean_tpot_ms | p99_tpot_ms | request_throughput |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in baselines:
        lines.append(
            f"| {r.get('rate')} | {r.get('duration')} | {r.get('mean_ttft_ms')} | {r.get('p99_ttft_ms')} | "
            f"{r.get('mean_tpot_ms')} | {r.get('p99_tpot_ms')} | {r.get('request_throughput')} |"
        )
    lines += [
        "",
        "## Ranked Candidates",
        "| Config | Rate | Score | Composite | Constraint | Violation |",
        "|---|---|---:|---:|---|---|",
    ]
    for r in ranked[:50]:
        lines.append(
            f"| {r.get('config_id')} | {r.get('rate')} | {r.get('score')} | {r.get('composite_score')} | "
            f"{r.get('constraint_violation')} | {r.get('violation_reason', '')} |"
        )
    if notes_path and notes_path.exists():
        lines += ["", "## Agent Notes", "", notes_path.read_text(encoding="utf-8")]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-csv", default=str(ROOT / "larry_results" / "all_runs.csv"))
    parser.add_argument("--out", default=str(ROOT / "result.md"))
    parser.add_argument("--notes")
    args = parser.parse_args()
    summarize(Path(args.results_csv), Path(args.out), Path(args.notes) if args.notes else None)


if __name__ == "__main__":
    main()
