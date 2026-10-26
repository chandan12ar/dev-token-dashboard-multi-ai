# How AI Fluency Insights Works

A 100% local dashboard for Claude Code and GitHub Copilot usage. It consumes
**zero tokens** — it never calls any API. It only reads the session logs those
tools already save on this machine, parses them, and serves a live web page.

## The data source

Claude Code logs every session as JSONL files under:

```
C:\Users\<you>\.claude\projects\<project-dir>\<session-id>.jsonl
```

Each line is one event: a user prompt, an assistant response (with token
usage, model name, timestamp), or a tool call (Write/Edit/etc. with the exact
content written). The dashboard is nothing more than a parser + aggregator
over these files. Nothing leaves the machine.

### GitHub Copilot's stores

Copilot writes several different logs, and none of them is a documented
format. The dashboard reads these, all **read-only**. `<User>` is the VS Code
user folder (`%APPDATA%\Code\User` on Windows,
`~/Library/Application Support/Code/User` on macOS, `~/.config/Code/User` on
Linux). Insiders, VSCodium, named profiles and `~/.vscode-server` are found
too, and `--copilot-root PATH` adds more.

| Store | Where | What it gives | Tokens |
|---|---|---|---|
| Chat sessions | `<User>/workspaceStorage/<hash>/chatSessions/` (and `globalStorage/emptyWindowChatSessions/` for chats with no folder open) | prompts, timestamps, models, tool calls, per-request credits, whether a request was stopped. Newer builds write an incremental `.jsonl`, older ones one `.json` | approx. |
| Transcripts | `.../GitHub.copilot-chat/transcripts/` | the turn-by-turn event log; on some machines the only chat store | none |
| Debug logs | `.../GitHub.copilot-chat/debug-logs/<session>/` | one record per model call, with exact tokens and credits, **when** the VS Code setting `github.copilot.chat.agentDebugLog.fileLogging.enabled` is on | exact |
| OpenTelemetry file | the path in `github.copilot.chat.otel.outfile` (`--setup-copilot` sets it) | exact tokens per model call; no prompt text by default | exact |
| Copilot CLI | `~/.copilot/session-state/<id>/events.jsonl` | one usage record per model call | exact, always |
| Session store and workspace map | `globalStorage/github.copilot-chat/session-store.db`, `workspaceStorage/<hash>/workspace.json` | only enrich: project folder, branch, summary | n/a |

**One session, one source.** The same chat can appear in several of these.
The dashboard works out each session once, by its session id:
- prompts, timestamps and tools come from the chat-session file, or from the
  transcript when there is no chat file;
- tokens come from exactly **one** place, the best available: OpenTelemetry,
  then the debug log, then the chat file's per-request numbers (approx.). They
  are never added together. For a CLI session the CLI's exact usage records win;
- anything seen twice inside one store (a usage record or a request) is counted
  once.

The session-store database is never a source of turns or tokens, and Copilot's
files are opened read-only (a database with a write-ahead log is read from a
temporary copy).

## The pipeline

1. **Scanner** (`Scanner` class) — walks the projects directory for `*.jsonl`
   files. It caches each file's `(mtime, size)`, so on refresh it re-parses
   only files that actually changed. Fast even with months of history.
