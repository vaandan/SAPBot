"""Schema Cacher & Memory (FSD 3.3) - per client workspace.

Screens are cached by PROGRAM_DYNPRO (e.g. SAPMV45A_4001): field ids, labels, buttons, tabs, grids. When a known
screen is revisited the tool layer can send only the dynamic state (values + status bar) to the model.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..types import now_iso

log = logging.getLogger(__name__)
KEY_RE = re.compile(r"^[A-Z0-9_/]{1,60}$")


class SchemaCache:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)

    def _file(self, key: str) -> Path:
        key = key.upper().replace("/", "#")
        return self.folder / f"{key}.json"

    def get(self, program_dynpro: str) -> dict[str, Any] | None:
        f = self._file(program_dynpro)
        if not f.exists():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def put(self, snap: dict[str, Any], notes: str = "") -> dict[str, Any]:
        key = snap.get("program_dynpro", "")
        if not key or not KEY_RE.match(key.upper()):
            return {}
        prev = self.get(key) or {}
        entry = {
            "program_dynpro": key, "transaction": snap.get("transaction"), "title": snap.get("title"),
            "fields": [{k: f.get(k) for k in ("id", "label", "type", "changeable", "required") if k in f}
                       for f in snap.get("fields", [])],
            "buttons": snap.get("buttons", []), "tabs": snap.get("tabs", []), "toolbar": snap.get("toolbar", []),
            "grids": [{"id": g["id"]} for g in snap.get("grids", [])],
            "tables": [{"id": t["id"], "columns": t.get("columns", [])} for t in snap.get("tables", [])],
            "notes": notes or prev.get("notes", ""), "seen": int(prev.get("seen", 0)) + 1, "updated": now_iso(),
        }
        self._file(key).write_text(json.dumps(entry, indent=1, ensure_ascii=False), encoding="utf-8")
        return entry

    def add_note(self, program_dynpro: str, note: str) -> None:
        e = self.get(program_dynpro)
        if e:
            e["notes"] = (e.get("notes", "") + "\n" + note).strip()[-3000:]
            self._file(program_dynpro).write_text(json.dumps(e, indent=1, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def dynamic_view(snap: dict[str, Any]) -> dict[str, Any]:
        """Token-saving view for a cached screen: only ids with values + status/popup."""
        out = {k: snap[k] for k in ("system", "client", "transaction", "program_dynpro", "title", "active_window",
                                     "status_bar") if k in snap}
        out["values"] = {f["id"]: f.get("value") for f in snap.get("fields", []) if f.get("value") not in ("", None)}
        out["grids"] = snap.get("grids", [])
        if "popup" in snap:
            out["popup"] = snap["popup"]
        out["cached_schema"] = True
        return out

    def list(self) -> list[dict[str, Any]]:
        out = []
        for f in sorted(self.folder.glob("*.json")):
            try:
                e = json.loads(f.read_text(encoding="utf-8"))
                out.append({"program_dynpro": e["program_dynpro"], "transaction": e.get("transaction"),
                            "title": e.get("title"), "seen": e.get("seen", 0)})
            except Exception:  # noqa: BLE001
                continue
        return out
