"""Policy engine - the single place where safety is decided (ADR B2). Every tool call from every brain
(Cowork, Claude API, local model) is classified into an ActionIntent and judged here BEFORE it touches SAP.

Read-only mode (default, A14) means: the bot may only act inside display transactions. Typing into fields or
pressing buttons inside a change/create transaction is refused unless the system allows writes (QA/DEV) or the
user holds an active elevation grant. On PROD every write additionally needs a per-action approval.
"""
from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..workspaces import SapSystem, Workspace
from .governance import Governance

log = logging.getLogger(__name__)


class Verdict(str, enum.Enum):
    ALLOW = "allow"
    APPROVE = "needs_approval"
    DENY = "deny"


class Kind(str, enum.Enum):
    META = "meta"          # status, task begin/end, schema cache, RAG search
    OBSERVE = "observe"    # read screen / grid
    NAVIGATE = "navigate"  # ok-code /nXXXX
    INPUT = "input"        # field updates / non-write buttons / Enter / Back
    WRITE = "write"        # save / post
    SCRIPT = "script"      # run_saved_script
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    BROWSER = "browser"    # Fiori tools


CHANNEL_FEATURE = {"cowork": "cowork_mode", "claude_api": "claude_api_mode", "local": "local_model"}


@dataclass
class ActionIntent:
    tool: str
    kind: Kind
    channel: str = "cowork"                       # cowork | claude_api | local
    tcode: str = ""                               # current transaction (for INPUT/WRITE) or target (NAVIGATE)
    okcode: str = ""
    write_reasons: list[str] = field(default_factory=list)
    script: dict[str, Any] | None = None          # {path, digest, read_only, findings, approval}
    summary: str = ""


@dataclass
class PolicyDecision:
    verdict: Verdict
    reason: str = ""
    hint: str = ""
    grant: dict[str, Any] | None = None
    checks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict.value, "reason": self.reason, "hint": self.hint,
                "grant_id": (self.grant or {}).get("id"), "checks": self.checks}


