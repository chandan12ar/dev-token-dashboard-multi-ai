# Dashboard Guide — every section explained

A plain-language walkthrough of the whole dashboard: what each section
tells you, where its data comes from, and how to read it. Written so you
can present the dashboard to someone who has never seen it.

For the exact math behind any number, see [METRICS.md](METRICS.md).
For the code architecture, see [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

---

## The one-minute story

> Every time you use Claude Code, it saves a detailed log file on your
> machine — every prompt you typed, every response, every file it edited,
> with timestamps and token counts. This dashboard reads those log files
> (and nothing else), does the math locally, and turns them into charts.
> It never calls any API, costs zero tokens, and nothing leaves the
> machine. It answers three questions: **how much am I using Claude, what
> am I using it for, and how well am I working with it?**

## How the data becomes the dashboard

```
~/.claude/projects/**/*.jsonl        (logs Claude Code already writes)
        │
        ▼
  Scanner  — finds every log file; re-reads only files that changed
        │
        ▼
  Parser   — each log line becomes numbers:
             · assistant messages → tokens, model, est. cost (deduplicated)
             · your prompts       → prompt counts, words, typed-token estimate
             · tool calls         → lines of code, reads vs writes, commands run
             · timestamps         → active time, heatmap, quiet hours, streaks
        │
        ▼
  /api/stats — one JSON payload, rebuilt on demand
        │
        ▼
  The web page — Chart.js renders it; auto-refreshes every 15 seconds
```

One Python file, standard library only, served at `http://localhost:8788`,
bound to your machine only (`127.0.0.1`). The page makes zero external
requests — even Chart.js is bundled locally.

---

## The tool switch *(v1.5)*

In the top bar, left of the date range, a switch picks whose numbers you are
looking at: **Claude Code | GitHub Copilot | All AI**.

- **It only appears when more than one tool has data** on this machine. With
  only Claude Code logs (or `--no-copilot`) the page looks as it always did.
- **Colours tell you which tool you are in.** Claude Code keeps its orange,
  GitHub Copilot is violet, All AI is teal. The same three colours mark each
  tool inside All AI charts. Text that names the tool changes too: "You vs
  Claude" becomes "You vs Copilot", the sidebar says "GitHub Copilot analytics",
  and "Est. API cost" becomes "Credits used".
- **Your choice is remembered** in this browser, and switching keeps the section
  and range you are on.
- **Each tool is computed on its own data.** Copilot gets the same sections as
  Claude Code (Overview, Time & Focus, Tools & Models, Projects, Weekly Report, My
  Adoption, Flow, Quality, Value, Advisor, Data Health), with Copilot's own
  tool names, feature list and tips. Claude's numbers are never mixed into
  Copilot's, or the reverse.
- **All AI is a separate summary** with four pages: Overview, Time & Focus,
  Tools & Models and Projects. The other pages are per-tool, so they are dimmed
  (not hidden) while All AI is selected; hover one for the reason. Switching to
  All AI from one of them lands on Overview. Combined totals add up tokens,
  prompts and sessions; active time counts the minutes you used *either* tool
  once; models and projects show each tool's share. **Cost is never added**:
  Claude's estimated dollars and Copilot's credits sit side by side. The rules
  are in [METRICS.md §19](METRICS.md#19-github-copilot-v15).

### "Not available for Copilot"

Copilot's logs do not carry everything Claude Code's do. When a card's number
cannot be known, it says **Not available for Copilot** with a one-line reason in
the card's normal place, instead of a `0`. Typical ones:

| Card | Why |
|---|---|
| Interrupted (in Friction) | only shown when your VS Code records stopped requests; older logs do not. The Friction card then reads "Tool errors" |
| Cache read and hit % | Copilot's chat history has no cache split; it shows up once some sessions have exact tokens |
| Cold-start tax, cache savings | Copilot bills per request, so there is no cache price to measure |
| Approval modes | Copilot does not log them |

A number that exists but is only an estimate (Copilot token counts from chat
history) carries an **approx.** badge. The Copilot **Data Health** tab lists
which signals your logs have, and whether token counts are exact or approximate.
If Copilot is selected but no Copilot logs are found, a friendly panel says where
the dashboard looked.

### Exact Copilot tokens

By default Copilot's chat history gives approximate tokens (per request, and an
agent turn may log only its last model call). Three things give exact ones: the
Copilot CLI (always exact), the VS Code setting
`github.copilot.chat.agentDebugLog.fileLogging.enabled` (note: this also stores
your full prompts on your disk), or the OpenTelemetry export that
`python ai_fluency_insights.py --setup-copilot` switches on (no prompt text by
default). See [SETUP.md](../SETUP.md#3c-optional-github-copilot).

---

# Tab 1: Dashboard

## Header & range bar

- **Live · 0 tokens** pill — a reminder the dashboard itself never spends
  tokens; it only reads files.
- **Range buttons (7 / 30 / 90 days / All time / custom dates)** — every
  card, chart, and table below follows the selected range. The Weekly
  Report tab is the exception: it is always full history. 7 / 30 / 90 days
  means today plus the days before it (calendar days, your local time), so
  "7 days" is always a full week even if you did not work on some of them.
- **Tool switch** — Claude Code / GitHub Copilot / All AI, see
  [above](#the-tool-switch-v15).

## Momentum bar

| Item | What it tells you | How it's computed |
|---|---|---|
| **Day streak** | consecutive days you've used Claude Code, counted back from your most recent active day | calendar days that have at least one logged message |
| **Best streak** | your longest-ever run | same, over all history |
| **Active days** | total days you've ever used it | count of days with activity |
| **Goal bars** | progress toward personal targets (daily LoC, token budgets) | optional — set in the `GOALS` dict at the top of the script |

*How to read it:* purely motivational — a GitHub-style contribution
streak for AI-assisted work.

## Plan usage limits

Shows your real Anthropic 5-hour and 7-day rate-limit usage — the same
percentages the `/usage` command and claude.ai report — not a locally
guessed estimate.

| Item | What it tells you | Where it comes from |
|---|---|---|
| **Current session** | % of the rolling 5-hour plan window used | `rate_limits.five_hour.used_percentage` from Claude Code's `statusLine` hook |
| **This week** | % of the rolling 7-day window used | `rate_limits.seven_day.used_percentage`, same hook |
| **"as of Xm ago" / ⚠ warning** | how old this snapshot is | see below |

*How it's captured:* a `statusLine` hook (`~/.claude/ai_fluency_insights_statusline.js`
if you used `--setup-notifications`, or your own script if you already had
one) writes Anthropic's real rate-limit numbers to
`~/.claude/rate_limits_latest.json` every time it runs. The dashboard
just reads that file — it never calls the Anthropic API itself, so this
stays free.

*Setting it up:* run `python ai_fluency_insights.py --setup-notifications`
(add `--dry-run` to preview first) — it configures the `statusLine` hook
for you and won't touch an existing custom one. See
[SETUP.md](../SETUP.md#3b-optional-accurate-plan-usage-numbers-and-toast-notifications).

*Why it can lag:* the hook only re-runs when a Claude Code session on
this machine is actually active (a new message, `/compact`, etc.) — not
on a fixed timer, unless you set `refreshInterval` in `statusLine`
settings (this project sets it to 30s). If no session has been active
recently, the number shown is real but not current, and the card marks
it with a yellow ⚠ once it's more than 2 minutes old. Refreshing the
browser can't fix that by itself — it re-reads the same file; only new
Claude Code activity (in any window) writes a fresher one. If the file
is missing entirely or older than 30 minutes, the card falls back to a
local token-count estimate and labels itself "(estimate)" instead of
"(live)".

## KPI cards

Each card shows the total for the selected range, plus a ▲/▼ percentage
vs the *previous window of the same length* (e.g. this 30 days vs the 30
days before).

| Card | What it tells you |
|---|---|
| **Input tokens** | text sent *to* Claude — your prompts plus the context (files, history) the agent gathers |
| **Output tokens** | everything Claude generated — answers, code, tool calls |
| **Cache read** | context Claude re-used from cache instead of re-processing. The **hit %** is why long sessions stay cheap — 99% means almost all context was re-used |
| **Est. API cost** | what this usage *would* cost at public API prices. On a subscription plan your real marginal cost is **$0** — this is a "value received" number, not a bill |
| **Lines of code** | lines Claude actually wrote into files (via its Write/Edit tools). Counted from the real content — additions, not a git diff |
| **Prompts** | messages you hand-typed (slash commands and system noise are filtered out) |
| **Sessions** | distinct Claude Code conversations, with average duration |
| **Friction** | times you hit Esc to interrupt + failed tool calls — a "how often did the workflow hurt" counter |
| **Your tokens** | an estimate of tokens *you personally typed* (~4 characters per token) |
| **Leverage** | the headline number: **Claude output tokens ÷ tokens you typed**. 50× means every token you type produces fifty back |

For GitHub Copilot the cards are the same, with Copilot's units: **Credits
used** replaces Est. API cost (credits as Copilot's logs report them, no dollar
conversion), and a **Premium requests** figure appears when your account is
billed per request. Credits and premium requests are never added together.

*How to read it:* Leverage is the best single "what does AI assistance
multiply my effort by" number. Friction trending down over weeks means
your prompting is improving.

## Daily tokens (bar chart)

Input / output / cache-read stacked per day. Cache read (grey) dwarfs
the rest by design — that's context re-use, not new spend.

## You vs Claude (line chart)

Tokens you typed vs tokens Claude produced, per day, on a **log scale**
(the gap is usually 10–100×, a linear scale would flatten your line to
zero). The vertical distance between the lines *is* your leverage,
visualised.

## Time & wellness *(v1.2)*

The "am I working sustainably" panel.

| Stat | How it works |
|---|---|
| **Active time** | every message timestamp lands in a 10-minute bucket; active time = buckets × 10 min. Much closer to real hands-on time than session wall-clock (which includes idle time) |
| **Avg per active day** | active time ÷ days that had any activity |
| **Longest focus block** | longest unbroken run of consecutive 10-min buckets in a single day |
| **Quiet hours %** | share of messages between 23:00 and 06:00 (tunable in the script) |
| **Weekends %** | share of messages on Sat/Sun |

The bar chart below shows active minutes per day — bars turn **red** on
days where more than a quarter of activity happened during quiet hours.

*How to read it:* inspired by Anthropic's "Reflect" wellness features.
A red-bar week or a rising quiet-hours share is worth a conversation
with yourself, not a metric to maximise.

## Model usage (doughnut)

Output tokens split by model (Opus, Sonnet, Haiku…). Tells you which
models do your heavy lifting — and, together with the model breakdown
table, where the estimated cost comes from.

## Lines of code / day (line chart)

Code Claude physically wrote into files each day. Spiky by nature —
big features spike it, research days flatline it. Zero on a busy day
just means the day was exploration, not implementation.

## Exploration vs building (stacked bars)

Every tool call is classified:
- **Exploration** = reading tools (Read, Grep, Glob, web search…)
- **Building** = file-editing tools (Write, Edit…)

*How to read it:* neither side is "good". A 90%-exploration day was
research or debugging; a 90%-building day was implementation. What
matters is whether the mix matches what you *meant* to be doing.

## Friction / day (stacked bars)

Interruptions (you pressed Esc because Claude went the wrong way) and
tool errors (a command or edit failed), per day. Spikes usually map to
a specific painful session — hover the day and go look at it.

## Task mix / day *(v1.2, stacked bars)*

Claude Code auto-titles every session. The dashboard keyword-matches
those titles to classify each session: **feature / bugfix / refactor /
docs / explore / other** (with the session's tool mix as a fallback when
the title is ambiguous).

*How to read it:* your work-type profile over time. A month of nothing
but bugfix bars tells a very different story from a feature-heavy one.
It's a heuristic — expect roughly-right, not perfect.

## Tool usage & slash commands (horizontal bars)

Raw counts of which tools Claude used (Edit, Bash, Read…) and which
slash commands you ran. A quick portrait of *how* you and Claude
actually work.

## Activity heatmap

Messages by weekday × hour of day, GitHub-style. Your working rhythm at
a glance — and the quickest way to spot a late-night habit.

## Tables

- **Projects** — tokens, LoC, prompts, sessions, and estimated cost per
  project. **Click any row** for a deep-dive modal: that project's daily
  chart, its sessions with titles, and its most-edited files.
- **Model breakdown** — the doughnut, in numbers.
- **Git branches** — output and LoC attributed per branch (Claude Code
  stamps every log entry with the git branch it ran on).
- **Longest prompts** — your biggest prompts, with previews. Fun, and
  occasionally revealing about what you over-explain.
- **Most-edited files** — where Claude's code actually landed, by LoC.

## Exports

- **Copy** — a text summary of the current range for pasting anywhere
- **CSV** — the per-day table, for spreadsheets
- **JSON** — the entire stats payload, for your own scripts

---

# Tab 2: Weekly Report

Built for one job: **your standup / weekly update writes itself.**
Browse any week with ‹ Prev / Next ›.

## KPI row

Sessions, focus hours, output tokens, LoC, prompts, and building % —
each with a delta vs the previous week.

## What you worked on

Claude Code already stores an AI-generated title for every session
(free — no extra tokens). The dashboard groups them by project with
date, duration, LoC and prompt counts: ready-made standup bullets.

## Exploration vs building & day-by-day charts

The week's read/write split as a doughnut, and output + LoC per weekday.

## AI Fluency report *(v1.2)*

Four 0–100 scores per week, adapted for coding from Anthropic's **4D
AI-fluency framework**, drawn as a radar (orange = this week, blue =
last week) with the underlying numbers and one tip per dimension:

| Dimension | The question it answers | Fed by |
|---|---|---|
| **Delegation** | do you hand Claude real building work, or only ask questions? | % of sessions that shipped code + your leverage ratio |
| **Description** | do your first prompts land, or need a second try? | % of prompts that needed no correction/interruption + prompt length sweet spot |
| **Discernment** | do you actually review and steer the output? | corrections + interruptions (pushing back is *good* here) + tool-error control |
| **Diligence** | do you verify what got written? | % of code-writing sessions that also ran a command (tests, builds, runs) afterwards |

Note the deliberate tension: a "correction" prompt (starting with *no /
wrong / actually / undo…*) **lowers** Description (first prompt didn't
land) but **raises** Discernment (you caught it). That's intentional.

*How to read it:* these are heuristic proxies, not measurements — the
week-over-week *shape* on the radar matters far more than any absolute
number.

## Reflection *(v1.2)*

Each week, the data picks **one question worth sitting with** — e.g.
"22% of this week's activity happened between 11pm and 6am. Is
late-night coding a deliberate choice — or a habit worth questioning?"
The rules fire on high correction rates, heavy quiet-hours use, extreme
exploration/building skew, or low verification; the fallback is
Anthropic's classic: *"What's one thing you want to keep doing yourself,
even if Claude could do it faster?"*

**Discuss with Claude** sends the question plus the week's stats through
`claude -p` for a short reflection with one concrete experiment to try.

## Copy as Markdown & AI summary

- **Copy as Markdown** — one click: the week's sessions, numbers, work
  split, and task mix as a formatted update for Teams / Slack / email.
- **AI summary** — pipes the week through `claude -p` for a polished
  first-person paragraph.

These two buttons (plus "Discuss with Claude") are the **only** features
in the entire dashboard that consume tokens — and only when pressed.

---

# Tab 3: My Adoption *(v1.3, extended in v1.4)*

A personal scorecard: **how deeply are you using Claude Code, and are you
getting better at it?** It is for you, not for a manager. It only ever
compares you with your own past, and it never leaves your machine. Like the
Weekly Report it always uses full history, so the range filter is hidden.

## Your score and level

One number from 0 to 100 and one of six levels: **Explorer → Practitioner →
Power user → Expert → Pro → Pro Max**. The ring and the ladder under it show
how far you are and how many points you need for the next level. The top two
need more than a score: **Pro** needs every pillar at 50 or more, and **Pro Max**
needs every pillar at 70 or more and weekly scores of 85 or more for four weeks
in a row. If a gate is holding you back, the page says exactly what to fix. A
short sentence underneath sums up the last 28 days in plain words, and a ▲/▼
shows the change versus the 28 days before.

## Four pillars

| Pillar | Plain-English meaning |
|---|---|
| **Habit** | Do you show up regularly? Days used and how evenly they spread across the weeks |
| **Range** | Do you use what Claude Code offers? Plan mode, subagents, skills, connected tools, CLAUDE.md... |
| **Craft** | Do you work with Claude well? Mostly the four AI-fluency skills from the Weekly Report, blended with your **Flow** and **Quality** scores |
| **Impact** | What does Claude get done for you? Lines written per active day and how often it ships code |

Each tile has a bar, a ▲/▼ versus the previous 4 weeks, and a tag for your
strongest pillar and the one with most room to grow.

## What changed lately, and your adoption journey

Two cards under the score. **What changed lately** lists up to four plain lines:
a pillar that rose or fell by 5 points or more, a level change, or a feature you
used for the first time. **Your adoption journey** shows four stages, each with its
evidence: *Started*, *Recurring* (2+ active days a week for 3 weeks running),
*Integrated* (4+ features in use) and *Verified outcomes* (at least half of your
edit sessions ended with a test, build or lint).

## Your next 3 moves

Up to three concrete suggestions picked from your own data, such as "You haven't
tried plan mode yet" with a one-line how-to. It is rule-based (no AI, no tokens).

## The detail cards

- **Habit** — a GitHub-style calendar of the last 17 weeks, plus active days per week
- **Range** — a checklist of ten features (adds hooks and task lists); used ones show how often, unused ones show how to start
- **Craft** — the four fluency skills plus Flow and Quality, each with a bar and trend arrow, and links to those tabs
- **Impact** — lines Claude wrote, share of active days that shipped code, leverage, and a weekly chart
- **Your adoption over time** — the score at the end of each of the last 12 weeks
- **Healthy habits** — active time, longest focus block, late-night and weekend share
- **How your score is worked out** — a collapsible plain-language summary of the weights

Exact formulas: [METRICS.md §12](METRICS.md#12-my-adoption-v13-extended-in-v14).

---

# Tabs 4-8: Engineering insight *(v1.4)*

Five more tabs, grouped under **Engineering insight** in the sidebar. They
answer the harder questions: *does work flow smoothly, is it checked, what does
it cost, and what should I change?* All of it comes from the same local logs.
Nothing needs Jira, GitHub or CI. Every number has a small badge (**Observed**,
**Derived**, **Proxy** or **Estimated**) so a rough signal is never presented as a fact.
Hover a badge for the formula.

## Flow

How smoothly work moves, over the selected range. A score out of 100, plus:
**turn time** (your prompt to Claude finishing), **clean first-pass turns** (no
error, interruption or correction), **failures fixed** (and stuck loops of 3+
failures in a row), and **time to first edit**. Charts show turn time and clean
turns per day; "Where tool time goes" shows which commands eat the clock.

## Quality

Whether speed comes with checking. A score plus: how often a **test, build or
lint ran after Claude's last edit**, whether the **first run passed**, whether
**tests were written alongside code**, and the **re-edit share**. Below: rework
hotspots (files edited again and again), friction, **safety signals**
(force-push, recursive delete, secret-like files) and how actions were permitted.

## Value

What it costs and what you get back. No score: dollars have no universal
"good", so each figure is compared with your previous period. Shows cost, cost
per 100 lines, cost per verified session, **how much caching saved**, daily cost
with spikes marked, and the model mix.

## Advisor

Your top moves, picked from your data, each quoting its evidence, for example
"A test ran after Claude's last edit in 7 % of edit sessions (1 of 15)". Pick
**Try it as an experiment** and the dashboard remembers the number today and shows
how it moves. "What is going well" balances the list. Rule-based: no AI, no tokens.

## Data Health

What *your* logs let us measure and what they cannot, so a missing signal is
shown as missing, not zero. Also the log inventory, Claude Code versions, the
full **metric catalog** with each formula and limitation, and **Privacy mode**
(hides titles, prompt previews and file names on screen).

Exact formulas: [METRICS.md §13-§17](METRICS.md#13-flow-tab-v14).

---

# FAQ — questions your team will ask

**"Can it show GitHub Copilot as well?"**
Yes. Use the switch in the top bar. It reads Copilot's local VS Code chat logs
and the Copilot CLI's logs, nothing else, and nothing leaves the machine. It
works for any Copilot plan, because it reads whatever Copilot wrote to disk.

**"Why does Copilot show credits and not dollars?"**
Copilot's logs report AI credits, so that is what is shown. Nothing is priced
from a model table, and credits are never added to Claude's estimated dollars.

**"Why does a Copilot card say 'Not available'?"**
Your Copilot logs do not contain that figure (for example, older VS Code does
not record stopped requests). It is never shown as 0. The Data Health tab says
what is missing. If the numbers look wrong, run the read-only log checker
described in [SETUP.md](../SETUP.md#3c-optional-github-copilot) and look at its
report.

**"Are Copilot's token counts exact?"**
Only for the Copilot CLI, or once you turn on the debug log or the
OpenTelemetry export (`--setup-copilot`). Otherwise they are approximate and
marked "approx.".

**"Is this sending our code anywhere?"**
No. It reads local log files, serves a page on `127.0.0.1` only, and
makes zero external requests. Prompts and code never leave the machine.

**"Why does it show a cost when we're on a subscription?"**
It's what the usage *would* cost at public API prices — a value-received
estimate, not a bill. On a subscription the marginal cost is $0.

**"Why are the token counts lower than Claude Code's own panel?"**
The dashboard deduplicates per API response; the official panel counts
some entries more than once. Full analysis in
[TOKEN_COUNTS.md](TOKEN_COUNTS.md) — the dedup'd number is the accurate
one.

**"How accurate is 'lines of code'?"**
It counts lines actually written by Claude's editing tools — real
content, but additions only (no credit for deletions; rewritten lines
count again). Treat it as an activity indicator, not a productivity KPI.

**"Why is my Adoption Score lower than I expected?"**
It is a self-comparison tool with deliberately demanding targets (about three
active days a week, six features in use, 300 lines per active day for full
marks). Look at the four pillars and "Your next 3 moves", and watch the trend.
A rising score matters more than the number.

**"Can it tell me my acceptance rate?"**
Only if your logs record permission decisions. The Data Health tab shows whether
they do. If not, it says so rather than inventing a number. The closest honest
signals are *verified after last edit* and *re-edit share*.

**"Does this need Jira, GitHub or CI?"**
No. Everything uses the Claude Code logs on your machine. Metrics that need other
tools (PR cycle time, deploy frequency, defects) are deliberately not shown.

**"Are the fluency scores real measurements?"**
No — honest heuristics, documented formula-by-formula in
[METRICS.md](METRICS.md). Watch the trend, not the value. Everything
that *is* exact (tokens, cache, tools, per-model/project splits) is
exact arithmetic over deduplicated logs.

**"Plan usage % doesn't match what claude.ai/`/usage` shows me right now"**
It's real Anthropic data, not an estimate — but it's captured passively
by a `statusLine` hook that only updates when a Claude Code session on
this machine is active, so it can be a few minutes behind. See
[Plan usage limits](#plan-usage-limits) above; the card itself flags a
stale reading with a yellow ⚠ instead of hiding the lag.
