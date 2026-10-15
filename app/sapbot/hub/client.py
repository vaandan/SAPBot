"""Laptop side of the Hub (ADR A9-A11, B11): sync + admin calls over HTTPS.

sync():
  1. GET /api/policy -> verify Ed25519 signature -> install (users, grants, flags; enrols the laptop)
  2. workspace definitions -> local workspaces/<id>/workspace.yaml (only assigned clients)
  3. approved script library -> local scripts + content-hash approvals
  4. upload queued audit rows
  5. upload own gold runs; download team gold runs per workspace -> local RAG (examples shared per client only)
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import yaml

from .. import paths
from .. import workspaces as wsmod
from ..config import get_settings
from ..memory.trajectory import Run, Step
from ..security.governance import Governance

log = logging.getLogger(__name__)


class HubClient:
    def __init__(self, url: str, token: str, verify: Any = True) -> None:
        if not url:
            raise RuntimeError("Hub URL not configured (Admin > Hub)")
        self.url = url.rstrip("/")
        self.token = token
        self.http = httpx.Client(base_url=self.url, headers={"Authorization": f"Bearer {token}"}, verify=verify,
                                 timeout=30)

    @classmethod
    def from_settings(cls, gov: Governance | None = None) -> "HubClient":
        from ..agent.llm import get_secret
        s = get_settings()
        tok = get_secret("hub_token", "SAPBOT_HUB_TOKEN")
        if not tok:
            raise RuntimeError("no Hub token - run 'sapbot secret set hub_token'")
        from ..config import ca_bundle
        return cls(s.hub.url, tok, ca_bundle() or True)

    def _get(self, path: str, **params: Any) -> Any:
        r = self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: Any) -> Any:
        r = self.http.post(path, json=body)
        r.raise_for_status()
        return r.json()

    def anthropic_proxy_url(self) -> str:
        return f"{self.url}/v1/anthropic"

    # ---------------------------------------------------------------- sync
    def sync(self, ex: Any) -> dict[str, Any]:
        gov: Governance = ex.gov
        rep: dict[str, Any] = {}
        bundle = self._get("/api/policy")
        gov.install_bundle(bundle)
        rep["policy_expires"] = bundle["payload"]["expires"]
        rep["user"] = bundle["payload"]["user"]
        # workspaces
        n = 0
        for w in bundle["payload"].get("workspaces", []):
            ws = wsmod.Workspace.model_validate(w)
            ws.save()
            n += 1
        rep["workspaces"] = n
        # scripts
        from ..scripts.library import sha256_text
        got = 0
        remote = self._get("/api/scripts")
        keep = {(sc["path"], sc["digest"]) for sc in remote}
        for a in gov.store.script_approvals():          # approvals revoked on the Hub are revoked here too
            if str(a.get("approved_by", "")).startswith("hub:") and (a["path"], a["digest"]) not in keep:
                gov.store.revoke_script(a["path"], a["digest"], "hub-sync")
        for sc in remote:
            if sha256_text(sc["content"]) != sc["digest"]:
                continue
            p = paths.scripts_dir() / sc["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(sc["content"], encoding="utf-8")
            gov.store.approve_script(sc["path"], sc["digest"], f"hub:{sc.get('approved_by', '')}",
                                     bool(sc.get("allow_prod")))
            got += 1
        rep["scripts"] = got
        # audit upload
        rows = list(reversed(gov.store.audit_rows(2000, only_unuploaded=True)))
        if rows:
            self._post("/api/audit", rows)
            gov.store.mark_uploaded([r["id"] for r in rows])
        rep["audit_uploaded"] = len(rows)
        # gold runs both ways
        up = down = 0
        p = bundle["payload"]
        allowed = {w["id"] for w in p.get("workspaces", [])} | {m["workspace"] for m in p.get("memberships", [])}
        for ws in wsmod.list_all():
            if ws.id not in allowed:          # e.g. created in laptop mode, not assigned on the Hub
                rep.setdefault("skipped_workspaces", []).append(ws.id)
                continue
            res = ex.resources(ws)
            sent = set(json.loads(gov.store.kv_get(f"hub_sent:{ws.id}") or "[]"))
            for r in res.logger.list_runs(5000):
                if r.get("quality") == "gold" and r["run_id"] not in sent:
                    from dataclasses import asdict
                    self._post(f"/api/trajectories/{ws.id}", asdict(res.logger.load(r["run_id"])))
                    sent.add(r["run_id"])
                    up += 1
            gov.store.kv_set(f"hub_sent:{ws.id}", json.dumps(sorted(sent)))
            seen = set(json.loads(gov.store.kv_get(f"hub_seen:{ws.id}") or "[]"))
            for d in self._get(f"/api/trajectories/{ws.id}"):
                rid = d.get("run_id")
                if rid in sent or rid in seen:
                    continue
                if res.rag is not None:
                    d.pop("uploaded_by", None)
                    d["steps"] = [Step(**s) for s in d.get("steps", [])]
                    res.rag.index_trajectory(Run(**d))
                    down += 1
                seen.add(rid)
            gov.store.kv_set(f"hub_seen:{ws.id}", json.dumps(sorted(seen)))
        rep["gold_uploaded"], rep["team_examples_indexed"] = up, down
        gov.store.kv_set("hub_last_sync", json.dumps(rep, default=str))
        return rep


class HubAdmin:
    """AdminAPI over the Hub (same method names as admin.LocalAdmin)."""

    def __init__(self, c: HubClient) -> None:
        self.c = c

    def list_users(self) -> list[dict[str, Any]]:
        return self.c._get("/api/users")

    def upsert_user(self, username: str, display_name: str, role: str, active: bool = True) -> None:
        self.c._post("/api/users", {"username": username, "display_name": display_name, "role": role,
                                    "active": active})

    def issue_token(self, username: str) -> str:
        return self.c._post(f"/api/users/{username}/token", {})["token"]

    def members(self, workspace: str | None = None) -> list[dict[str, Any]]:
        return self.c._get("/api/members", workspace=workspace)

    def set_member(self, workspace: str, username: str, ws_role: str) -> None:
        self.c._post("/api/members", {"workspace": workspace, "username": username, "ws_role": ws_role})

    def remove_member(self, workspace: str, username: str) -> None:
        self.c._post("/api/members", {"workspace": workspace, "username": username, "remove": True})

    def requests(self, status: str | None = "pending") -> list[dict[str, Any]]:
        return self.c._get("/api/requests", status=status or "")

    def decide_request(self, rid: int, approve: bool) -> int | None:
        return self.c._post(f"/api/requests/{rid}/decide", {"approve": approve}).get("grant")

    def add_request(self, workspace: str, system: str, client: str, tcodes: str, minutes: int, reason: str) -> int:
        return self.c._post("/api/requests", {"workspace": workspace, "system": system, "client": client,
                                              "tcodes": tcodes, "minutes": minutes, "reason": reason})["id"]

    def grants(self) -> list[dict[str, Any]]:
        return self.c._get("/api/grants")

    def add_grant(self, username: str, workspace: str, system: str, client: str, tcodes: str, minutes: int,
                  reason: str) -> int:
        return self.c._post("/api/grants", {"username": username, "workspace": workspace, "system": system,
                                            "client": client, "tcodes": tcodes, "minutes": minutes,
                                            "reason": reason})["id"]

    def revoke_grant(self, gid: int) -> None:
        r = self.c.http.delete(f"/api/grants/{gid}")
        r.raise_for_status()

    def flags(self, scope: str = "firm") -> dict[str, bool]:
        return self.c._get("/api/flags", scope=scope)

    def set_flag(self, name: str, value: bool, scope: str = "firm") -> None:
        self.c._post("/api/flags", {"name": name, "value": value, "scope": scope})

    def audit_rows(self, limit: int = 500, workspace: str | None = None, event: str | None = None) -> list[dict]:
        return self.c._get("/api/audit", limit=limit, workspace=workspace, event=event)

    def publish_script(self, path: str, content: str, allow_prod: bool = False, note: str = "") -> dict[str, Any]:
        return self.c._post("/api/scripts", {"path": path, "content": content, "allow_prod": allow_prod,
                                             "note": note})

    def save_workspace(self, ws: wsmod.Workspace) -> None:
        self.c._post("/api/workspaces", yaml.safe_load(yaml.safe_dump(ws.model_dump())))


# ---------------------------------------------------------------------------------------------------- enrolment
def enroll(ex: Any, hub_url: str, pubkey_file: str, token: str, ca_bundle_file: str = "",
           expected_fingerprint: str = "", http: Any = None) -> dict[str, Any]:
    """One-step laptop enrolment (DEPLOYMENT.md 6):
    1. verify + install the Hub public key (fingerprint compared if given)  2. save hub.url / CA
    3. token -> Windows Credential Manager  4. first sync (installs the signed policy; laptop becomes Hub-governed)
    """
    import shutil
    from pathlib import Path as _P

    from ..agent.llm import set_secret
    from ..config import save_settings_patch
    from ..security.signing import fingerprint
    src = _P(pubkey_file)
    if not src.exists():
        raise FileNotFoundError(f"public key file not found: {src}")
    fp = fingerprint(src.read_bytes())
    if expected_fingerprint and expected_fingerprint.replace(":", "").upper() != fp.replace(":", ""):
        raise ValueError(f"public key fingerprint mismatch: file has {fp} - do not enrol, ask IT for the right file")
    if not hub_url.lower().startswith("https://") and "localhost" not in hub_url and "127.0.0.1" not in hub_url:
        raise ValueError("Hub URL must use https://")
    dst = paths.config_dir() / "hub_public.pem"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dst.resolve() and paths.machine_dir() not in src.resolve().parents:
        shutil.copyfile(src, dst)
    elif paths.machine_dir() in src.resolve().parents:
        dst = src                       # IT-managed: keep using the read-only machine copy
    patch: dict[str, Any] = {"hub": {"url": hub_url.rstrip("/"), "public_key_file": str(dst)}}
    if ca_bundle_file:
        ca_dst = paths.config_dir() / "corporate_ca.pem"
        shutil.copyfile(ca_bundle_file, ca_dst)
        patch["network"] = {"ca_bundle": str(ca_dst)}
    s = save_settings_patch(patch)
    ex.gov.settings = s
    if http is None:
        set_secret("hub_token", token)
    client = HubClient(hub_url, token, (str(paths.config_dir() / "corporate_ca.pem") if ca_bundle_file else
                                        (s.network.ca_bundle or True)))
    if http is not None:
        client.http = http
    rep = client.sync(ex)
    ex.gov.store.audit(ex.gov.username(), "", "HUB_ENROLLED", {"hub": hub_url, "fingerprint": fp})
    return {"enrolled": True, "hub": hub_url, "public_key_fingerprint": fp, **rep}


_auto_sync_started = False


def start_auto_sync(ex: Any) -> None:
    """Background sync: once at start, then every hub.sync_interval_minutes. Failures (offline/VPN down) are
    logged; SAPBot keeps working on the cached signed policy until it expires (7 days)."""
    global _auto_sync_started
    if _auto_sync_started:
        return
    _auto_sync_started = True
    import threading
    import time as _t

    def loop() -> None:
        while True:
            try:
                rep = HubClient.from_settings(ex.gov).sync(ex)
                log.info("hub sync ok: %s", {k: rep.get(k) for k in ("policy_expires", "scripts", "audit_uploaded")})
            except Exception as e:  # noqa: BLE001
                log.warning("hub sync failed (working offline on cached policy): %s", e)
                ex.gov.store.kv_set("hub_last_error", f"{_t.strftime('%d.%m.%Y %H:%M')} {e}"[:500])
            mins = get_settings().hub.sync_interval_minutes
            if mins <= 0:
                return
            _t.sleep(mins * 60)
    threading.Thread(target=loop, name="sapbot-hub-sync", daemon=True).start()
