"""Local governance store (SQLite): users, roles, grants, elevation requests, approvals, script approvals,
feature flags, audit log. Used directly in laptop mode (ADR v1 pilot) and as the offline cache in Hub mode.

Thread-safe: one connection per call (SQLite file, WAL mode).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import getpass
import hashlib
import json
import logging
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .. import paths
from ..types import now_iso

log = logging.getLogger(__name__)

ROLES = ("admin", "lead", "consultant")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY, display_name TEXT, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
  token_hash TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS workspace_members(
  workspace TEXT NOT NULL, username TEXT NOT NULL, ws_role TEXT NOT NULL DEFAULT 'consultant',
  PRIMARY KEY(workspace, username));
CREATE TABLE IF NOT EXISTS grants(
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, workspace TEXT NOT NULL, system TEXT NOT NULL,
  client TEXT NOT NULL, tcodes TEXT NOT NULL DEFAULT '*', reason TEXT, granted_by TEXT, valid_from TEXT,
  valid_to TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, workspace TEXT NOT NULL, system TEXT NOT NULL,
  client TEXT NOT NULL, tcodes TEXT NOT NULL DEFAULT '*', minutes INTEGER NOT NULL, reason TEXT,
  status TEXT NOT NULL DEFAULT 'pending', decided_by TEXT, decided_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, kind TEXT, username TEXT, workspace TEXT, system TEXT,
  client TEXT, summary TEXT, details TEXT, status TEXT NOT NULL DEFAULT 'pending', decided_by TEXT, decided_at TEXT,
  run_id TEXT);
CREATE TABLE IF NOT EXISTS script_approvals(
  path TEXT NOT NULL, digest TEXT NOT NULL, approved_by TEXT, approved_at TEXT, allow_prod INTEGER NOT NULL DEFAULT 0,
  note TEXT, PRIMARY KEY(path, digest));
CREATE TABLE IF NOT EXISTS flags(scope TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL,
  PRIMARY KEY(scope, name));
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, username TEXT, workspace TEXT, event TEXT, detail TEXT, run_id TEXT,
  uploaded INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


def current_os_user() -> str:
    """One laptop = one consultant (ADR A12): the Windows user name identifies the consultant locally."""
    try:
        return getpass.getuser().lower()
    except Exception:  # noqa: BLE001
        return "unknown"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path or paths.db_file())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            yield con
            con.commit()
        finally:
            con.close()

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql: str, args: tuple = ()) -> int:
        with self._conn() as c:
            cur = c.execute(sql, args)
            return int(cur.lastrowid or 0)

    # ---------------------------------------------------------------- users
    def bootstrap(self) -> dict[str, Any]:
        """First run on a laptop: the current Windows user becomes admin if no user exists yet."""
        from ..config import machine_locked
        if machine_locked():          # IT-managed laptop: roles come only from the Hub
            return self.get_user(current_os_user()) or {}
        if not self._one("SELECT username FROM users LIMIT 1"):
            u = current_os_user()
            self.upsert_user(u, u, "admin")
            self.audit(u, "", "BOOTSTRAP_ADMIN", {"user": u})
        return self.get_user(current_os_user()) or {}

    def upsert_user(self, username: str, display_name: str, role: str, active: bool = True) -> None:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        self._exec("INSERT INTO users(username, display_name, role, active, created_at) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(username) DO UPDATE SET display_name=excluded.display_name, role=excluded.role, "
                   "active=excluded.active", (username.lower(), display_name, role, int(active), now_iso()))

    def issue_token(self, username: str) -> str:
        token = "sbt_" + secrets.token_urlsafe(32)
        self._exec("UPDATE users SET token_hash=? WHERE username=?", (hash_token(token), username.lower()))
        return token

    def user_by_token(self, token: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE token_hash=? AND active=1", (hash_token(token),))

    def get_user(self, username: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE username=?", (username.lower(),))

    def list_users(self) -> list[dict[str, Any]]:
        return self._all("SELECT username, display_name, role, active, created_at FROM users ORDER BY username")

    def set_member(self, workspace: str, username: str, ws_role: str = "consultant") -> None:
        self._exec("INSERT OR REPLACE INTO workspace_members VALUES(?,?,?)", (workspace, username.lower(), ws_role))

    def remove_member(self, workspace: str, username: str) -> None:
        self._exec("DELETE FROM workspace_members WHERE workspace=? AND username=?", (workspace, username.lower()))

    def members(self, workspace: str | None = None) -> list[dict[str, Any]]:
        if workspace:
            return self._all("SELECT * FROM workspace_members WHERE workspace=?", (workspace,))
        return self._all("SELECT * FROM workspace_members")

    def user_workspaces(self, username: str) -> list[str]:
        u = self.get_user(username)
        rows = self._all("SELECT workspace FROM workspace_members WHERE username=?", (username.lower(),))
        return [r["workspace"] for r in rows] if not (u and u["role"] == "admin") else ["*"]

    def leads_workspace(self, username: str, workspace: str) -> bool:
        u = self.get_user(username)
        if u and u["role"] == "admin":
            return True
        return bool(self._one("SELECT 1 AS x FROM workspace_members WHERE workspace=? AND username=? AND ws_role='lead'",
                              (workspace, username.lower())))

    # ---------------------------------------------------------------- grants & requests (A14)
    def add_grant(self, username: str, workspace: str, system: str, client: str, tcodes: str, minutes: int,
                  reason: str, granted_by: str) -> int:
        now = dt.datetime.now()
        gid = self._exec("INSERT INTO grants(username, workspace, system, client, tcodes, reason, granted_by, "
                         "valid_from, valid_to) VALUES(?,?,?,?,?,?,?,?,?)",
                         (username.lower(), workspace, system.upper(), str(client), _norm_tcodes(tcodes), reason,
                          granted_by, now.isoformat(timespec="seconds"),
                          (now + dt.timedelta(minutes=int(minutes))).isoformat(timespec="seconds")))
        self.audit(granted_by, workspace, "GRANT_ADDED", {"id": gid, "user": username, "system": system,
                                                          "client": client, "tcodes": tcodes, "minutes": minutes,
                                                          "reason": reason})
        return gid

    def revoke_grant(self, gid: int, by: str) -> None:
        self._exec("UPDATE grants SET revoked=1 WHERE id=?", (gid,))
        self.audit(by, "", "GRANT_REVOKED", {"id": gid})

    def active_grants(self, username: str | None = None) -> list[dict[str, Any]]:
        now = dt.datetime.now().isoformat(timespec="seconds")
        if username:
            return self._all("SELECT * FROM grants WHERE revoked=0 AND valid_to>? AND username=? ORDER BY valid_to",
                             (now, username.lower()))
        return self._all("SELECT * FROM grants WHERE revoked=0 AND valid_to>? ORDER BY valid_to", (now,))

    def add_request(self, username: str, workspace: str, system: str, client: str, tcodes: str, minutes: int,
                    reason: str) -> int:
        rid = self._exec("INSERT INTO requests(username, workspace, system, client, tcodes, minutes, reason, "
                         "created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (username.lower(), workspace, system.upper(), str(client), _norm_tcodes(tcodes),
                          int(minutes), reason, now_iso()))
        self.audit(username, workspace, "ELEVATION_REQUESTED", {"id": rid, "system": system, "tcodes": tcodes,
                                                                "minutes": minutes, "reason": reason})
        return rid

    def requests(self, status: str | None = "pending") -> list[dict[str, Any]]:
        if status:
            return self._all("SELECT * FROM requests WHERE status=? ORDER BY id DESC", (status,))
        return self._all("SELECT * FROM requests ORDER BY id DESC LIMIT 200")

    def decide_request(self, rid: int, approve: bool, by: str) -> int | None:
        r = self._one("SELECT * FROM requests WHERE id=?", (rid,))
        if not r or r["status"] != "pending":
            return None
        if not self.leads_workspace(by, r["workspace"]):
            raise PermissionError("only an Admin or the workspace's Engagement Lead can decide this request")
        me = self.get_user(by) or {}
        if r["username"] == by.lower() and me.get("role") != "admin":
            raise PermissionError("four-eyes rule: you cannot approve your own request")
        self._exec("UPDATE requests SET status=?, decided_by=?, decided_at=? WHERE id=?",
                   ("approved" if approve else "denied", by, now_iso(), rid))
        self.audit(by, r["workspace"], "ELEVATION_" + ("APPROVED" if approve else "DENIED"), {"request": rid})
        if approve:
            return self.add_grant(r["username"], r["workspace"], r["system"], r["client"], r["tcodes"], r["minutes"],
                                  r["reason"] or "", by)
        return None

    # ---------------------------------------------------------------- per-action approvals
    def add_approval(self, kind: str, username: str, workspace: str, system: str, client: str, summary: str,
                     details: dict[str, Any], run_id: str = "") -> int:
        return self._exec("INSERT INTO approvals(created_at, kind, username, workspace, system, client, summary, "
                          "details, run_id) VALUES(?,?,?,?,?,?,?,?,?)",
                          (now_iso(), kind, username, workspace, system, client, summary,
                           json.dumps(details, default=str), run_id))

    def approval(self, aid: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM approvals WHERE id=?", (aid,))

    def pending_approvals(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM approvals WHERE status='pending' ORDER BY id")

    def decide_approval(self, aid: int, approve: bool, by: str, edited_values: dict[str, Any] | None = None) -> None:
        if edited_values:
            row = self.approval(aid) or {}
            details = json.loads(row.get("details") or "{}")
            details["edited_values"] = edited_values
            self._exec("UPDATE approvals SET details=? WHERE id=?", (json.dumps(details, default=str), aid))
        self._exec("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=? AND status='pending'",
                   ("approved" if approve else "rejected", by, now_iso(), aid))

    # ---------------------------------------------------------------- script approvals (A3)
    def approve_script(self, path: str, digest: str, by: str, allow_prod: bool = False, note: str = "") -> None:
        self._exec("INSERT OR REPLACE INTO script_approvals VALUES(?,?,?,?,?,?)",
                   (path, digest, by, now_iso(), int(allow_prod), note))
        self.audit(by, "", "SCRIPT_APPROVED", {"path": path, "digest": digest[:12], "allow_prod": allow_prod})

    def revoke_script(self, path: str, digest: str, by: str) -> None:
        self._exec("DELETE FROM script_approvals WHERE path=? AND digest=?", (path, digest))
        self.audit(by, "", "SCRIPT_REVOKED", {"path": path, "digest": digest[:12]})

    def script_approval(self, path: str, digest: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM script_approvals WHERE path=? AND digest=?", (path, digest))

    def script_approvals(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM script_approvals ORDER BY path")

    # ---------------------------------------------------------------- feature flags (E1)
    def set_flag(self, name: str, value: bool, scope: str = "firm", by: str = "") -> None:
        self._exec("INSERT OR REPLACE INTO flags VALUES(?,?,?)", (scope, name, int(value)))
        self.audit(by, "" if scope == "firm" else scope, "FEATURE_SET", {"name": name, "value": value,
                                                                        "scope": scope})

    def flags(self, scope: str = "firm") -> dict[str, bool]:
        return {r["name"]: bool(r["value"]) for r in self._all("SELECT * FROM flags WHERE scope=?", (scope,))}

    # ---------------------------------------------------------------- audit + kv
    def audit(self, username: str, workspace: str, event: str, detail: dict[str, Any] | None = None,
              run_id: str = "") -> None:
        self._exec("INSERT INTO audit(ts, username, workspace, event, detail, run_id) VALUES(?,?,?,?,?,?)",
                   (now_iso(), username, workspace, event, json.dumps(detail or {}, default=str), run_id))

    def audit_rows(self, limit: int = 500, workspace: str | None = None, event: str | None = None,
                   only_unuploaded: bool = False) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM audit WHERE 1=1", []
        if workspace:
            sql += " AND workspace=?"
            args.append(workspace)
        if event:
            sql += " AND event LIKE ?"
            args.append(f"%{event}%")
        if only_unuploaded:
            sql += " AND uploaded=0"
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return self._all(sql, tuple(args))

    def mark_uploaded(self, ids: list[int]) -> None:
        with self._conn() as c:
            c.executemany("UPDATE audit SET uploaded=1 WHERE id=?", [(i,) for i in ids])

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else default

    def kv_set(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, value))


def _norm_tcodes(tcodes: str) -> str:
    t = ",".join(x.strip().upper() for x in (tcodes or "*").replace(";", ",").split(",") if x.strip())
    return t or "*"
