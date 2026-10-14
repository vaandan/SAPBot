"""Consultant pages: Cockpit, Workspaces, Run History, Scripts, Batch Runs, Requests, Settings (FSD 3.7)."""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from .. import workspaces as wsmod
from ..config import get_settings
from ..memory.trajectory import TrajectoryLogger
from .common import (admin, context_bar, env_badge, esc, executor, jobs, page_header, status_badge, tile)


# =================================================================================================== Cockpit
def page_cockpit() -> None:
    ex = executor()
    gov = ex.gov
    page_header("Cockpit", "Give SAPBot a task, watch every step, approve sensitive actions.")
    ws = context_bar()
    if ws is None:
        return
    res = ex.resources(ws)
    rag_stats = res.rag.get_stats() if res.rag else {"indexed": 0, "per_t_code": {}}
    runs = res.logger.list_runs(500)
    today = dt.date.today().strftime("%Y%m%d")
    runs_today = [r for r in runs if (r.get("run_id") or "").startswith(today)]
    gold = sum(1 for r in runs if r.get("quality") == "gold")

    live_tx, live_dyn, sb = "-", "-", {}
    if ex.task and ex.task.session_id:
        try:
            si = ex.bridge.session_info(ex.task.session_id)
            live_tx, live_dyn = si.transaction, f"{si.program}_{si.screen}"
        except Exception:  # noqa: BLE001
            pass
    job = jobs().current
    if job:
        for ev in reversed(job.events):
            r = ev.get("result") or {}
            if r.get("status_bar"):
                sb = r["status_bar"]
                break
    c = st.columns(4)
    c[0].markdown(tile("Active transaction", live_tx, live_dyn), unsafe_allow_html=True)
    c[1].markdown(tile("Last status bar", sb.get("type") or "-", (sb.get("text") or "")[:60]),
                  unsafe_allow_html=True)
    c[2].markdown(tile("Runs today", len(runs_today), f"{gold} gold runs in total"), unsafe_allow_html=True)
    c[3].markdown(tile("Learned examples (RAG)", rag_stats["indexed"],
                       ", ".join(f"{k}: {v}" for k, v in list(rag_stats["per_t_code"].items())[:3]) or "none yet"),
                  unsafe_allow_html=True)

    left, right = st.columns([3, 2])
    with left:
        with st.container(border=True):
            st.markdown("**New task**")
            api_on = gov.feature("claude_api_mode", ws.id)
            local_on = gov.feature("local_model", ws.id)
            if not (api_on or local_on):
                st.info("API mode is switched off. Use SAPBot from Claude Cowork, or ask an Admin to enable "
                        "'claude_api_mode' in Features. Approvals from Cowork pop up as a Windows dialog.")
            task = st.text_area("What should SAPBot do?", height=90, placeholder="e.g. Release process order "
                                "1000000993 on SUQ and list the batches it determined", key="task_text")
            cc = st.columns([2, 2, 1])
            systems = [f"{s.sid}/{s.client}" for s in ws.systems]
            sys_pick = cc[0].selectbox("System", systems or ["-"])
            brains = (["Claude"] if api_on else []) + (["Local model"] if local_on else [])
            if api_on and local_on and gov.feature("router", ws.id):
                brains = ["Auto (router)"] + brains
            brain = cc[1].selectbox("Model", brains or ["-"])
            go = cc[2].button("Run", type="primary", use_container_width=True,
                              disabled=not (task.strip() and brains) or jobs().busy())
            if go:
                _start_agent(task.strip(), sys_pick, brain)
                st.rerun()
        _live_panel()
    with right:
        _approvals_panel()
        _rag_panel(ws)


def _start_agent(task: str, sys_pick: str, brain: str) -> None:
    from ..agent.loop import AgentLoop
    ex = executor()
    sid, cl = sys_pick.split("/") if "/" in sys_pick else (None, None)
    force = {"Claude": "claude", "Local model": "local"}.get(brain, "")

    def fn(job):  # runs in a background thread
        loop = AgentLoop(ex, on_event=job.events.append, force_brain=force)
        return loop.run(task, sid, cl)
    jobs().start("agent", task, fn)


