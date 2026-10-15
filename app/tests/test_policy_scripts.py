import json
from pathlib import Path

import pytest

from sapbot import workspaces
from sapbot.scripts.library import ScriptLibrary, header, scan

SEED = Path(__file__).resolve().parent / "fixtures" / "sapgate_scripts"


def test_scan_seed_scripts():
    cor3 = (SEED / "library" / "pp_cor3_order_overview.vbs").read_text()
    rel = (SEED / "library" / "pp_cor2_release.vbs").read_text()
    assert scan(cor3, ["COR3"])["read_only"] is True
    s = scan(rel, ["COR3"])
    assert not s["read_only"] and any("Save" in f for f in s["findings"])
    assert header(rel)["PARAMS"].startswith("AUFNR")


def test_import_carries_matching_approvals(ex, tmp_path):
    from sapbot.scripts.library import sha256_text
    content = (SEED / "library" / "pp_cor2_release.vbs").read_text()
    appr = tmp_path / "approvals.json"
    appr.write_text(json.dumps({"library/pp_cor2_release.vbs": {sha256_text(content): {
        "approved_at": "2026-10-12", "allow_non_qa": False}},
        "library/pp_cor7_convert.vbs": {"deadbeef": {"approved_at": "x"}}}))
    res = ex.library.import_folder(SEED, appr)
    assert "library/pp_cor2_release.vbs" in res["imported"]
    assert res["approvals_carried_over"] == ["library/pp_cor2_release.vbs"]   # cor7 hash mismatch -> not carried


def test_script_policy_on_qa(ex, approvals):
    ex.library.import_folder(SEED)
    ex.execute("task_begin", {"task": "release order"})
    r = ex.execute("run_saved_script", {"reasoning": "x", "script": "library/pp_cor3_order_overview.vbs",
                                        "params_json": '{"AUFNR": "1000000993"}'})
    assert r.ok and r.policy["verdict"] == "allow" and r.data["results"]["STATUS"] == "REL"
    assert not approvals.seen
    r = ex.execute("run_saved_script", {"reasoning": "x", "script": "library/pp_cor2_release.vbs",
                                        "params_json": '{"AUFNR": "1000000993"}'})
    assert approvals.seen and "not yet approved" in approvals.seen[-1][1]
    bad = ex.execute("run_saved_script", {"reasoning": "x", "script": "library/pp_cor3_order_overview.vbs",
                                          "params_json": '{"aufnr": "1"}'})
    assert not bad.ok and "UPPER_CASE" in bad.error


@pytest.fixture()
def ex_prod(home, approvals):
    from sapbot.runtime import build
    from sapbot.sap.fake import FakeSapApp
    app = FakeSapApp()
    app.logon("SUP", "300")
    workspaces.create("acme", "ACME", [{"sid": "SUP", "client": "300", "env": "PROD"}], allowed_tcodes=["VA01"])
    e = build("cowork", app_factory=lambda: app, approval_callback=approvals,
              script_run_fn=lambda a, t: (0, "RESULT|STATUS_MSG|S: saved\n", ""))
    e.set_workspace("acme")
    return e


def test_prod_read_only_without_grant(ex_prod):
    ex = ex_prod
    ex.execute("task_begin", {"task": "t"})
    assert ex.execute("sap_act", {"reasoning": "x", "ok_code": "/nVA01"}).ok
    r = ex.execute("sap_act", {"reasoning": "x", "field_updates_json": '{"wnd[0]/usr/ctxtVBAK-AUART": "OR"}'})
    assert not r.ok and "production is read-only" in r.error


def test_prod_with_grant_still_needs_per_save_approval(ex_prod, approvals):
    ex = ex_prod
    ex.gov.store.add_grant(ex.gov.username(), "acme", "SUP", "300", "VA01", 60, "go-live fix", "lead")
    ex.execute("task_begin", {"task": "t"})
    ex.execute("sap_act", {"reasoning": "x", "ok_code": "/nVA01"})
    r = ex.execute("sap_act", {"reasoning": "x", "field_updates_json": '{"wnd[0]/usr/ctxtVBAK-AUART": "OR"}',
                               "vkey": 0})
    assert r.ok and not approvals.seen                 # input with grant: allowed
    approvals.answer = False
    r = ex.execute("sap_act", {"reasoning": "x", "field_updates_json": '{"wnd[0]/usr/ctxtKUAGV-KUNNR": "1"}',
                               "vkey": 11})
    assert not r.ok and approvals.seen and "production save" in approvals.seen[-1][1]


def test_prod_script_needs_prod_approval(ex_prod):
    ex = ex_prod
    ex.library.import_folder(SEED)
    ex.gov.store.add_grant(ex.gov.username(), "acme", "SUP", "300", "*", 60, "x", "lead")
    ex.execute("task_begin", {"task": "t"})
    r = ex.execute("run_saved_script", {"reasoning": "x", "script": "library/pp_cor2_release.vbs",
                                        "params_json": '{"AUFNR": "1"}'})
    assert not r.ok and "not approved for production" in r.error


def test_restricted_after_enrolment_without_valid_policy(home):
    from sapbot.security.governance import Governance
    from sapbot.security.store import Store
    st = Store()
    st.bootstrap()
    g = Governance(st)
    assert g.is_admin()
    st.kv_set("hub_enrolled", "1")
    g2 = Governance(st)
    assert g2.restricted and g2.role() == "consultant" and g2.active_grants() == []


def test_signed_policy_roundtrip(home, tmp_path):
    import datetime as dt
    from sapbot import config
    from sapbot.security import signing
    from sapbot.security.governance import Governance
    from sapbot.security.store import Store
    priv, pub = tmp_path / "k.pem", tmp_path / "k.pub"
    signing.generate_keypair(priv, pub)
    config.save_settings_patch({"hub": {"public_key_file": str(pub)}, "hub_bind_os_user": False})
    payload = {"user": {"username": "rkumar", "role": "consultant"}, "memberships": [{"workspace": "acme",
               "ws_role": "consultant"}], "grants": [], "flags": {"firm": {"claude_api_mode": True}},
               "expires": (dt.datetime.now() + dt.timedelta(days=7)).isoformat()}
    bundle = signing.sign(payload, priv.read_bytes())
    g = Governance(Store())
    g.install_bundle(bundle)
    assert g.hub_mode and g.username() == "rkumar" and g.feature("claude_api_mode")
    bundle["payload"]["user"]["role"] = "admin"          # tampering
    with pytest.raises(ValueError):
        g.install_bundle(bundle)


def test_machine_policy_locks_laptop(home, tmp_path, monkeypatch):
    from sapbot import config
    from sapbot.security.governance import Governance
    from sapbot.security.store import Store
    md = tmp_path / "ProgramData" / "SAPBot"
    md.mkdir(parents=True)
    (md / "machine.yaml").write_text("require_hub: true\nhub:\n  url: https://hub.office.local:8443\n")
    monkeypatch.setenv("SAPBOT_MACHINE_DIR", str(md))
    config.get_settings.cache_clear()
    st = Store()
    st.bootstrap()
    g = Governance(st)
    assert st.list_users() == []                       # no local admin on an IT-managed laptop
    assert g.enrolled and g.restricted and g.role() == "consultant"
    assert config.get_settings().hub.url == "https://hub.office.local:8443"
    config.save_settings_patch({"hub": {"url": "https://evil"}})
    assert config.get_settings().hub.url == "https://hub.office.local:8443"   # user file cannot override
