"""SAP GUI Automation Engine (FSD 3.1) - native COM via SAP GUI Scripting.

  * session hooking + own dedicated session (ADR B3)
  * sap_observe_screen  -> compact structured snapshot (ADR A5: text, never screenshots)
  * sap_act             -> ok-code, field updates, click, virtual key; waits while busy
  * sap_read_grid       -> ALV GuiGridView / GuiTableControl rows (GetCellValue < threshold, ALV local-file export
                           above it - proven in sap-gate; replaces FSD's clipboard idea which freezes COM)
  * warning auto-acknowledge ONLY for allow-listed message keys (project safety rule)
  * rollback helper: '/n' to leave the transaction and release locks
All COM work runs on the ComWorker thread.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from ..config import get_settings
from ..types import SessionInfo, StatusBar
from . import locale
from .com import ElementNotFound, SapComError, child, count, prop, real_application
from .worker import ComWorker

log = logging.getLogger(__name__)

INPUT_TYPES = {"GuiTextField", "GuiCTextField", "GuiPasswordField", "GuiComboBox", "GuiCheckBox", "GuiRadioButton",
               "GuiOkCodeField", "GuiTextedit"}
MAX_ELEMENTS = 350
MAX_DEPTH = 14


def rel_id(full_id: str) -> str:
    """'/app/con[0]/ses[1]/wnd[0]/usr/ctxtX' -> 'wnd[0]/usr/ctxtX'."""
    i = full_id.find("/wnd[")
    return full_id[i + 1:] if i >= 0 else full_id


class SAPGuiBridge:
    def __init__(self, app_factory: Callable[[], Any] | None = None, busy_timeout: float = 120.0) -> None:
        self.worker = ComWorker(app_factory or real_application)
        self.busy_timeout = busy_timeout
        self.s = get_settings()

    # ================================================================ sessions
    def list_sessions(self) -> list[SessionInfo]:
        return self.worker.call(self._list_sessions)

    def _iter_sessions(self) -> list[tuple[Any, Any]]:
        app = self.worker.app
        out = []
        for i in range(count(app.Children)):
            con = child(app.Children, i)
            for j in range(count(con.Children)):
                out.append((con, child(con.Children, j)))
        return out

    def _info(self, ses: Any) -> SessionInfo:
        info = ses.Info
        return SessionInfo(session_id=str(ses.Id), system=str(prop(info, "SystemName")).upper(),
                           client=str(prop(info, "Client")).zfill(3), user=str(prop(info, "User")),
                           language=str(prop(info, "Language")), transaction=str(prop(info, "Transaction")),
                           program=str(prop(info, "Program")), screen=str(prop(info, "ScreenNumber")),
                           busy=bool(prop(ses, "Busy", False)))

    def _list_sessions(self) -> list[SessionInfo]:
        out = []
        for _, s in self._iter_sessions():
            try:
                out.append(self._info(s))
            except Exception as e:  # noqa: BLE001 - busy/modal session: skip instead of failing the listing
                log.info("session %s not readable right now: %s", prop(s, "Id"), e)
        return out

    def _session(self, session_id: str) -> Any:
        for _, s in self._iter_sessions():
            if str(s.Id) == session_id:
                return s
        raise SapComError(f"SAP session {session_id} no longer exists (closed or SAP Logon restarted)")

    def session_info(self, session_id: str) -> SessionInfo:
        return self.worker.call(lambda: self._info(self._session(session_id)))

    def open_own_session(self, system: str, client: str) -> SessionInfo:
        """Create a NEW session on an existing logon of system/client (ADR B3). Never logs on, never handles
        credentials. Raises SapComError if no logon exists or the session limit is reached."""
        return self.worker.call(self._open_own_session, system.upper(), str(client).zfill(3))

    def _open_own_session(self, system: str, client: str) -> SessionInfo:
        match = []
        for c, s in self._iter_sessions():
            try:
                if str(prop(s.Info, "SystemName")).upper() == system and \
                        str(prop(s.Info, "Client")).zfill(3) == client and not prop(s, "Busy", False):
                    match.append((c, s))
            except Exception:  # noqa: BLE001
                continue
        if not match:
            raise SapComError(f"No SAP session for {system}/{client}. Log on to {system} client {client} in SAP "
                              "Logon first (SAPBot never logs on or handles passwords).")
        con, base = match[0]
        before = {str(child(con.Children, j).Id) for j in range(count(con.Children))}
        try:
            base.CreateSession()
        except Exception as e:  # noqa: BLE001
            raise SapComError(f"Could not open a new session on {system}/{client} (session limit?): {e}") from e
        end = time.time() + 20
        while time.time() < end:
            for j in range(count(con.Children)):
                s = child(con.Children, j)
                if str(s.Id) not in before:
                    self._wait(s)
                    return self._info(s)
            time.sleep(0.3)
        raise SapComError("New SAP session did not appear within 20 s")

    def find_session(self, system: str, client: str) -> SessionInfo | None:
        for si in self.list_sessions():
            if si.system == system.upper() and si.client == str(client).zfill(3):
                return si
        return None

    # ================================================================ helpers (worker thread)
    def _wait(self, ses: Any) -> None:
        end = time.time() + self.busy_timeout
        while prop(ses, "Busy", False):
            if time.time() > end:
                raise SapComError(f"SAP session busy for more than {self.busy_timeout:.0f} s")
            time.sleep(0.1)
        time.sleep(0.05)

    def _find(self, ses: Any, rid: str) -> Any | None:
        try:
            return ses.FindById(rid)
        except Exception:  # noqa: BLE001
            return None

    def _status(self, ses: Any) -> StatusBar:
        sb = self._find(ses, "wnd[0]/sbar")
        if sb is None:
            return StatusBar()
        raw_params = prop(sb, "MessageParameter", None)
        params: list[str] = []
        if isinstance(raw_params, str):
            params = [p for p in raw_params.split("|") if p]
        else:
            for i in range(1, 9):
                try:
                    p = sb.MessageParameter(i)
                    if p:
                        params.append(str(p).strip())
                except Exception:  # noqa: BLE001
                    break
        return StatusBar(type=str(prop(sb, "MessageType")), text=str(prop(sb, "Text")),
                         msg_id=str(prop(sb, "MessageId")).strip(), msg_number=str(prop(sb, "MessageNumber")).strip(),
                         msg_params=[str(p) for p in list(params)])

    def _active_window(self, ses: Any) -> Any:
        w = prop(ses, "ActiveWindow", None)
        return w if w is not None else ses.FindById("wnd[0]")

    # ================================================================ observe (FSD 3.1.2)
    def observe(self, session_id: str, include_values: bool = True) -> dict[str, Any]:
        return self.worker.call(self._observe, session_id, include_values)

    def _observe(self, session_id: str, include_values: bool = True) -> dict[str, Any]:
        ses = self._session(session_id)
        self._wait(ses)
        info = self._info(ses)
        win = self._active_window(ses)
        win_id = rel_id(str(win.Id))
        snap: dict[str, Any] = {
            "session_id": info.session_id, "system": info.system, "client": info.client, "user": info.user,
            "language": info.language, "transaction": info.transaction, "program": info.program,
            "screen": info.screen, "program_dynpro": f"{info.program}_{info.screen}",
            "title": str(prop(self._find(ses, "wnd[0]"), "Text")), "active_window": win_id,
            "status_bar": self._status(ses).model_dump(),
        }
        snap["toolbar"] = self._toolbar(ses, "wnd[0]/tbar[1]")
        snap.update(self._walk_usr(ses, "wnd[0]/usr", include_values))
        if win_id != "wnd[0]":
            popup = {"id": win_id, "title": str(prop(win, "Text"))}
            popup.update(self._walk_usr(ses, f"{win_id}/usr", include_values, labels_as_text=True))
            popup["toolbar"] = self._toolbar(ses, f"{win_id}/tbar[0]")
            snap["popup"] = popup
        return snap

    def _toolbar(self, ses: Any, tid: str) -> list[dict[str, str]]:
        tb = self._find(ses, tid)
        out = []
        if tb is None:
            return out
        for k in range(count(tb.Children)):
            b = child(tb.Children, k)
            if prop(b, "Type") == "GuiButton" and prop(b, "Changeable", True):
                out.append({"id": rel_id(str(b.Id)), "tooltip": str(prop(b, "Tooltip") or prop(b, "Text"))})
        return out

    def _walk_usr(self, ses: Any, root_id: str, include_values: bool, labels_as_text: bool = False) -> dict[str, Any]:
        root = self._find(ses, root_id)
        res: dict[str, Any] = {"fields": [], "buttons": [], "tabs": [], "grids": [], "tables": []}
        if labels_as_text:
            res["texts"] = []
        if root is None:
            return res
        labels: dict[str, str] = {}
        n = [0]

        def visit(o: Any, depth: int) -> None:
            if n[0] >= MAX_ELEMENTS or depth > MAX_DEPTH:
                return
            t = str(prop(o, "Type"))
            oid = rel_id(str(prop(o, "Id")))
            if t == "GuiLabel":
                txt = str(prop(o, "Text")).strip()
                if txt:
                    labels[oid] = txt
                    if labels_as_text:
                        res["texts"].append(txt)
            elif t in INPUT_TYPES:
                n[0] += 1
                fld: dict[str, Any] = {"id": oid, "type": t.replace("Gui", ""), "name": str(prop(o, "Name")),
                                       "changeable": bool(prop(o, "Changeable", False))}
                lab = prop(o, "LeftLabel", None)
                if lab is not None:
                    fld["label"] = str(prop(lab, "Text")).strip()
                tip = str(prop(o, "Tooltip")).strip()
                if tip and "label" not in fld:
                    fld["label"] = tip
                if include_values:
                    if t == "GuiPasswordField":
                        fld["value"] = "<SECRET>"
                    elif t in ("GuiCheckBox", "GuiRadioButton"):
                        fld["value"] = bool(prop(o, "Selected", False))
                        fld["label"] = fld.get("label") or str(prop(o, "Text")).strip()
                    elif t == "GuiComboBox":
                        fld["value"] = str(prop(o, "Key"))
                        fld["text"] = str(prop(o, "Value"))
                    else:
                        fld["value"] = str(prop(o, "Text"))
                if prop(o, "Required", False):
                    fld["required"] = True
                res["fields"].append(fld)
            elif t == "GuiButton":
                n[0] += 1
                res["buttons"].append({"id": oid, "text": str(prop(o, "Text")).strip(),
                                       "tooltip": str(prop(o, "Tooltip")).strip()})
            elif t == "GuiTab":
                n[0] += 1
                res["tabs"].append({"id": oid, "text": str(prop(o, "Text")).strip()})
            elif t == "GuiTableControl":
                n[0] += 1
                cols = []
                try:
                    for c in range(count(o.Columns)):
                        cols.append(str(prop(child(o.Columns, c), "Title")))
                except Exception:  # noqa: BLE001
                    pass
                res["tables"].append({"id": oid, "visible_rows": int(prop(o, "VisibleRowCount", 0)),
                                      "row_count": int(prop(o, "RowCount", 0)), "columns": cols[:60]})
                return  # do not walk cells
            elif t == "GuiShell" and str(prop(o, "SubType")) == "GridView":
                n[0] += 1
                res["grids"].append({"id": oid, "row_count": int(prop(o, "RowCount", 0)),
                                     "column_count": int(prop(o, "ColumnCount", 0))})
                return
            if prop(o, "ContainerType", False):
                try:
                    for k in range(count(o.Children)):
                        visit(child(o.Children, k), depth + 1)
                except Exception:  # noqa: BLE001
                    pass

        visit(root, 0)
        for f in res["fields"]:            # label fallback: a GuiLabel just left of the field on the same row
            if "label" not in f or not f["label"]:
                f["label"] = _guess_label(f["id"], labels)
        if res["grids"] == [] and root_id.startswith("wnd[0]"):
            g = self._find(ses, "wnd[0]/shellcont/shell")   # ALV in docking container (SE16N result etc.)
            if g is not None and str(prop(g, "SubType")) == "GridView":
                res["grids"].append({"id": "wnd[0]/shellcont/shell", "row_count": int(prop(g, "RowCount", 0)),
                                     "column_count": int(prop(g, "ColumnCount", 0))})
        if n[0] >= MAX_ELEMENTS:
            res["truncated"] = True
        return res

    # ================================================================ pre-action inspection (policy input)
    def element_info(self, session_id: str, rid: str) -> dict[str, Any]:
        """Type / text / tooltip of a control, used by the policy to classify clicks (no side effects)."""
        def _e() -> dict[str, Any]:
            ses = self._session(session_id)
            el = self._find(ses, rid)
            if el is None:
                return {"exists": False}
            return {"exists": True, "type": str(prop(el, "Type")), "text": str(prop(el, "Text")),
                    "tooltip": str(prop(el, "Tooltip")), "name": str(prop(el, "Name"))}
        return self.worker.call(_e)

    def popup_info(self, session_id: str) -> dict[str, Any] | None:
        """Active popup (wnd[1..]) title + texts, or None (no side effects)."""
        def _p() -> dict[str, Any] | None:
            ses = self._session(session_id)
            win = self._active_window(ses)
            wid = rel_id(str(win.Id))
            if wid == "wnd[0]":
                return None
            body = self._walk_usr(ses, f"{wid}/usr", True, labels_as_text=True)
            texts = body.get("texts", []) + [str(f.get("value", "")) for f in body.get("fields", [])
                                             if not f.get("changeable")]
            return {"id": wid, "title": str(prop(win, "Text")), "texts": texts[:20],
                    "buttons": [b.get("text") or b.get("tooltip") for b in body.get("buttons", [])]}
        return self.worker.call(_p)

    # ================================================================ act (FSD 3.1.3)
    def act(self, session_id: str, ok_code: str = "", field_updates: dict[str, Any] | None = None,
            click_id: str = "", vkey: int | None = None, date_format: str = "",
            warning_allowlist: list[str] | None = None) -> dict[str, Any]:
        return self.worker.call(self._act, session_id, ok_code, field_updates or {}, click_id, vkey, date_format,
                                warning_allowlist or [], timeout=self.busy_timeout + 30)

    def _act(self, session_id: str, ok_code: str, field_updates: dict[str, Any], click_id: str, vkey: int | None,
             date_format: str, allow: list[str]) -> dict[str, Any]:
        ses = self._session(session_id)
        self._wait(ses)
        done: list[str] = []
        skipped: list[dict[str, str]] = []
        if click_id and self._find(ses, click_id) is None:
            raise ElementNotFound(f"element {click_id} not found on the current screen (nothing was changed)")
        if ok_code:
            okcd = self._find(ses, "wnd[0]/tbar[0]/okcd")
            if okcd is None:
                raise SapComError("command field (okcd) not found")
            okcd.Text = ok_code
            ses.FindById("wnd[0]").SendVKey(0)
            self._wait(ses)
            done.append(f"ok_code {ok_code}")
        for fid, val in field_updates.items():
            el = self._find(ses, fid)
            if el is None:
                skipped.append({"id": fid, "reason": "not found on screen"})
                continue
            if not prop(el, "Changeable", True):
                skipped.append({"id": fid, "reason": "field is not changeable"})
                continue
            t = str(prop(el, "Type"))
            if t == "GuiCheckBox":
                el.Selected = str(val).strip().upper() in ("1", "X", "TRUE", "YES", "Y")
            elif t == "GuiRadioButton":
                el.Select()
            elif t == "GuiComboBox":
                el.Key = str(val)
            else:
                el.Text = locale.convert_std_date(str(val), date_format)
            done.append(f"set {fid}")
        if click_id:
            el = self._find(ses, click_id)
            if el is None:
                raise SapComError(f"element {click_id} disappeared during the action")
            t = str(prop(el, "Type"))
            if t in ("GuiTab", "GuiRadioButton", "GuiMenu"):
                el.Select()
            elif t == "GuiCheckBox":
                el.Selected = not bool(prop(el, "Selected", False))
            else:
                el.Press()
            self._wait(ses)
            done.append(f"click {click_id}")
        if vkey is not None:
            self._active_window(ses).SendVKey(int(vkey))
            self._wait(ses)
            done.append(f"vkey {vkey}")
        acked = []
        allow_set = set(allow)
        for _ in range(5):  # FSD 3.1.5, restricted to allow-listed warnings (project safety rule)
            sb = self._status(ses)
            if sb.type == "W" and ("*" in allow_set or (sb.key and sb.key in allow_set)):
                self._active_window(ses).SendVKey(0)
                self._wait(ses)
                acked.append(sb.key or "W")
            else:
                break
        info = self._info(ses)
        return {"done": done, "skipped": skipped, "auto_acknowledged_warnings": acked,
                "status_bar": self._status(ses).model_dump(), "transaction": info.transaction,
                "program_dynpro": f"{info.program}_{info.screen}",
                "active_window": rel_id(str(self._active_window(ses).Id))}

    def rollback(self, session_id: str) -> dict[str, Any]:
        """FSD 3.4.3: leave the transaction with /n so enqueue locks (SM12) are released. Unsaved data is lost."""
        def _rb() -> dict[str, Any]:
            ses = self._session(session_id)
            for k in (3, 2, 1):  # close popups first
                w = self._find(ses, f"wnd[{k}]")
                if w is not None:
                    try:
                        w.SendVKey(12)
                        self._wait(ses)
                    except Exception:  # noqa: BLE001
                        pass
            ses.FindById("wnd[0]/tbar[0]/okcd").Text = "/n"
            ses.FindById("wnd[0]").SendVKey(0)
            self._wait(ses)
            if self._find(ses, "wnd[1]") is not None:
                # Only confirm "data will be lost / exit" popups. A "save changes?" popup is answered with No
                # (or cancelled) - the rollback must never save (review finding).
                body = self._walk_usr(ses, "wnd[1]/usr", True, labels_as_text=True)
                txt = " ".join(body.get("texts", []) + [str(prop(self._find(ses, "wnd[1]"), "Text"))]).lower()
                asks_save = bool(re.search(r"save|sichern|speichern|änderungen|changes", txt))
                order = ["wnd[1]/usr/btnSPOP-OPTION2"] if asks_save else ["wnd[1]/usr/btnSPOP-OPTION1"]
                pressed = False
                for bid in order:
                    b = self._find(ses, bid)
                    if b is not None:
                        b.Press()
                        self._wait(ses)
                        pressed = True
                        break
                if not pressed or self._find(ses, "wnd[1]") is not None:
                    w = self._find(ses, "wnd[1]")
                    if w is not None:
                        w.SendVKey(12)
                        self._wait(ses)
                    return {"rolled_back": False, "reason": "popup could not be answered safely - check the "
                            "SAPBot SAP window", "status_bar": self._status(ses).model_dump()}
            return {"rolled_back": True, "status_bar": self._status(ses).model_dump()}
        return self.worker.call(_rb)

    # ================================================================ grids (FSD 3.1.4)
    def read_grid(self, session_id: str, grid_id: str = "", max_rows: int = 50,
                  export_dir: Path | None = None) -> dict[str, Any]:
        return self.worker.call(self._read_grid, session_id, grid_id, max_rows, export_dir,
                                timeout=max(120.0, self.busy_timeout * 3))

    def _locate_grid(self, ses: Any, grid_id: str) -> Any:
        if grid_id:
            g = self._find(ses, grid_id)
            if g is None:
                raise SapComError(f"grid {grid_id} not found")
            return g
        snap = self._walk_usr(ses, "wnd[0]/usr", False)
        cands = [x["id"] for x in snap["grids"]] + [x["id"] for x in snap["tables"]]
        if not cands:
            raise SapComError("no ALV grid or table control on the current screen")
        return ses.FindById(cands[0])

    def _read_grid(self, session_id: str, grid_id: str, max_rows: int, export_dir: Path | None) -> dict[str, Any]:
        ses = self._session(session_id)
        self._wait(ses)
        g = self._locate_grid(ses, grid_id)
        gtype = str(prop(g, "Type"))
        if gtype == "GuiTableControl":
            return self._read_table_control(ses, g, max_rows)
        total = int(prop(g, "RowCount", 0))
        cols = [str(child(g.ColumnOrder, c)) for c in range(count(g.ColumnOrder))]
        want = min(total, max(1, int(max_rows)))
        threshold = self.s.sap.grid_clipboard_threshold
        if want > threshold and export_dir is not None:
            rows = self._alv_export(ses, g, export_dir)
            if rows is not None:
                return {"grid_id": rel_id(str(g.Id)), "row_count": total, "columns": cols, "method": "alv_export",
                        "rows": rows[:want], "returned": min(want, len(rows))}
        rows = []
        step = max(1, int(prop(g, "VisibleRowCount", 15) or 15))
        for r in range(want):
            if r % step == 0:
                try:
                    g.FirstVisibleRow = r
                except Exception:  # noqa: BLE001
                    pass
            row = {}
            for c in cols:
                try:
                    row[c] = str(g.GetCellValue(r, c)).strip()
                except Exception:  # noqa: BLE001
                    row[c] = ""
            rows.append(row)
        return {"grid_id": rel_id(str(g.Id)), "row_count": total, "columns": cols, "method": "cells",
                "rows": rows, "returned": len(rows)}

    def _read_table_control(self, ses: Any, t: Any, max_rows: int) -> dict[str, Any]:
        cols = [str(prop(child(t.Columns, c), "Title")) for c in range(count(t.Columns))]
        rows = []
        vis = int(prop(t, "VisibleRowCount", 0)) or 10
        for r in range(min(int(max_rows), vis)):
            row = {}
            for c, title in enumerate(cols):
                try:
                    row[title or f"col{c}"] = str(t.GetCell(r, c).Text).strip()
                except Exception:  # noqa: BLE001
                    row[title or f"col{c}"] = ""
            if any(v for v in row.values()):
                rows.append(row)
        return {"grid_id": rel_id(str(t.Id)), "row_count": int(prop(t, "RowCount", len(rows))), "columns": cols,
                "method": "table_control_visible", "rows": rows, "returned": len(rows),
                "note": "table controls return visible rows only; scroll with sap_act to read more"}

    def _alv_export(self, ses: Any, g: Any, export_dir: Path) -> list[dict[str, str]] | None:
        """ALV toolbar Export > Local file > unconverted/text-with-tabs (same sequence sap-gate uses on SUQ)."""
        export_dir.mkdir(parents=True, exist_ok=True)
        name = f"grid_{dt.datetime.now():%Y%m%d_%H%M%S}.txt"
        path = export_dir / name
        try:
            g.PressToolbarContextButton("&MB_EXPORT")
            g.SelectContextMenuItem("&PC")
            self._wait(ses)
            rb = self._find(ses, "wnd[1]/usr/subSUBSCREEN_STEPLOOP:SAPLSPO5:0150/sub:SAPLSPO5:0150/"
                                 "radSPOPLI-SELFLAG[1,0]")
            if rb is not None:
                rb.Select()
            ses.FindById("wnd[1]/tbar[0]/btn[0]").Press()
            self._wait(ses)
            ses.FindById("wnd[1]/usr/ctxtDY_PATH").Text = str(export_dir)
            ses.FindById("wnd[1]/usr/ctxtDY_FILENAME").Text = name
            enc = self._find(ses, "wnd[1]/usr/ctxtDY_FILE_ENCODING")
            if enc is not None:
                enc.Text = "4110"
            ses.FindById("wnd[1]/tbar[0]/btn[11]").Press()   # file dialog 'Generate' - not an SAP save
            self._wait(ses)
            end = time.time() + 15
            while not path.exists() and time.time() < end:
                time.sleep(0.3)
            if not path.exists():
                return None
            return parse_alv_text(path.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001
            log.warning("ALV export failed, falling back to cell reads: %s", e)
            for k in (2, 1):
                w = self._find(ses, f"wnd[{k}]")
                if w is not None:
                    try:
                        w.SendVKey(12)
                    except Exception:  # noqa: BLE001
                        pass
            return None

    # ================================================================ probe support (ADR A6/B4)
    def detect_date_format(self, session_id: str) -> str:
        """Read-only: open SM37 (its 'from date' defaults to today) and see how today's date is rendered."""
        def _d() -> str:
            ses = self._session(session_id)
            ses.FindById("wnd[0]/tbar[0]/okcd").Text = "/nSM37"
            ses.FindById("wnd[0]").SendVKey(0)
            self._wait(ses)
            vals = [f.get("value", "") for f in self._walk_usr(ses, "wnd[0]/usr", True)["fields"]]
            fmt = locale.detect_date_format([str(v) for v in vals])
            ses.FindById("wnd[0]/tbar[0]/okcd").Text = "/n"
            ses.FindById("wnd[0]").SendVKey(0)
            self._wait(ses)
            return fmt
        return self.worker.call(_d)


