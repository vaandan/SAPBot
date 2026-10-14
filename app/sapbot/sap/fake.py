"""Fake SAP GUI scripting object model for tests and offline demos (no SAP needed).

Implements the subset of the GuiApplication API the bridge uses, with a few scripted transactions:
  SE16N (table T001 -> ALV grid), COR3 (display order), VA01 (create order -> save -> document number,
  warning V1/555 for big quantities), SM37 (date field = today), plus a 'data will be lost' popup on /n
  from a dirty VA01 screen.
Use: SAPGuiBridge(app_factory=FakeSapApp.factory("SUQ", "400")).
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable


class FakeCollection:
    def __init__(self, items: list[Any] | None = None) -> None:
        self.items = items or []

    @property
    def Count(self) -> int:  # noqa: N802
        return len(self.items)

    def ElementAt(self, i: int) -> Any:  # noqa: N802
        return self.items[i]

    def __call__(self, i: int) -> Any:
        return self.items[i]


class FakeEl:
    def __init__(self, type_: str, rid: str, text: str = "", changeable: bool = True, tooltip: str = "",
                 on_press: Callable[[], None] | None = None, container: bool = False, **extra: Any) -> None:
        self.Type = type_
        self._rid = rid
        self.Text = text
        self.Changeable = changeable
        self.Tooltip = tooltip
        tail = rid.rsplit("/", 1)[-1]
        self.Name = tail.lstrip("abcdefghijklmnopqrstuvwxyz") or tail
        self.ContainerType = container
        self.Children = FakeCollection()
        self.Selected = False
        self.Key = ""
        self.Value = ""
        self._on_press = on_press
        self._session: FakeSession | None = None
        for k, v in extra.items():
            setattr(self, k, v)

    @property
    def Id(self) -> str:  # noqa: N802
        base = self._session.Id if self._session else "/app/con[0]/ses[0]"
        return f"{base}/{self._rid}"

    def Press(self) -> None:  # noqa: N802
        if self._on_press:
            self._on_press()

    def Select(self) -> None:  # noqa: N802
        self.Selected = True
        if self._on_press:
            self._on_press()

    def SendVKey(self, k: int) -> None:  # noqa: N802
        assert self._session is not None
        self._session._vkey(self._rid.split("/")[0], int(k))


class FakeGrid(FakeEl):
    def __init__(self, rid: str, columns: list[str], rows: list[dict[str, str]]) -> None:
        super().__init__("GuiShell", rid, changeable=False, SubType="GridView")
        self._cols = columns
        self._rows = rows
        self.FirstVisibleRow = 0

    @property
    def RowCount(self) -> int:  # noqa: N802
        return len(self._rows)

    @property
    def ColumnCount(self) -> int:  # noqa: N802
        return len(self._cols)

    @property
    def ColumnOrder(self) -> FakeCollection:  # noqa: N802
        return FakeCollection(list(self._cols))

    def GetCellValue(self, r: int, c: str) -> str:  # noqa: N802
        return self._rows[r].get(c, "")

    def PressToolbarContextButton(self, _b: str) -> None:  # noqa: N802
        raise RuntimeError("export not supported in fake")


class FakeInfo:
    def __init__(self, system: str, client: str, user: str) -> None:
        self.SystemName = system
        self.Client = client
        self.User = user
        self.Language = "EN"
        self.Transaction = "SESSION_MANAGER"
        self.Program = "SAPLSMTR_NAVIGATION"
        self.ScreenNumber = 100


T001_ROWS = [{"BUKRS": f"{1000 + i}", "BUTXT": f"Company {i}", "ORT01": "Pune", "LAND1": "IN", "WAERS": "INR"}
             for i in range(130)]


class FakeSession:
    def __init__(self, con: "FakeConnection", idx: int, system: str, client: str, user: str) -> None:
        self.con = con
        self.idx = idx
        self.Info = FakeInfo(system, client, user)
        self.Busy = False
        self.els: dict[str, FakeEl] = {}
        self.popup: dict[str, Any] | None = None
        self.dirty = False
        self.saved_orders: list[str] = []
        self.next_order = 12345
        self.calls: list[str] = []
        self._sbar = ("", "", "", "", [])
        self._screen_init()

    @property
    def Id(self) -> str:  # noqa: N802
        return f"{self.con.Id}/ses[{self.idx}]"

    # ---------------------------------------------------------------- API
    def FindById(self, rid: str) -> Any:  # noqa: N802
        if rid.startswith("/app/"):
            rid = rid[rid.find("/wnd[") + 1:]
        if rid == "wnd[0]/sbar":
            t, txt, mid, num, prm = self._sbar
            el = FakeEl("GuiStatusbar", rid, txt, MessageType=t, MessageId=mid, MessageNumber=num,
                        MessageParameter="|".join(prm))
            el._session = self
            return el
        if rid in self.els:
            return self.els[rid]
        raise RuntimeError(f"The control could not be found by id. ({rid})")

    @property
    def ActiveWindow(self) -> FakeEl:  # noqa: N802
        return self.els["wnd[1]"] if "wnd[1]" in self.els else self.els["wnd[0]"]

    def CreateSession(self) -> None:  # noqa: N802
        if len(self.con.sessions) >= 6:
            raise RuntimeError("Maximum number of sessions reached")
        self.con.add_session(self.Info.SystemName, self.Info.Client, self.Info.User)

    # ---------------------------------------------------------------- screen building
    def _reg(self, el: FakeEl) -> FakeEl:
        el._session = self
        self.els[el._rid] = el
        return el

    def _base(self, title: str) -> FakeEl:
        self.els = {}
        w = self._reg(FakeEl("GuiMainWindow", "wnd[0]", title, container=True))
        self._reg(FakeEl("GuiOkCodeField", "wnd[0]/tbar[0]/okcd", ""))
        tb0 = self._reg(FakeEl("GuiToolbar", "wnd[0]/tbar[0]", container=True))
        save = self._reg(FakeEl("GuiButton", "wnd[0]/tbar[0]/btn[11]", tooltip="Save (Ctrl+S)",
                                on_press=lambda: self._vkey("wnd[0]", 11)))
        tb0.Children.items.append(save)
        self._reg(FakeEl("GuiToolbar", "wnd[0]/tbar[1]", container=True))
        usr = self._reg(FakeEl("GuiUserArea", "wnd[0]/usr", container=True))
        w.Children.items.append(usr)
        return usr

    def _add(self, parent: FakeEl, el: FakeEl) -> FakeEl:
        self._reg(el)
        parent.Children.items.append(el)
        return el

    def _set_status(self, t: str = "", txt: str = "", mid: str = "", num: str = "", prm: list[str] | None = None):
        self._sbar = (t, txt, mid, num, prm or [])

    def _screen_init(self) -> None:
        self.Info.Transaction, self.Info.Program, self.Info.ScreenNumber = "SESSION_MANAGER", "SAPLSMTR_NAVIGATION", 100
        self._base("SAP Easy Access")
        self.dirty = False

    def _screen_se16n(self) -> None:
        self.Info.Transaction, self.Info.Program, self.Info.ScreenNumber = "SE16N", "SAPLSE16N", 100
        usr = self._base("General Table Display")
        lab = self._add(usr, FakeEl("GuiLabel", "wnd[0]/usr/lblGD-TAB", "Table"))
        f = self._add(usr, FakeEl("GuiCTextField", "wnd[0]/usr/ctxtGD-TAB", ""))
        f.LeftLabel = lab
        self._add(usr, FakeEl("GuiTextField", "wnd[0]/usr/txtGD-MAX_LINES", "500"))
        tb1 = self.els["wnd[0]/tbar[1]"]
        ex = self._reg(FakeEl("GuiButton", "wnd[0]/tbar[1]/btn[8]", tooltip="Execute (F8)",
                              on_press=lambda: self._vkey("wnd[0]", 8)))
        tb1.Children.items.append(ex)

    def _screen_se16n_result(self, table: str) -> None:
        if table != "T001":
            self._set_status("I", f"No values found for table {table}", "WUSL", "009")
            return
        self.Info.ScreenNumber = 200
        usr = self._base(f"Table T001 - 130 entries")
        g = FakeGrid("wnd[0]/usr/cntlRESULT_LIST/shellcont/shell", ["BUKRS", "BUTXT", "ORT01", "LAND1", "WAERS"],
                     T001_ROWS)
        self._add(usr, g)
        self._set_status("S", "130 entries found", "WUSL", "010", ["130"])

    def _screen_cor3(self) -> None:
        self.Info.Transaction, self.Info.Program, self.Info.ScreenNumber = "COR3", "SAPLCOKO", 110
        usr = self._base("Display Process Order: Initial Screen")
        self._add(usr, FakeEl("GuiCTextField", "wnd[0]/usr/ctxtCAUFVD-AUFNR", "", tooltip="Process Order"))

    def _screen_cor3_detail(self, aufnr: str) -> None:
        self.Info.ScreenNumber = 115
        usr = self._base(f"Display Process Order {aufnr}: Header")
        self._add(usr, FakeEl("GuiTextField", "wnd[0]/usr/txtCAUFVD-AUART", "PI01", changeable=False,
                              tooltip="Order Type"))
        self._add(usr, FakeEl("GuiTextField", "wnd[0]/usr/txtCAUFVD-STTXT", "REL  PRC  BCRQ", changeable=False,
                              tooltip="System Status"))
        self._set_status("", "")

    def _screen_va01(self) -> None:
        self.Info.Transaction, self.Info.Program, self.Info.ScreenNumber = "VA01", "SAPMV45A", 101
        usr = self._base("Create Sales Documents")
        for fid, tip in (("ctxtVBAK-AUART", "Order Type"), ("ctxtVBAK-VKORG", "Sales Organization"),
                         ("ctxtVBAK-VTWEG", "Distribution Channel"), ("ctxtVBAK-SPART", "Division")):
            self._add(usr, FakeEl("GuiCTextField", f"wnd[0]/usr/{fid}", "", tooltip=tip))

    def _screen_va01_overview(self) -> None:
        self.Info.ScreenNumber = 4001
        usr = self._base("Create Standard Order: Overview")
        self._add(usr, FakeEl("GuiCTextField", "wnd[0]/usr/ctxtKUAGV-KUNNR", "", tooltip="Sold-To Party"))
        self._add(usr, FakeEl("GuiCTextField", "wnd[0]/usr/ctxtRV45A-MABNR", "", tooltip="Material"))
        self._add(usr, FakeEl("GuiTextField", "wnd[0]/usr/txtRV45A-KWMENG", "", tooltip="Order Quantity"))
        self._add(usr, FakeEl("GuiCTextField", "wnd[0]/usr/ctxtRV45A-KETDAT", "", tooltip="Req. deliv.date"))
        self._add(usr, FakeEl("GuiPasswordField", "wnd[0]/usr/pwdRSYST-BCODE", "hunter2", tooltip="Password"))
        self.dirty = True
        self.warned = False

    def _screen_sm37(self) -> None:
        self.Info.Transaction, self.Info.Program, self.Info.ScreenNumber = "SM37", "SAPLBTCH", 2170
        usr = self._base("Simple Job Selection")
        self._add(usr, FakeEl("GuiCTextField", "wnd[0]/usr/ctxtBTCH2170-FROM_DATE",
                              dt.date.today().strftime("%d.%m.%Y"), tooltip="From"))

    def _popup_data_loss(self, then: Callable[[], None]) -> None:
        w = self._reg(FakeEl("GuiModalWindow", "wnd[1]", "Exit Document", container=True))
        usr = self._reg(FakeEl("GuiUserArea", "wnd[1]/usr", container=True))
        w.Children.items.append(usr)
        usr.Children.items.append(self._reg(FakeEl("GuiLabel", "wnd[1]/usr/txtSPOP-TEXTLINE1",
                                                   "Data will be lost. Exit editing?")))

        def yes() -> None:
            self.els.pop("wnd[1]", None)
            then()
        usr.Children.items.append(self._reg(FakeEl("GuiButton", "wnd[1]/usr/btnSPOP-OPTION1", "Yes", on_press=yes)))
        self._reg(FakeEl("GuiToolbar", "wnd[1]/tbar[0]", container=True))

    # ---------------------------------------------------------------- behaviour
    def _vkey(self, wnd: str, k: int) -> None:
        self.calls.append(f"vkey {k} on {wnd}")
        if wnd == "wnd[1]":
            if k == 12:
                self.els = {kk: v for kk, v in self.els.items() if not kk.startswith("wnd[1]")}
            return
        okcd = self.els.get("wnd[0]/tbar[0]/okcd")
        cmd = okcd.Text.strip() if okcd else ""
        if okcd:
            okcd.Text = ""
        if cmd:
            return self._command(cmd)
        tx = self.Info.Transaction
        if tx == "SE16N" and k in (0, 8) and self.Info.ScreenNumber == 100:
            tab = self.els["wnd[0]/usr/ctxtGD-TAB"].Text.strip().upper()
            if k == 8 or tab:
                return self._screen_se16n_result(tab) if k == 8 else None
        if tx == "COR3" and k == 0 and self.Info.ScreenNumber == 110:
            aufnr = self.els["wnd[0]/usr/ctxtCAUFVD-AUFNR"].Text.strip()
            if not aufnr:
                return self._set_status("E", "Enter an order number", "CO", "001")
            return self._screen_cor3_detail(aufnr)
        if tx == "VA01" and k == 0 and self.Info.ScreenNumber == 101:
            if not self.els["wnd[0]/usr/ctxtVBAK-AUART"].Text.strip():
                return self._set_status("E", "Fill in all required entry fields", "00", "055")
            self._set_status("", "")
            return self._screen_va01_overview()
        if tx == "VA01" and self.Info.ScreenNumber == 4001:
            if k == 0 and getattr(self, "pending_warning", False):
                self.pending_warning = False
                self.warned = True
                return self._set_status("", "")
            if k == 11:
                kunnr = self.els["wnd[0]/usr/ctxtKUAGV-KUNNR"].Text.strip()
                if not kunnr:
                    return self._set_status("E", "Sold-to party is missing", "V1", "012")
                qty = self.els["wnd[0]/usr/txtRV45A-KWMENG"].Text.strip() or "0"
                if float(qty.replace(",", ".")) > 100 and not self.warned:
                    self.pending_warning = True
                    return self._set_status("W", "Quantity exceeds usual order size", "V1", "555", [qty])
                num = str(self.next_order)
                self.next_order += 1
                self.saved_orders.append(num)
                self._screen_va01()
                self.dirty = False
                return self._set_status("S", f"Standard Order {num} has been saved", "V1", "311",
                                        ["Standard Order", num])
        if k == 3:
            return self._screen_init()

    def _command(self, cmd: str) -> None:
        c = cmd.upper()
        target = c[2:] if c.startswith(("/N", "/O")) else c
        if c.startswith("/N") and self.dirty:
            return self._popup_data_loss(lambda: self._go(target))
        self._go(target)

    def _go(self, tcode: str) -> None:
        self._set_status("", "")
        screens = {"SE16N": self._screen_se16n, "COR3": self._screen_cor3, "VA01": self._screen_va01,
                   "SM37": self._screen_sm37, "": self._screen_init}
        fn = screens.get(tcode)
        if fn is None:
            self._screen_init()
            return self._set_status("E", f"Transaction {tcode} does not exist", "00", "343", [tcode])
        fn()


class FakeConnection:
    def __init__(self, app: "FakeSapApp", idx: int) -> None:
        self.app = app
        self.idx = idx
        self.sessions: list[FakeSession] = []

    @property
    def Id(self) -> str:  # noqa: N802
        return f"/app/con[{self.idx}]"

    @property
    def Children(self) -> FakeCollection:  # noqa: N802
        return FakeCollection(self.sessions)

    def add_session(self, system: str, client: str, user: str) -> FakeSession:
        s = FakeSession(self, len(self.sessions), system, client, user)
        self.sessions.append(s)
        return s


class FakeSapApp:
    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []

    @property
    def Children(self) -> FakeCollection:  # noqa: N802
        return FakeCollection(self.connections)

    def logon(self, system: str, client: str, user: str = "VANDAN") -> FakeSession:
        con = FakeConnection(self, len(self.connections))
        self.connections.append(con)
        return con.add_session(system, client, user)

    @classmethod
    def factory(cls, system: str = "SUQ", client: str = "400") -> Callable[[], "FakeSapApp"]:
        app = cls()
        app.logon(system, client)
        return lambda: app
