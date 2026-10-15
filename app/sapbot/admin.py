"""Admin backend used by the Cockpit admin pages and the CLI.

LocalAdmin  - laptop mode: writes the local Store (pilot, trust-based).
HubAdmin    - hub mode: the same calls go to the SAPBot Hub (sapbot.hub.client.HubAdmin); the Hub signs the
              resulting policy and every laptop picks it up on its next sync (ADR A9-A11).
Permission checks are enforced here (and again on the Hub).
"""
from __future__ import annotations

from typing import Any, Protocol

from .security.governance import Governance


class AdminAPI(Protocol):
    def list_users(self) -> list[dict[str, Any]]: ...
    def upsert_user(self, username: str, display_name: str, role: str, active: bool = True) -> None: ...
    def issue_token(self, username: str) -> str: ...
    def members(self, workspace: str | None = None) -> list[dict[str, Any]]: ...
    def set_member(self, workspace: str, username: str, ws_role: str) -> None: ...
    def remove_member(self, workspace: str, username: str) -> None: ...
    def requests(self, status: str | None = "pending") -> list[dict[str, Any]]: ...
    def decide_request(self, rid: int, approve: bool) -> int | None: ...
    def add_request(self, workspace: str, system: str, client: str, tcodes: str, minutes: int, reason: str) -> int: ...
    def grants(self) -> list[dict[str, Any]]: ...
    def add_grant(self, username: str, workspace: str, system: str, client: str, tcodes: str, minutes: int,
                  reason: str) -> int: ...
    def revoke_grant(self, gid: int) -> None: ...
    def flags(self, scope: str = "firm") -> dict[str, bool]: ...
    def set_flag(self, name: str, value: bool, scope: str = "firm") -> None: ...
    def audit_rows(self, limit: int = 500, workspace: str | None = None, event: str | None = None) -> list[dict]: ...
    def save_workspace(self, ws: Any) -> None: ...


class LocalAdmin:
    def __init__(self, gov: Governance) -> None:
        self.gov = gov
        self.st = gov.store

    def _admin(self) -> None:
        if not self.gov.is_admin():
            raise PermissionError("Admin role required")

    def list_users(self) -> list[dict[str, Any]]:
        return self.st.list_users()

    def upsert_user(self, username: str, display_name: str, role: str, active: bool = True) -> None:
        self._admin()
        if username.lower() == self.gov.username() and role != "admin":
            raise PermissionError("you cannot remove your own Admin role")
        self.st.upsert_user(username, display_name, role, active)
        self.st.audit(self.gov.username(), "", "USER_UPSERT", {"user": username, "role": role, "active": active})

    def issue_token(self, username: str) -> str:
        self._admin()
        self.st.audit(self.gov.username(), "", "TOKEN_ISSUED", {"user": username})
        return self.st.issue_token(username)

    def members(self, workspace: str | None = None) -> list[dict[str, Any]]:
        return self.st.members(workspace)

    def set_member(self, workspace: str, username: str, ws_role: str) -> None:
        self._admin()
        self.st.set_member(workspace, username, ws_role)
        self.st.audit(self.gov.username(), workspace, "MEMBER_SET", {"user": username, "ws_role": ws_role})

    def remove_member(self, workspace: str, username: str) -> None:
        self._admin()
        self.st.remove_member(workspace, username)
        self.st.audit(self.gov.username(), workspace, "MEMBER_REMOVED", {"user": username})

    def requests(self, status: str | None = "pending") -> list[dict[str, Any]]:
        rows = self.st.requests(status)
        if self.gov.is_admin():
            return rows
        led = set(self.gov.led_workspaces())
        me = self.gov.username()
        return [r for r in rows if r["workspace"] in led or r["username"] == me]

    def decide_request(self, rid: int, approve: bool) -> int | None:
        return self.st.decide_request(rid, approve, self.gov.username())

    def add_request(self, workspace: str, system: str, client: str, tcodes: str, minutes: int, reason: str) -> int:
        return self.st.add_request(self.gov.username(), workspace, system, client, tcodes, minutes, reason)

    def grants(self) -> list[dict[str, Any]]:
        rows = self.st.active_grants()
        if self.gov.is_admin():
            return rows
        led = set(self.gov.led_workspaces())
        return [g for g in rows if g["workspace"] in led or g["username"] == self.gov.username()]

    def add_grant(self, username: str, workspace: str, system: str, client: str, tcodes: str, minutes: int,
                  reason: str) -> int:
        if not self.st.leads_workspace(self.gov.username(), workspace):
            raise PermissionError("only an Admin or this workspace's Engagement Lead can grant")
        if username.lower() == self.gov.username() and not self.gov.is_admin():
            raise PermissionError("four-eyes rule: you cannot grant yourself access")
        return self.st.add_grant(username, workspace, system, client, tcodes, minutes, reason, self.gov.username())

    def revoke_grant(self, gid: int) -> None:
        g = next((x for x in self.st.active_grants() if x["id"] == gid), None)
        if g and not self.st.leads_workspace(self.gov.username(), g["workspace"]):
            raise PermissionError("not allowed")
        self.st.revoke_grant(gid, self.gov.username())

    def flags(self, scope: str = "firm") -> dict[str, bool]:
        return self.st.flags(scope)

    def set_flag(self, name: str, value: bool, scope: str = "firm") -> None:
        self.gov.set_feature(name, value, scope)

    def save_workspace(self, ws: Any) -> None:
        self._admin()
        ws.save()

    def audit_rows(self, limit: int = 500, workspace: str | None = None, event: str | None = None) -> list[dict]:
        if not self.gov.is_admin():
            led = self.gov.led_workspaces()
            if workspace and workspace not in led:
                return []
            rows = self.st.audit_rows(limit, workspace, event)
            return [r for r in rows if r["workspace"] in led or r["username"] == self.gov.username()]
        return self.st.audit_rows(limit, workspace, event)


def admin_api(gov: Governance) -> AdminAPI:
    if gov.hub_mode:
        from .hub.client import HubAdmin, HubClient
        return HubAdmin(HubClient.from_settings(gov))
    return LocalAdmin(gov)
