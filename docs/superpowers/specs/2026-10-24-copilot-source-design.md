# GitHub Copilot as a second source — design

Status: draft for approval · Date: 2026-10-24 · Repo: `chandan12ar/dev-token-dashboard-multi-ai`,
branch `copilot-source`

This work lives in its own repo, a copy of `dev-token-dashboard` with its full history. The
original `dev-token-dashboard` repo is **left exactly as it is** (D9).

## 1. Intent

AI Fluency Insights today reads only Claude Code's local logs. The owner also uses
**GitHub Copilot in VS Code** every day: an enterprise account at the office, a personal
one at home. They want the same dashboard to cover Copilot, so that one local page answers
"how am I using my AI coding tools?", without disturbing the Claude Code numbers that
already work.

The owner's main worry is **mismatched calculations**. If Claude and Copilot data are
mixed into the same formulas, the numbers stop meaning anything. So the core idea is:

- **Each tool keeps its own numbers.** Claude Code's calculations stay as they are. The one
  exception is date ranges, which become calendar days (D8). Copilot gets the same sections
  and the same calculations, run only on Copilot's data.
- **All AI is a separate summary** that combines only things that mean the same thing in
  both tools.
- **Codex comes next**, so adding a third tool should mean writing one more log reader,
  not reworking the dashboard.

What success looks like: open the dashboard, flip the switch to GitHub Copilot, and see
Overview, Time & Focus, Projects, Weekly Report and the rest filled from Copilot's own logs
with honest labels. Flip to All AI and see total tokens, total usage and model usage across
both tools.

## 2. Goals, non-goals, anti-goals

**Goals**
- Read GitHub Copilot's local logs (VS Code Copilot Chat in ask/edit/agent mode, plus
  Copilot CLI) and show them in the existing dashboard.
- A three-way tool switch: **Claude Code / GitHub Copilot / All AI**.
- Every existing section gets a Copilot version computed from Copilot data only.
- An All AI summary of combined usage.
- Works for **any Copilot plan or account type** (enterprise, business, personal, free).
  The dashboard reads whatever Copilot wrote to disk and has no plan setting.
- Stays 100% local, zero tokens, Python 3.8+ standard library, works offline.
- Structured so Codex can be added later as one more reader.

**Non-goals (not now)**
- Inline "ghost text" completions. Copilot does not log them with token counts.
- GitHub's organisation/enterprise Copilot Metrics API (admin-only, team-level, needs network).
- Copilot in JetBrains or Visual Studio. The log checker detects JetBrains files, but they
  are not parsed yet.
- Reconciling to the cent with GitHub's invoice. Credits are shown as the logs report them.
- A "% of monthly allowance" gauge. That needs the plan, and the owner wants no plan setting.
- Codex itself.
- Multi-user or team rollups.

**Anti-goals: failures even if everything else works**
- **Any Claude Code number changes**, other than through the calendar-day range (D8). For
  "All time" and for custom from–to ranges, the Claude view must be identical to the
  original dashboard's.
- **Mixing tools in one formula.** For example adding dollars to credits, or summing time so
  that 10 minutes spent with both tools counts as 20.
- **Fake zeros.** A Copilot card with no data must say so, never show `0`.
- **Anything leaving the machine.**
- **Basic Copilot stats requiring a setting.** The default logs must already give useful
  numbers. Settings only add precision.
- **Crashing or going blank** when Copilot changes its log format. Unknown fields are skipped
  and counted.

## 3. Decisions made with the owner

