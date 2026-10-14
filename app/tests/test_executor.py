import json

from sapbot import workspaces


def test_status_and_task_flow_va01_gold(ex):
    st = ex.execute("sapbot_status", {})
    assert st.ok and st.data["active_workspace"]["id"] == "acme"
    tb = ex.execute("task_begin", {"task": "Create sales order OR for customer 1000 material M-01 qty 10"})
    assert tb.ok, tb.error
    assert tb.data["session"].endswith("ses[1]")       # own session, consultant keeps ses[0]
    r = ex.execute("sap_act", {"reasoning": "open VA01", "ok_code": "/nVA01"})
    assert r.ok, r.error
    r = ex.execute("sap_act", {"reasoning": "order type", "field_updates_json": json.dumps(
        {"wnd[0]/usr/ctxtVBAK-AUART": "OR"}), "vkey": 0})
    assert r.ok
    obs = ex.execute("sap_observe_screen", {"reasoning": "see overview"})
    assert any(f["id"].endswith("KUAGV-KUNNR") for f in obs.data["fields"])
    assert all(f.get("value") != "hunter2" for f in obs.data["fields"])   # password never leaves
    r = ex.execute("sap_act", {"reasoning": "fill + save", "field_updates_json": json.dumps(
        {"wnd[0]/usr/ctxtKUAGV-KUNNR": "1000", "wnd[0]/usr/txtRV45A-KWMENG": "10"}), "vkey": 11})
    assert r.ok and r.status_bar.type == "S" and "12345" in r.status_bar.text
    assert r.policy["verdict"] == "allow"                # QA with write=True -> no approval needed
    # read back in a display transaction (fake: COR3 shows order number in title)
    ex.execute("sap_act", {"reasoning": "verify", "ok_code": "/nCOR3"})
    ex.execute("sap_act", {"reasoning": "verify", "field_updates_json": json.dumps(
        {"wnd[0]/usr/ctxtCAUFVD-AUFNR": "12345"}), "vkey": 0})
    ex.execute("sap_observe_screen", {"reasoning": "read back"})
    end = ex.execute("task_end", {"outcome": "success", "summary": "Order 12345 created"})
    assert end.ok and end.data["quality"] == "gold", end.data
    # gold run is now retrievable
    rag = ex.execute("rag_search_trajectories", {"reasoning": "x", "query": "create sales order for customer 1000"})
    assert rag.data["hits"] and rag.data["hits"][0]["similarity"] > 0.3
    ws = workspaces.load("acme")
    assert (ws.trajectories_dir / "gold_trajectories.jsonl").exists()


def test_unverified_posting_is_silver(ex):
    ex.execute("task_begin", {"task": "create order"})
    ex.execute("sap_act", {"reasoning": "a", "ok_code": "/nVA01"})
    ex.execute("sap_act", {"reasoning": "a", "field_updates_json": '{"wnd[0]/usr/ctxtVBAK-AUART": "OR"}', "vkey": 0})
    ex.execute("sap_act", {"reasoning": "a", "field_updates_json": '{"wnd[0]/usr/ctxtKUAGV-KUNNR": "1"}', "vkey": 11})
    end = ex.execute("task_end", {"outcome": "success", "summary": "done"})
    assert end.data["quality"] == "silver"


def test_requires_task(ex):
    r = ex.execute("sap_observe_screen", {"reasoning": "x"})
    assert not r.ok and "task_begin" in r.error


def test_navigation_alone(ex):
    ex.execute("task_begin", {"task": "t"})
    r = ex.execute("sap_act", {"reasoning": "x", "ok_code": "/nVA01", "vkey": 0})
    assert not r.ok and "on its own" in r.error


def test_hard_stop_needs_approval_and_refusal_counts(ex, approvals):
    ex.execute("task_begin", {"task": "t"})
    approvals.answer = False
    r = ex.execute("sap_act", {"reasoning": "x", "ok_code": "/nSE14"})
    assert not r.ok and "hard-stop" in r.error and approvals.seen
    end = ex.execute("task_end", {"outcome": "success", "summary": ""})
    assert end.data["quality"] == "failed"


def test_non_whitelisted_tcode_needs_approval(ex, approvals):
    ex.execute("task_begin", {"task": "t"})
    r = ex.execute("sap_act", {"reasoning": "x", "ok_code": "/nME21N"})
    assert approvals.seen and "whitelist" in approvals.seen[-1][1]


