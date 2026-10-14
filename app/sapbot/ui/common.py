"""Shared Cockpit helpers: theme, shell bar, cached runtime, background jobs (FSD 3.7)."""
from __future__ import annotations

import html
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import streamlit as st

from .. import workspaces as wsmod
from ..admin import AdminAPI, admin_api
from ..runtime import build, setup_logging
from ..tools.executor import ToolExecutor

CSS = (Path(__file__).parent / "fiori.css").read_text(encoding="utf-8")


@st.cache_resource(show_spinner=False)
def executor() -> ToolExecutor:
    """One executor per Cockpit process. Approvals appear in the Cockpit (db mode)."""
    setup_logging("cockpit", to_stderr=True)
    return build("cockpit", approval_mode="db")   # human-driven channel; the agent loop switches per run


def admin() -> AdminAPI:
    return admin_api(executor().gov)


def esc(x: Any) -> str:
    return html.escape(str(x if x is not None else ""))


def inject_theme() -> None:
    st.markdown(f"<style>{CSS}</style>", unsafe_allow_html=True)


def shell_bar() -> None:
    ex = executor()
    gov = ex.gov
    ws = ex.active_workspace()
    user = gov.username()
    initials = "".join(p[0] for p in user.replace(".", " ").split()[:2]).upper() or "?"
    mode = "Hub" if gov.hub_mode else ("Restricted" if gov.restricted else "Laptop")
    st.markdown(
        f"""<div class="sb-shell"><div class="sb-product">SAPBot <span>Cockpit</span></div>
        <div class="sb-chip">{esc(ws.name if ws else 'No client selected')}</div><div class="sb-spacer"></div>
        <div class="sb-chip">{esc(mode)} mode</div><div class="sb-chip">{esc(gov.role().title())}</div>
        <div class="sb-avatar" title="{esc(user)}">{esc(initials)}</div></div>""", unsafe_allow_html=True)


def page_header(title: str, subtitle: str = "") -> None:
    st.markdown(f'<div class="sb-objhdr"><h1>{esc(title)}</h1><div class="sub">{esc(subtitle)}</div></div>',
                unsafe_allow_html=True)


def status_badge(t: str, text: str = "") -> str:
    t = (t or "").upper() or "N"
    label = {"S": "Success", "W": "Warning", "E": "Error", "A": "Abort", "I": "Info", "N": "No message"}.get(t, t)
    return f'<span class="sb-status {esc(t)}">{esc(label)}</span> {esc(text)}'


def env_badge(sysm: wsmod.SapSystem) -> str:
    env = '<span class="sb-status PROD">PROD</span>' if sysm.is_prod else f'<span class="sb-status QA">{sysm.env}</span>'
    return env


def tile(title: str, kpi: Any, foot: str = "") -> str:
    return (f'<div class="sb-tile"><div class="t">{esc(title)}</div><div class="kpi">{esc(kpi)}</div>'
            f'<div class="foot">{esc(foot)}</div></div>')


def context_bar() -> wsmod.Workspace | None:
    """Workspace picker + system / mode chips, shown on consultant pages."""
    ex = executor()
    gov = ex.gov
    allowed = gov.allowed_workspaces()
    wss = [w for w in wsmod.list_all() if "*" in allowed or w.id in allowed]
    cur = ex.active_workspace()
    if not wss:
        st.info("No client workspace assigned to you yet. An Admin creates and assigns workspaces.")
        return None
    ids = [w.id for w in wss]
    idx = ids.index(cur.id) if cur and cur.id in ids else 0
    c1, c2 = st.columns([1, 3])
    with c1:
        pick = st.selectbox("Client workspace", ids, index=idx, format_func=lambda i: next(w.name for w in wss
                                                                                       if w.id == i) + f" ({i})",
                            disabled=ex.task is not None)
    if cur is None or pick != cur.id:
        try:
            cur = ex.set_workspace(pick)
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
    with c2:
        chips = []
        grants = gov.active_grants()
        for s in cur.systems if cur else []:
            g = [x for x in grants if x["system"] == s.sid and str(x["client"]) == s.client]
            if g:
                mode = f'<span class="sb-status RW">WRITE until {esc(g[0]["valid_to"][11:16])}</span>'
            elif s.write and not s.is_prod:
                mode = '<span class="sb-status RW">WRITE</span>'
            else:
                mode = '<span class="sb-status RO">READ-ONLY</span>'
            chips.append(f'<div><div class="lbl">System</div><div class="val">{esc(s.sid)}/{esc(s.client)} '
                         f'{env_badge(s)} {mode}</div></div>')
        pol = cur.data_policy if cur else "-"
        chips.append(f'<div><div class="lbl">Data policy</div><div class="val">{esc(pol)}</div></div>')
        st.markdown(f'<div class="sb-context">{"".join(chips)}</div>', unsafe_allow_html=True)
    return cur


# ------------------------------------------------------------------------------------------------- background jobs
@dataclass
class Job:
    kind: str
    title: str
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "running"         # running | done | error | stopped
    result: Any = None
    error: str = ""
    started: float = field(default_factory=time.time)
    stop: bool = False
    thread: threading.Thread | None = None


class JobManager:
    def __init__(self) -> None:
        self.current: Job | None = None
        self.history: list[Job] = []

    def busy(self) -> bool:
        return self.current is not None and self.current.status == "running"

    def start(self, kind: str, title: str, fn: Callable[[Job], Any]) -> Job:
        if self.busy():
            raise RuntimeError("another run is still in progress")
        job = Job(kind, title)

        def runner() -> None:
            try:
                job.result = fn(job)
                job.status = "stopped" if job.stop else "done"
            except Exception as e:  # noqa: BLE001
                job.status, job.error = "error", f"{type(e).__name__}: {e}"
        job.thread = threading.Thread(target=runner, name=f"sapbot-{kind}", daemon=True)
        self.current = job
        self.history.insert(0, job)
        self.history = self.history[:20]
        job.thread.start()
        return job


@st.cache_resource(show_spinner=False)
def jobs() -> JobManager:
    return JobManager()
