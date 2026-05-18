from __future__ import annotations

import csv
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def load_runtime(root: Path = ROOT) -> dict[str, Any]:
    runtime = load_json(root / "configs" / "runtime.json")
    runtime["agent_dir"] = str(Path(runtime.get("agent_dir", str(root))).expanduser())
    runtime["dataset"] = str(Path(runtime["dataset"]).expanduser())
    return runtime


def agent_dir(runtime: dict[str, Any]) -> Path:
    return Path(runtime["agent_dir"]).expanduser()


def ensure_dirs(runtime: dict[str, Any]) -> None:
    base = agent_dir(runtime)
    for rel in ("configs", "larry_configs", "larry_results", "server_logs", "agent"):
        (base / rel).mkdir(parents=True, exist_ok=True)


def conda_prefix(runtime: dict[str, Any]) -> list[str]:
    return [runtime.get("conda_executable", "conda"), "run", "-n", runtime["conda_env"]]


def run_command(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_csv(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_csv(path)
    fieldnames = list(row.keys())
    if existing:
        existing_fields = list(existing[0].keys())
        extra_fields = [key for key in row.keys() if key not in existing_fields]
        if extra_fields:
            fieldnames = existing_fields + extra_fields
            rows = [dict(r) for r in existing]
            rows.append(row)
            rewrite_csv(path, rows)
            return
        fieldnames = existing_fields
    write_header = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def rewrite_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def expand_api_key(raw: str | None) -> str | None:
    if not raw:
        return None
    if raw.startswith("${") and raw.endswith("}"):
        return os.environ.get(raw[2:-1])
    return raw


def default_larry_config(root: Path = ROOT) -> dict[str, Any]:
    return load_json(root / "larry_configs" / "config_default.json")


def fcfs_noop_config() -> dict[str, Any]:
    cfg = default_larry_config()
    cfg["MIN_QUEUE"] = 10**9
    return cfg
