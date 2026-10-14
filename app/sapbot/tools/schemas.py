"""Unified tool registry (FSD §4 + ADR A2/B1). Identical JSON schemas for Claude (Cowork via MCP, API mode) and the
local model. Every SAP tool carries a short required `reasoning` argument (ADR B1) so trajectories contain the
model's thought even when SAPBot cannot see it (Cowork mode).

FSD tools 1-8 are unchanged apart from `reasoning`; tool 9 `run_saved_script` is the ADR A2 addition.
Harness tools (sapbot_status, task_begin, task_end, list_saved_scripts, save_script_draft, request_elevation) manage
the run and governance; they never touch SAP.
"""
from __future__ import annotations

from typing import Any

REASONING = {"type": "string", "description": "One or two sentences: why you take this step (logged for learning)."}


def _with_reasoning(params: dict[str, Any]) -> dict[str, Any]:
    p = {"type": "object", "properties": dict(params.get("properties", {})), "required": list(params.get("required", []))}
    p["properties"]["reasoning"] = REASONING
    if "reasoning" not in p["required"]:
        p["required"].append("reasoning")
    return p


FSD_TOOLS: list[dict[str, Any]] = [
    {"name": "sap_observe_screen",
     "description": "Inspects the active SAP GUI window/popup of SAPBot's own session. Returns transaction, "
                    "program/dynpro, editable field IDs with labels and values, buttons, tabs, grids, toolbar and the "
                    "status bar (type S/W/E/A/I + message class/number).",
     "parameters": {"type": "object", "properties": {
         "force_refresh": {"type": "boolean", "description": "Bypass memory cache and re-crawl all components"}}}},
    {"name": "sap_act",
     "description": "Executes actions in SAP GUI in this order: ok_code, field updates, click, virtual key. Send "
                    "navigation (/nTCODE) on its own. Dates in the firm standard DD.MM.YYYY are converted to the "
                    "user's format automatically. Saving/posting and restricted transactions are checked by the "
                    "SAPBot policy and may need human approval or an elevation grant.",
     "parameters": {"type": "object", "properties": {
         "ok_code": {"type": "string", "description": "T-code or command for the OK-Code box, e.g. /nVA01, /nSE16N"},
         "field_updates_json": {"type": "string", "description": "JSON string mapping field IDs to values, e.g. "
                                "{\"wnd[0]/usr/ctxtVBAK-AUART\": \"OR\"}"},
         "click_id": {"type": "string", "description": "ID of GuiButton or GuiTab to press"},
         "vkey": {"type": "integer", "description": "Virtual Key: 0=Enter, 3=Back, 8=Execute/F8, 11=Save/Ctrl+S, "
                                                    "12=Cancel"}}}},
    {"name": "sap_read_grid",
     "description": "Reads data rows from an SAP ALV Grid or Table Control into structured JSON.",
     "parameters": {"type": "object", "properties": {
         "grid_id": {"type": "string", "description": "COM ID of GuiGridView or GuiTableControl (optional if only one "
                                                      "exists)"},
         "max_rows": {"type": "integer", "description": "Maximum number of rows to retrieve (default: 50)"}}}},
    {"name": "fiori_observe_dom",
     "description": "Inspects active SAP Fiori / WebDynpro browser tab (planned v1.1 - disabled unless the "
                    "'fiori' feature is on).",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "fiori_act",
     "description": "Executes actions in SAP Fiori / WebDynpro browser tab (planned v1.1).",
     "parameters": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["click", "fill", "upload_file", "press_key", "goto_url"]},
         "target": {"type": "string", "description": "CSS selector, element text, or file path (for uploads)"},
         "value": {"type": "string", "description": "Value to input (for fill or press_key)"}},
         "required": ["action", "target"]}},
    {"name": "local_file_workspace",
     "description": "Reads or writes local Excel (.xlsx), CSV, TXT, or JSON files to feed SAP or save SAP exports. "
                    "Writes go to the client workspace's output folder; reads are allowed from the workspace and "
                    "the user's Documents/Downloads/Desktop.",
     "parameters": {"type": "object", "properties": {
         "mode": {"type": "string", "enum": ["read_excel_or_csv", "write_csv", "write_excel", "read_text",
                                             "write_text"]},
         "file_path": {"type": "string", "description": "Absolute path (reads) or file name/absolute path inside "
                                                        "the workspace output folder (writes)"},
         "content_json": {"type": "string", "description": "JSON payload to write (for write modes): a list of row "
                                                           "objects for csv/excel, or text"}},
         "required": ["mode", "file_path"]}},
    {"name": "action_schema_memory",
     "description": "Retrieves cached field definitions, labels and notes for a SAP Dynpro / Program in this client's "
                    "system. Use program_dynpro 'LIST' to list known screens.",
     "parameters": {"type": "object", "properties": {
         "program_dynpro": {"type": "string", "description": "Key in format PROGRAM_DYNPRONUMBER, e.g. "
                                                             "SAPLSE16N_0100"},
         "note": {"type": "string", "description": "Optional: add a learned note about this screen"}},
         "required": ["program_dynpro"]}},
    {"name": "rag_search_trajectories",
     "description": "Searches this client's local vector store for the most similar past SUCCESSFUL SAP task "
                    "trajectories. Use it first for every new task, and when approaching an unfamiliar screen.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Natural language description of the current task or challenge"},
         "top_k": {"type": "integer", "description": "Number of similar trajectories (default: 3, max: 5)"},
         "filter_t_code": {"type": "string", "description": "Optional: only trajectories involving this T-code"}},
         "required": ["query"]}},
    {"name": "run_saved_script",
     "description": "Runs an approved/parameterised SAP GUI VBS script from the SAPBot library (see "
                    "list_saved_scripts) in SAPBot's own session. Read-only scripts run at once; posting scripts "
                    "follow the same policy as saving (write mode / grant / approval).",
     "parameters": {"type": "object", "properties": {
         "script": {"type": "string", "description": "e.g. library/pp_cor3_order_overview.vbs"},
         "params_json": {"type": "string", "description": "JSON object of UPPER_CASE parameters, e.g. "
                                                         "{\"AUFNR\": \"1000000993\"}"}},
         "required": ["script"]}},
]

