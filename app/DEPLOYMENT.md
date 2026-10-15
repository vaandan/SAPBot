# SAPBot — Enterprise Deployment & Operations Guide

**Version:** SAPBot 0.1.1 · **Owner:** Logic-motive (Vandan) · **Audience:** IT / server administrators, SAP Basis,
security & compliance, engagement leads, consultants.
**Related:** `README.md` (overview), Functional Specification (FSD v2.0) and Architecture Decisions (ADR) in the
*SAP_Bot* Claude project.

> SAPBot drives SAP GUI on a consultant's laptop on the consultant's behalf, with a safety policy in front of every
> action. It never stores SAP passwords and never logs on to SAP. Everything that is not needed yet ships
> **switched off**.

---

## Contents

1. [Architecture & topology](#1-architecture--topology)
2. [Prerequisites checklist](#2-prerequisites-checklist)
3. [Hub server deployment (Tier 1)](#3-hub-server-deployment-tier-1)
4. [Users, tokens, workspaces and features](#4-users-tokens-workspaces-and-features)
5. [Laptop installation (Tier 2)](#5-laptop-installation-tier-2)
6. [Enrolment & synchronisation](#6-enrolment--synchronisation)
7. [Governance & security model (as built)](#7-governance--security-model-as-built)
8. [Day-to-day operations](#8-day-to-day-operations)
9. [Offline behaviour](#9-offline-behaviour)
10. [Troubleshooting](#10-troubleshooting)
11. [Backup, upgrade, key rotation, offboarding, uninstall](#11-backup-upgrade-key-rotation-offboarding-uninstall)
12. [Reference: commands, files, network endpoints](#12-reference)

---

## 1. Architecture & topology

```
                         Office network / VPN
  ┌────────────────────────────────────────────────────────────────────────────┐
  │  TIER 1 - SAPBot Hub  (Windows Server, service "SAPBotHub", HTTPS :8443)   │
  │   users · roles · workspace memberships · grants · elevation requests       │
  │   feature flags · client workspace definitions · approved script library    │
  │   team gold runs per client (RAG sharing) · audit log · Claude API proxy    │
  │   signs a policy per user (Ed25519, valid 7 days)                           │
  └───────────────▲──────────────────────────────────────────┬─────────────────┘
                  │ HTTPS (token)                            │ HTTPS 443
                  │ policy, scripts, workspaces, team runs ↓  │ (Claude API mode only)
                  │ audit, own gold runs ↑                    ▼
  ┌───────────────┴───────────────────────┐        api.anthropic.com
  │ TIER 2 - Consultant laptop             │
  │  SAPBot tool server (MCP) ◄── Claude Desktop / Cowork (Team seat) ──► Anthropic cloud
  │  SAPBot Cockpit (localhost:8501)       │
  │  policy engine · approvals · logs · per-client RAG (ChromaDB)
  │        │ COM (SAP GUI Scripting, local only)
  │        ▼
  │  SAP GUI (SAPBot's own session) ──────────► client SAP systems (QA / PROD)
  └────────────────────────────────────────┘
```

| Component | Runs where | Talks to |
|---|---|---|
| SAP GUI automation | Laptop only (COM cannot be remote) | Local SAP Logon / SAP GUI |
| Brain – Cowork mode | Claude in Cowork (Anthropic cloud) | Laptop tool server via Claude Desktop |
| Brain – API mode | SAPBot agent loop on the laptop | Claude API **via the Hub proxy** (or direct with a personal key) |
| Brain – small model (later) | vLLM/Ollama next to the Hub | Laptop over HTTPS (OpenAI-compatible) |
| Governance authority | Hub | Laptops pull a signed policy |

**Where SAP data goes.** Screen contents, field values and grid rows that SAPBot reads are sent to the model that
drives the task: in Cowork and Claude API mode that is Anthropic's cloud (commercial terms; not used for training by
default). Workspaces with `data_policy: masked` mask configured personal-data fields first; `local-only` workspaces
refuse cloud models entirely (only the on-prem small model may be used). The Hub stores **gold runs** (task, steps,
compact observations) per client — treat the Hub as holding client data (access control, backup encryption,
retention).

---

## 2. Prerequisites checklist

### 2.1 Hub server (IT)

| Item | Requirement |
|---|---|
| OS | Windows Server 2019 / 2022 (domain-joined recommended) |
| Size | 2 vCPU, 4 GB RAM, 20 GB disk to start (grows with gold runs and audit) |
| Python | 3.11 or 3.12, **installed for all users** (python.org installer → "Install for all users") |
| Service manager | **NSSM** (`nssm.exe`, nssm.cc) — third-party, get it approved by security |
| DNS | Stable name, e.g. `sapbot-hub.corp.local` |
| TLS certificate | Server certificate from the **internal CA**, SAN = the DNS name, PEM cert + key files |
| Inbound firewall | TCP **8443** from office LAN + VPN ranges only |
| Outbound | HTTPS 443 to `api.anthropic.com` (Claude API mode); via proxy if required |
| Service account | Recommended: dedicated domain account (e.g. `CORP\svc_sapbot`), not LocalSystem |
| Backup | Nightly backup of `C:\SAPBotHub\data`, `workspaces`, `scripts`; `keys` once into the password vault / encrypted backup |

### 2.2 Consultant laptops (IT / consultant)

| Item | Requirement |
|---|---|
| OS | Windows 10 22H2 / Windows 11 |
| SAP GUI | SAP GUI for Windows 7.70 or 8.00 with scripting component installed |
| Claude | Claude Desktop installed and signed in with the consultant's **Team seat**; Cowork used as a **local** session |
| Python | 3.11+ (installer can fetch it via `winget` in user scope; or from the software portal) |
| Network (install) | `pypi.org`, `files.pythonhosted.org` (Python packages), `chroma-onnx-models.s3.amazonaws.com` (embedding model, one time) — or use the offline wheelhouse (§5.6) |
| Network (run) | Hub URL (office/VPN); `api.anthropic.com` only if API mode without Hub proxy |
| PowerShell | Execution policy must allow `-ExecutionPolicy Bypass` for the installer, or IT signs/deploys the script |
| Application control | AppLocker / Defender must allow `python.exe` from the install folder and `cscript.exe` (VBS scripts) |

### 2.3 SAP GUI scripting (consultant + Basis)

**Client side — on every laptop (mandatory):**

1. SAP Logon → *Options* → *Accessibility & Scripting* → *Scripting*
2. ✔ **Enable scripting**
3. ✘ **Notify when a script attaches to SAP GUI** (uncheck — otherwise a popup appears on every SAPBot step)
4. ✘ **Notify when a script opens a connection** (uncheck)

Some companies lock these via the SAP GUI registry/GPO (`HKCU\Software\SAP\SAPGUI Front\SAP Frontend Server\Security`);
IT then has to set them centrally.

**Server side — per SAP system (Basis):**

* Do **not** change anything up front. Run `sapbot probe` (§5.5). On the systems used so far, client-side
  scripting was sufficient.
* Only if probe reports *"scripting disabled"* / actions fail although the client setting is on: ask Basis to check
  profile parameter **`sapgui/user_scripting`** in **RZ11** (dynamic, no restart). If
  `sapgui/user_scripting_per_user = TRUE`, the consultant's SAP user additionally needs authorization object
  **`S_SCR`**.
* Optional hardening Basis may already use: `sapgui/user_scripting_disable_recording`,
  `sapgui/user_scripting_force_notification` (the latter forces the notify popups — agree an exception for SAPBot
  users or accept the popups).

**Other SAP prerequisites:**

* The consultant **logs on** in SAP Logon; SAPBot opens one extra session on that logon (default limit 6 sessions,
  parameter `rdisp/max_alt_modes`).
* Firm standard: logon language **EN**, date format **DD.MM.YYYY** (SU3). If different, SAPBot converts dates;
  `probe` reports it.
* **SAP authorizations of the consultant's user remain the hard limit.** SAPBot can never do more than the user may.
  All changes appear in SAP change documents under the consultant's user.

### 2.4 Claude administration

* Team plan seats for every consultant who uses Cowork mode.
* If your Claude organisation restricts local MCP servers / desktop extensions, allow the server named **`sapbot`**.
* For **Claude API mode** through the Hub: one API key from the Anthropic Console (per-token billing, separate from
  the Team plan) — stored only on the Hub.

### 2.5 Compliance sign-off (before first client use)

* Client informed that an assistant operates SAP GUI under the consultant's user; check NDA / data-processing terms
  for cloud AI processing (choose `masked` or `local-only` where required).
* Check the client's SAP licence terms regarding automation where relevant.
* Agree retention period per client workspace (default 365 days) and the offboarding procedure (§11.4).

---

## 3. Hub server deployment (Tier 1)

All steps in an **elevated** PowerShell on the server.

**3.1 Copy the package**

```powershell
New-Item -ItemType Directory C:\SAPBotHub -Force
# copy (or git clone) the SAPBot package so that C:\SAPBotHub\app\pyproject.toml exists
```

**3.2 Certificate.** Request a server certificate for `sapbot-hub.corp.local` from the internal CA; export as PEM:
`C:\certs\sapbot-hub.crt` and `C:\certs\sapbot-hub.key`. Restrict the key file to Administrators + the service
account.

**3.3 Install + service + firewall**

```powershell
powershell -ExecutionPolicy Bypass -File C:\SAPBotHub\app\install\install_hub.ps1 `
  -Admin vandan `
  -Cert C:\certs\sapbot-hub.crt -Key C:\certs\sapbot-hub.key `
  -AllowedRemote "10.10.0.0/16,10.200.0.0/16" `
  -ServiceAccount "CORP\svc_sapbot" -HubName sapbot-hub.corp.local `
  -HttpsProxy "http://proxy.corp.local:8080" -CaBundle C:\certs\corp_root_ca.pem   # only if outbound needs them
```

The script: creates `C:\SAPBotHub\.venv`, generates the **policy signing key pair** (`C:\SAPBotHub\keys`), creates the
first **Admin** (`-Admin` = that person's Windows user name) and prints the **admin token** and the **public key
fingerprint** once, installs the service `SAPBotHub` (auto-start, log rotation, optional service account), asks for
the **Claude API key** (stored in `C:\SAPBotHub\keys\anthropic_api_key.txt`; ACL Administrators, SYSTEM and the
service account), adds `C:\SAPBotHub\.venv\Scripts` to the machine PATH (command `sapbot-hub` in new elevated
shells), adds the firewall rule (Domain/Private profiles, `-AllowedRemote` only) and runs a health check against
`-HubName` (the name on the certificate). Paths must not contain spaces.

> **Save the admin token and the fingerprint in the password vault immediately.** The token is shown once.

The service refuses to start without a certificate. `-InsecureLanTest` exists only for a first test on an isolated
network (tokens would travel unencrypted).

**3.4 Verify from a laptop**

```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12   # Windows PowerShell 5.1
Invoke-RestMethod https://sapbot-hub.corp.local:8443/api/health
```

Expect `{"ok": true, ...}`. A certificate error means the laptop does not trust the internal CA (§10).

**3.5 Claude API proxy.** Laptops in API mode call `https://<hub>:8443/v1/anthropic/...`; the Hub forwards to
`api.anthropic.com` with the firm key, counts tokens per user and month, and returns HTTP 429 at the cap.

```powershell
sapbot-hub feature set claude_api_mode on   # the proxy refuses while this is off
sapbot-hub token-cap 20000000              # Claude tokens per user per month
```

Change the key later: re-run `install_hub.ps1` (it keeps everything else and asks for the key; *Enter* keeps the
existing key), or replace the content of `C:\SAPBotHub\keys\anthropic_api_key.txt` in an elevated editor and run
`nssm restart SAPBotHub`.

**3.6 Backups** — see §11.1. Without the private key in `keys\`, every laptop has to be re-enrolled.

---

## 4. Users, tokens, workspaces and features

Run in an **elevated** PowerShell on the Hub server (the `keys` folder is Administrators-only), or use the Cockpit
admin pages from an enrolled Admin laptop — both write to the Hub. `sapbot-hub` is the same as
`C:\SAPBotHub\.venv\Scripts\python.exe -m sapbot.hub.app`.

**4.1 Rule: Hub user name = the consultant's Windows user name** (lowercase, the part after the backslash of
`whoami`). The signed policy is bound to it; a mismatch makes enrolment fail with *"Hub policy is for X, but this
laptop's Windows user is Y"*.

**4.2 Client workspaces**

```powershell
sapbot-hub workspace create sudarshan --name "Sudarshan Chemical" `
   --system SUQ/400/QA/write --system SUP/300/PROD --policy full --tcodes "VA01,COR2,COR6N"
sapbot-hub workspace list
```

`SID/CLIENT/ENV[/write]`: `write` only for QA/DEV systems where the bot may save without a grant. PROD is always
read-only without a grant. `--tcodes` = change transactions allowed in addition to the built-in display list.

**4.3 Users and tokens**

```powershell
sapbot-hub user rkumar --role consultant --display "R. Kumar" --workspace sudarshan
sapbot-hub user asharma --role lead --workspace sudarshan      # engagement lead of that client
sapbot-hub users                                              # list users + memberships
sapbot-hub token rkumar                                       # new token, old one stops working
sapbot-hub user rkumar --disable                              # leaver: token stops immediately
sapbot-hub member sudarshan rkumar --remove
```

Creating a user prints its token **once**; `token <user>` or `user <user> --new-token` issues a new one (the old
one stops working). Changing an existing user (`--role`, `--display`, `--disable`, `--enable`, `--workspace`) keeps
everything you don't pass and issues no token; use `member` to change workspace assignments. Hand tokens over through
a secure channel (password-manager share, not plain e-mail/chat). On the laptop they live only in Windows Credential
Manager.

| Role | Can |
|---|---|
| Admin | everything: users, workspaces, features, script approval, grants anywhere, audit |
| Engagement Lead | approve/deny elevation requests and grant/revoke access **in their workspaces**; audit + learning for them |
| Consultant | work in assigned workspaces; request elevation; see own runs |

Four-eyes: nobody except an Admin can approve their own request or grant themselves access.

**4.4 Feature flags** (firm-wide or per workspace)

```powershell
sapbot-hub feature list
sapbot-hub feature set claude_api_mode on
sapbot-hub feature set batch_runs off --workspace sudarshan
```

Defaults when unset: `cowork_mode`, `rag`, `batch_runs` on; `claude_api_mode`, `local_model`, `router`,
`distillation`, `fiori` off. Changes reach laptops at their next sync (§6.3).

**4.5 Script library.** Approved scripts are published from an enrolled Admin laptop (Cockpit → Script Approval) or via
`POST /api/scripts`; laptops download them on sync. Approval is bound to the exact content (SHA-256).

---

## 5. Laptop installation (Tier 2)

### 5.1 Choose the installation path

| | **A. Self-install** | **B. IT-managed** |
|---|---|---|
| Who runs it | Consultant, **no admin rights** | IT (elevated, once per laptop) + consultant (first-time setup) |
| Code location | `C:\SAPBot\app` (or `%LOCALAPPDATA%\SAPBot\app` if `C:\` is locked) | `C:\Program Files\SAPBot` (read-only for users) |
| Data location | same folder | `%LOCALAPPDATA%\SAPBot` per user |
| Hub settings | user config (written by `sapbot enroll`) | **machine policy** `%ProgramData%\SAPBot\machine.yaml` (read-only for users, overrides user settings) |
| Local admin role | first Windows user is Admin until the laptop is enrolled | never — roles only from the Hub |
| Enforcement | **trust-based** (a user can edit code and data) | consultant cannot change code, Hub URL or Hub key; policy from the Hub only |
| Use for | pilot, admins, developers | **production rollout** |

Be precise with security teams: on path A the controls protect against mistakes, not against a determined user. On
path B the remaining hard boundaries are the Hub (grants, audit) and SAP authorizations.

### 5.2 Path A — self-install (no admin)

1. Get the package into `C:\SAPBot\app` (unzip `SAPBot-<version>.zip` into `C:\SAPBot`, or `git clone <repo> C:\SAPBot\app`).
   Copy only `app` — never another laptop's `data`, `config` or `workspaces`.
2. Install and (optionally) enrol in one go:
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\SAPBot\app\install\install.ps1 `
      -HubUrl https://sapbot-hub.corp.local:8443 -PubKey \\fileserver\it\sapbot\policy_signing.pub.pem `
      -Fingerprint "AB12:34CD:...." [-CaBundle C:\certs\corp_root_ca.pem] [-ImportSapGate]
   ```
   You are asked for your Hub token. Without `-HubUrl/-PubKey` the laptop runs in laptop mode (pilot): then create a
   workspace locally (`sapbot workspace create …`, see README). If such a laptop is enrolled later, local workspaces
   that are not assigned to the user on the Hub are skipped by sync (create the same workspace id on the Hub and
   assign the user to keep using its history).
   The installer adds the environment's `Scripts` folder to the user PATH, so `sapbot …` works in new windows
   (otherwise use `C:\SAPBot\.venv\Scripts\sapbot.exe`).
3. Continue with §5.4 – §5.5.

### 5.3 Path B — IT-managed

**IT (elevated, e.g. via Intune/SCCM):**

1. Copy the package to `C:\Program Files\SAPBot\app`. Python 3.11+ must be installed **for all users**.
2. Run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File "C:\Program Files\SAPBot\app\install\install.ps1" -Mode Machine `
      -InstallDir "C:\Program Files\SAPBot" -HubUrl https://sapbot-hub.corp.local:8443 `
      -PubKey .\policy_signing.pub.pem -Fingerprint "AB12:34CD:...." -CaBundle .\corp_root_ca.pem
   ```
   This installs SAPBot non-editable into its own environment, verifies the key fingerprint, writes the locked machine
   policy (`require_hub: true`, Hub URL, key, CA) to `%ProgramData%\SAPBot` with ACL *Users: read*, pre-fetches the
   embedding model and creates All-Users Start-menu entries.

**Consultant (normal rights):** Start → SAPBot → **SAPBot First-time Setup** (same as
`install.ps1 -Mode User`). It creates `%LOCALAPPDATA%\SAPBot`, asks for the Hub token, enrols (§6), registers SAPBot in
Claude Desktop. Then §5.4 – §5.5.

### 5.4 Claude Desktop / Cowork

* The installer registers the tool server in `%APPDATA%\Claude\claude_desktop_config.json` (entry `sapbot`, other
  servers untouched, backup `*.sapbot-backup`).
* **Quit Claude Desktop from the tray icon and start it again.**
* Add the Cowork plugin from `<app>\plugin\sapbot` (or from the org plugin catalogue once published). It teaches
  Claude the SAPBot workflow.
* Check in a new local Cowork task: *"Run sapbot_status."*

### 5.5 SAP check

1. Set the SAP GUI scripting options (§2.3) and log on to the QA system in SAP Logon.
2. Start → SAPBot → **SAPBot Probe** (`sapbot probe`). It checks, read-only: SAP Logon reachable + scripting enabled;
   screen read on each logged-on system; logon language; SAPBot can open its own session; user date format;
   system belongs to a client workspace.
3. Fix whatever is red (§10) before the first task.

### 5.6 Offline / restricted-internet laptops

Build a **wheelhouse** once on a Windows machine with internet and the **same Python minor version**:

```powershell
py -3.12 -m pip download -r C:\SAPBot\app\requirements.txt -d C:\SAPBot\app\wheelhouse
py -3.12 -m pip download "setuptools>=68" wheel -d C:\SAPBot\app\wheelhouse     # build backend for SAPBot itself
```

Ship `app` including `wheelhouse`; the installer then installs without internet. The embedding model needs one
download (IT-managed installs fetch it into `%ProgramData%\SAPBot\models`); without it SAPBot uses a built-in offline
search with weaker matching.

---

## 6. Enrolment & synchronisation

### 6.1 Secure public-key transfer

The Hub signs each user's policy with its private key; laptops trust **only** the public key file they were given.

1. Admin runs `sapbot-hub fingerprint` (also printed at install) and publishes the fingerprint on an internal,
   authenticated page (intranet / password vault note).
2. IT distributes `C:\SAPBotHub\keys\policy_signing.pub.pem` through software distribution or an access-controlled
   share. **Do not** take the key from the Hub's `/api/public-key` endpoint for enrolment — that would trust the
   network instead of IT.
3. Enrolment compares the file with the published fingerprint (`--fingerprint`); a mismatch aborts.

### 6.2 Enrol a laptop

Path A: `install.ps1 -HubUrl … -PubKey … -Fingerprint …`, or later:

```powershell
sapbot enroll --hub https://sapbot-hub.corp.local:8443 --pubkey <file> --fingerprint "AB12:...." [--ca-bundle <corp CA>]
```

Path B: *SAPBot First-time Setup* (Hub URL and key come from the machine policy).

What happens: key verified and stored → Hub URL (+ CA) saved → token stored in Windows Credential Manager → first
sync: signed policy installed, client workspaces, approved scripts and team examples downloaded, local audit
uploaded. From then on the laptop is **Hub-governed**: users, roles, grants and features come only from the signed
policy; local edits are ignored. Check with `sapbot hub-status` (Start → SAPBot → SAPBot Hub Status).

### 6.3 Synchronisation

| What | Direction | When |
|---|---|---|
| Signed policy (user, role, memberships, grants, flags, workspaces) | Hub → laptop | at SAPBot start, every 240 min (`hub.sync_interval_minutes`), *Sync now* (Cockpit), `sapbot sync` |
| Approved scripts (+ revocations) | Hub → laptop | same |
| Team gold runs of the laptop's clients → local RAG | Hub → laptop | same |
| Own gold runs | laptop → Hub | same |
| Audit log | laptop → Hub | same (queued while offline) |

A grant approved on the Hub becomes usable on the laptop at the next sync — tell the consultant to click *Sync now*
(Cockpit) or run `sapbot sync`. Running SAPBot processes (the Cowork tool server, the Cockpit) pick up a newly synced
policy within about 15 seconds; no restart needed.

### 6.4 Token rotation / lost laptop

`sapbot-hub token <user>` (new token, old one invalid) → consultant runs `sapbot enroll` again (or
`sapbot secret set hub_token` + `sapbot sync`). Lost/stolen laptop: `sapbot-hub user <user> --disable`, revoke grants;
the cached policy on that laptop expires within 7 days at the latest (checked continuously, also in running
processes); grants inside it expire at their own end time.

---

## 7. Governance & security model (as built)

| Control | Behaviour | Where enforced |
|---|---|---|
| One policy for all brains | Cowork, Claude API and the small model call the same tools through the same policy engine | Laptop tool layer |
| Client isolation | Separate folder, RAG store, logs, schema cache per client workspace; the live SAP SID/client must belong to the active workspace or every action is refused | Laptop |
| Read-only by default | Outside display transactions the bot may only type/press when the QA/DEV system has `write` or the user has an active **grant** | Laptop |
| Production | Any write-risk action needs a **grant** (time-boxed, scoped to system + T-codes) **and** a **per-action approval** by the consultant (Windows dialog / Cockpit) | Laptop + Hub (grants) |
| Four-eyes | Grants are given by an Admin or the workspace's Engagement Lead; nobody but an Admin can grant themselves or approve their own request (enforced on the Hub and in the laptop tools). *Per-save approvals are given by the consultant, not a second person.* | Hub |
| Hard stops | SE14, SE38, SA38, SE37, SE80, SM12, SM30, SM31, SM34, SE16, SCC4, SCC5, SE10, STMS, SU01, PFCG, SM59, RZ10, RZ11, SE06, SCC1, `&SAP_EDIT`, `&SAP_DELETE`, `/h`, `/hs` → always a human decision (+ write permission / grant) | Laptop |
| Write detection | Conservative: save keys/buttons, any function code not on the read-only list, keys outside Enter/F2/F3/F8/F12/paging, popup "Yes"/save prompts, buttons labelled save/change/post/delete/create/release/… count as writes. Commands only via the command field parameter; only ids of SAPBot's own session | Laptop |
| Warnings | Never auto-confirmed unless the message key is on `sap.warning_allowlist` (empty by default) | Laptop |
| Errors | When an SAP action fails with an exception inside a change transaction, SAPBot leaves it with `/n` (releases locks; a "save changes?" popup is answered **No**). An action that ends with an E/A status message or a timeout is reported and the run pauses; the transaction is not left automatically — the model or consultant decides | Laptop |
| Loops / budgets | Same call 3× with identical status bar → stop; 25 turns and a token budget per run (API mode); monthly token cap per user on the Hub proxy | Laptop + Hub |
| Scripts | Approval bound to exact content (SHA-256); strict read-only scanner; drafts written by Claude always need human approval; production needs an explicit production approval | Laptop + Hub |
| Credentials | No SAP passwords stored or typed (password fields are masked on read); API keys/tokens only in Windows Credential Manager or the Hub service environment | Laptop + Hub |
| Data policy | `masked` masks configured SAP fields and personal-data patterns (e-mail, phone, IBAN, PAN, secrets) in screen data, grid rows and status messages before they go to the model; free text that matches no pattern is not masked. `local-only` refuses cloud models, including examples and escalation | Laptop |
| Signed policy | Ed25519, bound to the Windows user, replay of older policies rejected, valid 7 days | Laptop verifies, Hub signs |
| Audit | Every task, step, approval, refusal, grant, script approval and workspace change; uploaded to the Hub | Laptop + Hub |
| Ultimate boundary | SAP authorizations of the consultant's own user; all changes in SAP change documents under that user | SAP |

**Known limits (state them to security):** write detection is a heuristic, not a parser of every SAP screen; on
self-installed laptops (path A) a user with access to the files can bypass local controls; on IT-managed laptops
(path B) code and machine policy are read-only for the user, but a local Windows administrator can still change
them — keep consultants non-admin; table controls return only visible rows; the Fiori bridge is not part of 0.1.

---

## 8. Day-to-day operations

**Consultant — Cowork:** open a local Cowork task → *"Use SAPBot: on Sudarshan QA, display process order 1000000993
with batches"*. SAPBot opens its own SAP window, shows similar past runs to Claude, works step by step. Approval
popups appear on the laptop for sensitive actions. For postings Claude reads the document back in a display
transaction; such runs become **gold** examples for the next similar task.

**Consultant — Cockpit** (Start → SAPBot Cockpit, `http://localhost:8501`): run tasks (API mode, when enabled),
watch every step, approve / edit values / abort, run library scripts, Excel batch runs (resume after interruption;
production batches only with an approved saved script and one batch approval), request elevation, run history.

**Engagement Lead:** Cockpit → *Grants & Requests* (approve with care: scope T-codes and duration), *Audit Log*,
*Learning*.

**Admin:** users/tokens (§4), workspaces, *Script Approval* (review red lines, approve content, production flag),
*Features*, *Learning* (gold runs, RAG hit rate, evaluation gate per T-code), training export.

**Activating the small model later:** host Qwen-2.5-14B/32B with vLLM/Ollama (OpenAI-compatible) → Features →
endpoint → *Test connection* → mark T-codes that passed evaluation in *Learning* → `feature set local_model on`
(+ `router on`). No reinstall.

---

## 9. Offline behaviour

| Situation | Behaviour |
|---|---|
| VPN down, policy valid | SAPBot works normally with the cached signed policy (grants still expire on time); audit and gold runs are queued; sync retries every interval |
| Policy older than 7 days | Laptop becomes **restricted** (also in already running processes): no workspace access, no grants — SAP actions are refused until a successful sync |
| Hub reachable but token disabled | Sync fails with 401; laptop keeps the cached policy until it expires |
| Claude API via Hub proxy offline | API mode tasks fail; Cowork mode is unaffected (Cowork talks to Anthropic directly) |
| No internet at install | Use the wheelhouse (§5.6) |
| Embedding model not downloadable | Offline fallback search (weaker matches); fetched automatically when reachable |

---

## 10. Troubleshooting

Start with `sapbot probe` and `sapbot hub-status`. Logs: laptop `<data>\logs\` (`mcp.log`, `cockpit.log`,
`cli.log`); Hub `C:\SAPBotHub\logs\hub.err.log`, `hub.out.log`.

| Symptom | Likely cause | Fix |
|---|---|---|
| `SAP Logon is not running` | SAP Logon closed | Start SAP Logon and log on |
| `SAP GUI scripting is disabled in SAP Logon` | Client option off / locked by GPO | §2.3 client side; IT if locked |
| Probe can attach but actions fail / "scripting disabled" | Server-side scripting off for this system/user | Basis: RZ11 `sapgui/user_scripting`; `S_SCR` if per-user scripting |
| Popup "A script is attempting to access SAP GUI" on every step | Notify options on | Uncheck both notify options (§2.3) |
| `No SAP session for SID/client` | Not logged on to that system | Log on in SAP Logon; SAPBot never logs on |
| `Could not open a new session (session limit?)` | 6 sessions open | Close a session, or allow `use_existing_session` when asked |
| `SAP system X is not part of workspace Y` | Wrong window/client or workspace | Switch workspace or add the system (Admin) |
| `production is read-only` | No grant | Request elevation; Lead approves; *Sync now* |
| `feature 'cowork_mode' is disabled` | Flag off | Admin: `feature set cowork_mode on` |
| `sapbot` tools missing in Cowork | Claude Desktop not restarted / JSON broken / cloud session / org policy | Quit from tray + restart; check `%APPDATA%\Claude\claude_desktop_config.json`; use a local session; Claude admin allows `sapbot` |
| `CERTIFICATE_VERIFY_FAILED` (Hub) | Laptop does not trust internal CA | `--ca-bundle <corp root CA>` at enrolment, or `network.ca_bundle` |
| `CERTIFICATE_VERIFY_FAILED` (pip / model / Claude) | TLS-inspecting proxy | `network.ca_bundle`; for pip `set PIP_CERT=<CA>`; Hub: `-CaBundle` |
| `407 Proxy Authentication Required` | Corporate proxy | Set `HTTPS_PROXY` / `NO_PROXY` (exclude the Hub host) |
| Hub health check times out | Firewall / service down | `Get-Service SAPBotHub`; firewall rule scope; port 8443 open from VPN range |
| `policy signature invalid` | Wrong public key file | Re-enrol with the file from IT and the correct fingerprint |
| `Hub policy is for X, but this laptop's Windows user is Y` | Hub user ≠ Windows user | Create the Hub user with the Windows user name (§4.1) |
| `sapbot` is not recognized | New PATH not loaded | Open a new PowerShell window, or use `<install>\.venv\Scripts\sapbot.exe` |
| `PermissionError` running `sapbot-hub` | Shell not elevated (`keys` is Administrators-only) | Run PowerShell as administrator |
| Sync reports `skipped_workspaces` | Local workspace not assigned on the Hub | Create/assign it on the Hub or purge it locally |
| SAP transaction stays locked after an error | Run ended with E/A status or timeout (no automatic `/n`) | `/n` in the SAPBot SAP window, or ask SAPBot to leave the transaction |
| `an older Hub policy was presented` | Restored/old database | `sapbot sync` |
| Cockpit shows *restricted* | Policy expired (offline > 7 days) | Connect to VPN, *Sync now* |
| API mode: HTTP 403 `claude_api_mode is disabled on the Hub` | Hub flag off | `feature set claude_api_mode on` |
| API mode: HTTP 429 | Monthly token cap reached | Raise `token-cap` or wait for next month |
| API mode: HTTP 500 `Hub has no Claude API key` | Key not in service environment | §3.5 |
| Dates rejected by SAP | User date format ≠ DD.MM.YYYY and not probed | Run `sapbot probe` (stores the format) |
| Approval popup does not appear | Hidden behind windows / timeout 300 s (then refused) | Check the taskbar. In Cowork mode answer in the Windows dialog — the Cockpit only lists it. (Setting `approvals.mode: db` moves all approvals into the Cockpit.) |

---

## 11. Backup, upgrade, key rotation, offboarding, uninstall

**11.1 Backup (Hub).** Nightly: `C:\SAPBotHub\data` (users, grants, audit, scripts DB) and `C:\SAPBotHub\workspaces`,
`scripts`. `C:\SAPBotHub\keys` once into the password vault / encrypted backup (private key!). Restore = stop service,
restore folders, start service. Laptops: nothing mandatory — gold runs and audit are on the Hub after sync.

**11.2 Upgrade.** Hub: stop service → replace `C:\SAPBotHub\app` → `C:\SAPBotHub\.venv\Scripts\python.exe -m pip install "C:\SAPBotHub\app[hub]"` → start. Laptops path A: replace `C:\SAPBot\app` (editable install picks it up; re-run `install.ps1` if dependencies
changed). Path B: IT re-runs `-Mode Machine`. Data and enrolment are kept.

**11.3 Key rotation / lost private key.** Generate new keys only if compromised or lost: stop the service, move
`keys\policy_signing*.pem` away, run `sapbot-hub init --admin <admin>` (creates a new key pair and a new admin
token; existing users/grants stay), restart, redistribute the new public key + fingerprint; every laptop re-enrols
(§6.2). Note: `install_hub.ps1` never replaces an existing key.

**11.4 Client offboarding.** Export what the engagement requires (Cockpit → Run History / Audit export), then: Hub —
remove memberships, delete `C:\SAPBotHub\workspaces\<id>`; laptops — on Hub-governed laptops consultants are never local Admins, so IT deletes `<data>\workspaces\<id>` (or an
Admin runs `sapbot workspace purge <id>` / Cockpit *Offboard client*).

**11.5 Leaver.** `sapbot-hub user <name> --disable`, revoke grants; laptop wipe per IT process.

**11.6 Uninstall (laptop).** Remove the `sapbot` entry from `%APPDATA%\Claude\claude_desktop_config.json`, delete
the Start-menu folder `SAPBot`, delete the install folder (and `%LOCALAPPDATA%\SAPBot` / `%ProgramData%\SAPBot` on
path B), remove the `sapbot` entries from Windows Credential Manager.

---

## 12. Reference

### 12.1 Commands

| Laptop (`sapbot …`) | Purpose |
|---|---|
| `init` | create data folder + default settings (`sapbot` = `<install>\.venv\Scripts\sapbot.exe`) |
| `probe [--system SID --client NNN]` | read-only SAP checks |
| `enroll --hub URL --pubkey F --fingerprint FP [--ca-bundle F]` | join the Hub |
| `user-setup` | first run on IT-managed laptops |
| `sync` · `hub-status` | sync now · enrolment/policy/sync status |
| `workspace list\|use\|show\|create\|add-system\|purge` | client workspaces (create only in laptop mode) |
| `scripts list\|approve` · `import-sapgate` | script library |
| `run "<task>"` · `batch --excel F --script S --map PARAM=Column` | API mode · batch |
| `cockpit` · `mcp-register` · `secret set <name>` · `export-training` | UI · Claude Desktop · keys · training data |

| Hub (`python -m sapbot.hub.app …`) | Purpose |
|---|---|
| `init --admin NAME` · `fingerprint` | keys + first admin · show key fingerprint |
| `serve --cert F --key F [--ca-bundle F]` | run (normally via the service) |
| `user NAME [--role R] [--display D] [--workspace W] [--disable\|--enable] [--new-token]` · `users` · `token NAME` | users and tokens |
| `workspace create\|list\|show` · `member WS USER [--ws-role lead] [--remove]` | workspaces |
| `feature list\|set NAME on\|off [--workspace W]` · `token-cap N` | features · Claude budget |

### 12.2 Files

| Path | Content |
|---|---|
| `<data>\config\sapbot.yaml` | user settings (path A); overridden by `%ProgramData%\SAPBot\machine.yaml` (path B) |
| `<data>\data\sapbot.db` | local store: cached policy, approvals, audit queue |
| `<data>\workspaces\<id>\` | per client: `workspace.yaml`, `chroma\`, `trajectories\`, `schema_cache\`, `output\` |
| `<data>\scripts\library\|custom\|drafts` | VBS scripts |
| `<data>\logs\` | `mcp.log`, `cockpit.log`, `cli.log` |
| `C:\SAPBotHub\keys\` | `policy_signing.pem` (private, protect), `policy_signing.pub.pem` (distribute) |
| `C:\SAPBotHub\data\hub.db` | Hub database |

`<data>` = `C:\SAPBot` (path A) or `%LOCALAPPDATA%\SAPBot` (path B).

### 12.3 Network endpoints

| From | To | Port | When |
|---|---|---|---|
| Laptop | Hub `https://<hub>:8443` | 443/8443 TCP | sync, admin actions, Claude proxy |
| Hub | `api.anthropic.com` | 443 | Claude API mode |
| Claude Desktop | Anthropic (Claude app endpoints) | 443 | Cowork mode |
| Laptop (install) | `pypi.org`, `files.pythonhosted.org`, `chroma-onnx-models.s3.amazonaws.com` | 443 | install / first run |
| Laptop | SAP application servers | SAP GUI ports (32xx/33xx, router) | unchanged SAP GUI traffic |
