"""Admin / Engagement Lead pages (ADR A12): Users, Client Workspaces, Grants & Requests, Script Approval,
Audit Log, Features, Learning, Hub."""
from __future__ import annotations

import json
import re

import pandas as pd
import streamlit as st

from .. import workspaces as wsmod
from ..config import get_settings, save_settings_patch
from ..security.governance import FEATURE_HELP, FEATURES
from .common import admin, esc, executor, page_header, status_badge, tile


def _need_admin() -> bool:
    if not executor().gov.is_admin():
        st.error("Admin role required.")
        return False
    return True


# =================================================================================================== Users
def page_users() -> None:
    if not _need_admin():
        return
    adm = admin()
    page_header("Users", "Create consultants, leads and admins; assign them to client workspaces.")
    users = adm.list_users()
    st.dataframe(pd.DataFrame(users), hide_index=True, use_container_width=True)
    c = st.columns(2)
    with c[0], st.form("user"):
        st.markdown("**Create / update user**")
        un = st.text_input("Username (Windows user name, lowercase)")
        dn = st.text_input("Display name")
        role = st.selectbox("Role", ["consultant", "lead", "admin"])
        active = st.checkbox("Active", True)
        if st.form_submit_button("Save user", type="primary"):
            if not re.match(r"^[a-z0-9._\-]{2,40}$", un.strip().lower()):
                st.error("Username: 2-40 chars, letters/digits/._-")
            else:
                try:
                    adm.upsert_user(un.strip().lower(), dn or un, role, active)
                    st.success("Saved.")
                    st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(str(e))
    with c[1]:
        with st.container(border=True):
            st.markdown("**Workspace membership**")
            wss = [w.id for w in wsmod.list_all()]
            u = st.selectbox("User", [x["username"] for x in users])
            w = st.selectbox("Workspace", wss) if wss else None
            r = st.selectbox("Role in workspace", ["consultant", "lead"])
            cc = st.columns(2)
            if w and cc[0].button("Assign", type="primary"):
                adm.set_member(w, u, r)
                st.rerun()
            if w and cc[1].button("Remove"):
                adm.remove_member(w, u)
                st.rerun()
            st.dataframe(pd.DataFrame(adm.members()), hide_index=True, use_container_width=True)
        with st.container(border=True):
            st.markdown("**Hub login token**")
            tu = st.selectbox("Issue token for", [x["username"] for x in users], key="tok_u")
            if st.button("Issue new token"):
                st.code(adm.issue_token(tu), language=None)
                st.caption("Shown once. The consultant enters it in the installer / Settings. Issuing a new token "
                           "invalidates the old one.")


