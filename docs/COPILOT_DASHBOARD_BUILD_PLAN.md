> **Superseded:** these are early research notes. The binding design and plan are `docs/superpowers/specs/2026-10-24-copilot-source-design.md` and `docs/superpowers/plans/2026-10-24-copilot-source.md`.

# Build Plan — Local GitHub Copilot Usage Dashboard (VS Code)

> **Purpose of this document.** This is a self-contained hand-off spec. It is written to be
> given to a **fresh Claude Code instance on a different laptop / different account**, with no
> access to the original project, and still be buildable. It describes *what* to build, the
> *architecture*, *every calculation*, *pros and cons*, and cites **official documentation** for
> every fact that can change over time.
>
> It is modelled on an existing, working tool — a 100%-local, zero-API dashboard that reads
> **Claude Code**'s JSONL session logs (`~/.claude/projects/*.jsonl`) and turns them into charts,
> developer insights, and a weekly report. **This plan ports that same idea to GitHub Copilot in
> VS Code.** The goal is the same product experience, a different data source.
>
> **Author's note to the building agent:** Copilot's on-disk log schema is **undocumented and
> changes between extension versions.** Do **not** trust any field name in this document as final.
> **Phase 0 below is mandatory** — you must empirically dump the real logs on the target machine
> and confirm the schema *before* writing the parser. Everything downstream depends on it.

---

## 1. Product summary

A single-file, local web dashboard that reads **GitHub Copilot's local logs in VS Code**, parses
them, and serves live charts + insights at `http://localhost:<port>`. **No API calls, nothing
leaves the machine.** Same philosophy as the reference tool:

- One program, standard library only where possible, works offline.
- Reads logs Copilot already writes locally; it is a **parser + aggregator + tiny web server**.
- Auto-refreshes; supports 7 / 30 / 90 / all + custom date range.
- Two tabs: **Dashboard** (usage analytics) and **Weekly Report** (standup generator).

### Design principle carried over from the reference tool
> The dashboard is nothing more than a parser over local log files. Nothing leaves the machine.

---

## 2. Non-negotiable ground truth about the data source

Unlike Claude Code (which writes one clean, stable JSONL event-per-line stream automatically),
Copilot's logging has three important properties the builder MUST internalise:

1. **Rich token data lives on the CHAT / AGENT side, not inline completions.** Inline "ghost
   text" completions are flat-rate and are *not* reliably token-logged. The good data (model,
   token counts, cost) is in **Copilot Chat / Agent** logs. Build around chat/agent.
2. **The schema is undocumented and version-fragile.** Field names differ across Copilot
   Chat extension versions. Treat the parser as defensive: tolerate missing fields, never crash
   on an unknown line, log-and-skip.
3. **Full-fidelity logging may need to be enabled.** The status-bar/basic data is always there,
   but the detailed request/response token accounting is surfaced via **trace logging** and the
   **Chat Debug View** (see §4).

---

## 3. Data sources (in priority order)

### 3.1 PRIMARY — Copilot Chat session JSON (local, per-workspace)
Copilot Chat conversation history is stored as **JSON files**, one per session, under VS Code's
`workspaceStorage`:

- **Windows:** `%APPDATA%\Code\User\workspaceStorage\<workspace-hash>\chatSessions\*.json`
- **macOS/Linux:** `~/.config/Code/User/workspaceStorage/<workspace-hash>/chatSessions/*.json`
- Each `<workspace-hash>` folder has a `workspace.json` that maps the hash → the real project
  folder path. **Use this to attribute usage per project** (the analog of Claude Code's project
  dirs). *(Source: community + VS Code issue tracker — see References. Confirm in Phase 0.)*

Variants to also scan (same relative layout, different root):
- VS Code Insiders: `Code - Insiders`
- VS Codium: `VSCodium`
- Copilot **CLI / Agent** writes **JSONL** (line-per-event, like Claude Code) — scan for these too.

### 3.2 SECONDARY (optional) — Trace logs / Chat Debug View
For per-request token counts, cost rates, and AI-credit accounting, enable trace logging:

1. Command Palette → **"Developer: Set Log Level"** → **Trace** for *GitHub Copilot* **and**
   *GitHub Copilot Chat*.
2. **"Output: Show Output Channels"** → pick the extension → this is the live stream.
3. Chat view overflow menu **`…`** → **"Show Chat Debug View"** — shows *"the raw details of each
   LLM request and response, including the full system prompt, user prompt, context, and tool
   invocation payloads."*
