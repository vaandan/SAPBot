"""Single-threaded COM worker (ADR guiding decision: COM objects cannot cross threads; Streamlit re-runs scripts).

All SAP GUI COM calls are executed on ONE dedicated thread that called CoInitialize. Other threads submit
callables and wait for the result with a timeout.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import queue
import threading
from typing import Any, Callable

from .com import co_initialize, co_uninitialize

log = logging.getLogger(__name__)


class ComWorker:
    def __init__(self, app_factory: Callable[[], Any], name: str = "sap-com-worker") -> None:
        self._factory = app_factory
        self._q: queue.Queue[tuple[Callable[..., Any], tuple, dict, cf.Future] | None] = queue.Queue()
        self._app: Any = None
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def app(self) -> Any:
        """Only valid inside the worker thread."""
        if self._app is None:
            self._app = self._factory()
        return self._app

    def reset_app(self) -> None:
        self._app = None

    def _run(self) -> None:
        co_initialize()
        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                fn, args, kwargs, fut = item
                if fut.set_running_or_notify_cancel():
                    try:
                        fut.set_result(fn(*args, **kwargs))
                    except BaseException as e:  # noqa: BLE001
                        self._app = None  # re-attach next time (SAP Logon may have restarted)
                        fut.set_exception(e)
        finally:
            co_uninitialize()

    def call(self, fn: Callable[..., Any], *args: Any, timeout: float = 120.0, **kwargs: Any) -> Any:
        if threading.current_thread() is self._thread:
            return fn(*args, **kwargs)
        fut: cf.Future = cf.Future()
        self._q.put((fn, args, kwargs, fut))
        try:
            return fut.result(timeout=timeout)
        except cf.TimeoutError:
            if fut.cancel():   # still queued: it will never run
                raise TimeoutError(f"SAP call timed out after {timeout:.0f} s (not executed)") from None
            # already running: SAP state is unknown - the caller must re-observe before acting again
            raise TimeoutError(f"SAP call still running after {timeout:.0f} s - SAP state unknown, re-observe "
                               "the screen before the next action") from None

    def stop(self) -> None:
        self._q.put(None)
