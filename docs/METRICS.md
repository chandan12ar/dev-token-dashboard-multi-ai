# Metrics & Math — how every number on the dashboard is computed

This document is the formula reference for the dashboard. It explains where
the data comes from, the exact math behind every metric, and what each section
of the UI refers to — so you can trust (or audit) any number you see.

Sections 1-18 describe Claude Code. GitHub Copilot runs the same calculations
on its own data; where it differs (credits, token accuracy, "interrupted") and
how the **All AI** view combines the two tools is in
[§19](#19-github-copilot-v15).

Companion docs:
- [HOW_IT_WORKS.md](HOW_IT_WORKS.md) — architecture and data pipeline
- [TOKEN_COUNTS.md](TOKEN_COUNTS.md) — why our token totals differ from the
  official Claude Code panel (deduplication analysis)

---

## 1. Where the numbers come from

Claude Code writes every session to JSONL files under
`~/.claude/projects/<project-dir>/<session-id>.jsonl`. Each line is one event
with a timestamp, session ID, git branch, and either:

| Event type | What we extract |
|---|---|
| `assistant` message | `usage` block (input / output / cache tokens), model name, `stop_reason`, thinking time and effort, tool calls with their full input |
| `user` message | prompt text, or a tool result riding back: its `tool_use_id` (to time the call), error flag, and any `permissionDecision` |
| `system` entry | `stop_hook_summary` (hooks that ran) |
| `attachment` entry | counted for the Data Health tab only |
| `ai-title` entry | the AI-generated session title (used by the Weekly Report) |
| `permission-mode` entry | auto / default / plan snapshots |

Fields can differ between Claude Code versions. The **Data Health** tab shows
which of them your logs actually contain, and the versions seen.

The dashboard re-parses only files whose `(mtime, size)` changed, rebuilds all
aggregates in memory, and serves them at `/api/stats`. **Nothing is ever sent
anywhere** — tracking is purely "read the logs that already exist, every 15
seconds".

All timestamps are converted to **your local timezone** before being bucketed
into calendar days, so "today" means your today, not UTC's.

---

## 2. Token metrics

### Deduplication rule (the foundation of everything)

When Claude streams a response, Claude Code journals **several JSONL lines for
the same API response**, each carrying a copy of the same `usage` object.
Summing every line overcounts tokens ~3×. So each response is counted exactly
once, keyed by:

```
dedup_key = (message.id, requestId)
```

Tool calls are likewise deduplicated by their tool-use `id`. The full analysis
and proof is in [TOKEN_COUNTS.md](TOKEN_COUNTS.md).

### The four token counters

From each unique `usage` block:

| Metric | Field | Meaning |
|---|---|---|
| **Input tokens** | `input_tokens` | fresh (non-cached) tokens sent to the model |
| **Output tokens** | `output_tokens` | tokens the model generated (text, code, tool calls, thinking) |
| **Cache read** | `cache_read_input_tokens` | context replayed from prompt cache — the bulk of real volume |
| **Cache write** | `cache_creation_input_tokens` | tokens written into the cache for future reuse |

### Cache hit rate

```
cache_efficiency = cache_read / (cache_read + input) × 100
```

The share of context that came from cache instead of being re-processed at
full price. High (95%+) is normal and good — it's why long Claude Code
sessions stay cheap.

### Cache-reset rate (mid-session cache health)

A session's *first* API call is expected to write the whole prefix to
cache — that's normal, not a problem. Every call *after* the first,
though, should mostly be reading that cache back, not rewriting it. A
non-first call where `cache_write > cache_read` means the prefix broke
(idle past the cache TTL, a tool list or model change, etc.) and the next
call is paying full price again, invisibly, on a dollar-only view:

```
cache_reset_pct = resets / non_first_calls × 100
```

where a "reset" is any call after a session's first with
`cache_creation_input_tokens > cache_read_input_tokens`. When this exceeds
5% of turns in the selected range, the **Cache read** KPI subtitle swaps
to a warning instead of showing the hit rate.

### Cold-start tax

Writing a session's frozen prefix to cache for the first time is the most
expensive call it makes — often a large share of the session's total cost.
This metric asks: across the selected range, what fraction of total
session cost was just that first call?

```
cold_start_pct = Σ(first_call_cost) / Σ(session_cost) × 100
```

Both sums are weighted by cost, not simple session averages, so a handful
of expensive cold sessions can't be washed out by many cheap warm ones.
High values point at frequent short/cold-start sessions rather than fewer,
longer, warm ones.

---

## 3. Estimated API cost

Per unique response, using the `PRICING` table at the top of the script
(USD per 1M tokens, editable):

```
cost = ( input  × price_in
       + output × price_out
       + cache_write × price_in × 1.25     # cache writes cost 25% extra
       + cache_read  × price_in × 0.10 )   # cache reads cost 10% of input
       / 1,000,000
```

The price row is selected by substring match on the model name
(`opus`, `sonnet`, `haiku`, `fable`); unknown models fall back to
Sonnet-class pricing.

> **Important:** this is *"what this usage would cost at public API
> pay-as-you-go prices"*. On a Pro/Max subscription your real marginal cost
> is **$0** — the card exists to show the value you're extracting, not a bill.

---

## 4. Human-side metrics

### Prompts

A "prompt" is a user message with real typed text. Excluded so the count
means *things you actually asked*:

- slash-command invocations (`<command-name>…` blocks) — counted separately
  in the **Slash commands** chart
- local command output blocks
- interruption markers (counted as interruptions instead)
- tool results riding back on user-type entries

`avg_prompt_lines` / `avg_prompt_words` are simple means over the prompts in
the selected range.

### Your tokens (estimated)

There is no tokenizer in the loop, so typed tokens are estimated with the
standard ~4-characters-per-token heuristic:

```
human_tokens = max(1, len(prompt_text) / 4)   per prompt, summed
```

### Leverage ratio

The headline "how much am I getting out of this" number:

```
leverage = claude_output_tokens / your_typed_tokens
```

A leverage of 200× means every token you typed produced two hundred tokens of
Claude output in return. Because `human_tokens` is an estimate, treat
leverage as an order-of-magnitude indicator, not a precise figure.

---

## 5. Lines of code (LoC)

Counted from the **actual content** of file-writing tool calls — not a git
diff. For each deduplicated tool call:

| Tool | Counted text |
|---|---|
| `Write` / `Create` | `content` |
| `Edit` / `StrEditReplace` | `new_string` |
| `MultiEdit` | sum of every edit's `new_string` |
| `NotebookEdit` | `new_source` |

```
loc = newline_count(text) + 1
```

This measures **lines Claude wrote**, i.e. additions/replacements. It does
not subtract deletions, and rewriting the same function twice counts twice.
The same numbers feed the per-project, per-branch, and most-edited-files
breakdowns (files are keyed by basename).

---

## 6. Activity & flow metrics

### Exploration vs building

Every tool call is classified into one of two buckets:

- **Exploration (reads):** `Read`, `Grep`, `Glob`, `LS`, `WebFetch`,
  `WebSearch`, `NotebookRead`, `TodoRead`, `ToolSearch`
- **Building (writes):** `Write`, `Create`, `Edit`, `StrEditReplace`,
  `MultiEdit`, `NotebookEdit`

```
building % = writes / (reads + writes) × 100
```

A 90%-exploration day was research/debugging; a 70%-building day was heads-
down implementation. Neither is "better" — the split just tells you what kind
of week it was.

### Friction

```
friction/day = interruptions + tool_errors
```

- **Interruptions** — user messages containing `[Request interrupted` (you
  pressed Esc or rejected an action)
- **Tool errors** — tool results flagged `is_error` (failed commands, bad
  edits, permission denials)

Spikes here show where the workflow hurt.

### Sessions

A session = one distinct `sessionId`. Its duration is
`last_timestamp − first_timestamp` within that session (wall-clock span, so
it includes thinking/idle time between messages — read "avg session" as
*engagement span*, not pure compute time). When a time range is selected,
sessions are included by their **start day** so the session KPIs stay
consistent with the token KPIs beside them.

### Activity heatmap

Message count bucketed by `(weekday, local hour)` over the selected range.
Darker cell = more messages in that hour slot.

---

## 7. Time ranges, deltas, streaks & goals

### Range filtering

The 7 / 30 / 90-day buttons keep **calendar days**: today plus the N−1 days
before it, in your local time. A day with no activity inside that window still
counts as part of it, so "last 7 days" always spans a full week, whatever tool
you are looking at (the same rule for Claude Code, GitHub Copilot and All AI).
The custom from–to picker filters by exact calendar dates, and "All time" is
everything. Either way the server rebuilds a range-scoped stats object, so
**every card — charts, tables, longest prompts, project modal — respects the
range**, not just the headline KPIs. The Weekly Report tab always uses full
history.

*Before v1.5 the 7 / 30 / 90 buttons kept the last N days that had activity.
For a tool used only on some days that gave the wrong window, so it changed. "All
time" and custom ranges give the same Claude Code numbers as before.*

### Period-over-period deltas (▲ / ▼ on KPI cards)

For a rolling N-day window, the comparison window is the N calendar days
immediately before it:

```
current = today and the N−1 days before   previous = the N days before those
delta % = round((current − previous) / previous × 100)
```

Shown for output, input, cache read, cost, LoC, prompts, sessions, and your
tokens. Hidden when there's no previous window (custom ranges / all-time).

### Streaks

Over all-time active days:

- **Current streak** — consecutive calendar days counted backwards from the
  most recent active day (a gap of one full day breaks it)
- **Best streak** — longest consecutive run anywhere in history
- **Active days** — total calendar days with any activity

### Goals (progress bars)

Set personal targets in the `GOALS` dict at the top of the script:

```python
GOALS = {
    "daily_loc": 500,        # target lines of code / day
    "daily_tokens": 0,       # output-token budget / day   (0 = hide bar)
    "weekly_tokens": 0,      # output-token budget / week  (0 = hide bar)
}
```

```
progress % = min(100, current / goal × 100)
```

"Today" is your most recent active day; "this week" is the last 7 active
days of output. The bar changes color when you're over target. These are
**your** targets — they have nothing to do with Anthropic's plan limits.

---

## 8. Weekly Report math

Weeks are **ISO weeks (Monday–Sunday)**, keyed like `2026-W28`.

- **Focus hours** = Σ session durations for sessions starting that week ÷ 60
- **Week-over-week deltas** = same percentage formula as above, vs the
  previous ISO week
- **Building %** = the exploration-vs-building formula over the week's tool
  calls
- **What you worked on** = the AI-generated session titles Claude Code
  already stores (`ai-title` entries), grouped by project — zero extra tokens
- **AI summary** = the only token-consuming feature: it pipes the week's
  bullets through `claude -p` locally, only when you press the button (capped
  at 50 session bullets to keep the prompt bounded)

---

## 9. Time & wellness (v1.2)

### Active time

Each message's timestamp is dropped into a **10-minute bucket**
(`hour × 6 + minute ÷ 10`, per local calendar day). Active time =
`buckets × 10 min`. A lone message therefore counts as 10 minutes; a
gap longer than ~10 minutes stops counting. This is deliberately closer
to "time you actually spent" than session wall-clock span.

- **Active time in range** = Σ daily bucket counts × 10
- **Avg per active day** = active time ÷ days with ≥ 1 message
- **Longest focus block** = longest run of *consecutive* buckets in a single
  day × 10 (does not span midnight)

### Quiet hours & weekends

- **Quiet-hours share** = messages with local hour ≥ 23 or < 6, ÷ all
  messages in range. Tune `QUIET_START` / `QUIET_END` at the top of the
  script. Days where > 25 % of messages fall in quiet hours are drawn in
  red on the active-minutes chart.
- **Weekend share** = messages on Sat/Sun ÷ all messages in range.

## 10. Task-type classification (v1.2)

Every session is bucketed into **feature / bugfix / refactor / docs /
explore / other** by keyword-matching its AI-generated title (bugfix
keywords win over refactor, then docs, feature, explore). Sessions with
no title signal fall back to tool mix: wrote code → feature; only
reads → explore. It's a heuristic — expect roughly-right, not perfect.

## 11. AI Fluency report (v1.2)

Four 0–100 heuristic scores per ISO week, adapted for coding from the
4D AI Fluency Framework (Delegation, Description, Discernment, Diligence) by
Rick Dakan and Joseph Feller with Anthropic, licensed
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) — see
[Credits](../README.md#credits). Changes were made; not endorsed by the
authors. All computed locally:

| Dimension | Formula |
|---|---|
| **Delegation** | `50 × (code sessions ÷ sessions) + 50 × min(1, leverage ÷ 50)` — are you handing Claude real building work, not just questions? |
| **Description** | `65 × one-shot rate + 35 × sweet-spot factor`. One-shot rate = `1 − (corrections + interruptions) ÷ prompts`. Sweet spot = avg prompt of 8–150 words scores 1.0, shorter/longer decays. |
| **Discernment** | `50 × engagement + 50 × error control`. Engagement = corrections + interruptions reaching 5 % of prompts scores 1.0 (pushing back is *good* — it means you review output). Error control = `1 − tool errors ÷ edits`. |
| **Diligence** | `% of code-writing sessions that also ran ≥ 1 Bash/PowerShell command` — a proxy for "verified the work" (tests, builds, runs). |

**Corrections** are prompts that *start* with correction language
("no", "wrong", "actually", "that's not", "undo", "revert",
"you missed"…, case-insensitive regex `CORRECT_RE` in the script).
Note the deliberate tension: corrections lower Description (the first
prompt didn't land) but raise Discernment (you caught it).

### Reflection question

One data-backed question per week, picked deterministically (rotating by
week index) from whichever rules fire: correction rate > 8 %,
quiet-hours share > 20 %, exploration share > 65 %, building share
> 85 %, verification rate < 40 % — with Anthropic's classic fallback:
*"What's one thing you want to keep doing yourself, even if Claude could
do it faster?"*. **Discuss with Claude** pipes the question + the week's
stats through `claude -p` (token-costing, button-only, like AI summary).

---

## 12. My Adoption (v1.3, extended in v1.4)

A personal scorecard on its own tab. Everything is measured over a rolling
**28-day window ending on your most recent active day**, compared with the 28
days before it, and always uses full history (the range filter does not apply).
It compares you with *your own past* only. It never ranks you against anyone.
Targets and weights live in the `ADOPTION` table at the top of the script.

Fewer than 3 active days in the window → the tab shows "Building your
baseline" instead of a score.

### The four pillars (each 0-100)

| Pillar | Question | Formula |
|---|---|---|
| **Habit** | Do you show up regularly? | `60 × min(1, active_days ÷ 12) + 40 × (weeks with ≥ 2 active days ÷ 4)`: 12 active days is about 3 a week |
| **Range** | Do you use what Claude Code offers? | `75 × min(1, features_used ÷ 6) + 25 × substantial-session share` |
| **Craft** | Do you work with Claude well? | `0.6 × fluency mean + 0.2 × Flow score + 0.2 × Quality score` (§13, §14). The fluency mean is the four dimensions in §11. If Flow or Quality has no score yet, its weight is shared out among the rest |
| **Impact** | What does Claude get done for you? | `50 × min(1, LoC per active day ÷ 300) + 50 × (active days where Claude wrote code ÷ active days)` |

*Features* are ten things you can switch on, detected from the logs. Each
counts if you used it at least once in the window:

| Feature | Detected from |
|---|---|
| Project memory | a `CLAUDE.md` or `AGENTS.md` in the working folder of a project you were active in |
| Run & verify | `Bash` / `PowerShell` tool calls |
| Plan mode | `EnterPlanMode` / `ExitPlanMode` tool calls, or a `plan` permission-mode snapshot |
| Subagents | `Agent` / `Task` tool calls, or subagent (sidechain) log lines |
| Skills | `Skill` tool calls |
| Hooks | `stop_hook_summary` entries |
| Task lists | `TodoWrite` / `TaskCreate` / `TaskUpdate` tool calls |
| Connected tools | any `mcp__*` tool call |
| Slash commands | slash-command invocations |
| Web research | `WebSearch` / `WebFetch` tool calls |

A session is **substantial** when it has ≥ 10 tool actions (reads + edits + commands).

### Adoption Score and levels

```
score = 0.25 × Habit + 0.25 × Range + 0.30 × Craft + 0.20 × Impact
```

| Score | Level | Extra condition |
|---|---|---|
| 0-19 | Explorer | none |
| 20-39 | Practitioner | none |
| 40-59 | Power user | none |
| 60-74 | Expert | none |
| 75-89 | Pro | every pillar ≥ 50 |
| 90-100 | Pro Max | every pillar ≥ 70, **and** the score at each of the last 4 weekly points ≥ 85 |

If your score reaches a level but the extra condition fails, you stay one level
down and the page says exactly what to fix ("Lift Range (now 40) to 50 to unlock Pro").

The trend chart recomputes the score as of each of the last 12 week-ends
(same 28-day window), skipping weeks with fewer than 3 active days.

### "What changed lately" and the adoption journey

- **What changed lately**: up to four lines: pillars that moved 5+ points, a level change, and any feature used for the first time in the window.
- **Adoption journey**: *Started* (first active day); *Recurring* (2+ active days a week for 3 weeks in a row); *Integrated* (4+ features used in the window); *Verified outcomes* (3+ edit sessions and at least half of them ended with a test, build or lint).

### "Your next 3 moves" (coach)

Rule-based, no AI and no tokens. Candidates are ranked and the top three
shown (at most two "try a feature" tips):

- an unused feature → ranked by its usefulness weight (+10 if you have never used it)
- Habit below 70 → "make it a steady habit"
- your weakest fluency dimension, if it is below 70
- Impact below 50 → "aim Claude at real changes"
- more than 20 % of activity between 11pm and 6am → "protect your evenings" (ranked high on purpose)

The Advisor tab (§16) reuses these and adds the evidence-based rules.

### Healthy habits

Average active minutes per active day, longest single-day focus block, share
of messages in quiet hours (`QUIET_START`-`QUIET_END`), and share on weekends,
all over the window.

---

## 13. Flow tab (v1.4)

All numbers follow the selected date range. Every metric carries a confidence
badge (§17). Sources are fields verified in real Claude Code logs.

| Metric | Formula |
|---|---|
| **Turn time** | A *turn* starts at a prompt you typed and ends at Claude's reply with `stop_reason = end_turn`. Median and 90th percentile of the elapsed seconds. Includes any time waiting for you to approve an action |
| **Clean first-pass turns** | `turns with no tool error, no interruption and no correction ÷ all turns`. A *correction* is a next prompt that starts with correction wording (`CORRECT_RE`). Turns that never reached `end_turn` count as not clean |
| **Failure episodes** | The same tool failing in a session. It is *recovered* when that tool later succeeds in that session; steps to recover = tool results in between. *Stuck* = 3 or more failures in a row |
| **Where tool time goes** | Seconds between each `tool_use` and its `tool_result` (matched by id), grouped by tool or, for shell commands, by what ran (`git push`, `pytest`, `python`...). Share = group seconds ÷ all tool seconds |
| **Time to first edit** | Seconds from a session's first entry to Claude's first edit tool call; median over sessions that edited |
| **Session length** | Last entry minus first entry of a session, median and 90th percentile |
| **Thinking time, effort** | `thinkingDurationMs` and `effort` as reported on Claude's responses |

```
Flow score = 100 × (0.5 × clean-turn rate + 0.3 × recovery rate + 0.2 × (1 − stuck share))
```
Needs 5 turns. With no failures at all, recovery counts as 100 % and stuck share as 0.

**Subagents:** log lines flagged `isSidechain` are kept out of turn and verification tracking so a subagent finishing does not end your turn.

---

## 14. Quality tab (v1.4)

| Metric | Formula |
|---|---|
| **Verified after last edit** (proxy) | Sessions where a *test, build or lint* command ran after Claude's last edit ÷ sessions where Claude edited. A command's kind comes from what it invokes (`pytest`, `npm run build`, `ruff`...), never from text inside a heredoc, quote or commit message |
| **First-run pass rate** | For each session and kind of check, whether the first run succeeded (no error flag). Fail → fixed counts sessions where a failed run was later followed by a pass |
| **Tests alongside code** (proxy) | Edit sessions that edited a test file (`tests/`, `test_*`, `*_test.*`, `*.spec.*`...) ÷ edit sessions |
| **Re-edit share** (proxy) | Edits to a file already edited earlier in the same session ÷ all edits |
| **Rework hotspots** (proxy) | Files edited 5+ times that were edited 3+ times in one session or on 3+ days |
| **Errors and interruptions** | Failed tool results per 100 edits; interruptions per 100 prompts |
| **Safety signals** (proxy) | Counts of force-push, hard reset, `git clean -f`, recursive delete, pipe-to-shell, skipped git hooks, `chmod 777`, SQL drops, and reads/edits of secret-like files (`.env`, `*.pem`, keys, credentials). Only the category is stored, never the command |
| **Permission decisions** | The `permissionDecision` recorded with tool results, by decision and source, when the logs include it |

```
Quality score = weighted mean of: 40% verified + 20% tests alongside + 20% (1 − min(1, re-edit share ÷ 50%))
                + 20% first-run pass (only when a check ran; otherwise the others are re-weighted)
```
Needs 3 edit sessions.

---

## 15. Value tab (v1.4)

No score: a dollar figure has no universal "good", so each number is shown with
its change against your own previous period.

| Metric | Formula |
|---|---|
| **Estimated cost** | Token usage priced from the `PRICING` table (§3) |
| **Cost per 100 lines** | `estimated cost ÷ lines Claude wrote × 100` |
| **Cost per verified session** | Estimated cost of verified edit sessions ÷ their count |
| **Cache savings** | `(cache_read × 0.90 − cache_write × 0.25) × input price` per response: reads billed at 10 % instead of 100 %, minus the 25 % write premium. Assumes the same tokens would otherwise be sent uncached |
| **Cold-start tax / cache rebuilds** | As in §2, restricted to sessions in the window |
| **Cost spikes** | Days costing ≥ 2× the window's median active day (and ≥ $0.50); needs 5 active days. Names the biggest session that day |
| **Model mix** | Each model's share of estimated cost; a session's primary model is the one that cost the most in it |
| **Top-tier model on no-edit sessions** (proxy) | Share of top-tier-model cost (input price ≥ $10/M) spent in sessions where Claude made no edits |

---

## 16. Advisor tab (v1.4)

Ranked, evidence-quoting recommendations over the same 28-day window as My
Adoption. Rule-based: no AI is called and no tokens are used. Each carries its
confidence badge and, where it makes sense, a one-click **experiment**:
the dashboard stores the current value of the related metric in your browser
(`localStorage`) and shows how it moves. The figure uses a rolling 4-week window,
so it responds slowly.

| Rule | Fires when |
|---|---|
| Make Claude check its own work | verified after last edit < 60 % (3+ edit sessions) |
| First test or build run often fails | first-run pass < 60 % over 5+ session-check pairs |
| Sharper first prompts | clean first-pass turns < 60 % (8+ turns) |
| Break out of failure loops | 2+ stuck loops |
| A tool dominates your time | one tool or command ≥ 35 % of tool time and ≥ 60 s |
| Start a fresh session per task | 90th-percentile session ≥ 3 h, or median ≥ 90 min |
| Short sessions pay the cache set-up cost | cold-start tax ≥ 45 % over 8+ sessions |
| Cache rebuilt mid-session | ≥ 8 % of follow-up calls |
| Files edited again and again | re-edit share ≥ 35 % over 30+ edits |
| Ask for tests alongside code | tests alongside < 40 % (5+ edit sessions) |
| Risky commands / secret files | any detected |
| Cost spike | a spike day exists |
| Top-tier model on sessions with no edits | ≥ 50 % of top-tier cost (≥ $5) |
| Coach items | feature, habit, craft, impact and evening rules from §12 |

"What is going well" lists the matching positives, such as high verification or a long streak.

---

## 17. Data Health and confidence labels (v1.4)

Every metric on the new tabs has a badge:

| Badge | Meaning |
|---|---|
| **Observed** | read straight from a field in the log |
| **Derived** | computed from observed fields with a stated formula |
| **Proxy** | an indirect signal that may not mean what it suggests |
| **Estimated** | depends on list prices or a model |

The **Data Health** tab shows, from *your* logs, which signals exist (tool
timing, turn timing, permission decisions, thinking time, effort, hooks, subagent
activity, timestamps, session titles), the log entry types and counts, the Claude
Code versions seen, parse failures and duplicate lines dropped. A metric whose
signal is missing says so instead of showing zero. The **metric catalog** on that
tab lists every metric's formula and limitation (single source: `METRIC_CATALOG`
in the script). **Privacy mode** hides session titles, prompt previews and file
names on screen.

**Considered and rejected after checking real logs:** the `edited_text_file`
attachment ("a file changed outside Claude") also fires when Claude edits a file
through a shell command, so it cannot tell your hand edits from Claude's. It is
not used as a "human touch-up" metric.

---

## 18. Known approximations (honest accounting)

| Metric | Approximation |
|---|---|
| Est. cost | public API list prices; $0 real cost on subscription plans |
| Your tokens / leverage | ~4 chars-per-token heuristic, no real tokenizer |
| LoC | additions only; no deletion credit; rewrites count again |
| Session duration | wall-clock span including idle time |
| 7/30/90-day ranges | calendar days: today plus the N−1 days before it, local time (before v1.5 this was the last N *active* days) |
| Messages | meta entries, command output and interruptions are filtered — so the count is lower than the official panel's |
| Active time | 10-min buckets; an isolated message = 10 min; focus blocks don't span midnight |
| Task types | keyword heuristics on AI titles; ambiguous sessions land in "other" |
| Fluency scores | heuristic proxies, not measurements — trends week-over-week matter more than absolute values |
| Corrections | only catches corrections at the *start* of a prompt |
| Task mix / day chart | built from the 300 most recent sessions; very old days in long ranges may undercount |
| Cold-start tax / cache-reset rate | per-session, like reads/writes/LoC — if a custom date range starts mid-session, that session's *visible* first call is treated as its cold start even if the real first call happened earlier, outside the range |
| Adoption Score | a heuristic blend with hand-picked weights and targets; the levels are motivational labels, not a standard. Watch your trend rather than the exact number |
| Feature detection | inferred from tool names in the logs; a feature you used without a tool call (for example a CLAUDE.md you wrote but no session has run in) is only seen if the project folder still exists |
| Window | the last 28 calendar days ending on your most recent active day, so a long break does not make the score collapse |
| Turn and tool times | include any wait for you to approve an action; a session is not a task |
| Shell-made edits | edits Claude makes through shell commands (sed, scripts) are invisible to LoC, edit and re-edit counts |
| Verified / first-run pass | a check *ran* and exited without the error flag; it does not prove the code is correct |
| Command kinds | detected from the invoked command's name; unusual wrappers or custom scripts are classed "other" |
| Cache savings | list-price arithmetic that assumes you would otherwise have sent the same tokens uncached |
| Flow, Quality and Adoption scores | heuristic blends with hand-picked weights; the trend matters more than the number |

Everything else — token totals, cache numbers, tool counts, per-model and
per-project splits — is exact arithmetic over deduplicated log entries.

---

## 19. GitHub Copilot (v1.5)

Copilot's logs are translated into the same event shape as Claude Code's and run
through the same formulas, each tool on its own data. Sections 2-17 therefore
apply to Copilot as they are, except for what is listed here. Copilot's log
formats are undocumented and change between releases, so the field names the
reader uses are the best current knowledge, and anything it does not recognise is
skipped and counted on the Data Health tab.

### Credits, not dollars

Copilot's cost is **AI credits**, read from the logs and never priced from a
model table:

```
credits = nanoAiu / 1,000,000,000          (Copilot CLI, debug log, OpenTelemetry)
credits = copilotCredits as logged         (VS Code chat sessions, already in credits)
```

Every place the Claude view shows "Est. API cost" shows "Credits used" for
Copilot ("Credits per 100 lines" instead of "Cost per 100 lines", and so on). A
model call with no credits in the logs adds nothing and is counted on Data
Health, so the total can read low rather than be guessed. There is no dollar
conversion and no "% of monthly allowance" (that would need your plan).

### Premium requests

Some accounts are billed per request instead of in credits. Then a request's
details read like "Claude Opus 4.5 • 3x" (a multiplier) rather than "• 0.5
credits". The unit is decided per record from what the log says. Each user
prompt counts once times its multiplier, in a **separate "premium requests"
figure**. Premium requests are never added to credits, and a machine whose logs
hold both shows both.

### Approximate versus exact tokens

| How the session was logged | Tokens |
|---|---|
| Copilot CLI | **exact**, always (one usage record per model call) |
| VS Code debug log (setting `github.copilot.chat.agentDebugLog.fileLogging.enabled`) | **exact**; the debug log also stores your full prompts locally |
| OpenTelemetry file export (`--setup-copilot`) | **exact**; no prompt text by default |
| Otherwise: VS Code chat history | **approx.**: per-request prompt and completion tokens, and an agent turn may record only its **last** model call, so totals can read low |

Each session uses exactly **one** token source, never a sum, in that order. The
Data Health tab shows how many sessions are exact and how many are approximate,
and names the setting to turn on. Approximate figures carry the "approx."
badge on screen.

### The token convention

Claude's `input` excludes cache tokens (cache read and write are separate
fields, and cache hit % = cache read ÷ (cache read + input)). Copilot is made to
match wherever its log gives a split:

