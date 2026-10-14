---
name: sapbot
description: Work in SAP GUI (transactions, tables, postings, mass runs) for the consultant through the SAPBot tool server with its safety policy. Use whenever the user asks to check, display, extract, create, change, post or verify anything in an SAP system.
---

# SAPBot – SAP GUI work through the safety policy

SAPBot runs on the consultant's Windows PC and drives SAP GUI in **its own SAP session**. Every tool call passes
the SAPBot policy (client workspace, QA/PROD, write mode, grants, approvals). You cannot switch the policy off.

## Workflow (always)

1. `sapbot_status` – who the user is, the active **client workspace**, its systems (QA/PROD, read-only/write),
   open SAP sessions, grants, features. If no workspace is active, ask the user which client and call
   `sapbot_status(set_workspace=<id>)`.
2. `task_begin(task=<the user's request>, system=?, client=?)` – one task per user request. It opens SAPBot's own
   session and returns **similar past successful runs**: follow their pattern where it fits.
3. Work step by step and pass a short `reasoning` on every SAP tool:
   - `sap_observe_screen` before acting on an unfamiliar screen; use field **ids** exactly as returned.
   - `sap_act`: navigation alone (`ok_code="/nVA03"`), then fields + key (`field_updates_json`, `vkey=0` Enter,
     `8` Execute, `11` Save, `3` Back, `12` Cancel).
   - `sap_read_grid` for ALV lists / table controls.
   - `list_saved_scripts` → `run_saved_script` for routine steps that already have a proven script.
   - `rag_search_trajectories` again when you hit an unfamiliar screen; `action_schema_memory` for known screens.
4. **Postings**: after a save, read the document back in a display transaction (VA03, ME23N, COR3, MB03, FB03 …)
   so the run counts as verified.
5. `task_end(outcome, summary)` – include document numbers and anything the user must follow up.

## Rules

- Read the status bar after every action (type S/W/E/A/I + message class/number). Don't treat an empty status bar
  as proof of success.
- Dates: always give dates as **DD.MM.YYYY** (firm standard). SAPBot converts to the user's SAP format.
- If a call is **refused**, do not try another route to the same effect. Tell the user exactly what is needed:
  approval (Windows popup on their PC), an elevation grant (`request_elevation`), or a workspace change by an
  Admin. Production is read-only unless a grant exists, and every production save still needs approval.
- Loop detected → stop and ask the user.
- Never type passwords, never log on – the user logs on in SAP Logon.
- New repeatable procedure? Save it with `save_script_draft` (PURPOSE / PARAMS / RESULT header, `Need`/`Opt`
  parameters, no Attach/GetObject). An Admin approves it before it can post.
- Mass runs from Excel: read the file with `local_file_workspace`, process row by row, write results with
  `write_excel` to the workspace output folder (or use the Cockpit's Batch Runs page).
