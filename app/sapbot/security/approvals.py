"""Human-in-the-loop approvals (FSD 3.4.4 / 3.7.5, ADR E3).

Modes (settings.approvals.mode):
  dialog  - native Windows popup raised by the tool server itself (Cowork mode; no other app needed).
  db      - pending row in the local store; the Cockpit (Streamlit) shows Approve / Edit values / Abort.
  deny    - never approve (headless / CI).
A callback can be injected (tests, Cockpit in-process approvals). Every request + decision is recorded.
"""
from __future__ import annotations

import ctypes
import json
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .store import Store

log = logging.getLogger(__name__)

IDYES, IDTIMEOUT = 6, 32000
MB_YESNO, MB_ICONWARNING, MB_TOPMOST, MB_SETFOREGROUND, MB_SYSTEMMODAL = 0x4, 0x30, 0x40000, 0x10000, 0x1000


@dataclass
class Decision:
    approved: bool
    by: str = ""
    approval_id: int = 0
    edited_values: dict[str, Any] = field(default_factory=dict)
    note: str = ""


ApprovalCallback = Callable[[int, str, dict[str, Any]], Decision]


class ApprovalBroker:
    _dialog_lock = threading.Lock()

    def __init__(self, store: Store, mode: str = "dialog", timeout_seconds: int = 300,
                 callback: ApprovalCallback | None = None) -> None:
        self.store = store
        self.mode = mode
        self.timeout = timeout_seconds
        self.callback = callback

    def request(self, kind: str, summary: str, details: dict[str, Any], *, username: str, workspace: str,
                system: str, client: str, run_id: str = "") -> Decision:
        aid = self.store.add_approval(kind, username, workspace, system, client, summary, details, run_id)
        self.store.audit(username, workspace, "APPROVAL_REQUESTED", {"id": aid, "kind": kind, "summary": summary},
                         run_id)
        try:
            if self.callback is not None:
                dec = self.callback(aid, summary, details)
            elif self.mode == "dialog" and sys.platform == "win32":
                dec = self._dialog(aid, summary, details, system, client)
            elif self.mode in ("db", "dialog"):
                dec = self._poll_db(aid)
            else:
                dec = Decision(False, "policy", aid, note="approvals disabled (mode=deny)")
        except Exception as e:  # noqa: BLE001
            log.exception("approval failed")
            dec = Decision(False, "error", aid, note=str(e))
        dec.approval_id = aid
        if self.store.approval(aid)["status"] == "pending":  # type: ignore[index]
            self.store.decide_approval(aid, dec.approved, dec.by or username)
        self.store.audit(dec.by or username, workspace, "APPROVAL_" + ("GRANTED" if dec.approved else "REFUSED"),
                         {"id": aid, "summary": summary, "note": dec.note}, run_id)
        return dec

    # ----------------------------------------------------------------
    def _dialog(self, aid: int, summary: str, details: dict[str, Any], system: str, client: str) -> Decision:
        text = (f"SAPBot asks for approval  (#{aid})\n\nSystem: {system}/{client}\n\n{summary}\n\n"
                f"Details:\n{json.dumps(details, indent=1, default=str)[:1500]}\n\nApprove this action?")
        title = f"SAPBot approval - {system}/{client}"
        flags = MB_YESNO | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND | MB_SYSTEMMODAL
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        with self._dialog_lock:
            fn = getattr(user32, "MessageBoxTimeoutW", None)
            if fn is not None:
                res = fn(None, text, title, flags, 0, int(self.timeout * 1000))
            else:
                res = user32.MessageBoxW(None, text, title, flags)
        if res == IDTIMEOUT:
            return Decision(False, "timeout", aid, note=f"no answer within {self.timeout}s")
        return Decision(res == IDYES, "dialog-user", aid)

    def _poll_db(self, aid: int) -> Decision:
        end = time.time() + self.timeout
        while time.time() < end:
            row = self.store.approval(aid)
            if row and row["status"] != "pending":
                details = json.loads(row.get("details") or "{}")
                return Decision(row["status"] == "approved", row.get("decided_by") or "", aid,
                                edited_values=details.get("edited_values", {}))
            time.sleep(1.0)
        self.store.decide_approval(aid, False, "timeout")
        return Decision(False, "timeout", aid, note=f"no answer within {self.timeout}s")
