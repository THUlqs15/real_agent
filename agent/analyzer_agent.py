from __future__ import annotations

import json
from pathlib import Path

from agent.common import ROOT, load_json, read_csv
from agent.llm_client import GPTClient, LLMUnavailable


def analyze_round(round_id: int, results_csv: Path, use_llm: bool = True) -> str:
    rows = read_csv(results_csv)
    recent = [r for r in rows if r.get("round_id") == str(round_id)]
    if not recent:
        return f"Round {round_id}: no rows were recorded."

    ranked = sorted(
        recent,
        key=lambda r: float(r.get("composite_score") or r.get("score") or "-999"),
        reverse=True,
    )
    fallback = [
        f"Round {round_id} analysis:",
        f"- Best observed row: {ranked[0].get('config_id')} at rate {ranked[0].get('rate')} with score {ranked[0].get('score')}.",
        "- Check constraint_violation and server logs before trusting the ranking.",
    ]
    if not use_llm:
        return "\n".join(fallback)

    system = (
        "You analyze LLM serving scheduler tuning experiments. Be concise, "
        "focus on metric tradeoffs, constraint violations, and next search moves."
    )
    prompt = json.dumps(
        {
            "round_id": round_id,
            "objective": load_json(ROOT / "configs" / "objective.json"),
            "recent_rows": recent,
            "top_rows": ranked[:10],
            "questions": [
                "Which configs look best and why?",
                "Were any improvements bought by unacceptable tail latency?",
                "Which parameters should be explored next?",
            ],
        },
        indent=2,
    )
    try:
        return str(GPTClient().complete(system, prompt, json_mode=False))
    except LLMUnavailable:
        return "\n".join(fallback)
