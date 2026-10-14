# SAPBot Codebase: Comprehensive Architectural & Technical Review

**Review Date:** October 15, 2026  
**Target Codebase:** `C:\SAPBot\app\`  
**Scope:** Core Architecture, Tool Executor, SAP GUI COM Bridge, Memory/RAG, Security Governance, Streamlit Cockpit, FastAPI Hub Server, and Test Suite  

---

## 1. Executive Summary & Architecture Scorecard

The `C:\SAPBot` repository represents an **exceptionally mature, enterprise-grade autonomous SAP operator**. It implements a complete defense-in-depth security model, physical client isolation, cryptographic policy enforcement, an on-device RAG engine, and a dual-tier Teacher-Student distillation pipeline.

### Architectural Health Scorecard

| Subsystem | Key Files | Code Quality | Robustness | Enterprise Safety | Overall Grade |
| :--- | :--- | :---: | :---: | :---: | :---: |
| **Core Agent Loop & Routing** | `loop.py`, `llm.py`, `router.py`, `batch.py` | ★★★★☆ | ★★★★☆ | ★★★★★ | **A-** |
| **SAP Execution & COM Engine** | `executor.py`, `bridge.py`, `worker.py`, `fake.py` | ★★★★★ | ★★★★☆ | ★★★★★ | **A-** |
| **Security & Governance** | `policy.py`, `governance.py`, `store.py`, `signing.py` | ★★★★★ | ★★★★★ | ★★★★★ | **A** |
| **Memory & Trajectory RAG** | `rag.py`, `trajectory.py`, `embeddings.py`, `schema_cache.py` | ★★★★☆ | ★★★★☆ | ★★★★★ | **B+** |
| **Learning & Distillation** | `distill.py`, `train_qlora.py` | ★★★★☆ | ★★★☆☆ | ★★★★☆ | **B** |
| **UI, Hub Server & Test Suite** | `ui/app.py`, `hub/app.py`, `conftest.py`, `test_*.py` | ★★★★★ | ★★★★★ | ★★★★★ | **A** |

---

## 2. Deep Dive by Subsystem

### 2.1 Core Agent Loop & Model Routing (`sapbot/agent/`)

#### Architecture Highlights
- **ReAct Loop with Crash Safety**: `AgentLoop.run()` wraps execution in a strict `try ... finally` block that guarantees `task_end` is called even during unhandled exceptions, preventing orphaned tasks and locked database records.
- **Fail-Safe Router (`router.py`)**: The local model is only deployed when **all four conditions** hold:
  1. Features `local_model` and `router` are enabled for the workspace.
  2. Data policy is satisfied (workspaces marked `local-only` reject cloud models at the gate).
  3. ChromaDB finds a similar gold trajectory ($\ge 0.75$ cosine similarity).
  4. The workspace eval harness key `eval_pass:<ws>:<tcode>` equals `1`.
  Otherwise, it gracefully defaults to Claude Enterprise.
- **Batch Processing with State Checkpoints (`batch.py`)**: Mass runs from Excel/CSV persist a JSON checkpoint after each row. Interrupted batches resume without re-running completed rows. Agent loops in batch mode are strictly barred from production systems.

#### Critical Observations & Gaps
1. **DPO Pair Leaking Race Condition (`loop.py`)**: When escalating from local model to Claude, `pending_dpo` captures the local failure, but the `chosen` completion is only bound if Claude's very next turn emits a tool call. If Claude's first turn is a pure text analysis or clarification, `pending_dpo` is dropped.
2. **Missing Retry Logic (`llm.py`)**: Neither `ClaudeLLM` nor `OpenAICompatLLM` incorporates exponential back-off with jitter for transient HTTP 429 or 503 errors.
3. **Escalation Discards Structured History**: The escalation handover serializes message history into a truncated 6,000-character plain-text summary rather than passing structured message blocks.

---

### 2.2 SAP Execution & Windows COM Layer (`sapbot/sap/` & `sapbot/tools/`)

#### Architecture Highlights
- **Single-Threaded Apartment (STA) Isolation (`worker.py`)**: Solves Windows COM thread affinity issues. COM objects (`win32com.client`) cannot cross thread boundaries without explicit marshaling. `ComWorker` runs on a single background daemon thread, shielding COM from Streamlit script re-runs.
- **Session Dedication (`bridge.py`)**: `open_own_session` spawns its own session (`ses[1]`) via `base.CreateSession()`, allowing the human consultant to keep their working window in `ses[0]`.
- **Hybrid ALV Grid Extraction**: Grids below the threshold (< 250 rows) are read via direct COM cell indexing. Grids exceeding the threshold trigger SAP's native unconverted text export (`&MB_EXPORT` -> `&PC` -> Text with Tabs -> UTF-8), completely eliminating COM thread freezing on large tables.
- **Offline Virtualization (`fake.py`)**: Comprehensive mock simulating SAP GUI COM objects, enabling 100% of the test suite to run in headless Linux/CI pipelines without a live SAP installation.

#### Critical Bugs & Findings
1. **`sapui.vbs`: `Key(n)` Always Targets `wnd[0]`**:
   ```vbscript
   Sub Key(n)
     s.FindById("wnd[0]").SendVKey n: W
   End Sub
   ```
   *Bug*: When a modal popup (`wnd[1]`) is open, targeting `wnd[0]` throws a COM error because `wnd[0]` is disabled. Must be changed to `s.ActiveWindow.SendVKey n`.
2. **Warning Auto-Ack Modal Window Crash (`bridge.py` line 371)**:
   ```python
   ses.FindById("wnd[0]").SendVKey(0)
   ```
   *Bug*: Sending Enter to `wnd[0]` during a modal popup warning crashes with `Control is disabled by modal dialog`. Must use `self._active_window(ses).SendVKey(0)`.
3. **`MessageParameter` COM Signature Mismatch (`bridge.py` line 146)**:
   In real SAP GUI Scripting COM, `MessageParameter` is an indexed method (`sb.MessageParameter(1..8)`), not a string property. `prop(sb, "MessageParameter")` returns empty strings on live SAP instances.
4. **VBS File ASCII Encoding Corruption (`library.py` line 261)**:
   Composed scripts are written using `encoding="ascii", errors="replace"`. Non-ASCII characters (e.g., German umlauts `ä, ö, ü, ß`, accented characters in material descriptions) are replaced with `?`, corrupting search inputs.

---

### 2.3 Memory, RAG & Distillation Subsystem (`sapbot/memory/` & `sapbot/learning/`)

#### Architecture Highlights
- **Strict Physical Isolation**: Each workspace maintains its own ChromaDB storage (`workspaces/<id>/chroma/`). Vector indices are never shared across clients.
- **Deterministic Embedder Fallback (`embeddings.py`)**: Employs ONNX Runtime `all-MiniLM-L6-v2` (no PyTorch dependencies). For air-gapped environments, it features a deterministic BLAKE2b feature hasher that tokenizes n-grams into a normalized 384-dim vector.
- **Closed-Loop Verification Gate (`trajectory.py`)**: Write tasks are only certified "Gold" if the document number extracted from the SAP status message is subsequently verified by reading it back inside a display transaction (`display_tcodes`) or read-only grid query.

#### Critical Observations & Gaps
1. **Raw String Truncation JSON Crash (`rag.py` lines 46 & 76)**:
   ```python
   # Indexing:
   "serialized_steps_json": json.dumps(steps, ensure_ascii=False)[:20000]
   # Retrieval:
   "steps": json.loads(meta.get("serialized_steps_json") or "[]")
   ```
   *Bug*: Slicing raw JSON text at 20,000 characters cuts mid-token. Succeeded tasks with many steps will throw unhandled `JSONDecodeError` during RAG retrieval. Must truncate the list of steps prior to serialization.
2. **DPO Training Target Mismatch (`train_qlora.py` line 64)**:
   The rejected completion injects execution error strings (`{"name": ..., "error": ...}`). TRL's `DPOTrainer` expects chosen and rejected completions to follow the model's natural assistant output distribution. Injecting error messages teaches the student model to hallucinate synthetic error formats.
3. **Local-Only Data Leak in Distillation Export (`distill.py` lines 40–54)**:
   Exporting training data across all workspaces (`workspace=None`) bundles `local-only` trajectories into `sft_gold.jsonl` alongside public client data, risking inadvertent cloud upload.

---

### 2.4 Security, Governance & Human Approval (`sapbot/security/`)

#### Architecture Highlights
- **Deterministic 7-Gate Policy Engine (`policy.py`)**: Every tool call is intercepted and validated against channel features, workspace authorization, wrong-window checks (verifying live SAP session SID/client against workspace declarations), hard-stop T-codes, navigation allowlists, script signatures, and production write grants.
- **Cryptographic Policy Verification (`signing.py`)**: Laptops in Hub mode consume signed Ed25519 policy bundles. Replay timestamps, expiration checks, and enrollment latches prevent privilege tampering.
- **Four-Eyes Elevation Rule (`store.py`)**: Consultants cannot approve their own elevation requests. Only an Engagement Lead or Firm Admin can grant access.

#### Critical Vulnerabilities & Gaps
1. **Secret Regex Truncation Leak (`masking.py` line 13)**:
   ```python
   (re.compile(r"(?i)\b(password|passwort|pwd|token|api[_-]?key|secret)\b\s*[:=]\s*\S+"), r"\1=<SECRET>")
   ```
   *Vulnerability*: `\S+` stops at whitespace. Values like `password: "My Secret Key 123"` redact only the first word, leaking `"Secret Key 123"` into trajectory logs.
2. **Client System Clock Manipulation**:
   Expiration for Hub policies and elevation grants relies on the local OS clock. A user with local Windows time modification rights can alter their system time backward to keep expired grants active.
3. **Admin Self-Approval Control**:
   `store.py` line 200 explicitly exempts admins from the four-eyes rule (`and me.get("role") != "admin"`), creating a compliance exception for regulated environments (SOX/SOC2).

---

### 2.5 Cockpit UI, Hub Server & Test Suite (`ui/`, `hub/`, `tests/`)

#### Architecture Highlights
- **Fiori Horizon Theme**: Implements SAP Fiori design standards via injected CSS (`fiori.css`), status badges, and contextual shellbars.
- **Non-Blocking Telemetry Fragments**: Uses Streamlit `@st.fragment(run_every="1s")` to stream thoughts, tool calls, and status bars live from background daemon worker threads (`JobManager`) without full-page re-renders.
- **Keyless Claude Proxy (`hub/app.py`)**: Consultant laptops hold no Anthropic API keys. The Hub proxies requests, verifies bearer tokens, enforces monthly per-user token quotas, and manages credentials via Windows Credential Manager.
- **Regression Suite (`test_review_fixes.py`)**: Thoroughly tests command injection guards, debugger bypasses (`/H`), `/*` transaction jumping, and AST scanner evasion tricks.

---

## 3. Top 10 Priority Action Items

| Priority | Category | File | Description & Fix |
| :---: | :--- | :--- | :--- |
| **P0** | Bug | `scripts/vbs/sapui.vbs` | Change `s.FindById("wnd[0]").SendVKey n` to `s.ActiveWindow.SendVKey n` so popups don't crash scripts. |
| **P0** | Bug | `sap/bridge.py` | Change `ses.FindById("wnd[0]").SendVKey(0)` to `self._active_window(ses).SendVKey(0)` in warning auto-ack. |
| **P0** | Bug | `memory/rag.py` | Truncate the Python `steps` list before JSON serialization to eliminate `JSONDecodeError` during RAG search. |
| **P1** | Security | `security/masking.py` | Support quoted strings in secret scrubbing regex to prevent partial password leaks in logs. |
| **P1** | Security | `learning/distill.py` | Isolate `local-only` workspaces into separate export folders so private schemas are never bundled into shared training sets. |
| **P1** | Bug | `sap/bridge.py` | Query `sb.MessageParameter(i)` (indexed 1..8) to correctly capture status bar parameters on real SAP instances. |
| **P2** | Data | `scripts/library.py` | Switch VBS file writing from `ascii` to `utf-8-sig` to preserve German umlauts and international characters. |
| **P2** | ML | `agent/loop.py` | Save `pending_dpo` pairs on `task_end` so escalation pairs aren't lost if Claude's first turn has no tool calls. |
| **P2** | ML | `learning/train_qlora.py` | Format DPO rejected completions as standard assistant tool-call messages without embedding error JSON strings. |
| **P3** | Perf | `tools/executor.py` | Cache `session_info` during policy gating to eliminate 3–4 redundant COM round-trips per action. |

---

## 4. Verdict

The `C:\SAPBot` implementation is **already a near-production-grade system** that significantly exceeds typical agentic wrappers. Its architectural separation between the **Execution Layer (COM Worker)**, **Governance Engine (Deterministic Policy)**, and **Cognitive Layer (Claude/Qwen ReAct)** is robust and well-conceived. Addressing the five P0/P1 fixes above will make the system production-ready.