4. Overflow menu → **"Show Agent Debug Logs"** — *"a chronological event log of agent
   interactions during a chat session."*

Log files are stored in *"the standard log location for VS Code extensions"* — i.e. under
`%APPDATA%\Code\logs\<timestamp>\window<n>\exthost\<extension-id>\`. **Exact filenames must be
confirmed in Phase 0.** *(Source: VS Code Copilot troubleshooting docs — see References.)*

### 3.3 NOT in scope for the personal tool — Copilot Metrics API
There is an **organization/enterprise-admin** REST API (`/orgs/{org}/copilot/metrics`) giving
aggregate acceptance rate, active users, per-language stats. It is **team-level, needs admin
scope, and is not per-you-local.** Mention it in the README as a future "team edition," but the
personal dashboard uses local logs only, to preserve the zero-config, zero-network promise.

---

## 4. PHASE 0 — Mandatory schema discovery (do this FIRST, before any code)

The single biggest risk is guessing field names. On the **target machine that actually uses
Copilot**, the building agent must:

```bash
# 1. Find the chat session JSON files (Windows / Git-Bash example)
ls -R "$APPDATA/Code/User/workspaceStorage" | grep -i chatSessions
find "$APPDATA/Code/User/workspaceStorage" -path "*chatSessions*.json" | head

# 2. Pretty-print ONE recent session and study its real structure
python -c "import json,sys;print(json.dumps(json.load(open(sys.argv[1],encoding='utf-8')),indent=2)[:6000])" "<one session>.json"

# 3. Look for the token/model/cost fields. Grep candidate keys:
grep -o -E '"(model|modelId|prompt_tokens|completion_tokens|cached_tokens|cache_creation_input_tokens|tokens|usage|timestamp|requestId|result)"' "<session>.json" | sort -u