@st.fragment(run_every="1s")
def _live_panel() -> None:
    job = jobs().current
    with st.container(border=True):
        st.markdown("**Run steps**")
        if job is None:
            st.caption("No run yet in this Cockpit session.")
            return
        badge = {"running": "I", "done": "S", "error": "E", "stopped": "W"}[job.status]
        st.markdown(f"{status_badge(badge, job.status)} &nbsp; {esc(job.title[:120])}", unsafe_allow_html=True)
        if job.status == "running" and st.button("Stop after current step"):
            job.stop = True
        for i, ev in enumerate(job.events[-60:]):
            t = ev["type"]
            if t == "thought":
                st.markdown(f'<div class="sb-step think">{esc(ev["text"][:600])}</div>', unsafe_allow_html=True)
            elif t == "tool_call":
                args = {k: v for k, v in ev["args"].items() if k != "reasoning"}
                with st.expander(f"→ {ev['name']}  {json.dumps(args, ensure_ascii=False)[:90]}"):
                    st.caption(ev["args"].get("reasoning", ""))
                    st.json(ev["args"], expanded=False)
            elif t == "tool_result":
                r = ev["result"]
                sb = r.get("status_bar") or {}
                cls = "" if r.get("ok") else " err"
                txt = r.get("error") or sb.get("text") or ("ok" if r.get("ok") else "")
                st.markdown(f'<div class="sb-step{cls}">{status_badge(sb.get("type", ""), "")} {esc(txt[:300])}'
                            f'</div>', unsafe_allow_html=True)
            elif t in ("final", "error", "escalated"):
                kind = "S" if ev.get("outcome") == "success" else ("W" if t == "escalated" else "E")
                st.markdown(f'<div class="sb-card">{status_badge(kind, "")} <b>{esc(t.title())}</b> '
                            f'{esc(ev.get("summary") or ev.get("text") or "")}<br><small>quality: '
                            f'{esc(ev.get("quality", ""))} · turns {esc(ev.get("turns", ""))} · tokens '
                            f'{esc(ev.get("tokens", ""))}</small></div>', unsafe_allow_html=True)
        if job.error:
            st.error(job.error)


@st.fragment(run_every="1s")
def _approvals_panel() -> None:
    ex = executor()
    pend = [a for a in ex.gov.store.pending_approvals() if a["username"] == ex.gov.username()]
    with st.container(border=True):
        waiting = f'<span class="sb-status W">{len(pend)} waiting</span>' if pend else ""
        st.markdown(f"**Approvals** {waiting}", unsafe_allow_html=True)
        if not pend:
            st.caption("Nothing waiting. Saves on production, hard-stop transactions and unapproved posting scripts "
                       "appear here.")
        for a in pend:
            det = json.loads(a.get("details") or "{}")
            st.markdown(f'<div class="sb-card"><b>#{a["id"]}</b> · {esc(a["system"])}/{esc(a["client"])} · '
                        f'{esc(a["kind"])}<br>{esc(a["summary"]).replace(chr(10), "<br>")}</div>',
                        unsafe_allow_html=True)
            edited = None
            if det.get("field_updates"):
                txt = st.text_area("Edit values (JSON)", json.dumps(det["field_updates"], indent=1),
                                   key=f"edit_{a['id']}")
                try:
                    edited = json.loads(txt)
                    if edited == det["field_updates"]:
                        edited = None
                except json.JSONDecodeError:
                    st.warning("invalid JSON - values will not be changed")
            c = st.columns(2)
            if c[0].button("Approve", key=f"ok_{a['id']}", type="primary", use_container_width=True):
                ex.gov.store.decide_approval(a["id"], True, ex.gov.username(), edited)
                st.rerun(scope="fragment")
            if c[1].button("Abort", key=f"no_{a['id']}", use_container_width=True):
                ex.gov.store.decide_approval(a["id"], False, ex.gov.username())
                st.rerun(scope="fragment")


def _rag_panel(ws: wsmod.Workspace) -> None:
    job = jobs().current
    with st.container(border=True):
        st.markdown("**Similar past runs used as examples**")
        ex_text = ""
        if job:
            for ev in job.events:
                if ev["type"] == "task_begin":
                    ex_text = ev["data"].get("examples", "")
        if ex_text and "No similar" not in ex_text:
            st.code(ex_text[:3000], language=None)
        else:
            st.caption("Shown here when a run starts. Every gold run of this client becomes an example "
                       "for the next similar task.")


