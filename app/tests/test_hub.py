import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("SAPBOT_HUB_HOME", str(tmp_path / "hub"))
    from sapbot.hub.app import HubState, create_app
    st = HubState()
    st.store.upsert_user("vandan", "Vandan", "admin")
    admin_tok = st.store.issue_token("vandan")
    st.store.upsert_user("rkumar", "R Kumar", "consultant")
    tok = st.store.issue_token("rkumar")
    st.save_workspace({"id": "acme", "name": "ACME", "systems": [{"sid": "SUQ", "client": "400", "env": "QA",
                                                                    "write": True}]})
    st.store.set_member("acme", "rkumar", "consultant")
    st.store.set_flag("claude_api_mode", True)
    app = create_app(st)
    return st, app, admin_tok, tok


def test_policy_and_permissions(hub):
    st, app, admin_tok, tok = hub
    c = TestClient(app)
    assert c.get("/api/policy").status_code == 401
    b = c.get("/api/policy", headers={"Authorization": f"Bearer {tok}"}).json()
    assert b["payload"]["user"]["role"] == "consultant" and b["payload"]["workspaces"][0]["id"] == "acme"
    r = c.post("/api/users", json={"username": "x", "role": "admin"}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403                                  # consultant cannot create users
    r = c.post("/api/grants", json={"username": "rkumar", "workspace": "acme", "system": "SUQ", "client": "400",
                                    "minutes": 60}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403                                  # nor grant himself
    r = c.post("/api/grants", json={"username": "rkumar", "workspace": "acme", "system": "SUQ", "client": "400",
                                    "minutes": 60, "reason": "test"}, headers={"Authorization": f"Bearer {admin_tok}"})
    assert r.status_code == 200
    b = c.get("/api/policy", headers={"Authorization": f"Bearer {tok}"}).json()
    assert len(b["payload"]["grants"]) == 1


def test_laptop_sync(hub, ex, home):
    from sapbot import config
    from sapbot.hub.client import HubClient
    from sapbot.security.governance import Governance
    st, app, admin_tok, tok = hub
    config.save_settings_patch({"hub": {"url": "http://hub", "public_key_file": str(st.pub)}, "hub_bind_os_user": False})
    ex.gov.settings = config.get_settings()
    hc = HubClient("http://hub", tok)
    hc.http = TestClient(app, headers={"Authorization": f"Bearer {tok}"})
    st.store.approve_script("library/x.vbs", "0" * 64, "vandan")   # bad digest/no file -> ignored
    rep = hc.sync(ex)
    assert rep["user"]["username"] == "rkumar" and rep["workspaces"] == 1
    g = Governance(ex.gov.store)
    assert g.hub_mode and g.enrolled and g.username() == "rkumar" and g.role() == "consultant"
    assert g.feature("claude_api_mode")
    assert st.store.audit_rows(10, event="BOOTSTRAP")            # laptop audit uploaded


def test_enroll_flow(hub, ex, home, monkeypatch):
    from sapbot import config, paths
    from sapbot.hub.client import enroll
    from sapbot.security.signing import fingerprint
    st, app, admin_tok, tok = hub
    config.save_settings_patch({"hub_bind_os_user": False})
    fp = fingerprint(st.pub.read_bytes())
    with pytest.raises(ValueError):
        enroll(ex, "https://hub.office.local:8443", str(st.pub), tok, expected_fingerprint="0000:1111",
               http=TestClient(app, headers={"Authorization": f"Bearer {tok}"}))
    with pytest.raises(ValueError):
        enroll(ex, "http://hub.office.local:8443", str(st.pub), tok,
               http=TestClient(app, headers={"Authorization": f"Bearer {tok}"}))
    rep = enroll(ex, "https://hub.office.local:8443", str(st.pub), tok, expected_fingerprint=fp,
                 http=TestClient(app, headers={"Authorization": f"Bearer {tok}"}))
    assert rep["enrolled"] and rep["public_key_fingerprint"] == fp
    s = config.get_settings()
    assert s.hub.url == "https://hub.office.local:8443"
    assert (paths.config_dir() / "hub_public.pem").read_bytes() == st.pub.read_bytes()
    assert ex.gov.enrolled and ex.gov.username() == "rkumar"


def test_hub_cli(tmp_path, monkeypatch, capsys):
    import json as _j
    monkeypatch.setenv("SAPBOT_HUB_HOME", str(tmp_path / "hub2"))
    from sapbot.hub.app import HubState, main
    main(["init", "--admin", "Vandan"])
    init = _j.loads(capsys.readouterr().out)
    assert init["admin"] == "vandan" and init["admin_token"].startswith("sbt_") and ":" in init["public_key_fingerprint"]
    main(["workspace", "create", "acme", "--name", "ACME", "--system", "SUQ/400/QA/write", "--system", "SUP/300/PROD"])
    main(["user", "rkumar", "--workspace", "acme"])
    capsys.readouterr()
    main(["users"])
    u = _j.loads(capsys.readouterr().out)
    assert any(m["username"] == "rkumar" and m["workspace"] == "acme" for m in u["members"])
    st = HubState()
    assert st.workspaces()[0]["systems"][1]["env"] == "PROD"
    with pytest.raises(SystemExit):
        main(["serve", "--port", "1"])                         # no cert, no --insecure-lan-test


def test_hub_feature_cli(tmp_path, monkeypatch, capsys):
    import json as _j
    monkeypatch.setenv("SAPBOT_HUB_HOME", str(tmp_path / "hub3"))
    from sapbot.hub.app import main
    main(["feature", "set", "claude_api_mode", "on"])
    assert _j.loads(capsys.readouterr().out)["flags"]["claude_api_mode"] is True


def test_publish_script_and_sync(hub, ex, home):
    from sapbot import config
    from sapbot.hub.client import HubAdmin, HubClient
    st, app, admin_tok, tok = hub
    config.save_settings_patch({"hub": {"url": "http://hub", "public_key_file": str(st.pub)}, "hub_bind_os_user": False})
    ex.gov.settings = config.get_settings()
    ac = HubClient("http://hub", admin_tok)
    ac.http = TestClient(app, headers={"Authorization": f"Bearer {admin_tok}"})
    body = "' PURPOSE: x\n' PARAMS: none\n' RESULT: x\nGo \"COR3\"\n"
    HubAdmin(ac).publish_script("library/team_check.vbs", body)
    hc = HubClient("http://hub", tok)
    hc.http = TestClient(app, headers={"Authorization": f"Bearer {tok}"})
    assert hc.sync(ex)["scripts"] == 1
    assert ex.library.info("library/team_check.vbs").approval


def test_policy_refresh_across_processes(hub, home):
    from sapbot import config
    from sapbot.security.governance import Governance
    from sapbot.security.store import Store
    st, app, admin_tok, tok = hub
    config.save_settings_patch({"hub": {"public_key_file": str(st.pub)}, "hub_bind_os_user": False})
    store = Store()
    running = Governance(store)                 # e.g. the Cowork tool server
    assert not running.hub_mode
    Governance(store).install_bundle(st.policy_for("rkumar"))   # e.g. Cockpit "Sync now"
    running._checked = 0                        # skip the 15 s throttle
    assert running.hub_mode and running.username() == "rkumar"


def test_hub_user_update_keeps_role(tmp_path, monkeypatch, capsys):
    import json as _j
    monkeypatch.setenv("SAPBOT_HUB_HOME", str(tmp_path / "hub4"))
    from sapbot.hub.app import HubState, main
    main(["user", "lead1", "--role", "lead"])
    assert "token" in _j.loads(capsys.readouterr().out)
    main(["user", "lead1", "--disable"])
    r = _j.loads(capsys.readouterr().out)
    assert r["role"] == "lead" and r["active"] is False and "token" not in r
    main(["user", "lead1", "--enable", "--new-token"])
    r = _j.loads(capsys.readouterr().out)
    assert r["role"] == "lead" and r["active"] is True and "token" in r