class PolicyEngine:
    def __init__(self, gov: Governance, settings: Settings | None = None) -> None:
        self.gov = gov
        self.s = settings or get_settings()

    # ---------------------------------------------------------------- helpers
    def is_display(self, tcode: str) -> bool:
        return tcode.upper() in {t.upper() for t in self.s.sap.display_tcodes}

    def is_hard_stop(self, tcode: str = "", okcode: str = "") -> str:
        if tcode and tcode.upper() in {t.upper() for t in self.s.sap.hard_stop_tcodes}:
            return f"transaction {tcode.upper()} is on the hard-stop list"
        hs_codes = {o.upper().lstrip("=") for o in self.s.sap.hard_stop_okcodes}
        if okcode and okcode.strip().upper().lstrip("=") in hs_codes:
            return f"function code {okcode} is on the hard-stop list"
        return ""

    def write_reasons(self, vkey: int | None = None, click_id: str = "", okcode: str = "") -> list[str]:
        r = []
        if vkey is not None and vkey in self.s.sap.write_vkeys:
            r.append(f"VKey {vkey} (save)")
        if click_id and any(click_id.endswith(sfx) for sfx in self.s.sap.write_button_suffixes):
            r.append(f"button {click_id} (save)")
        fc = okcode.strip().upper().lstrip("=")
        if fc and fc in {o.upper().lstrip("=") for o in self.s.sap.write_okcodes}:
            r.append(f"function code {okcode}")
        return r

    def classify_act(self, ok_code: str, field_ids: list[str], click_id: str, vkey: int | None, cur_tx: str,
                     element: dict[str, Any] | None = None, popup: dict[str, Any] | None = None) -> dict[str, Any]:
        """Turn one sap_act call into {kind, tcode, okcode, reasons, error}. Conservative by design: anything
        that might change data is a WRITE (review findings 1-4, 6, 7)."""
        sap = self.s.sap
        for fid in field_ids:
            if not fid.startswith("wnd[") or "okcd" in fid.lower():
                return {"error": f"field id '{fid}' not allowed: use ids from sap_observe_screen (wnd[...]); "
                                 "commands go into ok_code"}
        if click_id and not click_id.startswith("wnd["):
            return {"error": "click_id must be an id of SAPBot's own session starting with wnd["}
        up = ok_code.strip().upper()
        cur = (cur_tx or "").upper()
        nav = ""
        fcode = ""
        if up:
            if up.startswith("/O"):
                return {"error": "/o opens extra windows - use /nTCODE in SAPBot's own session"}
            if up.startswith(("/I", "/$")) or up in ("/NEND", "/NEX"):
                return {"error": f"command {ok_code} is not allowed"}
            if up.startswith(("/N", "/*")):
                nav = up[2:].strip()
            elif up.startswith("/H"):
                fcode = up                      # debugger -> hard stop
            elif up.startswith("/"):
                fcode = up
            elif cur in ("", "SESSION_MANAGER", "S000") and re.fullmatch(r"[A-Z0-9_/]{2,20}", up):
                nav = up
            else:
                fcode = up.lstrip("=")
            if (nav or up.startswith(("/N", "/*"))) and (field_ids or click_id or vkey is not None):
                return {"error": "send navigation (/nTCODE) on its own, then act on the new screen"}
        if up and (nav or up.startswith(("/N", "/*"))):
            return {"kind": Kind.NAVIGATE, "tcode": nav, "okcode": ok_code, "reasons": []}
        reasons = self.write_reasons(vkey, click_id, fcode)
        if fcode and not reasons:
            safe = {o.upper().lstrip("=") for o in sap.safe_okcodes}
            hs = {o.upper() for o in sap.hard_stop_okcodes}
            if fcode not in safe and fcode not in hs and up not in hs:
                reasons.append(f"function code {ok_code} is not on the read-only list")
        if vkey is not None and vkey not in sap.write_vkeys and vkey not in sap.readonly_vkeys:
            reasons.append(f"VKey {vkey} is not on the read-only list")
        risky = re.compile(sap.risky_click_regex, re.I)
        safe_pop = re.compile(sap.safe_popup_regex, re.I)
        if click_id and element is not None:
            label = f"{element.get('text', '')} {element.get('tooltip', '')}".strip()
            on_popup = not click_id.startswith("wnd[0]")
            if risky.search(label):
                reasons.append(f"click on '{label[:60]}'")
            elif on_popup and not safe_pop.search(label):
                reasons.append(f"click on popup button '{label[:60] or click_id}'")
        if popup and vkey == 0:
            ptxt = " ".join([popup.get("title", "")] + list(popup.get("texts", [])))
            if risky.search(ptxt):
                reasons.append(f"Enter on popup '{popup.get('title', '')}' that asks to save/post")
        kind = Kind.WRITE if reasons else Kind.INPUT
        return {"kind": kind, "tcode": cur, "okcode": ok_code if fcode else "", "reasons": reasons}

    def _write_permission(self, ws: Workspace, sysm: SapSystem, tcode: str) -> tuple[bool, dict | None, str]:
        grant = self.gov.grant_for(ws.id, sysm.sid, sysm.client, tcode)
        if sysm.is_prod:
            return (grant is not None, grant,
                    "" if grant else "production is read-only; ask an Admin/Lead for a time-boxed elevation grant")
        if sysm.write or grant:
            return True, grant, ""
        return False, None, (f"{sysm.sid}/{sysm.client} is read-only in workspace '{ws.id}'; enable write for this "
                             "QA/DEV system in the workspace or request elevation")

    # ---------------------------------------------------------------- main
    def decide(self, intent: ActionIntent, ws: Workspace | None, sysm: SapSystem | None,
               live_system: tuple[str, str] | None = None) -> PolicyDecision:
        checks: list[str] = []
        ws_id = ws.id if ws else None

        # 0. channel + feature gates (E1)
        feat = CHANNEL_FEATURE.get(intent.channel)
        if feat and not self.gov.feature(feat, ws_id):
            return PolicyDecision(Verdict.DENY, f"feature '{feat}' is disabled", "an Admin can enable it in Features")
        if intent.kind == Kind.BROWSER and not self.gov.feature("fiori", ws_id):
            return PolicyDecision(Verdict.DENY, "Fiori bridge is not enabled (planned v1.1)")
        checks.append("feature")
        if ws is not None and ws.data_policy == "local-only" and intent.channel not in ("local", "cockpit"):
            return PolicyDecision(Verdict.DENY, f"workspace '{ws.id}' is local-only: SAP data and examples may not "
                                  "be sent to cloud models", "use the on-prem small model for this client")
        if intent.kind == Kind.META:
            return PolicyDecision(Verdict.ALLOW, "meta", checks=checks)

        # 1. workspace
        if ws is None:
            return PolicyDecision(Verdict.DENY, "no active client workspace",
                                  "select a workspace (sapbot_status / Cockpit) before working in SAP")
        allowed = self.gov.allowed_workspaces()
        if "*" not in allowed and ws.id not in allowed:
            return PolicyDecision(Verdict.DENY, f"you are not assigned to workspace '{ws.id}'")
        checks.append("workspace")

        if intent.kind in (Kind.FILE_READ, Kind.FILE_WRITE):
            return PolicyDecision(Verdict.ALLOW, "file access inside allowed folders", checks=checks)

        # 2. live SAP system must belong to the workspace (wrong-window guard, A13)
        if sysm is None:
            live = f"{live_system[0]}/{live_system[1]}" if live_system else "unknown"
            return PolicyDecision(Verdict.DENY, f"SAP system {live} is not part of workspace '{ws.id}'",
                                  "switch the workspace or add the system to it")
        checks.append("system")

        if intent.kind == Kind.OBSERVE:
            return PolicyDecision(Verdict.ALLOW, "read-only observation", checks=checks)

        # 3. hard stops - always a human decision; on PROD a grant is required as well
        hs = self.is_hard_stop(intent.tcode if intent.kind == Kind.NAVIGATE else "", intent.okcode)
        if intent.kind in (Kind.INPUT, Kind.WRITE) and not hs:
            hs = self.is_hard_stop(intent.tcode)
        if hs:
            if sysm.is_prod or intent.kind in (Kind.INPUT, Kind.WRITE):
                ok, grant, why = self._write_permission(ws, sysm, intent.tcode)
                if not ok:
                    return PolicyDecision(Verdict.DENY, hs + "; " + why, checks=checks)
                return PolicyDecision(Verdict.APPROVE, hs, grant=grant, checks=checks)
            return PolicyDecision(Verdict.APPROVE, hs, checks=checks)

        # 4. navigation
        if intent.kind == Kind.NAVIGATE:
            if not intent.tcode or self.is_display(intent.tcode):
                return PolicyDecision(Verdict.ALLOW, "display transaction", checks=checks)
            if not ws.tcode_allowed(intent.tcode, self.s.sap.display_tcodes):
                return PolicyDecision(Verdict.APPROVE, f"transaction {intent.tcode} is not on the workspace "
                                      "whitelist", "an Admin can add it to the workspace's allowed T-codes",
                                      checks=checks)
            return PolicyDecision(Verdict.ALLOW, "whitelisted transaction (no save yet)", checks=checks)

        # 5. scripts (A2/A3)
        if intent.kind == Kind.SCRIPT:
            return self._decide_script(intent, ws, sysm, checks)

        # 6. input / write inside a transaction
        if intent.kind == Kind.INPUT and (not intent.tcode or self.is_display(intent.tcode)):
            return PolicyDecision(Verdict.ALLOW, "input inside a display transaction", checks=checks)
        ok, grant, why = self._write_permission(ws, sysm, intent.tcode)
        if not ok:
            return PolicyDecision(Verdict.DENY, why, "use sapbot_status to see grants; request elevation",
                                  checks=checks)
        if intent.kind == Kind.WRITE and sysm.is_prod:
            return PolicyDecision(Verdict.APPROVE, "production save/write-risk action: per-action approval required "
                                  f"(A14) - {'; '.join(intent.write_reasons)}", grant=grant, checks=checks)
        return PolicyDecision(Verdict.ALLOW, "write permitted" if intent.kind == Kind.WRITE else
                              "input permitted (write mode)", grant=grant, checks=checks)

    def _decide_script(self, intent: ActionIntent, ws: Workspace, sysm: SapSystem,
                       checks: list[str]) -> PolicyDecision:
        sc = intent.script or {}
        appr = sc.get("approval")
        read_only = bool(sc.get("read_only"))
        if str(sc.get("path", "")).startswith("drafts/") and not appr:
            if not read_only:
                ok, _, why = self._write_permission(ws, sysm, (sc.get("tcodes") or [""])[0])
                if not ok:
                    return PolicyDecision(Verdict.DENY, "unapproved draft may post data; " + why, checks=checks)
            return PolicyDecision(Verdict.APPROVE, "unapproved draft script: a human must check it before it runs",
                                  checks=checks)
        if read_only:
            if not sysm.is_prod or appr:
                return PolicyDecision(Verdict.ALLOW, "read-only script" + (" (approved)" if appr else ""),
                                      checks=checks)
            return PolicyDecision(Verdict.APPROVE, "read-only script on production without content approval",
                                  checks=checks)
        tcodes = sc.get("tcodes") or [""]
        grant = None
        for t in tcodes:
            ok, grant, why = self._write_permission(ws, sysm, t)
            if not ok:
                return PolicyDecision(Verdict.DENY, f"script may post data ({'; '.join(sc.get('findings', []))}). "
                                      + why, checks=checks)
        if sysm.is_prod:
            if not (appr and appr.get("allow_prod")):
                return PolicyDecision(Verdict.DENY, "posting script is not approved for production",
                                      "an Admin approves scripts for production in Script Approval", checks=checks)
            return PolicyDecision(Verdict.APPROVE, "production posting script: per-run approval required",
                                  grant=grant, checks=checks)
        if appr:
            return PolicyDecision(Verdict.ALLOW, "approved script content", grant=grant, checks=checks)
        return PolicyDecision(Verdict.APPROVE, "script content not yet approved and may post data: "
                              + "; ".join(sc.get("findings", [])), grant=grant, checks=checks)
