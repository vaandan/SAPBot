"""Model adapters with one interface (FSD 3.4.1 unified tool registry).

  ClaudeLLM        Anthropic Messages API (API mode, ADR A16). Key from Windows Credential Manager / env, or the
                   Hub proxy (key never on the laptop).
  OpenAICompatLLM  Any OpenAI-compatible /chat/completions endpoint with tool calling: vLLM or Ollama serving the
                   small model (Qwen-2.5-14B/32B). Dormant until the 'local_model' feature is enabled (ADR E1).
Each adapter keeps its own native message history.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..tools.schemas import anthropic_tools, openai_tools

log = logging.getLogger(__name__)
KEYRING_SERVICE = "sapbot"


def get_secret(name: str, env: str = "") -> str:
    """Windows Credential Manager via keyring (ADR B10); env var fallback for CI."""
    if env and os.environ.get(env):
        return os.environ[env]
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, name) or ""
    except Exception:  # noqa: BLE001
        return ""


def set_secret(name: str, value: str) -> None:
    import keyring
    keyring.set_password(KEYRING_SERVICE, name, value)


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Turn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""


def _filter_tools(tools: list[dict[str, Any]], exclude: set[str]) -> list[dict[str, Any]]:
    out = []
    for t in tools:
        name = t.get("name") or t.get("function", {}).get("name")
        if name not in exclude:
            out.append(t)
    return out


class ClaudeLLM:
    name = "claude"

    def __init__(self, model: str, api_key: str = "", base_url: str = "", exclude_tools: set[str] | None = None,
                 max_tokens: int = 4096) -> None:
        import anthropic
        key = api_key or get_secret("anthropic_api_key", "ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("No Claude API key: run 'sapbot secret set anthropic_api_key' (stored in Windows "
                               "Credential Manager) or enable the Hub proxy")
        kw: dict[str, Any] = {"api_key": key}
        if base_url:
            kw["base_url"] = base_url
        from ..config import ca_bundle
        if ca_bundle():
            kw["http_client"] = anthropic.DefaultHttpxClient(verify=ca_bundle())
        self.client = anthropic.Anthropic(**kw)
        self.model = model
        self.max_tokens = max_tokens
        self.tools = _filter_tools(anthropic_tools(), exclude_tools or set())
        self.system = ""
        self.messages: list[dict[str, Any]] = []

    def start(self, system: str, user: str) -> None:
        self.system = system
        self.messages = [{"role": "user", "content": user}]

    def step(self) -> Turn:
        resp = self.client.messages.create(model=self.model, system=self.system, messages=self.messages,
                                           tools=self.tools, max_tokens=self.max_tokens)
        content: list[dict[str, Any]] = []
        for b in resp.content:
            if b.type == "text":
                content.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        self.messages.append({"role": "assistant", "content": content or [{"type": "text", "text": "."}]})
        t = Turn(stop_reason=str(resp.stop_reason), input_tokens=resp.usage.input_tokens,
                 output_tokens=resp.usage.output_tokens)
        for b in resp.content:
            if b.type == "text":
                t.text += b.text
            elif b.type == "tool_use":
                t.tool_calls.append(ToolCall(b.id, b.name, dict(b.input or {})))
        return t

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": cid, "content": content, "is_error": is_err}
            for cid, content, is_err in results]})

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})


class OpenAICompatLLM:
    name = "local"

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: int = 120,
                 exclude_tools: set[str] | None = None) -> None:
        if not base_url:
            raise RuntimeError("local model endpoint not configured (Settings > Features > Local model)")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.headers = {"Authorization": f"Bearer {api_key or get_secret('local_model_api_key') or 'none'}"}
        self.timeout = timeout
        self.tools = _filter_tools(openai_tools(), exclude_tools or set())
        self.messages: list[dict[str, Any]] = []

    def start(self, system: str, user: str) -> None:
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def step(self) -> Turn:
        body = {"model": self.model, "messages": self.messages, "tools": self.tools, "tool_choice": "auto",
                "temperature": 0.1}
        r = httpx.post(self.url, json=body, headers=self.headers, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        self.messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        usage = data.get("usage", {})
        t = Turn(text=msg.get("content") or "", stop_reason=data["choices"][0].get("finish_reason", ""),
                 input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0))
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": tc["function"].get("arguments")}
            t.tool_calls.append(ToolCall(tc.get("id", f"call_{len(t.tool_calls)}"), tc["function"]["name"], args))
        return t

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        for cid, content, _ in results:
            self.messages.append({"role": "tool", "tool_call_id": cid, "content": content})

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    @staticmethod
    def test_connection(base_url: str, model: str, api_key: str = "") -> dict[str, Any]:
        """Admin 'Test connection' button (ADR E1)."""
        try:
            r = httpx.get(base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {api_key or 'none'}"},
                          timeout=15)
            r.raise_for_status()
            ids = [m.get("id") for m in r.json().get("data", [])]
            return {"ok": model in ids or not ids, "models": ids[:20],
                    "note": "" if model in ids else f"model '{model}' not listed by the endpoint"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
