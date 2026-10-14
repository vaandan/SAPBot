"""SAPBot MCP server for Claude Cowork / Claude Desktop (ADR E2, E3).

Registered by the installer in %APPDATA%\\Claude\\claude_desktop_config.json:
  "sapbot": {"command": "C:\\\\SAPBot\\\\.venv\\\\Scripts\\\\python.exe", "args": ["-m", "sapbot.mcp_server"]}
Runs on the consultant's Windows PC (SAP GUI scripting is local). Same tools, same policy as API mode.
Write approvals pop up as a Windows dialog on this PC (settings.approvals.mode = dialog).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .runtime import build, setup_logging
from .tools.schemas import all_tools

log = logging.getLogger("sapbot.mcp")

INSTRUCTIONS = (
    "SAPBot drives SAP GUI for the consultant through a safety policy. Workflow: 1) sapbot_status (choose the "
    "client workspace if none), 2) task_begin with the user's request (SAPBot opens its own SAP session and returns "
    "similar past successful runs), 3) sap_observe_screen / sap_act / sap_read_grid / run_saved_script - always with "
    "a short 'reasoning', 4) for postings read the document back in a display transaction, 5) task_end. "
    "Never try to bypass a refusal: tell the user what is needed (approval, elevation grant, workspace setting)."
)


def _mcp_server_class() -> Any:
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2
        return MCPServer
    except ImportError:  # pragma: no cover
        from mcp.server.fastmcp import FastMCP  # mcp 1.x
        return FastMCP


def create_server() -> Any:
    setup_logging("mcp", to_stderr=True)
    ex = build("cowork")
    Server = _mcp_server_class()
    mcp = Server("sapbot", instructions=INSTRUCTIONS)

    def _call(name: str, args: dict[str, Any]) -> str:
        res = ex.execute(name, {k: v for k, v in args.items() if v is not None})
        return json.dumps(res.model_dump(exclude_none=True), ensure_ascii=False, default=str)[:180_000]

    # Tools are registered with explicit signatures so the client sees proper schemas.
    @mcp.tool(description=_desc("sapbot_status"))
    def sapbot_status(set_workspace: str | None = None) -> str:
        return _call("sapbot_status", {"set_workspace": set_workspace})

    @mcp.tool(description=_desc("task_begin"))
    def task_begin(task: str, system: str | None = None, client: str | None = None,
                   use_existing_session: bool = False, expected_output: str | None = None) -> str:
        return _call("task_begin", {"task": task, "system": system, "client": client,
                                    "use_existing_session": use_existing_session, "expected_output": expected_output})

    @mcp.tool(description=_desc("task_end"))
    def task_end(outcome: str, summary: str) -> str:
        return _call("task_end", {"outcome": outcome, "summary": summary})

    @mcp.tool(description=_desc("sap_observe_screen"))
    def sap_observe_screen(reasoning: str, force_refresh: bool = False) -> str:
        return _call("sap_observe_screen", {"reasoning": reasoning, "force_refresh": force_refresh})

    @mcp.tool(description=_desc("sap_act"))
    def sap_act(reasoning: str, ok_code: str | None = None, field_updates_json: str | None = None,
                click_id: str | None = None, vkey: int | None = None) -> str:
        return _call("sap_act", {"reasoning": reasoning, "ok_code": ok_code, "field_updates_json": field_updates_json,
                                 "click_id": click_id, "vkey": vkey})

    @mcp.tool(description=_desc("sap_read_grid"))
    def sap_read_grid(reasoning: str, grid_id: str | None = None, max_rows: int = 50) -> str:
        return _call("sap_read_grid", {"reasoning": reasoning, "grid_id": grid_id, "max_rows": max_rows})

    @mcp.tool(description=_desc("run_saved_script"))
    def run_saved_script(reasoning: str, script: str, params_json: str | None = None) -> str:
        return _call("run_saved_script", {"reasoning": reasoning, "script": script, "params_json": params_json})

    @mcp.tool(description=_desc("list_saved_scripts"))
    def list_saved_scripts(folder: str | None = None) -> str:
        return _call("list_saved_scripts", {"folder": folder})

    @mcp.tool(description=_desc("save_script_draft"))
    def save_script_draft(name: str, content: str, reasoning: str) -> str:
        return _call("save_script_draft", {"name": name, "content": content, "reasoning": reasoning})

    @mcp.tool(description=_desc("local_file_workspace"))
    def local_file_workspace(reasoning: str, mode: str, file_path: str, content_json: str | None = None) -> str:
        return _call("local_file_workspace", {"reasoning": reasoning, "mode": mode, "file_path": file_path,
                                              "content_json": content_json})

    @mcp.tool(description=_desc("action_schema_memory"))
    def action_schema_memory(reasoning: str, program_dynpro: str, note: str | None = None) -> str:
        return _call("action_schema_memory", {"reasoning": reasoning, "program_dynpro": program_dynpro, "note": note})

    @mcp.tool(description=_desc("rag_search_trajectories"))
    def rag_search_trajectories(reasoning: str, query: str, top_k: int = 3, filter_t_code: str | None = None) -> str:
        return _call("rag_search_trajectories", {"reasoning": reasoning, "query": query, "top_k": top_k,
                                                 "filter_t_code": filter_t_code})

    @mcp.tool(description=_desc("request_elevation"))
    def request_elevation(system: str, client: str, tcodes: str, minutes: int, reason: str) -> str:
        return _call("request_elevation", {"system": system, "client": client, "tcodes": tcodes,
                                           "minutes": minutes, "reason": reason})

    @mcp.tool(description=_desc("fiori_observe_dom"))
    def fiori_observe_dom(reasoning: str) -> str:
        return _call("fiori_observe_dom", {"reasoning": reasoning})

    @mcp.tool(description=_desc("fiori_act"))
    def fiori_act(reasoning: str, action: str, target: str, value: str | None = None) -> str:
        return _call("fiori_act", {"reasoning": reasoning, "action": action, "target": target, "value": value})

    mcp._sapbot_executor = ex  # for tests
    return mcp


def _desc(name: str) -> str:
    for t in all_tools():
        if t["name"] == name:
            return t["description"]
    return name


def main() -> None:
    server = create_server()
    server.run("stdio")


if __name__ == "__main__":
    main()