```
input = inputTokens − cacheReadTokens − cacheWriteTokens      (floored at 0)
```

Copilot's CLI, OpenTelemetry and debug logs report input *including* cached
tokens, hence the subtraction. VS Code chat history has no split: the whole
prompt counts as `input`, cache stays unknown, and the session is flagged "no
cache split". So Copilot's cache hit % is computed **only over the sessions that
have a split** (the exact ones in the selected range): cache read ÷ (cache read +
input) of those sessions, so approximate sessions never dilute it. When the
selected range holds no session with a split, cache read and cache hit % read
"Not available for Copilot" for that range, with the reason, never 0%. Turn on
exact tokens (above) to get a split for new chats.

### "Interrupted" and other figures Copilot may not have

A figure the logs do not carry is shown as "Not available for Copilot" with the
reason, never as 0.

- **Interrupted** comes from `requests[].modelState.value == 2` (VS Code's
  `ResponseModelState`, "Cancelled"). It is available only when the scanned chat
  logs record `modelState`; older logs read "Not available for Copilot". Data
  Health shows a "Stopped requests" row (ok / partial / missing).
- **Cache hit %** and cache reads: only when some sessions in the range have exact tokens (see above).
- **Cold-start tax** and **cache savings**: never available, because Copilot
  bills per request and there is no cache price to measure against.
