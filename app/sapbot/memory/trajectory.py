"""Trajectory logging + quality gate (FSD 3.5.1-3.5.2, 3.5.5; ADR B1, B5).

Every task is a Run with steps (reasoning, tool, args, compact observation, status bar). On finish the quality
gate grades the run:
  gold   - write task: final status S + document number captured + read back in a display transaction
           read task : reported success, last status not E/A, data returned or output file written
           and no user abort / no refused approval
  silver - S but not verified (not used for training / RAG)
  failed - anything else
Gold runs are dual-written: <ws>/trajectories/gold_trajectories.jsonl (OpenAI tool-call format) AND the
per-client RAG store (FSD 3.5.5). Secrets are scrubbed; masked workspaces are masked before writing.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..security.masking import mask_obj, scrub_secrets
from ..types import now_iso, now_str

log = logging.getLogger(__name__)
DOC_NUM = re.compile(r"\b\d{5,12}\b")


@dataclass
class Step:
    n: int
    ts: str
    tool: str
    args: dict[str, Any]
    reasoning: str = ""
    ok: bool = True
    status_bar: dict[str, Any] = field(default_factory=dict)
    observation: str = ""          # compact text the model saw (truncated)
    transaction: str = ""
    policy: str = ""
    error: str = ""
    is_write: bool = False


@dataclass
class Run:
    run_id: str
    workspace: str
    user: str
    channel: str
    model: str
    task: str
    system: str = ""
    client: str = ""
    started: str = field(default_factory=now_iso)
    ended: str = ""
    steps: list[Step] = field(default_factory=list)
    outcome: str = ""             # success | failure | aborted
    summary: str = ""
    document_numbers: list[str] = field(default_factory=list)
    verified: bool = False
    expected_output: str = ""
    aborted: bool = False
    refused_approvals: int = 0
    quality: str = ""             # gold | silver | failed
    t_codes: list[str] = field(default_factory=list)
    escalated_from_local: bool = False

    @property
    def is_write_task(self) -> bool:
        return any(s.is_write for s in self.steps)

    @property
    def final_status(self) -> str:
        for s in reversed(self.steps):
            t = (s.status_bar or {}).get("type", "")
            if t:
                return t
        return ""


class TrajectoryLogger:
    """One logger per workspace. Thread-safe; runs are persisted after every step (crash-safe)."""

    def __init__(self, folder: Path, mask_fields: list[str] | None = None, masked: bool = False) -> None:
        self.folder = folder
        (self.folder / "runs").mkdir(parents=True, exist_ok=True)
        self.mask_fields = mask_fields or []
        self.masked = masked
        self._lock = threading.Lock()
        self.active: dict[str, Run] = {}

    # ---------------------------------------------------------------- lifecycle
    def begin(self, workspace: str, user: str, channel: str, model: str, task: str, system: str = "",
              client: str = "", expected_output: str = "") -> Run:
        run = Run(run_id=f"{now_iso().replace(':', '').replace('-', '')}_{uuid.uuid4().hex[:6]}",
                  workspace=workspace, user=user, channel=channel, model=model, task=self._clean(task),
                  system=system, client=client, expected_output=expected_output)
        with self._lock:
            self.active[run.run_id] = run
        self._persist(run)
        return run

    def add_step(self, run: Run, tool: str, args: dict[str, Any], reasoning: str, ok: bool,
                 status_bar: dict[str, Any] | None, observation: Any, transaction: str = "", policy: str = "",
                 error: str = "", is_write: bool = False) -> Step:
        obs = observation if isinstance(observation, str) else json.dumps(self._clean(observation),
                                                                           ensure_ascii=False, default=str)
        st = Step(n=len(run.steps) + 1, ts=now_str(), tool=tool, args=self._clean(args),
                  reasoning=self._clean(reasoning or "")[:2000], ok=ok, status_bar=status_bar or {},
                  observation=obs[:4000], transaction=transaction, policy=policy, error=(error or "")[:1000],
                  is_write=is_write)
        with self._lock:
            run.steps.append(st)
            if transaction and transaction not in run.t_codes and transaction not in ("SESSION_MANAGER", ""):
                run.t_codes.append(transaction)
            sb = status_bar or {}
            if sb.get("type") == "S":
                for p in list(sb.get("msg_params", [])) + DOC_NUM.findall(sb.get("text", "")):
                    if DOC_NUM.fullmatch(str(p)) and str(p) not in run.document_numbers:
                        run.document_numbers.append(str(p))
        self._persist(run)
        return st

    def finish(self, run: Run, outcome: str, summary: str = "", aborted: bool = False,
               display_tcodes: set[str] | None = None) -> Run:
        run.ended = now_iso()
        run.outcome = outcome
        run.summary = self._clean(summary)[:3000]
        run.aborted = aborted or outcome == "aborted"
        run.verified = self._verify(run, display_tcodes or set())
        run.quality = self.grade(run)
        with self._lock:
            self.active.pop(run.run_id, None)
        self._persist(run)
        if run.quality == "gold":
            self._append_gold(run)
        return run

    # ---------------------------------------------------------------- quality gate (B5)
    @staticmethod
    def _verify(run: Run, display_tcodes: set[str]) -> bool:
        if not run.is_write_task:
            return True
        if not run.document_numbers:
            return False
        last_write = max((s.n for s in run.steps if s.is_write), default=0)
        for s in run.steps:
            if s.n <= last_write or not s.ok:
                continue
            if s.transaction.upper() in display_tcodes and any(d in s.observation for d in run.document_numbers):
                return True
            if s.tool in ("run_saved_script", "sap_read_grid") and "read-only" in s.policy and \
                    any(d in s.observation for d in run.document_numbers):
                return True
        return False

    @staticmethod
    def grade(run: Run) -> str:
        if run.aborted or run.refused_approvals or run.outcome != "success" or not run.steps:
            return "failed"
        if run.final_status in ("E", "A"):
            return "failed"
        if run.is_write_task:
            if run.final_status != "S" and not run.document_numbers:
                return "failed"
            return "gold" if run.verified else "silver"
        if run.expected_output and not Path(run.expected_output).exists():
            return "silver"
        return "gold"

    # ---------------------------------------------------------------- persistence
    def _clean(self, obj: Any) -> Any:
        obj = scrub_secrets(obj)
        return mask_obj(obj, self.mask_fields) if self.masked else obj

    def _persist(self, run: Run) -> None:
        f = self.folder / "runs" / f"{run.run_id}.json"
        f.write_text(json.dumps(asdict(run), ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    def _append_gold(self, run: Run) -> None:
        with self._lock, open(self.folder / "gold_trajectories.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(to_openai_record(run), ensure_ascii=False, default=str) + "\n")

    def load(self, run_id: str) -> Run:
        d = json.loads((self.folder / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
        d["steps"] = [Step(**s) for s in d.get("steps", [])]
        return Run(**d)

    def list_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        out = []
        for f in sorted((self.folder / "runs").glob("*.json"), reverse=True)[:limit]:
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                out.append({k: d.get(k) for k in ("run_id", "started", "ended", "user", "channel", "model", "task",
                                                  "system", "client", "outcome", "quality", "t_codes",
                                                  "document_numbers")} | {"steps": len(d.get("steps", []))})
            except Exception:  # noqa: BLE001
                continue
        return out


def to_openai_record(run: Run, system_prompt: str = "", tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """FSD 3.5.1: system / user / assistant(thought + tool call) / tool(observation) messages."""
    msgs: list[dict[str, Any]] = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.append({"role": "user", "content": run.task})
    for s in run.steps:
        cid = f"call_{s.n}"
        args = {k: v for k, v in s.args.items() if k != "reasoning"}
        msgs.append({"role": "assistant", "content": s.reasoning,
                     "tool_calls": [{"id": cid, "type": "function",
                                     "function": {"name": s.tool, "arguments": json.dumps(args, ensure_ascii=False)}}]})
        msgs.append({"role": "tool", "tool_call_id": cid, "content": s.observation})
    msgs.append({"role": "assistant", "content": run.summary or "Done."})
    rec: dict[str, Any] = {"messages": msgs, "meta": {"run_id": run.run_id, "workspace": run.workspace,
                                                     "t_codes": run.t_codes, "final_status": run.final_status,
                                                     "documents": run.document_numbers, "model": run.model,
                                                     "quality": run.quality, "ended": run.ended}}
    if tools:
        rec["tools"] = tools
    return rec