# =================================================================================================== Workspaces
def page_workspaces() -> None:
    ex = executor()
    page_header("My Workspaces", "Clients you are assigned to and their SAP systems.")
    allowed = ex.gov.allowed_workspaces()
    wss = [w for w in wsmod.list_all() if "*" in allowed or w.id in allowed]
    if not wss:
        st.info("No workspace assigned yet.")
    for w in wss:
        with st.container(border=True):
            st.markdown(f"### {esc(w.name)} <small>({esc(w.id)})</small>", unsafe_allow_html=True)
            rows = [{"System": s.sid, "Client": s.client, "Env": s.env,
                     "Mode": "write" if s.write and not s.is_prod else "read-only", "Description": s.description}
                    for s in w.systems]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            st.caption(f"Data policy: {w.data_policy} · extra allowed T-codes: {', '.join(w.allowed_tcodes) or '-'}"
                       f" · retention {w.retention_days} days")
            if st.button("Make active", key=f"use_{w.id}", disabled=ex.task is not None):
                ex.set_workspace(w.id)
                st.rerun()


# =================================================================================================== History
def page_history() -> None:
    ex = executor()
    page_header("Run History", "Every task with its steps, status and learning quality.")
    ws = context_bar()
    if ws is None:
        return
    logger = ex.resources(ws).logger
    runs = logger.list_runs(500)
    if not ex.gov.is_lead_or_admin():
        runs = [r for r in runs if r.get("user") == ex.gov.username()]
    if not runs:
        st.info("No runs yet for this client.")
        return
    df = pd.DataFrame(runs)
    q = st.columns(3)
    qual = q[0].multiselect("Quality", ["gold", "silver", "failed", ""], default=["gold", "silver", "failed"])
    chan = q[1].multiselect("Channel", sorted(df["channel"].dropna().unique()))
    text = q[2].text_input("Search task")
    view = df[df["quality"].fillna("").isin(qual)]
    if chan:
        view = view[view["channel"].isin(chan)]
    if text:
        view = view[view["task"].str.contains(text, case=False, na=False)]
    st.dataframe(view[["run_id", "started", "user", "channel", "task", "system", "outcome", "quality", "steps",
                       "document_numbers"]], hide_index=True, use_container_width=True)
    rid = st.selectbox("Open run", [""] + list(view["run_id"]))
    if rid:
        run = logger.load(rid)
        st.markdown(f"**{esc(run.task)}**  \n{status_badge('S' if run.quality == 'gold' else ('W' if run.quality == 'silver' else 'E'), run.quality)}"
                    f" · {esc(run.summary)}", unsafe_allow_html=True)
        for s in run.steps:
            with st.expander(f"{s.n}. {s.tool} [{(s.status_bar or {}).get('type', '')}] "
                             f"{(s.status_bar or {}).get('text', '')[:80]}"):
                st.caption(f"{s.ts} · {s.transaction} · policy: {s.policy}")
                if s.reasoning:
                    st.markdown(f"_Reasoning:_ {esc(s.reasoning)}")
                st.json(s.args, expanded=False)
                st.code(s.observation[:3000], language="json")
        st.download_button("Download run (JSON)", (logger.folder / "runs" / f"{rid}.json").read_bytes(),
                           file_name=f"{rid}.json")


