# SAPBot – autonomous SAP GUI operator (Logic-motive)

SAPBot drives SAP GUI on a consultant's laptop through a safety policy, learns from every verified run (per-client
RAG) and is prepared for a later on-prem small model. It implements the FSD v2.0 and the Architecture Decisions
(ADR) kept in the *SAP_Bot* Claude project. Version **0.1.1**.

**➡ Rollout across the office server and consultant laptops: read [`DEPLOYMENT.md`](DEPLOYMENT.md).**

| Mode | Brain | Start |
|---|---|---|
| **Cowork** | Claude in Cowork (Team plan) calls SAPBot's local tool server | install → restart Claude Desktop → ask Cowork |
| **API** | SAPBot's own agent loop (FSD 3.4) → Claude API (via Hub proxy), later the small model | `sapbot run "<task>"` or the Cockpit |
| **Cockpit** | Streamlit, Fiori look; consultant + admin screens | Start menu → *SAPBot Cockpit* |
| **Hub** | Office Windows Server: users, grants, signed policy, scripts, team examples, audit, Claude proxy | `install\install_hub.ps1` |

## Quick start – single pilot laptop (laptop mode, no Hub)

```powershell
# 1. code in C:\SAPBot\app, then (no admin rights needed):
powershell -ExecutionPolicy Bypass -File C:\SAPBot\app\install\install.ps1 -ImportSapGate
# 2. SAP Logon > Options > Accessibility & Scripting: Enable scripting, untick both "Notify ..." boxes; log on to QA
# 3. client workspace + check
$sb = "C:\SAPBot\.venv\Scripts\python.exe"
& $sb -m sapbot.cli workspace create sudarshan --name "Sudarshan" --system SUQ/400/QA/write --system SUP/300/PROD
& $sb -m sapbot.cli workspace use sudarshan
& $sb -m sapbot.cli probe
# 4. quit Claude Desktop from the tray icon, start it again, add the plugin from C:\SAPBot\app\plugin\sapbot
```

In laptop mode the first Windows user is Admin of that laptop and governance is trust-based. For production use the
Hub and the IT-managed install (DEPLOYMENT.md §5.3).

### Day-1 test plan (QA)

| # | Ask in Cowork | Expect |
|---|---|---|
| 1 | "Run sapbot_status" | user, workspace, sessions; QA = write, PROD = read-only |
| 2 | "Show me the first 20 company codes from T001" | SAPBot opens its **own SAP window**; SE16N; rows returned |
| 3 | "Display process order <n> with its batches" | read-only library script runs at once |
| 4 | "Release process order <n>" (QA) | approved posting script runs on QA |
| 5 | Same on PROD | refused: production needs a grant |
| 6 | "Open SE14" | Windows approval popup (hard stop) – click *No* |
| 7 | Repeat task 2 | task 2 now comes back as a **past example** |

## Safety in one paragraph

Every tool call from every brain passes one policy engine: the live SAP system must belong to the active client
workspace; read-only by default (writes only with QA/DEV write mode or a time-boxed grant); production writes need a
grant **and** a per-action approval; hard-stop transactions always need a human; conservative write detection;
warnings never auto-confirmed unless allow-listed; `/n` rollback on errors; loop guard and budgets; no SAP passwords;
data masking / local-only per client; signed Hub policy; full audit. SAP authorizations of the consultant's user stay
the hard limit. Details and limits: DEPLOYMENT.md §7.

## Developers

* `pytest` – 49 tests against a simulated SAP GUI (`sapbot/sap/fake.py`); `SAPBOT_FAKE_SAP=1` runs the whole app
  without SAP.
* Layout: `sapbot/sap` (COM bridge, worker thread, fake), `security` (store, governance, policy, approvals, signing,
  masking), `tools` (schemas, executor), `memory` (trajectories, RAG, schema cache), `scripts` (VBS library + runner),
  `agent` (LLM adapters, loop, router, batch), `ui` (Cockpit), `hub` (server + client), `learning`.
* Known limits of 0.1: no Fiori bridge (v1.1); table controls return visible rows only; date format detection reads
  SM37's default date; no signed `Setup.exe` yet.
