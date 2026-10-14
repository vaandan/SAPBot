"""Standardized Tool Execution Engine (FSD §2 'Primitives', ADR B2).

Every tool call - from Cowork (MCP), the Claude API loop or the local model - goes through ToolExecutor.execute():
  1. parse args, pull out `reasoning`
  2. ensure a task (run) and SAPBot's own SAP session exist
  3. classify -> ActionIntent -> PolicyEngine (feature / workspace / system / hard-stop / write / script rules)
  4. human approval when required (Windows dialog or Cockpit)
  5. loop guard (same tool+args with identical status bar N times -> pause for human)
  6. execute; on unexpected error inside a transaction: '/n' rollback (FSD 3.4.3)
  7. mask (workspace data policy), cache screen schema, log the step (trajectory)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .. import workspaces as wsmod
from ..config import Settings, get_settings
from ..memory.rag import TrajectoryRAG, format_examples
from ..memory.schema_cache import SchemaCache
from ..memory.trajectory import Run, TrajectoryLogger
from ..scripts.library import ScriptLibrary, ScriptRunner
from ..security.approvals import ApprovalBroker
from ..security.governance import Governance
from ..security.masking import mask_obj
from ..security.policy import ActionIntent, Kind, PolicyDecision, PolicyEngine, Verdict
from ..sap.bridge import SAPGuiBridge
from ..sap.com import ElementNotFound, SapComError
from ..types import StatusBar, ToolResult
from .schemas import SAP_TOOL_NAMES

log = logging.getLogger(__name__)


@dataclass
class WsResources:
    ws: wsmod.Workspace
    logger: TrajectoryLogger
    schema: SchemaCache
    rag: TrajectoryRAG | None


@dataclass
class TaskState:
    run: Run
    session_id: str = ""
    system: str = ""
    client: str = ""
    date_format: str = ""
    history: list[tuple[str, str]] = field(default_factory=list)


class ToolExecutor:
    def __init__(self, gov: Governance, bridge: SAPGuiBridge, broker: ApprovalBroker,
                 library: ScriptLibrary, runner: ScriptRunner, channel: str = "cowork", model: str = "",
                 settings: Settings | None = None) -> None:
        self.gov = gov
        self.bridge = bridge
        self.broker = broker
        self.library = library
        self.runner = runner
        self.channel = channel
        self.model = model or {"cowork": "cowork-claude"}.get(channel, channel)
        self.s = settings or get_settings()
        self.policy = PolicyEngine(gov, self.s)
        self._res: dict[str, WsResources] = {}
        self.task: TaskState | None = None
        self._lock = threading.RLock()
        self.on_step: list[Any] = []     # UI streaming callbacks: fn(dict)
        self._own_sessions: dict[tuple[str, str], str] = {}
        self.preapproved: set[str] = set()  # batch-level approval scope (ADR B6): script path or "*task*"

    # ================================================================ workspace
    def active_workspace(self) -> wsmod.Workspace | None:
        wid = os.environ.get("SAPBOT_WORKSPACE") or self.gov.store.kv_get(f"active_workspace:{self.gov.username()}")
        if not wid:
            return None
        try:
            return wsmod.load(wid)
        except FileNotFoundError:
            return None

    def set_workspace(self, wid: str) -> wsmod.Workspace:
        ws = wsmod.load(wid)
        allowed = self.gov.allowed_workspaces()
        if "*" not in allowed and ws.id not in allowed:
            raise PermissionError(f"you are not assigned to workspace '{ws.id}'")
        if self.task:
            raise RuntimeError("finish the current task (task_end) before switching workspace")
        self.gov.store.kv_set(f"active_workspace:{self.gov.username()}", ws.id)
        self.gov.store.audit(self.gov.username(), ws.id, "WORKSPACE_SELECTED", {})
        return ws

    def resources(self, ws: wsmod.Workspace) -> WsResources:
        r = self._res.get(ws.id)
        if r is None:
            rag = None
            if self.gov.feature("rag", ws.id):
                try:
                    rag = TrajectoryRAG(ws.chroma_dir)
                except Exception as e:  # noqa: BLE001
                    log.warning("RAG unavailable for %s: %s", ws.id, e)
            r = WsResources(ws, TrajectoryLogger(ws.trajectories_dir, ws.mask_fields, ws.data_policy == "masked"),
                            SchemaCache(ws.schema_cache_dir), rag)
            self._res[ws.id] = r
        r.ws = ws
        return r

    # ================================================================ entry point
    def execute(self, tool: str, args: dict[str, Any] | None) -> ToolResult:
        args = dict(args or {})
        with self._lock:
            try:
                handler = getattr(self, f"_t_{tool}", None)
                if handler is None:
                    return ToolResult.fail(f"unknown tool '{tool}'")
                res = handler(args)
            except PermissionError as e:
                res = ToolResult.fail(str(e), needs_human=True)
            except (SapComError, ValueError, FileNotFoundError, RuntimeError) as e:
                res = ToolResult.fail(str(e))
            except Exception as e:  # noqa: BLE001
                log.exception("tool %s crashed", tool)
                res = ToolResult.fail(f"{type(e).__name__}: {e}")
            for cb in self.on_step:
                try:
                    cb({"tool": tool, "args": args, "result": res.model_dump()})
                except Exception:  # noqa: BLE001
                    pass
            return res

    # ================================================================ harness tools
    def _t_sapbot_status(self, a: dict[str, Any]) -> ToolResult:
        if a.get("set_workspace"):
            self.set_workspace(str(a["set_workspace"]))
        ws = self.active_workspace()
        allowed = self.gov.allowed_workspaces()
        try:
            sessions = [s.model_dump(include={"session_id", "system", "client", "user", "transaction", "busy"})
                        for s in self.bridge.list_sessions()]
            sap_err = ""
        except SapComError as e:
            sessions, sap_err = [], str(e)
        data = {
            "user": self.gov.username(), "role": self.gov.role(), "hub_mode": self.gov.hub_mode,
            "restricted": self.gov.restricted,
            "workspaces_available": [w.id for w in wsmod.list_all() if "*" in allowed or w.id in allowed],
            "active_workspace": None if ws is None else {
                "id": ws.id, "name": ws.name, "data_policy": ws.data_policy,
                "systems": [{"system": s.sid, "client": s.client, "env": s.env,
                             "mode": "write" if (s.write and not s.is_prod) else "read-only (writes need a grant)"
                             if not s.is_prod else "PROD - read-only; writes need grant + approval"}
                            for s in ws.systems]},
            "sap_sessions": sessions, "sap_error": sap_err,
            "grants": [{k: g[k] for k in ("workspace", "system", "client", "tcodes", "valid_to")}
                       for g in self.gov.active_grants()],
            "features": self.gov.all_features(ws.id if ws else None),
            "current_task": None if not self.task else {"run_id": self.task.run.run_id, "task": self.task.run.task,
                                                         "system": self.task.system, "client": self.task.client,
                                                         "steps": len(self.task.run.steps)},
        }
        if ws is None:
            data["next_step"] = "Call sapbot_status with set_workspace=<id> to choose the client workspace."
        return ToolResult(ok=True, data=data)

    def _t_task_begin(self, a: dict[str, Any]) -> ToolResult:
        ws = self.active_workspace()
        if ws is None:
            return ToolResult.fail("no active client workspace - call sapbot_status(set_workspace=...) first",
                                   needs_human=True)
        dec = self.policy.decide(ActionIntent("task_begin", Kind.META, self.channel), ws, None)
        if dec.verdict == Verdict.DENY:
            return ToolResult.fail(dec.reason, policy=dec.as_dict())
        if self.task is not None:
            self._finish_task("aborted", "superseded by a new task_begin", aborted=True)
        task = str(a.get("task", "")).strip()
        if not task:
            return ToolResult.fail("task text is required")
        sysm = self._pick_system(ws, a.get("system"), a.get("client"))
        if sysm is None:
            return ToolResult.fail("no SAP system of this workspace has an open logon; log on in SAP Logon first "
                                   f"(workspace systems: {[f'{s.sid}/{s.client}' for s in ws.systems]})")
        res = self.resources(ws)
        run = res.logger.begin(ws.id, self.gov.username(), self.channel, self.model, task, sysm.sid, sysm.client,
                               str(a.get("expected_output", "")))
        st = TaskState(run=run, system=sysm.sid, client=sysm.client)
        self.task = st
        try:
            st.session_id = self._claim_session(sysm.sid, sysm.client, bool(a.get("use_existing_session")))
        except SapComError as e:
            self._finish_task("failure", f"could not get a SAP session: {e}")
            return ToolResult.fail(str(e), needs_human=True)
        st.date_format = self.gov.store.kv_get(f"datefmt:{sysm.sid}/{sysm.client}:{self.gov.username()}") or ""
        examples: list[dict[str, Any]] = []
        if res.rag is not None:
            examples = res.rag.search_similar_trajectories(task, self.s.rag.top_k,
                                                           min_similarity=self.s.rag.min_similarity)
        self.gov.store.audit(self.gov.username(), ws.id, "TASK_BEGIN", {
            "task": task, "system": sysm.sid, "client": sysm.client, "channel": self.channel,
            "rag_hits": len(examples), "top_similarity": examples[0]["similarity"] if examples else 0}, run.run_id)
        return ToolResult(ok=True, data={
            "run_id": run.run_id, "system": sysm.sid, "client": sysm.client, "env": sysm.env,
            "mode": "write" if sysm.write and not sysm.is_prod else "read-only",
            "session": st.session_id, "user_date_format": st.date_format or "not probed (run sapbot probe)",
            "examples": format_examples(examples) or "No similar past runs yet.",
            "rules": "Use reasoning on every SAP tool. Saves/postings may need approval. For postings, read the "
                     "document back in a display transaction before task_end."})

    def _t_task_end(self, a: dict[str, Any]) -> ToolResult:
        if self.task is None:
            return ToolResult.fail("no active task")
        run = self._finish_task(str(a.get("outcome", "success")), str(a.get("summary", "")),
                                aborted=a.get("outcome") == "aborted")
        return ToolResult(ok=True, data={"run_id": run.run_id, "quality": run.quality, "verified": run.verified,
                                         "documents": run.document_numbers, "steps": len(run.steps),
                                         "note": {"gold": "Indexed for future examples.",
                                                  "silver": "Not used for learning: posting was not read back / "
                                                            "verified.",
                                                  "failed": "Not used for learning."}[run.quality]})

    def _finish_task(self, outcome: str, summary: str, aborted: bool = False) -> Run:
        st = self.task
        assert st is not None
        ws = wsmod.load(st.run.workspace)
        res = self.resources(ws)
        run = res.logger.finish(st.run, outcome, summary, aborted,
                                {t.upper() for t in self.s.sap.display_tcodes})
        if run.quality == "gold" and res.rag is not None:
            try:
                res.rag.index_trajectory(run)
            except Exception as e:  # noqa: BLE001
                log.warning("RAG indexing failed: %s", e)
        self.gov.store.audit(self.gov.username(), ws.id, "TASK_END", {"outcome": outcome, "quality": run.quality,
                                                                     "documents": run.document_numbers}, run.run_id)
        self.task = None
        return run

    def _t_list_saved_scripts(self, a: dict[str, Any]) -> ToolResult:
        folder = a.get("folder") or None
        return ToolResult(ok=True, data=[i.as_dict() for i in self.library.list(folder)])

    def _t_save_script_draft(self, a: dict[str, Any]) -> ToolResult:
        info = self.library.save_draft(str(a.get("name", "")), str(a.get("content", "")), self.gov.username())
        self._log_meta("save_script_draft", a, f"draft {info.path}")
        return ToolResult(ok=True, data={**info.as_dict(), "next_step": "An Admin reviews and approves it in the "
                                         "Cockpit (Script Approval). Read-only drafts can already run on QA."})

    def _t_request_elevation(self, a: dict[str, Any]) -> ToolResult:
        ws = self.active_workspace()
        if ws is None:
            return ToolResult.fail("no active workspace")
        rid = self.gov.store.add_request(self.gov.username(), ws.id, str(a["system"]), str(a["client"]).zfill(3),
                                         str(a.get("tcodes", "*")), int(a.get("minutes", 60)),
                                         str(a.get("reason", "")))
        return ToolResult(ok=True, data={"request_id": rid, "status": "pending",
                                         "next_step": "An Admin/Engagement Lead approves it in the Cockpit "
                                                      "(Grants & Requests). Tell the user."})

    # ================================================================ SAP tools
    def _t_sap_observe_screen(self, a: dict[str, Any]) -> ToolResult:
        ws, sysm, st, dec = self._gate(ActionIntent("sap_observe_screen", Kind.OBSERVE, self.channel))
        if dec.verdict != Verdict.ALLOW:
            return self._refused("sap_observe_screen", a, dec)
        snap = self.bridge.observe(st.session_id)
        res = self.resources(ws)
        cached = res.schema.get(snap["program_dynpro"])
        res.schema.put(snap)
        view = snap
        if cached and int(cached.get("seen", 0)) >= 2 and not a.get("force_refresh"):
            view = SchemaCache.dynamic_view(snap)
            view["hint"] = "known screen: labels via action_schema_memory or force_refresh=true"
        view = self._mask(ws, view)
        sb = snap.get("status_bar", {})
        self._log_step("sap_observe_screen", a, True, sb, view, snap.get("transaction", ""), dec)
        return ToolResult(ok=True, data=view, status_bar=StatusBar(**sb), policy=dec.as_dict())

    def _t_sap_act(self, a: dict[str, Any]) -> ToolResult:
        ok_code = str(a.get("ok_code") or "").strip()
        fu_raw = a.get("field_updates_json") or a.get("field_updates") or {}
        fu = json.loads(fu_raw) if isinstance(fu_raw, str) and fu_raw.strip() else (fu_raw or {})
        if not isinstance(fu, dict):
            return ToolResult.fail("field_updates_json must be a JSON object")
        click = str(a.get("click_id") or "").strip()
        vkey = a.get("vkey")
        vkey = int(vkey) if vkey not in (None, "") else None
        if not (ok_code or fu or click or vkey is not None):
            return ToolResult.fail("nothing to do: give ok_code, field_updates_json, click_id or vkey")
        sid = self._session_for_gate()
        cur_tx = self.bridge.session_info(sid).transaction
        element = self.bridge.element_info(sid, click) if click and click.startswith("wnd[") else None
        if element is not None and not element.get("exists"):
            return ToolResult.fail(f"element {click} not found on the current screen - observe the screen first")
        popup = self.bridge.popup_info(sid) if vkey == 0 or click else None
        c = self.policy.classify_act(ok_code, list(fu.keys()), click, vkey, cur_tx, element, popup)
        if c.get("error"):
            return ToolResult.fail(c["error"])
        wr = c["reasons"]
        intent = ActionIntent("sap_act", c["kind"], self.channel, tcode=c["tcode"], okcode=c.get("okcode", ""),
                              write_reasons=wr)
        intent.summary = self._describe_act(ok_code, fu, click, vkey, cur_tx) + \
            (f"\nWhy this needs care: {'; '.join(wr)}" if wr else "")
        ws, sysm, st, dec = self._gate(intent)
        if dec.verdict == Verdict.DENY:
            return self._refused("sap_act", a, dec)
        if dec.verdict == Verdict.APPROVE:
            d = self._approve(intent, dec, sysm, {"ok_code": ok_code, "field_updates": fu, "click_id": click,
                                                  "vkey": vkey, "transaction": cur_tx})
            if not d.approved:
                return self._refused("sap_act", a, dec, note=f"not approved ({d.note or d.by})")
            if d.edited_values:   # approver may change VALUES of the shown fields only, never add fields/commands
                fu.update({k: v for k, v in d.edited_values.items() if k in fu})
        loop = self._loop_check("sap_act", a)
        if loop:
            return loop
        try:
            out = self.bridge.act(st.session_id, ok_code, fu, click, vkey, st.date_format,
                                  self.s.sap.warning_allowlist)
        except ElementNotFound as e:
            self._log_step("sap_act", a, False, {}, {"error": str(e)}, cur_tx, dec, error=str(e))
            return ToolResult.fail(str(e))
        except TimeoutError as e:
            self._log_step("sap_act", a, False, {}, {"error": str(e)}, cur_tx, dec, error=str(e))
            return ToolResult.fail(str(e), needs_human=True)
        except Exception as e:  # noqa: BLE001
            rb = self._rollback(st, cur_tx)
            self._log_step("sap_act", a, False, {}, {"error": str(e), "rollback": rb}, cur_tx, dec,
                           error=str(e), is_write=bool(wr))
            return ToolResult.fail(f"SAP action failed: {e}" + (" - left the transaction with /n (locks released)"
                                                                 if rb.get("rolled_back") else ""))
        sb = out.get("status_bar", {})
        self._remember_status("sap_act", a, sb)
        view = self._mask(ws, out)
        self._log_step("sap_act", a, sb.get("type") not in ("E", "A"), sb, view, out.get("transaction", cur_tx),
                       dec, is_write=bool(wr))
        return ToolResult(ok=sb.get("type") not in ("E", "A"), data=view, status_bar=StatusBar(**sb),
                          policy=dec.as_dict(),
                          error=None if sb.get("type") not in ("E", "A") else f"SAP {sb.get('type')}: {sb.get('text')}")

    def _t_sap_read_grid(self, a: dict[str, Any]) -> ToolResult:
        ws, sysm, st, dec = self._gate(ActionIntent("sap_read_grid", Kind.OBSERVE, self.channel))
        if dec.verdict != Verdict.ALLOW:
            return self._refused("sap_read_grid", a, dec)
        data = self.bridge.read_grid(st.session_id, str(a.get("grid_id") or ""), int(a.get("max_rows") or 50),
                                     ws.output_dir)
        view = self._mask(ws, data)
        tx = self.bridge.session_info(st.session_id).transaction
        obs = dict(view)
        obs["rows"] = view.get("rows", [])[:25]
        self._log_step("sap_read_grid", a, True, {}, obs, tx, dec)
        return ToolResult(ok=True, data=view, policy=dec.as_dict())

    def _t_run_saved_script(self, a: dict[str, Any]) -> ToolResult:
        info = self.library.info(str(a.get("script", "")))
        content = self.library.resolve(info.path).read_text(encoding="utf-8", errors="replace")
        praw = a.get("params_json") or a.get("params") or {}
        params = self.runner.validate_params(json.loads(praw) if isinstance(praw, str) and praw.strip() else praw)
        intent = ActionIntent("run_saved_script", Kind.SCRIPT, self.channel,
                              script={"path": info.path, "digest": info.digest, "read_only": info.scan["read_only"],
                                      "findings": info.scan["findings"], "tcodes": info.scan["tcodes"],
                                      "approval": info.approval},
                              summary=f"Run script {info.path} with {params}\nPURPOSE: {info.header.get('PURPOSE', '')}")
        ws, sysm, st, dec = self._gate(intent)
        if dec.verdict == Verdict.DENY:
            return self._refused("run_saved_script", a, dec)
        if dec.verdict == Verdict.APPROVE:
            d = self._approve(intent, dec, sysm, {"script": info.path, "params": params,
                                                  "findings": info.scan["findings"],
                                                  "content_hash": info.digest[:12]})
            if not d.approved:
                return self._refused("run_saved_script", a, dec, note=f"not approved ({d.note or d.by})")
        loop = self._loop_check("run_saved_script", a)
        if loop:
            return loop
        out = self.runner.run(info, content, params, st.session_id, ws.output_dir)
        sb = self.bridge.observe(st.session_id).get("status_bar", {})
        ok = out["return_code"] == 0 and not out["missing_params"]
        view = self._mask(ws, out)
        self._remember_status("run_saved_script", a, sb)
        self._log_step("run_saved_script", a, ok, sb, view, (info.scan["tcodes"] or [""])[0], dec,
                       is_write=not info.scan["read_only"])
        return ToolResult(ok=ok, data=view, status_bar=StatusBar(**sb), policy=dec.as_dict(),
                          error=None if ok else (out["stderr"] or f"script ended with code {out['return_code']}"))

    def _meta_gate(self, tool: str) -> tuple[wsmod.Workspace | None, ToolResult | None]:
        ws = self.active_workspace()
        if ws is None:
            return None, ToolResult.fail("no active workspace")
        dec = self.policy.decide(ActionIntent(tool, Kind.META, self.channel), ws, None)
        if dec.verdict == Verdict.DENY:
            return None, ToolResult.fail(dec.reason, policy=dec.as_dict())
        return ws, None

    def _t_action_schema_memory(self, a: dict[str, Any]) -> ToolResult:
        ws, err = self._meta_gate("action_schema_memory")
        if err:
            return err
        res = self.resources(ws)
        key = str(a.get("program_dynpro", "")).strip().upper()
        if key == "LIST":
            return ToolResult(ok=True, data=res.schema.list())
        if a.get("note"):
            res.schema.add_note(key, str(a["note"]))
        e = res.schema.get(key)
        return ToolResult(ok=e is not None, data=e, error=None if e else f"{key} not cached yet - observe it first")

    def _t_rag_search_trajectories(self, a: dict[str, Any]) -> ToolResult:
        ws, err = self._meta_gate("rag_search_trajectories")
        if err:
            return err
        res = self.resources(ws)
        if res.rag is None:
            return ToolResult(ok=True, data={"examples": [], "note": "RAG is disabled for this workspace"})
        hits = res.rag.search_similar_trajectories(str(a.get("query", "")), int(a.get("top_k") or self.s.rag.top_k),
                                                   a.get("filter_t_code") or None)
        if self.task:
            self._log_step("rag_search_trajectories", a, True, {}, f"{len(hits)} examples", "",
                           PolicyDecision(Verdict.ALLOW, "meta"))
        return ToolResult(ok=True, data={"examples": format_examples(hits) or "No similar past runs.",
                                         "hits": [{k: h[k] for k in ("run_id", "similarity", "task")} for h in hits]})

    def _t_local_file_workspace(self, a: dict[str, Any]) -> ToolResult:
        ws = self.active_workspace()
        mode = str(a.get("mode", ""))
        kind = Kind.FILE_WRITE if mode.startswith("write") else Kind.FILE_READ
        dec = self.policy.decide(ActionIntent("local_file_workspace", kind, self.channel), ws, None)
        if dec.verdict != Verdict.ALLOW or ws is None:
            return ToolResult.fail(dec.reason or "no active workspace", policy=dec.as_dict())
        path = self._safe_path(ws, str(a.get("file_path", "")), write=kind == Kind.FILE_WRITE)
        if mode == "read_excel_or_csv":
            df = pd.read_csv(path, dtype=str, sep=None, engine="python", encoding="utf-8-sig") if path.suffix.lower() in (".csv", ".txt") \
                else pd.read_excel(path, dtype=str)
            df = df.fillna("")
            data = {"file": str(path), "rows": len(df), "columns": list(df.columns),
                    "records": df.head(200).to_dict(orient="records")}
        elif mode == "read_text":
            data = {"file": str(path), "text": path.read_text(encoding="utf-8", errors="replace")[:50000]}
        elif mode in ("write_csv", "write_excel"):
            rows = json.loads(a.get("content_json") or "[]")
            df = pd.DataFrame(rows)
            if mode == "write_csv":
                df.to_csv(path, index=False, encoding="utf-8-sig")
            else:
                df.to_excel(path, index=False)
            data = {"file": str(path), "rows_written": len(df)}
        elif mode == "write_text":
            c = a.get("content_json") or ""
            try:
                c = json.loads(c) if isinstance(c, str) and c[:1] in "[{\"" else c
            except json.JSONDecodeError:
                pass
            path.write_text(c if isinstance(c, str) else json.dumps(c, indent=1, ensure_ascii=False), encoding="utf-8")
            data = {"file": str(path)}
        else:
            return ToolResult.fail(f"unknown mode {mode}")
        data = self._mask(ws, data)
        if self.task:
            self._log_step("local_file_workspace", {k: v for k, v in a.items() if k != "content_json"}, True, {},
                           {k: v for k, v in data.items() if k != "records"}, "", dec)
        return ToolResult(ok=True, data=data)

    def _t_fiori_observe_dom(self, a: dict[str, Any]) -> ToolResult:
        return self._fiori(a)

    def _t_fiori_act(self, a: dict[str, Any]) -> ToolResult:
        return self._fiori(a)

    def _fiori(self, a: dict[str, Any]) -> ToolResult:
        ws = self.active_workspace()
        dec = self.policy.decide(ActionIntent("fiori", Kind.BROWSER, self.channel), ws, None)
        if dec.verdict == Verdict.DENY:
            return ToolResult.fail(dec.reason, policy=dec.as_dict())
        return ToolResult.fail("Fiori bridge ships in v1.1 (ADR B14)")

    # ================================================================ internals
    def _pick_system(self, ws: wsmod.Workspace, system: Any, client: Any) -> wsmod.SapSystem | None:
        if system:
            return ws.find_system(str(system), str(client or (ws.systems[0].client if ws.systems else "")))
        try:
            live = {(s.system, s.client) for s in self.bridge.list_sessions()}
        except SapComError:
            live = set()
        for s in ws.systems:
            if (s.sid, s.client) in live:
                return s
        return None

    def _claim_session(self, system: str, client: str, use_existing: bool) -> str:
        """Own session per process (ADR B3). Remembered in memory only: SAP reuses ses[N] ids, so an id from an
        earlier process could point at the consultant's own window (review finding 13)."""
        key = (system, client)
        known = self._own_sessions.get(key)
        if known:
            try:
                si = self.bridge.session_info(known)
                if si.system == system and si.client == client:
                    return known
            except SapComError:
                pass
            self._own_sessions.pop(key, None)
        if self.s.sap.open_own_session:
            try:
                si = self.bridge.open_own_session(system, client)
                self._own_sessions[key] = si.session_id
                return si.session_id
            except SapComError as e:
                if not use_existing:
                    raise SapComError(f"{e}. Ask the user whether SAPBot may use an existing session, then call "
                                      "task_begin again with use_existing_session=true.") from e
        si = self.bridge.find_session(system, client)
        if si is None:
            raise SapComError(f"no session for {system}/{client}")
        self._own_sessions[key] = si.session_id
        self.gov.store.audit(self.gov.username(), "", "EXISTING_SESSION_USED", {"system": system, "client": client,
                                                                                "session": si.session_id})
        return si.session_id

    def _session_for_gate(self) -> str:
        if self.task is None or not self.task.session_id:
            raise RuntimeError("no active task - call task_begin first")
        return self.task.session_id

    def _gate(self, intent: ActionIntent) -> tuple[wsmod.Workspace, wsmod.SapSystem, TaskState, PolicyDecision]:
        ws = self.active_workspace()
        if ws is None:
            raise PermissionError("no active client workspace - call sapbot_status(set_workspace=...)")
        if self.task is None:
            raise RuntimeError("no active task - call task_begin(task=...) first")
        if self.task.run.workspace != ws.id:
            raise RuntimeError("active workspace changed during the task - call task_end first")
        st = self.task
        live = self.bridge.session_info(st.session_id)
        sysm = ws.find_system(live.system, live.client)
        dec = self.policy.decide(intent, ws, sysm, (live.system, live.client))
        if sysm is None:
            raise PermissionError(dec.reason)
        return ws, sysm, st, dec

    def _approve(self, intent: ActionIntent, dec: PolicyDecision, sysm: wsmod.SapSystem, details: dict[str, Any]):
        assert self.task is not None
        scope = (intent.script or {}).get("path") or "*task*"
        batch_covered = dec.reason.startswith(("production save", "production posting script")) and \
            scope != "*task*"
        if batch_covered and scope in self.preapproved:
            from ..security.approvals import Decision
            self.gov.store.audit(self.gov.username(), self.task.run.workspace, "APPROVAL_BATCH_SCOPE",
                                 {"scope": scope, "reason": dec.reason}, self.task.run.run_id)
            return Decision(True, "batch-approval")
        d = self.broker.request(intent.tool, f"{dec.reason}\n\n{intent.summary}", details,
                                username=self.gov.username(), workspace=self.task.run.workspace,
                                system=sysm.sid, client=sysm.client, run_id=self.task.run.run_id)
        if not d.approved:
            self.task.run.refused_approvals += 1
        return d

    def _refused(self, tool: str, a: dict[str, Any], dec: PolicyDecision, note: str = "") -> ToolResult:
        msg = f"refused by SAPBot policy: {dec.reason}" + (f" - {note}" if note else "")
        if dec.hint:
            msg += f". Hint: {dec.hint}"
        if self.task:
            self._log_step(tool, a, False, {}, msg, "", dec, error=msg)
        self.gov.store.audit(self.gov.username(), self.task.run.workspace if self.task else "", "POLICY_REFUSED",
                             {"tool": tool, "reason": dec.reason, "note": note},
                             self.task.run.run_id if self.task else "")
        return ToolResult.fail(msg, needs_human=True, policy=dec.as_dict())

    def _loop_check(self, tool: str, a: dict[str, Any]) -> ToolResult | None:
        st = self.task
        if st is None:
            return None
        key = tool + json.dumps({k: v for k, v in a.items() if k != "reasoning"}, sort_keys=True, default=str)
        n = self.s.agent.loop_repeat_limit
        last = st.history[-(n - 1):] if n > 1 else []
        if len(last) == n - 1 and all(k == key for k, _ in last) and len({s for _, s in last}) == 1:
            self.gov.store.audit(self.gov.username(), st.run.workspace, "LOOP_DETECTED", {"tool": tool},
                                 st.run.run_id)
            return ToolResult.fail(f"loop detected: the same {tool} call was repeated {n} times with an identical "
                                   "status bar. Stop and ask the user how to proceed.", needs_human=True)
        return None

    def _remember_status(self, tool: str, a: dict[str, Any], sb: dict[str, Any]) -> None:
        if self.task:
            key = tool + json.dumps({k: v for k, v in a.items() if k != "reasoning"}, sort_keys=True, default=str)
            self.task.history.append((key, f"{sb.get('type')}|{sb.get('text')}"))
            self.task.history = self.task.history[-20:]

    def _rollback(self, st: TaskState, cur_tx: str) -> dict[str, Any]:
        if cur_tx and cur_tx.upper() in {t.upper() for t in self.s.sap.display_tcodes}:
            return {"rolled_back": False, "reason": "display transaction"}
        try:
            out = self.bridge.rollback(st.session_id)
            self.gov.store.audit(self.gov.username(), st.run.workspace, "ROLLBACK", out, st.run.run_id)
            return out
        except Exception as e:  # noqa: BLE001
            return {"rolled_back": False, "error": str(e)}

    def _mask(self, ws: wsmod.Workspace, obj: Any) -> Any:
        return mask_obj(obj, ws.mask_fields) if ws.data_policy == "masked" else obj

    def _log_step(self, tool: str, a: dict[str, Any], ok: bool, sb: dict[str, Any], obs: Any, tx: str,
                  dec: PolicyDecision, error: str = "", is_write: bool = False) -> None:
        if not self.task:
            return
        ws = wsmod.load(self.task.run.workspace)
        self.resources(ws).logger.add_step(self.task.run, tool, {k: v for k, v in a.items() if k != "reasoning"},
                                           str(a.get("reasoning", "")), ok, sb, obs, tx,
                                           f"{dec.verdict.value}: {dec.reason}", error, is_write)

    def _log_meta(self, tool: str, a: dict[str, Any], obs: str) -> None:
        if self.task:
            self._log_step(tool, {k: v for k, v in a.items() if k != "content"}, True, {}, obs, "",
                           PolicyDecision(Verdict.ALLOW, "meta"))

    def _safe_path(self, ws: wsmod.Workspace, p: str, write: bool) -> Path:
        if not p:
            raise ValueError("file_path is required")
        path = Path(p)
        if write:
            if not path.is_absolute():
                path = ws.output_dir / path.name
            path = path.resolve()
            if ws.output_dir.resolve() not in path.parents:
                raise PermissionError(f"writes are only allowed in {ws.output_dir}")
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        path = path.resolve() if path.is_absolute() else (ws.output_dir / path).resolve()
        home = Path.home()
        roots = [ws.root.resolve()] + [(home / d).resolve() for d in ("Documents", "Downloads", "Desktop",
                                                                       "OneDrive")]
        if not any(r == path or r in path.parents for r in roots):
            raise PermissionError("reads are allowed from the workspace and Documents/Downloads/Desktop/OneDrive")
        if not path.exists():
            raise FileNotFoundError(str(path))
        return path

    @staticmethod
    def _describe_act(ok_code: str, fu: dict[str, Any], click: str, vkey: int | None, tx: str) -> str:
        parts = [f"Transaction: {tx or '-'}"]
        if ok_code:
            parts.append(f"Command: {ok_code}")
        for k, v in list(fu.items())[:15]:
            parts.append(f"Set {k.rsplit('/', 1)[-1]} = {v}")
        if click:
            parts.append(f"Click: {click}")
        if vkey is not None:
            parts.append(f"Key: {vkey} ({ {0: 'Enter', 3: 'Back', 8: 'Execute', 11: 'SAVE', 12: 'Cancel'}.get(vkey, '')})")
        return "\n".join(parts)


def is_sap_tool(name: str) -> bool:
    return name in SAP_TOOL_NAMES
