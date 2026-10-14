"""SAPBot Cockpit (FSD 3.7) - one Streamlit app, screens by role (ADR A12), SAP Fiori look (ADR E4).

Start: `sapbot cockpit` (opens http://localhost:8501).
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # started via `streamlit run .../sapbot/ui/app.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st  # noqa: E402

from sapbot.ui import pages_admin as pa  # noqa: E402
from sapbot.ui import pages_consultant as pc  # noqa: E402
from sapbot.ui.common import executor, inject_theme, shell_bar  # noqa: E402

st.set_page_config(page_title="SAPBot Cockpit", page_icon=":material/smart_toy:", layout="wide",
                   initial_sidebar_state="expanded")
inject_theme()

try:
    ex = executor()
except Exception as e:  # noqa: BLE001
    st.error(f"SAPBot could not start: {e}")
    st.stop()

gov = ex.gov
shell_bar()

consultant = [
    st.Page(pc.page_cockpit, title="Cockpit", icon=":material/space_dashboard:", default=True),
    st.Page(pc.page_workspaces, title="My Workspaces", icon=":material/apartment:", url_path="workspaces"),
    st.Page(pc.page_history, title="Run History", icon=":material/history:", url_path="history"),
    st.Page(pc.page_scripts, title="Script Library", icon=":material/code:", url_path="scripts"),
    st.Page(pc.page_batch, title="Batch Runs", icon=":material/table_view:", url_path="batch"),
    st.Page(pc.page_requests, title="Requests & Grants", icon=":material/key:", url_path="requests"),
    st.Page(pc.page_settings, title="Settings", icon=":material/settings:", url_path="settings"),
]
nav: dict[str, list] = {"Work": consultant}
if gov.is_lead_or_admin():
    lead_pages = [st.Page(pa.page_grants, title="Grants & Requests", icon=":material/verified_user:",
                          url_path="admin-grants"),
                  st.Page(pa.page_audit, title="Audit Log", icon=":material/fact_check:", url_path="admin-audit"),
                  st.Page(pa.page_learning, title="Learning", icon=":material/school:", url_path="admin-learning")]
    if gov.is_admin():
        lead_pages = [st.Page(pa.page_users, title="Users", icon=":material/group:", url_path="admin-users"),
                      st.Page(pa.page_ws_admin, title="Client Workspaces", icon=":material/domain_add:",
                              url_path="admin-workspaces")] + lead_pages + [
            st.Page(pa.page_script_approval, title="Script Approval", icon=":material/rule:",
                    url_path="admin-scripts"),
            st.Page(pa.page_features, title="Features", icon=":material/toggle_on:", url_path="admin-features"),
            st.Page(pa.page_hub, title="Hub", icon=":material/hub:", url_path="admin-hub")]
    nav["Admin" if gov.is_admin() else "Engagement Lead"] = lead_pages

with st.sidebar:
    st.markdown(f'<div class="sb-side-user">Signed in as <b>{gov.username()}</b> · {gov.role()}'
                f'{" · restricted (Hub policy expired)" if gov.restricted else ""}</div>', unsafe_allow_html=True)

st.navigation(nav, position="sidebar", expanded=True).run()
