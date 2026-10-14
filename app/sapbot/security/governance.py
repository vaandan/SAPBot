"""Governance facade: who is the user, what role, which grants and feature flags apply (ADR A10-A14, E1).

Authority:
  * laptop mode (hub_sync off): the local Store (SQLite) is the authority - pilot / trust-based.
  * hub mode (hub_sync on + valid signed policy cached): users, grants, flags and memberships come ONLY from the
    signed bundle; local edits are ignored. Approvals and audit stay local and are uploaded.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from ..config import get_settings
from . import signing
from .store import Store, current_os_user

log = logging.getLogger(__name__)

FEATURES = ["cowork_mode", "claude_api_mode", "local_model", "router", "rag", "distillation", "batch_runs", "fiori",
            "hub_sync"]
FEATURE_HELP = {
    "cowork_mode": "Claude in Cowork can drive SAP through the local SAPBot tool server (MCP).",
    "claude_api_mode": "SAPBot's own agent loop calls the Claude API (CLI / Cockpit). Needs an API key.",
    "local_model": "Use the hosted small model (OpenAI-compatible endpoint) as the agent brain.",
    "router": "Automatically choose local model vs Claude per task (needs local_model).",
    "rag": "Retrieve similar past successful runs as examples (per-client store).",
    "distillation": "Export gold runs / DPO pairs as training data for the small model.",
    "batch_runs": "Run a saved script or task template for every row of an Excel file.",
    "fiori": "Fiori / WebDynpro browser bridge (v1.1).",
    "hub_sync": "Take users, grants and flags from the signed Hub policy; sync scripts and gold runs.",
}


class Governance:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()
        self.settings = get_settings()
        self._bundle: dict[str, Any] | None = None
        self._load_cached_bundle()

    # ---------------------------------------------------------------- hub bundle
    def _load_cached_bundle(self) -> None:
        raw = self.store.kv_get("hub_policy_bundle")
        if not raw:
            return
        try:
            self._bundle = self.verify_bundle(json.loads(raw))
        except Exception as e:  # noqa: BLE001
            log.warning("Cached Hub policy rejected: %s", e)
            self._bundle = None

    def verify_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        pk = self.settings.hub.public_key_file
        if not pk or not Path(pk).exists():
            raise ValueError("Hub public key not configured (hub.public_key_file)")
        payload = signing.verify(bundle, Path(pk).read_bytes())
        if dt.datetime.fromisoformat(payload["expires"]) < dt.datetime.now():
            raise ValueError("Hub policy expired - connect to the office network/VPN to refresh")
        if self.settings.hub_bind_os_user and payload["user"]["username"].lower() != current_os_user():
            raise ValueError(f"Hub policy is for '{payload['user']['username']}', but this laptop's Windows user is "
                             f"'{current_os_user()}'")
        newest = self.store.kv_get("hub_policy_issued") or ""
        if payload.get("issued", "") < newest:
            raise ValueError("an older Hub policy was presented (replay) - sync with the Hub")
        return payload

    def install_bundle(self, bundle: dict[str, Any]) -> None:
        self._bundle = self.verify_bundle(bundle)
        self.store.kv_set("hub_policy_bundle", json.dumps(bundle))
        self.store.kv_set("hub_policy_issued", self._bundle.get("issued", ""))
        self.store.kv_set("hub_enrolled", "1")   # from now on local admin rights no longer apply

    @property
    def enrolled(self) -> bool:
        """Once a laptop has received a Hub policy it stays Hub-governed (no falling back to local admin)."""
        return self.store.kv_get("hub_enrolled") == "1"

    @property
    def hub_mode(self) -> bool:
        return self._bundle is not None and (self.enrolled or self._local_flag("hub_sync"))

    @property
    def restricted(self) -> bool:
        """Enrolled but no valid policy (expired / tampered): read-only consultant, no grants, default flags."""
        return self.enrolled and self._bundle is None

    @property
    def bundle(self) -> dict[str, Any] | None:
        return self._bundle

    # ---------------------------------------------------------------- identity
    def username(self) -> str:
        if self.hub_mode:
            return str(self._bundle.get("user", {}).get("username", current_os_user()))  # type: ignore[union-attr]
        return current_os_user()

    def user(self) -> dict[str, Any]:
        if self.restricted:
            return {"username": current_os_user(), "display_name": current_os_user(), "role": "consultant",
                    "active": 1, "restricted": True}
        if self.hub_mode:
            return dict(self._bundle["user"])  # type: ignore[index]
        u = self.store.get_user(current_os_user())
        return u or self.store.bootstrap()

    def role(self) -> str:
        return str(self.user().get("role", "consultant"))

    def is_admin(self) -> bool:
        return self.role() == "admin"

    def is_lead_or_admin(self) -> bool:
        return self.role() in ("admin", "lead") or bool(self.led_workspaces())

    def led_workspaces(self) -> list[str]:
        if self.restricted:
            return []
        if self.hub_mode:
            return [m["workspace"] for m in self._bundle.get("memberships", [])  # type: ignore[union-attr]
                    if m.get("ws_role") == "lead"]
        return [m["workspace"] for m in self.store.members() if m["username"] == self.username()
                and m["ws_role"] == "lead"]

    def allowed_workspaces(self) -> list[str]:
        """'*' = all (admin)."""
        if self.is_admin():
            return ["*"]
        if self.restricted:
            return []
        if self.hub_mode:
            return [m["workspace"] for m in self._bundle.get("memberships", [])]  # type: ignore[union-attr]
        return self.store.user_workspaces(self.username())

    # ---------------------------------------------------------------- grants
    def active_grants(self) -> list[dict[str, Any]]:
        now = dt.datetime.now().isoformat(timespec="seconds")
        if self.restricted:
            return []
        if self.hub_mode:
            return [g for g in self._bundle.get("grants", []) if g["valid_to"] > now]  # type: ignore[union-attr]
        return self.store.active_grants(self.username())

    def grant_for(self, workspace: str, system: str, client: str, tcode: str) -> dict[str, Any] | None:
        for g in self.active_grants():
            if g["workspace"] != workspace or g["system"] != system.upper() or str(g["client"]) != str(client):
                continue
            codes = [c.strip().upper() for c in str(g["tcodes"]).split(",")]
            if "*" in codes or (tcode and tcode.upper() in codes):
                return g
        return None

    # ---------------------------------------------------------------- feature flags (E1)
    def _local_flag(self, name: str, workspace: str | None = None) -> bool:
        val = bool(self.settings.features_default.get(name, False))
        firm = self.store.flags("firm")
        val = firm.get(name, val)
        if workspace:
            val = self.store.flags(workspace).get(name, val)
        return val

    def feature(self, name: str, workspace: str | None = None) -> bool:
        if name == "hub_sync":
            return self.enrolled or self._local_flag("hub_sync")
        if self.restricted:
            return bool(self.settings.features_default.get(name, False))
        if self.hub_mode:
            flags = self._bundle.get("flags", {})  # type: ignore[union-attr]
            val = bool(flags.get("firm", {}).get(name, self.settings.features_default.get(name, False)))
            if workspace and name in flags.get(workspace, {}):
                val = bool(flags[workspace][name])
            return val
        return self._local_flag(name, workspace)

    def all_features(self, workspace: str | None = None) -> dict[str, bool]:
        return {f: self.feature(f, workspace) for f in FEATURES}

    def set_feature(self, name: str, value: bool, scope: str = "firm") -> None:
        if not self.is_admin():
            raise PermissionError("only an Admin can change features")
        if self.hub_mode and name != "hub_sync":
            raise PermissionError("features are managed on the Hub while hub_sync is on")
        self.store.set_flag(name, value, scope, by=self.username())
