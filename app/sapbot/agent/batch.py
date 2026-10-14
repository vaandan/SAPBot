"""Mass / batch runs from Excel (FSD 1.1, ADR B6).

Two modes:
  script - run a saved VBS script per row, parameters mapped from columns ({"AUFNR": "Order"}); deterministic.
  task   - run the agent loop per row with a task template ("Release process order {Order}") (API mode / local).
Row-by-row with a checkpoint file (resume after failure) and result columns written to a copy of the workbook in
the workspace output folder. On PRODUCTION one batch-level approval covers the batch (same script / template).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ..tools.executor import ToolExecutor

log = logging.getLogger(__name__)


@dataclass
class BatchSpec:
    excel: str
    mode: str                              # script | task
    script: str = ""
    param_map: dict[str, str] | None = None  # PARAM -> column
    task_template: str = ""
    sheet: str | int = 0
    system: str | None = None
    client: str | None = None
    name: str = ""


def _read(path: Path, sheet: str | int) -> pd.DataFrame:
    if path.suffix.lower() in (".csv", ".txt"):
        return pd.read_csv(path, dtype=str, sep=None, engine="python", encoding="utf-8-sig").fillna("")
    return pd.read_excel(path, sheet_name=sheet, dtype=str).fillna("")


def run_batch(ex: ToolExecutor, spec: BatchSpec, on_row: Callable[[int, dict[str, Any]], None] | None = None,
              stop_flag: Callable[[], bool] | None = None) -> dict[str, Any]:
    ws = ex.active_workspace()
    if ws is None:
        raise RuntimeError("no active workspace")
    if not ex.gov.feature("batch_runs", ws.id):
        raise PermissionError("feature 'batch_runs' is disabled")
    src = Path(spec.excel)
    df = _read(src, spec.sheet)
    name = spec.name or src.stem
    out_xlsx = ws.output_dir / f"{name}_sapbot_results.xlsx"
    ckpt = ws.output_dir / f"{name}_sapbot_checkpoint.json"
    state = json.loads(ckpt.read_text(encoding="utf-8")) if ckpt.exists() else {"done": {}}
    for col in ("SAPBOT_STATUS", "SAPBOT_MESSAGE", "SAPBOT_RESULT", "SAPBOT_RUN_ID"):
        if col not in df.columns:
            df[col] = ""
    for idx, saved in state["done"].items():
        i = int(idx)
        if i < len(df):
            for k, v in saved.items():
                df.at[i, k] = v

    # batch-level approval on production (ADR B6)
    sysm = ex._pick_system(ws, spec.system, spec.client)
    if sysm is None:
        raise RuntimeError("no SAP system of this workspace has an open logon")
    spec.system, spec.client = sysm.sid, sysm.client
    if sysm.is_prod and spec.mode != "script":
        raise PermissionError("batches on PRODUCTION must use an approved saved script (task-template batches would "
                              "let the model post freely under one approval)")
    if sysm.is_prod:
        d = ex.broker.request("batch", f"Batch run on PRODUCTION {sysm.sid}/{sysm.client}: {len(df)} rows, "
                              f"{spec.mode} {spec.script or spec.task_template}",
                              {"excel": str(src), "rows": len(df)}, username=ex.gov.username(), workspace=ws.id,
                              system=sysm.sid, client=sysm.client)
        if not d.approved:
            return {"ok": False, "error": "batch not approved", "rows": len(df)}
        ex.preapproved.add(spec.script if spec.mode == "script" else "*task*")

    processed = ok_rows = 0
    try:
        for i, row in df.iterrows():
            if str(i) in state["done"]:
                continue
            if stop_flag and stop_flag():
                break
            rec: dict[str, Any] = {}
            try:
                if spec.mode == "script":
                    params = {p: str(row[c]).strip() for p, c in (spec.param_map or {}).items()}
                    tb = ex.execute("task_begin", {"task": f"Batch {name} row {i + 1}: run {spec.script} with "
                                                           f"{params}", "system": spec.system,
                                                   "client": spec.client})
                    if not tb.ok:
                        raise RuntimeError(tb.error)
                    r = ex.execute("run_saved_script", {"reasoning": f"batch row {i + 1}", "script": spec.script,
                                                        "params_json": json.dumps(params)})
                    sb = r.status_bar
                    rec = {"SAPBOT_STATUS": "OK" if r.ok else "ERROR",
                           "SAPBOT_MESSAGE": (f"{sb.type}: {sb.text}" if sb.text else (r.error or "")),
                           "SAPBOT_RESULT": json.dumps((r.data or {}).get("results", {}), ensure_ascii=False),
                           "SAPBOT_RUN_ID": tb.data["run_id"]}
                    ex.execute("task_end", {"outcome": "success" if r.ok else "failure",
                                            "summary": rec["SAPBOT_MESSAGE"]})
                else:
                    from .loop import AgentLoop
                    task = spec.task_template.format(**{str(k): v for k, v in row.to_dict().items()})
                    lr = AgentLoop(ex).run(task, spec.system, spec.client)
                    rec = {"SAPBOT_STATUS": "OK" if lr.outcome == "success" else "ERROR",
                           "SAPBOT_MESSAGE": lr.summary[:500], "SAPBOT_RESULT": lr.quality,
                           "SAPBOT_RUN_ID": lr.run_id}
            except Exception as e:  # noqa: BLE001
                if ex.task:
                    ex.execute("task_end", {"outcome": "failure", "summary": str(e)})
                rec = {"SAPBOT_STATUS": "ERROR", "SAPBOT_MESSAGE": str(e)[:500], "SAPBOT_RESULT": "",
                       "SAPBOT_RUN_ID": ""}
            for k, v in rec.items():
                df.at[i, k] = v
            state["done"][str(i)] = rec
            ckpt.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            processed += 1
            ok_rows += rec["SAPBOT_STATUS"] == "OK"
            if on_row:
                on_row(int(i), rec)
    finally:
        ex.preapproved.discard(spec.script if spec.mode == "script" else "*task*")
        df.to_excel(out_xlsx, index=False)
    total_done = len(state["done"])
    return {"ok": True, "rows": len(df), "processed_now": processed, "ok_now": ok_rows, "done_total": total_done,
            "complete": total_done >= len(df), "results_file": str(out_xlsx), "checkpoint": str(ckpt)}