2. **Parser** (`parse_entry`) — turns each log line into stats:
   - *Assistant entries*: token usage (input, output, cache read, cache
     write), model, estimated cost. **Deduplicated by message ID** — see
     [TOKEN_COUNTS.md](TOKEN_COUNTS.md) for why this matters.
   - *User entries*: prompt count, prompt length, estimated "human tokens"
     (~4 chars per token), slash commands, interruptions.
   - *Tool calls*: tool usage counts, and lines of code written via
     Write/Edit/MultiEdit/NotebookEdit (counted from the actual content, so
     it's additions, not a git diff).
3. **Web server** — a stdlib `ThreadingHTTPServer` on `http://localhost:8787`
   serving a single-page dashboard. The page fetches `/api/stats` and
   re-renders every 15 seconds.

**With Copilot.** `CopilotScanner` (in the main file; the Copilot readers and
translators it calls live in `copilot_source.py`, beside it) has the same shape
as `Scanner`: it caches each file's `(mtime, size)` and re-reads only what
changed. A *translator* turns each Copilot session into the same event shape the
Claude parser already understands, and those events go through the existing
`parse_entry` into a **separate** stats object. So every calculation runs
unchanged on Copilot, each tool on its own data, and Copilot's numbers can never
leak into Claude's or the reverse. Unknown fields are skipped and counted
(visible on Data Health), never fatal: each session is translated inside its own
guard, so a session that cannot be translated at all is counted as bad on Data
Health (and not retried until one of its files changes) while every other
session still shows. If building a page's stats still fails, `/api/stats`
answers with a JSON error and the page says "Couldn't build the stats for" that
tool instead of showing another tool's numbers.

`/api/stats?source=claude|copilot|all` picks the tool; no `source` means Claude,
so old bookmarks keep working. `all` builds a smaller payload that combines only
what means the same thing in both tools (rules in
[METRICS.md §19](METRICS.md#19-github-copilot-v15)). Adding another tool later
means one more reader plus one row in the `SOURCES` table.

Requires only Python 3.8+ — standard library, no pip installs. Chart.js is
bundled locally (`chart.umd.min.js`), so the page makes **no external
requests at all** and works fully offline.

## What it shows

**KPI cards**
- Input / output tokens (deduplicated, per API response)
- Cache read tokens + cache hit %
- Estimated API cost (public API pricing; on a subscription the real
  marginal cost is $0 — edit the `PRICING` table in the script to tune)
- Lines of code Claude wrote
- Prompt count + average prompt length
- Sessions + average session duration
- Interruptions (esc / rejections)
- Your typed tokens (estimated) and your **leverage ratio** — Claude output
  tokens per token you typed

**Charts and tables**
- Daily token bars (input / output / cache read)
- "You vs Claude" balance chart (log scale)
- Model usage doughnut + model breakdown table
- Lines of code per day
- Tool usage and slash command usage
- Weekday × hour activity heatmap
- Per-project table (tokens, LoC, prompts, sessions, est. cost)
- Longest prompts and most-edited files

**Time ranges**: 7 / 30 / 90 days / all time, plus a custom from–to date
picker. "Last N days" means today plus the N−1 calendar days before it (local
time), the same for every tool. The selected range applies to *everything* on the Dashboard tab —
charts, tables, and the project deep-dive — and KPI cards show a Δ vs the
previous equal-length window.

**Momentum bar**: current & best day streak, all-time active days, and
optional goal progress bars (configure the `GOALS` dict in the script).

**Exports**: copy a text summary to the clipboard, download the daily table
as CSV, or the full `/api/stats` payload as JSON.

The exact formula behind every one of these numbers is documented in
[METRICS.md](METRICS.md).

## Weekly Report tab

A second tab built for one thing: writing your weekly update in seconds.
All of it comes free from the logs — Claude Code stores an AI-generated
title for every session (`ai-title` entries), so no extra AI calls are
needed.

- **Week selector** — browse any past week with ‹ prev / next ›
- **What you worked on** — session titles grouped by project, with date,
  duration, LoC, and prompt count per session (ready-made standup bullets)
- **KPIs with week-over-week deltas** — sessions, focus hours, output
  tokens, LoC, prompts, building %
- **Exploration vs building gauge** — read-type tool calls (Read, Grep,
  Glob, WebSearch…) vs file-editing calls (Write, Edit…). A 90%-reads week
  was research; a 70%-writes week was implementation.
- **Copy as Markdown** — one click produces a formatted weekly update you
  can paste into Teams, email, or a wiki
- **AI summary (optional)** — a button that pipes the week's bullets
  through `claude -p` for a polished first-person paragraph. This is the
  *only* feature in the whole dashboard that consumes tokens, and only
  when you press it. (Note: the `claude -p` run itself gets logged as a
  tiny new session, so it will appear in your stats.)

## My Adoption tab

A third tab built for the developer themselves: an Adoption Score (0-100, with
levels), four pillars (Habit, Range, Craft, Impact), a calendar, a feature
checklist and a "next 3 moves" coach. It reads the same parsed stats as the other
tabs. The parser additionally records the days each Claude Code feature was used
(plan mode, subagents, skills, MCP tools, slash commands, web research, commands run), and
`build_adoption()` turns that into the payload under `adoption` in `/api/stats`.
It is always computed over full history, never the selected range. Formulas:
[METRICS.md §12](METRICS.md#12-my-adoption-v13-extended-in-v14).

## Engineering insight tabs (Flow, Quality, Value, Advisor, Data Health)

The parser also records per-event facts, each with its local day: turn start and
end (a prompt to Claude's `end_turn` reply), every tool call paired with its result
by `tool_use_id` (timing and pass/fail), the kind of each shell command (test,
build, lint), failure episodes, edits by file, safety patterns (category only),
permission decisions, thinking time and effort. Because every record carries its
day, `build_flow`, `build_quality` and `build_value` can slice any window out of
full history without re-parsing, which is also how the period-over-period deltas
and the Advisor's 28-day window work. `build_health` reports which of these
signals the logs actually contain, and `METRIC_CATALOG` is the single source for
every metric's confidence badge, formula and limitation.

## Other developer features

- **Exploration vs building per day** — stacked chart of read vs write
  tool calls on the main dashboard
- **Friction per day** — interruptions (esc) and failed tool calls, to
  spot where the workflow hurts
- **Project deep-dive** — click any row in the Projects table for a modal
  with that project's daily output/LoC chart, its sessions (with titles),
  and its most-edited files
- **Git branch breakdown** — output tokens and LoC attributed per branch
  (uses the `gitBranch` field Claude Code stamps on every log entry)

## Running it

```
python ai_fluency_insights.py               # serves http://localhost:8787
python ai_fluency_insights.py --port 9000   # different port
python ai_fluency_insights.py --dir <path>  # custom Claude logs location
python ai_fluency_insights.py --dump        # print raw stats JSON and exit
python ai_fluency_insights.py --dump --source copilot   # one tool's stats (claude, copilot or all)
python ai_fluency_insights.py --copilot-root <path>     # extra VS Code user folder for Copilot logs
python ai_fluency_insights.py --no-copilot              # ignore Copilot logs
python ai_fluency_insights.py --setup-copilot           # turn on exact Copilot token logging (asks first)
```

On Windows, after editing the script, `Restart-Insights.bat` stops every
running copy (matched by command line *and* by whoever owns port 8787) and
relaunches one fresh windowless instance, then opens the browser.

## Auto-start at logon

The dashboard can start automatically when you log in, so
http://localhost:8787 is always available. On Windows use either the
built-in `--install-startup` flag (Task Scheduler, needs an Administrator
terminal) or a Startup-folder shortcut pointing at `pythonw.exe` (no admin
needed). macOS and Linux equivalents (cron / systemd) are covered in
[../SETUP.md](../SETUP.md#3-optional-start-automatically-at-logon).