HARNESS_TOOLS: list[dict[str, Any]] = [
    {"name": "sapbot_status",
     "description": "Shows the user, role, active client workspace and its SAP systems (QA/PROD, write mode), open "
                    "SAP sessions, active elevation grants, enabled features and the current task. Call it first.",
     "parameters": {"type": "object", "properties": {
         "set_workspace": {"type": "string", "description": "Optional: switch the active client workspace (id)"}}}},
    {"name": "task_begin",
     "description": "Starts a logged task (required before SAP actions; one task per user request). SAPBot opens "
                    "its own SAP session on the given system so the consultant keeps working in theirs. Returns "
                    "similar past successful runs as examples.",
     "parameters": {"type": "object", "properties": {
         "task": {"type": "string", "description": "The user's request in one or two sentences"},
         "system": {"type": "string", "description": "SID, e.g. SUQ (default: first system of the workspace with "
                                                     "an open logon)"},
         "client": {"type": "string", "description": "Client, e.g. 400"},
         "use_existing_session": {"type": "boolean", "description": "Only if the user explicitly agreed: use an "
                                  "existing session when no new session can be opened (session limit)"},
         "expected_output": {"type": "string", "description": "Optional file the task must produce"}},
         "required": ["task"]}},
    {"name": "task_end",
     "description": "Finishes the current task. For postings, first read the created document back in a display "
                    "transaction (e.g. VA03, ME23N, COR3) so the run can be verified.",
     "parameters": {"type": "object", "properties": {
         "outcome": {"type": "string", "enum": ["success", "failure", "aborted"]},
         "summary": {"type": "string", "description": "What was done, document numbers, open points"}},
         "required": ["outcome", "summary"]}},
    {"name": "list_saved_scripts",
     "description": "Lists VBS scripts (library, custom, drafts) with PURPOSE / PARAMS / RESULT, read-only status "
                    "and approval state.",
     "parameters": {"type": "object", "properties": {
         "folder": {"type": "string", "enum": ["library", "custom", "drafts"]}}}},
    {"name": "save_script_draft",
     "description": "Saves a new parameterised VBS script as a DRAFT for Admin review (it cannot post data until "
                    "approved). Body only: no Attach/GetObject; header lines ' PURPOSE: / ' PARAMS: / ' RESULT:; "
                    "helpers Go, SetT, Key, W, Has, Sbar, Popups, TableRows, GridRows, Need, Opt, Res.",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string", "description": "lowercase_name without .vbs"},
         "content": {"type": "string", "description": "Full VBS script text"},
         "reasoning": REASONING}, "required": ["name", "content", "reasoning"]}},
    {"name": "request_elevation",
     "description": "Asks an Admin / Engagement Lead for time-boxed write access on a system (needed for any "
                    "write on PROD, or on a read-only QA system).",
     "parameters": {"type": "object", "properties": {
         "system": {"type": "string"}, "client": {"type": "string"},
         "tcodes": {"type": "string", "description": "Comma list, e.g. COR2,COR6N ('*' = all)"},
         "minutes": {"type": "integer", "description": "Duration, e.g. 120"},
         "reason": {"type": "string"}}, "required": ["system", "client", "tcodes", "minutes", "reason"]}},
]

SAP_TOOL_NAMES = {t["name"] for t in FSD_TOOLS}


def all_tools() -> list[dict[str, Any]]:
    out = []
    for t in FSD_TOOLS:
        out.append({**t, "parameters": _with_reasoning(t["parameters"])})
    out.extend(HARNESS_TOOLS)
    return out


def anthropic_tools() -> list[dict[str, Any]]:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in all_tools()]


def openai_tools() -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["parameters"]}} for t in all_tools()]
