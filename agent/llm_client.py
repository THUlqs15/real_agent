from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent.common import ROOT, expand_api_key, load_json


class LLMUnavailable(RuntimeError):
    pass


class GPTClient:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or ROOT / "configs" / "llm.json"
        self.config = load_json(self.config_path)
        if self.config.get("provider") != "openai":
            raise ValueError("Only provider=openai is supported")
        self.api_key = expand_api_key(self.config.get("api_key"))
        if not self.api_key:
            raise LLMUnavailable("OpenAI API key is not configured")

    def complete(self, system_prompt: str, user_prompt: str, json_mode: bool = False) -> Any:
        payload: dict[str, Any] = {
            "model": self.config.get("model", "gpt-4.1"),
            "temperature": self.config.get("temperature", 0.2),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self.config.get("max_output_tokens"):
            payload["max_tokens"] = int(self.config["max_output_tokens"])
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        timeout = int(self.config.get("timeout_seconds", 120))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise LLMUnavailable(f"OpenAI request failed: {exc}") from exc

        obj = json.loads(raw)
        content = obj["choices"][0]["message"]["content"]
        return json.loads(content) if json_mode else content
