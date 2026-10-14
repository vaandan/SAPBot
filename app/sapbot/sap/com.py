"""Thin COM helpers. Windows-only imports are guarded so the package imports (and tests run) anywhere."""
from __future__ import annotations

import logging
import sys
from typing import Any

log = logging.getLogger(__name__)

try:  # pragma: no cover - Windows only
    import pythoncom  # type: ignore
    import win32com.client  # type: ignore
    HAVE_COM = True
except Exception:  # noqa: BLE001
    pythoncom = None  # type: ignore
    win32com = None  # type: ignore
    HAVE_COM = False


class SapComError(RuntimeError):
    """Raised for SAP GUI scripting problems with a clear, user-facing message."""


class ElementNotFound(SapComError):
    """The requested control does not exist on the current screen - nothing was changed in SAP."""


def co_initialize() -> None:
    if pythoncom is not None:
        pythoncom.CoInitialize()


def co_uninitialize() -> None:
    if pythoncom is not None:
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


def real_application() -> Any:  # pragma: no cover - needs SAP GUI
    """GuiApplication of the running SAP Logon. Only client-side GUI scripting is required (ADR A6)."""
    if not HAVE_COM:
        raise SapComError("SAP GUI scripting needs Windows with pywin32 installed")
    try:
        sapgui = win32com.client.GetObject("SAPGUI")
    except Exception as e:  # noqa: BLE001
        raise SapComError("SAP Logon is not running (GetObject('SAPGUI') failed) - start SAP Logon and log on") from e
    try:
        app = sapgui.GetScriptingEngine
    except Exception as e:  # noqa: BLE001
        raise SapComError("SAP GUI scripting is disabled in SAP Logon: Options > Accessibility & Scripting > "
                          "Scripting > Enable scripting") from e
    if app is None:
        raise SapComError("SAP GUI scripting engine not available")
    return app


def child(coll: Any, i: int) -> Any:
    """Index a GuiComponentCollection the way that works in both pywin32 and the fake."""
    try:
        return coll.ElementAt(i)
    except Exception:  # noqa: BLE001
        return coll(i)


def count(coll: Any) -> int:
    try:
        return int(coll.Count)
    except Exception:  # noqa: BLE001
        return int(coll.Length)


def prop(obj: Any, name: str, default: Any = "") -> Any:
    try:
        v = getattr(obj, name)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def is_windows() -> bool:
    return sys.platform == "win32"
