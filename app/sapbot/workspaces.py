"""Client workspaces (ADR A13): physical isolation per client.

<SAPBOT_HOME>/workspaces/<id>/
  workspace.yaml     systems (SID, client, env QA/DEV/PROD, write), allowed T-codes, data policy, retention
  chroma/            this client's RAG vector store (never shared with other clients)
  trajectories/      run logs + gold/silver JSONL
  schema_cache/      FSD 3.3 screen schema cache
  output/            grid exports, batch results, script output
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from . import paths

log = logging.getLogger(__name__)
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,40}$")


class SapSystem(BaseModel):
    sid: str
    client: str
    env: Literal["QA", "DEV", "PROD"] = "QA"
    write: bool = False            # QA/DEV write enabled (A14). PROD write always needs a grant.
    description: str = ""

    @field_validator("sid")
    @classmethod
    def _sid(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("client", mode="before")
    @classmethod
    def _client(cls, v: object) -> str:
        return str(v).strip().zfill(3)

    @property
    def is_prod(self) -> bool:
        return self.env == "PROD"


class Workspace(BaseModel):
    id: str
    name: str
    data_policy: Literal["full", "masked", "local-only"] = "full"
    systems: list[SapSystem] = Field(default_factory=list)
    allowed_tcodes: list[str] = Field(default_factory=list)   # on top of display list; "*" = any (hard stops apply)
    retention_days: int = 365
    mask_fields: list[str] = Field(default_factory=lambda: [
        "NAME1", "NAME2", "NAME3", "STRAS", "TELF1", "TELFX", "SMTP_ADDR", "BANKN", "IBAN", "STCD1", "STCD2",
        "STCD3", "J_1IPANNO", "PERNR", "BNAME"])
    shared_pool_opt_in: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        v = v.strip().lower()
        if not ID_RE.match(v):
            raise ValueError("workspace id: lowercase letters, digits, - or _ (2-41 chars)")
        return v

    # ---------------------------------------------------------------- paths
    @property
    def root(self) -> Path:
        return paths.workspaces_dir() / self.id

    @property
    def chroma_dir(self) -> Path:
        return self.root / "chroma"

    @property
    def trajectories_dir(self) -> Path:
        return self.root / "trajectories"

    @property
    def schema_cache_dir(self) -> Path:
        return self.root / "schema_cache"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    def find_system(self, sid: str, client: str) -> SapSystem | None:
        c = str(client).zfill(3)
        return next((s for s in self.systems if s.sid == sid.upper() and s.client == c), None)

    def tcode_allowed(self, tcode: str, display_tcodes: list[str]) -> bool:
        t = tcode.upper()
        allowed = {x.upper() for x in self.allowed_tcodes}
        return "*" in allowed or t in allowed or t in {d.upper() for d in display_tcodes}

    def save(self) -> Path:
        for d in (self.root, self.chroma_dir, self.trajectories_dir, self.schema_cache_dir, self.output_dir):
            d.mkdir(parents=True, exist_ok=True)
        f = self.root / "workspace.yaml"
        f.write_text(yaml.safe_dump(self.model_dump(), sort_keys=False, allow_unicode=True), encoding="utf-8")
        return f


def load(ws_id: str) -> Workspace:
    f = paths.workspaces_dir() / ws_id.lower() / "workspace.yaml"
    if not f.exists():
        raise FileNotFoundError(f"workspace '{ws_id}' not found ({f})")
    return Workspace.model_validate(yaml.safe_load(f.read_text(encoding="utf-8")))


def list_all() -> list[Workspace]:
    out: list[Workspace] = []
    base = paths.workspaces_dir()
    if not base.exists():
        return out
    for f in sorted(base.glob("*/workspace.yaml")):
        try:
            out.append(Workspace.model_validate(yaml.safe_load(f.read_text(encoding="utf-8"))))
        except Exception as e:  # noqa: BLE001
            log.error("Bad workspace file %s: %s", f, e)
    return out


def create(ws_id: str, name: str, systems: list[dict] | None = None, **kw: object) -> Workspace:
    ws = Workspace(id=ws_id, name=name, systems=[SapSystem(**s) for s in (systems or [])], **kw)
    if (ws.root / "workspace.yaml").exists():
        raise FileExistsError(f"workspace '{ws.id}' already exists")
    ws.save()
    return ws


def purge(ws_id: str) -> None:
    """Client offboarding (ADR B9): delete every local trace of the workspace. Export first if needed."""
    ws = load(ws_id)
    shutil.rmtree(ws.root, ignore_errors=True)


def find_workspace_for(sid: str, client: str, allowed: list[str] | None = None) -> Workspace | None:
    """Which workspace owns a live SAP system/client (used to detect wrong-client mistakes)."""
    for ws in list_all():
        if allowed is not None and "*" not in allowed and ws.id not in allowed:
            continue
        if ws.find_system(sid, client):
            return ws
    return None