| # | Decision | Who |
|---|---|---|
| D1 | The tool switch sits **in the top bar, next to the date range** (mockup option B). The sidebar stays as today. | owner |
| D2 | Each tool has its own **accent colour**: Claude Code orange (today's `--acc`), GitHub Copilot violet, All AI teal. The colour change is how you always know whose numbers you're seeing. | owner (Claude suggested) |
| D3 | Account type doesn't matter. Capture whatever Copilot logs locally, on any plan. No plan setting. | owner |
| D4 | Claude Code side unchanged. Copilot gets the same sections, computed separately. | owner |
| D5 | All AI shows total tokens, total usage, model usage across both tools. | owner |
| D6 | Codex follows later. Build so a new tool = one new reader. | owner |
| D7 | Fast track: build now from the research and the real Copilot samples on the home laptop. Check against the office laptop's report (Monday) and adjust the reader then. | owner |
| D8 | **"Last 7 / 30 / 90 days" means calendar days**: today and the N−1 days before it, in local time. This applies to all three tools. Custom from–to dates stay as they are, for any week or single day. Today's code instead takes the last N days *that had activity* (`alldays[-days:]` in the `/api/stats` handler). That gives the wrong window for a tool used on only some days, such as Copilot at home. | owner |
| D9 | The multi-AI dashboard is a **separate repo**, `dev-token-dashboard-multi-ai`. The original `dev-token-dashboard` is not changed. | owner |

The owner approved these proposals as a set (2026-10-24):

| # | Proposal |
|---|---|
| P1 | **Translate, don't fork.** Copilot logs are translated into the same internal shape the Claude Code logs produce, so every existing calculation runs unchanged on Copilot data, each tool on its own copy. |
| P2 | **All AI has four pages:** Overview, Time & Focus, Tools & Models, Projects. Per-tool pages (Weekly Report, My Adoption, Flow, Quality, Value, Advisor, Data Health) are dimmed in the sidebar while All AI is selected. |
| P3 | **Cost is never added across tools.** Claude shows its estimated $, Copilot shows AI credits, All AI shows both side by side. |
| P4 | **No fake zeros.** A Copilot card without data shows "Not available for Copilot" plus a one-line reason. Estimated numbers carry an "approx." badge, reusing the existing confidence badges. |
| P5 | **Copilot CLI counts as GitHub Copilot** (same account, same credits), with one chart splitting VS Code vs CLI. |
| P6 | **Exact tokens via an opt-in setup command** (like `--setup-notifications`). It asks first, then turns on VS Code's official OpenTelemetry file export. Without it, per-prompt numbers from chat history are used and marked approx. The Copilot Data Health page shows which mode is active. |

## 4. Where Copilot's data comes from

Verified 2026-10-24 on the home laptop (VS Code 1.139, Copilot Chat 0.67, Copilot CLI
1.0.100) with `tools/copilot_log_check.py`, plus research:

- rajbos/ai-engineering-fluency, `docs/logFilesSchema/*`
- vshulcz/deja-vu, `docs/registry/copilot-chat.md`
- VS Code docs, "Monitor agent usage with OpenTelemetry"

**Copilot's formats are undocumented and change between releases.** Field names here are
the best current knowledge, not a contract. The office report is due Monday.

`<User>` is the VS Code user folder:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\Code\User` |
| macOS | `~/Library/Application Support/Code/User` |
| Linux | `~/.config/Code/User` |

The same layout appears under `Code - Insiders`, `VSCodium`, `profiles/<id>/` and
`~/.vscode-server/data/User`. Discovery should cover the same roots the checker scans.

| Store | Path | What's in it | Tokens? |
|---|---|---|---|
| **Chat sessions, incremental** | `<User>/workspaceStorage/<hash>/chatSessions/<sessionId>.jsonl` | JSONL mutation log. `kind:0` is a full snapshot, `kind:1` sets the value at path `k`, `kind:2` pushes onto the array at `k` (optional index `i`), `kind:3` deletes. Replay in order to rebuild the session object. Per request: `timestamp`, `modelId`, `message.text`, `agent.id`, `result.timings.{firstProgress,totalElapsed}`, `elapsedMs`, `result.details` (e.g. "GPT-5.6 Luna • 0.5 credits"), `promptTokens`, `completionTokens`, `copilotCredits`, `promptTokenDetails[].{category,percentageOfPrompt}`, `result.metadata.toolCallRounds[].{modelId,timestamp,toolCalls[].{name,arguments,id}}`. Snapshot: `creationDate`, `inputState.mode.kind` (agent/ask/edit), `inputState.selectedModel.identifier`. | **Approx.** Per-request prompt/completion tokens. Agent turns may record only the last LLM call. Credits per request look exact. |
| Chat sessions, whole-file | same folder, `*.json` | Older builds. The same `requests[]` shape in one JSON document. Tokens at `result.metadata.promptTokens/outputTokens`, or `result.usage`. | Approx. |
| Empty-window chats | `<User>/globalStorage/emptyWindowChatSessions/*` | Chats with no folder open. Same formats. | Approx. |
| **Transcripts** (VS Code 1.137+) | `<User>/workspaceStorage/<hash>/GitHub.copilot-chat/transcripts/<sessionId>.jsonl` | Event log: `session.start` (`copilotVersion`, `vscodeVersion`, `startTime`), `user.message`, `assistant.message` (`toolRequests[]`), `assistant.turn_start/turn_end`, `tool.execution_start` (`toolName`, `arguments`), `tool.execution_complete` (`success`). On some machines (VS Code Server 1.137) this is the **only** chat store. | **None** |
| Debug logs | `.../GitHub.copilot-chat/debug-logs/<sessionId>/main.jsonl` | With the experimental "Agent Debug Log › File Logging" setting on: `llm_request` events with exact `inputTokens`, `outputTokens`, `cachedTokens`, `model`, `copilotUsageNanoAiu`, `ttft`. Otherwise only `session_start`. | Exact, when on |
| **OpenTelemetry file** | path from `github.copilot.chat.otel.outfile` | Official and documented. With `otel.enabled` + `exporterType: "file"`: `chat` spans with `gen_ai.usage.input_tokens/output_tokens/cache_read.input_tokens/cache_creation.input_tokens`, `gen_ai.request.model`, `gen_ai.conversation.id`, `copilot_chat.time_to_first_token`, and `execute_tool` spans. No prompt content by default. | **Exact**, when on |
| Session store DB | `<User>/globalStorage/github.copilot-chat/session-store.db` (SQLite, WAL) | `sessions(id, cwd, repository, branch, summary, …)`, `turns`, `session_files`. Only recent sessions. | None |
| Workspace map | `<User>/workspaceStorage/<hash>/workspace.json` | `{"folder": "file:///…"}` or `{"workspace": "…code-workspace"}`. The project folder. | — |
| **Copilot CLI** | `~/.copilot/session-state/<id>/events.jsonl` | `session.start` (`context.{cwd,gitRoot,branch,repository}`, `copilotVersion`), `user.message`, `assistant.message`, `tool.execution_*`, and **`session.usage_record` per LLM call** with exact `inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`, `reasoningTokens`, `model`, `copilotUsage.totalNanoAiu`, `timeToFirstTokenMs`, `duration`, `initiator`. `session.usage_checkpoint` / `session.shutdown` carry **cumulative** totals. | **Exact** |

Credits: `nanoAiu / 1e9 = AI credits`. For example, 246,088,000 nano-AIU = 0.246 credits.
`copilotCredits` in chat sessions is already in credits.

The home-laptop samples cover the incremental chat sessions, empty-window chats, one
transcript, one debug log with only `session_start`, the session store, and one CLI
session. Builders should copy the *shape* of these into test fixtures with made-up values.
The real files stay on the machine. The checker report at
`C:\Users\<user>\AppData\Local\Temp\claude\…\scratchpad\report_this_laptop.md` lists the
observed field paths. Re-run `python tools/copilot_log_check.py` to regenerate it.

## 5. How it works

### 5.1 One reader per tool, one shared engine (P1)

Today: `Scanner` reads `~/.claude/projects/**/*.jsonl`, `parse_entry()` folds each line
into a `Stats` object, and every `build_*()` function plus `stats_to_json()` work from
`Stats`.

New:
- A **`CopilotScanner`** with the same interface as `Scanner`: `scan()` with a per-file
  `(mtime, size)` cache, and `build_stats(min_day, max_day) -> Stats`.
- Inside, a **translator** turns each Copilot session into the same event shape
  `parse_entry()` already understands. That means `user` entries with a prompt and a
  timestamp, `assistant` entries with `message.usage`, `model` and `tool_use` blocks, and
  tool results with an error flag. These go through the **existing** `parse_entry()` into a
  **separate** `Stats` object.
- Every existing calculation then works on Copilot automatically: turns, tool pairing,
  read-vs-write mix, lines of code from file-writing tools, command kinds, heatmap,
  10-minute activity buckets, sessions, projects, weeks, adoption, flow, quality, value.
  Copilot's numbers can never leak into Claude's `Stats`, or the reverse.

Small, explicit hooks where Copilot differs. The builder decides the exact form:
- **Tool vocabulary.** Add Copilot's tool names to the existing category sets, rather than
  renaming them, so charts show Copilot's real names:
  - `READ_TOOLS`: `read_file`, `grep_search`, `file_search`, `semantic_search`, `list_dir`,
    `fetch_webpage`, …
  - `WRITE_TOOLS`: `create_file`, `replace_string_in_file`, `multi_replace_string_in_file`,
    `apply_patch`, `insert_edit_into_file`, `edit_notebook_file`, …
  - `CMD_TOOLS`: `run_in_terminal`
  - Copilot CLI's names: shell, view, edit, create, …

  These names never collide with Claude's.
- **Lines of code.** Count added lines from each write tool's arguments:
  - `create_file.content`
  - `replace_string_in_file.newString`
  - `multi_replace_string_in_file.replacements[].newString`
  - the `+` lines of `apply_patch.input`
  - for `insert_edit_into_file.code`, drop the `...existing code...` markers

  Skip calls whose result failed.
- **Cost.** The translator puts Copilot's **credits** where Claude's estimated dollars go:
  per-request `copilotCredits`, or `totalNanoAiu / 1e9` for CLI and debug-log calls. So in
  the Copilot `Stats`, every "cost" field is in credits. The payload carries
  `"cost_unit": "credits"` (`"usd"` for Claude), and the UI formats and labels by that.
  Requests with no credit data contribute nothing and are counted in Data Health; credits
  are never guessed from model prices.
- **Premium requests.** Some accounts may report premium requests instead of credits, for
  example enterprise plans still on request-based billing. `result.details` then reads like
  "Claude Opus 4.5 • 3x", a multiplier, not "• 0.5 credits". The unit is decided **per
  record from what the log says**. Premium requests are tallied in their own counter (one
  user prompt × the multiplier) and shown as a separate "premium requests" figure. They are
  **never added to credits**. A machine with both shows both. This follows D3: show
  whatever the account's logs report. *(Claude's call.)*
- **Token convention.** Claude's `input` excludes cache tokens: cache read and cache write
  are separate fields, and the cache hit rate is cache_read / (cache_read + input). The
  translator **normalises Copilot to the same convention** wherever the source gives a split:
  - CLI `usage_record`, OTel and debug logs report input **including** cached tokens, so
    `input = inputTokens − cacheReadTokens − cacheWriteTokens`, floored at 0.
  - Chat-session `promptTokens` has no split. The whole prompt goes to `input`, cache stays
    unknown, and the session is flagged "no cache split".

  Copilot's cache hit rate is computed only over sessions that have a split. With fewer than
  half the range's sessions split, it shows as approx.; with none, "Not available for
  Copilot". *(Claude's call.)*
- **Project-instructions check.** For Copilot, look for `.github/copilot-instructions.md`
  or `AGENTS.md`, instead of `CLAUDE.md` / `AGENTS.md`.

### 5.2 Which store wins: never count anything twice

The same chat can appear in several stores. The rules, in order:

1. **Session identity is the session id.** It's shared by the chat-session file name, the
   transcript file name, the debug-log folder name, and the session-store row. Each session
   is translated **once**.
2. **Turns, prompts, timestamps, tools:**
   - Use the chat-session file (incremental or whole-file) when it exists.
   - Otherwise use the transcript. On VS Code Server 1.137 that's the only store.
   - Transcripts can still add tool `success` flags to a chat-session translation.
3. **Tokens per session, best source first:**
   - OTel `chat` spans matching `gen_ai.conversation.id`, then
   - debug-log `llm_request` events, then
   - chat-session per-request `promptTokens` / `completionTokens` (approx.).

   **Exactly one source per session**, never a sum of two. The chosen source is recorded so
   Data Health can say "exact" or "approx." per session.
4. **Copilot CLI:**
   - Sum `session.usage_record` events, one per LLM call.
   - Use `usage_checkpoint` / `shutdown` totals only for a session with no usage records,
     and then only the **last** snapshot. They're cumulative.
   - **CLI sessions started from VS Code chat** (VS Code's Copilot CLI agent host). The
     once-per-session rule from 1 applies across VS Code and CLI stores too. If a CLI
     `events.jsonl` and a VS Code chat file share a session id, they are one session, and
     the CLI usage records win for tokens because they're exact. For the VS Code vs CLI
     split (P5), a session counts where it was **started**: `session.start.context.hostType`
     when present, otherwise the store it came from. *(Claude's call.)*
5. **Deduplicate inside a store too.** For example, a usage record seen twice by
   `usage.apiCallId`, or a request by `requestId`. This mirrors the Claude side's
   message-id dedupe.

### 5.3 Projects, branches, titles

- **Project:**
  - VS Code: `workspace.json` folder URI decoded to a path.
  - CLI: `session.start.context.cwd`, or `gitRoot`.
  - Empty-window chats: project "(no folder)".
  - The label follows the Claude side's style. The real path goes in `project_cwd`.
- **Branch:**
  - CLI: `session.start.context.branch`.
  - VS Code: session-store `sessions.branch` when the session is there, otherwise "(none)".
- **Session title** (for Weekly Report and the project deep-dive), first available of:
  - the chat's own title field, if present
  - session-store `summary`
  - the first prompt, trimmed to about 60 characters

### 5.4 Server and payload

- `/api/stats?source=claude|copilot|all`. Missing `source` means `claude`, so old
  bookmarks and the `--dump` output keep working.
- **Date window (D8).** For `days=N`:
  - the current window runs from today − (N−1) through today, local time
  - the previous window, used for the Δ deltas, is the N calendar days before that
  - both windows are the same for every source

  All AI uses one window for both tools, so the per-tool split inside All AI always matches
  each tool's own page for the same button. Streaks, active days, Weekly Report and My
  Adoption keep using full history, as today.
- The handler's current body becomes a function of `(scanner, query)` and runs unchanged for
  `claude` or `copilot`. Every payload gains `"source"` and `"cost_unit"`.
- Claude-only parts are left out of Copilot and All AI payloads, and their panels are hidden
  in the UI:
  - the plan-usage window and toast notifications (they read Claude's rate-limit and
    statusline data)
  - the root path chip, which shows the Copilot paths instead
- `/api/ai_summary` and `/api/reflect` take `source` too. They still run `claude -p` on the
  chosen tool's week, only when the button is pressed, and fail gracefully as today when
  `claude` isn't installed.
- **A source registry:** a small table of `{id, label, short name, colour, scanner,
  cost_unit, instructions files}`. The UI switch and the server both read it. Adding Codex
  = one new reader plus one registry row.
- CLI flags:
  - `--copilot-root PATH` (repeatable): extra VS Code User folders
  - `--no-copilot`: disable the source; the switch then hides
  - `--dump --source copilot`

### 5.5 All AI: what may be combined (P2, P3)

`source=all` builds both tools' `Stats` for the selected range and returns a **smaller
payload** for the four All AI pages. Combining rules:

| Quantity | Rule |
|---|---|
| Tokens | The headline **"Total tokens" = input + cache read + cache write + output**. With the §5.1 convention this means "everything the model processed" in both tools, including Copilot sessions without a cache split. Each part is also summed separately, always with the per-tool split. Copilot's cache parts carry the approx. badge when some of its sessions lack a split. |
| Prompts, sessions, active days | Sum prompts and sessions. Active days = days with **any** AI activity (set union). |
| Active time / focus | **Union of 10-minute buckets** across tools, never a sum. Ten minutes using both tools counts as 10 minutes. |
| Heatmap (weekday × hour), late-night and weekend share | Built from the **same union of 10-minute slots**: a cell counts the active slots in that weekday-hour. It is not a sum of each tool's log-line counts, because Claude writes many lines per prompt and Copilot few, so a sum would measure log chattiness instead of time. *(Claude's call, after the reviewer flagged it.)* |
| Models | One list, each row tagged with its tool. A model used through both tools appears twice, as separate rows. |
| Projects | Matched across tools by **real folder path** (case-insensitive on Windows), with per-tool tokens/prompts side by side. *(As built: merged by the project label, Claude Code's folder naming with the drive letter normalised, so the same folder from both tools is one row. Two folders whose names differ only in punctuation, such as `my.proj` and `my-proj`, share a label.)* |
| Cost | **Never summed.** Claude "$X est." and Copilot "N credits" shown side by side. |
| Deltas vs previous window | Computed on the combined totals with the same rules. |

The All AI pages:
- **Overview:** combined KPIs with a per-tool split, and a daily tokens chart stacked by
  tool in the tool colours.
- **Time & Focus:** combined active time, heatmap, late-night and weekend share.
- **Tools & Models:** the model table across tools, plus a tool-share chart.
- **Projects:** the cross-tool projects table.

Pages outside these four are dimmed for All AI (§6).

### 5.6 Exact tokens setup (P6)

- `python ai_fluency_insights.py --setup-copilot`, with `--dry-run`, modelled on
  `--setup-notifications`. It explains what it will change and asks for a yes. Then it
  sets these in the VS Code user `settings.json`:
  - `github.copilot.chat.otel.enabled: true`
  - `github.copilot.chat.otel.exporterType: "file"`
  - `github.copilot.chat.otel.outfile: "~/.copilot/otel/vscode-copilot-otel.jsonl"`
    (expanded to an absolute path, and the folder created)
- It backs up `settings.json` first. That file is JSONC (comments, trailing commas). If it
  doesn't parse as plain JSON, the command **does not edit it**. Instead it prints the three
  lines for the owner to paste.
- It applies only to chats after a VS Code window reload. The old history stays approx.
- Copilot CLI needs no setup; its usage records are already exact.
- `--setup-copilot --undo` removes the three keys, using the backup.

### 5.7 Data Health for Copilot

`build_health` gets a Copilot variant. It lists capabilities with the same
ok / partial / missing statuses:
- chat sessions found, and per store
- tokens mode: exact (OTel / debug log) vs approx., with the share of sessions in each
- credits present
- Copilot CLI sessions
- transcripts-only sessions
- project mapping found
- branches found
- session titles

It also shows:
- the Copilot / VS Code versions seen
- parse errors
- an **unknown-format counter**: lines or fields the reader didn't recognise. This is how
  format drift becomes visible instead of silently wrong.

Every Copilot metric whose capability is `missing` renders as "Not available for Copilot"
plus the capability's detail line.

## 6. UI

- **Tool switch** (D1): a segmented control "Claude Code | GitHub Copilot | All AI".
  - It sits in the top bar (`.acts`), to the left of the date-range control, and matches
    its style.
  - The choice is kept in `localStorage` (key `dtd-source`, wrapped in try/catch like the
    existing keys) and sent as `source=` on every `/api/stats` call.
  - Switching keeps the current section and range.
- **Colours** (D2): `data-source="claude|copilot|all"` on `<html>` overrides the accent
  tokens (`--acc`, `--acc-2`, `--acc-soft`, `--acc-line`, `--acc-ink`), in light and dark,
  following the existing token blocks.
  - Claude keeps today's values exactly.
  - Copilot uses violet; the existing `--violet` (#7355ea light / #a78bfa dark) is the
    starting point.
  - All AI uses teal (`--teal`, #0d8f89 / #2dd4bf).

  Per-tool series in All AI charts use the same three colours.
- **Labels.** Text that names Claude takes the selected tool's short name: "generated by
  Claude" becomes "generated by Copilot", "You vs Claude" becomes "You vs Copilot". About 39
  strings in the HTML. The sidebar subtitle ("Claude Code analytics") follows the tool, and
  so do the footer chips. "Est. API cost" becomes "Credits used" for Copilot.
- **All AI sidebar:** items outside the four All AI pages are dimmed, not removed, so the
  sidebar doesn't jump. Hovering one says "Pick Claude Code or GitHub Copilot to see this."
  If the current section isn't available when switching to All AI, go to Overview.
- **Not-available cards** (P4): "Not available for Copilot" plus a one-line reason from Data
  Health, in the card's normal footprint so layouts don't shift. Approximate values get the
  existing "approx."/confidence badge styling.
- **VS Code vs CLI** (P5): one small split chart on Copilot's Overview, or Tools & Models.
  The builder picks which.
- **Copilot empty state:** no Copilot logs found → a friendly panel saying where it looked
  and that Copilot Chat history appears here after first use. All AI then shows Claude only,
  with a note.
- Claude-only panels (plan usage limits, notification toasts) are hidden on Copilot and All AI.

## 7. Section-by-section expectations for Copilot

What each section can show from default logs. Before the office report these are
expectations, not promises; the builder marks them through Data Health.

| Section | Copilot (default logs) |
|---|---|
| Overview | Tokens (approx.), credits, prompts, sessions, active time, leverage, lines of code from write tools, tool errors. Cache tokens only in exact mode or CLI. |
| Time & Focus | Full: timestamps per request. |
| Tools & Models | Full: models per request and per tool round, tool names. |
| Projects | Full via `workspace.json`. Branch partial. |
| Weekly Report | Full; titles from summary or first prompt. |
| My Adoption | **Copilot's own feature list**: agent / ask / edit mode, plan agent, subagents, MCP tools, custom instructions file, prompt files, terminal commands, web fetch, model variety. Claude-only features are not shown. Scoring formulas unchanged. *(Claude's call, see §9.)* |
| Flow | Turn time from `timings.totalElapsed` / `elapsedMs`. Clean-turn and recovery where tool success is known. |
| Quality | Command pass/fail where `run_in_terminal` results carry it. Partial at first. |
| Value | In credits. "Cost per 100 LoC" becomes credits per 100 LoC. Cache savings only with cache data. |
| Advisor | Only tips whose inputs exist for Copilot. Claude-specific tips (CLAUDE.md, plan mode, statusline…) hidden. *(Claude's call.)* |
| Data Health | Copilot variant (§5.7). |

## 8. Constraints

- Python 3.8+, standard library only (sqlite3 is stdlib). No pip installs, no network,
  works offline. Chart.js stays bundled.
- **Read-only on Copilot's files.** Open SQLite read-only. For a WAL database, read a temp
  copy, as the checker does.
- Windows is the primary platform. macOS and Linux paths are supported the same way the
  checker supports them.
- Auto-refresh every 15 s. The Copilot scan must stay cheap: per-file mtime/size cache, and
  re-parse only changed files. Chat-session files get rewritten (compacted), so a changed
  file is re-read whole.
- The parser must tolerate unknown and missing fields: skip and count, never crash
  (anti-goal).
- **Claude regression guard:** a test runs a Claude fixture through the old and new code
  paths. For "All time" and for custom from–to ranges, it asserts identical output for
  `source=claude`. Rolling N-day windows get their own tests for the calendar rule (D8).
- The existing test style (`unittest`, `test_ai_fluency_insights.py`) is extended with
  Copilot fixtures. Fixtures are synthetic, shaped like the observed formats, with made-up
  values.

## 9. Calls Claude made (owner to check)

- **New module.** Copilot code lives in `copilot_source.py` beside `ai_fluency_insights.py`,
  not inside the 5,500-line main file. It's still stdlib-only and still one command to run.
  This sets the Codex pattern: one file per tool.
- **Credits are shown as credits.** No dollar conversion, since GitHub's rate isn't confirmed
  from an official source.
- **My Adoption and Advisor.** Copilot gets its own feature list and only data-backed tips,
  instead of Claude's list showing "not used".
- **All AI models.** A model used through both tools shows as two rows, one per tool, so
  per-tool numbers stay separate.
- **Session-store DB is an enricher only** (branch, summary). It's never a source of turns
  or tokens; it covers only recent sessions.
- **Setup refuses JSONC.** The setup command won't edit a `settings.json` with comments; it
  prints the lines instead.
- **Token convention** (§5.1). Copilot is normalised to Claude's uncached-input convention
  where a split exists. All AI's headline is total tokens (all four parts).
- **Premium requests** (§5.1). Kept as their own unit, never added to credits.
- **CLI-from-VS-Code sessions** (§5.2). One session across stores, exact CLI tokens win,
  counted where started.
- **All AI heatmap and late-night share** (§5.5). Built from 10-minute slots, not summed
  log lines.
- **Claude agent inside VS Code.** VS Code can run a Claude agent billed through Copilot. If
  its logs land in `~/.claude/projects`, they stay in the Claude Code view for now. Moving
  them would change Claude numbers. Revisit if the office report shows them.

## 10. Left to the builder

- The exact translator mapping per store, within §5.1–5.3.
- Visual details of the switch, colour shades, dimmed-item styling, and where the
  VS Code-vs-CLI chart lives.
- Wording of not-available reasons and of the empty state.
- How the handler is refactored into a per-source function, as long as `source=claude`
  output is unchanged.
- Build order. Suggested:
  1. switch, colours and `source` param, with a Copilot empty state
  2. Copilot reader for chat sessions, transcripts and CLI, plus the Copilot pages
  3. All AI
  4. `--setup-copilot` and Copilot Data Health
  5. after the office report: field-name fixes and docs (README, METRICS, HOW_IT_WORKS,
     DASHBOARD_GUIDE)

## 11. After the office report (Monday)

The owner runs `tools/copilot_log_check.py` on the office laptop and shares the
structure-only report. Then:
- compare it with §4
- fix field names in the translator
- add a fixture for any new shape
- update the "Last verified" line in this document

Nothing in §5–7 is expected to change. Only §4's field details and the translator are.

### Result (2026-10-26)

**Last verified:** 2026-10-26, against three office reports (checker v1). They came from VS Code 1.121–1.142 and Copilot Chat 0.49–0.70, with Copilot CLI 1.0.74–1.0.103 on one machine.

- **No field names changed.** Every field in §4 exists as described, including the older whole-file `.json` format.
- **One model appeared under several spellings** (`claude-haiku-4-5-20251001`, `claude-haiku-4.5`, `copilot/claude-haiku-4.5`). Model names are now normalised to one id (plan Task 15).
- **Requests record `modelState {value, completedAt}`.** In VS Code `ResponseModelState`, value 2 means Cancelled. Copilot therefore gets a real "interrupted" figure when its logs have this field, and §7's "interruptions not available" applies only to logs without it. `completedAt` gives the request's end time.
- **CLI logs carry routine `permission.*`, `skill.*`, `subagent.*` and `model.*` records.** They are ignored and are no longer reported as unknown types.
- **Exact tokens:** two of the three machines had debug logs without `llm_request` because the setting was off. The confirmed setting is `github.copilot.chat.agentDebugLog.fileLogging.enabled`, and Data Health names it next to the OTel option.
