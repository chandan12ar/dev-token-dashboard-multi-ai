# AI Fluency Insights — Problem, Solution and Roadmap

*A plain-language report on what problem this tool solves, what every page does,
how every number is calculated, and where it can go next.*

---

## Contents

1. [Executive summary](#1-executive-summary)
2. [The problem](#2-the-problem)
3. [The idea](#3-the-idea)
4. [How it works (and why it is safe)](#4-how-it-works-and-why-it-is-safe)
5. [Design principles](#5-design-principles)
6. [A tour of every page](#6-a-tour-of-every-page)
7. [How the key scores are calculated](#7-how-the-key-scores-are-calculated)
8. [How it helps the individual developer](#8-how-it-helps-the-individual-developer)
9. [Trust, privacy and honesty](#9-trust-privacy-and-honesty)
10. [Limits of the current version](#10-limits-of-the-current-version)
11. [Roadmap: how this can grow](#11-roadmap-how-this-can-grow)
12. [Appendix: glossary and metric quick reference](#12-appendix)

---

## 1. Executive summary

AI coding assistants are now part of daily engineering work. Using them well
is a skill, and like any skill it improves faster when you can see what you are
doing. Today, almost nobody can.

**AI Fluency Insights turns the log files Claude Code already writes on
each developer's machine into a private, personal view of how they use it:
how much, how well, and what to try next.**

- **100 % local.** One Python file, standard library only. It never calls an
  API, uses no tokens, and sends nothing anywhere. It works offline.
- **One place instead of many.** Claude Code shows usage through about ten
  separate commands and screens, most of them one-session snapshots. This puts
  usage, cost, habits, quality and history in a single dashboard with one date
  range and one set of definitions ([section 2](#2-the-problem)).
- **Zero setup cost.** `python ai_fluency_insights.py` and open
  `http://localhost:8788`. No installs, no accounts, no admin rights.
- **Built for the developer first.** It compares a person only with their own
  past. It never ranks people against each other.
- **Honest about its numbers.** Every metric on the newer pages carries a
  badge (Observed, Derived, Proxy or Estimated) so a rough signal is never
  shown as a fact.
- **Eleven pages** answer six questions: *How much am I using it? What is it
  doing for me? Am I using it well? Is the work getting checked? What does it
  cost? What should I do differently?*

It is tested (142 automated tests), documented, and every formula is published
in [METRICS.md](METRICS.md).

---

## 2. The problem

### 2.1 The situation

Claude Code is powerful, but how it is used varies enormously from one
developer to the next. Two people with the same tool and the same licence can
get very different results:

- One starts every task with a clear prompt, lets Claude run the tests, keeps
  project instructions in a `CLAUDE.md` file, and uses plan mode for risky
  changes.
- Another types short, vague requests, accepts the output without checking it,
  fixes the same file five times in a row, and has never tried most of the
  features.

Both show up as "a Claude Code user". Nothing in their day tells either of
them which one they are.

The obvious reply is: *"Claude Code already has commands that show usage."* It
does, and they are useful. The problem is what happens when you try to answer a
real question with them.

### 2.2 Visibility exists, but it is scattered

Claude Code gives you several ways to look at your usage. Each one answers one
narrow question, in a different place, for a different scope. This is how they
stand in the current Claude Code documentation:

| Where you look | What it shows | Scope | What it cannot tell you |
|---|---|---|---|
| `/usage` (also reachable as `/cost` and `/stats`) | Session cost estimate, API and wall-clock time, lines added/removed, tokens by model, a prompt-cache line. On subscription plans, plan-usage bars and a breakdown by skill, subagent, plugin and MCP server | The session block **resets when you `/clear`**. The plan view covers the last 24 hours or 7 days, from this machine's history | A month of history, trend lines, per-project cost, or whether the work was any good |
| `/context` | What is filling the context window right now, with suggestions | The current session only | Anything about past sessions |
| `/status` | Version, model, account, connectivity | Right now | Any usage or cost number |
| `/insights` | An AI-written HTML report on recent sessions (projects, patterns, friction, suggestions) | Up to 200 new sessions per run, on this machine | It is a document you generate on demand, **and it spends your tokens** |
| `/resume` | A list of past sessions with title, branch and size | Local transcripts | Cost, quality, or any comparison between sessions |
| `/export` | A plain-text copy of one conversation | The current session | Anything across sessions |
| `/memory`, `/permissions`, `/hooks`, `/agents`, `/mcp`, `/model` | How Claude Code is *configured* | The current setup | Whether you actually *use* what you configured |
| Status line | Live cost, context % and, for Pro/Max, plan limits, shown while you work | The current session; you must write or install a script | Anything once you close the window: nothing is kept |
| OpenTelemetry export | Metrics and events (cost, tokens, active time, lines, tool decisions) streamed to a backend | Whatever you collect | It needs an endpoint, a collector and dashboards someone has to build and run |
| Admin analytics (Console, claude.ai) | Team usage, estimated spend, acceptance rates, per-user views | Owners and Admins on Team/Enterprise plans | A personal view for an individual developer; the perspective is organisational |

**Read down that table and the pattern is clear.** There are about ten different
surfaces. Most are one-moment, one-session snapshots shown in a terminal. They
disappear when you scroll or `/clear`. The ones that last (telemetry, admin
analytics) need infrastructure or administrator access and look at people from
above. Nothing in the list gives an individual developer **one place, with
history, that connects usage to habits and outcomes.**

### 2.3 One question, many places

Here is what it takes to answer ordinary questions a developer might ask.

| The question | Closest built-in answer | What is still missing | Dashboard page |
|---|---|---|---|
| "How much did I use Claude last month, per project and per model?" | `/usage` shows the current session | A month view, split by project and model | Overview, Tools & Models, Projects |
| "What did that spike on Tuesday come from?" | `/usage` covers the current session and a 24-hour / 7-day plan view | The day, the session and the model behind a spike | Value |
| "Am I any better at using Claude than I was a month ago?" | No built-in view found in the docs | A baseline and a trend | My Adoption |
| "Is Claude's code getting tested?" | No built-in view found in the docs | Whether a test ran after the last edit | Quality |
| "Why does everything feel slow and stuck?" | `/usage` shows API time, not where it went | Turn time, failure loops, slow commands | Flow |
| "Which Claude Code features have I never tried?" | `/memory`, `/hooks`, `/mcp`... show what is *set up* | What is actually *used* | My Adoption |
| "What should I do differently?" | `/insights` gives a narrative (and uses tokens) | Ranked, evidence-based moves at no token cost | Advisor |
| "What did I do this week?" | `/resume` lists sessions | A grouped summary for a standup | Weekly Report |
| "Am I working too late?" | Not in a personal view (telemetry records active time, but you must collect and chart it) | Active time, quiet hours, weekends | Time & Focus |
| "Can I trust these numbers?" | No built-in view found in the docs | What the logs contain, and how sure each metric is | Data Health |

To answer even three of these today a developer would need to run several
commands, in the right session, at the right moment, and copy the results by
hand into a spreadsheet. In practice they don't, so the questions go
unanswered.

### 2.4 The problems, one by one

**Problem 1: Information is split across many commands and screens.**
*Today:* cost is in one command, context in another, sessions in a third,
configuration in half a dozen more, and team figures in a web console that most
developers cannot open. *Why it hurts:* no one assembles the pieces, so no one
sees the whole picture. *What the dashboard does:* puts all of it on one page
with one date selector, one set of definitions and one look, so a number on one
tab means the same thing on every other tab.

**Problem 2: Usage is a snapshot, not a history.**
*Today:* the figures that are easy to see belong to "right now" or "this
session", and some reset when you clear the conversation. *Why it hurts:*
improvement is a change over time, and you cannot see change without history.
*What the dashboard does:* reads every session log still on the machine, so it
can show 7 days, 30 days, 90 days, all time or a custom range, always with the
change against the previous equal-length period.

**Problem 3: Activity is shown, effectiveness is not.**
*Today:* the built-in views tell you how much you did (tokens, minutes, lines).
*Why it hurts:* "how much" says nothing about "how well". *What the dashboard
does:* adds the missing layer, with Flow (does work move smoothly), Quality
(was it checked) and Adoption (are you using the tool deeply and getting better).

**Problem 4: The easy yardsticks are the wrong ones.**
*Today:* when people do measure, they count tokens or lines produced. *Why it
hurts:* these reward waste. A sharp prompt that gets a small, correct change
looks worse than a long loop of retries. *What the dashboard does:* shows
tokens and lines only as context, never as a goal, and builds scores on
behaviour: clean first-pass turns, recovery from failures, verification after
edits, and use of the right features.

**Problem 5: Configuration is visible; use is not.**
*Today:* `/memory`, `/hooks`, `/agents` and `/mcp` tell you what is set up.
*Why it hurts:* a feature that is configured but never used, or a capability
nobody knows about, stays invisible. *What the dashboard does:* the My Adoption
checklist detects ten capabilities from real tool calls (project memory,
run-and-verify, plan mode, subagents, skills, hooks, task lists, connected
tools, slash commands, web research) and shows which ones you have used and
which you never tried.

**Problem 6: Quality of AI-written code is invisible.**
*Today:* no built-in view tells you whether Claude's last edit was followed by a test,
whether the first test run passed, or which files keep being reworked. *Why it
hurts:* unchecked AI code is the biggest risk of working with an assistant.
*What the dashboard does:* the Quality page measures verification after the last
edit, first-run pass rate, tests written alongside code, rework hotspots and
risky-command signals.

**Problem 7: Cost has no context.**
*Today:* spend is a total (and, on a subscription plan, a share of a limit).
*Why it hurts:* you cannot tell a healthy $40 from a wasteful $40, or explain
a spike. *What the dashboard does:* Value shows cost per 100 lines, cost per
verified session, cache savings, spike days with the session behind them, and
whether an expensive model is being used on sessions that edited nothing.

**Problem 8: There is no feedback and no path to get better.**
*Today:* no built-in view tells a developer what "good" looks like or what to try next.
*Why it hurts:* skill grows slowly and by accident. *What the dashboard does:*
My Adoption gives a score, six levels with a clear way up, and the next three
moves. The Advisor ranks the most valuable changes, quotes the evidence behind
each, and lets you run a one-click experiment to see whether the change worked.

**Problem 9: Reporting takes effort.**
*Today:* "what did you work on this week?" is rebuilt from memory.
*Why it hurts:* it wastes time and loses detail. *What the dashboard does:* the
Weekly Report builds it from the session titles Claude Code already stores and
copies it as Markdown in one click.

**Problem 10: The tools that last need infrastructure or admin access.**
*Today:* durable analytics means setting up OpenTelemetry with a collector and
dashboards, or having an Owner/Admin role on a Team or Enterprise plan.
*Why it hurts:* an individual developer has neither, and the organisational
view is about people, not about helping a person improve. *What the dashboard
does:* needs one Python file and no accounts. The developer owns the data and
the view.

**Problem 11: Raw numbers do not tell you what to do.**
*Today:* even good data stays a table of figures. *Why it hurts:* people stop
looking when the data does not say what to change. *What the dashboard does:*
every page ends in plain language, and the Advisor turns the data into a short
list of moves ("only 38 % of your edit sessions were verified, so ask Claude to
run the tests").

**Problem 12: Raw log numbers cannot be trusted as they are.**
*Today:* Claude Code's logs repeat the same response more than once, so naive
counting inflates tokens and cost. Different Claude Code versions also write
different fields. *Why it hurts:* a dashboard built on inflated or missing data
misleads. *What the dashboard does:* de-duplicates every response, and the Data
Health page shows which signals exist in *your* logs, so a missing signal shows
as "not in your logs" instead of a quiet zero.

**Problem 13: Seeing yourself should not mean sending yourself somewhere.**
*Today:* many analytics approaches assume prompts and code go to a server.
*Why it hurts:* that is a non-starter for a lot of engineers and
organisations. *What the dashboard does:* nothing leaves the machine, it calls
no API, and it works offline.

### 2.5 What it costs to leave this unsolved

For the **developer**: slower skill growth, unchecked AI-written code reaching
review, wasted time in failure loops, wasted spend they cannot explain, and
unused features that would have saved effort.

For the **organisation**: very uneven practice between people with the same
licence, no way to know whether the tool is being used deeply or barely
touched, and no safe, consent-based way to learn from the people who use it
best. The tool is paid for either way. Whether it pays off depends on habits
that nobody can currently see.

### 2.6 What this is, and what it is not

The dashboard **complements** the built-in commands. It does not replace them:

- Keep using `/context` to manage the live context window, and `/usage` for the
  authoritative view of plan limits.
- Keep using the Console and your cloud billing console for anything that
  affects an invoice. The dashboard's cost is an estimate.
- Organisations that need centralised monitoring still have OpenTelemetry and
  admin analytics for that job.

The dashboard fills the gap **between** them: a personal, local, historical
view that joins usage, habits, quality and cost in one place.

---

## 3. The idea

> **Every time you use Claude Code, it saves a detailed diary of the session
> on your machine. This dashboard reads that diary, does the maths locally, and
> shows you a mirror.**

**From many commands to one dashboard**

```
  Today                                         With the dashboard
  ─────                                         ──────────────────
  /usage    cost, current session ┐
  /context  window, right now     │
  /status   version, account      │
  /insights narrative (uses tokens)├──────────▶  ONE page, one date range
  /resume   list of sessions      │              · usage + cost + history
  /memory /hooks /agents /mcp     │              · habits + adoption level
  status line (live, not kept)    │              · flow + quality + value
  OpenTelemetry (needs a backend) │              · ranked advice
  admin console (admins only)     ┘              · all on your own machine
```

The mirror is meant to be:

- **Private**: it lives and stays on the developer's machine.
- **Personal**: you are compared with your own last month, not with a
  colleague.
- **Practical**: each page ends in something you can act on, not just a
  chart.
- **Plain**: no jargon; where a number is a rough guess, it says so.

This is a deliberate choice about *who the tool is for*. Self-awareness tools
that the person controls are adopted willingly. Tools that feel like
surveillance are gamed or ignored. Starting with the individual gives honest
data and real behaviour change, and that is a much better foundation for any
later team-level view (see [section 11](#11-roadmap-how-this-can-grow)).

---

## 4. How it works (and why it is safe)

```
~/.claude/projects/**/*.jsonl      logs Claude Code already writes
            │
            ▼
   Scanner   finds every log; re-reads only the files that changed
            │
            ▼
   Parser    each log line becomes numbers (tokens, tool calls, timings,
             edits, test runs, timestamps), de-duplicated by message ID
            │
            ▼
   Engines   Flow · Quality · Value · Adoption · Advisor · Data Health
            │
            ▼
   /api/stats   one JSON payload, rebuilt on demand
            │
            ▼
   Browser page on localhost:8788   charts bundled locally, refreshes every 15 s
```

- **Source:** only the session log files Claude Code writes itself.
- **Footprint:** one Python file; Python 3.8+ standard library; the chart
  library is bundled, so the page makes no external requests.
- **Speed:** a file cache means only changed logs are re-read, so months of
  history stay fast.
- **Cross-platform:** Windows, macOS and Linux (auto-start instructions for
  each are in [SETUP.md](../SETUP.md)).
- **Cost:** zero tokens. The only two features that ever call Claude are
  buttons the user presses on purpose ("AI summary" and "Discuss with
  Claude"), and they are clearly marked.

Deeper technical detail: [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

---

## 5. Design principles

1. **Local first.** Nothing leaves the machine. This removes the biggest
   barrier to adoption.
2. **Compare to yourself.** Scores track your trend. They are not league tables.
3. **Say how sure you are.** Every metric is labelled by how it was obtained:

   | Badge | Meaning |
   |---|---|
   | **Observed** | read straight from a field in the log |
   | **Derived** | computed from observed fields with a stated formula |
   | **Proxy** | an indirect signal that may not mean what it suggests |
   | **Estimated** | depends on list prices or a model |

4. **No assumptions presented as facts.** If the logs do not contain a signal,
   the page says "not in your logs" rather than showing a misleading zero.
5. **Explain every number.** Each metric has a formula and a stated
   limitation in the built-in catalog.
6. **Actionable over decorative.** Pages end in "next moves" with the evidence
   quoted, not a wall of charts.
7. **No gaming-friendly vanity metrics.** Value has no score on purpose, since
   a dollar figure has no universal "good". Lines of code and token counts are
   shown as context, never as a goal.

---

## 6. A tour of every page

The dashboard has eleven pages in the left navigation. The first six cover
usage and growth, and the last five (grouped as **Engineering insight**) cover
how the work actually flows and gets checked.

At a glance:

| # | Page | Core question it answers | Follows date range? |
|---|---|---|---|
| 1 | Overview | How much am I using Claude and what does it produce? | Yes |
| 2 | Time & Focus | When and how long do I work? Is it sustainable? | Yes |
| 3 | Tools & Models | Which models, tools and features do I rely on? | Yes |
| 4 | Projects | Where is the effort going? | Yes |
| 5 | Weekly Report | What did I do this week, and how well did I work? | By week |
| 6 | My Adoption | How deeply do I use Claude Code, and am I improving? | No (28-day window) |
| 7 | Flow | Does my work move smoothly, or do I get stuck? | Yes |
| 8 | Quality | Is the work being checked? | Yes |
| 9 | Value | What does it cost, and what do I get for it? | Yes |
| 10 | Advisor | What are the best things to change? | No (recent history) |
| 11 | Data Health | Can I trust what is shown? | No |

A date selector (7 / 30 / 90 days, all time, or a custom range) applies to the
pages marked "Yes". Everything, including the project detail view, respects it.

---

### 6.1 Overview

![Overview](screenshots/overview.png)

**Purpose:** the headline picture of usage and output.

**What is on it**
- **KPI cards**, each with a change against the previous equal-length period:
  input and output tokens, cache reads and hit rate, estimated API cost, lines
  of code Claude wrote, prompts, sessions, friction, your own typed tokens, and
  your **leverage ratio**.
- **Momentum bar:** current and best day streak, active days, and optional
  personal goals (daily lines, daily or weekly token budget).
- **Charts:** daily tokens; **You vs Claude** (how much you typed against how
  much Claude produced); lines of code per day; **exploration vs building**.
- **Exports:** copy a text summary, download a CSV of daily numbers, or the
  full JSON.
- **Plan usage limits:** your real 5-hour and 7-day plan usage, the same
  percentages Claude Code reports, shown beside your history instead of only in
  the moment. They are captured through Claude Code's status-line hook (set up
  once with `python ai_fluency_insights.py --setup-notifications`) and are
  available on Pro and Max plans. The card flags a stale reading, and falls back
  to a clearly labelled estimate when none exists. On Windows, optional toast
  notifications can warn you as you cross usage milestones.

**How the main numbers are calculated**
- *Tokens* are summed per Claude response and **de-duplicated** by message ID
  and request ID, because Claude Code writes the same response more than once
  to its logs. Without this, totals are inflated (see
  [TOKEN_COUNTS.md](TOKEN_COUNTS.md)).
- *Cache hit rate* = cache reads ÷ (cache reads + fresh input).
- *Estimated cost* = tokens × public API list prices from an editable price
  table. On a subscription plan the real marginal cost is $0, so this is a
  "what it would cost" figure.
- *Lines of code* are counted from the content Claude actually wrote or edited
  (additions only, not a git diff).
- *Leverage ratio* = Claude output tokens ÷ the tokens you typed (estimated at
  about 4 characters per token). It shows how much work you are getting for
  each word you write.
- *Exploration vs building* = share of tool calls that read and search
  against those that edit and run.
- *Friction* = interruptions plus tool errors.

**How it helps:** gives a developer, in ten seconds, a grounded answer to "how
am I using this?", with a trend instead of a single number.

---

### 6.2 Time & Focus

**Purpose:** show working rhythm and protect against burnout.

**What is on it:** active time, average per active day, longest focus block,
quiet-hours share (23:00-06:00) and weekend share, an active-minutes chart that
flags late-night-heavy days, a **weekday × hour heatmap**, **task mix per day**
(feature, bug fix, refactor, docs, explore) and **friction per day**.

**How it is calculated**
- *Active time* groups activity into 10-minute buckets; a lone message counts
  as 10 minutes.
- *Task mix* classifies each session from the AI-written session title by
  keyword (bug-fix wording wins over refactor, then docs, feature, explore),
  with tool mix as a fallback.

**How it helps:** reveals patterns people rarely notice: always working late,
losing weekends, or spending most sessions on exploration rather than
building. These are the early signals of an unsustainable pace.

---

### 6.3 Tools & Models

**Purpose:** show which parts of Claude Code the developer actually relies on.

**What is on it:** model usage and a model breakdown table (tokens and
estimated cost per model), autonomy mode (how much you let Claude act on its
own), delegation, tool usage counts, and slash-command usage.

**How it helps:** shows whether an expensive model is being used for light
tasks, whether the developer ever delegates to subagents, and which
capabilities have never been touched.

---

### 6.4 Projects

**Purpose:** answer "where did my effort go?".

**What is on it:** a table of projects with tokens, cost, sessions and lines;
git branches; the most-edited files; and the longest prompts. Clicking a project
opens a **per-project deep dive** with its own timeline, sessions and files.

**How it helps:** makes it easy to report time per project and to spot a file
that keeps being rewritten.

---

### 6.5 Weekly Report

![Weekly report](screenshots/weekly-report.png)

**Purpose:** replace the Monday scramble to remember last week.

**What is on it**
- **What you worked on:** session titles grouped by project. Claude Code
  already stores an AI-written title for every session, so this is free.
- **KPIs with week-over-week change** and an exploration-vs-building gauge.
- **AI Fluency report:** four 0-100 scores with a radar chart against last
  week, and a concrete tip for each (below).
- **Reflection:** one data-backed question about how you worked this week,
  with an optional *Discuss with Claude* button.
- **Copy as Markdown:** a formatted weekly update ready to paste into chat or
  email.
- **AI summary (optional):** a polished first-person paragraph, written by
  Claude only when the user presses the button.

**The AI Fluency dimensions** adapt Anthropic's 4D framework for coding:

| Dimension | What it asks | Basis |
|---|---|---|
| **Delegation** | Am I handing Claude real building work? | share of sessions that write code, and leverage |
| **Description** | Do my prompts land the first time? | one-shot rate and prompt length sweet spot |
| **Discernment** | Do I review and push back? | corrections and interruptions, and error control |
| **Diligence** | Do I verify the result? | share of code sessions that also ran a command |

These are heuristic proxies, so the week-to-week trend matters more than the
absolute figure.

**How it helps:** a standup update that takes one click and is accurate, plus
a weekly nudge on one habit worth changing.

---

### 6.6 My Adoption

![My Adoption](screenshots/my-adoption.png)

**Purpose:** a personal scorecard for *how deeply and how well* the developer
uses Claude Code, and whether they are improving.

**What is on it**
- **Adoption Score (0-100) and a level:** Explorer, Practitioner, Power user,
  Expert, Pro, Pro Max.
- **Four pillars** with trend against the previous four weeks: **Habit**,
  **Range**, **Craft**, **Impact**.
- **What changed lately:** plain-language notes such as a pillar that moved
  5+ points, a level change, or a feature used for the first time.
- **Adoption journey:** Started → Recurring → Integrated → Verified outcomes.
- **Feature checklist:** ten capabilities (project memory, run and verify, plan
  mode, subagents, skills, hooks, task lists, connected tools, slash commands,
  web research), showing used and unused.
- **Habit calendar, 12-week trend chart,** and **healthy-habits** panel.
- **Your next 3 moves:** the three most useful, rule-based suggestions, with no
  AI and no tokens.

**The pillars**

| Pillar | Question | Measured by |
|---|---|---|
| **Habit** | Do you show up regularly? | active days and consistency across weeks |
| **Range** | Do you use what Claude Code offers? | how many of the ten features you use, and how often sessions are substantial |
| **Craft** | Do you work with Claude well? | fluency, plus the Flow and Quality scores |
| **Impact** | What does Claude get done for you? | lines per active day, and days where Claude wrote code |

**The levels have gates at the top** so the high levels mean something: *Pro*
needs every pillar at 50 or above, and *Pro Max* needs every pillar at 70 or
above **and** four consecutive weeks of a score at 85 or higher. If a score
reaches a level but a gate fails, the page names the exact pillar to lift
("Lift Range (now 40) to 50 to unlock Pro").

**How it helps:** turns "am I any good at this?" into something visible and
motivating, and removes guesswork about what to try next.

---

### 6.7 Flow

![Flow](screenshots/flow.png)

**Purpose:** show whether work moves smoothly or stalls.

**What is on it**
- **Turn time:** how long Claude takes from your prompt to its reply (median
  and 90th percentile).
- **Clean first-pass turns:** turns with no tool error, no interruption and no
  correction.
- **Failure recovery:** when a tool fails, how often and how quickly it is
  fixed, and how often you get *stuck* (three or more failures in a row).
- **Where tool time goes:** time spent per tool or per kind of command
  (`git push`, `pytest`, `python`...).
- **Time to first edit, session length, thinking time and effort.**

**Flow score** = 100 × (0.5 × clean-turn rate + 0.3 × recovery rate +
0.2 × (1 − stuck share)). It needs at least five turns.

**How it helps:** shows whether prompts are landing first time, whether long
waits come from one slow command, and whether the developer is stuck in failure
loops that cost time and money.

---

### 6.8 Quality

![Quality](screenshots/quality.png)

**Purpose:** show whether Claude's work was checked.

**What is on it**
- **Verified after last edit:** share of edit sessions where a test, build or
  lint command ran *after* Claude's last edit.
- **First-run pass rate:** whether the first test or build run succeeded, and
  how often a failure was later fixed.
- **Tests alongside code:** share of edit sessions that also touched a test
  file.
- **Re-edit share and rework hotspots:** files edited again and again.
- **Errors and interruptions** per 100 edits / prompts.
- **Safety signals:** counts of risky commands (force-push, hard reset,
  recursive delete, pipe-to-shell, skipped git hooks, `chmod 777`, SQL drops)
  and reads or edits of secret-like files. Only the *category* is stored,
  never the command itself.
- **Permission decisions** when the logs include them.

**Quality score** = a weighted mean of verified (40 %), tests alongside
(20 %), low rework (20 %) and first-run pass (20 %, only if checks ran). It
needs at least three edit sessions.

**Command detection is invocation-aware.** A word such as `pytest` appearing
inside a commit message or a heredoc is not counted as a test run. Commands
are split into real segments and classified by what they actually invoke.

**How it helps:** it nudges the habit that matters most with AI-written code,
which is to check it, and points straight at the files that keep needing
rework.

---

### 6.9 Value

![Value](screenshots/value.png)

**Purpose:** put spend in context.

**What is on it:** estimated cost, **cost per 100 lines**, **cost per verified
session**, **cache savings** (what prompt caching saved against sending the
same tokens fresh), cold-start tax and cache rebuilds, **cost spikes** (days at
2× or more of a typical day, naming the session responsible), and **model mix**
by cost, including top-tier model spend on sessions where nothing was edited.

**No score, by design.** A dollar figure has no universal "good", so each number
is shown beside its change against the developer's own previous period.

**How it helps:** separates healthy spend from wasteful spend, and explains
spikes instead of just reporting them.

---

### 6.10 Advisor

![Advisor](screenshots/advisor.png)

**Purpose:** convert all the data into a short list of what to change.

**What it does:** runs a set of transparent rules over the last 28 days and
shows the most valuable moves first, **quoting the evidence** behind each. For
example, if fewer than 60 % of edit sessions were verified it suggests making
Claude run the tests, and shows the exact figure. It also lists **what is going
well**, so feedback is balanced.

**Experiments:** with one click, the developer can start an experiment on a
suggestion. The dashboard stores the current value in the browser and shows how
the metric moves over the following weeks.

**It uses no AI and no tokens.** Every rule is a visible threshold in the code,
documented in [METRICS.md](METRICS.md#16-advisor-tab-v14).

**How it helps:** the page most likely to change behaviour. It converts "here
are 40 numbers" into "do these two things".

---

### 6.11 Data Health

![Data Health](screenshots/data-health.png)

**Purpose:** make the whole dashboard trustworthy.

**What is on it**
- Which signals exist **in this developer's own logs** (tool timing, turn
  timing, permission decisions, thinking time, effort, hooks, subagent
  activity, timestamps, session titles) and which do not.
- Log entry types and counts, Claude Code versions seen, parse failures and
  duplicate lines dropped.
- The **metric catalog:** every metric's confidence badge, formula and known
  limitation, from a single source in the code.
- **Privacy mode:** hides session titles, prompt previews and file names on
  screen, so the dashboard can be screen-shared safely.

**How it helps:** if a number looks wrong or missing, this page says why.
Different Claude Code versions write different fields, so an honest tool has to
say what it can and cannot see.

---

## 7. How the key scores are calculated

Full formulas and every threshold are in [METRICS.md](METRICS.md). The summary:

```
Adoption Score = 0.25 × Habit + 0.25 × Range + 0.30 × Craft + 0.20 × Impact
                 (rolling 28 days ending on your latest active day)

Craft   = 0.6 × fluency mean + 0.2 × Flow score + 0.2 × Quality score
Flow    = 100 × (0.5 × clean turns + 0.3 × recovery + 0.2 × (1 − stuck share))
Quality = 40% verified + 20% tests alongside + 20% low rework + 20% first-run pass
```

| Level | Score | Extra gate |
|---|---|---|
| Explorer | 0-19 | none |
| Practitioner | 20-39 | none |
| Power user | 40-59 | none |
| Expert | 60-74 | none |
| Pro | 75-89 | every pillar ≥ 50 |
| Pro Max | 90-100 | every pillar ≥ 70 and 4 weekly points ≥ 85 |

Things worth stating plainly:

- The weights and targets are hand-chosen and sit in one table at the top of
  the script (`ADOPTION`), so they can be tuned.
- The levels are **motivational labels, not a standard.** The trend matters
  more than the exact number.
- With fewer than 3 active days in the window, the page shows "Building your
  baseline" instead of inventing a score.
- If a signal is missing, its weight is shared among the others, and the page
  says so.

---

## 8. How it helps the individual developer

Section 2 listed what is missing. This section shows what the developer gets
back, first as a quick lookup and then as situations.

### 8.1 Quick lookup

| What the developer wants | Where they get it |
|---|---|
| "How much did I use it, per project and model?" | **Overview**, **Tools & Models**, **Projects** |
| "Am I getting better at this?" | **My Adoption** level, pillar trends, 12-week chart |
| "What should I try next?" | **Advisor** and **Your next 3 moves** |
| "Why did that session cost so much?" | **Value** cost spikes and the session behind them |
| "Am I shipping unchecked code?" | **Quality** verified-after-edit and first-run pass |
| "Why does everything feel slow?" | **Flow** turn time and where tool time goes |
| "What did I do this week?" | **Weekly Report** with one-click Markdown |
| "Am I overworking?" | **Time & Focus** quiet-hours and weekend share |
| "Are my prompts any good?" | **Flow** clean first-pass turns and **Description** fluency |
| "What features am I missing?" | **My Adoption** feature checklist |
| "Am I close to my plan limit?" | **Overview** plan usage card, with optional notifications |
| "Can I trust these numbers?" | **Data Health** and the confidence badges |

### 8.2 What changes for the developer

| Before | After |
|---|---|
| Ten places to look, each a snapshot | One page with history and a date selector |
| "I think I'm using it well" | A score, a level and a trend |
| Cost is a number | Cost per 100 lines, per verified session, with spikes explained |
| Quality is a feeling | Verified-after-edit and first-run pass, measured |
| Unknown features stay unknown | A checklist of what you have and have not tried |
| Advice means reading and guessing | Ranked moves with the evidence quoted |
| Monday standup from memory | One click to a Markdown update |
| Data sent somewhere to be analysed | Everything stays on your laptop |

### 8.3 Situations (illustrative)

These are examples of how the tool is meant to be used. They are not measured
results.

**The newcomer.** A developer in their first weeks opens My Adoption and sees
"Building your baseline", then Explorer. The checklist shows nine unused
features and the coach suggests two of them, so they learn what exists by
using it, not by reading a manual.

**The heavy user who is not getting better.** Someone uses Claude every day and
feels productive, yet sits at Power user. The Advisor shows only 38 % of edit
sessions ended with a test or build run. They start the one-click experiment,
begin adding "run the tests and fix failures" to their prompts, and over the
following weeks watch the Quality page climb. The same developer notices Range is
their weakest pillar because they have never used plan mode or task lists, and
tries both on the next risky change.

**The developer who keeps getting stuck.** Flow shows two failure loops last
week and a 90th-percentile session of more than three hours. The Advisor
suggests starting a fresh session per task. They do, and the stuck count falls.

**The cost-conscious developer.** Value shows a spike day, names the session
behind it, and shows that most top-tier model spend went to sessions where
nothing was edited. They switch to a lighter model for exploration.

**The developer who is working too late.** Time & Focus shows a quarter of
messages after 23:00 and most weekends touched. The coach ranks "protect your
evenings" first. It is a quiet, private nudge, not a report to anyone.

**The developer who needs a status update.** On Friday afternoon they open the
Weekly Report, review what they worked on by project, copy it as Markdown and
paste it into the team channel.

In each case no manager was involved, no data left the laptop, and the change
came from the developer's own evidence.

**Why this changes behaviour:** the feedback is immediate, it is about *your*
habits, it names a specific action, and there is no audience to perform for.

---

## 9. Trust, privacy and honesty

- **Nothing leaves the machine.** No telemetry, no upload, no API calls. The
  page loads no external resources.
- **Read-only.** The tool reads Claude Code's logs. It does not modify them.
- **Minimal storage.** Risky-command detection records the *category* only.
  Experiments are stored in the browser's `localStorage`.
- **Privacy mode** masks titles, prompt previews and file names for screen
  sharing.
- **Individual by design.** Scores are relative to your own history. There is
  no ranking and no comparison with other people.
- **Plain about uncertainty.** Observed, Derived, Proxy and Estimated badges;
  a published formula and limitation for every metric; and a
  [list of known approximations](METRICS.md#18-known-approximations-honest-accounting).
- **Checked against real logs.** Signals were verified against real Claude Code
  logs before being built. One idea was dropped after testing: the
  "file edited outside Claude" attachment also fires for Claude's own shell
  edits, so it could not honestly separate human edits from Claude's.

---

## 10. Limits of the current version

Being direct about what it does not do:

- **Local logs only.** It cannot see pull requests, tickets, builds, deploys or
  outcomes in production. "Verified" means a check *ran*, not that the code is
  correct.
- **One machine at a time.** It reads the logs on the machine it runs on. A
  developer who uses several machines sees each separately.
- **Claude Code only.** Other assistants are not covered yet.
- **Estimates are estimates.** Cost uses public list prices; typed tokens use a
  characters-per-token guess; lines count additions only; shell-made edits are
  invisible to line and edit counts.
- **Scores are heuristics.** Weights and thresholds are judgement calls, and
  they should be tuned once real usage data has been reviewed.
- **Corrections are detected at the start of a prompt only.**
- **It can only see logs that still exist.** Claude Code's documentation says
  session transcripts are kept for 30 days by default (the `cleanupPeriodDays`
  setting). If your logs are cleaned up, "all time" means "all that remains".
  Check or raise the setting if you want longer history.
- **Plan usage needs the status-line hook and a subscription.** The live 5-hour
  and 7-day figures are captured by a hook that runs only while a Claude Code
  session is active, and Claude Code provides them for Pro and Max plans. Toast
  notifications are Windows-only for now.
- **Like `/usage`'s plan view, it reads this machine only.** Work done on other
  devices or in claude.ai is not included.
- **Some signals depend on the Claude Code version.** Permission "reject"
  values, compaction events and subagent log lines have not yet been confirmed
  on a wide range of real logs. Data Health shows what each installation has.

---

## 11. Roadmap: how this can grow

The sequencing below follows one rule: **earn trust at the individual level
before anything is aggregated.**

### Near term: sharpen what exists
| Item | Why |
|---|---|
| **Calibrate thresholds on real data.** Review scores from several developers' real logs and adjust targets and level boundaries. | The weights are hand-picked and should be validated, not trusted. |
| **"Copy my adoption report" button.** A Markdown summary of level, pillars, what changed and next moves. | Lets a developer share their own progress on their own terms. |
| **Confirm unverified log signals** on more Claude Code versions. | Fewer "not in your logs" gaps. |
| **Keep a personal history that outlives log cleanup.** Save small daily roll-ups (numbers only) next to the dashboard. | Trends survive even when old transcripts are deleted. |
| **Saved experiments over longer periods,** with a before/after summary. | Closes the loop on behaviour change. |

### Medium term: broaden the lens
| Item | Why |
|---|---|
| **Support for other assistants.** A build plan for a GitHub Copilot version already exists ([COPILOT_DASHBOARD_BUILD_PLAN.md](COPILOT_DASHBOARD_BUILD_PLAN.md)). | One view of all AI-assisted work. |
| **Merge several machines** (for example by exporting and importing a personal snapshot). | Developers rarely work on one device. |
| **Personal goals for the new pillars** (for example "verified ≥ 70 %"), with progress bars. | Connects insight to intent. |
| **Richer prompt-craft feedback** from local analysis of prompt structure. | Description is the lever with the most upside. |

### Longer term: from personal to team (strictly opt-in)
| Item | Why | Safeguards |
|---|---|---|
| **Voluntary, anonymised team summary.** Each developer may choose to export a small summary file (scores and trends only, no prompts, titles or code). | Gives leaders a picture of adoption maturity without surveillance. | Opt-in per person; the file is shown before it is shared; no ranking by name. |
| **Team-calibrated targets.** Set level thresholds from the team's own distribution. | Fairer than fixed numbers. | Aggregates only. |
| **Optional import from OpenTelemetry** for organisations that already collect it. | Lets the same pages work from an existing pipeline. | Opt-in; no new data collection. |
| **Optional links to delivery data** (pull requests, CI results) to test whether better habits go with better outcomes. | Turns "Proxy" signals into evidence. | Explicitly outside the current scope; adds integration and consent work. |
| **Org-wide learning loop:** share the habits of people whose Quality and Flow improved fastest. | Spreads practice, not just measurement. | Needs voluntary participation. |

### What we would deliberately not build
- Leaderboards or per-person rankings.
- Reading or uploading prompt and code content.
- Treating tokens or lines of code as a productivity target.

---

## 12. Appendix

### 12.1 Glossary

| Term | Meaning |
|---|---|
| **Session** | One continuous Claude Code conversation, stored as one log file. |
| **Turn** | From a prompt you type to Claude's final reply. |
| **Leverage ratio** | Claude output tokens ÷ tokens you typed. |
| **Cache hit rate** | Share of input served from prompt cache, which is cheaper and faster. |
| **Cold-start tax** | Cost of loading context at the start of short sessions. |
| **Clean first-pass turn** | A turn with no error, interruption or correction. |
| **Verified** | A test, build or lint command ran after the last edit. |
| **Rework hotspot** | A file edited five or more times, repeatedly. |
| **Subagent** | A helper Claude hands part of a task to. |
| **Adoption level** | Explorer → Practitioner → Power user → Expert → Pro → Pro Max. |
| **Gate** | An extra condition for the top levels so they cannot be reached by one strong pillar alone. |

### 12.2 Where the evidence comes from

| Signal | Source in the log |
|---|---|
| Tokens, model, cache | assistant message `usage` and `model` |
| Tool calls and timings | `tool_use` / `tool_result` pairs matched by ID, with timestamps |
| Turn boundaries | user prompt and assistant `stop_reason` |
| Failed commands | tool result text and error flag |
| Permission decisions | `permissionDecision` on tool results, when present |
| Thinking time and effort | `thinkingDurationMs` and `effort` on responses |
| Hooks | `stop_hook_summary` entries |
| Subagents | `Agent`/`Task` calls and sidechain log lines |
| Session titles | `ai-title` entries |

### 12.3 Further reading

- [README](../README.md): overview, quick start, screenshots
- [SETUP.md](../SETUP.md): install, auto-start and troubleshooting
- [DASHBOARD_GUIDE.md](DASHBOARD_GUIDE.md): every section in plain language
- [METRICS.md](METRICS.md): the exact formula for every number
- [HOW_IT_WORKS.md](HOW_IT_WORKS.md): architecture
- [TOKEN_COUNTS.md](TOKEN_COUNTS.md): why raw log counts are inflated and how we de-duplicate