- **Permission modes**: Copilot does not log approval modes.
- Inline "ghost text" completions are not logged at all, so they are not counted.

### Other differences

- **Tools and lines of code.** Copilot's real tool names are added to the
  read / write / command categories (for example `read_file`,
  `replace_string_in_file`, `run_in_terminal`), so charts show them as they are.
  Lines of code are counted from each write tool's arguments (additions only).
  Copilot skips write calls whose result failed (an `ERROR` result in chat
  history, `success: false` in an agent log), so a failed edit adds no lines;
  Claude Code's lines are counted as its logs record them, failed or not. Models are normalised so one model has one row
  (dates stripped, `claude-haiku-4-5` and `claude-haiku-4.5` are the same).
- **Projects, branches, titles.** The project is the VS Code workspace folder (or
  the Copilot CLI's working folder); chats with no folder open are "(no folder)".
  The branch comes from the CLI or the session store, else "(none)". A session's
  title is Copilot's own, else the session store's summary, else the first prompt.
- **Project instructions** look for `.github/copilot-instructions.md` or
  `AGENTS.md` instead of `CLAUDE.md`.
- **VS Code vs CLI.** A small chart on Copilot's Overview splits prompts by where a
  session was *started* (VS Code chat or Copilot CLI).
- **My Adoption and Advisor** use Copilot's own feature list (agent / ask / edit /
  plan mode, subagents, MCP tools, custom instructions, prompt files, terminal
  commands, web fetch, model variety) and only the tips whose inputs exist for
  Copilot. The scoring formulas are unchanged.
