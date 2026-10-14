"""`sapbot` command line.

  sapbot init                       create C:\\SAPBot layout, default settings, first admin (current Windows user)
  sapbot probe [--system SUQ --client 400]   read-only check: SAP Logon, scripting, own session, date format
  sapbot workspace create|list|use|show|add-system|purge
  sapbot import-sapgate [--src C:\\Claude_CoWork]   copy the sap-gate scripts (+matching approvals)
  sapbot scripts list|approve
  sapbot user add|list|token        sapbot grant add|list|revoke      sapbot request list|approve|deny
  sapbot feature list|set           sapbot secret set <name>
  sapbot run "<task>" [--brain claude|local]   API mode (FSD 3.4 loop)
  sapbot batch --excel F --script S --map PARAM=Column ...
  sapbot mcp | mcp-register         sapbot cockpit                  sapbot export-training
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__, paths
from . import workspaces as wsmod
from .config import get_settings, write_default_settings
from .runtime import build, setup_logging
from .security.governance import FEATURES, Governance
from .security.store import Store


def out(obj: Any) -> None:
    sys.stdout.write((obj if isinstance(obj, str) else json.dumps(obj, indent=2, ensure_ascii=False, default=str))
                     + "\n")


def _gov() -> Governance:
    st = Store()
    st.bootstrap()
    return Governance(st)


def _require_admin(gov: Governance) -> None:
    if not gov.is_admin():
        raise SystemExit("This command needs the Admin role.")


# ---------------------------------------------------------------------------------------------------------------
def cmd_init(a: argparse.Namespace) -> None:
    home = paths.ensure_layout()
    f = write_default_settings()
    gov = _gov()
    out({"home": str(home), "settings": str(f), "user": gov.username(), "role": gov.role(), "version": __version__,
         "next": ["sapbot workspace create <id> --name ... --system SUQ/400/QA/write",
                  "sapbot import-sapgate", "sapbot probe", "sapbot mcp-register"]})


def cmd_probe(a: argparse.Namespace) -> None:
    from .sap.com import SapComError
    ex = build("cli")
    rep: dict[str, Any] = {"checks": []}

    def check(name: str, ok: bool, detail: Any = "") -> None:
        rep["checks"].append({"check": name, "ok": ok, "detail": detail})

    try:
        sessions = ex.bridge.list_sessions()
        check("SAP Logon running + client-side GUI scripting enabled", True, f"{len(sessions)} session(s)")
    except SapComError as e:
        check("SAP Logon running + client-side GUI scripting enabled", False, str(e))
        out(rep)
        return
    rep["sessions"] = [s.model_dump(include={"session_id", "system", "client", "user", "language", "transaction"})
                       for s in sessions]
    targets = [(a.system.upper(), str(a.client).zfill(3))] if a.system else sorted({(s.system, s.client)
                                                                                     for s in sessions})
    for sid, cl in targets:
        base = ex.bridge.find_session(sid, cl)
        if base is None:
            check(f"{sid}/{cl}: logged on", False, "log on in SAP Logon first")
            continue
        try:
            snap = ex.bridge.observe(base.session_id, include_values=False)
            check(f"{sid}/{cl}: read screen (attach)", True, f"{snap['transaction']} {snap['program_dynpro']}")
        except Exception as e:  # noqa: BLE001
            check(f"{sid}/{cl}: read screen (attach)", False, str(e))
            continue
        check(f"{sid}/{cl}: logon language", base.language.upper() in ("EN", "E"),
              f"{base.language or '?'} (firm standard EN)")
        try:
            own = ex._claim_session(sid, cl, use_existing=False)
            check(f"{sid}/{cl}: SAPBot own session", True, own)
        except Exception as e:  # noqa: BLE001
            check(f"{sid}/{cl}: SAPBot own session", False, str(e))
            continue
        try:
            fmt = ex.bridge.detect_date_format(own)
            if fmt:
                ex.gov.store.kv_set(f"datefmt:{sid}/{cl}:{ex.gov.username()}", fmt)
            check(f"{sid}/{cl}: user date format", fmt == "DD.MM.YYYY",
                  (fmt or "not detected") + ("" if fmt == "DD.MM.YYYY" else " - SAPBot converts DD.MM.YYYY input"))
        except Exception as e:  # noqa: BLE001
            check(f"{sid}/{cl}: user date format", False, str(e))
        ws = wsmod.find_workspace_for(sid, cl)
        check(f"{sid}/{cl}: belongs to a client workspace", ws is not None, ws.id if ws else
              "add it: sapbot workspace add-system <id> --system SID/CLIENT/QA")
    out(rep)


def cmd_workspace(a: argparse.Namespace) -> None:
    gov = _gov()
    if a.action == "list":
        out([{"id": w.id, "name": w.name, "policy": w.data_policy,
              "systems": [f"{s.sid}/{s.client}/{s.env}{'/write' if s.write else ''}" for s in w.systems]}
             for w in wsmod.list_all()])
        return
    if a.action == "use":
        ex = build("cli")
        out({"active": ex.set_workspace(a.id).id})
        return
    if a.action == "show":
        out(wsmod.load(a.id).model_dump())
        return
    _require_admin(gov)
    if a.action == "create":
        ws = wsmod.create(a.id, a.name or a.id, [_parse_sys(s) for s in (a.system or [])],
                          data_policy=a.policy, allowed_tcodes=[t.strip().upper() for t in (a.tcodes or "").split(",")
                                                                if t.strip()])
        gov.store.audit(gov.username(), ws.id, "WORKSPACE_CREATED", ws.model_dump())
        out({"created": ws.id, "folder": str(ws.root)})
    elif a.action == "add-system":
        ws = wsmod.load(a.id)
        for s in a.system or []:
            ws.systems.append(wsmod.SapSystem(**_parse_sys(s)))
        ws.save()
        out(ws.model_dump())
    elif a.action == "purge":
        if input(f"Type the workspace id '{a.id}' to delete ALL its local data: ").strip() != a.id:
            raise SystemExit("aborted")
        wsmod.purge(a.id)
        gov.store.audit(gov.username(), a.id, "WORKSPACE_PURGED", {})
        out({"purged": a.id})


def _parse_sys(s: str) -> dict[str, Any]:
    """SID/CLIENT/ENV[/write], e.g. SUQ/400/QA/write or SUP/300/PROD."""
    p = s.split("/")
    if len(p) < 2:
        raise SystemExit(f"bad system '{s}' - use SID/CLIENT/ENV[/write]")
    return {"sid": p[0], "client": p[1], "env": (p[2].upper() if len(p) > 2 else "QA"),
            "write": len(p) > 3 and p[3].lower() == "write"}


def cmd_import_sapgate(a: argparse.Namespace) -> None:
    gov = _gov()
    _require_admin(gov)
    from .scripts.library import ScriptLibrary
    src = Path(a.src)
    res = ScriptLibrary(gov.store).import_folder(src / "workspace" / "scripts",
                                                 src / "server" / "state" / "approvals.json", gov.username())
    out(res)


def cmd_scripts(a: argparse.Namespace) -> None:
    gov = _gov()
    from .scripts.library import ScriptLibrary
    lib = ScriptLibrary(gov.store)
    if a.action == "list":
        out([i.as_dict() for i in lib.list(a.folder)])
    elif a.action == "approve":
        _require_admin(gov)
        info = lib.info(a.script) if not a.promote_to else lib.promote(a.script, a.promote_to, gov.username(),
                                                                       a.allow_prod)
        if not a.promote_to:
            gov.store.approve_script(info.path, info.digest, gov.username(), a.allow_prod)
        out(lib.info(info.path).as_dict())


def cmd_user(a: argparse.Namespace) -> None:
    gov = _gov()
    if a.action == "list":
        out({"users": gov.store.list_users(), "members": gov.store.members()})
        return
    _require_admin(gov)
    if a.action == "add":
        gov.store.upsert_user(a.username, a.display or a.username, a.role)
        for w in a.workspace or []:
            gov.store.set_member(w, a.username, "lead" if a.role == "lead" else "consultant")
        gov.store.audit(gov.username(), "", "USER_UPSERT", {"user": a.username, "role": a.role})
        out(gov.store.get_user(a.username))
    elif a.action == "token":
        out({"user": a.username, "token": gov.store.issue_token(a.username),
             "note": "shown once - give it to the consultant for the Hub login"})


def cmd_grant(a: argparse.Namespace) -> None:
    gov = _gov()
    if a.action == "list":
        out(gov.store.active_grants())
        return
    if a.action == "revoke":
        _require_admin(gov)
        gov.store.revoke_grant(a.id, gov.username())
        out({"revoked": a.id})
        return
    if not gov.store.leads_workspace(gov.username(), a.workspace):
        raise SystemExit("Only an Admin or the workspace's Engagement Lead can grant.")
    sid, cl = a.system.split("/")
    gid = gov.store.add_grant(a.username, a.workspace, sid, cl, a.tcodes, a.minutes, a.reason, gov.username())
    out({"grant": gid})


def cmd_request(a: argparse.Namespace) -> None:
    gov = _gov()
    if a.action == "list":
        out(gov.store.requests(None if a.all else "pending"))
    else:
        out({"grant": gov.store.decide_request(a.id, a.action == "approve", gov.username())})


def cmd_feature(a: argparse.Namespace) -> None:
    gov = _gov()
    if a.action == "list":
        out(gov.all_features(a.workspace))
    else:
        if a.name not in FEATURES:
            raise SystemExit(f"unknown feature; choose from {FEATURES}")
        gov.set_feature(a.name, a.value.lower() in ("1", "on", "true", "yes"), a.workspace or "firm")
        out(gov.all_features(a.workspace))


def cmd_secret(a: argparse.Namespace) -> None:
    from .agent.llm import set_secret
    val = getpass.getpass(f"Value for {a.name} (input hidden): ")
    set_secret(a.name, val.strip())
    out({"stored": a.name, "where": "Windows Credential Manager (service 'sapbot')"})


def cmd_run(a: argparse.Namespace) -> None:
    from .agent.loop import AgentLoop
    ex = build("claude_api", approval_mode=a.approvals)
    if a.workspace:
        ex.set_workspace(a.workspace)

    def show(ev: dict[str, Any]) -> None:
        t = ev["type"]
        if t == "thought":
            out(f"\n[think] {ev['text'][:800]}")
        elif t == "tool_call":
            out(f"[tool ] {ev['name']} {json.dumps({k: v for k, v in ev['args'].items() if k != 'reasoning'})[:300]}")
        elif t == "tool_result":
            r = ev["result"]
            sb = r.get("status_bar") or {}
            out(f"[ok={r.get('ok')}] {sb.get('type', '')} {sb.get('text', '')} {r.get('error') or ''}"[:300])
        elif t in ("final", "error", "escalated"):
            out(f"\n[{t}] {json.dumps(ev, default=str)[:1500]}")
    res = AgentLoop(ex, on_event=show, force_brain=a.brain or "").run(a.task, a.system, a.client)
    out({"run_id": res.run_id, "outcome": res.outcome, "quality": res.quality, "turns": res.turns,
         "tokens": res.tokens, "brain": res.brain, "escalated": res.escalated})


def cmd_batch(a: argparse.Namespace) -> None:
    from .agent.batch import BatchSpec, run_batch
    ex = build("claude_api" if a.task_template else "cowork", approval_mode=a.approvals)
    if a.workspace:
        ex.set_workspace(a.workspace)
    pm = dict(m.split("=", 1) for m in (a.map or []))
    spec = BatchSpec(excel=a.excel, mode="task" if a.task_template else "script", script=a.script or "",
                     param_map=pm, task_template=a.task_template or "", system=a.system, client=a.client)
    out(run_batch(ex, spec, on_row=lambda i, r: out(f"row {i + 1}: {r['SAPBOT_STATUS']} {r['SAPBOT_MESSAGE']}")))


def cmd_mcp(a: argparse.Namespace) -> None:
    from .mcp_server import main as mcp_main
    mcp_main()


def cmd_mcp_register(a: argparse.Namespace) -> None:
    """Adds 'sapbot' to %APPDATA%\\Claude\\claude_desktop_config.json (backup kept), leaves other servers alone."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise SystemExit("APPDATA not set - run this on the Windows laptop")
    f = Path(appdata) / "Claude" / "claude_desktop_config.json"
    data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    if f.exists():
        shutil.copyfile(f, f.with_suffix(".json.sapbot-backup"))
    data.setdefault("mcpServers", {})["sapbot"] = {"command": sys.executable, "args": ["-m", "sapbot.mcp_server"],
                                                   "env": {"SAPBOT_HOME": str(paths.home())}}
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2), encoding="utf-8")
    out({"registered": str(f), "next": "Quit Claude Desktop from the tray icon and start it again."})


