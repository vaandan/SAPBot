"""Distillation data export (FSD 3.5.3-3.5.4). Dormant until the 'distillation' feature is enabled.

export_training() collects, per workspace (or all), the gold runs (OpenAI tool-call JSONL, with tool schemas and
the system prompt) and DPO pairs into one folder that the QLoRA pipeline (learning/train_qlora.py, GPU box)
consumes. Client data stays separated by workspace id in the metadata; masked workspaces were already masked
when logged. Only Admins can export.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from .. import paths
from .. import workspaces as wsmod
from ..agent.loop import SYSTEM_PROMPT
from ..memory.trajectory import TrajectoryLogger, to_openai_record
from ..security.governance import Governance
from ..tools.schemas import openai_tools

log = logging.getLogger(__name__)


def export_training(gov: Governance, workspace: str | None, out: Path | None) -> dict[str, Any]:
    if not gov.is_admin():
        raise PermissionError("Admin role required")
    wss = [wsmod.load(workspace)] if workspace else wsmod.list_all()
    out = out or (paths.data_dir() / "training" / dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    tools = openai_tools()
    sft, dpo, skipped = 0, 0, []
    with open(out / "sft_gold.jsonl", "w", encoding="utf-8") as f_sft, \
            open(out / "dpo_pairs.jsonl", "w", encoding="utf-8") as f_dpo:
        for ws in wss:
            if not gov.feature("distillation", ws.id):
                skipped.append(ws.id)
                continue
            if ws.data_policy == "local-only":
                log.info("%s is local-only: exported for on-prem training only", ws.id)
            lg = TrajectoryLogger(ws.trajectories_dir)
            for r in lg.list_runs(100_000):
                if r.get("quality") != "gold":
                    continue
                run = lg.load(r["run_id"])
                sysm = ws.find_system(run.system, run.client)
                sp = SYSTEM_PROMPT.format(user="consultant", ws_name=ws.name, system=run.system, client=run.client,
                                          env=sysm.env if sysm else "?", mode="write" if sysm and sysm.write else
                                          "read-only")
                rec = to_openai_record(run, sp, tools)
                rec["meta"]["workspace"] = ws.id
                f_sft.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                sft += 1
            p = ws.trajectories_dir / "dpo_pairs.jsonl"
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    d = json.loads(line)
                    if d.get("chosen") and d.get("rejected"):
                        d["workspace"] = ws.id
                        f_dpo.write(json.dumps(d, ensure_ascii=False) + "\n")
                        dpo += 1
    gov.store.audit(gov.username(), workspace or "*", "TRAINING_EXPORT", {"sft": sft, "dpo": dpo, "out": str(out)})
    return {"folder": str(out), "sft_examples": sft, "dpo_pairs": dpo,
            "skipped_workspaces_distillation_off": skipped,
            "next": "copy the folder to the GPU machine and run: python -m sapbot.learning.train_qlora --data <folder>"}