# 4. Enable Trace logging (see 3.2), run a few chat + agent prompts, then locate the exthost logs:
find "$APPDATA/Code/logs" -iname "*copilot*" -newermt "-1 hour"
```

**Deliverable of Phase 0:** a short `SCHEMA_NOTES.md` recording the *actual* keys observed, with
2–3 redacted example records. **The parser is written against `SCHEMA_NOTES.md`, not against this
plan.** Known-plausible field names to expect (confirm each): `model` / `modelId` (e.g.
`claude-sonnet-4.6`), `prompt_tokens`, `completion_tokens`, `cached_tokens`,
`cache_creation_input_tokens`, `timeToFirstToken`, `duration`, and an AI-Credit / cost block with
per-token rates. *(These field names are reported by community analyses; verify.)*

---

## 5. Architecture

Mirror the reference tool exactly — it is the most portable, zero-dependency shape.

**Stack:** Python 3.8+, **standard library only** (`http.server`, `json`, `argparse`,
`collections`), **Chart.js bundled locally** (no CDN, fully offline). Single file
`copilot_dashboard.py` + bundled `chart.umd.min.js`.

> **Why not a VS Code extension?** An extension is more "native," but it locks you to the VS Code
> runtime, needs a publish/build toolchain, and can't be a standalone always-on local page. The
> Python single-file server matches the reference product and is trivial to hand off. Keep it.

```
┌──────────────────────────────────────────────────────────────┐
│  copilot_dashboard.py  (one file, stdlib + bundled Chart.js)  │
│                                                                │
│  Scanner ─► Parser ─► Aggregator ─► ThreadingHTTPServer        │
│    │          │           │              │                     │
│    │          │           │              └─ GET /            → SPA (HTML+JS)
│    │          │           │              └─ GET /api/stats   → JSON
│    │          │           └─ per-day / per-model / per-project / per-week rollups
│    │          └─ parse_session(): tokens, model, cost, prompts, tools, timestamps
│    └─ walk workspaceStorage/*/chatSessions/*.json (+ CLI *.jsonl); cache (mtime,size)
└──────────────────────────────────────────────────────────────┘
Front-end fetches /api/stats every 15s and re-renders. 100% local.
```

### Modules (map 1:1 to the reference tool's classes)
- **`Scanner`** — walk all `chatSessions` dirs across VS Code variants; cache each file's
  `(mtime, size)`; re-parse only changed files. Resolve `workspace.json` → project name.
- **`parse_session()`** — turn one session JSON into per-request stat records. **Deduplicate by
  request/message id** (Copilot may re-log the same request; dedupe like the reference tool does
  by message ID — otherwise tokens double-count).
- **Aggregator** — daily buckets, per-model, per-project, per-week, activity heatmap (weekday×hour
  from timestamps).
- **Web server** — `ThreadingHTTPServer`; `/api/stats` returns the whole payload; `--port`,
  `--dir`, `--dump` flags like the reference tool.

---

## 6. Metrics — full mapping, portability, and exact formulas

Legend: **✅ Direct** (data exists) · **🟡 Adapt** (needs derivation/approximation) ·
**🔴 Drop/Defer** (Copilot doesn't expose the needed field cleanly).

| Reference-tool metric | Copilot port | Status | Formula / source of number |
|---|---|---|---|
| Input tokens | `prompt_tokens` summed, deduped by request id | ✅ | `Σ prompt_tokens` over range |
| Output tokens | `completion_tokens` summed, deduped | ✅ | `Σ completion_tokens` |
| Cache read tokens + hit % | `cached_tokens` | 🟡 | `Σ cached_tokens`; hit% = `cached_tokens / (prompt_tokens)` — confirm field |
| Model usage doughnut | group by `model`/`modelId` | ✅ | count & output-tokens per model |
| Estimated cost | **Premium requests / AI credits** (see §7) | 🟡 | `Σ (premium_request_count × model_multiplier)` — reframed from $ to credits |
| Prompt count + avg length | 1 per user turn; length from prompt text | ✅ | `count(user turns)`, `avg(len(prompt)/4)` for ~tokens |
| Sessions + avg duration | one JSON file = one session; first→last ts | ✅ | `count(files)`; duration = `max(ts)−min(ts)` per file |
| Activity heatmap (weekday×hour) | from request timestamps | ✅ | bucket each request ts into weekday×hour |
| Daily token bars | daily rollup | ✅ | group by `date(ts)` |
| Per-project table | via `workspace.json` path mapping | ✅ | attribute each session to its workspace |
| Interruptions / friction | cancelled / errored requests | 🟡 | only if a status/cancel field exists — else defer |
| **Lines of code AI wrote** | agent-mode edits in logs | 🟡 | Copilot **agent** edits *may* appear in Chat Debug / agent logs; parse applied edits and count added lines. Fragile — approximate, label as "est." |
| **Most-edited files** | agent edit targets | 🟡 | same source as LoC; only if edit payloads are logged |
| **Leverage ratio** (AI out ÷ you-typed) | AI `completion_tokens` ÷ human prompt tokens | 🟡 | `Σ completion_tokens / Σ (len(prompt)/4)` — human tokens estimated at ~4 chars/token |
| **Exploration vs building** | read-type vs edit-type tool calls | 🟡 | only if tool-invocation payloads are logged (Chat Debug View exposes them); else drop |
| **Git-branch breakdown** | `gitBranch` field | 🔴 | Copilot doesn't stamp branch. Alternative: shell out to `git branch --show-current` per project at scan time (approx) or drop |
| **AI-generated session titles** | Copilot chat title, if present | 🟡 | use the chat session's own title/first prompt; else derive from first user prompt |
| Weekly Report (standup) | reuse titles + per-project sessions | 🟡 | same as reference once titles resolved |
| Time & wellness (active mins, quiet hours) | from request timestamps | ✅ | 10-min activity buckets from ts; quiet hours 23:00–06:00 share; weekend share |
| AI-fluency 0–100 scores | derived heuristics | 🟡 | port the reference formulas; some inputs (corrections, verifies) may be weaker without tool logs |

**Human-token estimate (carried over):** `human_tokens ≈ len(prompt_text) / 4`
(~4 characters per token). Used for leverage ratio and "you vs AI" balance.

**Dedup rule (critical):** maintain a `seen_request_ids` set; a token record only counts once.
This exactly mirrors the reference tool's "deduplicated by message ID" behaviour and prevents
inflated totals when Copilot re-emits a request in the log.

---

## 7. Cost model — Premium Requests / AI Credits (with exact math & official caveats)

**Copilot does NOT bill per raw token to the user.** It bills in **premium requests** (legacy,
request-based plans) or **usage-based billing / AI Credits** (current model). The dashboard's
"cost" tab must reflect *this*, not a token-price estimate.

### 7.1 What counts as a premium request *(official)*
Per GitHub Docs, a request = *"any interaction where you ask Copilot to do something."* Premium
request consumption:
- **Copilot Chat:** 1 request per user prompt × model multiplier
- **Copilot CLI:** 1 request per prompt × model multiplier
- **Copilot Code Review:** 13 requests per review
- **Copilot Cloud Agent:** 1 per session (+1 per steering comment)
- **Copilot Spaces:** 1 per prompt · **Spark:** 4 per prompt · **3rd-party agents:** 1 per prompt
- **Agentic tool calls Copilot makes autonomously do NOT count** — only the prompts you send.

> Source: [Requests in GitHub Copilot — GitHub Docs](https://docs.github.com/en/copilot/concepts/billing/copilot-requests)

### 7.2 Core cost formula
```
premium_requests_used(range) = Σ over prompts [ request_units(feature) × model_multiplier(model) × discount ]

where:
  request_units(feature)  = 1 for chat/CLI, 13 for code review, 4 for Spark, etc. (table above)
  model_multiplier(model) = from the official multiplier table (see 7.3) — MUST be fetched live
  discount                = 0.9 if the prompt used auto model selection, else 1.0
```
Auto-selection discount is **10%** (a 1× model is billed at **0.9×**) *(per GitHub docs)*.

### 7.3 Model multipliers — SNAPSHOT ONLY, verify live before shipping
> ⚠️ **These numbers change frequently and GitHub has moved multipliers to *legacy* status under
> its newer usage-based billing.** Do **not** hardcode blindly. The dashboard must ship with a
> small editable `MULTIPLIERS` dict (like the reference tool's `PRICING` table) and a comment
> linking to the official page. Snapshot captured **2026-07** for reference only:

| Model (example) | Multiplier (snapshot) |
|---|---|
| Base / included model (e.g. GPT-5.2-Codex, Claude Sonnet 4.5) | 1× |
| GPT-5.3 Codex | 6× |
| Claude Opus 4.5 | 3× |
| Claude Opus 4.7 | 27× |
| Claude Opus 4.8 | 15× |

> Sources: [Model multipliers — GitHub Docs](https://docs.github.com/en/copilot/reference/copilot-billing/request-based-billing-legacy/model-multipliers-for-annual-plans)
> (authoritative, fetch at build time). Third-party snapshots corroborate but are secondary.

### 7.4 Plan allowances *(official, for the "budget used %" gauge)*
- **Copilot Pro:** 300 premium requests / month
- **Copilot Pro+:** 1,500 premium requests / month
- Free / Business / Enterprise: fetch current values from the billing docs at build time.

**Budget gauge formula:** `budget_used_% = premium_requests_used_this_month / plan_allowance × 100`.
Make `plan_allowance` a config constant the user sets to their plan.

> Because multipliers/allowances drift, the dashboard should render cost as **"≈ N premium
> requests (est.)"** with a visible "verify against your GitHub billing page" footnote — the same
> honesty the reference tool uses for its $ estimates.

---

## 8. UI / pages (reuse the reference tool's layout wholesale)
- **Sticky glass header**, KPI cards with accent colors, gradient Chart.js charts, dark theme,
  responsive, a live status pill ("reading local Copilot logs").
- **Dashboard tab:** KPI cards (tokens, requests, est. premium requests, sessions, prompts,
  leverage), daily token bars, model doughnut + table, activity heatmap, per-project table with a
  click-through project modal, time & wellness panel, AI-fluency radar.
- **Weekly Report tab:** week selector, "what you worked on" grouped by project from session
  titles, WoW deltas, exploration-vs-building gauge (if tool logs available), **Copy as Markdown**.
- **Exports:** copy text summary, CSV of the daily table, JSON of `/api/stats`.
- **Footer:** "100% local · reads Copilot logs from `<paths>` · refreshes every 15s · premium-
  request counts are estimates, verify on your GitHub billing page."

---

## 9. Pros & cons of this approach

### Pros
- **Same proven product**; only the parser changes — fast to build on top of the reference UI.
- **100% local, zero network, zero API cost** — preserves the whole value proposition.
- Copilot chat/agent logs **do** carry model + token + (increasingly) cost/credit data → the core
  KPIs are genuinely portable.
- **Premium-request / AI-credit** framing is arguably a *better* cost story for Copilot users than
  raw token $ (it matches what GitHub actually bills).
- Works across VS Code / Insiders / VSCodium and Copilot CLI with one scanner.

### Cons / risks
- **Undocumented, version-fragile schema** — the #1 risk. Mitigate with Phase 0 + defensive parser
  + a pinned `SCHEMA_NOTES.md` and a schema-version guard.
- **Full token/cost data may require enabling Trace logging** — extra one-time user setup vs
  Claude Code's automatic logs. Document it in SETUP.
- **Inline completions aren't token-logged** — "leverage/LoC from completions" is not available;
  only chat/agent contributes. Be explicit that this measures *Copilot Chat/Agent*, not ghost-text.
- **Some signature metrics degrade:** git-branch attribution, precise LoC-written, tool-call
  exploration/building depend on fields Copilot may not log → mark as "est." or defer.
- **Prior art exists** (see §12) — differentiate on product polish (weekly report / wellness /
  fluency), not on raw token counting.
- **Multipliers/allowances change often** — never hardcode; make them config + cite live docs.

---

## 10. Build phases (milestones for the building Claude Code)
1. **Phase 0 — Schema discovery** (§4). Produce `SCHEMA_NOTES.md`. *Gate: do not proceed without
   real sample records.*
2. **Phase 1 — Scanner + parser + `/api/stats --dump`.** Prove tokens/model/sessions/projects
   parse correctly from real files. CLI only, JSON output.
3. **Phase 2 — Web server + Dashboard tab** (KPIs, daily bars, model doughnut, project table,
   heatmap). Reuse reference HTML/JS.
4. **Phase 3 — Cost tab** (premium requests, multipliers dict, budget gauge) with live-doc caveat.
5. **Phase 4 — Weekly Report tab** (titles, per-project grouping, Copy-as-Markdown).
6. **Phase 5 — Wellness + AI-fluency panels** (timestamps → active mins; port fluency formulas).
7. **Phase 6 — Polish:** date ranges (7/30/90/all + custom), auto-refresh, exports, SETUP.md,
   auto-start at logon, cross-variant scanning.

---

## 11. Acceptance criteria
- [ ] Runs with `python copilot_dashboard.py`, opens `http://localhost:<port>`, **no pip installs,
      no network calls** (verify with a network monitor / offline run).
- [ ] Parses real Copilot chat session JSON on the target machine without crashing on unknown
      fields; unknown lines are skipped, not fatal.
- [ ] KPI totals (tokens, requests, sessions) reconcile within reason against the Chat Debug View
      for a sample day.
- [ ] Token totals are **deduped by request id** (no double counting).
- [ ] Cost tab shows premium-request estimate using an **editable** multiplier table + a "verify
      on GitHub billing" note; numbers match §7 formulas.
- [ ] Per-project attribution works via `workspace.json` mapping.
- [ ] Date-range selector applies to every chart/table.
- [ ] `SCHEMA_NOTES.md` committed, documenting the real observed schema + extension version.

---

## 12. Prior art (be aware, differentiate — do not reinvent the parser blindly)
Several tools already read Copilot local logs for token/cost estimates. Study them for the real
log schema, then build the *product* on top:
- **rajbos/ai-engineering-fluency** (formerly github-copilot-token-usage) — reads local session
  logs, shows token usage + "fluency scores," and already supports **both Copilot and Claude Code**
  + CLI/Agent. Closest overlap; a good schema reference.
- **Token-Track**, **github-copilot-token-tracker**, **UncleBats/github-copilot-token-usage** —
  status-bar token/cost estimators from local debug logs.

Our differentiator: the standalone, always-on **local web dashboard** with the **Weekly Report,
wellness, and reflection** experience — not the raw counting.

---

## 13. Official references (fetch live at build time — these numbers drift)
- Requests in GitHub Copilot (what counts as a premium request):
  https://docs.github.com/en/copilot/concepts/billing/copilot-requests
- Model multipliers (authoritative table):
  https://docs.github.com/en/copilot/reference/copilot-billing/request-based-billing-legacy/model-multipliers-for-annual-plans
- Troubleshoot AI in VS Code (log locations, Set Log Level → Trace, Chat Debug View, Agent Debug
  Logs): https://code.visualstudio.com/docs/copilot/troubleshooting
- Optimize AI credit usage in VS Code:
  https://code.visualstudio.com/docs/agents/guides/optimize-usage
- Copilot Chat history location (workspaceStorage/chatSessions) — community/issue references:
  - https://github.com/orgs/community/discussions/69740
  - https://github.com/orgs/community/discussions/129888
  - VS Code issue #285059, #291897 (chatSessions JSON behavior)

> **Reminder to the building agent:** VS Code and Copilot ship weekly. Before finalizing the parser
> and the multiplier table, re-fetch the docs above and re-run Phase 0 on the target machine. This
> plan captures the approach and the math; the *exact field names and multiplier values* must be
> confirmed against the live system.