# =================================================================================================== Workspaces
def page_ws_admin() -> None:
    if not _need_admin():
        return
    ex = executor()
    page_header("Client Workspaces", "Onboard clients: SAP systems, write mode, allowed T-codes, data policy.")
    wss = wsmod.list_all()
    tab_new, tab_edit = st.tabs(["Onboard new client", "Edit client"])
    with tab_new, st.form("new_ws"):
        c = st.columns(2)
        wid = c[0].text_input("Workspace id (e.g. acme)")
        name = c[1].text_input("Client name")
        pol = st.selectbox("Data policy", ["full", "masked", "local-only"],
                           help="masked: personal data in SAP fields is replaced before it leaves the laptop; "
                                "local-only: only the on-prem small model may see SAP data")
        systems = st.data_editor(pd.DataFrame([{"sid": "", "client": "", "env": "QA", "write": False,
                                                "description": ""}]), num_rows="dynamic", use_container_width=True,
                                 column_config={"env": st.column_config.SelectboxColumn(options=["QA", "DEV", "PROD"])})
        tcodes = st.text_input("Extra allowed T-codes (comma) - display T-codes are always allowed", "")
        if st.form_submit_button("Create workspace", type="primary"):
            try:
                if wid.strip().lower() in [w.id for w in wsmod.list_all()]:
                    raise FileExistsError(f"workspace '{wid}' already exists")
                ws = wsmod.Workspace(id=wid, name=name or wid, data_policy=pol,
                                     systems=[wsmod.SapSystem(**r) for r in systems.to_dict("records") if r.get("sid")],
                                     allowed_tcodes=[t.strip().upper() for t in tcodes.split(",") if t.strip()])
                admin().save_workspace(ws)      # Hub mode: saved centrally, laptops get it on next sync
                ws.save()
                ex.gov.store.audit(ex.gov.username(), ws.id, "WORKSPACE_CREATED", ws.model_dump())
                st.success(f"Created {ws.id} - folder {ws.root}")
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
    with tab_edit:
        if not wss:
            st.info("No workspaces yet.")
            return
        wid = st.selectbox("Workspace", [w.id for w in wss])
        ws = wsmod.load(wid)
        with st.form("edit_ws"):
            name = st.text_input("Client name", ws.name)
            pol = st.selectbox("Data policy", ["full", "masked", "local-only"],
                               index=["full", "masked", "local-only"].index(ws.data_policy))
            systems = st.data_editor(pd.DataFrame([s.model_dump() for s in ws.systems] or
                                                  [{"sid": "", "client": "", "env": "QA", "write": False,
                                                    "description": ""}]),
                                     num_rows="dynamic", use_container_width=True,
                                     column_config={"env": st.column_config.SelectboxColumn(
                                         options=["QA", "DEV", "PROD"])})
            tcodes = st.text_input("Extra allowed T-codes", ",".join(ws.allowed_tcodes))
            mask = st.text_input("Masked SAP fields (masked policy)", ",".join(ws.mask_fields))
            ret = st.number_input("Retention (days)", 30, 3650, ws.retention_days)
            if st.form_submit_button("Save", type="primary"):
                ws.name, ws.data_policy, ws.retention_days = name, pol, int(ret)
                ws.systems = [wsmod.SapSystem(**{k: v for k, v in r.items() if k in ("sid", "client", "env", "write",
                                                                                     "description")})
                              for r in systems.to_dict("records") if r.get("sid")]
                ws.allowed_tcodes = [t.strip().upper() for t in tcodes.split(",") if t.strip()]
                ws.mask_fields = [t.strip().upper() for t in mask.split(",") if t.strip()]
                admin().save_workspace(ws)
                ws.save()
                ex.gov.store.audit(ex.gov.username(), ws.id, "WORKSPACE_UPDATED", ws.model_dump())
                st.success("Saved.")
        with st.expander("Offboard client (export + purge)"):
            st.warning("Deletes this client's local runs, RAG store, schema cache and output on this laptop.")
            confirm = st.text_input("Type the workspace id to confirm")
            if st.button("Purge workspace", disabled=confirm != ws.id):
                wsmod.purge(ws.id)
                ex.gov.store.audit(ex.gov.username(), ws.id, "WORKSPACE_PURGED", {})
                st.success("Purged.")
                st.rerun()


# =================================================================================================== Grants
def page_grants() -> None:
    ex = executor()
    if not ex.gov.is_lead_or_admin():
        st.error("Admin or Engagement Lead role required.")
        return
    adm = admin()
    page_header("Grants & Requests", "Approve elevation requests and manage time-boxed write access.")
    pend = adm.requests("pending")
    badge = f'<span class="sb-status W">{len(pend)} waiting</span>' if pend else ""
    st.markdown(f"**Pending requests** {badge}", unsafe_allow_html=True)
    if not pend:
        st.caption("No pending requests.")
    for r in pend:
        with st.container(border=True):
            c = st.columns([5, 1, 1])
            c[0].markdown(f"**{esc(r['username'])}** · {esc(r['workspace'])} · {esc(r['system'])}/{esc(r['client'])}"
                          f" · T-codes `{esc(r['tcodes'])}` · {r['minutes']} min<br><small>{esc(r['reason'])}</small>",
                          unsafe_allow_html=True)
            if c[1].button("Approve", key=f"ra{r['id']}", type="primary"):
                adm.decide_request(r["id"], True)
                st.rerun()
            if c[2].button("Deny", key=f"rd{r['id']}"):
                adm.decide_request(r["id"], False)
                st.rerun()
    with st.expander("New grant", expanded=False), st.form("grant"):
        users = [u["username"] for u in adm.list_users()]
        wss = wsmod.list_all()
        c = st.columns(3)
        u = c[0].selectbox("User", users)
        w = c[1].selectbox("Workspace", [x.id for x in wss])
        ws = next((x for x in wss if x.id == w), None)
        sysx = c[2].selectbox("System", [f"{s.sid}/{s.client} ({s.env})" for s in (ws.systems if ws else [])])
        c2 = st.columns(3)
        tcodes = c2[0].text_input("T-codes", "*")
        minutes = c2[1].number_input("Minutes", 15, 7 * 24 * 60, 120, step=15)
        reason = c2[2].text_input("Reason")
        if st.form_submit_button("Grant", type="primary") and sysx:
            sid, cl = sysx.split(" ")[0].split("/")
            try:
                adm.add_grant(u, w, sid, cl, tcodes, int(minutes), reason)
                st.success("Granted.")
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
    st.markdown("**Active grants**")
    for g in adm.grants():
        c = st.columns([6, 1])
        c[0].markdown(f"{esc(g['username'])} · {esc(g['workspace'])} {esc(g['system'])}/{esc(g['client'])} · "
                      f"`{esc(g['tcodes'])}` · until **{esc(g['valid_to'].replace('T', ' ')[:16])}** · "
                      f"by {esc(g['granted_by'])} · {esc(g['reason'])}", unsafe_allow_html=True)
        if c[1].button("Revoke", key=f"gv{g['id']}"):
            adm.revoke_grant(g["id"])
            st.rerun()