def cmd_cockpit(a: argparse.Namespace) -> None:
    ui = paths.package_dir() / "ui"
    subprocess.call([sys.executable, "-m", "streamlit", "run", str(ui / "app.py"), "--server.port", str(a.port),
                     "--server.address", "localhost", "--browser.gatherUsageStats", "false"],
                    cwd=str(ui))   # picks up ui/.streamlit/config.toml (Fiori theme)


def cmd_export_training(a: argparse.Namespace) -> None:
    from .learning.distill import export_training
    gov = _gov()
    _require_admin(gov)
    out(export_training(gov, a.workspace, Path(a.out) if a.out else None))


# ---------------------------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sapbot", description="SAPBot - SAP GUI agent (Logic-motive)")
    p.add_argument("--version", action="version", version=__version__)
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("init").set_defaults(fn=cmd_init)
    pr = sp.add_parser("probe")
    pr.add_argument("--system")
    pr.add_argument("--client", default="")
    pr.set_defaults(fn=cmd_probe)

    w = sp.add_parser("workspace")
    w.add_argument("action", choices=["create", "list", "use", "show", "add-system", "purge"])
    w.add_argument("id", nargs="?")
    w.add_argument("--name")
    w.add_argument("--system", action="append", help="SID/CLIENT/ENV[/write], repeatable")
    w.add_argument("--policy", default="full", choices=["full", "masked", "local-only"])
    w.add_argument("--tcodes", help="extra allowed T-codes, comma list")
    w.set_defaults(fn=cmd_workspace)

    im = sp.add_parser("import-sapgate")
    im.add_argument("--src", default=r"C:\Claude_CoWork")
    im.set_defaults(fn=cmd_import_sapgate)

    sc = sp.add_parser("scripts")
    sc.add_argument("action", choices=["list", "approve"])
    sc.add_argument("script", nargs="?")
    sc.add_argument("--folder")
    sc.add_argument("--promote-to", choices=["library", "custom"])
    sc.add_argument("--allow-prod", action="store_true")
    sc.set_defaults(fn=cmd_scripts)

    u = sp.add_parser("user")
    u.add_argument("action", choices=["add", "list", "token"])
    u.add_argument("username", nargs="?")
    u.add_argument("--display")
    u.add_argument("--role", default="consultant", choices=["admin", "lead", "consultant"])
    u.add_argument("--workspace", action="append")
    u.set_defaults(fn=cmd_user)

    g = sp.add_parser("grant")
    g.add_argument("action", choices=["add", "list", "revoke"])
    g.add_argument("--id", type=int)
    g.add_argument("--username")
    g.add_argument("--workspace")
    g.add_argument("--system", help="SID/CLIENT")
    g.add_argument("--tcodes", default="*")
    g.add_argument("--minutes", type=int, default=120)
    g.add_argument("--reason", default="")
    g.set_defaults(fn=cmd_grant)

    r = sp.add_parser("request")
    r.add_argument("action", choices=["list", "approve", "deny"])
    r.add_argument("id", nargs="?", type=int)
    r.add_argument("--all", action="store_true")
    r.set_defaults(fn=cmd_request)

    f = sp.add_parser("feature")
    f.add_argument("action", choices=["list", "set"])
    f.add_argument("name", nargs="?")
    f.add_argument("value", nargs="?")
    f.add_argument("--workspace")
    f.set_defaults(fn=cmd_feature)

    s = sp.add_parser("secret")
    s.add_argument("action", choices=["set"])
    s.add_argument("name", choices=["anthropic_api_key", "local_model_api_key", "hub_token"])
    s.set_defaults(fn=cmd_secret)

    ru = sp.add_parser("run")
    ru.add_argument("task")
    ru.add_argument("--workspace")
    ru.add_argument("--system")
    ru.add_argument("--client")
    ru.add_argument("--brain", choices=["claude", "local"])
    ru.add_argument("--approvals", choices=["dialog", "db", "deny"], default=None)
    ru.set_defaults(fn=cmd_run)

    b = sp.add_parser("batch")
    b.add_argument("--excel", required=True)
    b.add_argument("--script")
    b.add_argument("--map", action="append", help="PARAM=Column, repeatable")
    b.add_argument("--task-template")
    b.add_argument("--workspace")
    b.add_argument("--system")
    b.add_argument("--client")
    b.add_argument("--approvals", choices=["dialog", "db", "deny"], default=None)
    b.set_defaults(fn=cmd_batch)

    sp.add_parser("mcp").set_defaults(fn=cmd_mcp)
    sp.add_parser("mcp-register").set_defaults(fn=cmd_mcp_register)
    c = sp.add_parser("cockpit")
    c.add_argument("--port", type=int, default=8501)
    c.set_defaults(fn=cmd_cockpit)
    et = sp.add_parser("export-training")
    et.add_argument("--workspace")
    et.add_argument("--out")
    et.set_defaults(fn=cmd_export_training)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd != "mcp":
        setup_logging("cli")
    get_settings()
    args.fn(args)


if __name__ == "__main__":
    main()
