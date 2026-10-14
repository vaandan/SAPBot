# SAPBot – autonomous SAP GUI operator (Logic-motive)

Implements the FSD v2.0 (*SAP_Autonomous_Agent_Functional_Specification.md*) with the agreed Architecture Decisions
(*SAPBot_Architecture_Decisions.md*, both in the Claude project **SAP_Bot**). Version 0.1.0 – pilot.

| Mode | Brain | Start |
|---|---|---|
| **Cowork** (day 1) | Claude in Cowork (Team plan) calls SAPBot's local tool server | Install → restart Claude Desktop → ask Cowork |
| **API** | SAPBot's own agent loop (FSD 3.4) → Claude API, later the small model | `sapbot run "<task>"` or the Cockpit |
| **Cockpit** | Streamlit, Fiori look; consultant + admin screens | Start menu → *SAPBot Cockpit* |
| **Hub** | Office Windows server: users, grants, signed policy, script library, team examples, Claude proxy | `install_hub.ps1` |

Everything that is not needed yet ships **switched off** (Admin → Features): Claude API mode, local model,
router, distillation, Fiori, Hub sync.

---

## 1. Install on a laptop (≈10 minutes)

The code is in `C:\SAPBot\app`. In PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File C:\SAPBot\app\install\install.ps1 -ImportSapGate
```

What it does: private Python env in `C:\SAPBot\.venv`, `sapbot init` (you become Admin on this laptop), fetches the
embedding model once, copies the 13 sap-gate scripts (sap-gate itself is **not** touched), registers the `sapbot`
tool server in Claude Desktop, creates Start-menu shortcuts *SAPBot Cockpit* and *SAPBot Probe*.

Then:

```powershell
$sb = "C:\SAPBot\.venv\Scripts\python.exe -m sapbot.cli"
# client workspace: SUQ 400 = QA with write, SUP 300 = production (read-only)
& C:\SAPBot\.venv\Scripts\python.exe -m sapbot.cli workspace create sudarshan --name "Sudarshan" --system SUQ/400/QA/write --system SUP/300/PROD
& C:\SAPBot\.venv\Scripts\python.exe -m sapbot.cli workspace use sudarshan
& C:\SAPBot\.venv\Scripts\python.exe -m sapbot.cli probe
```

Quit Claude Desktop from the tray icon and start it again. Add the Cowork plugin from `C:\SAPBot\app\plugin\sapbot`
(skill that tells Claude how to work with SAPBot).

## 2. Day-1 test plan on SUQ/400 (QA)

| # | Ask in Cowork | Expect |
|---|---|---|
| 1 | "Run sapbot_status" | your user, workspace *sudarshan*, sessions, SUQ/400 = write, SUP/300 = PROD read-only |
| 2 | "Show me the first 20 company codes from T001" | task_begin opens a **new SAP window** (SAPBot's own session); SE16N; grid rows returned |
| 3 | "Display process order 1000000993 with its batches" | uses `library/pp_cor3_order_overview.vbs` (read-only → runs at once) |
| 4 | "Release process order …" | `pp_cor2_release.vbs` – approved in sap-gate, approval carried over → runs on QA |
| 5 | Same on SUP/300 | refused: production is read-only without a grant |
| 6 | "Open SE14" | Windows approval popup (hard-stop list) – click *No* |
| 7 | Repeat task 2 | `task_begin` now returns task 2 as a **past example** (RAG) |

Check afterwards: Cockpit → Run History (steps, reasoning, gold/silver), Audit Log, Learning.

## 3. Safety model (where the rules live)

All rules are enforced in one place – the tool execution layer (`sapbot/tools/executor.py` + `security/policy.py`) –
for every brain (Cowork, Claude API, small model):

* **Workspace + system check**: the live SAP SID/client must belong to the active client workspace.
* **Read-only by default**: outside display transactions the bot may only type/press if the QA/DEV system has
  `write: true` or the user holds a time-boxed **grant**. Production always needs a grant **and** a per-save approval.
* **Hard-stop list** (SE14, SE38, SM12, SM30, SU01, PFCG, SE16N edit `&SAP_EDIT`, …) → always a human decision.
* **Scripts**: approval bound to the exact content (SHA-256), read-only scanner from sap-gate, prod needs explicit
  prod approval.
* Warnings are only auto-acknowledged for message keys on `sap.warning_allowlist` (empty by default).
* Loop guard (3× same call + same status bar), 25-turn budget, `/n` rollback on errors in change transactions.
* Passwords never leave the screen reader; secrets scrubbed from logs; `masked` workspaces mask personal fields;
  `local-only` workspaces refuse cloud models (incl. examples and escalation).
* Four-eyes: nobody but an Admin can approve their own elevation request or grant themselves access.
* **Limits to know:** click/function-code classification is a conservative heuristic (unknown function codes,
  popup "Yes" buttons and toolbar buttons labelled save/change/post/… count as writes) – SAP authorizations of the
  consultant's user remain the hard limit. In laptop mode governance is trust-based: a Windows admin can edit
  `C:\SAPBot\data`. For enforcement use Hub mode **and** let IT install SAPBot with consultants as non-admin users
  (read-only ACL on `C:\SAPBot\data` and `config`).

## 4. Learning

Every task is a run (`workspaces/<client>/trajectories/runs/*.json`). Gold = posting read back in a display
transaction (or a clean read task). Gold runs are written to `gold_trajectories.jsonl` **and** the client's
ChromaDB store; the next similar task gets them as examples. Training export (Admin → Learning) builds SFT + DPO
files for `sapbot/learning/train_qlora.py` on a GPU machine – only worth it after a few hundred gold runs.

## 5. Small model later

Host Qwen-2.5-14B/32B with vLLM/Ollama (OpenAI-compatible). Admin → Features → endpoint → *Test connection* →
mark T-codes as passed in Learning → enable `local_model` (+ `router`). No reinstall.

## 6. Hub (office server)

Copy the package to `C:\SAPBotHub\app`, run `install\install_hub.ps1 -Admin vandan -Cert … -Key …`, copy the public
key to laptops, set Hub URL in Cockpit → Admin → Hub, issue tokens (Users page), `sapbot secret set hub_token`,
Settings → *Sync with Hub*. From then on users, grants and features come only from the Hub's signed policy.

## 7. Developer notes

* `pytest` – 42 tests against a simulated SAP GUI (`sapbot/sap/fake.py`); `SAPBOT_FAKE_SAP=1` runs the whole app
  without SAP.
* Layout: `sapbot/sap` (COM bridge, worker thread, fake), `security` (store, governance, policy, approvals,
  signing, masking), `tools` (schemas, executor), `memory` (trajectories, RAG, schema cache), `scripts` (VBS library
  + runner), `agent` (LLM adapters, loop, router, batch), `ui` (Cockpit), `hub`, `learning`.
* Known limits of 0.1: Fiori bridge not included (v1.1); SAP date format detection reads SM37's default date;
  table controls return visible rows only; no signed `Setup.exe` yet (ADR D8).