# =================================================================================================== Scripts
RISKY = re.compile(r"btn\[11\]|SendVKey\s*\(?\s*11|Key\s+11|\.Press|WScript\.Shell|\.Run\b|\.Exec|Execute|"
                   r"GetObject|CreateObject|Delete|Go\s*\(?\s*\"", re.I)


def page_script_approval() -> None:
    if not _need_admin():
        return
    ex = executor()
    page_header("Script Approval", "Review drafts and unapproved scripts. Approval is bound to the exact content.")
    infos = ex.library.list()
    todo = [i for i in infos if not i.approval and not i.scan["read_only"]] + \
           [i for i in infos if i.path.startswith("drafts/") and (i.approval or i.scan["read_only"])]
    st.markdown(f"**Needs review: {len(todo)}** · total scripts {len(infos)}")
    pick = st.selectbox("Script", [i.path for i in (todo or infos)])
    info = next(i for i in infos if i.path == pick)
    text = ex.library.resolve(pick).read_text(encoding="utf-8", errors="replace")
    c = st.columns([3, 2])
    with c[0]:
        lines = []
        for n, ln in enumerate(text.splitlines(), 1):
            style = "background:#FFEAF4;" if RISKY.search(ln.split("'", 1)[0]) else ""
            lines.append(f'<div style="{style}font-family:Consolas,monospace;font-size:12px;white-space:pre">'
                         f'{n:>3}  {esc(ln)}</div>')
        st.markdown('<div class="sb-card" style="max-height:520px;overflow:auto">' + "".join(lines) + "</div>",
                    unsafe_allow_html=True)
    with c[1], st.container(border=True):
        st.markdown(f"**{esc(pick)}**  \n{esc(info.header.get('PURPOSE', ''))}")
        st.markdown(status_badge("S" if info.scan["read_only"] else "W",
                                 "read-only" if info.scan["read_only"] else "may post data"), unsafe_allow_html=True)
        for f in info.scan["findings"]:
            st.markdown(f"- {esc(f)}")
        st.caption(f"content hash {info.digest[:12]} · approved: {bool(info.approval)}")
        folder = st.selectbox("Store in", ["library", "custom"]) if pick.startswith("drafts/") else None
        prod = st.checkbox("Also allow on PRODUCTION (still needs a grant + per-run approval)")
        if st.button("Approve this content", type="primary"):
            if folder:
                info = ex.library.promote(pick, folder, ex.gov.username(), prod)
            else:
                ex.gov.store.approve_script(info.path, info.digest, ex.gov.username(), prod)
            if ex.gov.hub_mode:            # publish centrally: every laptop gets it on its next sync
                adm = admin()
                if hasattr(adm, "publish_script"):
                    adm.publish_script(info.path, ex.library.resolve(info.path).read_text(encoding="utf-8"), prod)
            st.success("Approved" + (" and published to the Hub." if ex.gov.hub_mode else "."))
            st.rerun()
        if info.approval and st.button("Revoke approval"):
            ex.gov.store.revoke_script(info.path, info.digest, ex.gov.username())
            st.rerun()
        if pick.startswith("drafts/") and st.button("Reject draft (delete)"):
            ex.library.resolve(pick).unlink()
            ex.gov.store.audit(ex.gov.username(), "", "SCRIPT_DRAFT_REJECTED", {"path": pick})
            st.rerun()


