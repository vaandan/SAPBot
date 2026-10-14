"""Shared payload types. Every tool returns ToolResult -> {ok, data, status_bar:{type,text,...}, error}."""
from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field

MsgType = Literal["S", "W", "E", "A", "I", ""]


class StatusBar(BaseModel):
    type: str = ""            # S W E A I or ""
    text: str = ""
    msg_id: str = ""          # message class, e.g. "V1"
    msg_number: str = ""      # e.g. "311"
    msg_params: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        """Language-independent message key 'CLASS/NUMBER' (ADR B4)."""
        return f"{self.msg_id}/{self.msg_number}" if self.msg_id else ""


class ToolResult(BaseModel):
    ok: bool
    data: Any = None
    status_bar: StatusBar = Field(default_factory=StatusBar)
    error: str | None = None
    needs_human: bool = False         # loop guard / approval pending / elevation required
    policy: dict[str, Any] | None = None

    @classmethod
    def fail(cls, error: str, **kw: Any) -> "ToolResult":
        return cls(ok=False, error=error, **kw)


class SessionInfo(BaseModel):
    session_id: str
    system: str
    client: str
    user: str = ""
    language: str = ""
    transaction: str = ""
    program: str = ""
    screen: str = ""
    busy: bool = False


def now_str() -> str:
    """Timestamps in logs use the firm standard DD.MM.YYYY (ADR B4)."""
    return dt.datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")