- **Plan usage limits and toast notifications** are Claude-only and are hidden for
  Copilot and All AI.

### All AI: what may be combined

The **All AI** view builds both tools over **one** date window (so its per-tool
split always matches each tool's own page for the same button) and combines only
what means the same thing in both:

| Quantity | Rule |
|---|---|
| Total tokens | input + cache read + cache write + output, summed over both tools, always with the per-tool split. Copilot's part of the split, and its series in the daily chart, carry "approx." when some of its sessions in the range have approximate tokens (`by_tool.copilot.token_modes`) |
| Prompts, sessions | summed |
| Active days | days with **any** AI activity (set union) |
| Active time and focus | the **union of 10-minute slots** across tools, never a sum: ten minutes spent in both tools counts as ten |
| Weekday × hour heatmap, late-night and weekend share | built from the same union of 10-minute slots, not from log-line counts (Claude writes many lines per prompt and Copilot few, so summing lines would measure log chattiness instead of time) |
| Models | one list with each row tagged by its tool; a model used through both tools appears as two rows |
| Projects | merged by the project label, which is Claude Code's folder naming (every non-alphanumeric character becomes "-") with the drive letter normalised, so the same folder used from both tools is one row, with per-tool numbers side by side. Two different folders whose names differ only in punctuation (for example `my.proj` and `my-proj`) share a label and are merged |
| Cost | **never summed**: Claude's "$ est." and Copilot's credits (and premium requests) are shown side by side |
| Interruptions | counted only from tools that log them; the payload names them in `interruptions_from` |
| Deltas vs the previous window | computed on the combined totals with the same rules |

All AI has four pages (Overview, Time & Focus, Tools & Models, Projects). Weekly
Report, My Adoption, Flow, Quality, Value, Advisor and Data Health are per-tool
and are dimmed while All AI is selected. All AI is offered only when **both** tools
have data on this machine.