def test_read_only_system_blocks_input_in_change_tcode(ex):
    ws = workspaces.load("acme")
    ws.systems[0].write = False
    ws.save()
    ex.execute("task_begin", {"task": "t"})
    assert ex.execute("sap_act", {"reasoning": "x", "ok_code": "/nVA01"}).ok      # navigation allowed
    r = ex.execute("sap_act", {"reasoning": "x", "field_updates_json": '{"wnd[0]/usr/ctxtVBAK-AUART": "OR"}'})
    assert not r.ok and "read-only" in r.error
    # display transactions still work
    assert ex.execute("sap_act", {"reasoning": "x", "ok_code": "/nSE16N"}).ok
    assert ex.execute("sap_act", {"reasoning": "x", "field_updates_json": '{"wnd[0]/usr/ctxtGD-TAB": "T001"}',
                                  "vkey": 8}).ok


def test_grant_enables_write_on_read_only_system(ex):
    ws = workspaces.load("acme")
    ws.systems[0].write = False
    ws.save()
    ex.gov.store.add_grant(ex.gov.username(), "acme", "SUQ", "400", "VA01", 60, "test", "admin")
    ex.execute("task_begin", {"task": "t"})
    ex.execute("sap_act", {"reasoning": "x", "ok_code": "/nVA01"})
    r = ex.execute("sap_act", {"reasoning": "x", "field_updates_json": '{"wnd[0]/usr/ctxtVBAK-AUART": "OR"}',
                               "vkey": 0})
    assert r.ok and r.policy["grant_id"]


def test_loop_guard(ex):
    ex.execute("task_begin", {"task": "t"})
    ex.execute("sap_act", {"reasoning": "x", "ok_code": "/nCOR3"})
    a = {"reasoning": "x", "vkey": 0}
    r1, r2 = ex.execute("sap_act", a), ex.execute("sap_act", a)
    r3 = ex.execute("sap_act", a)
    assert not r3.ok and "loop detected" in r3.error and r3.needs_human


def test_grid_and_masked_policy(ex):
    ws = workspaces.load("acme")
    ws.data_policy = "masked"
    ws.mask_fields = ["BUTXT"]
    ws.save()
    ex.execute("task_begin", {"task": "read T001"})
    ex.execute("sap_act", {"reasoning": "x", "ok_code": "/nSE16N"})
    ex.execute("sap_act", {"reasoning": "x", "field_updates_json": '{"wnd[0]/usr/ctxtGD-TAB": "T001"}', "vkey": 8})
    g = ex.execute("sap_read_grid", {"reasoning": "x", "max_rows": 5})
    assert g.ok and g.data["returned"] == 5 and g.data["rows"][0]["BUTXT"] == "<MASKED>"
    assert g.data["rows"][0]["BUKRS"] == "1000"


def test_local_only_workspace_refuses_cloud_channel(ex):
    ws = workspaces.load("acme")
    ws.data_policy = "local-only"
    ws.save()
    r = ex.execute("task_begin", {"task": "t"})
    assert not r.ok and "local-only" in r.error            # not even examples go to the cloud
    r = ex.execute("rag_search_trajectories", {"reasoning": "x", "query": "q"})
    assert not r.ok and "local-only" in r.error


def test_feature_gate(ex):
    ex.gov.store.set_flag("cowork_mode", False)
    r = ex.execute("task_begin", {"task": "t"})
    assert not r.ok and "cowork_mode" in r.error


def test_wrong_system_guard(ex):
    ws = workspaces.load("acme")
    ws.systems = [s for s in ws.systems if s.sid == "SUQ"]
    ws.save()
    ex.execute("task_begin", {"task": "t"})
    ws.systems = [workspaces.SapSystem(sid="SUD", client="100")]
    ws.save()
    r = ex.execute("sap_observe_screen", {"reasoning": "x"})
    assert not r.ok and "not part of workspace" in r.error


def test_file_workspace_write_and_read(ex, home):
    ex.execute("task_begin", {"task": "t"})
    w = ex.execute("local_file_workspace", {"reasoning": "x", "mode": "write_csv", "file_path": "out.csv",
                                            "content_json": json.dumps([{"A": "1", "B": "x"}])})
    assert w.ok
    r = ex.execute("local_file_workspace", {"reasoning": "x", "mode": "read_excel_or_csv",
                                            "file_path": w.data["file"]})
    assert r.ok and r.data["records"][0]["A"] == "1"
    bad = ex.execute("local_file_workspace", {"reasoning": "x", "mode": "write_text", "file_path": "/etc/passwd",
                                              "content_json": "x"})
    assert not bad.ok