def _guess_label(fid: str, labels: dict[str, str]) -> str:
    # ids like wnd[0]/usr/ctxtVBAK-AUART: use the technical name as last resort
    tail = fid.rsplit("/", 1)[-1]
    for lid, txt in labels.items():
        if lid.rsplit("/", 1)[0] == fid.rsplit("/", 1)[0] and tail[4:] and tail[4:] in lid:
            return txt
    return tail[4:] if len(tail) > 4 else tail


def parse_alv_text(text: str) -> list[dict[str, str]]:
    """Parse SAP 'unconverted' ALV export: title lines, dashed separators, a header line, then '|'- or tab-separated
    rows. Returns list of dicts keyed by the header labels."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not set(ln.strip()) <= set("-|")]
    if not lines:
        return []
    delim = "\t" if any("\t" in ln for ln in lines[:10]) else "|"
    rows = [[c.strip() for c in next(csv.reader(io.StringIO(ln), delimiter=delim))] for ln in lines]
    rows = [r[1:-1] if delim == "|" and len(r) > 2 and r[0] == "" and r[-1] == "" else r for r in rows]
    width = max(len(r) for r in rows)
    hdr_idx = next((i for i, r in enumerate(rows) if len(r) == width), 0)
    header = [h or f"col{i}" for i, h in enumerate(rows[hdr_idx])]
    return [dict(zip(header, r)) for r in rows[hdr_idx + 1:] if len(r) == width]
