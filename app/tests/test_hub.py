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
