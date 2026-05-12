from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ALIASES = {
    "duration": ["duration", "benchmark_duration_s", "elapsed_time"],
    "request_throughput": ["request_throughput", "requests_per_second"],
    "output_throughput": ["output_throughput", "output_tokens_per_second"],
    "mean_ttft_ms": ["mean_ttft_ms", "avg_ttft_ms"],
    "p99_ttft_ms": ["p99_ttft_ms"],
    "mean_tpot_ms": ["mean_tpot_ms", "avg_tpot_ms"],
    "p99_tpot_ms": ["p99_tpot_ms"],
}


def _get_metric(raw: dict[str, Any], key: str) -> float | None:
    for alias in ALIASES[key]:
        if alias in raw and raw[alias] is not None:
            return float(raw[alias])
    percentiles = raw.get("percentiles") or raw.get("metrics") or {}
    if key in percentiles and percentiles[key] is not None:
        return float(percentiles[key])
    return None


def parse_metrics(raw_path: Path, config_id: str, rate: str) -> dict[str, Any]:
    with raw_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    row: dict[str, Any] = {
        "config_id": config_id,
        "rate": rate,
        "success": True,
        "error": "",
        "raw_result_path": str(raw_path),
    }
    for key in ALIASES:
        row[key] = _get_metric(raw, key)
    if row["duration"] is None:
        row["success"] = False
        row["error"] = "missing duration; benchmark JSON shape may have changed"
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", required=True)
    parser.add_argument("--config-id", required=True)
    parser.add_argument("--rate", required=True)
    parser.add_argument("--out")
    args = parser.parse_args()
    row = parse_metrics(Path(args.raw), args.config_id, args.rate)
    data = json.dumps(row, indent=2)
    if args.out:
        Path(args.out).write_text(data + "\n", encoding="utf-8")
    else:
        print(data)


if __name__ == "__main__":
    main()
