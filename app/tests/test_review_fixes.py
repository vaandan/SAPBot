"""Regression tests for the security review findings."""
import json

import pytest

from sapbot import workspaces
from sapbot.security.policy import Kind


@pytest.fixture()
def ex_prod_ro(home, approvals):
    from sapbot.runtime import build
    from sapbot.sap.fake import FakeSapApp
    app = FakeSapApp()
    app.logon("SUP", "300")
    workspaces.create("acme", "ACME", [{"sid": "SUP", "client": "300", "env": "PROD"}], allowed_tcodes=["*"])
    e = build("cowork", app_factory=lambda: app, approval_callback=approvals)
    e.set_workspace("acme")
    e.execute("task_begin", {"task": "t"})
    return e


def test_okcd_via_field_updates_rejected(ex_prod_ro):
    ex = ex_prod_ro
    ex.execute("sap_act", {"reasoning": "x", "ok_code": "/nSE16N"})
    r = ex.execute("sap_act", {"reasoning": "x", "field_updates_json": json.dumps(
        {"wnd[0]/tbar[0]/okcd": "/nVA01"}), "vkey": 0})
    assert not r.ok and "not allowed" in r.error


def test_foreign_session_ids_rejected(ex_prod_ro):
    r = ex_prod_ro.execute("sap_act", {"reasoning": "x", "field_updates_json": json.dumps(
        {"/app/con[0]/ses[0]/wnd[0]/usr/ctxtX": "1"})})
    assert not r.ok and "wnd[" in r.error


def test_function_code_inside_transaction_is_write(ex_prod_ro):
    ex = ex_prod_ro
    ex.execute("sap_act", {"reasoning": "x", "ok_code": "/nVA01"})
    r = ex.execute("sap_act", {"reasoning": "x", "ok_code": "SICH"})
    assert not r.ok and "production is read-only" in r.error


def test_slash_star_and_debugger(ex_prod_ro, approvals):
    ex = ex_prod_ro
    ex.execute("sap_act", {"reasoning": "x", "ok_code": "/nSE16N"})
    approvals.answer = False
    r = ex.execute("sap_act", {"reasoning": "x", "ok_code": "/*SE14"})
    assert not r.ok and "hard-stop" in r.error
    r = ex.execute("sap_act", {"reasoning": "x", "ok_code": "/h"})
    assert not r.ok and ("hard-stop" in r.error or "read-only" in r.error)


def test_classify_risky_clicks_and_popups(ex):
    pol = ex.policy
    c = pol.classify_act("", [], "wnd[0]/tbar[1]/btn[25]", None, "ME23N", {"exists": True, "text": "",
                                                                         "tooltip": "Display <-> Change"})
    assert c["kind"] == Kind.WRITE
    c = pol.classify_act("", [], "wnd[1]/usr/btnSPOP-OPTION1", None, "ME23N", {"exists": True, "text": "Yes",
                                                                             "tooltip": ""})
    assert c["kind"] == Kind.WRITE
    c = pol.classify_act("", [], "wnd[1]/tbar[0]/btn[12]", None, "SE16N", {"exists": True, "text": "",
                                                                         "tooltip": "Cancel"})
    assert c["kind"] == Kind.INPUT
    c = pol.classify_act("", [], "", 0, "ME23N", None, {"title": "Save changes?", "texts": ["Do you want to save?"]})
    assert c["kind"] == Kind.WRITE
    c = pol.classify_act("", [], "", 5, "COR3")
    assert c["kind"] == Kind.WRITE                       # unknown vkey -> write-risk
    c = pol.classify_act("=BACK", [], "", None, "COR3")
    assert c["kind"] == Kind.INPUT


def test_hard_stop_input_needs_write_permission_on_qa(ex):
    ws = workspaces.load("acme")
    ws.systems[0].write = False
    ws.save()
    ex.execute("task_begin", {"task": "t"})
    d = ex.policy.decide(
        __import__("sapbot.security.policy", fromlist=["ActionIntent"]).ActionIntent(
            "sap_act", Kind.WRITE, "cowork", tcode="SM30", write_reasons=["VKey 11"]),
        workspaces.load("acme"), workspaces.load("acme").systems[0])
    assert d.verdict.value == "deny"


def test_draft_scripts_need_approval(ex, approvals):
    ex.library.save_draft("my_check", "' PURPOSE: x\n' PARAMS: none\n' RESULT: x\nGo \"COR3\"\n", "u")
    ex.execute("task_begin", {"task": "t"})
    approvals.answer = False
    r = ex.execute("run_saved_script", {"reasoning": "x", "script": "drafts/my_check.vbs"})
    assert not r.ok and approvals.seen and "draft" in approvals.seen[-1][1]


def test_scanner_bypasses_closed():
    from sapbot.scripts.library import scan
    for code in ['Go "COR3"\nx="it\'s": s.FindById("wnd[0]/tbar[0]/btn[11]").Press',
                 'Go "COR3"\ns.FindById("wnd[0]").sendVKey &HB',
                 'Go "COR3"\nsession.StartTransaction "VA02"',
                 'Go "COR3"\ns.FindById("wnd[0]/tbar[0]/btn[1" & "1]").Press']:
        assert scan(code, ["COR3"])["read_only"] is False


def test_four_eyes(home):
    from sapbot.security.store import Store
    st = Store()
    st.upsert_user("lead1", "L", "lead")
    st.set_member("acme", "lead1", "lead")
    rid = st.add_request("lead1", "acme", "SUP", "300", "*", 60, "self")
    with pytest.raises(PermissionError):
        st.decide_request(rid, True, "lead1")


def test_prod_task_batch_refused(ex_prod_ro, tmp_path):
    import pandas as pd

    from sapbot.agent.batch import BatchSpec, run_batch
    ex = ex_prod_ro
    ex.execute("task_end", {"outcome": "aborted", "summary": ""})
    xl = tmp_path / "x.xlsx"
    pd.DataFrame({"A": ["1"]}).to_excel(xl, index=False)
    with pytest.raises(PermissionError):
        run_batch(ex, BatchSpec(excel=str(xl), mode="task", task_template="do {A}"))