# =================================================================================================== Scripts
def page_scripts() -> None:
    ex = executor()
    page_header("Script Library", "Proven, parameterised SAP GUI scripts. Read-only scripts run immediately.")
    ws = context_bar()
    infos = ex.library.list()
    rows = [{"Script": i.path, "Purpose": i.header.get("PURPOSE", "")[:120], "Params": i.header.get("PARAMS", "")[:100],
             "Read-only": i.scan["read_only"], "Approved": bool(i.approval),
             "Prod": bool(i.approval and i.approval.get("allow_prod"))} for i in infos]
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    if ws is None:
        return
    with st.container(border=True):
        st.markdown("**Run a script**")
        pick = st.selectbox("Script", [i.path for i in infos])
        info = next(i for i in infos if i.path == pick) if infos else None
        if info:
            st.caption(f"PARAMS: {info.header.get('PARAMS', '-')}  \nRESULT: {info.header.get('RESULT', '-')}")
            params = st.text_area("Parameters (JSON)", '{"AUFNR": ""}', height=80)
            sysx = st.selectbox("System", [f"{s.sid}/{s.client}" for s in ws.systems], key="scr_sys")
            if st.button("Run script", type="primary", disabled=jobs().busy()):
                sid, cl = sysx.split("/")

                def fn(job):
                    tb = ex.execute("task_begin", {"task": f"Run {pick} with {params}", "system": sid, "client": cl})
                    job.events.append({"type": "task_begin", "data": tb.data or {}})
                    if not tb.ok:
                        raise RuntimeError(tb.error)
                    r = ex.execute("run_saved_script", {"reasoning": "manual run from Cockpit", "script": pick,
                                                        "params_json": params})
                    job.events.append({"type": "tool_result", "name": "run_saved_script",
                                       "result": r.model_dump(exclude_none=True)})
                    e = ex.execute("task_end", {"outcome": "success" if r.ok else "failure",
                                                "summary": r.error or json.dumps((r.data or {}).get("results", {}))})
                    job.events.append({"type": "final", "outcome": "success" if r.ok else "failure",
                                       "quality": (e.data or {}).get("quality"), "summary": r.error or "done"})
                    return r
                jobs().start("script", f"{pick} {params}", fn)
                st.success("Started - follow it on the Cockpit page (Run steps).")


# =================================================================================================== Batch
def page_batch() -> None:
    ex = executor()
    page_header("Batch Runs", "Run a saved script (or a task template) for every row of an Excel file.")
    ws = context_bar()
    if ws is None:
        return
    if not ex.gov.feature("batch_runs", ws.id):
        st.warning("Batch runs are disabled (Admin > Features).")
        return
    up = st.file_uploader("Excel or CSV file", type=["xlsx", "xls", "csv"])
    if not up:
        st.caption("Results and a checkpoint are written to the workspace output folder; an interrupted batch "
                   "resumes where it stopped.")
        return
    dest = ws.output_dir / up.name
    dest.write_bytes(up.getvalue())
    df = pd.read_csv(dest, dtype=str, sep=None, engine="python", encoding="utf-8-sig") if dest.suffix == ".csv" \
        else pd.read_excel(dest, dtype=str)
    st.dataframe(df.head(20), use_container_width=True, hide_index=True)
    mode = st.radio("Mode", ["Saved script (deterministic)", "Task template (agent per row)"], horizontal=True)
    spec_kw: dict = {}
    if mode.startswith("Saved"):
        infos = ex.library.list()
        pick = st.selectbox("Script", [i.path for i in infos])
        info = next(i for i in infos if i.path == pick)
        st.caption(f"PARAMS: {info.header.get('PARAMS', '-')}")
        mapping = st.data_editor(pd.DataFrame({"PARAM": [""], "Column": [df.columns[0]]}), num_rows="dynamic",
                                 column_config={"Column": st.column_config.SelectboxColumn(options=list(df.columns))},
                                 use_container_width=True)
        spec_kw = {"mode": "script", "script": pick,
                   "param_map": {r.PARAM.strip().upper(): r.Column for r in mapping.itertuples() if r.PARAM}}
    else:
        tmpl = st.text_input("Task template", "Display process order {" + str(df.columns[0]) + "} in COR3")
        spec_kw = {"mode": "task", "task_template": tmpl}
    sysx = st.selectbox("System", [f"{s.sid}/{s.client}" for s in ws.systems])
    if st.button("Start batch", type="primary", disabled=jobs().busy()):
        from ..agent.batch import BatchSpec, run_batch
        sid, cl = sysx.split("/")
        spec = BatchSpec(excel=str(dest), system=sid, client=cl, **spec_kw)

        def fn(job):
            return run_batch(ex, spec, on_row=lambda i, r: job.events.append(
                {"type": "tool_result", "name": f"row {i + 1}", "result": {"ok": r["SAPBOT_STATUS"] == "OK",
                                                                           "error": r["SAPBOT_MESSAGE"]}}),
                stop_flag=lambda: job.stop)
        jobs().start("batch", f"Batch {up.name}", fn)
    job = jobs().current
    if job and job.kind == "batch":
        st.markdown(f"Batch status: {status_badge({'running': 'I', 'done': 'S'}.get(job.status, 'E'), job.status)}"
                    f" · rows processed: {len(job.events)}", unsafe_allow_html=True)
        if job.status == "running" and st.button("Stop batch"):
            job.stop = True
        if job.result and job.result.get("results_file"):
            p = Path(job.result["results_file"])
            st.download_button("Download results", p.read_bytes(), file_name=p.name)
        if job.error:
            st.error(job.error)


