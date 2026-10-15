"""SAPBot Hub - office Windows server, reached over LAN/VPN (ADR A9-A11, B12, B13).

  * users, roles, workspace memberships, grants, elevation requests, feature flags (central authority)
  * signed policy bundle per user (Ed25519) - laptops verify it and work offline until it expires
  * client workspace definitions, approved script library, team gold runs per workspace (RAG sharing)
  * audit upload from laptops
  * Claude API proxy (one firm key on the Hub; per-user monthly token cap)
  * later: the small model can be served next to it (vLLM) - laptops only need its URL

Run:   sapbot-hub init --admin vandan          (creates keys, DB, first admin + token)
       sapbot-hub serve --port 8443 --cert hub.crt --key hub.key
As a Windows service: see install/hub_service.ps1 (NSSM).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from ..security import signing
from ..security.store import Store

log = logging.getLogger("sapbot.hub")
POLICY_DAYS = 7


def hub_home() -> Path:
    return Path(os.environ.get("SAPBOT_HUB_HOME") or (r"C:\SAPBotHub" if sys.platform == "win32"
                                                       else Path.home() / ".sapbot-hub"))


class HubState:
    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home or hub_home())
        for d in ("data", "keys", "scripts/library", "scripts/custom", "workspaces", "downloads", "logs"):
            (self.home / d).mkdir(parents=True, exist_ok=True)
        self.store = Store(self.home / "data" / "hub.db")
        self.priv = self.home / "keys" / "policy_signing.pem"
        self.pub = self.home / "keys" / "policy_signing.pub.pem"
        if not self.priv.exists():
            signing.generate_keypair(self.priv, self.pub)

    # ---------------------------------------------------------------- workspaces (central)
    def ws_dir(self, wid: str) -> Path:
        if not wid.replace("-", "").replace("_", "").isalnum():
            raise ValueError("bad workspace id")
        return self.home / "workspaces" / wid

    def workspaces(self) -> list[dict[str, Any]]:
        out = []
        for f in sorted((self.home / "workspaces").glob("*/workspace.yaml")):
            out.append(yaml.safe_load(f.read_text(encoding="utf-8")))
        return out

    def save_workspace(self, data: dict[str, Any]) -> None:
        from ..workspaces import Workspace
        ws = Workspace.model_validate(data)   # validation only
        d = self.ws_dir(ws.id)
        (d / "gold").mkdir(parents=True, exist_ok=True)
        (d / "workspace.yaml").write_text(yaml.safe_dump(ws.model_dump(), sort_keys=False), encoding="utf-8")

    # ---------------------------------------------------------------- policy
    def policy_for(self, username: str) -> dict[str, Any]:
        u = self.store.get_user(username)
        if not u or not u["active"]:
            raise PermissionError("user inactive")
        members = [m for m in self.store.members() if m["username"] == username]
        wids = [m["workspace"] for m in members] if u["role"] != "admin" else [w["id"] for w in self.workspaces()]
        flags: dict[str, dict[str, bool]] = {"firm": self.store.flags("firm")}
        for w in wids:
            f = self.store.flags(w)
            if f:
                flags[w] = f
        payload = {
            "issuer": "sapbot-hub", "issued": dt.datetime.now().isoformat(timespec="seconds"),
            "expires": (dt.datetime.now() + dt.timedelta(days=POLICY_DAYS)).isoformat(timespec="seconds"),
            "user": {"username": u["username"], "display_name": u["display_name"], "role": u["role"]},
            "memberships": members if u["role"] != "admin" else [{"workspace": w, "username": username,
                                                                   "ws_role": "lead"} for w in wids],
            "grants": self.store.active_grants(username),
            "flags": flags,
            "workspaces": [w for w in self.workspaces() if w["id"] in wids],
        }
        return signing.sign(payload, self.priv.read_bytes())


def create_app(state: HubState | None = None) -> Any:
    from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, Response

    st = state or HubState()
    app = FastAPI(title="SAPBot Hub", version="0.1.0")

    def user(authorization: str = Header(default="")) -> dict[str, Any]:
        tok = authorization.removeprefix("Bearer ").strip()
        u = st.store.user_by_token(tok) if tok else None
        if not u:
            raise HTTPException(401, "invalid or missing token")
        return u

    def admin(u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        if u["role"] != "admin":
            raise HTTPException(403, "admin role required")
        return u

    def lead_of(u: dict[str, Any], ws: str) -> None:
        if not st.store.leads_workspace(u["username"], ws):
            raise HTTPException(403, "admin or engagement lead of this workspace required")

    @app.exception_handler(PermissionError)
    async def _perm(_: Request, e: PermissionError) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=403)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "time": dt.datetime.now().isoformat(timespec="seconds"), "version": "0.1.0"}

    @app.get("/api/public-key")
    def public_key() -> Response:
        return Response(st.pub.read_bytes(), media_type="application/x-pem-file")

    @app.get("/api/policy")
    def policy(u: dict = Depends(user)) -> dict[str, Any]:
        return st.policy_for(u["username"])

    # ------------------------------------------------------------ users / members (admin)
    @app.get("/api/users")
    def users(u: dict = Depends(user)) -> list[dict]:
        return st.store.list_users()

    @app.post("/api/users")
    def upsert_user(body: dict = Body(...), a: dict = Depends(admin)) -> dict:
        st.store.upsert_user(body["username"], body.get("display_name") or body["username"], body["role"],
                             bool(body.get("active", True)))
        st.store.audit(a["username"], "", "USER_UPSERT", body)
        return {"ok": True}

    @app.post("/api/users/{username}/token")
    def token(username: str, a: dict = Depends(admin)) -> dict:
        st.store.audit(a["username"], "", "TOKEN_ISSUED", {"user": username})
        return {"token": st.store.issue_token(username)}

    @app.get("/api/members")
    def members(workspace: str | None = None, u: dict = Depends(user)) -> list[dict]:
        return st.store.members(workspace)

    @app.post("/api/members")
    def set_member(body: dict = Body(...), a: dict = Depends(admin)) -> dict:
        if body.get("remove"):
            st.store.remove_member(body["workspace"], body["username"])
        else:
            st.store.set_member(body["workspace"], body["username"], body.get("ws_role", "consultant"))
        st.store.audit(a["username"], body["workspace"], "MEMBER_SET", body)
        return {"ok": True}

    # ------------------------------------------------------------ workspaces
    @app.get("/api/workspaces")
    def workspaces(u: dict = Depends(user)) -> list[dict]:
        if u["role"] == "admin":
            return st.workspaces()
        mine = {m["workspace"] for m in st.store.members() if m["username"] == u["username"]}
        return [w for w in st.workspaces() if w["id"] in mine]

    @app.post("/api/workspaces")
    def save_ws(body: dict = Body(...), a: dict = Depends(admin)) -> dict:
        st.save_workspace(body)
        st.store.audit(a["username"], body.get("id", ""), "WORKSPACE_SAVED", body)
        return {"ok": True}

    # ------------------------------------------------------------ requests / grants
    @app.get("/api/requests")
    def requests(status: str | None = "pending", u: dict = Depends(user)) -> list[dict]:
        rows = st.store.requests(status or None)
        if u["role"] == "admin":
            return rows
        return [r for r in rows if r["username"] == u["username"] or st.store.leads_workspace(u["username"],
                                                                                             r["workspace"])]

    @app.post("/api/requests")
    def add_request(body: dict = Body(...), u: dict = Depends(user)) -> dict:
        rid = st.store.add_request(u["username"], body["workspace"], body["system"], body["client"],
                                   body.get("tcodes", "*"), int(body["minutes"]), body.get("reason", ""))
        return {"id": rid}

    @app.post("/api/requests/{rid}/decide")
    def decide(rid: int, body: dict = Body(...), u: dict = Depends(user)) -> dict:
        try:
            return {"grant": st.store.decide_request(rid, bool(body["approve"]), u["username"])}
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e

    @app.get("/api/grants")
    def grants(u: dict = Depends(user)) -> list[dict]:
        rows = st.store.active_grants()
        if u["role"] == "admin":
            return rows
        return [g for g in rows if g["username"] == u["username"] or st.store.leads_workspace(u["username"],
                                                                                             g["workspace"])]

    @app.post("/api/grants")
    def add_grant(body: dict = Body(...), u: dict = Depends(user)) -> dict:
        lead_of(u, body["workspace"])
        if body["username"].lower() == u["username"] and u["role"] != "admin":
            raise HTTPException(403, "four-eyes rule: you cannot grant yourself access")
        gid = st.store.add_grant(body["username"], body["workspace"], body["system"], body["client"],
                                 body.get("tcodes", "*"), int(body["minutes"]), body.get("reason", ""), u["username"])
        return {"id": gid}

    @app.delete("/api/grants/{gid}")
    def revoke(gid: int, u: dict = Depends(user)) -> dict:
        g = next((x for x in st.store.active_grants() if x["id"] == gid), None)
        if g:
            lead_of(u, g["workspace"])
        st.store.revoke_grant(gid, u["username"])
        return {"ok": True}

    # ------------------------------------------------------------ flags
    @app.get("/api/flags")
    def flags(scope: str = "firm", u: dict = Depends(user)) -> dict:
        return st.store.flags(scope)

    @app.post("/api/flags")
    def set_flag(body: dict = Body(...), a: dict = Depends(admin)) -> dict:
        st.store.set_flag(body["name"], bool(body["value"]), body.get("scope", "firm"), a["username"])
        return {"ok": True}

    # ------------------------------------------------------------ audit
    @app.post("/api/audit")
    def upload_audit(rows: list[dict] = Body(...), u: dict = Depends(user)) -> dict:
        for r in rows[:5000]:
            st.store.audit(r.get("username") or u["username"], r.get("workspace", ""), r.get("event", ""),
                           {"laptop_ts": r.get("ts"), "uploader": u["username"],
                            **(json.loads(r["detail"]) if isinstance(r.get("detail"), str) else r.get("detail") or {})},
                           r.get("run_id", ""))
        return {"stored": min(len(rows), 5000)}

    @app.get("/api/audit")
    def audit(limit: int = 500, workspace: str | None = None, event: str | None = None,
              u: dict = Depends(user)) -> list[dict]:
        rows = st.store.audit_rows(limit, workspace, event)
        if u["role"] == "admin":
            return rows
        return [r for r in rows if r["username"] == u["username"] or
                (r["workspace"] and st.store.leads_workspace(u["username"], r["workspace"]))]

    # ------------------------------------------------------------ scripts (approved library)
    @app.get("/api/scripts")
    def scripts(u: dict = Depends(user)) -> list[dict]:
        out = []
        for a in st.store.script_approvals():
            p = st.home / "scripts" / a["path"]
            if p.exists():
                out.append({**a, "content": p.read_text(encoding="utf-8")})
        return out

    @app.post("/api/scripts")
    def publish_script(body: dict = Body(...), a: dict = Depends(admin)) -> dict:
        from ..scripts.library import sha256_text
        path = body["path"].replace("\\", "/")
        if not (path.startswith("library/") or path.startswith("custom/")) or ".." in path:
            raise HTTPException(400, "path must be library/x.vbs or custom/x.vbs")
        content = body["content"]
        d = sha256_text(content)
        (st.home / "scripts" / path).write_text(content, encoding="utf-8")
        st.store.approve_script(path, d, a["username"], bool(body.get("allow_prod")), body.get("note", ""))
        return {"path": path, "digest": d}

    # ------------------------------------------------------------ team gold runs (RAG sharing)
    def _member_or_admin(u: dict, ws: str) -> None:
        if u["role"] != "admin" and ws not in {m["workspace"] for m in st.store.members()
                                               if m["username"] == u["username"]}:
            raise HTTPException(403, "not a member of this workspace")

    @app.post("/api/trajectories/{ws}")
    def upload_run(ws: str, run: dict = Body(...), u: dict = Depends(user)) -> dict:
        _member_or_admin(u, ws)
        if run.get("quality") != "gold":
            raise HTTPException(400, "only gold runs are shared")
        if not re.fullmatch(r"[0-9A-Za-z_]{6,40}", str(run.get("run_id", ""))) or run.get("workspace") != ws:
            raise HTTPException(400, "invalid run_id / workspace")
        run["uploaded_by"] = u["username"]
        d = st.ws_dir(ws) / "gold"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{run['run_id']}.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
        return {"ok": True}

    @app.get("/api/trajectories/{ws}")
    def list_runs(ws: str, since: str = "", u: dict = Depends(user)) -> list[dict]:
        _member_or_admin(u, ws)
        out = []
        for f in sorted((st.ws_dir(ws) / "gold").glob("*.json")):
            if f.stem > since:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            if len(out) >= 500:
                break
        return out

    # ------------------------------------------------------------ updates (D6)
    @app.get("/api/version")
    def version() -> dict:
        f = st.home / "downloads" / "latest.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"version": "0.1.0", "url": ""}

    @app.get("/downloads/{name}")
    def download(name: str, u: dict = Depends(user)) -> FileResponse:
        p = (st.home / "downloads" / name).resolve()
        if p.parent != (st.home / "downloads").resolve() or not p.exists():
            raise HTTPException(404, "not found")
        return FileResponse(p)

    # ------------------------------------------------------------ Claude proxy (A16, B13)
    @app.post("/v1/anthropic/v1/messages")
    async def anthropic_proxy(request: Request, x_api_key: str = Header(default="")) -> Response:
        import httpx
        u = st.store.user_by_token(x_api_key.strip()) if x_api_key else None
        if not u:
            raise HTTPException(401, "invalid hub token")
        if not st.store.flags("firm").get("claude_api_mode", False):
            raise HTTPException(403, "claude_api_mode is disabled on the Hub")
        month = dt.date.today().strftime("%Y%m")
        used = int(st.store.kv_get(f"tokens:{u['username']}:{month}") or 0)
        cap = int(st.store.kv_get("monthly_token_cap") or 20_000_000)
        if used >= cap:
            raise HTTPException(429, f"monthly token cap reached ({cap})")
        key = _hub_secret("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise HTTPException(500, "Hub has no Claude API key configured")
        body = await request.body()
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post("https://api.anthropic.com/v1/messages", content=body,
                             headers={"x-api-key": key, "anthropic-version": request.headers.get(
                                 "anthropic-version", "2023-06-01"), "content-type": "application/json"})
        try:
            usage = r.json().get("usage", {})
            st.store.kv_set(f"tokens:{u['username']}:{month}",
                            str(used + int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))))
        except Exception:  # noqa: BLE001
            pass
        return Response(r.content, status_code=r.status_code, media_type="application/json")

    app.state.hub = st
    return app


def _hub_secret(name: str) -> str:
    """Claude key: env var (legacy) or <hub>/keys/anthropic_api_key.txt (ACL: Administrators, SYSTEM, service)."""
    f = hub_home() / "keys" / f"{name}.txt"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    try:
        import keyring
        return keyring.get_password("sapbot-hub", name) or ""
    except Exception:  # noqa: BLE001
        return ""


def _parse_sys(spec: str) -> dict[str, Any]:
    p = spec.split("/")
    if len(p) < 3:
        raise SystemExit(f"bad system '{spec}' - use SID/CLIENT/ENV[/write], e.g. SUQ/400/QA/write")
    return {"sid": p[0], "client": p[1], "env": p[2].upper(), "write": len(p) > 3 and p[3].lower() == "write"}


def main(argv: list[str] | None = None) -> None:
    """sapbot-hub init | serve | user | users | token | workspace | member | fingerprint"""
    ap = argparse.ArgumentParser(prog="sapbot-hub")
    sp = ap.add_subparsers(dest="cmd", required=True)
    i = sp.add_parser("init", help="create keys, database and the first admin")
    i.add_argument("--admin", required=True, help="Windows user name of the first admin (lowercase)")
    s = sp.add_parser("serve", help="run the Hub (normally as the SAPBotHub Windows service)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8443)
    s.add_argument("--cert", help="server certificate (PEM) from the internal CA")
    s.add_argument("--key", help="private key of the server certificate (PEM)")
    s.add_argument("--ca-bundle", help="corporate root CA for outbound calls (Claude API) through a TLS proxy")
    s.add_argument("--insecure-lan-test", action="store_true", help="allow plain HTTP (first LAN test only)")
    u = sp.add_parser("user", help="create a user (prints a token) or change role/name/status of an existing one")
    u.add_argument("username", help="must equal the consultant's Windows user name")
    u.add_argument("--role", choices=["admin", "lead", "consultant"], help="default for new users: consultant")
    u.add_argument("--display")
    u.add_argument("--workspace", action="append", help="assign to workspace (repeatable)")
    u.add_argument("--disable", action="store_true", help="deactivate the user (token stops working)")
    u.add_argument("--enable", action="store_true", help="re-activate a disabled user")
    u.add_argument("--new-token", action="store_true", help="issue a new token for an existing user")
    sp.add_parser("users", help="list users and memberships")
    t = sp.add_parser("token", help="issue a new token for an existing user (old token stops working)")
    t.add_argument("username")
    w = sp.add_parser("workspace", help="client workspaces")
    w.add_argument("action", choices=["create", "list", "show"])
    w.add_argument("id", nargs="?")
    w.add_argument("--name")
    w.add_argument("--system", action="append", help="SID/CLIENT/ENV[/write], repeatable")
    w.add_argument("--policy", default="full", choices=["full", "masked", "local-only"])
    w.add_argument("--tcodes", default="", help="extra allowed T-codes, comma list")
    m = sp.add_parser("member", help="assign a user to a workspace")
    m.add_argument("workspace")
    m.add_argument("username")
    m.add_argument("--ws-role", default="consultant", choices=["consultant", "lead"])
    m.add_argument("--remove", action="store_true")
    sp.add_parser("fingerprint", help="show the policy public key fingerprint (give it to IT / consultants)")
    f = sp.add_parser("feature", help="switch a feature for all laptops (or one workspace)")
    f.add_argument("action", choices=["list", "set"])
    f.add_argument("name", nargs="?")
    f.add_argument("value", nargs="?", choices=["on", "off"])
    f.add_argument("--workspace", default="firm")
    c = sp.add_parser("token-cap", help="monthly Claude token cap per user for the proxy")
    c.add_argument("tokens", type=int)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    st = HubState()
    out = lambda o: sys.stdout.write(json.dumps(o, indent=2, default=str) + "\n")  # noqa: E731
    fp = signing.fingerprint(st.pub.read_bytes())
    if a.cmd == "init":
        st.store.upsert_user(a.admin.lower(), a.admin, "admin")
        out({"hub_home": str(st.home), "public_key_file": str(st.pub), "public_key_fingerprint": fp,
             "admin": a.admin.lower(), "admin_token": st.store.issue_token(a.admin.lower()),
             "next": ["distribute the public key file to laptops via IT (never by e-mail alone)",
                      "keep the admin token secret - it is shown only once"]})
    elif a.cmd == "user":
        name = a.username.lower()
        if not re.fullmatch(r"[a-z0-9._\-]{2,40}", name):
            raise SystemExit("username: 2-40 chars a-z 0-9 . _ - (use the Windows user name)")
        cur = st.store.get_user(name)
        role = a.role or (cur["role"] if cur else "consultant")
        display = a.display or (cur["display_name"] if cur else name)
        active = False if a.disable else (True if (a.enable or not cur) else bool(cur["active"]))
        st.store.upsert_user(name, display, role, active=active)
        for wid in a.workspace or []:
            st.store.set_member(wid, name, "lead" if role == "lead" else "consultant")
        st.store.audit("hub-cli", "", "USER_UPSERT", {"user": name, "role": role, "active": active})
        res: dict[str, Any] = {"user": name, "role": role, "active": active, "created": cur is None}
        if active and (cur is None or a.new_token):
            res["token"] = st.store.issue_token(name)
            res["note"] = "give the token to the consultant over a secure channel; it is shown only once"
        out(res)
    elif a.cmd == "users":
        out({"users": st.store.list_users(), "members": st.store.members()})
    elif a.cmd == "token":
        if not st.store.get_user(a.username):
            raise SystemExit("unknown user")
        st.store.audit("hub-cli", "", "TOKEN_ISSUED", {"user": a.username.lower()})
        out({"user": a.username.lower(), "token": st.store.issue_token(a.username.lower())})
    elif a.cmd == "workspace":
        if a.action == "list":
            out([{"id": w["id"], "name": w["name"], "policy": w.get("data_policy"),
                  "systems": [f"{x['sid']}/{x['client']}/{x['env']}" for x in w.get("systems", [])]}
                 for w in st.workspaces()])
        elif a.action == "show":
            out(next((w for w in st.workspaces() if w["id"] == a.id), {}))
        else:
            if not a.id:
                raise SystemExit("workspace id required")
            st.save_workspace({"id": a.id, "name": a.name or a.id, "data_policy": a.policy,
                               "systems": [_parse_sys(x) for x in a.system or []],
                               "allowed_tcodes": [t.strip().upper() for t in a.tcodes.split(",") if t.strip()]})
            st.store.audit("hub-cli", a.id, "WORKSPACE_SAVED", {"id": a.id})
            out({"saved": a.id})
    elif a.cmd == "member":
        if a.remove:
            st.store.remove_member(a.workspace, a.username.lower())
        else:
            st.store.set_member(a.workspace, a.username.lower(), a.ws_role)
        out({"members": st.store.members(a.workspace)})
    elif a.cmd == "fingerprint":
        out({"public_key_file": str(st.pub), "fingerprint": fp})
    elif a.cmd == "feature":
        from ..security.governance import FEATURES
        if a.action == "set":
            if a.name not in FEATURES or a.value is None:
                raise SystemExit(f"usage: feature set <{'|'.join(FEATURES)}> on|off [--workspace id]")
            st.store.set_flag(a.name, a.value == "on", a.workspace, "hub-cli")
        out({"scope": a.workspace, "flags": st.store.flags(a.workspace),
             "note": "unset flags use the laptop defaults (cowork_mode, rag, batch_runs on; others off)"})
    elif a.cmd == "token-cap":
        st.store.kv_set("monthly_token_cap", str(a.tokens))
        out({"monthly_token_cap_per_user": a.tokens})
    elif a.cmd == "serve":
        import uvicorn
        kw: dict[str, Any] = {}
        if a.ca_bundle or os.environ.get("SAPBOT_CA_BUNDLE"):
            os.environ.setdefault("SSL_CERT_FILE", a.ca_bundle or os.environ["SAPBOT_CA_BUNDLE"])
        if a.cert and a.key:
            kw = {"ssl_certfile": a.cert, "ssl_keyfile": a.key}
        elif a.insecure_lan_test:
            log.warning("serving WITHOUT HTTPS (--insecure-lan-test) - tokens travel in clear text")
        else:
            raise SystemExit("refusing to start without HTTPS: pass --cert and --key (internal CA certificate), "
                             "or --insecure-lan-test for a first test on an isolated LAN")
        log.info("policy key fingerprint %s", fp)
        uvicorn.run(create_app(st), host=a.host, port=a.port, **kw)


if __name__ == "__main__":
    main()
