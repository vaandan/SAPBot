"""Builds the SAPBot object graph for a given channel (Cowork MCP server, CLI/API loop, Cockpit, tests)."""
from __future__ import annotations

import logging
import logging.handlers
import os
from typing import Any, Callable

from . import paths
from .config import get_settings
from .scripts.library import RunFn, ScriptLibrary, ScriptRunner
from .security.approvals import ApprovalBroker, ApprovalCallback
from .security.governance import Governance
from .security.store import Store
from .sap.bridge import SAPGuiBridge
from .tools.executor import ToolExecutor

_logging_done = False


def setup_logging(name: str = "sapbot", to_stderr: bool = True) -> None:
    """File log in <home>/logs (rotating). MCP stdio servers must never print to stdout."""
    global _logging_done
    if _logging_done:
        return
    paths.ensure_layout()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(paths.logs_dir() / f"{name}.log", maxBytes=5_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%d.%m.%Y %H:%M:%S"))
    root.addHandler(fh)
    if to_stderr:
        sh = logging.StreamHandler()
        sh.setLevel(logging.WARNING)
        root.addHandler(sh)
    _logging_done = True


def build(channel: str = "cowork", *, model: str = "", app_factory: Callable[[], Any] | None = None,
          approval_callback: ApprovalCallback | None = None, approval_mode: str | None = None,
          script_run_fn: RunFn | None = None) -> ToolExecutor:
    paths.ensure_layout()
    s = get_settings()
    store = Store()
    store.bootstrap()
    gov = Governance(store)
    if app_factory is None and os.environ.get("SAPBOT_FAKE_SAP") == "1":
        from .sap.fake import FakeSapApp
        app_factory = FakeSapApp.factory(os.environ.get("SAPBOT_FAKE_SID", "SUQ"),
                                         os.environ.get("SAPBOT_FAKE_CLIENT", "400"))
    bridge = SAPGuiBridge(app_factory)
    broker = ApprovalBroker(store, approval_mode or s.approvals.mode, s.approvals.timeout_seconds,
                            approval_callback)
    lib = ScriptLibrary(store)
    runner = ScriptRunner(lib, script_run_fn)
    return ToolExecutor(gov, bridge, broker, lib, runner, channel=channel, model=model, settings=s)