# =================================================================================================== Requests
def page_requests() -> None:
    ex = executor()
    page_header("Requests & Grants", "Ask for time-boxed write access; see your active grants.")
    ws = context_bar()
    if ws is None:
        return
    adm = admin()
    with st.form("req"):
        st.markdown("**Request elevation**")
        c = st.columns(3)
        sysx = c[0].selectbox("System", [f"{s.sid}/{s.client} ({s.env})" for s in ws.systems])
        tcodes = c[1].text_input("T-codes (comma, * = all)", "*")
        minutes = c[2].number_input("Minutes", 15, 7 * 24 * 60, 120, step=15)
        reason = st.text_input("Reason (shown to the approver and kept in the audit log)")
        if st.form_submit_button("Send request", type="primary"):
            if not reason.strip():
                st.error("A reason is required.")
            else:
                sid, cl = sysx.split(" ")[0].split("/")
                rid = adm.add_request(ws.id, sid, cl, tcodes, int(minutes), reason)
                st.success(f"Request #{rid} sent to the Admin / Engagement Lead.")
    st.markdown("**My active grants**")
    g = [x for x in ex.gov.active_grants()]
    st.dataframe(pd.DataFrame(g) if g else pd.DataFrame(columns=["workspace", "system", "client", "tcodes",
                                                                  "valid_to"]), hide_index=True,
                 use_container_width=True)
    st.markdown("**My requests**")
    mine = [r for r in adm.requests(None) if r["username"] == ex.gov.username()]
    st.dataframe(pd.DataFrame(mine) if mine else pd.DataFrame(), hide_index=True, use_container_width=True)


# =================================================================================================== Settings
def page_settings() -> None:
    ex = executor()
    s = get_settings()
    page_header("Settings", "Connections, model and personal settings.")
    c = st.columns(2)
    with c[0]:
        with st.container(border=True):
            st.markdown("**SAP GUI connection**")
            if st.button("Probe SAP GUI (read-only)"):
                try:
                    ss = ex.bridge.list_sessions()
                    st.success(f"SAP Logon reachable, GUI scripting on - {len(ss)} session(s)")
                    st.dataframe(pd.DataFrame([x.model_dump() for x in ss]), hide_index=True)
                except Exception as e:  # noqa: BLE001
                    st.error(str(e))
            st.caption("Full check incl. own session and date format: `sapbot probe` in a terminal.")
    with c[1]:
        with st.container(border=True):
            st.markdown("**Claude API key (API mode)**")
            from ..agent.llm import get_secret, set_secret
            have = bool(get_secret("anthropic_api_key", "ANTHROPIC_API_KEY"))
            st.markdown(status_badge("S" if have else "W", "stored in Windows Credential Manager" if have
                                     else "not set"), unsafe_allow_html=True)
            if ex.gov.hub_mode:
                st.caption("Hub mode: Claude calls go through the Hub proxy; no key needed on this laptop.")
            else:
                k = st.text_input("Set / replace key", type="password")
                if st.button("Save key") and k.strip():
                    set_secret("anthropic_api_key", k.strip())
                    st.success("Saved.")
            st.caption(f"Model: {s.models.claude_model} (escalation: {s.models.claude_escalation_model})")
    with st.container(border=True):
        st.markdown("**Hub**")
        st.write({"hub_url": s.hub.url or "(not configured)", "hub_mode": ex.gov.hub_mode,
                  "enrolled": ex.gov.enrolled, "restricted": ex.gov.restricted})
        if s.hub.url:
            if st.button("Sync with Hub now"):
                from ..hub.client import HubClient
                try:
                    st.json(HubClient.from_settings(ex.gov).sync(ex))
                except Exception as e:  # noqa: BLE001
                    st.error(str(e))
