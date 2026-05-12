from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent.common import (
    ROOT,
    agent_dir,
    append_csv,
    atomic_write_json,
    conda_prefix,
    default_larry_config,
    fcfs_noop_config,
    load_json,
    load_runtime,
    read_csv,
    run_command,
    rewrite_csv,
)
from agent.parse_metrics import parse_metrics
from agent.score import score_rows


def _load_candidate(config_json: str) -> dict[str, Any]:
    if config_json == "fcfs":
        return fcfs_noop_config()
    cfg = default_larry_config()
    cfg.update(load_json(Path(config_json)))
    return cfg


def _benchmark_cmd(
    runtime: dict[str, Any],
    rate: str,
    result_filename: str,
    num_prompts: int | None = None,
) -> list[str]:
    base = f"http://{runtime['server_host']}:{runtime['server_port']}"
    return (
        conda_prefix(runtime)
        + [
            "vllm",
            "bench",
            "serve",
            "--backend",
            "vllm",
            "--model",
            runtime["model"],
            "--base-url",
            base,
            "--endpoint",
            "/v1/completions",
            "--dataset-name",
            "sharegpt",
            "--dataset-path",
            runtime["dataset"],
            "--num-prompts",
            str(num_prompts or runtime["num_prompts"]),
            "--request-rate",
            rate,
            "--temperature",
            "0",
            "--disable-tqdm",
            "--save-result",
            "--result-dir",
            str(agent_dir(runtime) / "larry_results"),
            "--result-filename",
            result_filename,
        ]
    )


def _safe_name(value: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in value)


def _new_run_id(config_id: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{_safe_name(config_id)}_{uuid4().hex[:8]}"


def run_one(
    config_id: str,
    config_json: str,
    rates: list[str],
    dry_run: bool = False,
    round_id: str = "",
    run_id: str | None = None,
    num_prompts: int | None = None,
) -> list[dict[str, Any]]:
    runtime = load_runtime()
    base_dir = agent_dir(runtime)
    results_dir = base_dir / "larry_results"
    run_id = run_id or _new_run_id(config_id)
    cfg = _load_candidate(config_json)
    active = base_dir / "larry_configs" / "active.json"
    atomic_write_json(active, cfg)
    time.sleep(float(runtime.get("reload_wait_seconds", 3)))

    rows: list[dict[str, Any]] = []
    for rate in rates:
        result_name = f"{run_id}_rate{_safe_name(rate)}.json"
        raw_path = results_dir / result_name
        if dry_run:
            synthetic = {
                "duration": 100.0,
                "request_throughput": 5.0,
                "output_throughput": 1000.0,
                "mean_ttft_ms": 500.0,
                "p99_ttft_ms": 2500.0,
                "mean_tpot_ms": 30.0,
                "p99_tpot_ms": 90.0,
            }
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps(synthetic, indent=2) + "\n", encoding="utf-8")
        else:
            proc = run_command(_benchmark_cmd(runtime, rate, result_name, num_prompts), cwd=base_dir)
            if proc.returncode != 0:
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(
                    json.dumps({"duration": None, "error": proc.stdout[-4000:]}, indent=2) + "\n",
                    encoding="utf-8",
                )
        row = parse_metrics(raw_path, config_id, rate)
        row["run_id"] = run_id
        row["round_id"] = round_id
        row["config_json"] = json.dumps(cfg, sort_keys=True)
        append_csv(results_dir / "all_runs.csv", row)
        rows.append(row)

    all_rows = score_rows(read_csv(results_dir / "all_runs.csv"), load_json(ROOT / "configs" / "objective.json"))
    rewrite_csv(results_dir / "all_runs.csv", all_rows)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-id", required=True)
    parser.add_argument("--config-json", required=True)
    parser.add_argument("--rates", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--round-id", default="")
    parser.add_argument("--run-id")
    parser.add_argument("--num-prompts", type=int)
    args = parser.parse_args()
    runtime = load_runtime()
    rates = args.rates.split(",") if args.rates else runtime["rates"]
    rows = run_one(
        args.config_id,
        args.config_json,
        rates,
        args.dry_run,
        args.round_id,
        args.run_id,
        args.num_prompts,
    )
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
