"""VBS script library, read-only scanner and runner (ADR A2/A3, tool #9 run_saved_script).

Folders under <SAPBOT_HOME>/scripts:
  library/  approved, parameterised scripts (firm-wide; synced from the Hub later)
  custom/   scripts written for a requirement (may be approved)
  drafts/   new scripts proposed by Claude - always need Admin approval before they can post

Approval is bound to the exact content (SHA-256) - edit => re-approval. The runner executes a snapshot of the
content that was checked, injected after the helper library (sapui.vbs) and AttachId of the bot's own session.
Concepts carried over from sap-gate (which stays untouched).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import paths
from ..config import get_settings
from ..security.store import Store

log = logging.getLogger(__name__)

PARAM_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,29}$")
PARAM_VAL = re.compile(r"^[A-Za-z0-9 _.,/\-:*%+#&()@]{0,200}$")
FOLDERS = ("library", "custom", "drafts")

NEVER_AUTO = [
    (r"WScript\.Shell|Shell\.Application|\.ShellExecute|\.Exec\s*\(|\.Run\s*[\( ]", "starts other programs"),
    (r"\bExecuteGlobal\b|\bExecute\s|\bEval\s*\(", "executes dynamic code"),
    (r"GetObject\s*\(", "attaches to objects directly (the runner attaches)"),
    (r"\bAttach(Id)?\b", "calls Attach (the runner attaches to the bot's session)"),
    (r"CreateObject\s*\(\s*\"(?!Scripting\.FileSystemObject|Scripting\.Dictionary|ADODB\.Stream)",
     "creates COM objects"),
    (r"DeleteFile|DeleteFolder|MoveFile|MoveFolder|CopyFile|CopyFolder", "manipulates files"),
    (r"okcd\"\)\.Text", "types into the command field directly (use Go)"),
    (r"btn\[11\]", "presses Save (btn[11])"),
    (r"SendVKey\s*\(?\s*11\b|\bKey\s+\(?\s*11\b", "sends Save (Ctrl+S)"),
    (r"SendVKey\s*\(?\s*(&H|[A-Za-z_(])|\bKey\s+\(?\s*(&H|[A-Za-z_])", "sends a computed / hex virtual key"),
    (r"StartTransaction|\bokcd\b", "starts transactions or uses the command field directly (use Go)"),
    (r"btn\[\d*\"\s*&|&\s*\"\d*\]", "builds a button id dynamically"),
    (r"\.Parent\b|\.Connections\b|\bGetScriptingEngine\b|\bses\[", "reaches other SAP sessions/windows"),
    (r"\.SelectContextMenuItem|\.PressToolbarButton|\.PressToolbarContextButton|\.DoubleClick|\.Select\b",
     "uses menu / grid toolbar functions"),
]
GO_CALL = re.compile(r"\bGo\b\s*\(?\s*\"([^\"]+)\"", re.I)
GO_DYNAMIC = re.compile(r"\bGo\b\s*\(?\s*[A-Za-z_]", re.I)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scan(text: str, display_tcodes: list[str] | None = None) -> dict[str, Any]:
    """Static read-only check - deliberately strict; anything unclear is NOT read-only."""
    display = {t.upper() for t in (display_tcodes if display_tcodes is not None else
                                    get_settings().sap.display_tcodes)}
    findings: list[str] = []
    code = "\n".join(_strip_comment(ln) for ln in text.splitlines())
    for pat, why in NEVER_AUTO:
        if re.search(pat, code, re.I):
            findings.append(why)
    tcodes = [re.sub(r"^/[nNoO]", "", t.strip()).upper() for t in GO_CALL.findall(code)]
    for t in tcodes:
        if t not in display:
            findings.append(f"uses transaction {t}, which is not on the display-only list")
    if GO_DYNAMIC.search(code):
        findings.append("calls Go with a computed transaction (cannot be checked)")
    if not tcodes and re.search(r"\.Press|SendVKey|\bKey\s+\d", code):
        findings.append("acts on the current screen without naming a display transaction")
    return {"read_only": not findings, "findings": sorted(set(findings)), "tcodes": sorted(set(tcodes))}


def _strip_comment(line: str) -> str:
    """Remove a VBScript comment (' or REM) but not an apostrophe inside a string literal."""
    out, in_str = [], False
    for ch in line:
        if ch == '"':
            in_str = not in_str
        elif ch == "'" and not in_str:
            break
        out.append(ch)
    s = "".join(out)
    return "" if re.match(r"^\s*REM\b", s, re.I) else s


def header(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    last = ""
    for ln in text.splitlines()[:20]:
        s = ln.strip()
        if not s.startswith("'"):
            continue
        body = s.lstrip("' ").strip()
        m = re.match(r"^(PURPOSE|PARAMS|RESULT)\s*:\s*(.*)$", body, re.I)
        if m:
            last = m.group(1).upper()
            out[last] = m.group(2).strip()
        elif last and body:
            out[last] += " " + body
    return out


@dataclass
class ScriptInfo:
    path: str          # e.g. "library/pp_cor3_order_overview.vbs"
    digest: str
    header: dict[str, str]
    scan: dict[str, Any]
    approval: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {"script": self.path, "purpose": self.header.get("PURPOSE", ""),
                "params": self.header.get("PARAMS", ""), "result": self.header.get("RESULT", ""),
                "read_only": self.scan["read_only"], "findings": self.scan["findings"], "tcodes": self.scan["tcodes"],
                "approved": bool(self.approval), "approved_for_production": bool(self.approval and
                                                                                 self.approval.get("allow_prod")),
                "content_hash": self.digest[:12]}


class ScriptLibrary:
    def __init__(self, store: Store, root: Path | None = None) -> None:
        self.store = store
        self.root = Path(root or paths.scripts_dir())
        for f in FOLDERS:
            (self.root / f).mkdir(parents=True, exist_ok=True)

    def resolve(self, rel: str) -> Path:
        p = (self.root / rel.replace("\\", "/")).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("script must be inside the SAPBot scripts folder")
        if p.suffix.lower() != ".vbs":
            raise ValueError("only .vbs scripts can be run")
        if not p.exists():
            raise FileNotFoundError(f"{rel} not found (use list_saved_scripts)")
        return p

    def rel(self, p: Path) -> str:
        return str(p.resolve().relative_to(self.root.resolve())).replace("\\", "/")

    def info(self, rel: str) -> ScriptInfo:
        p = self.resolve(rel)
        text = p.read_text(encoding="utf-8", errors="replace")
        d = sha256_text(text)
        r = self.rel(p)
        return ScriptInfo(r, d, header(text), scan(text), self.store.script_approval(r, d))

    def list(self, folder: str | None = None) -> list[ScriptInfo]:
        out = []
        for f in ([folder] if folder else list(FOLDERS)):
            for p in sorted((self.root / f).glob("*.vbs")):
                try:
                    out.append(self.info(self.rel(p)))
                except Exception as e:  # noqa: BLE001
                    log.warning("cannot read %s: %s", p, e)
        return out

    def save_draft(self, name: str, content: str, author: str) -> ScriptInfo:
        if not re.match(r"^[a-z0-9_\-]{3,60}$", name):
            raise ValueError("draft name: lowercase letters, digits, _ or - (3-60 chars), without .vbs")
        h = header(content)
        if "PURPOSE" not in h or "PARAMS" not in h:
            raise ValueError("script needs header comments ' PURPOSE: ..., ' PARAMS: ..., ' RESULT: ...")
        p = self.root / "drafts" / f"{name}.vbs"
        p.write_text(content, encoding="utf-8")
        self.store.audit(author, "", "SCRIPT_DRAFT_SAVED", {"path": self.rel(p), "digest": sha256_text(content)[:12]})
        return self.info(self.rel(p))

    def promote(self, rel: str, folder: str, by: str, allow_prod: bool = False) -> ScriptInfo:
        """Admin action: approve the current content and move a draft into library/custom."""
        if folder not in ("library", "custom"):
            raise ValueError("folder must be library or custom")
        src = self.resolve(rel)
        dst = self.root / folder / src.name
        if src != dst:
            shutil.move(str(src), str(dst))
        info = self.info(self.rel(dst))
        self.store.approve_script(info.path, info.digest, by, allow_prod)
        return self.info(info.path)

    def import_folder(self, src_root: Path, approvals_file: Path | None = None, by: str = "import") -> dict[str, Any]:
        """One-time COPY import of sap-gate scripts (ADR A3). Approvals carry over only for identical content."""
        imported, carried = [], []
        appr = {}
        if approvals_file and approvals_file.exists():
            appr = json.loads(approvals_file.read_text(encoding="utf-8"))
        for folder in ("library", "custom"):
            for p in sorted((src_root / folder).glob("*.vbs")):
                dst = self.root / folder / p.name
                if not dst.exists():
                    shutil.copyfile(p, dst)
                    imported.append(f"{folder}/{p.name}")
                text = dst.read_text(encoding="utf-8", errors="replace")
                d = sha256_text(text)
                entry = appr.get(f"{folder}/{p.name}", {}).get(d)
                if entry and not self.store.script_approval(f"{folder}/{p.name}", d):
                    self.store.approve_script(f"{folder}/{p.name}", d, f"{by} (sap-gate approval "
                                              f"{entry.get('approved_at', '')})", bool(entry.get("allow_non_qa")))
                    carried.append(f"{folder}/{p.name}")
        return {"imported": imported, "approvals_carried_over": carried}


RunFn = Callable[[list[str], int], tuple[int, str, str]]


def _subprocess_run(args: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        cp = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return cp.returncode, cp.stdout, cp.stderr
    except subprocess.TimeoutExpired as e:
        return -1, (e.stdout or "") if isinstance(e.stdout, str) else "", f"timeout after {timeout} s"
    except FileNotFoundError:
        return -2, "", "cscript not found - VBS scripts run on Windows only"


class ScriptRunner:
    def __init__(self, library: ScriptLibrary, run_fn: RunFn | None = None) -> None:
        self.lib = library
        self.run_fn = run_fn or _subprocess_run
        self.runs_dir = paths.data_dir() / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate_params(params: dict[str, Any] | None) -> dict[str, str]:
        out = {}
        for k, v in (params or {}).items():
            if not PARAM_KEY.match(str(k)):
                raise ValueError(f"parameter name '{k}' must be UPPER_CASE letters/digits/_")
            if not PARAM_VAL.match(str(v)):
                raise ValueError(f"parameter '{k}' contains characters that are not allowed")
            out[str(k)] = str(v)
        return out

    def compose(self, content: str, session_id: str, out_dir: Path) -> str:
        lib = (paths.package_dir() / "scripts" / "vbs" / "sapui.vbs").read_text(encoding="utf-8")
        return ("' ==== composed by SAPBot - do not edit ====\n" + lib + "\n"
                "Dim P: Set P=CreateObject(\"Scripting.Dictionary\")\n"
                "Dim gi_, ga_, gp_\n"
                "For gi_=0 To WScript.Arguments.Count-1\n"
                "  ga_=WScript.Arguments(gi_): gp_=InStr(ga_,\"=\")\n"
                "  If gp_>1 Then P(UCase(Left(ga_,gp_-1)))=Mid(ga_,gp_+1)\n"
                "Next\n"
                f"Dim OUT_DIR: OUT_DIR=\"{out_dir}\"\n"
                f"AttachId \"{session_id}\"\n"
                "' ==== script ====\n" + content)

    def run(self, info: ScriptInfo, content: str, params: dict[str, str], session_id: str, out_dir: Path,
            timeout: int | None = None) -> dict[str, Any]:
        if sha256_text(content) != info.digest:
            raise ValueError("script content changed after it was checked - run again")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_file = self.runs_dir / f"{stamp}_{Path(info.path).stem}.vbs"
        run_file.write_text(self.compose(content, session_id, out_dir), encoding="ascii", errors="replace")
        args = ["cscript", "//nologo", str(run_file)] + [f"{k}={v}" for k, v in params.items()]
        rc, out, err = self.run_fn(args, int(timeout or get_settings().sap.run_timeout_seconds))
        results: dict[str, list[str]] = {}
        for ln in out.splitlines():
            if ln.startswith("RESULT|"):
                parts = ln.split("|", 2)
                if len(parts) == 3:
                    results.setdefault(parts[1], []).append(parts[2])
        flat = {k: (v[0] if len(v) == 1 else v) for k, v in results.items()}
        missing = [ln.split("|", 1)[1] for ln in out.splitlines() if ln.startswith("MISSING_PARAM|")]
        return {"return_code": rc, "results": flat, "missing_params": missing, "stdout_tail": out[-6000:],
                "stderr": err[-2000:], "run_file": str(run_file)}