# =================================================================================================== Audit
def page_audit() -> None:
    ex = executor()
    if not ex.gov.is_lead_or_admin():
        st.error("Admin or Engagement Lead role required.")
        return
    page_header("Audit Log", "Every run, approval, grant, script and workspace change.")
    c = st.columns(3)
    ws = c[0].selectbox("Workspace", ["(all)"] + [w.id for w in wsmod.list_all()])
    ev = c[1].text_input("Event contains (e.g. APPROVAL, GRANT, TASK)")
    lim = c[2].number_input("Rows", 50, 5000, 500, step=50)
    rows = admin().audit_rows(int(lim), None if ws == "(all)" else ws, ev or None)
    df = pd.DataFrame(rows)
    st.dataframe(df, hide_index=True, use_container_width=True)
    if not df.empty:
        st.download_button("Export CSV", df.to_csv(index=False).encode("utf-8-sig"), "sapbot_audit.csv")


# =================================================================================================== Features
def page_features() -> None:
    if not _need_admin():
        return
    ex = executor()
    s = get_settings()
    adm = admin()
    page_header("Features", "Everything ships dormant - switch capabilities on when you are ready (ADR E1).")
    scope = st.selectbox("Scope", ["firm"] + [w.id for w in wsmod.list_all()],
                         help="Workspace scope overrides the firm setting for that client only")
    cur = ex.gov.all_features(None if scope == "firm" else scope)
    for f in FEATURES:
        c = st.columns([1, 4])
        new = c[0].toggle(f, value=cur[f], key=f"ft_{scope}_{f}")
        c[1].caption(FEATURE_HELP.get(f, ""))
        if new != cur[f]:
            try:
                adm.set_flag(f, new, scope)
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
    st.divider()
    with st.container(border=True):
        st.markdown("**Local / small model endpoint** (activate when the hosted model is ready)")
        c = st.columns(3)
        url = c[0].text_input("Base URL (OpenAI-compatible)", s.models.local_model.base_url,
                              placeholder="http://hub-server:8000/v1")
        model = c[1].text_input("Model name", s.models.local_model.model)
        key = c[2].text_input("API key (optional)", type="password")
        cc = st.columns(3)
        if cc[0].button("Save endpoint"):
            save_settings_patch({"models": {"local_model": {"base_url": url, "model": model}}})
            if key:
                from ..agent.llm import set_secret
                set_secret("local_model_api_key", key)
            st.success("Saved.")
        if cc[1].button("Test connection"):
            from ..agent.llm import OpenAICompatLLM
            st.json(OpenAICompatLLM.test_connection(url, model, key))
        st.caption("Sequence: Save → Test → run the evaluation tasks (Learning page) → enable 'local_model' and "
                   "'router'. The router only uses the local model for T-codes that passed the evaluation.")
    with st.container(border=True):
        st.markdown("**Claude models (API mode)**")
        c = st.columns(2)
        m1 = c[0].text_input("Default model", s.models.claude_model)
        m2 = c[1].text_input("Escalation model", s.models.claude_escalation_model)
        if st.button("Save models"):
            save_settings_patch({"models": {"claude_model": m1, "claude_escalation_model": m2}})
            st.success("Saved - restart the Cockpit to apply.")


