"""Agent Core & ReAct loop (FSD 3.4) for API mode and the local model.

Reason -> Act -> Observe with:
  * turn budget (default 25) and token budget (ADR B13)
  * loop guard + policy + rollback live in the ToolExecutor (shared with Cowork mode, ADR B2)
  * RAG few-shot block injected between system prompt and the user task (FSD 3.6.4)
  * router (ADR B7): local model first only when allowed; escalate to Claude after N failed steps and record a
    DPO pair (local failed call = rejected, Claude's call = chosen; FSD 3.5.3)
  * event callback for real-time UI streaming (FSD 3.7.4)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import get_settings
from ..tools.executor import ToolExecutor
from .llm import ClaudeLLM, OpenAICompatLLM, ToolCall, Turn
from .router import choose_brain

log = logging.getLogger(__name__)
Event = dict[str, Any]
EXCLUDED_TOOLS = {"task_begin", "task_end"}   # the loop owns the task lifecycle

SYSTEM_PROMPT = """You are SAPBot, an expert SAP consultant operating SAP GUI for {user} at client "{ws_name}".
System: {system}/{client} ({env}, {mode}). User date format: DD.MM.YYYY (SAPBot converts automatically).

How to work:
- Observe before you act on an unfamiliar screen; use field ids exactly as returned by sap_observe_screen.
- sap_act order: ok_code, field updates, click, vkey. Send navigation (/nTCODE) on its own.
- After every action read the status bar (S/W/E/A/I + message class/number). An empty status bar is not proof.
- Prefer proven library scripts (list_saved_scripts / run_saved_script) for routine steps.
- For postings: after saving, read the document back in a display transaction (VA03, ME23N, COR3, FB03, MB03 ...).
- Always pass a short 'reasoning' on every SAP tool call.
- If SAPBot refuses an action, do not look for a workaround; explain what is needed (approval, grant, setting).
- When finished, reply with a short summary (what was done, document numbers, open points) and no tool call.
"""


@dataclass
class LoopResult:
    run_id: str = ""
    outcome: str = ""
    quality: str = ""
    summary: str = ""
    turns: int = 0
    tokens: int = 0
    brain: str = ""
    escalated: bool = False
    needs_human: bool = False
    events: list[Event] = field(default_factory=list)


class AgentLoop:
    def __init__(self, executor: ToolExecutor, on_event: Callable[[Event], None] | None = None,
                 force_brain: str = "", llm_factory: Callable[[str], Any] | None = None) -> None:
        self.ex = executor
        self.s = get_settings()
        self.on_event = on_event
        self.force_brain = force_brain
        self.llm_factory = llm_factory    # tests / custom brains

    # ----------------------------------------------------------------
    def _emit(self, res: LoopResult, ev: Event) -> None:
        res.events.append(ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:  # noqa: BLE001
                pass

    def _make_llm(self, brain: str) -> Any:
        if self.llm_factory is not None:
            return self.llm_factory(brain)
        gov = self.ex.gov
        if brain == "local":
            lm = self.s.models.local_model
            return OpenAICompatLLM(lm.base_url, lm.model, timeout=lm.timeout_seconds, exclude_tools=EXCLUDED_TOOLS)
        base_url, key = "", ""
        if gov.hub_mode and self.s.hub.url:
            from ..hub.client import HubClient
            hc = HubClient.from_settings(gov)
            base_url, key = hc.anthropic_proxy_url(), hc.token
        return ClaudeLLM(self.s.models.claude_model, api_key=key, base_url=base_url, exclude_tools=EXCLUDED_TOOLS)

    def run(self, task: str, system: str | None = None, client: str | None = None,
            expected_output: str = "") -> LoopResult:
        prev_channel, prev_model = self.ex.channel, self.ex.model
        try:
            return self._run(task, system, client, expected_output)
        finally:
            if self.ex.task is not None:   # never leave a task open (crash in the middle)
                self.ex.execute("task_end", {"outcome": "failure", "summary": "agent loop ended unexpectedly"})
            self.ex.channel, self.ex.model = prev_channel, prev_model

    def _run(self, task: str, system: str | None, client: str | None, expected_output: str) -> LoopResult:
        res = LoopResult()
        ws = self.ex.active_workspace()
        if ws is None:
            raise RuntimeError("no active client workspace (sapbot workspace use <id>)")
        brain = self.force_brain or choose_brain(self.ex, ws, task)
        res.brain = brain
        self.ex.channel = "local" if brain == "local" else "claude_api"
        llm = self._make_llm(brain)
        self.ex.model = getattr(llm, "model", brain)
        tb = self.ex.execute("task_begin", {"task": task, "system": system, "client": client,
                                            "expected_output": expected_output})
        if not tb.ok:
            res.outcome, res.summary, res.needs_human = "failure", tb.error or "", tb.needs_human
            self._emit(res, {"type": "error", "text": tb.error})
            return res
        d = tb.data
        res.run_id = d["run_id"]
        self._emit(res, {"type": "task_begin", "data": d, "brain": brain})
        sysm = ws.find_system(d["system"], d["client"])
        system_prompt = SYSTEM_PROMPT.format(user=self.ex.gov.username(), ws_name=ws.name, system=d["system"],
                                             client=d["client"], env=sysm.env if sysm else "?", mode=d["mode"])
        if d.get("examples") and "No similar" not in d["examples"]:
            system_prompt += "\n\n" + d["examples"]          # FSD 3.6.4 augmentation
        llm.start(system_prompt, task)
        failed_local = 0
        pending_dpo: dict[str, Any] | None = None
        outcome = "failure"
        for turn_no in range(1, self.s.agent.max_turns + 1):
            res.turns = turn_no
            try:
                turn: Turn = llm.step()
            except Exception as e:  # noqa: BLE001
                self._emit(res, {"type": "error", "text": f"model call failed: {e}"})
                res.summary = f"model call failed: {e}"
                break
            res.tokens += turn.input_tokens + turn.output_tokens
            if turn.text:
                self._emit(res, {"type": "thought", "text": turn.text})
            if pending_dpo is not None and turn.tool_calls:
                pending_dpo["chosen"] = {"name": turn.tool_calls[0].name, "args": turn.tool_calls[0].args}
                self._save_dpo(ws, pending_dpo)
                pending_dpo = None
            if not turn.tool_calls:
                res.summary = turn.text.strip()
                outcome = "success"
                break
            results = []
            for tc in turn.tool_calls:
                tr = self._call_tool(res, tc, turn.text)
                results.append((tc.id, tr["content"], not tr["ok"]))
                if tr["needs_human"]:
                    res.needs_human = True
                if brain == "local" and not tr["ok"] and not tr["policy_refusal"]:
                    failed_local += 1
                    if failed_local >= self.s.router.escalate_after_failed_steps and \
                            ws.data_policy != "local-only" and self.ex.gov.feature("claude_api_mode", ws.id):
                        pending_dpo = {"prompt": task, "context": self._context_text(llm),
                                       "rejected": {"name": tc.name, "args": tc.args, "error": tr["content"][:500]}}
            llm.add_tool_results(results)
            if res.needs_human:
                res.summary = "Paused: SAPBot needs a human decision (see the last tool result)."
                outcome = "failure"
                break
            if pending_dpo is not None:                           # escalate local -> Claude mid-run
                brain = "claude"
                res.escalated = True
                self.ex.channel = "claude_api"
                done_so_far = self._context_text(llm)
                llm = self._make_llm("claude")
                self.ex.model = getattr(llm, "model", "claude")
                llm.start(system_prompt, f"{task}\n\nA smaller model started this task and got stuck. Steps so "
                                         f"far (SAP state is live, re-observe first):\n{done_so_far[-6000:]}")
                self._emit(res, {"type": "escalated", "text": "Escalated to Claude after failed local steps"})
                if self.ex.task:
                    self.ex.task.run.escalated_from_local = True
            if res.tokens > self.s.agent.max_tokens_per_run:
                res.summary = f"Stopped: token budget {self.s.agent.max_tokens_per_run} exceeded"
                break
        else:
            res.summary = f"Stopped: turn budget of {self.s.agent.max_turns} reached"
            res.needs_human = True
        end = self.ex.execute("task_end", {"outcome": outcome, "summary": res.summary})
        res.outcome = outcome
        res.quality = (end.data or {}).get("quality", "") if end.ok else ""
        self._emit(res, {"type": "final", "outcome": outcome, "quality": res.quality, "summary": res.summary,
                         "turns": res.turns, "tokens": res.tokens})
        return res

    def _call_tool(self, res: LoopResult, tc: ToolCall, thought: str) -> dict[str, Any]:
        args = dict(tc.args)
        if tc.name not in ("sapbot_status", "list_saved_scripts", "request_elevation") and not args.get("reasoning"):
            args["reasoning"] = (thought or "").strip()[:500]
        self._emit(res, {"type": "tool_call", "name": tc.name, "args": args})
        if tc.name in EXCLUDED_TOOLS:
            out = {"ok": False, "error": f"{tc.name} is handled by the agent loop"}
            tr_ok, needs, refused = False, False, False
        else:
            r = self.ex.execute(tc.name, args)
            out = r.model_dump(exclude_none=True)
            tr_ok, needs = r.ok, r.needs_human
            refused = bool(r.policy and r.policy.get("verdict") in ("deny", "needs_approval")) and not r.ok
        content = json.dumps(out, ensure_ascii=False, default=str)
        if len(content) > 60_000:
            content = content[:60_000] + "...(truncated)"
        self._emit(res, {"type": "tool_result", "name": tc.name, "ok": tr_ok, "result": out})
        return {"content": content, "ok": tr_ok, "needs_human": needs, "policy_refusal": refused}

    @staticmethod
    def _context_text(llm: Any) -> str:
        parts = []
        for m in getattr(llm, "messages", [])[1:]:
            c = m.get("content")
            parts.append(f"{m.get('role')}: {c if isinstance(c, str) else json.dumps(c, default=str)[:1500]}")
            if m.get("tool_calls"):
                parts.append(json.dumps(m["tool_calls"], default=str)[:1500])
        return "\n".join(parts)

    def _save_dpo(self, ws: Any, pair: dict[str, Any]) -> None:
        f = ws.trajectories_dir / "dpo_pairs.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(pair, ensure_ascii=False, default=str) + "\n")
