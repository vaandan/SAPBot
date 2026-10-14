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
        return cls(s.hub.url, tok, s.hub.ca_bundle or True)

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
        for ws in wsmod.list_all():
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

    def save_workspace(self, ws: wsmod.Workspace) -> None:
        self.c._post("/api/workspaces", yaml.safe_load(yaml.safe_dump(ws.model_dump())))