# =================================================================================================== Learning
def page_learning() -> None:
    ex = executor()
    if not ex.gov.is_lead_or_admin():
        st.error("Admin or Engagement Lead role required.")
        return
    page_header("Learning", "Gold runs, RAG coverage and hit rate, small-model readiness (FSD 3.6.6 / 3.7.6).")
    for ws in wsmod.list_all():
        res = ex.resources(ws)
        runs = res.logger.list_runs(5000)
        q = pd.Series([r.get("quality") or "open" for r in runs]).value_counts().to_dict() if runs else {}
        audit = ex.gov.store.audit_rows(5000, ws.id, "TASK_BEGIN")
        hits = [json.loads(a["detail"]).get("rag_hits", 0) for a in audit]
        rate = (sum(1 for h in hits if h) / len(hits) * 100) if hits else 0
        stats = res.rag.get_stats() if res.rag else {"indexed": 0, "per_t_code": {}, "embedder": "-"}
        dpo = ws.trajectories_dir / "dpo_pairs.jsonl"
        n_dpo = len(dpo.read_text(encoding="utf-8").splitlines()) if dpo.exists() else 0
        with st.container(border=True):
            st.markdown(f"### {esc(ws.name)} <small>({esc(ws.id)})</small>", unsafe_allow_html=True)
            c = st.columns(5)
            c[0].markdown(tile("Runs", len(runs)), unsafe_allow_html=True)
            c[1].markdown(tile("Gold", q.get("gold", 0), f"silver {q.get('silver', 0)} · failed {q.get('failed', 0)}"),
                          unsafe_allow_html=True)
            c[2].markdown(tile("Indexed examples", stats["indexed"], stats.get("embedder", "")),
                          unsafe_allow_html=True)
            c[3].markdown(tile("RAG hit rate", f"{rate:.0f}%", f"{len(hits)} tasks"), unsafe_allow_html=True)
            c[4].markdown(tile("DPO pairs", n_dpo, "local → Claude fixes"), unsafe_allow_html=True)
            if stats["per_t_code"]:
                st.bar_chart(pd.Series(stats["per_t_code"], name="gold examples"), height=220)
            with st.expander("Small-model evaluation gate per T-code"):
                for t in stats["per_t_code"]:
                    k = f"eval_pass:{ws.id}:{t}"
                    v = ex.gov.store.kv_get(k) == "1"
                    if st.checkbox(f"{t} passed evaluation", value=v, key=k) != v:
                        ex.gov.store.kv_set(k, "0" if v else "1")
                        ex.gov.store.audit(ex.gov.username(), ws.id, "EVAL_GATE", {"tcode": t, "pass": not v})
    if ex.gov.is_admin() and st.button("Export training data (gold JSONL + DPO pairs)"):
        from ..learning.distill import export_training
        st.json(export_training(ex.gov, None, None))


# =================================================================================================== Hub
def page_hub() -> None:
    if not _need_admin():
        return
    import json as _json
    from pathlib import Path

    from ..config import machine_locked
    from ..security.signing import fingerprint
    ex = executor()
    s = get_settings()
    page_header("Hub", "Central users, grants, signed policy, script library and sync (office server).")
    b = ex.gov.bundle or {}
    pk = Path(s.hub.public_key_file) if s.hub.public_key_file else None
    st.dataframe(pd.DataFrame([
        {"Item": "Hub URL", "Value": s.hub.url or "not configured"},
        {"Item": "Enrolled / Hub mode", "Value": f"{ex.gov.enrolled} / {ex.gov.hub_mode}"},
        {"Item": "IT machine policy", "Value": str(machine_locked())},
        {"Item": "Policy issued / expires", "Value": f"{b.get('issued', '-')} / {b.get('expires', '-')}"},
        {"Item": "Public key fingerprint", "Value": fingerprint(pk.read_bytes()) if pk and pk.exists() else "-"},
        {"Item": "Last sync", "Value": _json.dumps(_json.loads(ex.gov.store.kv_get("hub_last_sync") or "null"))[:200]},
        {"Item": "Last sync error", "Value": ex.gov.store.kv_get("hub_last_error") or "-"},
    ]), hide_index=True, use_container_width=True)
    st.caption("Enrol a laptop with `sapbot enroll --hub https://<server>:8443 --pubkey <file> --fingerprint <fp>` "
               "(DEPLOYMENT.md section 6). After enrolment users, grants and features come only from the Hub.")
    if s.hub.url and st.button("Sync now"):
        from ..hub.client import HubClient
        try:
            st.json(HubClient.from_settings(ex.gov).sync(ex))
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
