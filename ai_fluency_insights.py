#!/usr/bin/env python3
"""
AI Fluency Insights — a fully-local, zero-token-cost dashboard for your Claude Code usage.

It reads the JSONL session logs Claude Code already writes to ~/.claude/projects/
(no API calls, no tokens consumed) and serves a live auto-refreshing dashboard.

Usage:
    python ai_fluency_insights.py            # serves on http://localhost:8788
    python ai_fluency_insights.py --port 9000
    python ai_fluency_insights.py --dir "C:/Users/you/.claude/projects"

Requires: Python 3.8+ (standard library only).
"""

import argparse
import http.client
import json
import math
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from collections import defaultdict, Counter
from datetime import datetime, timezone, date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import copilot_source

# ---------------------------------------------------------------------------
# Pricing (USD per 1M tokens). Edit freely — these are estimates for the
# "what would this cost on the API" card. Cache write ≈ 1.25x input,
# cache read ≈ 0.1x input.
# ---------------------------------------------------------------------------
PRICING = [
    ("opus",   {"in": 15.0, "out": 75.0}),
    ("sonnet", {"in": 2.0,  "out": 10.0}),
    ("haiku",  {"in": 1.0,  "out": 5.0}),
    ("fable",  {"in": 15.0, "out": 75.0}),
]
DEFAULT_PRICE = {"in": 3.0, "out": 15.0}

# ---------------------------------------------------------------------------
# Optional personal goals / budgets. Set a value to show a progress bar on the
# dashboard; leave 0 to hide that bar. These are targets for YOU — they have
# nothing to do with Anthropic's opaque plan limits.
# ---------------------------------------------------------------------------
GOALS = {
    "daily_loc": 500,        # target lines of code / day
    "daily_tokens": 0,       # output-token budget / day (e.g. 1_000_000)
    "weekly_tokens": 0,      # output-token budget / week (e.g. 5_000_000)
}

# ---------------------------------------------------------------------------
# "My Adoption" tab: how the 0-100 Adoption Score is built. Everything is
# measured over the last `window_days` calendar days (ending on your most
# recent active day) and compared with the window before it. Tweak the
# targets to match your own working rhythm; formulas are in docs/METRICS.md.
# ---------------------------------------------------------------------------
ADOPTION = {
    "window_days": 28,
    "min_active_days": 3,       # fewer active days than this -> "building your baseline"
    "target_days": 12,          # active days per window for full Habit marks (3 a week)
    "features_full": 6,         # distinct features used for full Range marks
    "substantial_actions": 10,  # tool actions that make a session "substantial"
    "loc_target": 300,          # lines Claude wrote per active day for full Impact marks
    "weights": {"habit": 0.25, "range": 0.25, "craft": 0.30, "impact": 0.20},
}
ADOPTION_LEVELS = [
    (0, "Explorer", "You're getting to know Claude Code."),
    (20, "Practitioner", "Claude Code is part of your regular workflow."),
    (40, "Power user", "You hand over real work and check the results."),
    (60, "Expert", "You use the range of Claude Code and it mostly works first time."),
    (75, "Pro", "You work with Claude Code like a skilled teammate: planned, verified, steady."),
    (90, "Pro Max", "Top of the ladder: strong on every pillar, week after week."),
]
# The top two rungs need more than a score: no weak pillar, and (Pro Max) a sustained run.
LEVEL_GATES = {"Pro": {"min_pillar": 50}, "Pro Max": {"min_pillar": 70, "sustain_weeks": 4, "sustain_score": 85}}

# ---------------------------------------------------------------------------
# Live token-usage pop-up notifications for whatever Claude Code session is
# currently running. Independent signals, each fired at most once per
# threshold as it's crossed:
#   - plan_pct:     ALL sessions' tokens combined, as a % of window_ceiling,
#                   within a rolling ~5h window that resets like the Claude
#                   app's "Current session" plan-usage panel. window_ceiling
#                   is a local estimate (Anthropic's real ceiling isn't
#                   queryable) — tune it by eyeballing the app's real %.
#   - context_pct:  latest turn's (input+cache) as a % of context_window
#   - session_step: cumulative session tokens, every flat session_step tokens
#   - task_step:    cumulative tokens since the last user message, every
#                   flat task_step tokens (resets each new user message)
# ---------------------------------------------------------------------------
NOTIFY = {
    "enabled": True,
    "context_window": 0,       # 0 = disabled; was firing per-turn context-fill pops
    # weighted (input-token-equivalent) tokens for one 5h plan window — see
    # weighted_tokens(). Calibrated 2026-09-07 against the real Pro app panel
    # (35% used, resets in 3h16m -> ~104min elapsed): this session's actual
    # weighted usage in that span was ~9.16M, so ceiling = 9.16M / 0.35.
    # One data point on one (cache-heavy coding) session — re-tune if it
    # drifts from the real panel.
    "window_ceiling": 26_000_000,
    "session_step": 0,         # 0 = disabled; redundant with plan_pct's 100% mark
    "task_step": 0,            # 0 = disabled; was firing alongside session_step
    "poll_seconds": 5,
    "active_window_min": 15,   # only watch sessions written to in the last N min
}
MILESTONES = (25, 50, 75, 85, 100)
# Per-kind override for the official rate-limit notifications (see
# NotificationWatcher._poll_official_rate_limits) -- the weekly window is
# watched at a coarser cadence than the 5h "current session" one, which
# uses MILESTONES above.
OFFICIAL_MILESTONES = {
    "five_hour": MILESTONES,
    "seven_day": (20, 40, 60, 80, 100),
}


def should_run_notifications(notify=None):
    notify = NOTIFY if notify is None else notify
    return bool(notify.get("enabled", True))


CODE_TOOLS_WRITE = {"Write", "Create"}          # tools whose 'content' is new code
CODE_TOOLS_EDIT = {"Edit", "StrEditReplace"}    # tools whose 'new_string' is new code

# tool classification for the exploration-vs-building split
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch",
              "NotebookRead", "TodoRead", "ToolSearch"}
WRITE_TOOLS = CODE_TOOLS_WRITE | CODE_TOOLS_EDIT | {"MultiEdit", "NotebookEdit"}
CMD_TOOLS = {"Bash", "PowerShell"}   # command runs — the verification proxy for diligence
# GitHub Copilot's tool names count the same way (its logs are translated into this shape)
READ_TOOLS |= copilot_source.COPILOT_READ_TOOLS
WRITE_TOOLS |= copilot_source.COPILOT_WRITE_TOOLS
CMD_TOOLS |= copilot_source.COPILOT_CMD_TOOLS

# Claude Code features tracked on the "My Adoption" tab. tool name -> feature id;
# MCP tools are matched by their "mcp_" prefix (Claude: mcp__, Copilot: mcp_), slash commands / plan-mode
# snapshots / CLAUDE.md are detected separately (see parse_entry, build_adoption).
TOOL_FEATURES = {"EnterPlanMode": "plan", "ExitPlanMode": "plan",
                 "Agent": "subagents", "Task": "subagents",
                 "WebSearch": "web", "WebFetch": "web", "Skill": "skills",
                 "Bash": "verify", "PowerShell": "verify",
                 "TodoWrite": "tasks", "TaskCreate": "tasks", "TaskUpdate": "tasks"}
TOOL_FEATURES.update(copilot_source.COPILOT_TOOL_FEATURES)
# ---- log-derived signals for the Flow / Quality / Value tabs -----------------
EXIT_RE = re.compile(r"Exit code (-?\d+)")
TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|__tests__|specs?)/|(^|/)test_[^/]*$|_test\.[A-Za-z]+$|\.(test|spec)\.[A-Za-z]+$",
    re.IGNORECASE)
SECRET_PATH_RE = re.compile(
    r"(^|/)\.env(\.[\w-]+)?$|\.pem$|(^|/)id_(rsa|ed25519)$|(^|/)credentials(\.\w+)?$"
    r"|(^|/)secrets?\.(json|ya?ml|toml)$", re.IGNORECASE)
# What a shell command was for. Patterns are matched against each *invoked*
# command (see _cmd_segments), never against heredoc bodies or quoted text, so a
# script that merely mentions "pytest" or "--no-verify" does not count.
CMD_KIND_RES = [
    ("test", re.compile(
        r"(pytest|py\.test|unittest|nosetests|tox|nox|jest|vitest|mocha|karma|rspec|phpunit|ctest)\b"
        r"|go\s+test\b|cargo\s+test\b|(npm|yarn|pnpm|bun)\s+(run\s+)?test\b"
        r"|(mvnw?|gradlew?)\b.*\btest\b|dotnet\s+test\b|make\s+test\b"
        r"|(python[\d.]*\s+)?(\S*/)?manage\.py\s+test\b|playwright\s+test\b|cypress\s+run\b",
        re.IGNORECASE)),
    ("lint", re.compile(
        r"(eslint|ruff|flake8|pylint|mypy|pyright|golangci-lint|rubocop|stylelint|shellcheck)\b"
        r"|tsc\b.*--noEmit|prettier\b.*--check|black\b.*--check"
        r"|(npm|yarn|pnpm)\s+(run\s+)?(lint|typecheck)\b|cargo\s+clippy\b|make\s+lint\b",
        re.IGNORECASE)),
    ("build", re.compile(
        r"(tsc|webpack|rollup|esbuild|cmake)\b|vite\s+build\b|gradlew?\s+(build|assemble)\b"
        r"|mvnw?\b.*\b(package|compile|install)\b|cargo\s+build\b|go\s+build\b|dotnet\s+build\b"
        r"|make\b|(build|compileall|py_compile)\b|(npm|yarn|pnpm)\s+(run\s+)?build\b"
        r"|node\s+--check\b", re.IGNORECASE)),
]
# commands worth surfacing as a safety signal (category only is stored, never the command)
RISKY_CMD_RES = [
    ("force_push", re.compile(r"git\s+push\b.*(--force\b|--force-with-lease|\s-f\b)")),
    ("hard_reset", re.compile(r"git\s+reset\s+--hard\b")),
    ("git_clean", re.compile(r"git\s+clean\s+-[a-zA-Z]*f")),
    ("recursive_delete", re.compile(r"rm\s+-[a-zA-Z]*[rR]")),
    ("pipe_to_shell", re.compile(r"(curl|wget)\b.*\|\s*(sudo\s+)?(ba|z)?sh\b")),
    ("skip_hooks", re.compile(r"git\s+(commit|push)\b.*--no-verify\b")),
    ("wide_permissions", re.compile(r"chmod\s+(-R\s+)?777\b")),
    ("sql_drop", re.compile(r"(psql|mysql|sqlite3|sqlcmd)\b.*\bdrop\s+(table|database)\b", re.IGNORECASE)),
]
SUBCMD_TOOLS = {"git", "npm", "yarn", "pnpm", "docker", "cargo", "go", "pip", "pip3", "kubectl",
                "gh", "make", "uv", "poetry", "bun"}
_LEAD_WORDS = ("sudo", "time", "env", "nohup", "command", "exec")
_RUNNER_RE = re.compile(
    r"^(?:npx\s+|bunx\s+|uvx\s+|uv\s+run\s+|poetry\s+run\s+|pipenv\s+run\s+|bundle\s+exec\s+"
    r"|python[\d.]*\s+-m\s+|\./|(?:\S*/)?node_modules/\.bin/)")


def _cmd_segments(cmd):
    """The commands a shell string actually invokes, normalised: the text after a
    heredoc marker is dropped, then it is split on && || ; and newlines, and
    leading env assignments, sudo/time/nohup and runners (npx, uv run, python -m) are stripped."""
    if not isinstance(cmd, str):
        return []
    m = re.search(r"<<-?\s*['\"]?\w+['\"]?", cmd)
    if m:
        nl = cmd.find("\n", m.end())
        cmd = cmd[:nl] if nl != -1 else cmd
    out = []
    for seg in re.split(r"&&|\|\||;|\n", cmd):
        seg = seg.strip().lstrip("({").strip()
        while True:
            toks = seg.split(None, 1)
            if toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]) or toks[0] in _LEAD_WORDS):
                seg = toks[1] if len(toks) > 1 else ""
                continue
            m2 = _RUNNER_RE.match(seg)
            if m2 and m2.end() < len(seg):
                seg = seg[m2.end():]
                continue
            break
        if seg:
            out.append(seg)
    return out


def classify_cmd(cmd) -> str:
    """'test' | 'lint' | 'build' | 'other' for the commands a shell string invokes."""
    segs = _cmd_segments(cmd)
    for kind, rx in CMD_KIND_RES:
        if any(rx.match(sg) for sg in segs):
            return kind
    return "other"


def risky_categories(cmd):
    segs = _cmd_segments(cmd)
    return [cat for cat, rx in RISKY_CMD_RES if any(rx.match(sg) for sg in segs)]


def cmd_family(cmd) -> str:
    """Short label for what a shell command ran (e.g. 'git push', 'pytest', 'python')."""
    segs = [sg for sg in _cmd_segments(cmd) if not re.match(r"^cd(\s|$)", sg)]
    if not segs:
        return "shell"
    toks = segs[0].split()
    first = os.path.basename(toks[0].strip("'\"")) or "shell"
    if re.match(r"^python[\d.]*$", first):
        return "python"
    if first in ("npm", "yarn", "pnpm", "bun") and len(toks) > 2 and toks[1] == "run" \
            and re.match(r"^[\w:-]+$", toks[2]):
        return first + " run " + toks[2]
    if first in SUBCMD_TOOLS and len(toks) > 1 and re.match(r"^[a-z][\w-]*$", toks[1]):
        return first + " " + toks[1]
    return first[:24]


def _entry_time(entry):
    try:
        ts = datetime.fromisoformat((entry.get("timestamp") or "").replace("Z", "+00:00")).astimezone()
        return ts, ts.strftime("%Y-%m-%d")
    except Exception:
        return None, ""


def _tool_result_text(block) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c if isinstance(b, dict))
    return ""


def _parse_aux_entry(entry, etype, st, min_day, max_day):
    """System and attachment lines: hooks and (for the health panel) what the log contains."""
    ts, day = _entry_time(entry)
    if not day:
        return
    if (min_day is not None and day < min_day) or (max_day is not None and day > max_day):
        return
    if etype == "system" and entry.get("subtype") == "stop_hook_summary":
        st.hc["stop_hook_summary"] += 1
        st.feature_days["hooks"].add(day)
    elif etype == "attachment":
        kind = (entry.get("attachment") or {}).get("type")
        if kind:
            st.attachment_types[kind] += 1


# id, label, short description, how-to-start tip, usefulness weight for the coach
ADOPTION_FEATURES = [
    ("memory", "Project memory", "A CLAUDE.md that teaches Claude your project",
     "Run /init in a project so Claude learns your conventions once, instead of every session.", 58),
    ("verify", "Run & verify", "Claude runs commands, tests and builds",
     "Ask Claude to run your tests or build after it edits, so it can check its own work.", 60),
    ("plan", "Plan mode", "Agree on an approach before any edits",
     "Press Shift+Tab to switch to plan mode and let Claude propose an approach before it touches files.", 55),
    ("subagents", "Subagents", "Helpers that research or review in parallel",
     "Say \"use a subagent to research this\" to keep your main conversation clean.", 50),
    ("skills", "Skills", "Reusable workflows Claude can pick up",
     "Package something you repeat weekly as a skill so Claude does it the same way every time.", 45),
    ("hooks", "Hooks", "Automatic checks that run on Claude Code events",
     "Add a hook in your settings to run your formatter or tests automatically after Claude edits.", 48),
    ("tasks", "Task lists", "Claude tracks multi-step work in a checklist",
     "For a big job, ask Claude to write a task list first and tick items off as it goes.", 38),
    ("mcp", "Connected tools", "MCP servers for GitHub, docs, databases...",
     "Connect a tool you already use with `claude mcp add` so Claude can work with it directly.", 42),
    ("slash", "Slash commands", "Shortcuts like /compact, /clear, /init",
     "Type / to see the shortcuts. /compact and /clear keep long sessions fast and focused.", 40),
    ("web", "Web research", "Looking up docs and errors online",
     "Ask Claude to look up the docs or an error message instead of pasting it in yourself.", 35),
]

# The same tuples for GitHub Copilot: its own feature list, so Claude-only features (skills, hooks) are
# not shown. Same ids as the Claude list where the idea is shared, because parse_entry records both
# tools' usage under them (TOOL_FEATURES, the chat mode, prompt files and slash commands).
COPILOT_ADOPTION_FEATURES = [
    ("memory", "Custom instructions (.github/copilot-instructions.md or AGENTS.md)",
     "An instructions file that teaches Copilot your project",
     "Add a .github/copilot-instructions.md to a project so Copilot learns your conventions once, instead of every chat.", 58),
    ("verify", "Run & verify", "Copilot runs commands, tests and builds",
     "Ask Copilot to run your tests or build after it edits, so it can check its own work.", 60),
    ("agent", "Agent mode", "Copilot plans, edits files and runs tools on its own",
     "Switch the chat to Agent mode and hand Copilot a whole task, not just a question.", 55),
    ("plan", "Plan mode", "Agree on an approach before any edits",
     "Pick the Plan agent in the chat and let Copilot propose an approach before it touches files.", 50),
    ("subagents", "Subagents", "Helpers that research or review in parallel",
     "Say \"use a subagent to research this\" to keep your main chat clean.", 45),
    ("tasks", "Todo lists", "Copilot tracks multi-step work in a checklist",
     "For a big job, ask Copilot to write a todo list first and tick items off as it goes.", 38),
    ("mcp", "Connected tools (MCP)", "MCP servers for GitHub, docs, databases...",
     "Connect an MCP server for a tool you already use so Copilot can work with it directly.", 42),
    ("slash", "Prompt files & slash commands", "Shortcuts like /explain and /fix, and saved prompt files",
     "Type / in the chat to see the shortcuts, and save a prompt you repeat as a prompt file.", 40),
    ("web", "Web fetch", "Looking up docs and errors online",
     "Ask Copilot to look up the docs or an error message instead of pasting it in yourself.", 35),
]

# Quiet hours for the wellness panel (24h clock, wraps midnight): messages with
# a timestamp >= QUIET_START or < QUIET_END count as late-night activity.
QUIET_START, QUIET_END = 23, 6

CMD_RE = re.compile(r"<command-name>\s*(/?[\w:_-]+)\s*</command-name>")

# prompts that read as corrections of Claude's previous output (heuristic,
# matched at the start of the prompt) — a signal for the fluency report
CORRECT_RE = re.compile(
    r"^(no+[,.! ]|no+$|not (that|what|like)|wrong|nope|incorrect|actually[, ]"
    r"|that'?s (not|wrong)|stop[,.! ]|undo |revert |don'?t |you (missed|forgot|broke))",
    re.IGNORECASE)


def price_for(model: str):
    m = (model or "").lower()
    for key, p in PRICING:
        if key in m:
            return p
    return DEFAULT_PRICE


def count_lines(text) -> int:
    if not isinstance(text, str) or not text:
        return 0
    return text.count("\n") + 1


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _day_stat():
    return {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        "cost": 0.0, "loc": 0, "prompts": 0, "prompt_lines": 0,
        "prompt_words": 0, "messages": 0, "human_tokens": 0,
        "reads": 0, "writes": 0, "interruptions": 0, "tool_errors": 0,
        "late_msgs": 0, "corrections": 0, "cache_savings": 0.0,
    }


def _proj_stat():
    return {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "loc": 0, "prompts": 0,
        "sessions": set(), "cost": 0.0,
        "days": defaultdict(lambda: {"output": 0, "loc": 0}),
        "files": Counter(),
    }


class Stats:
    def __init__(self):
        self.days = defaultdict(_day_stat)
        self.models = defaultdict(lambda: {
            "msgs": 0, "input": 0, "output": 0, "cache_read": 0,
            "cache_write": 0, "cost": 0.0,
        })
        self.tools = Counter()
        self.slash = Counter()
        self.projects = defaultdict(_proj_stat)
        self.sessions = {}          # sid -> per-session record
        self.session_titles = {}    # sid -> AI-generated title from the logs
        self.branches = defaultdict(lambda: {"msgs": 0, "output": 0, "loc": 0})
        self.heatmap = defaultdict(int)   # (weekday, hour) -> messages
        self.day_buckets = defaultdict(set)  # day -> set of active 10-min buckets
        self.prompt_words = 0
        self.prompt_chars = 0
        self.longest_prompts = []   # list of (lines, preview, day)
        self.files_touched = Counter()
        self.seen_msg_ids = set()
        self.seen_tool_ids = set()
        self.errors = 0
        self.tool_errors = 0
        self.parse_errors = 0   # log entries we failed to parse (format drift)
        self.permission_modes = Counter()  # auto / default / acceptEdits snapshots
        self.subagents = Counter()         # Agent tool calls, by subagent_type
        self.project_cwd = {}              # project label -> real absolute path (first seen)
        self.last_day = None               # most recent dated entry seen (undated events piggyback on it)
        self.feature_days = defaultdict(set)  # feature id -> days it was used (see ADOPTION_FEATURES)
        # --- per-event records for the Flow / Quality / Value tabs. Every record
        # carries its local day, so any window can be sliced out of the full
        # history without re-parsing (see build_flow / build_quality / build_value).
        self.turns = []        # {day, sid, secs|None, steps, err, intr, corrected}
        self.tool_calls = []   # (day, tool, family, secs, ok)
        self.cmd_runs = []     # (day, sid, kind, ok)  kind in test/build/lint
        self.episodes = []     # (day, tool, fails, steps_to_recover|None)  closed failure episodes
        self.edits = []        # (day, sid, path, project)
        self.safety = []       # (day, category)
        self.perm_decisions = []   # (day, decision, source)
        self.thinking = []     # (day, ms)
        self.efforts = []      # (day, effort)
        self.pending_tools = {}    # tool_use_id -> info, until its result arrives
        # --- what the log contains (all-time, for the Data Health tab)
        self.hc = Counter()
        self.entry_types = Counter()
        self.attachment_types = Counter()
        self.versions = Counter()
        self.entrypoints = Counter()

    # -- merge helpers ------------------------------------------------------
    def touch_session(self, sid, ts, project):
        if not sid:
            return None
        s = self.sessions.setdefault(sid, {
            "start": ts, "end": ts, "msgs": 0, "project": project,
            "reads": 0, "writes": 0, "loc": 0, "prompts": 0, "output": 0,
            "bash": 0,
            "cost": 0.0, "first_call_cost": 0.0, "api_calls": 0, "cache_resets": 0,
            "seq": 0, "last_write_seq": -1, "last_verify_seq": -1, "last_cmd_seq": -1,
            "test_files": 0, "first_edit_s": None, "model_cost": Counter(),
            "turn": None, "last_turn": None, "open_err": {}, "start_dt": None,
        })
        if ts:
            if not s["start"] or ts < s["start"]:
                s["start"] = ts
            if not s["end"] or ts > s["end"]:
                s["end"] = ts
        s["msgs"] += 1
        return s


def _begin_turn(st, sess, sid, day, ts, is_correction):
    """A real prompt starts a turn; a prompt that follows an unfinished turn closes it as abandoned."""
    prev = sess["turn"]
    if prev is not None:
        prev["secs"] = None
        prev.pop("start", None)
        st.turns.append(prev)
        sess["last_turn"] = prev
    last = sess["last_turn"]
    if is_correction and last is not None:
        last["corrected"] = True
    sess["turn"] = {"day": day, "sid": sid, "start": ts, "secs": None, "steps": 0,
                    "err": False, "intr": False, "corrected": False}


def _end_turn(st, sess, ts):
    turn = sess["turn"]
    if turn is None or ts is None or turn.get("start") is None:
        return
    turn["secs"] = max(0.0, (ts - turn.pop("start")).total_seconds())
    st.turns.append(turn)
    sess["last_turn"] = turn
    sess["turn"] = None


def _handle_tool_results(content, st, sess, day, ts, side):
    """Every tool_result block: error counts (as before), tool timing, test/build/lint
    outcomes, and failure -> recovery episodes."""
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "tool_result":
            continue
        is_err = bool(b.get("is_error"))
        if is_err:
            st.tool_errors += 1
            if day:
                st.days[day]["tool_errors"] += 1
        st.hc["tool_results"] += 1
        info = st.pending_tools.pop(b.get("tool_use_id"), None)
        if not info:
            continue
        st.hc["tool_results_matched"] += 1
        name = info["name"]
        if day and ts is not None and info["ts"] is not None:
            st.tool_calls.append((day, name, info["family"],
                                  max(0.0, (ts - info["ts"]).total_seconds()), not is_err))
        if is_err and name in CMD_TOOLS and EXIT_RE.search(_tool_result_text(b)):
            st.hc["exit_code_text"] += 1
        if day and info["kind"] in ("test", "build", "lint"):
            st.cmd_runs.append((day, info["sid"], info["kind"], not is_err))
        if sess is None or info["side"]:
            continue
        if sess["turn"] is not None and is_err:
            sess["turn"]["err"] = True
        oe = sess["open_err"]
        for ep in oe.values():
            ep["steps"] += 1
        if is_err:
            ep = oe.get(name)
            if ep is None:
                oe[name] = {"fails": 1, "steps": 0, "day": day}
            else:
                ep["fails"] += 1
        else:
            ep = oe.pop(name, None)
            if ep:
                st.episodes.append((ep["day"], name, ep["fails"], ep["steps"]))



def parse_entry(entry: dict, project: str, st: Stats, min_day=None, max_day=None):
    etype = entry.get("type")
    st.entry_types[etype] += 1
    if entry.get("version"):
        st.versions[entry["version"]] += 1
    if entry.get("entrypoint"):
        st.entrypoints[entry["entrypoint"]] += 1

    # AI-generated session title lines (written by Claude Code itself)
    if etype == "ai-title":
        sid, title = entry.get("sessionId"), entry.get("aiTitle")
        if sid and title:
            st.session_titles[sid] = title
        return

    # permission-mode snapshots ("auto" / "default" / "acceptEdits") carry no
    # timestamp of their own — they piggyback on the most recently seen dated
    # entry so they still respect the selected date range.
    if etype == "permission-mode":
        day = st.last_day
        if min_day is not None and (not day or day < min_day):
            return
        if max_day is not None and (not day or day > max_day):
            return
        st.permission_modes[entry.get("permissionMode") or "unknown"] += 1
        if entry.get("permissionMode") == "plan" and day:
            st.feature_days["plan"].add(day)
        return

    if etype in ("system", "attachment"):
        _parse_aux_entry(entry, etype, st, min_day, max_day)
        return

    if etype not in ("user", "assistant"):
        return
    if entry.get("isMeta"):
        return

    ts_raw = entry.get("timestamp") or ""
    day, hour, weekday, ts = "", None, None, None
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone()
        day = ts.strftime("%Y-%m-%d")
        hour, weekday = ts.hour, ts.weekday()
    except Exception:
        pass
    if day:
        st.last_day = day

    cwd = entry.get("cwd")
    if cwd and project not in st.project_cwd:
        st.project_cwd[project] = cwd

    # date-window filter (used to build a range-scoped Stats). When no window is
    # set (all-time) undated entries are kept exactly as before.
    if min_day is not None and (not day or day < min_day):
        return
    if max_day is not None and (not day or day > max_day):
        return

    sid = entry.get("sessionId")
    branch = entry.get("gitBranch") or "(none)"
    sess = st.touch_session(sid, ts_raw, project)
    side = bool(entry.get("isSidechain"))      # subagent work, kept out of turn/verification tracking
    st.hc["entries"] += 1
    if day:
        st.hc["entries_dated"] += 1
    if side:
        st.hc["sidechain_entries"] += 1
        if day:
            st.feature_days["subagents"].add(day)
    if sess is not None and ts is not None and (sess["start_dt"] is None or ts < sess["start_dt"]):
        sess["start_dt"] = ts
    pdec = entry.get("permissionDecision")
    if isinstance(pdec, dict) and day:
        st.hc["permission_decisions"] += 1
        st.perm_decisions.append((day, str(pdec.get("decision")), str(pdec.get("source"))))
    if day:
        st.days[day]["messages"] += 1
    if hour is not None:
        st.heatmap[(weekday, hour)] += 1
        if day:
            # 10-minute activity buckets — the basis for "active time"
            st.day_buckets[day].add(hour * 6 + ts.minute // 10)
            if hour >= QUIET_START or hour < QUIET_END:
                st.days[day]["late_msgs"] += 1

    msg = entry.get("message") or {}
    content = msg.get("content")

    # ------------------------------------------------------------------ user
    if etype == "user":
        # failed tool results ride back on user-type entries
        if isinstance(content, list):
            _handle_tool_results(content, st, sess, day, ts, side)
        text = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            if texts:
                text = "\n".join(texts)
        if not text:
            return
        # slash commands
        cmd = CMD_RE.search(text)
        if cmd:
            st.slash[cmd.group(1)] += 1
            if day:
                st.feature_days["slash"].add(day)
            return  # command invocations aren't hand-typed prompts
        if text.startswith("<local-command") or text.startswith("<command-"):
            return
        if "[Request interrupted" in text:
            st.errors += 1
            if day:
                st.days[day]["interruptions"] += 1
            if sess is not None and not side and sess["turn"] is not None:
                sess["turn"]["intr"] = True
            return
        lines = count_lines(text)
        words = len(text.split())
        if day and CORRECT_RE.match(text.strip()):
            st.days[day]["corrections"] += 1
        if day:
            st.days[day]["prompts"] += 1
            st.days[day]["prompt_lines"] += lines
            st.days[day]["prompt_words"] += words
            # ~4 chars per token: estimate of tokens the human typed
            st.days[day]["human_tokens"] += max(1, len(text) // 4)
        if sess is not None and not side:
            _begin_turn(st, sess, sid, day, ts, bool(CORRECT_RE.match(text.strip())))
        st.prompt_words += words
        st.prompt_chars += len(text)
        st.projects[project]["prompts"] += 1
        if sess:
            sess["prompts"] += 1
        preview = text.strip().replace("\n", " ")[:120]
        st.longest_prompts.append((lines, words, preview, day))
        st.longest_prompts.sort(key=lambda x: -x[0])
        del st.longest_prompts[40:]   # keep a pool so range-filtering still yields 8
        return

    # -------------------------------------------------------------- assistant
    usage = msg.get("usage") or {}
    model = msg.get("model") or "unknown"
    mid = msg.get("id") or entry.get("uuid")
    dedup_key = (mid, entry.get("requestId"))
    st.hc["assistant_lines"] += 1
    if msg.get("stop_reason"):
        st.hc["stop_reason"] += 1
        if msg.get("stop_reason") == "end_turn" and sess is not None and not side:
            _end_turn(st, sess, ts)
    td = entry.get("thinkingDurationMs")
    if isinstance(td, (int, float)) and day and (mid, "think") not in st.seen_msg_ids:
        st.seen_msg_ids.add((mid, "think"))
        st.hc["thinking"] += 1
        st.thinking.append((day, td))

    cost_override = entry.get("_cost_override")     # Copilot: credits instead of a price-based estimate
    if isinstance(cost_override, bool) or not isinstance(cost_override, (int, float)):
        cost_override = None
    if usage and dedup_key in st.seen_msg_ids:
        st.hc["duplicate_lines"] += 1
    if usage and dedup_key not in st.seen_msg_ids:
        st.seen_msg_ids.add(dedup_key)
        i = usage.get("input_tokens", 0) or 0
        o = usage.get("output_tokens", 0) or 0
        cw = usage.get("cache_creation_input_tokens", 0) or 0
        cr = usage.get("cache_read_input_tokens", 0) or 0
        p = price_for(model)
        cost = (i * p["in"] + o * p["out"] + cw * p["in"] * 1.25 + cr * p["in"] * 0.10) / 1_000_000
        if cost_override is not None:
            cost = cost_override
        st.hc["assistant_msgs"] += 1
        if entry.get("effort") and day:
            st.hc["effort"] += 1
            st.efforts.append((day, str(entry["effort"])))
        if day:
            d = st.days[day]
            d["input"] += i; d["output"] += o
            d["cache_read"] += cr; d["cache_write"] += cw
            d["cost"] += cost
            # what caching saved: reads billed at 10% instead of 100%, minus the 25% write premium
            if cost_override is None:
                d["cache_savings"] += (cr * 0.90 - cw * 0.25) * p["in"] / 1_000_000
        m = st.models[model]
        m["msgs"] += 1; m["input"] += i; m["output"] += o
        m["cache_read"] += cr; m["cache_write"] += cw; m["cost"] += cost
        pr = st.projects[project]
        pr["input"] += i; pr["output"] += o; pr["cost"] += cost
        pr["cache_read"] += cr; pr["cache_write"] += cw
        if day:
            pr["days"][day]["output"] += o
        if sid:
            pr["sessions"].add(sid)
        if sess:
            sess["output"] += o
            sess["cost"] += cost
            sess["model_cost"][model] += cost
            if sess["api_calls"] == 0:
                sess["first_call_cost"] = cost
            elif cw > cr:
                sess["cache_resets"] += 1
            sess["api_calls"] += 1
        b = st.branches[branch]
        b["msgs"] += 1; b["output"] += o
    elif cost_override is not None and not usage and dedup_key not in st.seen_msg_ids:
        # credits with no tokens (Copilot, exact-token mode): cost totals only — no message or call counts
        st.seen_msg_ids.add(dedup_key)
        if day:
            st.days[day]["cost"] += cost_override
        st.models[model]["cost"] += cost_override
        st.projects[project]["cost"] += cost_override
        if sess:
            sess["cost"] += cost_override
            sess["model_cost"][model] += cost_override

    # tool calls + lines of code
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tid = block.get("id")
            if tid:
                if tid in st.seen_tool_ids:
                    continue
                st.seen_tool_ids.add(tid)
            name = block.get("name", "?")
            st.tools[name] += 1
            feat = "mcp" if name.startswith("mcp_") else TOOL_FEATURES.get(name)   # mcp__x__y (Claude), mcp_x_y (Copilot)
            if feat and day:
                st.feature_days[feat].add(day)
            if name in READ_TOOLS:
                if day:
                    st.days[day]["reads"] += 1
                if sess:
                    sess["reads"] += 1
            elif name in WRITE_TOOLS:
                if day:
                    st.days[day]["writes"] += 1
                if sess:
                    sess["writes"] += 1
            elif name in CMD_TOOLS:
                if sess:
                    sess["bash"] += 1
            elif name == "Agent":
                inp0 = block.get("input") or {}
                st.subagents[inp0.get("subagent_type") or "unknown"] += 1
            inp = block.get("input") or {}
            st.hc["tool_uses"] += 1
            is_cmd = name in CMD_TOOLS
            cmd = inp.get("command") if is_cmd else None
            kind = classify_cmd(cmd) if is_cmd else None
            if tid:
                st.pending_tools[tid] = {
                    "name": name, "ts": ts, "sid": sid, "side": side, "kind": kind,
                    "family": cmd_family(cmd) if is_cmd else name}
            fp0 = inp.get("file_path") or inp.get("notebook_path") or ""
            if day and is_cmd and isinstance(cmd, str):
                for cat in risky_categories(cmd):
                    st.safety.append((day, cat))
            if day and fp0 and (name in WRITE_TOOLS or name == "Read") and SECRET_PATH_RE.search(fp0):
                st.safety.append((day, "secret_file_touched"))
            if sess is not None and not side:
                sess["seq"] += 1
                if name in WRITE_TOOLS:
                    sess["last_write_seq"] = sess["seq"]
                    if sess["first_edit_s"] is None and ts is not None and sess["start_dt"] is not None:
                        sess["first_edit_s"] = max(0.0, (ts - sess["start_dt"]).total_seconds())
                    if fp0:
                        if day:
                            st.edits.append((day, sid, fp0, project))
                        if TEST_FILE_RE.search(fp0):
                            sess["test_files"] += 1
                elif is_cmd:
                    sess["last_cmd_seq"] = sess["seq"]
                    if kind in ("test", "build", "lint"):
                        sess["last_verify_seq"] = sess["seq"]
                if sess["turn"] is not None:
                    sess["turn"]["steps"] += 1
            loc = 0
            if name in CODE_TOOLS_WRITE:
                loc = count_lines(inp.get("content"))
            elif name in CODE_TOOLS_EDIT:
                loc = count_lines(inp.get("new_string"))
            elif name == "MultiEdit":
                loc = sum(count_lines(e.get("new_string")) for e in inp.get("edits", [])
                          if isinstance(e, dict))
            elif name == "NotebookEdit":
                loc = count_lines(inp.get("new_source"))
            if isinstance(inp.get("_loc"), int):      # Copilot: lines already counted by copilot_source.tool_loc
                loc = inp["_loc"]
            if loc:
                if day:
                    st.days[day]["loc"] += loc
                st.projects[project]["loc"] += loc
                if day:
                    st.projects[project]["days"][day]["loc"] += loc
                st.branches[branch]["loc"] += loc
                if sess:
                    sess["loc"] += loc
                fp = inp.get("file_path") or inp.get("notebook_path")
                if fp:
                    st.files_touched[os.path.basename(fp)] += loc
                    st.projects[project]["files"][os.path.basename(fp)] += loc


# ---------------------------------------------------------------------------
# Incremental scanner (re-reads only files whose mtime changed)
# ---------------------------------------------------------------------------

class Scanner:
    def __init__(self, root):
        self.root = root
        self._cache = {}     # path -> (mtime, size)
        self._entries = {}   # path -> list[dict]
        self._lock = threading.Lock()

    def scan(self):
        found = set()
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in files:
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, fn)
                found.add(path)
                try:
                    stat = os.stat(path)
                    key = (stat.st_mtime, stat.st_size)
                except OSError:
                    continue
                if self._cache.get(path) == key:
                    continue
                entries = []
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entries.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                except OSError:
                    continue
                self._cache[path] = key
                self._entries[path] = entries
        # drop deleted files
        for path in list(self._entries):
            if path not in found:
                self._entries.pop(path, None)
                self._cache.pop(path, None)

    def available(self) -> bool:
        return os.path.isdir(self.root)

    def build_stats(self, min_day=None, max_day=None) -> Stats:
        with self._lock:
            self.scan()
            st = Stats()
            for path, entries in self._entries.items():
                project = os.path.basename(os.path.dirname(path)) or "unknown"
                # prettify: Claude Code encodes the cwd in the dir name
                project = project.lstrip("-").replace("--", "/").replace("-", "/")
                for e in entries:
                    try:
                        parse_entry(e, project, st, min_day, max_day)
                    except Exception:
                        st.parse_errors += 1
            if st.parse_errors:
                print(f"[warn] {st.parse_errors} log entries could not be parsed "
                      "(log format may have changed - stats may be incomplete)")
            return st


# ---------------------------------------------------------------------------
# Sources: the AI tools this dashboard can read. Each has a scanner with
# available() and build_stats(min_day, max_day) -> Stats.
# ---------------------------------------------------------------------------

SOURCES = {
    "claude": {"label": "Claude Code", "short": "Claude", "cost_unit": "usd",
               "instructions": ("CLAUDE.md", "AGENTS.md"), "skip_recs": ()},
    "copilot": {"label": "GitHub Copilot", "short": "Copilot", "cost_unit": "credits",
                "instructions": (".github/copilot-instructions.md", "AGENTS.md"),
                "skip_recs": ("coldstart", "cachereset"),
                # the default: no "interrupted" figure. build_payload clears it when the scanned chat logs record
                # stopped requests (requests[].modelState), so the advisor, weekly text and KPIs follow the data
                "no_interruptions": True,
                # advice that names a Claude-only feature (/clear, /compact, Claude Code's permission rules), by rec id
                "rec_actions": {
                    "longsess": "Start a new chat for each task.",
                    "risky": "Review VS Code's terminal auto-approve settings for Copilot, so the commands you "
                             "never want to run unattended always ask first.",
                }},
}


COPILOT_CHAT_MODES = ("agent", "ask", "edit", "plan")
COPILOT_SLASH_RE = re.compile(r"\s*(/[A-Za-z][\w:-]*)(?:\s|$)")     # "/explain this", not "/home/me/x"


def _file_sigs(paths):
    """(path, (mtime, size)) for each file, None for one that is gone: a session's cache key."""
    out = []
    for path in paths:
        try:
            s = os.stat(path)
            out.append((path, (s.st_mtime, s.st_size)))
        except (OSError, ValueError):
            out.append((path, None))
    return tuple(out)


def copilot_stats() -> Stats:
    """An empty Stats with the Copilot-only counters, kept off Stats.__init__ so Claude payloads stay as they are."""
    st = Stats()
    st.premium_days = Counter()        # day -> premium requests (one prompt x its multiplier)
    st.surfaces = {"vscode": Counter(), "cli": Counter()}   # prompts / output / sessions, by where a session started
    st.token_modes = Counter()         # "exact" / "approx" / "none" -> sessions
    st.no_cache_split = 0              # sessions whose tokens have no cache split (approx)
    st.split_days = defaultdict(Counter)   # day -> input / cache_read of sessions that have a cache split (exact)
    st.no_credit_calls = 0             # LLM calls with tokens but no credits (premium-billed ones excluded)
    st.copilot_versions = Counter()    # Copilot version (agent logs' session.start) -> sessions
    st.unknown_types = Counter()       # agent-log record types the reader did not recognise
    st.chat_requests = 0               # chat requests with a timestamp, over every scanned session (not just the range)
    st.chat_states = 0                 # ... of which record a modelState: stopped requests are logged (newer VS Code)
    st.bad_lines = 0                   # lines that were not JSON, or files that could not be read
    return st


def _one_credit_store(primary, exact):
    """A session's credits come from one store, never two (§5.2). When any exact call carries credits,
    the chat or transcript translation's credits go (its premium multiplier stays). Otherwise, when the
    chat recorded credits, those stand, and the exact calls are no longer "missing" credits; they keep
    their 0.0 pin, so their tokens are never priced from the Claude table."""
    if any("_cost_override" in e and not e.get("_no_credits") for e in exact):
        for e in primary:
            e.pop("_cost_override", None)
            e.pop("_no_credits", None)
    elif any("_cost_override" in e and not e.get("_no_credits") for e in primary):
        for e in exact:
            e.pop("_no_credits", None)


def _first_prompt(events):
    """The first real prompt's text, for a session title: never a sub-agent's task, and never the
    "[Request interrupted by user]" marker a stopped request leaves (parse_entry does not count it as a prompt)."""
    for e in events:
        text = (e.get("message") or {}).get("content") if e.get("type") == "user" else None
        if (isinstance(text, str) and text.strip() and not e.get("isSidechain")
                and "[Request interrupted" not in text):
            return " ".join(text.split())[:60]
    return None


def _translate_copilot_session(sid, kind, path, ws, debug, otel, row):
    """One Copilot session (§5.2-5.3) as Claude-shaped events plus what build_stats records about it.

    `kind` is the primary store: "cli" (its own usage records are exact; it wins outright), "chat" or
    "transcript". For the last two the tokens come from the OpenTelemetry or debug-log calls when there
    are any (the primary is then translated without usage), else from the chat's own per-request
    numbers (approx.); a transcript has none. `row` is the session-store row ({} when there is none)."""
    t = {"kind": kind, "surface": "vscode", "title": None, "chat_mode": None, "version": None,
         "unknown": Counter(), "bad": 0, "states": (0, 0)}
    calls = [] if kind == "cli" else copilot_source.exact_calls_for(sid, debug, otel)[1]
    folder = branch = None
    if kind == "chat":
        sess, bad = copilot_source.load_chat_session(path)
        t["bad"] += bad
        primary = copilot_source.chat_session_to_events(sess, sid, None, None, None, with_usage=not calls)
        chat = copilot_source.chat_session_meta(sess)
        t["title"], t["chat_mode"] = chat["title"], chat["mode"]
        t["states"] = copilot_source.chat_state_counts(sess)
    else:                                   # an agent log: the CLI's events.jsonl or a VS Code transcript
        lines = copilot_source.read_lines(path)
        if lines is None:                   # the file could not be read
            t["bad"] += 1
        primary, meta = copilot_source.agent_log_to_events(lines or [], sid, None, None, None,
                                                           with_usage=kind == "cli")
        t["bad"] += meta["bad_lines"]
        t["unknown"].update(meta["unknown_types"])
        t["version"] = meta["copilot_version"]
        if kind == "cli":
            folder, branch = meta["cwd"], meta["branch"]
            t["surface"] = "vscode" if meta["host_type"] == "vscode" else "cli"
    if kind != "cli":
        folder = copilot_source.workspace_folder(ws)
    exact = copilot_source.usage_events(sid, calls, None, None, None) if calls else []
    if exact:                               # tokens from OTel / debug logs: credits from one store too
        _one_credit_store(primary, exact)
    mode = "exact" if kind == "cli" or calls else ("approx" if kind == "chat" else "none")
    if not any((e.get("message") or {}).get("usage") for e in primary + exact):
        mode = "none"                       # the chosen store holds no token counts for this session
    folder = folder or row.get("cwd")
    branch = branch or row.get("branch") or ""
    events = primary + exact
    for e in events:                        # one project folder and branch for the whole session
        if "cwd" in e:
            e["cwd"] = folder or ""
        if "gitBranch" in e:
            e["gitBranch"] = branch
    t.update(events=events, mode=mode, folder=folder, label=copilot_source.project_label(folder),
             title=t["title"] or row.get("summary") or _first_prompt(primary))
    return t


def _failed_copilot_session(kind):
    """What build_stats records for a session whose translation raised: no events, one bad file for Data Health.
    Cached like a real translation, so the same files are not tried again on every scan."""
    return {"kind": kind, "surface": "vscode", "title": None, "chat_mode": None, "version": None,
            "unknown": Counter(), "bad": 1, "states": (0, 0), "events": [], "mode": "none",
            "folder": None, "label": copilot_source.project_label(None)}


def _feed_copilot_session(st, sid, t, min_day, max_day):
    """parse_entry() every event of one translated session into `st`, plus the Copilot-only counters."""
    label = t["label"]
    if t["folder"]:
        st.project_cwd.setdefault(label, t["folder"])
    if t["title"]:
        st.session_titles[sid] = t["title"]
    split = t["mode"] == "exact"                       # its tokens have a cache split: they feed the cache hit rate
    for e in t["events"]:
        msg = e.get("message") or {}
        key = (msg.get("id") or e.get("uuid"), e.get("requestId"))
        # credits missing: read from the marker, never from a missing cost; a premium-billed request is not missing
        missing = bool(e.get("_no_credits")) and "_premium" not in e and key not in st.seen_msg_ids
        sday = _entry_time(e)[1] if split and msg.get("usage") else ""
        if sday:
            d0 = st.days.get(sday)
            before = (d0["input"], d0["cache_read"]) if d0 is not None else (0, 0)
        try:
            parse_entry(e, label, st, min_day, max_day)
        except Exception:
            st.parse_errors += 1
            continue
        if sday and st.days.get(sday) is not None:     # what parse_entry added (nothing for a repeat or out of range)
            d1 = st.days[sday]
            di, dc = d1["input"] - before[0], d1["cache_read"] - before[1]
            if di or dc:
                st.split_days[sday]["input"] += di
                st.split_days[sday]["cache_read"] += dc
        if missing and key in st.seen_msg_ids:        # counted by parse_entry: in range, not a repeat
            st.no_credit_calls += 1
        premium = e.get("_premium")
        if isinstance(premium, bool) or not isinstance(premium, (int, float)):
            premium = None
        text = msg.get("content") if e.get("type") == "user" and not e.get("isSidechain") else None
        prompt = isinstance(text, str) and bool(text.strip())
        if premium is None and not prompt:
            continue
        _ts, day = _entry_time(e)
        if not _in(day, min_day, max_day):
            continue
        if premium is not None:
            st.premium_days[day] += premium
        if prompt:
            if t["chat_mode"] in COPILOT_CHAT_MODES:
                st.feature_days[t["chat_mode"]].add(day)
            cmd = COPILOT_SLASH_RE.match(text)
            if cmd:
                st.slash[cmd.group(1)] += 1
                st.feature_days["slash"].add(day)
    s = st.sessions.get(sid)
    if s is not None:                                  # the session has activity in the range
        # its project counts it even without token counts (parse_entry adds a session only on the usage path)
        st.projects[label]["sessions"].add(sid)
        st.token_modes[t["mode"]] += 1
        if t["mode"] == "approx":
            st.no_cache_split += 1
        sf = st.surfaces[t["surface"]]
        sf["sessions"] += 1
        sf["prompts"] += s["prompts"]
        sf["output"] += s["output"]
    if t["version"]:
        st.copilot_versions[t["version"]] += 1
    st.unknown_types.update(t["unknown"])
    st.bad_lines += t["bad"]
    st.chat_requests += t["states"][0]               # whatever the range: it says what this VS Code logs
    st.chat_states += t["states"][1]


class CopilotScanner:
    """GitHub Copilot's logs. Finding the files walks several folders, so the result is
    kept and re-found at most every `files_ttl` seconds. Each session is translated once and
    kept until a file it read changes; build_stats() then only re-runs parse_entry()."""
    files_ttl = 15

    def __init__(self, user_roots, copilot_home):
        self.user_roots = list(user_roots)
        self.copilot_home = copilot_home
        self._files = None
        self._files_at = 0.0
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._sessions = {}           # sid -> (cache key, translated session)
        self._otel = (None, {})       # (files key, read_otel_calls index)
        self._store = (None, {})      # (files key, read_session_store rows)

    def files(self) -> dict:
        """copilot_source.discover() for this machine."""
        with self._lock:
            if self._files is None or time.monotonic() - self._files_at >= self.files_ttl:
                self._files = copilot_source.discover(self.user_roots, self.copilot_home)
                self._files_at = time.monotonic()
            return self._files

    def available(self) -> bool:
        f = self.files()
        return bool(f["chat"] or f["transcripts"] or f["cli"])

    def _otel_index(self, paths):
        """read_otel_calls() over the OTel files, read again only when one of them changed."""
        key = _file_sigs(paths)
        if self._otel[0] != key:
            self._otel = (key, copilot_source.read_otel_calls(paths))
        return self._otel[1]

    def _store_rows(self, paths):
        """read_session_store() over the session-store databases, read again only when one (or its -wal) changed,
        or when the last read of one failed."""
        key = _file_sigs([p for db in paths for p in (db, db + "-wal")])
        if self._store[0] != key:
            problems = []
            rows = copilot_source.read_session_store(paths, problems)
            # a database that could not be read (copied mid-write, locked) is tried again next scan, not kept as a gap
            self._store = (None if problems else key, rows)
        return self._store[1]

    def _session(self, sid, f, otel, store):
        """(key, translation) for one session; the cached one while nothing it read has changed.

        The primary store is the CLI log, else the chat file, else the transcript. The key holds the
        (mtime, size) of every file the session reads (its log, workspace.json, its debug logs) plus
        its slice of the two shared stores (its OTel calls and its session-store row), so a change to
        another session's OTel spans or store row does not re-read this one. A session whose translation raises
        gets _failed_copilot_session() under the same key: counted as bad, and not tried again until a file changes."""
        if sid in f["cli"]:
            kind, path, ws = "cli", f["cli"][sid], ""
        else:
            kind = "chat" if sid in f["chat"] else "transcript"
            entry = f["chat"][sid] if kind == "chat" else f["transcripts"][sid]
            path, ws = entry["path"], entry["workspace"]
        used = [path] + ([os.path.join(ws, "workspace.json")] if ws else [])
        calls = None if kind == "cli" else otel.get(sid)
        if kind != "cli" and not calls:
            used += f["debug"].get(sid, [])
        row = store.get(sid)
        key = (kind, _file_sigs(used), calls, row)
        old = self._sessions.get(sid)
        if old is not None and old[0] == key:
            return old
        try:
            return key, _translate_copilot_session(sid, kind, path, ws, f["debug"], otel, row or {})
        except Exception:                  # one malformed session never takes the Copilot and All AI views down
            return key, _failed_copilot_session(kind)

    def build_stats(self, min_day=None, max_day=None) -> Stats:
        """Copilot's Stats for the range: every session (chat, transcript or CLI, by id) translated
        once from one store, its tokens from one source, then fed through parse_entry()."""
        with self._build_lock:
            f = self.files()
            otel = self._otel_index(f["otel"])
            store = self._store_rows(f["stores"])
            st = copilot_stats()
            sessions = {}
            for sid in sorted(set(f["chat"]) | set(f["transcripts"]) | set(f["cli"])):
                sessions[sid] = self._session(sid, f, otel, store)
                _feed_copilot_session(st, sid, sessions[sid][1], min_day, max_day)
            self._sessions = sessions          # sessions whose files are gone drop out of the cache
            return st


def copilot_extras(st: Stats) -> dict:
    """The Copilot-only figures for the payload: premium requests (a unit of their own, never added to
    credits), the VS Code / CLI split and how many sessions have exact, approx. or no token counts."""
    surfaces = getattr(st, "surfaces", {})
    modes = getattr(st, "token_modes", Counter())
    return {
        "premium_requests": round(float(sum(getattr(st, "premium_days", Counter()).values())), 2),
        "surfaces": {name: {k: surfaces.get(name, Counter())[k] for k in ("prompts", "output", "sessions")}
                     for name in ("vscode", "cli")},
        "token_modes": {mode: modes[mode] for mode in ("exact", "approx", "none")},
    }


def copilot_stops_logged(st: Stats) -> bool:
    """True when at least one scanned chat request records a modelState, i.e. this VS Code logs stopped
    requests. `st` is the Copilot Stats over full history; with no modelState anywhere there is no
    interruption figure, never a row of zeros."""
    return getattr(st, "chat_states", 0) > 0


def _tilde(path):
    """`path` with the home folder shown as ~."""
    home = os.path.normcase(os.path.expanduser("~"))
    norm = os.path.normcase(path)
    if norm == home or norm.startswith(home.rstrip(os.sep) + os.sep):
        return "~" + path[len(home):]
    return path


def copilot_root_label(user_roots, copilot_home) -> str:
    """The root chip for Copilot: the VS Code user folders and the Copilot CLI folder."""
    return " · ".join(_tilde(p) for p in list(user_roots) + [copilot_home] if p)


def enabled_sources(claude_dir, copilot) -> list:
    """The source ids to serve at startup: Claude when its log folder exists, Copilot
    when a CopilotScanner is given and has found sessions."""
    out = []
    if claude_dir and os.path.isdir(claude_dir):
        out.append("claude")
    if copilot is not None and copilot.available():
        out.append("copilot")
    return out


def pick_source(scanners, requested) -> str:
    """The source to serve for `source=requested`: that one when it is on, otherwise
    Claude, otherwise the first one that is. ("all" is not one tool's source: see wants_all_ai.)"""
    if requested in scanners:
        return requested
    if "claude" in scanners:
        return "claude"
    return next(iter(scanners), "claude")


def sources_status(scanners) -> list:
    """What the tool switch shows: each source, then "All AI", which needs them all."""
    rows = [{"id": sid, "label": s["label"], "short": s["short"],
             "available": sid in scanners and scanners[sid].available()}
            for sid, s in SOURCES.items()]
    rows.append({"id": "all", "label": "All AI", "short": "AI",
                 "available": all(r["available"] for r in rows)})
    return rows


def wants_all_ai(scanners, requested) -> bool:
    """True when `source=requested` asks for All AI and every tool is on. When a tool is missing the
    request falls back to pick_source(), as an unknown source does: there is nothing to combine."""
    return requested == "all" and sources_status(scanners)[-1]["available"]


# ---------------------------------------------------------------------------
# Token-usage notification thresholds (pure functions — no I/O, easy to test)
# ---------------------------------------------------------------------------

def milestones_crossed(prev_value, new_value, total, milestones=MILESTONES):
    """Milestone percentages (of `total`) whose threshold lies in
    (prev_value, new_value]. Comparing prev vs. new (rather than tracking
    "already fired" state) means a threshold naturally refires if usage
    drops below it (e.g. a context compaction) and climbs back past it."""
    if not total or total <= 0:
        return []
    crossed = []
    for m in milestones:
        thresh = total * m / 100.0
        if prev_value < thresh <= new_value:
            crossed.append(m)
    return crossed


def steps_crossed(prev_total, new_total, step):
    """Flat multiples of `step` whose value lies in (prev_total, new_total]."""
    if not step or step <= 0:
        return []
    prev_n = int(prev_total // step)
    new_n = int(new_total // step)
    if new_n <= prev_n:
        return []
    return [step * n for n in range(prev_n + 1, new_n + 1)]


def weighted_tokens(input_tokens, output_tokens, cache_write, cache_read):
    """Quota-weighted usage in input-token-equivalent units, using the same
    relative weights as PRICING (every tier prices output at 5x input,
    cache write at 1.25x, cache read at 0.1x) — a raw token sum wildly
    overcounts a cache-heavy coding session against Anthropic's real
    rolling-window quota, since cache reads are far cheaper than fresh input."""
    return (input_tokens + output_tokens * 5
            + cache_write * 1.25 + cache_read * 0.1)


class SessionTokenTracker:
    """Replays a Claude Code session's usage events in order and reports
    newly-crossed notification thresholds. Stateless besides the three
    running totals, so it can be resumed after a restart by passing in the
    previously persisted totals — avoids re-firing thresholds already seen."""

    def __init__(self, notify=NOTIFY, session_total=0, task_total=0,
                 last_context_usage=0):
        self.notify = notify
        self.session_total = session_total
        self.task_total = task_total
        self.last_context_usage = last_context_usage

    def on_user_message(self):
        """Call on each new real (non-tool-result) user message — starts a
        new task, so the task_step counter resets."""
        self.task_total = 0

    def on_usage(self, input_tokens, output_tokens, cache_write, cache_read):
        events = []
        added = input_tokens + output_tokens + cache_write + cache_read
        context_now = input_tokens + cache_write + cache_read

        prev_context = self.last_context_usage
        self.last_context_usage = context_now
        for m in milestones_crossed(prev_context, context_now,
                                     self.notify["context_window"]):
            events.append(("context_pct", m, context_now))

        prev_session = self.session_total
        self.session_total += added
        for step_val in steps_crossed(prev_session, self.session_total,
                                       self.notify["session_step"]):
            events.append(("session_step", step_val, self.session_total))

        prev_task = self.task_total
        self.task_total += added
        for step_val in steps_crossed(prev_task, self.task_total,
                                       self.notify["task_step"]):
            events.append(("task_step", step_val, self.task_total))

        return events

    def state(self):
        return {"session_total": self.session_total,
                "task_total": self.task_total,
                "last_context_usage": self.last_context_usage}


WINDOW_SECONDS = 5 * 3600

# Official rate-limit data, captured by a Claude Code statusLine hook (see
# ~/.claude/statusline.js) and dropped as JSON every time the status line
# renders — the exact 5h/7d plan-usage percentage Anthropic's backend
# reports, not the ceiling guess PlanWindowTracker below makes. Preferred
# over the estimate whenever it's fresh. The status line only updates while
# a session is open and actively rendering, so a stale file (older than
# RATE_LIMITS_STALE_SECONDS) is treated as unavailable and we fall back to
# the estimate rather than show a number that's stopped moving.
RATE_LIMITS_PATH = os.path.join(os.path.expanduser("~"), ".claude", "rate_limits_latest.json")
RATE_LIMITS_STALE_SECONDS = 30 * 60


def load_rate_limits(path, now=None):
    now = time.time() if now is None else now
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if now - data.get("captured_at", 0) > RATE_LIMITS_STALE_SECONDS:
        return None
    return data


def load_rate_limits_any_age(path):
    """Like load_rate_limits(), but never discards the file for being old.

    The statusline hook only re-captures when Claude Code is actively used
    on this machine (see statusline.js), so a quiet stretch longer than
    RATE_LIMITS_STALE_SECONDS makes load_rate_limits() return None. But a
    real Anthropic-reported percentage from an hour ago is still a better
    basis for plan_window_snapshot()'s fallback than PlanWindowTracker's
    single-session-calibrated ceiling guess -- callers pair this with each
    window's `resets_at` to tell a merely-old number from one that's since
    reset to 0%. Only a missing or corrupt file returns None here."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _official_pct_now(window, now):
    """A rate-limit window's used_percentage, adjusted for a capture that's
    old enough its `resets_at` has already passed -- i.e. the account-side
    window has since reset to 0% even though the last capture we have on
    disk still shows whatever it was at right before the reset. Without
    this, an old-but-real capture could be *more* misleading than the local
    estimate it's meant to replace."""
    pct = window.get("used_percentage")
    if pct is None:
        return None
    resets_at = window.get("resets_at")
    if resets_at is not None and now >= resets_at:
        return 0
    return pct


class PlanWindowTracker:
    """Approximates the Claude app's 'Current session' plan-usage panel: a
    single GLOBAL rolling window (combining every Claude Code session, not
    per-session like SessionTokenTracker) that starts on the first usage
    event seen and resets fully — not a sliding average — once WINDOW_SECONDS
    has elapsed, mirroring the app's hard reset rather than a decaying one.
    `ceiling` is a local, user-tuned estimate; Anthropic's real per-window
    token ceiling isn't queryable locally. Used only as a fallback when
    load_rate_limits() has no fresh official number (see RATE_LIMITS_PATH)."""

    def __init__(self, ceiling, window_start=None, window_total=0):
        self.ceiling = ceiling
        self.window_start = window_start
        self.window_total = window_total

    def add(self, ts_epoch, tokens):
        if self.window_start is None or ts_epoch - self.window_start >= WINDOW_SECONDS:
            self.window_start = ts_epoch
            self.window_total = 0
        prev_total = self.window_total
        self.window_total += tokens
        return milestones_crossed(prev_total, self.window_total, self.ceiling)

    def pct(self, now_epoch):
        # add() only rolls window_start/window_total over to a fresh window
        # when a *new* usage event arrives. During an idle stretch spanning
        # the 5h boundary, nothing has called add() yet, so without this
        # check pct() would keep reporting the previous, already-expired
        # window's leftover percentage (while resets_in_seconds() correctly
        # says the window is already over) -- e.g. "8% used, resets in 0h 0m".
        if not self.ceiling or self.window_start is None:
            return 0
        if now_epoch - self.window_start >= WINDOW_SECONDS:
            return 0
        return min(100, int(self.window_total / self.ceiling * 100))

    def resets_in_seconds(self, now_epoch):
        if self.window_start is None:
            return 0
        return max(0, int(self.window_start + WINDOW_SECONDS - now_epoch))

    def state(self):
        return {"window_start": self.window_start, "window_total": self.window_total}


def load_notify_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_notify_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def _fmt_k(n):
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return str(int(n))


def send_windows_toast(title, message):
    """Fire a native Windows toast via the built-in WinRT API through
    powershell.exe — no pip dependency, no module install, matches how
    Restart-Insights.ps1 already shells out to PowerShell."""
    if os.name != "nt":
        return
    ps_script = (
        '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, '
        'ContentType=WindowsRuntime] > $null;'
        '[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, '
        'ContentType=WindowsRuntime] > $null;'
        '$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('
        '[Windows.UI.Notifications.ToastTemplateType]::ToastText02);'
        '$x = $t.GetElementsByTagName("text");'
        '$x.Item(0).AppendChild($t.CreateTextNode($env:AFI_TOAST_TITLE)) > $null;'
        '$x.Item(1).AppendChild($t.CreateTextNode($env:AFI_TOAST_MSG)) > $null;'
        '$n = [Windows.UI.Notifications.ToastNotification]::new($t);'
        '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('
        '"AI Fluency Insights").Show($n)'
    )
    env = dict(os.environ, AFI_TOAST_TITLE=title, AFI_TOAST_MSG=message)
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-Command", ps_script],
            env=env, creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


class NotificationWatcher:
    """Polls the Scanner for sessions being actively written to and fires
    token-usage notifications as SessionTokenTracker crosses thresholds.

    Progress is persisted to `state_path` so restarting the dashboard mid
    -session doesn't re-fire thresholds already seen. A session's very first
    sighting (either on disk from a prior run, or freshly discovered) is
    replayed silently to build an accurate baseline before any events fire —
    otherwise reopening the dashboard mid-session would immediately dump a
    burst of "you already passed 25/50/75%" notifications.
    """

    def __init__(self, scanner, notify=NOTIFY, state_path=None, rate_limits_path=None):
        self.scanner = scanner
        self.notify = notify
        self.state_path = state_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), ".notify_state.json")
        self.rate_limits_path = rate_limits_path or RATE_LIMITS_PATH
        saved = load_notify_state(self.state_path)
        self._raw_state = saved.get("sessions", {})
        # last-seen official five_hour/seven_day used_percentage, so a
        # restart doesn't refire milestones already crossed before it -- same
        # "baseline on first sight" reasoning as _seen_counts/baseline_sids.
        self._official_prev = dict(saved.get("official_prev", {}))
        # which milestones have already fired *within the account's current
        # window* for each kind, plus that window's resets_at -- unlike
        # _official_prev's simple prev<thresh<=new check, this survives the
        # officially-reported used_percentage wobbling by a point or two
        # between statusline captures (not a true drop-and-reclimb like a
        # context compaction) without refiring the same milestone repeatedly.
        # Cleared only when resets_at itself changes, i.e. a genuinely new
        # window has started.
        self._official_fired = {k: set(v) for k, v in
                                 (saved.get("official_fired") or {}).items()}
        self._official_window_reset_at = dict(saved.get("official_window_reset_at", {}))
        # how many entries of each file were already replayed *before* this
        # process started — restoring this is what stops a restart from
        # replaying a session's whole history on top of its already-saved
        # cumulative total (which would double-count it, and can also mean
        # firing thousands of already-past thresholds in one synchronous
        # burst for a long-running session).
        self._seen_counts = dict(saved.get("seen_counts", {}))
        self.trackers = {}       # sid -> SessionTokenTracker
        self.meta = {}           # sid -> {"project":..., "title":...}
        self.recent_events = []  # for the /api/stats "notifications" field
        # True while the original Claude-only dashboard runs: it shows the same pop-ups, so this
        # copy still tracks and lists events but leaves the toasts to it (each one once, not twice).
        self.muted = False
        self._lock = threading.Lock()
        pw = saved.get("plan_window") or {}
        self.plan_tracker = PlanWindowTracker(
            ceiling=self.notify["window_ceiling"],
            window_start=pw.get("window_start"),
            window_total=pw.get("window_total", 0))

    def _tracker_for(self, sid):
        t = self.trackers.get(sid)
        if t is None:
            saved = self._raw_state.get(sid, {})
            t = SessionTokenTracker(
                notify=self.notify,
                session_total=saved.get("session_total", 0),
                task_total=saved.get("task_total", 0),
                last_context_usage=saved.get("last_context_usage", 0),
            )
            self.trackers[sid] = t
        return t

    @staticmethod
    def _is_real_user_text(content):
        # Failed tool results ride back on "user"-type entries (see
        # parse_entry) — a block-list made up entirely of tool_result isn't
        # a real user message and shouldn't reset the per-task counter.
        if isinstance(content, list):
            if content and all(isinstance(b, dict) and b.get("type") == "tool_result"
                                for b in content):
                return False
            return True
        return bool(content)

    def poll_once(self):
        if not self.notify.get("enabled"):
            return
        with self.scanner._lock:
            self.scanner.scan()
            entries_by_path = dict(self.scanner._entries)
        cutoff = time.time() - self.notify["active_window_min"] * 60
        # Snapshot which sessions we already knew about *before* this poll
        # cycle touches anything — a session's logs can span multiple files,
        # and computing this per-file (against a live-mutating self.trackers)
        # would let a session slip past the "first sight" baseline check on
        # whichever file happens to be processed second.
        known_before_poll = set(self._raw_state) | set(self.trackers)
        # Coalesce every threshold crossed this poll into one entry per
        # (sid, kind) — a burst of catch-up entries (dashboard was down, or
        # several turns landed between polls) can cross the same kind of
        # threshold repeatedly, and firing a toast per crossing turns one
        # real event into a rapid-fire stack of near-identical popups.
        pending = {}
        # (ts_epoch, tokens) for every usage event this poll, across every
        # session — the plan window is account-wide, not per session, so it
        # has to see all of them merged in true chronological order.
        plan_events = []
        for path, entries in entries_by_path.items():
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            project = os.path.basename(os.path.dirname(path)) or "unknown"
            project = project.lstrip("-").replace("--", "/").replace("-", "/")
            self._replay(path, entries, project, known_before_poll, pending, plan_events)
        for (sid, kind), (value, total) in pending.items():
            self._fire(sid, kind, value, total)
        plan_events.sort(key=lambda e: e[0])
        plan_fire = None
        for ts_epoch, tokens in plan_events:
            crossed = self.plan_tracker.add(ts_epoch, tokens)
            if crossed:
                plan_fire = (crossed[-1], self.plan_tracker.window_total)
        if plan_fire is not None:
            self._fire_plan(*plan_fire)
        self._poll_official_rate_limits()
        self._save_state()

    def _poll_official_rate_limits(self):
        data = load_rate_limits(self.rate_limits_path)
        if not data:
            return
        rl = data.get("rate_limits") or {}
        for kind, label in (("five_hour", "Current 5h session"), ("seven_day", "This week")):
            window = rl.get(kind)
            if not window or window.get("used_percentage") is None:
                continue
            pct = window["used_percentage"]
            resets_at = window.get("resets_at")
            milestones = OFFICIAL_MILESTONES[kind]

            if self._official_window_reset_at.get(kind) != resets_at:
                self._official_window_reset_at[kind] = resets_at
                self._official_fired[kind] = set()
            fired = self._official_fired.setdefault(kind, set())

            prev = self._official_prev.get(kind)
            if prev is not None:
                eligible = [m for m in milestones if pct >= m and m not in fired]
                if eligible:
                    milestone = max(eligible)
                    fired.update(m for m in milestones if m <= milestone)
                    self._fire_official(kind, label, milestone, pct, resets_at)
            else:
                # first sighting of this kind: seed the baseline without
                # firing, same "don't dump history on open" reasoning as
                # baseline_sids in _replay.
                fired.update(m for m in milestones if pct >= m)
            self._official_prev[kind] = pct

    def _replay(self, path, entries, project, known_before_poll, pending, plan_events):
        seen = self._seen_counts.get(path, 0)
        new_entries = entries[seen:]
        self._seen_counts[path] = len(entries)
        if not new_entries:
            return
        baseline_sids = ({e.get("sessionId") for e in new_entries if e.get("sessionId")}
                          - known_before_poll)
        for entry in new_entries:
            etype = entry.get("type")
            sid = entry.get("sessionId")
            if etype == "ai-title" and sid:
                title = entry.get("aiTitle")
                if title:
                    self.meta.setdefault(sid, {})["title"] = title
                continue
            if etype not in ("user", "assistant") or entry.get("isMeta") or not sid:
                continue
            meta = self.meta.setdefault(sid, {})
            meta.setdefault("project", project)
            tracker = self._tracker_for(sid)
            if etype == "user":
                content = (entry.get("message") or {}).get("content")
                if self._is_real_user_text(content):
                    tracker.on_user_message()
                continue
            usage = (entry.get("message") or {}).get("usage") or {}
            if not usage:
                continue
            input_tokens = usage.get("input_tokens", 0) or 0
            output_tokens = usage.get("output_tokens", 0) or 0
            cache_write = usage.get("cache_creation_input_tokens", 0) or 0
            cache_read = usage.get("cache_read_input_tokens", 0) or 0
            events = tracker.on_usage(input_tokens, output_tokens, cache_write, cache_read)
            if sid in baseline_sids:
                continue
            for kind, value, total in events:
                pending[(sid, kind)] = (value, total)
            # a brand-new session's own first sighting still skips the plan
            # window too — same reasoning as baseline_sids above, otherwise
            # reopening the dashboard mid-session would dump that session's
            # entire history into the account-wide total in one shot.
            try:
                ts_epoch = datetime.fromisoformat(
                    (entry.get("timestamp") or "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            plan_events.append((ts_epoch, weighted_tokens(
                input_tokens, output_tokens, cache_write, cache_read)))

    def _fire(self, sid, kind, value, total):
        meta = self.meta.get(sid, {})
        title = meta.get("title") or "Untitled session"
        project = meta.get("project", "unknown")
        label = {
            "context_pct": f"Context {value}% full ({_fmt_k(total)} / "
                            f"{_fmt_k(self.notify['context_window'])} tokens)",
            "session_step": f"Session has used {_fmt_k(total)} tokens (+{_fmt_k(value)})",
            "task_step": f"Current task has used {_fmt_k(total)} tokens (+{_fmt_k(value)})",
        }[kind]
        event = {
            "id": f"{sid}:{kind}:{value}",
            "ts": time.time(),
            "project": project,
            "title": title,
            "message": label,
        }
        with self._lock:
            self.recent_events.append(event)
            del self.recent_events[:-50]
        self._toast(f"{project} — {title}"[:64], label)

    def _fire_plan(self, value, total):
        resets_in = self.plan_tracker.resets_in_seconds(time.time())
        h, m = divmod(resets_in // 60, 60)
        label = (f"Current session {value}% used ({_fmt_k(total)} / "
                 f"{_fmt_k(self.notify['window_ceiling'])} tokens, est.) — "
                 f"resets in {h}h {m}m")
        event = {
            "id": f"plan:{value}:{int(total)}",
            "ts": time.time(),
            "project": "plan",
            "title": "Plan usage (estimate)",
            "message": label,
        }
        with self._lock:
            self.recent_events.append(event)
            del self.recent_events[:-50]
        self._toast("Plan usage limits (estimate)", label)

    def _fire_official(self, kind, label, milestone, pct, resets_at):
        resets_txt = ""
        if resets_at:
            resets_in = max(0, int(resets_at - time.time()))
            h, m = divmod(resets_in // 60, 60)
            resets_txt = f" — resets in {h}h {m}m"
        message = f"{label} {milestone}% used (official){resets_txt}"
        event = {
            "id": f"official:{kind}:{milestone}",
            "ts": time.time(),
            "project": "plan",
            "title": f"{label} usage",
            "message": message,
        }
        with self._lock:
            self.recent_events.append(event)
            del self.recent_events[:-50]
        self._toast(f"{label} usage", message)

    def _toast(self, title, message):
        if not self.muted:
            send_windows_toast(title, message)

    def plan_window_snapshot(self):
        now = time.time()
        # Prefer a fresh (<=RATE_LIMITS_STALE_SECONDS) capture; failing
        # that, fall back to whatever official capture exists, however old
        # -- Anthropic's real last-known percentage beats the local ceiling
        # guess even when stale, as long as we account for windows that
        # have since reset (see _official_pct_now below). Only when no
        # capture has EVER been written do we drop to the raw estimate.
        official = load_rate_limits(self.rate_limits_path) or load_rate_limits_any_age(self.rate_limits_path)
        if official:
            rl = official.get("rate_limits") or {}
            five = rl.get("five_hour") or {}
            five_pct = _official_pct_now(five, now)
            if five_pct is not None:
                seven = rl.get("seven_day") or {}
                seven_pct = _official_pct_now(seven, now)

                def resets_min(w):
                    ra = w.get("resets_at")
                    return max(0, int((ra - now) // 60)) if ra else None

                # The statusline only re-captures on session events (a new
                # assistant message, /compact, ...), not on a timer, so the
                # number can lag real usage by however long since the last
                # render. Surfacing that age lets the UI show *why* this can
                # be behind what /usage reports at the exact moment you check
                # it -- and, past RATE_LIMITS_STALE_SECONDS, that it's from a
                # session that's gone idle rather than a live number.
                age_min = max(0, int((now - official.get("captured_at", now)) // 60))
                return {
                    "source": "official",
                    "pct": min(100, round(five_pct)),
                    "resets_in_min": resets_min(five),
                    "week_pct": min(100, round(seven_pct)) if seven_pct is not None else None,
                    "week_resets_in_min": resets_min(seven),
                    "captured_age_min": age_min,
                }
        return {
            "source": "estimate",
            "pct": self.plan_tracker.pct(now),
            "used": self.plan_tracker.window_total,
            "ceiling": self.notify["window_ceiling"],
            "resets_in_min": self.plan_tracker.resets_in_seconds(now) // 60,
        }

    def _save_state(self):
        # Merge onto the originally-loaded sessions rather than replacing
        # them outright — a session outside the active window this poll
        # (gone quiet, or not yet rescanned) must keep its persisted total;
        # otherwise it would silently reset to 0 the next time it's touched.
        sessions = dict(self._raw_state)
        sessions.update({sid: t.state() for sid, t in self.trackers.items()})
        state = {"sessions": sessions, "seen_counts": self._seen_counts,
                  "plan_window": self.plan_tracker.state(),
                  "official_prev": self._official_prev,
                  "official_fired": {k: sorted(v) for k, v in self._official_fired.items()},
                  "official_window_reset_at": self._official_window_reset_at}
        try:
            save_notify_state(self.state_path, state)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Task-type classification (heuristic: AI session title first, tool mix as
# fallback) and wellness helpers
# ---------------------------------------------------------------------------

TASK_TYPES = ["feature", "bugfix", "refactor", "docs", "explore", "other"]


def classify_session(title, reads=0, writes=0, loc=0):
    t = " %s " % (title or "").lower()

    def has(*words):
        return any(w in t for w in words)

    if has("fix", " bug", "error", "issue", "crash", "debug", "broken",
           "fail", "troubleshoot", "repair", "not work"):
        return "bugfix"
    if has("refactor", "clean", "rename", "simplif", "reorganiz",
           "restructur", "tidy", "polish", "dedup"):
        return "refactor"
    if has("readme", "document", " docs", " doc ", "changelog",
           "write-up", "writeup", "comment"):
        return "docs"
    if has("add ", "implement", "build", "creat", " new ", "feature",
           "support", "integrat", "set up", "setup", "install", "launch",
           "generat", "redesign", "improve", "update", "upgrade"):
        return "feature"
    if has("explor", "investigat", "understand", "analy", "review",
           "explain", "research", "compare", "look at", "check ",
           "question", "how ", "why ", "what "):
        return "explore"
    # no title signal — fall back to what the session actually did
    if loc > 0 or writes > reads:
        return "feature"
    if reads:
        return "explore"
    return "other"


def longest_focus(buckets) -> int:
    """Longest run of consecutive 10-min activity buckets within a day, in minutes."""
    if not buckets:
        return 0
    b = sorted(buckets)
    best = run = 1
    for i in range(1, len(b)):
        run = run + 1 if b[i] == b[i - 1] + 1 else 1
        best = max(best, run)
    return best * 10


# ---------------------------------------------------------------------------
# Weekly aggregation (for the Weekly Report tab)
# ---------------------------------------------------------------------------

def build_weeks(st: Stats):
    weeks = {}

    def week_of(dt):
        iso = dt.isocalendar()
        y, wn = iso[0], iso[1]
        start = datetime.fromisocalendar(y, wn, 1).strftime("%Y-%m-%d")
        end = datetime.fromisocalendar(y, wn, 7).strftime("%Y-%m-%d")
        return "%d-W%02d" % (y, wn), start, end

    for day, ds in st.days.items():
        try:
            dt = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            continue
        key, start, end = week_of(dt)
        w = weeks.setdefault(key, {
            "key": key, "start": start, "end": end,
            "input": 0, "output": 0, "loc": 0, "prompts": 0,
            "reads": 0, "writes": 0, "interruptions": 0, "tool_errors": 0,
            "human_tokens": 0, "cost": 0.0, "minutes": 0.0,
            "prompt_words": 0, "corrections": 0, "late_msgs": 0,
            "messages": 0, "active_min": 0,
            "days": [], "sessions": [],
        })
        for f in ("input", "output", "loc", "prompts", "reads", "writes",
                  "interruptions", "tool_errors", "human_tokens",
                  "prompt_words", "corrections", "late_msgs", "messages"):
            w[f] += ds[f]
        w["cost"] += ds["cost"]
        w["active_min"] += len(st.day_buckets.get(day, ())) * 10
        w["days"].append({"day": day, "output": ds["output"], "loc": ds["loc"],
                          "reads": ds["reads"], "writes": ds["writes"]})

    for sid, s in st.sessions.items():
        try:
            a = datetime.fromisoformat(s["start"].replace("Z", "+00:00")).astimezone()
            b = datetime.fromisoformat(s["end"].replace("Z", "+00:00")).astimezone()
        except Exception:
            continue
        key, _, _ = week_of(a)
        if key not in weeks:
            continue
        mins = max(0.0, (b - a).total_seconds()) / 60
        weeks[key]["minutes"] += mins
        title = st.session_titles.get(sid) or "Untitled session"
        weeks[key]["sessions"].append({
            "id": sid[:8], "title": title,
            "project": s["project"], "day": a.strftime("%Y-%m-%d"),
            "min": round(mins), "loc": s["loc"], "reads": s["reads"],
            "writes": s["writes"], "output": s["output"], "prompts": s["prompts"],
            "bash": s["bash"],
            "type": classify_session(title, s["reads"], s["writes"], s["loc"]),
        })

    out = []
    for key in sorted(weeks):
        w = weeks[key]
        w["days"].sort(key=lambda x: x["day"])
        w["sessions"].sort(key=lambda x: x["day"])
        w["minutes"] = round(w["minutes"])
        w["cost"] = round(w["cost"], 2)
        out.append(w)
    return out


def active_streak(days_sorted):
    """Return (current_streak, longest_streak) of consecutive active calendar days.
    current_streak is counted backwards from the most recent active day."""
    from datetime import date, timedelta
    if not days_sorted:
        return 0, 0
    dset = set(days_sorted)
    try:
        d = date.fromisoformat(days_sorted[-1])
    except ValueError:
        return 0, 0
    cur = 0
    while d.isoformat() in dset:
        cur += 1
        d -= timedelta(days=1)
    best = run = 0
    prev = None
    for ds in days_sorted:
        try:
            cd = date.fromisoformat(ds)
        except ValueError:
            continue
        run = run + 1 if (prev and (cd - prev).days == 1) else 1
        best = max(best, run)
        prev = cd
    return cur, best


# ---------------------------------------------------------------------------
# Flow / Quality / Value engines. Each takes the full-history Stats plus an
# optional [d0, d1] day window (ISO strings) and returns plain dicts for the UI.
# Nothing here reads outside the logs; every metric is tagged in METRIC_CATALOG
# as Observed / Derived / Proxy / Estimated so it is never overstated.
# ---------------------------------------------------------------------------

def _in(day, d0, d1) -> bool:
    return bool(day) and (d0 is None or day >= d0) and (d1 is None or day <= d1)


def _median(xs):
    return statistics.median(xs) if xs else None


def _p90(xs):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, math.ceil(0.9 * len(xs)) - 1))]


def _rate(n, d):
    return _rnd(100 * n / d) if d else None


def _r1(x):
    return None if x is None else round(x, 1)


def _sess_rows(st: Stats, d0=None, d1=None):
    """(sid, session, local start day) for sessions starting inside the window."""
    rows = []
    for sid, s in st.sessions.items():
        try:
            a = datetime.fromisoformat(s["start"].replace("Z", "+00:00")).astimezone()
        except Exception:
            continue
        day = a.strftime("%Y-%m-%d")
        if _in(day, d0, d1):
            rows.append((sid, s, day))
    return rows


def _sess_minutes(s):
    try:
        a = datetime.fromisoformat(s["start"].replace("Z", "+00:00"))
        b = datetime.fromisoformat(s["end"].replace("Z", "+00:00"))
        return max(0.0, (b - a).total_seconds()) / 60
    except Exception:
        return 0.0


def _iso_monday(day: str) -> str:
    d = date.fromisoformat(day)
    return (d - timedelta(days=d.weekday())).isoformat()


def build_flow(st: Stats, d0=None, d1=None) -> dict:
    turns = [t for t in st.turns if _in(t["day"], d0, d1)]
    done = [t for t in turns if t["secs"] is not None]
    secs = [t["secs"] for t in done]
    clean = [t for t in done if not t["err"] and not t["intr"] and not t["corrected"]]
    clean_pct = _rate(len(clean), len(turns))

    by_day = defaultdict(list)
    for t in turns:
        by_day[t["day"]].append(t)
    series = []
    for day in sorted(by_day):
        ts_ = by_day[day]
        dsecs = [t["secs"] for t in ts_ if t["secs"] is not None]
        series.append({"day": day, "turns": len(ts_), "median": _r1(_median(dsecs)),
                       "p90": _r1(_p90(dsecs)),
                       "clean_pct": _rate(sum(1 for t in ts_ if t["secs"] is not None and not t["err"]
                                              and not t["intr"] and not t["corrected"]), len(ts_))})

    # where tool time goes (sequential tool calls; excludes the model's own time)
    fam = defaultdict(lambda: {"calls": 0, "secs": [], "fails": 0})
    for day, _tool, family, sec, ok in st.tool_calls:
        if _in(day, d0, d1):
            f = fam[family]
            f["calls"] += 1
            f["secs"].append(sec)
            f["fails"] += 0 if ok else 1
    total_tool = sum(sum(f["secs"]) for f in fam.values())
    tools = sorted(({"family": k, "calls": v["calls"], "total_s": round(sum(v["secs"]), 1),
                     "share_pct": _rate(sum(v["secs"]), total_tool) if total_tool else 0,
                     "median_s": _r1(_median(v["secs"])), "p90_s": _r1(_p90(v["secs"])),
                     "max_s": _r1(max(v["secs"])), "fails": v["fails"]}
                    for k, v in fam.items()), key=lambda x: -x["total_s"])[:10]
    turn_total = sum(secs)
    tool_share = min(100, _rate(total_tool, turn_total)) if turn_total and total_tool else None

    # failure -> recovery episodes: the same tool failing, then succeeding later
    eps = [(day, tool, fails, steps, True) for day, tool, fails, steps in st.episodes if _in(day, d0, d1)]
    for sid, s, _day in _sess_rows(st, d0, d1):
        for tool, ep in s["open_err"].items():
            if _in(ep["day"], d0, d1):
                eps.append((ep["day"], tool, ep["fails"], None, False))
    n_eps = len(eps)
    recovered = [e for e in eps if e[4]]
    stuck = [e for e in eps if e[2] >= 3]
    recovery_pct = _rate(len(recovered), n_eps) if n_eps else None
    fail_tools = Counter(e[1] for e in eps).most_common(5)

    rows = _sess_rows(st, d0, d1)
    mins = [_sess_minutes(s) for _sid, s, _d in rows]
    first_edit = [s["first_edit_s"] for _sid, s, _d in rows if s["first_edit_s"] is not None]
    think = [ms / 1000 for day, ms in st.thinking if _in(day, d0, d1)]
    effort = Counter(e for day, e in st.efforts if _in(day, d0, d1))

    score = None
    if len(turns) >= 5:
        rec = (len(recovered) / n_eps) if n_eps else 1.0
        stuck_share = (len(stuck) / n_eps) if n_eps else 0.0
        score = _rnd(100 * (0.5 * (len(clean) / len(turns)) + 0.3 * rec + 0.2 * (1 - stuck_share)))
    return {
        "score": score, "ready": len(turns) >= 5, "turns": len(turns), "turns_done": len(done),
        "abandoned": len(turns) - len(done),
        "turn_median_s": _r1(_median(secs)), "turn_p90_s": _r1(_p90(secs)),
        "turn_max_s": _r1(max(secs)) if secs else None,
        "steps_median": _r1(_median([t["steps"] for t in done])),
        "clean_pct": clean_pct,
        "corrected": sum(1 for t in turns if t["corrected"]),
        "interrupted": sum(1 for t in turns if t["intr"]),
        "with_errors": sum(1 for t in turns if t["err"]),
        "series": series, "tools": tools, "tool_share_pct": tool_share,
        "episodes": n_eps, "recovered": len(recovered), "recovery_pct": recovery_pct,
        "unrecovered": n_eps - len(recovered), "stuck": len(stuck),
        "recover_steps_median": _r1(_median([e[3] for e in recovered if e[3] is not None])),
        "fail_tools": [{"tool": k, "episodes": v} for k, v in fail_tools],
        "sessions": len(rows), "session_median_min": _r1(_median(mins)),
        "session_p90_min": _r1(_p90(mins)),
        "first_edit_median_s": _r1(_median(first_edit)), "first_edit_n": len(first_edit),
        "thinking_median_s": _r1(_median(think)), "thinking_total_s": round(sum(think), 1) if think else 0,
        "thinking_share_pct": min(100, _rate(sum(think), turn_total)) if think and turn_total else None,
        "effort": [{"level": k, "n": v} for k, v in effort.most_common()],
    }


def build_quality(st: Stats, d0=None, d1=None) -> dict:
    rows = _sess_rows(st, d0, d1)
    edited = [(sid, s, day) for sid, s, day in rows if s["last_write_seq"] >= 0]
    verified = [r for r in edited if r[1]["last_verify_seq"] > r[1]["last_write_seq"]]
    any_cmd = [r for r in edited if r[1]["last_cmd_seq"] > r[1]["last_write_seq"]]
    with_tests = [r for r in edited if r[1]["test_files"] > 0]

    # test / build / lint runs and how they went
    runs = [(day, sid, kind, ok) for day, sid, kind, ok in st.cmd_runs if _in(day, d0, d1)]
    groups = defaultdict(list)
    for _day, sid, kind, ok in runs:
        groups[(sid, kind)].append(ok)
    by_kind = {}
    for kind in ("test", "build", "lint"):
        ks = [(k, v) for k, v in groups.items() if k[1] == kind]
        n_runs = sum(len(v) for _k, v in ks)
        if not n_runs:
            continue
        failing = [v for _k, v in ks if False in v]
        recovered_g = [v for v in failing if True in v[v.index(False):]]
        by_kind[kind] = {
            "kind": kind, "runs": n_runs, "fails": sum(1 for _k, v in ks for ok in v if not ok),
            "groups": len(ks), "first_pass_pct": _rate(sum(1 for _k, v in ks if v[0]), len(ks)),
            "fail_groups": len(failing), "recovered_groups": len(recovered_g)}
    all_groups = list(groups.values())
    first_run_pass = _rate(sum(1 for v in all_groups if v[0]), len(all_groups)) if all_groups else None

    # rework proxy: the same file edited again and again
    edits = [(d, sid, path, proj) for d, sid, path, proj in st.edits if _in(d, d0, d1)]
    per_sess = Counter((sid, path) for _d, sid, path, _p in edits)
    reedits = sum(c - 1 for c in per_sess.values())
    reedit_pct = _rate(reedits, len(edits)) if edits else None
    files = defaultdict(lambda: {"n": 0, "days": set(), "sids": Counter(), "project": ""})
    for d, sid, path, proj in edits:
        f = files[path]
        f["n"] += 1
        f["days"].add(d)
        f["sids"][sid] += 1
        f["project"] = proj
    hot = []
    for path, f in files.items():
        if f["n"] >= 5 and (max(f["sids"].values()) >= 3 or len(f["days"]) >= 3):
            hot.append({"file": os.path.basename(path), "project": f["project"], "edits": f["n"],
                        "days": len(f["days"]), "sessions": len(f["sids"])})
    hot.sort(key=lambda x: -x["edits"])

    days = [d for d in st.days if _in(d, d0, d1)]
    n_writes = sum(st.days[d]["writes"] for d in days)
    n_prompts = sum(st.days[d]["prompts"] for d in days)
    errors = sum(st.days[d]["tool_errors"] for d in days)
    intr = sum(st.days[d]["interruptions"] for d in days)
    safety = Counter(c for d, c in st.safety if _in(d, d0, d1))
    perms = Counter((dec, src) for d, dec, src in st.perm_decisions if _in(d, d0, d1))

    weeks = defaultdict(lambda: [0, 0])
    for _sid, s, day in edited:
        w = weeks[_iso_monday(day)]
        w[0] += 1
        w[1] += 1 if s["last_verify_seq"] > s["last_write_seq"] else 0
    weekly = [{"start": k, "edited": v[0], "verified": v[1], "pct": _rate(v[1], v[0])}
              for k, v in sorted(weeks.items())]

    score = None
    if len(edited) >= 3:
        terms = [(0.40, len(verified) / len(edited)), (0.20, len(with_tests) / len(edited)),
                 (0.20, 1 - min(1.0, (reedit_pct or 0) / 100 / 0.5))]
        if first_run_pass is not None:
            terms.append((0.20, first_run_pass / 100))
        score = _rnd(100 * sum(w * v for w, v in terms) / sum(w for w, _ in terms))
    return {
        "score": score, "ready": len(edited) >= 3, "edited_sessions": len(edited),
        "verified_pct": _rate(len(verified), len(edited)), "verified": len(verified),
        "any_command_pct": _rate(len(any_cmd), len(edited)),
        "tests_alongside_pct": _rate(len(with_tests), len(edited)), "with_tests": len(with_tests),
        "runs": by_kind, "first_run_pass_pct": first_run_pass, "run_groups": len(all_groups),
        "reedit_pct": reedit_pct, "edits": len(edits), "hotspots": hot[:8],
        "errors_per_100_edits": _r1(100 * errors / n_writes) if n_writes else None,
        "tool_errors": errors, "interrupts_per_100_prompts": _r1(100 * intr / n_prompts) if n_prompts else None,
        "interruptions": intr, "safety": dict(safety),
        "permissions": [{"decision": k[0], "source": k[1], "n": v} for k, v in perms.most_common()],
        "weekly": weekly,
    }


def _is_top_tier(model: str) -> bool:
    return price_for(model)["in"] >= 10


def build_value(st: Stats, d0=None, d1=None) -> dict:
    days = sorted(d for d in st.days if _in(d, d0, d1))
    cost = sum(st.days[d]["cost"] for d in days)
    saved = sum(st.days[d]["cache_savings"] for d in days)
    loc = sum(st.days[d]["loc"] for d in days)
    rows = _sess_rows(st, d0, d1)
    sess_costs = [s["cost"] for _sid, s, _d in rows if s["cost"] > 0]
    edited = [r for r in rows if r[1]["last_write_seq"] >= 0]
    verified = [r for r in edited if r[1]["last_verify_seq"] > r[1]["last_write_seq"]]
    ver_cost = sum(r[1]["cost"] for r in verified)

    # cost spikes: days well above this window's own typical day
    day_costs = [st.days[d]["cost"] for d in days if st.days[d]["cost"] > 0]
    med = _median(day_costs) or 0
    spikes = []
    if len(day_costs) >= 5:
        for d in days:
            c = st.days[d]["cost"]
            if c >= max(2 * med, 0.5):
                top = max((r for r in rows if r[2] == d), key=lambda r: r[1]["cost"], default=None)
                spikes.append({"day": d, "cost": round(c, 2), "x_typical": round(c / med, 1) if med else None,
                               "session": (st.session_titles.get(top[0]) or "Untitled session") if top else None,
                               "session_cost": round(top[1]["cost"], 2) if top else None})
        spikes.sort(key=lambda x: -x["cost"])

    first = sum(s.get("first_call_cost", 0.0) for _sid, s, _d in rows)
    total_sess = sum(s.get("cost", 0.0) for _sid, s, _d in rows)
    nf = sum(max(0, s["api_calls"] - 1) for _sid, s, _d in rows)
    resets = sum(s["cache_resets"] for _sid, s, _d in rows if s["api_calls"] > 1)

    mcost = Counter()
    sess_by_model = Counter()
    top_tier_cost = top_tier_nocode = 0.0
    for _sid, s, _d in rows:
        for m, c in s["model_cost"].items():
            mcost[m] += c
        if s["model_cost"]:
            primary = s["model_cost"].most_common(1)[0][0]
            sess_by_model[primary] += 1
            if _is_top_tier(primary):
                top_tier_cost += s["cost"]
                if s["last_write_seq"] < 0:
                    top_tier_nocode += s["cost"]
    mtotal = sum(mcost.values())
    models = [{"model": m, "cost": round(c, 2), "share_pct": _rate(c, mtotal),
               "sessions": sess_by_model.get(m, 0)} for m, c in mcost.most_common(6)]

    weeks = defaultdict(lambda: [0.0, 0])
    for d in days:
        w = weeks[_iso_monday(d)]
        w[0] += st.days[d]["cost"]
        w[1] += st.days[d]["loc"]
    weekly = [{"start": k, "cost": round(v[0], 2), "loc": v[1],
               "per_100_loc": round(v[0] / v[1] * 100, 2) if v[1] else None}
              for k, v in sorted(weeks.items())]
    return {
        "ready": bool(days) and cost > 0, "cost": round(cost, 2), "loc": loc,
        "per_100_loc": round(cost / loc * 100, 2) if loc else None,
        "sessions": len(rows), "session_median_cost": round(_median(sess_costs), 2) if sess_costs else None,
        "verified_sessions": len(verified),
        "cost_per_verified": round(ver_cost / len(verified), 2) if verified else None,
        "cache_savings": round(saved, 2),
        "cache_saved_pct": _rnd(100 * saved / (cost + saved)) if (cost + saved) > 0 and saved > 0 else 0,
        "cold_start_pct": _r1(100 * first / total_sess) if total_sess else None,
        "cache_reset_pct": _r1(100 * resets / nf) if nf else None,
        "daily": [{"day": d, "cost": round(st.days[d]["cost"], 3),
                   "spike": any(sp["day"] == d for sp in spikes)} for d in days],
        "spikes": spikes[:5], "median_day_cost": round(med, 2) if med else None,
        "models": models,
        "top_tier_nocode_pct": _rate(top_tier_nocode, top_tier_cost) if top_tier_cost else None,
        "top_tier_cost": round(top_tier_cost, 2), "weekly": weekly,
    }


def _field_status(n, d):
    if not d:
        return "missing"
    r = n / d
    return "ok" if r >= 0.9 else ("partial" if n else "missing")


def build_health(st: Stats) -> dict:
    """What this machine's logs actually contain, so a metric can say 'not in your logs'
    instead of showing a misleading zero."""
    hc = st.hc
    perms = Counter(dec for _d, dec, _s in st.perm_decisions)
    caps = [
        ("tokens", "Tokens and cost", "tokens",
         _field_status(hc["assistant_msgs"], hc["assistant_msgs"]),
         "%d responses with token usage (%d duplicate log lines dropped)" % (hc["assistant_msgs"], hc["duplicate_lines"])),
        ("turns", "Turn timing", "flow",
         "ok" if len(st.turns) else "missing",
         "%d turns timed from your prompt to Claude finishing" % len(st.turns)),
        ("tooltime", "Tool timing", "flow",
         _field_status(hc["tool_results_matched"], hc["tool_results"]),
         "%d of %d tool results matched to their call" % (hc["tool_results_matched"], hc["tool_results"])),
        ("cmdout", "Command outcomes", "quality",
         "ok" if hc["tool_results"] else "missing",
         "Pass/fail comes from the error flag; exit-code text seen on %d failed commands" % hc["exit_code_text"]),
        ("perms", "Permission decisions", "quality",
         "ok" if hc["permission_decisions"] else "missing",
         ("Seen: " + ", ".join("%s %d" % (k, v) for k, v in perms.most_common())) if perms
         else "None in these logs, so accept/reject rates are not shown"),
        ("thinking", "Thinking time", "flow",
         "ok" if hc["thinking"] else "missing", "%d responses report thinking time" % hc["thinking"]),
        ("effort", "Effort level", "flow",
         "ok" if hc["effort"] else "missing", "%d responses report an effort level" % hc["effort"]),
        ("subagents", "Subagent activity", "adoption",
         "ok" if (hc["sidechain_entries"] or sum(st.subagents.values())) else "missing",
         "%d subagent call%s, %d subagent log lines" % (sum(st.subagents.values()),
                                                       "" if sum(st.subagents.values()) == 1 else "s", hc["sidechain_entries"])
         if (hc["sidechain_entries"] or sum(st.subagents.values())) else "No subagent work seen yet"),
        ("hooks", "Hooks", "adoption",
         "ok" if hc["stop_hook_summary"] else "missing",
         "%d hook runs recorded" % hc["stop_hook_summary"] if hc["stop_hook_summary"] else "No hook runs seen"),
        ("time", "Timestamps", "all",
         _field_status(hc["entries_dated"], hc["entries"]),
         "%d of %d entries carry a timestamp" % (hc["entries_dated"], hc["entries"])),
    ]
    titled = sum(1 for sid in st.sessions if sid in st.session_titles)
    caps.append(("titles", "Session titles", "weekly",
                 _field_status(titled, len(st.sessions)),
                 "%d of %d sessions have an AI title" % (titled, len(st.sessions))))
    return {
        "capabilities": [{"id": i, "label": l, "tab": t, "status": s, "detail": d} for i, l, t, s, d in caps],
        "entry_types": [{"type": str(k), "n": v} for k, v in st.entry_types.most_common()],
        "versions": [{"version": k, "n": v} for k, v in st.versions.most_common(6)],
        "entrypoints": [{"name": k, "n": v} for k, v in st.entrypoints.most_common(6)],
        "parse_errors": st.parse_errors, "duplicate_lines": hc["duplicate_lines"],
        "sessions": len(st.sessions),
        "limits": [
            "Edits Claude makes through shell commands (sed, python scripts) are not counted as edits or lines of code.",
            "Turn and tool times include any time spent waiting for you to approve an action.",
            "The logs do not show which of Claude's output you kept, so there is no true acceptance rate unless permission decisions appear.",
            "A session is not a task: a long session can hold several tasks, and one task can span sessions.",
            "Cost is an estimate at public list prices; on a subscription plan your real marginal cost is $0.",
        ],
    }


COPILOT_SETUP_HINT = "Turn on with: python ai_fluency_insights.py --setup-copilot"
# the VS Code setting that writes the agent debug log (exact tokens and credits per call), confirmed on real logs
COPILOT_DEBUG_SETTING = "github.copilot.chat.agentDebugLog.fileLogging.enabled"
COPILOT_EXACT_HINT = (COPILOT_SETUP_HINT + " (OpenTelemetry), or switch on the VS Code setting " + COPILOT_DEBUG_SETTING
                      + ' (Settings > "Agent Debug Log > File Logging"). The debug log keeps your full prompts on this machine;'
                      " the OpenTelemetry export does not by default.")


def build_health_copilot(st: Stats, files: dict) -> dict:
    """build_health() for Copilot: what its logs on this machine contain, in the same shape, so a
    metric can say 'not in your logs' instead of showing a misleading zero. `st` is the Copilot
    Stats over full history and `files` the CopilotScanner.files() map of what was found."""
    hc = st.hc
    n = len(st.sessions)
    modes = getattr(st, "token_modes", Counter())
    surfaces = getattr(st, "surfaces", {})

    def sess_of(name):
        return surfaces.get(name, Counter())["sessions"]

    # sessions per store: a session's primary store is the CLI log, else the chat file, else the transcript
    cli_ids = set(files.get("cli") or ())
    chat_ids = set(files.get("chat") or ()) - cli_ids
    transcript_ids = set(files.get("transcripts") or ()) - cli_ids - chat_ids
    stores = ", ".join("%d %s" % (c, name) for c, name in (
        (len(chat_ids), "VS Code chat"), (len(transcript_ids), "transcript only"), (len(cli_ids), "Copilot CLI")) if c)
    if n:
        chat_detail = "%d session%s with activity" % (n, "" if n == 1 else "s") + ("; files found: " + stores if stores else "")
    else:
        chat_detail = "No Copilot session with activity found" + (" (files found: " + stores + ")" if stores else "")

    with_tokens = modes["exact"] + modes["approx"]
    exact_status = _field_status(modes["exact"], n)
    calls, no_credits = hc["assistant_msgs"], getattr(st, "no_credit_calls", 0)
    if not calls or no_credits >= calls:
        credits_status = "missing"
    else:
        credits_status = "ok" if not no_credits else "partial"
    if not calls:
        credits_detail = "No model calls with token counts yet, so there are no credits to read"
    elif credits_status == "missing":
        credits_detail = "None of the %d model calls carries credits, so credits are not available" % calls
    elif no_credits:
        credits_detail = "%d of %d model calls have no credits in the logs, so credits are understated" % (no_credits, calls)
    else:
        credits_detail = "Every one of %d model calls has its credits or is billed as a premium request" % calls

    chat_reqs, with_state = getattr(st, "chat_requests", 0), getattr(st, "chat_states", 0)
    if not with_state:
        stopped_status = "missing"
        stopped_detail = ("No chat request records whether it was stopped, so stopped requests are not available "
                          "(older VS Code)" if chat_reqs else
                          "No chat request with a timestamp found, so stopped requests are not available")
    elif with_state >= chat_reqs:
        stopped_status = "ok"
        stopped_detail = "All %d chat requests record whether they were stopped" % chat_reqs
    else:
        stopped_status = "partial"
        stopped_detail = ("%d of %d chat requests record whether they were stopped (the rest are from an older VS Code)"
                          % (with_state, chat_reqs))

    mapped = sum(1 for s in st.sessions.values() if s.get("project") not in (None, "", "(no folder)"))
    branch_msgs = sum(b["msgs"] for b in st.branches.values())
    with_branch = branch_msgs - st.branches.get("(none)", {}).get("msgs", 0)
    titled = sum(1 for sid in st.sessions if sid in st.session_titles)
    caps = [
        ("chat", "Chat history", "all", "ok" if n else "missing", chat_detail),
        ("tokens", "Token counts", "tokens", _field_status(with_tokens, n),
         "%d of %d sessions have token counts (%d exact, %d approx.)" % (with_tokens, n, modes["exact"], modes["approx"])
         + ("; %d without any (a transcript carries no tokens)" % (n - with_tokens) if n > with_tokens else "")),
        ("exact", "Exact tokens", "tokens", exact_status,
         "%d of %d sessions have exact token counts (Copilot CLI, OpenTelemetry or debug logs)" % (modes["exact"], n)
         + ("" if exact_status == "ok" else ". " + COPILOT_EXACT_HINT)),
        ("credits", "Credits", "value", credits_status, credits_detail),
        ("stopped", "Stopped requests", "quality", stopped_status, stopped_detail),
        ("cli", "Copilot CLI", "all", "ok" if sess_of("cli") else "missing",
         "%d Copilot CLI session%s (its usage records are exact)" % (sess_of("cli"), "" if sess_of("cli") == 1 else "s")
         if sess_of("cli") else "No Copilot CLI sessions found; the CLI needs no setup"),
        ("projects", "Project folders", "projects", _field_status(mapped, n),
         "%d of %d sessions map to a project folder" % (mapped, n)),
        ("branches", "Git branches", "projects", _field_status(with_branch, branch_msgs),
         "%d of %d model responses carry a git branch (from the Copilot CLI or the session store)" % (with_branch, branch_msgs)),
        ("titles", "Session titles", "weekly", _field_status(titled, n),
         "%d of %d sessions have a title (Copilot's own, the session store's, or the first prompt)" % (titled, n)),
        ("time", "Timestamps", "all", _field_status(hc["entries_dated"], hc["entries"]),
         "%d of %d entries carry a timestamp" % (hc["entries_dated"], hc["entries"])),
    ]
    return {
        "capabilities": [{"id": i, "label": l, "tab": t, "status": s, "detail": d} for i, l, t, s, d in caps],
        "entry_types": [{"type": str(k), "n": v} for k, v in st.entry_types.most_common()],
        "versions": [{"version": k, "n": v} for k, v in getattr(st, "copilot_versions", Counter()).most_common(6)],
        "entrypoints": [{"name": name, "n": c} for name, c in (("VS Code", sess_of("vscode")), ("Copilot CLI", sess_of("cli"))) if c],
        "unknown_types": [{"type": str(k), "n": v} for k, v in getattr(st, "unknown_types", Counter()).most_common()],
        "bad_lines": getattr(st, "bad_lines", 0),
        "parse_errors": st.parse_errors, "duplicate_lines": hc["duplicate_lines"],
        "sessions": n,
        "limits": [
            "Inline code completions (the grey suggestions as you type) are not logged, so they are not counted.",
            "Without exact tokens, an agent turn may record only its last model call, so approximate token counts can run low.",
            "Credits are as the logs report them; where a request has none, nothing is guessed from model prices.",
            "A session is not a task: a long session can hold several tasks, and one task can span sessions.",
        ],
    }


def copilot_na(health: dict) -> dict:
    """KPI key -> why Copilot cannot give it, for the cards that show 'Not available for Copilot'.
    `health` is build_health_copilot(). Two gaps are always there; interruptions join them unless the
    logs record stopped requests (the "stopped" row is not missing); the cache hit rate joins them
    while exact tokens are off, because chat history has no cache split."""
    caps = health.get("capabilities", [])
    na = {
        "cold_start_pct": "Copilot bills per request, so there is no cache set-up cost to measure.",
        "permission_modes": "Copilot doesn't log approval modes.",
    }
    if not any(c.get("id") == "stopped" and c.get("status") in ("ok", "partial") for c in caps):
        na["interruptions"] = "Your Copilot logs don't record stopped requests (older VS Code)."
    if any(c.get("id") == "exact" and c.get("status") == "missing" for c in caps):
        na["cache_read"] = ("Copilot's chat history has no cache split; turn on exact tokens (--setup-copilot, "
                            "or the VS Code setting " + COPILOT_DEBUG_SETTING + ").")
    return na


COPILOT_NO_SPLIT_IN_RANGE = ("No session in this range has a cache split: only exact token counts (Copilot CLI, "
                             "OpenTelemetry or debug logs) have one, and chat history's approx. counts do not.")


def copilot_cache_rate(data, st):
    """Copilot's cache hit rate (design 5.1): cache read / (cache read + input) over the sessions that have a cache
    split only (`st.split_days`, the range's exact sessions), so approx. sessions do not dilute it. With no such
    session in the range the rate is None and data["na"]["cache_read"] says why: never a made-up 0%."""
    split = getattr(st, "split_days", {})
    inp = sum(v["input"] for v in split.values())
    read = sum(v["cache_read"] for v in split.values())
    if inp + read:
        data["totals"]["cache_efficiency"] = round(read / (read + inp) * 100, 1)
        data["na"].pop("cache_read", None)
    else:
        data["totals"]["cache_efficiency"] = None
        data["na"].setdefault("cache_read", COPILOT_NO_SPLIT_IN_RANGE)


# (id, tab, name, confidence, how it is computed, main limitation)
METRIC_CATALOG = [
    ("turn_time", "flow", "Turn time", "derived",
     "Time from your prompt to Claude finishing its answer (stop reason end_turn); median and 90th percentile.",
     "Includes any time spent waiting for you to approve an action."),
    ("clean_turns", "flow", "Clean first-pass turns", "derived",
     "Turns that finished with no tool error, no interruption, and no correction from your next prompt, divided by all turns.",
     "A correction is detected from wording at the start of your next prompt, so it can miss some."),
    ("recovery", "flow", "Failure recovery", "derived",
     "Of the times a tool failed, the share where the same tool later succeeded in that session.",
     "A different fix that does not retry the same tool counts as unrecovered."),
    ("stuck", "flow", "Stuck loops", "derived",
     "Failure episodes where the same tool failed three or more times in a row.", "Three failures is a convention, not a law."),
    ("first_edit", "flow", "Time to first edit", "derived",
     "Seconds from a session's first log entry to Claude's first edit, median over sessions that edited.",
     "Edits made through shell commands are not seen."),
    ("tool_time", "flow", "Where tool time goes", "observed",
     "Time between each tool call and its result, grouped by tool or command.",
     "Excludes the model's own thinking and generation time."),
    ("session_len", "flow", "Session length", "derived",
     "Last entry minus first entry in a session (median, 90th percentile).", "Includes idle time."),
    ("thinking", "flow", "Thinking time", "observed",
     "thinkingDurationMs reported on Claude's responses.", "Only present when the logs include it."),
    ("flow_score", "flow", "Flow score", "derived",
     "50% clean-turn rate + 30% failure-recovery rate + 20% (1 - stuck share); needs 5+ turns.",
     "A heuristic blend; the trend matters more than the number."),
    ("verified", "quality", "Verified after last edit", "proxy",
     "Sessions where a test, build or lint command ran after Claude's last edit, divided by sessions where Claude edited.",
     "Running a check is not the same as the check passing."),
    ("first_run", "quality", "First-run pass rate", "derived",
     "For each session and kind of check (test, build, lint), whether the first run passed.",
     "Pass/fail comes from the command's error flag."),
    ("tests_alongside", "quality", "Tests alongside code", "proxy",
     "Sessions that edited a test file, divided by sessions where Claude edited.",
     "Detected from file names such as test_*, *.spec.*, tests/."),
    ("reedit", "quality", "Re-edit share", "proxy",
     "Edits to a file already edited earlier in the same session, divided by all edits.",
     "Normal step-by-step work also re-edits files."),
    ("hotspots", "quality", "Rework hotspots", "proxy",
     "Files edited 5+ times that were edited 3+ times in one session or on 3+ days.", "Some files are simply central."),
    ("friction", "quality", "Errors and interruptions", "observed",
     "Failed tool results per 100 edits, and interruptions per 100 prompts.", "A failed command is often a normal step."),
    ("safety", "quality", "Safety signals", "proxy",
     "Counts of risky commands (force-push, hard reset, recursive delete, pipe-to-shell, skipped hooks) and touches of secret-like files.",
     "Pattern matching on command text; only category counts are stored."),
    ("perms", "quality", "Permission decisions", "observed",
     "The decision and source recorded with each tool call, when the logs include it.", "Not present in every setup."),
    ("quality_score", "quality", "Quality score", "derived",
     "40% verified + 20% tests alongside + 20% low re-edits + 20% first-run pass (when checks ran); needs 3+ edited sessions.",
     "A heuristic blend; the trend matters more than the number."),
    ("cost", "value", "Estimated cost", "estimated",
     "Token usage priced at the PRICING table in the script.", "List prices; $0 real marginal cost on a subscription."),
    ("cost_100_loc", "value", "Cost per 100 lines", "estimated",
     "Estimated cost divided by lines Claude wrote, times 100.", "Lines of code are additions only."),
    ("cost_verified", "value", "Cost per verified session", "estimated",
     "Estimated cost of sessions that edited and then ran a check, divided by their count.", "Sessions are not tasks."),
    ("cache_savings", "value", "Cache savings", "estimated",
     "Cache reads billed at 10% instead of 100%, minus the 25% premium for cache writes, at list prices.",
     "Assumes you would otherwise have sent the same tokens uncached."),
    ("cold_start", "value", "Cold-start tax", "estimated",
     "Cost of each session's first call as a share of all session cost.", "See METRICS.md."),
    ("cost_spikes", "value", "Cost spikes", "derived",
     "Days costing 2x or more your typical day in the window.", "Needs five or more active days."),
    ("model_mix", "value", "Model mix", "derived",
     "Each model's share of estimated cost, by session.", "A session's primary model is the one that cost the most."),
    ("top_tier_nocode", "value", "Top-tier model on no-edit sessions", "proxy",
     "Share of top-tier-model cost spent in sessions where Claude made no edits.",
     "Some thinking-heavy sessions legitimately need a top-tier model."),
    ("adoption_score", "adoption", "Adoption Score", "derived",
     "25% Habit + 25% Range + 30% Craft + 20% Impact.", "A heuristic blend with hand-picked weights."),
]
METRIC_CATALOG_BY_ID = {m[0]: m for m in METRIC_CATALOG}


def catalog_payload():
    return {m[0]: {"tab": m[1], "name": m[2], "confidence": m[3], "how": m[4], "limit": m[5]}
            for m in METRIC_CATALOG}


# Copilot's money is AI credits read from its logs, not a price worked out from a rate table, so the
# cost entries above (Claude's dollar estimate at list prices) are the wrong words and the wrong
# confidence for it. These replace them in the Copilot payload's catalog; "na" marks a metric
# Copilot cannot give, with the reason in `limit`. Claude's catalog is untouched.
COPILOT_CATALOG = {
    "cost": {"name": "Credits used", "confidence": "observed",
             "how": "AI credits as Copilot's logs report them, summed over the range. Nothing is priced from model rates.",
             "limit": "A call with no credits in the logs adds nothing, so the total can read low. Premium requests are counted apart."},
    "cost_100_loc": {"name": "Credits per 100 lines", "confidence": "derived",
                     "how": "Credits used divided by lines Copilot wrote, times 100.",
                     "limit": "Lines of code are additions only."},
    "cost_verified": {"name": "Credits per verified session", "confidence": "derived",
                      "how": "Credits of sessions that edited and then ran a check, divided by their count.",
                      "limit": "Sessions are not tasks."},
    "model_mix": {"confidence": "derived",
                  "how": "Each model's share of credits, by session.",
                  "limit": "A session's primary model is the one that used the most credits."},
    "cache_savings": {"confidence": "na",
                      "how": "Not available for Copilot: it bills per request, so there is no cache price to save against.",
                      "limit": "Cache tokens may be in the logs, but nothing turns them into credits, so no figure is shown."},
    "cold_start": {"confidence": "na",
                   "how": "Not available for Copilot: it bills per request, so there is no cache set-up cost to measure.",
                   "limit": "Same reason as cache savings; the dashboard shows no figure rather than a zero."},
}


def catalog_for(source: str, tool: dict) -> dict:
    """The metric catalog the page gets for `source`: the shared text with the tool's names put in
    (retool_text) and, for Copilot, its credit-based entries (COPILOT_CATALOG)."""
    cat = retool_text(catalog_payload(), tool)
    if source == "copilot":
        for mid, over in COPILOT_CATALOG.items():
            cat[mid].update(over)
    return cat


def retool_text(obj, tool: dict):
    """A deep copy of `obj` with Claude's names in every string swapped for `tool`'s (a SOURCES entry):
    CLAUDE.md -> its instructions file, Claude Code -> its label, Claude -> its short name. Dict keys and
    anything that is not a string are left as they are. The advisor and My Adoption texts are written for
    Claude Code; they pass each fixed template through this BEFORE session titles, file names or numbers
    are put into it, so quoted user data is never rewritten. A no-op for the Claude entry."""
    if isinstance(obj, str):
        return (obj.replace("CLAUDE.md", tool["instructions"][0])
                   .replace("Claude Code", tool["label"]).replace("Claude", tool["short"]))
    if isinstance(obj, dict):
        return {k: retool_text(v, tool) for k, v in obj.items()}
    if isinstance(obj, list):
        return [retool_text(v, tool) for v in obj]
    if isinstance(obj, tuple):
        return tuple(retool_text(v, tool) for v in obj)
    return obj


def build_advisor(st: Stats, adoption: dict, tool: dict = SOURCES["claude"]) -> dict:
    """Evidence-based next steps over the same 28-day window as My Adoption. Every
    recommendation quotes the numbers behind it; none of this uses AI or tokens.
    Recommendations whose id is in `tool["skip_recs"]` (the cache ones, for Copilot) are left out,
    money is written in the tool's cost unit, and every fixed sentence goes through T() (retool_text for
    this tool) before any session title, file name or number is put into it."""
    out = {"ready": False, "recs": [], "more": [], "wins": [], "metrics": {}}
    usd = tool.get("cost_unit", "usd") == "usd"

    def money(x):
        return "$%.2f" % x if usd else "%.2f credits" % x

    def T(template):
        return retool_text(template, tool)

    end_day = adoption.get("end")
    if not end_day:
        return out
    d1 = end_day
    d0 = (date.fromisoformat(end_day) - timedelta(days=ADOPTION["window_days"] - 1)).isoformat()
    fl, q, v = build_flow(st, d0, d1), build_quality(st, d0, d1), build_value(st, d0, d1)
    out.update({"ready": True, "start": d0, "end": d1,
                "scores": {"flow": fl["score"], "quality": q["score"]}})
    recs, wins = [], []

    def add(rid, area, sev, title, evidence, action, conf, metric=None, better=None, label=None):
        if rid in tool.get("skip_recs", ()):
            return
        action = tool.get("rec_actions", {}).get(rid, action)       # advice that names a feature only Claude has
        rec = {"id": rid, "area": area, "severity": round(sev, 1), "title": title,
               "evidence": evidence, "action": action, "confidence": conf}
        if metric:
            rec["experiment"] = {"metric": metric, "better": better, "label": label}
        recs.append(rec)

    vp = q["verified_pct"]
    if q["edited_sessions"] >= 3 and vp is not None:
        if vp < 60:
            add("verify", "quality", (100 - vp) * 0.9, T("Make Claude check its own work"),
                T("A test, build or lint ran after Claude's last edit in %d%% of edit sessions (%d of %d).")
                % (vp, q["verified"], q["edited_sessions"]),
                T("End prompts with \"then run the tests\", or put your test command in CLAUDE.md so Claude runs it on its own."),
                "proxy", "verified_pct", "up", "Ask for a test run after every change, for your next 5 edit sessions")
        elif vp >= 80:
            wins.append("You check the work: %d%% of edit sessions ended with a test, build or lint." % vp)
    fp = q["first_run_pass_pct"]
    if fp is not None and q["run_groups"] >= 5:
        if fp < 60:
            add("firstrun", "quality", (100 - fp) * 0.8, "Your first test or build run often fails",
                "Only %d%% of first runs passed across %d session-and-check pairs." % (fp, q["run_groups"]),
                T("Before new work, have Claude run the suite and fix what is red first; paste the failing output into the prompt."),
                "derived", "first_run_pass_pct", "up", "Start each session by running the tests, for your next 5 sessions")
        elif fp >= 85:
            wins.append("Your tests and builds pass first time: %d%% of first runs." % fp)
    cp = fl["clean_pct"]
    if fl["turns"] >= 8 and cp is not None:
        if cp < 60:
            if tool.get("no_interruptions"):        # no stopped requests in the tool's logs: no "0 were interrupted"
                clean = ("%d%% of turns were clean first tries: %d needed a correction and %d hit tool errors (of %d turns)."
                         % (cp, fl["corrected"], fl["with_errors"], fl["turns"]))
            else:
                clean = ("%d%% of turns were clean first tries: %d needed a correction, %d were interrupted and %d hit tool errors (of %d turns)."
                         % (cp, fl["corrected"], fl["interrupted"], fl["with_errors"], fl["turns"]))
            add("clean", "flow", (100 - cp) * 0.8, "Sharper first prompts", clean,
                "State the goal, the files involved and what \"done\" looks like in the first prompt. For bigger changes, start in plan mode.",
                "derived", "clean_pct", "up", "Write the goal, files and a done-check in your first prompt, for 5 sessions")
        elif cp >= 75:
            wins.append("Most of your turns land first time: %d%% clean." % cp)
    if fl["stuck"] >= 2:
        top = fl["fail_tools"][0]["tool"] if fl["fail_tools"] else "a tool"
        add("stuck", "flow", min(90, 45 + 10 * fl["stuck"]), "Break out of failure loops sooner",
            "%d times the same tool failed 3 or more times in a row (most often %s)." % (fl["stuck"], top),
            T("After two failures, stop: restate the problem, change the approach, or switch to plan mode instead of letting Claude retry."),
            "derived", "stuck", "down", "After two failures, stop and change approach, for your next 5 sessions")
    if fl["episodes"] >= 5 and fl["recovery_pct"] is not None and fl["recovery_pct"] >= 85:
        wins.append("Failures get fixed: %d%% of failed tool calls were followed by a success." % fl["recovery_pct"])
    tools = [t for t in fl["tools"] if t["total_s"] >= 60]
    if tools and tools[0]["share_pct"] >= 35:
        t = tools[0]
        add("slowtool", "flow", 30 + t["share_pct"] * 0.4, "\u2018%s\u2019 dominates your tool time" % t["family"],
            "%s took %d%% of tool time: %ds over %d calls, slowest %ds." % (t["family"], t["share_pct"], t["total_s"], t["calls"], t["max_s"]),
            T("Run it less often, scope it to the files you changed, or ask Claude to run it once at the end instead of after every edit."),
            "observed")
    sp90 = fl["session_p90_min"]
    if sp90 and fl["sessions"] >= 5 and (sp90 >= 180 or (fl["session_median_min"] or 0) >= 90):
        add("longsess", "flow", min(70, 30 + sp90 / 6), "Start a fresh session per task",
            "Median session is %d min and the longest 10%% run %d min."
            % (fl["session_median_min"] or 0, sp90),
            "Use /clear between unrelated tasks, and /compact when one long task has to continue.",
            "derived")
    cs = v["cold_start_pct"]
    if cs is not None and cs >= 45 and v["sessions"] >= 8:
        add("coldstart", "value", min(70, cs * 0.7), "Many short sessions are paying the cache set-up cost",
            "%.0f%% of session cost went on each session's first call, across %d sessions." % (cs, v["sessions"]),
            "Batch related small tasks into one session rather than opening a new one for each.",
            "estimated", "cold_start_pct", "down", "Do related small tasks in one session, for a week")
    cr = v["cache_reset_pct"]
    if cr is not None and cr >= 8:
        add("cachereset", "value", min(65, cr * 3), "The cache is being rebuilt mid-session",
            "%.0f%% of follow-up calls rebuilt the cache instead of reusing it." % cr,
            "Avoid long pauses in the middle of a task, and avoid switching tools or models partway through.",
            "estimated", "cache_reset_pct", "down", "Finish tasks without long pauses, for a week")
    rp = q["reedit_pct"]
    if rp is not None and q["edits"] >= 30:
        if rp >= 35:
            hot = ('; most reworked: "%s"' % q["hotspots"][0]["file"]) if q["hotspots"] else ""
            add("rework", "quality", min(75, rp), "Files are being edited again and again",
                "%d%% of edits were to a file already edited in that session%s." % (rp, hot),
                T("Give Claude the full requirement and the check for \"done\" up front, so it makes one pass instead of many small ones."),
                "proxy", "reedit_pct", "down", T("Describe the full requirement before Claude edits, for 5 sessions"))
        elif rp <= 15:
            wins.append("You get things done in few passes: only %d%% of edits revisit a file." % rp)
    ta = q["tests_alongside_pct"]
    if q["edited_sessions"] >= 5 and ta is not None and ta < 40:
        add("tests", "quality", (100 - ta) * 0.5, "Ask for tests alongside code",
            T("Claude touched a test file in only %d%% of edit sessions.") % ta,
            "End prompts with \"and add or update the tests for this change\".",
            "proxy", "tests_alongside_pct", "up", "Ask for tests with every change, for 5 sessions")
    risky = {k: n for k, n in q["safety"].items() if k != "secret_file_touched"}
    if risky:
        n = sum(risky.values())
        add("risky", "quality", min(85, 40 + 10 * n), "Risky commands ran",
            "%d risky command%s: %s." % (n, "" if n == 1 else "s", ", ".join("%s x%d" % (k.replace("_", " "), c) for k, c in risky.items())),
            T("Add deny rules in your Claude Code permission settings for the ones you never want to run unattended."),
            "proxy")
    if q["safety"].get("secret_file_touched"):
        n = q["safety"]["secret_file_touched"]
        add("secrets", "quality", min(90, 55 + 10 * n), T("Claude touched secret-like files"),
            "%d read or edit%s of files like .env, keys or credentials." % (n, "" if n == 1 else "s"),
            "Deny reads of .env and key files in your permission settings, or keep secrets outside the project folder.",
            "proxy")
    if v["spikes"]:
        s = v["spikes"][0]
        add("spike", "value", min(70, 35 + (s["x_typical"] or 0) * 5), "Cost spiked on %s" % s["day"],
            "%s was %.1fx a typical day; biggest session: \"%s\" (%s)."
            % (money(s["cost"]), s["x_typical"] or 0, s["session"], money(s["session_cost"] or 0)),
            "Check that session for a runaway loop or an oversized context, and split big tasks.",
            "estimated")
    tn = v["top_tier_nocode_pct"]
    if tn is not None and tn >= 50 and v["top_tier_cost"] >= 5:
        add("model", "value", 35, "Top-tier model on sessions with no edits",
            T("%d%% of top-tier-model spend (%s) went to sessions where Claude changed nothing.") % (tn, money(v["top_tier_cost"])),
            "For questions and exploration try a lighter model, and keep the top tier for building.",
            "proxy")
    for c in adoption.get("coach", []):
        add("adopt_" + (c.get("feature") or c["area"]), c["area"] if c["area"] != "wellbeing" else "habit",
            min(80, c.get("severity", 40)), c["title"], c["why"], c["how"], "derived")
    if (adoption.get("habit") or {}).get("streak", 0) >= 5:
        wins.append("You're on a %d-day streak." % adoption["habit"]["streak"])
    if v["cache_saved_pct"] >= 20 and v["cache_savings"] >= 1:
        wins.append("Caching saved you about %d%% (about $%.2f at list prices)." % (v["cache_saved_pct"], v["cache_savings"]))
    if q["edits"] >= 20 and not q["safety"]:
        wins.append("No risky commands or secret-file touches in this window.")

    recs.sort(key=lambda r: -r["severity"])
    for i, r in enumerate(recs):
        r["rank"] = i + 1
    out["recs"], out["more"] = recs[:5], recs[5:12]
    out["wins"] = wins[:4]
    out["metrics"] = {
        "verified_pct": vp, "first_run_pass_pct": fp, "clean_pct": cp, "stuck": fl["stuck"],
        "reedit_pct": rp, "tests_alongside_pct": ta, "recovery_pct": fl["recovery_pct"],
        "cold_start_pct": cs, "cache_reset_pct": cr, "per_100_loc": v["per_100_loc"]}
    return out


# ---------------------------------------------------------------------------
# "My Adoption" -- one developer's adoption story. Four pillars (Habit, Range,
# Craft, Impact) are each scored 0-100 over a rolling window and blended into
# one Adoption Score. Pure functions over Stats; formulas in docs/METRICS.md.
# ---------------------------------------------------------------------------

def _rnd(x) -> int:
    """Round half up (matches the front end's Math.round, unlike banker's round)."""
    return int(x + 0.5)


def fluency_of(w) -> dict:
    """Python twin of the front end's fluencyOf(): the four 0-100 AI-fluency
    dimensions for one aggregate `w` (a week or an adoption window)."""
    sess = w["sessions"]
    code = [s for s in sess if s["loc"] > 0]
    prompts = w["prompts"]
    lev = w["output"] / w["human_tokens"] if w["human_tokens"] else 0
    code_share = len(code) / len(sess) if sess else 0
    delegation = _rnd(100 * (0.5 * code_share + 0.5 * min(1, lev / 50)))
    avg_w = w["prompt_words"] / prompts if prompts else 0
    pushback = w["corrections"] + w["interruptions"]
    one_shot = max(0, 1 - pushback / prompts) if prompts else 0
    if avg_w <= 0:
        sweet = 0
    elif avg_w < 8:
        sweet = avg_w / 8
    elif avg_w <= 150:
        sweet = 1
    else:
        sweet = max(0, 1 - (avg_w - 150) / 300)
    description = _rnd(100 * (0.65 * one_shot + 0.35 * sweet))
    engaged = min(1, pushback / max(1, prompts * 0.05))
    err_ctl = max(0, 1 - w["tool_errors"] / max(1, w["writes"] or 1))
    discernment = _rnd(100 * (0.5 * engaged + 0.5 * err_ctl))
    verified = (len([s for s in code if s["bash"] > 0]) / len(code)) if code else 0
    diligence = _rnd(100 * verified) if code else 0
    return {"delegation": delegation, "description": description,
            "discernment": discernment, "diligence": diligence,
            "lev": lev, "code_share": code_share, "avg_words": avg_w,
            "one_shot": one_shot, "engaged": engaged, "verified": verified,
            "ncode": len(code), "pushback": pushback}


FLUENCY_TIPS = {
    "delegation": "Try handing Claude whole tasks (build it and check it), not just questions.",
    "description": "Put the constraints, file paths and expected behavior in your first prompt.",
    "discernment": "Review diffs and say so when output misses. Pushing back is a good sign.",
    "diligence": "Ask Claude to run the tests or build after edits, so work gets verified.",
}


def _adoption_sessions(st: Stats):
    """Per-session rows with a local start day, for window slicing."""
    rows = []
    for sid, s in st.sessions.items():
        try:
            a = datetime.fromisoformat(s["start"].replace("Z", "+00:00")).astimezone()
            b = datetime.fromisoformat(s["end"].replace("Z", "+00:00")).astimezone()
        except Exception:
            continue
        title = st.session_titles.get(sid) or ""
        rows.append({
            "day": a.strftime("%Y-%m-%d"), "min": max(0.0, (b - a).total_seconds()) / 60,
            "loc": s["loc"], "reads": s["reads"], "writes": s["writes"],
            "bash": s["bash"], "prompts": s["prompts"], "project": s["project"],
            "type": classify_session(title, s["reads"], s["writes"], s["loc"]),
        })
    return rows


def _window_agg(st: Stats, sess_rows, start: date, end: date) -> dict:
    """Everything one adoption window needs, summed over [start, end]."""
    s0, s1 = start.isoformat(), end.isoformat()
    days = sorted(d for d in st.days if s0 <= d <= s1 and st.days[d]["messages"] > 0)
    w = {"start": s0, "end": s1, "days": days, "sessions": [],
         "active_days": len(days)}
    for k in ("input", "output", "loc", "prompts", "reads", "writes", "interruptions",
              "tool_errors", "human_tokens", "prompt_words", "corrections",
              "late_msgs", "messages"):
        w[k] = sum(st.days[d][k] for d in days)
    w["sessions"] = [r for r in sess_rows if s0 <= r["day"] <= s1]
    w["active_min"] = sum(len(st.day_buckets.get(d, ())) * 10 for d in days)
    w["code_days"] = sum(1 for d in days if st.days[d]["loc"] > 0)
    return w


def _has_instructions(cwd, instructions) -> bool:
    """True when the project folder `cwd` holds one of the `instructions` files (paths relative to it, "/"-separated)."""
    return bool(cwd) and any(os.path.isfile(os.path.join(cwd, *rel.split("/"))) for rel in instructions)


def _feature_status(st: Stats, w: dict, features=ADOPTION_FEATURES, instructions=("CLAUDE.md", "AGENTS.md")) -> dict:
    """feature id -> days used inside the window (memory is project-based)."""
    used = {}
    for fid, *_ in features:
        if fid == "memory":
            projs = {r["project"] for r in w["sessions"]}
            used[fid] = sum(1 for p in projs if _has_instructions(st.project_cwd.get(p), instructions))
        else:
            used[fid] = sum(1 for d in st.feature_days.get(fid, ())
                            if w["start"] <= d <= w["end"])
    return used


def _pillar_scores(st: Stats, w: dict, features=ADOPTION_FEATURES, instructions=("CLAUDE.md", "AGENTS.md")) -> dict:
    cfg = ADOPTION
    span = (date.fromisoformat(w["end"]) - date.fromisoformat(w["start"])).days + 1
    # Habit: how often, and how evenly across the weeks in the window
    target = cfg["target_days"] * span / cfg["window_days"]
    buckets = max(1, span // 7)
    start = date.fromisoformat(w["start"])
    covered = 0
    for i in range(buckets):
        lo = (start + timedelta(days=7 * i)).isoformat()
        hi = (start + timedelta(days=7 * i + 6)).isoformat()
        if sum(1 for d in w["days"] if lo <= d <= hi) >= 2:
            covered += 1
    habit = _rnd(100 * (0.6 * min(1, w["active_days"] / target) + 0.4 * covered / buckets))
    # Range: how much of Claude Code you actually use, and how deep sessions go
    feats = _feature_status(st, w, features, instructions)
    n_used = sum(1 for v in feats.values() if v > 0)
    sess = w["sessions"]
    substantial = [r for r in sess
                   if r["reads"] + r["writes"] + r["bash"] >= cfg["substantial_actions"]]
    sub_share = len(substantial) / len(sess) if sess else 0
    range_ = _rnd(100 * (0.75 * min(1, n_used / cfg["features_full"]) + 0.25 * sub_share))
    # Craft: the four AI-fluency dimensions, averaged, blended with how smoothly work
    # flows and how well it is checked (the Flow and Quality tabs) when those have data
    fl = fluency_of(w)
    craft_base = _rnd((fl["delegation"] + fl["description"] + fl["discernment"] + fl["diligence"]) / 4)
    flow_d, qual_d = build_flow(st, w["start"], w["end"]), build_quality(st, w["start"], w["end"])
    parts = [(0.6, craft_base)]
    if flow_d["score"] is not None:
        parts.append((0.2, flow_d["score"]))
    if qual_d["score"] is not None:
        parts.append((0.2, qual_d["score"]))
    craft = _rnd(sum(a * b for a, b in parts) / sum(a for a, _ in parts))
    # Impact: code Claude wrote for you, and how many of your days it did so
    per_day = w["loc"] / w["active_days"] if w["active_days"] else 0
    code_day_share = w["code_days"] / w["active_days"] if w["active_days"] else 0
    impact = _rnd(100 * (0.5 * min(1, per_day / cfg["loc_target"]) + 0.5 * code_day_share))
    wt = cfg["weights"]
    overall = _rnd(wt["habit"] * habit + wt["range"] * range_ + wt["craft"] * craft
                   + wt["impact"] * impact)
    return {"habit": habit, "range": range_, "craft": craft, "impact": impact,
            "overall": overall, "feats": feats, "n_used": n_used, "covered": covered,
            "buckets": buckets, "sub_share": sub_share, "substantial": len(substantial),
            "per_day": per_day, "code_day_share": code_day_share, "fluency": fl,
            "craft_base": craft_base, "flow": flow_d["score"], "quality": qual_d["score"],
            "flow_d": flow_d, "quality_d": qual_d}


def adoption_level(score: int, pillars=None, history=None, tool: dict = SOURCES["claude"]) -> dict:
    """Level for a score. Pro and Pro Max also need every pillar above a floor, and
    Pro Max a sustained run of high weekly scores. Without pillars/history only the
    score is checked (used for quick lookups). The level's description names `tool`."""
    top = 0
    for i, (floor, _n, _d) in enumerate(ADOPTION_LEVELS):
        if score >= floor:
            top = i
    idx, blocker = top, None
    weakest = min(pillars, key=lambda k: pillars[k]) if pillars else None
    while idx > 0:
        name = ADOPTION_LEVELS[idx][1]
        gate = LEVEL_GATES.get(name)
        if not gate or pillars is None:
            break
        problem = None
        if min(pillars.values()) < gate["min_pillar"]:
            problem = "Lift %s (now %d) to %d to unlock %s." % (
                weakest.capitalize(), pillars[weakest], gate["min_pillar"], name)
        elif "sustain_weeks" in gate:
            recent = [h["score"] for h in (history or [])][-gate["sustain_weeks"]:]
            if len(recent) < gate["sustain_weeks"] or min(recent) < gate["sustain_score"]:
                problem = "Hold %d+ for %d weeks in a row to unlock %s." % (
                    gate["sustain_score"], gate["sustain_weeks"], name)
        if not problem:
            break
        if blocker is None:
            blocker = problem
        idx -= 1
    floor, name, desc = ADOPTION_LEVELS[idx]
    nxt = ADOPTION_LEVELS[idx + 1] if idx + 1 < len(ADOPTION_LEVELS) else None
    return {"index": idx, "name": name, "desc": retool_text(desc, tool), "floor": floor,
            "next_name": nxt[1] if nxt else None,
            "next_floor": nxt[0] if nxt else None,
            "to_next": max(0, nxt[0] - score) if nxt else 0,
            "blocker": blocker,
            "all": [{"floor": f, "name": n} for f, n, _ in ADOPTION_LEVELS]}


def _feature_name(label: str) -> str:
    """A feature's label as it reads inside a sentence: lower case, without a trailing "(...)" detail."""
    return label.split(" (")[0].lower()


def _coach(st: Stats, cur: dict, w: dict, wellbeing: dict, ever: dict, features=ADOPTION_FEATURES,
           tool: dict = SOURCES["claude"]) -> list:
    """Up to three concrete next steps, ranked by how much they should help. Their fixed wording
    names `tool` (retool_text on each template, before any number is put into it)."""
    def T(template):
        return retool_text(template, tool)
    cands = []
    for fid, label, _desc, tip, weight in features:
        if cur["feats"][fid] > 0:
            continue
        before = ever.get(fid, 0) > 0
        name = _feature_name(label)
        cands.append({
            "area": "range", "feature": fid, "severity": weight + (0 if before else 10),
            "title": "Try %s" % name if fid != "memory" else T("Add a CLAUDE.md"),
            "why": ("You've used %s before, but not in the last %d days."
                    % (name, ADOPTION["window_days"]) if before
                    else "You haven't tried %s yet." % name),
            "how": tip})
    if cur["habit"] < 70:
        cands.append({
            "area": "habit", "feature": None, "severity": (100 - cur["habit"]) * 0.7,
            "title": "Make it a steady habit",
            "why": "You were active on %d of the last %d days."
                   % (w["active_days"], ADOPTION["window_days"]),
            "how": T("Pick one small task a day (a bug, a test, a commit message) and give it to Claude.")})
    fl = cur["fluency"]
    dims = [("delegation", "Delegate bigger tasks"), ("description", "Write sharper first prompts"),
            ("discernment", "Review and steer more"), ("diligence", "Verify the work")]
    weakest = min(dims, key=lambda d: fl[d[0]])
    if fl[weakest[0]] < 70:
        cands.append({
            "area": "craft", "feature": None, "severity": (100 - fl[weakest[0]]) * 0.75,
            "title": weakest[1],
            "why": "%s is your lowest craft score (%d/100)." % (weakest[0].capitalize(), fl[weakest[0]]),
            "how": T(FLUENCY_TIPS[weakest[0]])})
    if cur["impact"] < 50 and w["active_days"] >= ADOPTION["min_active_days"]:
        cands.append({
            "area": "impact", "feature": None, "severity": (100 - cur["impact"]) * 0.45,
            "title": T("Aim Claude at real changes"),
            "why": T("Claude wrote code on %d%% of your active days.") % _rnd(100 * cur["code_day_share"]),
            "how": T("Start the day by handing Claude one concrete change (a fix, a refactor, a test) to ship.")})
    if wellbeing["late_pct"] > 20:
        cands.append({
            "area": "wellbeing", "feature": None,
            "severity": min(90, 40 + wellbeing["late_pct"]),   # outranks "try a feature" tips
            "title": "Protect your evenings",
            "why": "%d%% of your activity happened between 11pm and 6am." % wellbeing["late_pct"],
            "how": T("Claude will still be there tomorrow. Try a firm stop time for a week.")})
    cands.sort(key=lambda c: -c["severity"])
    picked, feats = [], 0
    for c in cands:
        if c["feature"]:
            if feats >= 2:
                continue
            feats += 1
        picked.append(c)
        if len(picked) == 3:
            break
    return picked


def _highlights(cur, prev, w, pw, feature_rows, alldays, level) -> list:
    """Up to four plain-language lines about what moved since the previous window."""
    out = []
    names = {"habit": "Habit", "range": "Range", "craft": "Craft", "impact": "Impact"}
    if prev:
        moves = sorted(((cur[k] - prev[k], k) for k in names), key=lambda x: -abs(x[0]))
        for delta, k in moves[:2]:
            if abs(delta) < 5:
                continue
            extra = ""
            if k == "habit":
                extra = " (active on %d days, was %d)" % (w["active_days"], pw["active_days"])
            elif k == "range":
                extra = " (%d features in use, was %d)" % (cur["n_used"], prev["n_used"])
            elif k == "impact":
                extra = " (%s lines, was %s)" % (w["loc"], pw["loc"])
            out.append({"kind": "up" if delta > 0 else "down",
                        "text": "%s %s %d points%s" % (names[k], "rose" if delta > 0 else "fell", abs(delta), extra)})
        prev_level = adoption_level(prev["overall"])
        if prev_level["index"] != level["index"]:
            out.append({"kind": "level", "text": "Level change: %s \u2192 %s" % (prev_level["name"], level["name"])})
    for f in feature_rows:
        if f["first"] and alldays and f["first"] > alldays[0] and w["start"] <= f["first"] <= w["end"]:
            out.append({"kind": "new", "text": "First time using %s (%s)" % (_feature_name(f["label"]), f["first"])})
    if not out:
        out.append({"kind": "flat", "text": "A steady stretch: no pillar moved by 5 points or more."})
    return out[:4]


def _funnel(st: Stats, alldays, cur, w, tool: dict = SOURCES["claude"]) -> list:
    """Four stages of personal adoption, each with the evidence for it (worded for `tool`)."""
    per_week = Counter(_iso_monday(d) for d in alldays)
    m, last = date.fromisoformat(_iso_monday(alldays[0])), date.fromisoformat(_iso_monday(alldays[-1]))
    run, rec_date = 0, None
    while m <= last:
        run = run + 1 if per_week.get(m.isoformat(), 0) >= 2 else 0
        if run >= 3 and rec_date is None:
            rec_date = (m + timedelta(days=6)).isoformat()
        m += timedelta(days=7)
    qd = cur["quality_d"]
    outcome = qd["edited_sessions"] >= 3 and (qd["verified_pct"] or 0) >= 50
    return [
        {"id": "started", "label": "Started", "achieved": True,
         "detail": "First active day: %s" % alldays[0]},
        {"id": "recurring", "label": "Recurring", "achieved": rec_date is not None,
         "detail": ("2+ active days a week for 3 weeks in a row (reached %s)" % rec_date) if rec_date
         else "Reach 2+ active days a week for 3 weeks in a row"},
        {"id": "integrated", "label": "Integrated", "achieved": cur["n_used"] >= 4,
         "detail": "%d features used in the last %d days (need 4)" % (cur["n_used"], ADOPTION["window_days"])},
        {"id": "outcome", "label": "Verified outcomes", "achieved": outcome,
         "detail": ("%d%% of edit sessions ended with a test, build or lint (need 50%%)" % qd["verified_pct"])
         if qd["edited_sessions"] >= 3 else retool_text("Needs 3+ sessions where Claude edited files", tool)},
    ]


def build_adoption(st: Stats, features=ADOPTION_FEATURES, instructions=("CLAUDE.md", "AGENTS.md"),
                   tool: dict = SOURCES["claude"]) -> dict:
    """Payload for the My Adoption tab. Always full history (like the weekly report).
    `features` is the tool's feature list (ADOPTION_FEATURES or COPILOT_ADOPTION_FEATURES),
    `instructions` the project files that count as its "memory" (SOURCES[...]["instructions"]) and
    `tool` its SOURCES entry, which the fixed wording (coach, levels, pillars, tips) is written for."""
    alldays = [d for d in sorted(st.days) if st.days[d]["messages"] > 0]
    cfg = ADOPTION
    out = {"ready": False, "min_days": cfg["min_active_days"], "window_days": cfg["window_days"],
           "active_days": 0, "targets": {k: cfg[k] for k in
                                         ("target_days", "features_full", "substantial_actions", "loc_target")},
           "weights": cfg["weights"]}
    if not alldays:
        return out
    try:
        end = date.fromisoformat(alldays[-1])
    except ValueError:
        return out
    span = cfg["window_days"]
    rows = _adoption_sessions(st)

    def window(end_d):
        return _window_agg(st, rows, end_d - timedelta(days=span - 1), end_d)

    w = window(end)
    out["active_days"] = w["active_days"]
    out["end"] = end.isoformat()
    out["start"] = w["start"]
    if w["active_days"] < cfg["min_active_days"]:
        return out
    cur = _pillar_scores(st, w, features, instructions)
    pw = window(end - timedelta(days=span))
    prev = _pillar_scores(st, pw, features, instructions) if pw["active_days"] >= cfg["min_active_days"] else None

    # --- history: the score as of each of the last 12 week-ends --------------
    history = []
    for i in range(11, -1, -1):
        e = end - timedelta(days=7 * i)
        hw = window(e)
        if hw["active_days"] >= cfg["min_active_days"]:
            history.append({"end": e.isoformat(), "score": _pillar_scores(st, hw, features, instructions)["overall"]})

    # --- wellbeing -----------------------------------------------------------
    wk_msgs = sum(st.days[d]["messages"] for d in w["days"]
                  if date.fromisoformat(d).weekday() >= 5)
    wellbeing = {
        "late_pct": _rnd(100 * w["late_msgs"] / w["messages"]) if w["messages"] else 0,
        "weekend_pct": _rnd(100 * wk_msgs / w["messages"]) if w["messages"] else 0,
        "avg_active_min": _rnd(w["active_min"] / w["active_days"]) if w["active_days"] else 0,
        "longest_focus_min": max((longest_focus(st.day_buckets.get(d, ())) for d in w["days"]), default=0),
    }

    # --- range: feature checklist -------------------------------------------
    ever = {}
    for fid, *_ in features:
        ever[fid] = len(st.feature_days.get(fid, ()))
    ever["memory"] = 1 if cur["feats"]["memory"] > 0 else 0
    feature_rows = []
    for fid, label, desc, tip, _wt in features:
        fd = st.feature_days.get(fid, ())
        feature_rows.append({
            "id": fid, "label": label, "desc": desc, "tip": tip,
            "recent": cur["feats"][fid], "ever": ever[fid] > 0,
            "first": min(fd) if fd else None})

    # --- craft ---------------------------------------------------------------
    fl, pfl = cur["fluency"], (prev["fluency"] if prev else None)
    dims = [
        ("delegation", "Delegation",
         "%d%% of sessions shipped code; %.1f× leverage" % (_rnd(100 * fl["code_share"]), fl["lev"])),
        ("description", "Description",
         "%d%% of prompts needed no correction; %d words on average"
         % (_rnd(100 * fl["one_shot"]), _rnd(fl["avg_words"]))),
        ("discernment", "Discernment",
         ("%d corrections; %d tool errors" if tool.get("no_interruptions") else "%d corrections and interruptions; %d tool errors")
         % (fl["pushback"], w["tool_errors"])),
        ("diligence", "Diligence",
         "%d%% of %d code session%s ran commands afterwards"
         % (_rnd(100 * fl["verified"]), fl["ncode"], "" if fl["ncode"] == 1 else "s")),
    ]
    craft = [{"id": k, "label": lab, "score": fl[k], "prev": pfl[k] if pfl else None,
              "detail": det, "tip": retool_text(FLUENCY_TIPS[k], tool)} for k, lab, det in dims]
    fd, qd = cur["flow_d"], cur["quality_d"]
    if fd["score"] is not None:
        craft.append({"id": "flow", "label": "Flow", "score": fd["score"],
                      "prev": prev["flow"] if prev else None, "link": "flow",
                      "detail": "%s%% of turns were clean first tries; %d stuck loop%s"
                                % (fd["clean_pct"], fd["stuck"], "" if fd["stuck"] == 1 else "s"),
                      "tip": "Open the Flow tab to see what slows you down."})
    if qd["score"] is not None:
        craft.append({"id": "quality", "label": "Quality", "score": qd["score"],
                      "prev": prev["quality"] if prev else None, "link": "quality",
                      "detail": "%s%% of edit sessions verified; %s%% of edits revisit a file"
                                % (qd["verified_pct"], qd["reedit_pct"] if qd["reedit_pct"] is not None else 0),
                      "tip": "Open the Quality tab to see how your work gets checked."})

    # --- impact --------------------------------------------------------------
    mix = Counter(r["type"] for r in w["sessions"])
    impact = {
        "loc": w["loc"], "per_day": _rnd(cur["per_day"]),
        "code_day_pct": _rnd(100 * cur["code_day_share"]),
        "leverage": round(fl["lev"], 1), "sessions": len(w["sessions"]),
        "verified_pct": _rnd(100 * fl["verified"]), "task_mix": dict(mix),
        "prev_loc": pw["loc"] if prev else None,
    }

    # --- habit ---------------------------------------------------------------
    streak, best = active_streak(alldays)
    habit = {"active_days": w["active_days"], "weeks_covered": cur["covered"],
             "weeks": cur["buckets"], "streak": streak, "best_streak": best,
             "all_time_days": len(alldays),
             "prev_active_days": pw["active_days"] if prev else None}

    # --- weekly series (last 12 Mon-Sun weeks) + calendar --------------------
    monday = end - timedelta(days=end.weekday())
    weekly = []
    for i in range(11, -1, -1):
        m = monday - timedelta(days=7 * i)
        ww = _window_agg(st, rows, m, m + timedelta(days=6))
        weekly.append({"start": m.isoformat(), "active_days": ww["active_days"],
                       "loc": ww["loc"], "sessions": len(ww["sessions"])})
    cal_start = monday - timedelta(days=7 * 16)
    cells = []
    for i in range(17 * 7):
        d = cal_start + timedelta(days=i)
        if d > end:
            cells.append(-1)
        else:
            cells.append(len(st.day_buckets.get(d.isoformat(), ())) * 10)

    level = adoption_level(cur["overall"], {k: cur[k] for k in ("habit", "range", "craft", "impact")},
                           history, tool)
    highlights = _highlights(cur, prev, w, pw, feature_rows, alldays, level)
    funnel = _funnel(st, alldays, cur, w, tool)
    pillars = []
    for pid, label, blurb in (
            ("habit", "Habit", "How regularly you show up"),
            ("range", "Range", "How much of Claude Code you use"),
            ("craft", "Craft", "How well you work with Claude"),
            ("impact", "Impact", "What Claude gets done for you")):
        pillars.append({"id": pid, "label": label, "blurb": retool_text(blurb, tool), "score": cur[pid],
                        "prev": prev[pid] if prev else None})
    best_p = max(pillars, key=lambda p: p["score"])
    low_p = min(pillars, key=lambda p: p["score"])
    out.update({
        "ready": True, "score": cur["overall"],
        "prev_score": prev["overall"] if prev else None,
        "level": level, "pillars": pillars,
        "strength": best_p["id"], "growth": low_p["id"],
        "history": history, "habit": habit,
        "range": {"features": feature_rows, "used": cur["n_used"],
                  "total": len(features), "substantial_pct": _rnd(100 * cur["sub_share"]),
                  "sessions": len(w["sessions"])},
        "craft": craft, "impact": impact, "wellbeing": wellbeing,
        "weekly": weekly, "calendar": {"start": cal_start.isoformat(), "cells": cells},
        "coach": _coach(st, cur, w, wellbeing, ever, features, tool),
        "highlights": highlights, "funnel": funnel,
    })
    return out


def session_rows(st: Stats) -> list:
    """One row per session of `st` (unsorted, not trimmed): what the Sessions table and All AI show."""
    rows = []
    for sid, s in st.sessions.items():
        try:
            a = datetime.fromisoformat(s["start"].replace("Z", "+00:00")).astimezone()
            b = datetime.fromisoformat(s["end"].replace("Z", "+00:00")).astimezone()
            day = a.strftime("%Y-%m-%d")
            mins = round(max(0.0, (b - a).total_seconds()) / 60)
        except Exception:
            day, mins = "", 0
        title = st.session_titles.get(sid) or "Untitled session"
        rows.append({
            "id": sid[:8], "title": title,
            "project": s["project"], "day": day, "min": mins,
            "loc": s["loc"], "reads": s["reads"], "writes": s["writes"],
            "output": s["output"], "prompts": s["prompts"], "bash": s["bash"],
            "type": classify_session(title, s["reads"], s["writes"], s["loc"]),
        })
    return rows


def stats_to_json(st: Stats, days_filter=None, instructions=("CLAUDE.md", "AGENTS.md")) -> dict:
    """The range payload. `instructions` are the files (relative to a project folder) that make a
    project's has_instructions true: SOURCES[...]["instructions"]."""
    all_days = sorted(st.days.keys())
    if days_filter:
        all_days = all_days[-days_filter:]
    dayset = set(all_days)

    def tot(key):
        return sum(st.days[d][key] for d in all_days)

    prompts_total = tot("prompts")
    # session count/avg respect the selected range (by the session's start day),
    # so they stay consistent with the ranged token KPIs beside them.
    sess_durs = []
    # cold-start tax: what share of session cost is just the first API call
    # of each session (re-priming the cache) -- see docs/METRICS.md.
    total_session_cost = 0.0
    cold_start_cost = 0.0
    # cache-health: mid-session calls (not the session's first) where new
    # cache-write material outweighs reused cache-read material, a sign the
    # prefix cache was invalidated (idle >TTL, tool-list/model change, ...).
    non_first_calls = 0
    cache_reset_calls = 0
    for sid, s in st.sessions.items():
        try:
            a = datetime.fromisoformat(s["start"].replace("Z", "+00:00")).astimezone()
            b = datetime.fromisoformat(s["end"].replace("Z", "+00:00")).astimezone()
            sday = a.strftime("%Y-%m-%d")
            dur = max(0, (b - a).total_seconds())
        except Exception:
            sday, dur = "", 0
        if days_filter and sday not in dayset:
            continue
        sess_durs.append(dur)
        total_session_cost += s.get("cost", 0.0)
        cold_start_cost += s.get("first_call_cost", 0.0)
        calls = s.get("api_calls", 0)
        if calls > 1:
            non_first_calls += calls - 1
            cache_reset_calls += s.get("cache_resets", 0)
    sessions_count = len(sess_durs)
    avg_session_min = (sum(sess_durs) / sessions_count / 60) if sessions_count else 0
    cold_start_pct = (cold_start_cost / total_session_cost * 100) if total_session_cost else 0
    cache_reset_pct = (cache_reset_calls / non_first_calls * 100) if non_first_calls else 0

    cache_read = tot("cache_read")
    inp = tot("input")
    cache_eff = cache_read / (cache_read + inp) * 100 if (cache_read + inp) else 0

    projects = []
    for name, p in st.projects.items():
        pdays = sorted(p["days"].keys())
        has_instructions = _has_instructions(st.project_cwd.get(name), instructions)
        projects.append({
            "name": name, "input": p["input"], "output": p["output"],
            "loc": p["loc"], "prompts": p["prompts"],
            "sessions": len(p["sessions"]), "cost": round(p["cost"], 2),
            "days": [{"day": d, **p["days"][d]} for d in pdays],
            "files": p["files"].most_common(8),
            "has_instructions": has_instructions,
        })
    projects.sort(key=lambda x: -(x["input"] + x["output"]))

    models = []
    for name, m in st.models.items():
        models.append({"name": name, **{k: (round(v, 2) if k == "cost" else v)
                                        for k, v in m.items()}})
    models.sort(key=lambda x: -x["output"])

    branches = []
    for name, b in st.branches.items():
        branches.append({"name": name, **b})
    branches.sort(key=lambda x: -x["output"])

    heat = [[st.heatmap.get((wd, h), 0) for h in range(24)] for wd in range(7)]

    sessions_list = session_rows(st)
    sessions_list.sort(key=lambda x: x["day"], reverse=True)
    del sessions_list[300:]

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "totals": {
            "input": inp, "output": tot("output"),
            "cache_read": cache_read, "cache_write": tot("cache_write"),
            "cost": round(tot("cost"), 2), "loc": tot("loc"),
            "prompts": prompts_total, "messages": tot("messages"),
            "sessions": sessions_count,
            "avg_session_min": round(avg_session_min, 1),
            "cache_efficiency": round(cache_eff, 1),
            "cold_start_pct": round(cold_start_pct, 1),
            "cache_reset_pct": round(cache_reset_pct, 1),
            "avg_prompt_lines": round(tot("prompt_lines") / prompts_total, 1) if prompts_total else 0,
            "avg_prompt_words": round(tot("prompt_words") / prompts_total, 1) if prompts_total else 0,
            "interruptions": tot("interruptions"),
            "tool_errors": tot("tool_errors"),
            "reads": tot("reads"), "writes": tot("writes"),
            "human_tokens": tot("human_tokens"),
            "leverage": round(tot("output") / tot("human_tokens"), 1) if tot("human_tokens") else 0,
            "parse_errors": st.parse_errors,
            "corrections": tot("corrections"),
            "delegated": sum(st.subagents.values()),
        },
        "days": [{"day": d, **st.days[d], "cost": round(st.days[d]["cost"], 3),
                  "active_min": len(st.day_buckets.get(d, ())) * 10,
                  "focus_max": longest_focus(st.day_buckets.get(d, ()))} for d in all_days],
        "models": models,
        "tools": st.tools.most_common(12),
        "slash": st.slash.most_common(12),
        "projects": projects[:10],
        "branches": branches[:10],
        "heatmap": heat,
        "longest_prompts": [
            {"lines": l, "words": w, "preview": p, "day": d}
            for (l, w, p, d) in st.longest_prompts if d in dayset or not days_filter
        ][:8],
        "top_files": st.files_touched.most_common(8),
        "sessions_list": sessions_list,
        "quiet_hours": [QUIET_START, QUIET_END],
        "permission_modes": st.permission_modes.most_common(),
        "subagents": st.subagents.most_common(10),
        # weekly report data is always computed over all history
        "weeks": build_weeks(st),
    }


# ---------------------------------------------------------------------------
# Optional AI weekly summary (runs `claude -p`; the ONE feature that costs
# tokens, triggered only by an explicit button press in the UI)
# ---------------------------------------------------------------------------

def _run_claude(prompt: str) -> dict:
    import shutil
    import subprocess
    # Resolve the absolute path once. Relying on the child process to re-resolve
    # "claude" on its own PATH (via `cmd /c claude`) is unreliable — it can pick
    # up a stale/broken shim and silently produce no output.
    exe = shutil.which("claude")
    if not exe:
        return {"ok": False, "error": "claude CLI not found on PATH."}
    # Call the resolved executable directly, passing the prompt as an argument
    # (avoids stdin-piping quirks). Only .cmd/.bat shims need the cmd wrapper.
    if exe.lower().endswith((".cmd", ".bat")):
        base = ["cmd", "/c", exe]
    else:
        base = [exe]
    try:
        r = subprocess.run(base + ["-p", prompt], capture_output=True, text=True,
                           timeout=300, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "claude -p timed out after 5 minutes."}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    text = (r.stdout or "").strip()
    if text:
        return {"ok": True, "text": text}
    # No summary text — surface the real reason instead of a generic message.
    err = (r.stderr or "").strip()
    return {"ok": False, "error": "claude -p exited %d with no text (%s)"
            % (r.returncode, (err or "no stdout or stderr")[:400])}


def ai_week_summary(week: dict) -> dict:
    sess = week["sessions"]
    bullets = "\n".join(
        "- [%s] %s (%s, %s min, %s LoC)" % (s["project"], s["title"], s["day"], s["min"], s["loc"])
        for s in sess[:50]
    ) or "- (no sessions logged this week)"
    if len(sess) > 50:
        bullets += "\n- ...and %d more sessions" % (len(sess) - 50)
    hours = round(week["minutes"] / 60, 1)
    prompt = (
        "Write a short weekly work update for a developer's standup, covering "
        "%s to %s.\n\nCoding sessions this week:\n%s\n\n"
        "Stats: %s lines of code written, %s prompts, %s output tokens, "
        "~%s hours in sessions, tool mix %s file edits vs %s reads/searches.\n\n"
        "Format: one first-person paragraph of 3-5 sentences, then 3-6 short "
        "bullet points of key accomplishments. Plain markdown, no headers, no fluff."
        % (week["start"], week["end"], bullets, week["loc"], week["prompts"],
           week["output"], hours, week["writes"], week["reads"])
    )
    return _run_claude(prompt)


def ai_reflection(week: dict, question: str) -> dict:
    """Discuss the weekly reflection question with claude -p (button-only)."""
    types = Counter(s.get("type", "other") for s in week["sessions"])
    mix = ", ".join("%d %s" % (n, t) for t, n in types.most_common()) or "no sessions"
    prompt = (
        "You are helping a developer reflect on how they use AI coding tools. "
        "Their local usage dashboard asked them this reflection question:\n\n"
        "\"%s\"\n\n"
        "Context for the week %s to %s: %d sessions (%s), %s lines of code "
        "written via the AI, %s prompts (%s corrections, %s interruptions), "
        "%s file edits vs %s reads/searches, %s tool errors, ~%s active hours, "
        "%s messages during quiet hours (11pm-6am).\n\n"
        "Give a thoughtful, concise reflection (under 180 words): interpret what "
        "the data suggests, name one trade-off worth considering, and end with "
        "one concrete experiment to try next week. Plain text, no headers, "
        "address the developer as 'you'."
        % (question, week["start"], week["end"], len(week["sessions"]), mix,
           week["loc"], week["prompts"], week.get("corrections", 0),
           week["interruptions"], week["writes"], week["reads"],
           week["tool_errors"], round(week.get("active_min", 0) / 60, 1),
           week.get("late_msgs", 0))
    )
    return _run_claude(prompt)


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>AI Fluency Insights</title>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<script src="/chart.umd.min.js"></script>
<style>
/* ═══════════════════════════════════════════════════════════════════════
   Tokens — light is the base palette; dark redefines only what changes.
   Every colour has its definition here on bare :root so no value exists
   only inside a media query.
   ═══════════════════════════════════════════════════════════════════════ */
:root{
color-scheme:light dark;
--canvas:#f2f2f5;--canvas-tint:rgba(194,96,61,.05);
--fill:#ffffff;--fill-2:#fafafb;--fill-3:rgba(0,0,0,.04);--fill-4:rgba(0,0,0,.07);
--chrome:rgba(247,247,249,.72);--chrome-side:rgba(243,243,246,.68);
--hair:rgba(0,0,0,.085);--hair-2:rgba(0,0,0,.16);
--txt:#17181c;--txt-2:#4b4d55;--txt-3:#8b8d96;
--acc:#c2603d;--acc-2:#a94e2e;--acc-ink:#fff;
--claude:#c2603d;
--acc-soft:rgba(194,96,61,.10);--acc-line:rgba(194,96,61,.32);
--sky:#2f7ee0;--violet:#7355ea;--emerald:#0f9367;--amber:#b57d00;
--rose:#d84b64;--teal:#0d8f89;--slate:#a4a9b4;
--grid:rgba(0,0,0,.065);
--sh-1:0 1px 1px rgba(0,0,0,.04),0 1px 3px rgba(0,0,0,.05);
--sh-2:0 1px 2px rgba(0,0,0,.05),0 14px 30px -18px rgba(0,0,0,.28);
--sh-3:0 2px 10px rgba(0,0,0,.10),0 40px 80px -28px rgba(0,0,0,.40);
--r-card:16px;--r-ctl:10px;--r-sheet:24px;--r-pill:999px;
--font:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI Variable Text','Segoe UI',Inter,Roboto,'Helvetica Neue',sans-serif;
--mono:'SF Mono','Cascadia Code',ui-monospace,Consolas,monospace;
}
/* dark — defined twice on purpose: once for the system default, once for the
   explicit toggle, so the toggle wins in both directions */
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--canvas:#0b0c0f;--canvas-tint:rgba(232,145,107,.07);
--fill:#15171c;--fill-2:#111318;--fill-3:rgba(255,255,255,.05);--fill-4:rgba(255,255,255,.09);
--chrome:rgba(15,17,21,.68);--chrome-side:rgba(13,15,19,.62);
--hair:rgba(255,255,255,.09);--hair-2:rgba(255,255,255,.18);
--txt:#eef1f6;--txt-2:#adb3c0;--txt-3:#787f8d;
--acc:#e8916b;--acc-2:#d97757;--acc-ink:#20120c;
--claude:#e8916b;
--acc-soft:rgba(232,145,107,.14);--acc-line:rgba(232,145,107,.38);
--sky:#5aa4f5;--violet:#a78bfa;--emerald:#34d399;--amber:#fbbf24;
--rose:#fb7185;--teal:#2dd4bf;--slate:#4a515f;
--grid:rgba(255,255,255,.06);
--sh-1:0 1px 1px rgba(0,0,0,.3),0 1px 3px rgba(0,0,0,.3);
--sh-2:0 1px 2px rgba(0,0,0,.4),0 14px 30px -18px rgba(0,0,0,.8);
--sh-3:0 2px 10px rgba(0,0,0,.5),0 40px 80px -28px rgba(0,0,0,.85);
}}
:root[data-theme="dark"]{
--canvas:#0b0c0f;--canvas-tint:rgba(232,145,107,.07);
--fill:#15171c;--fill-2:#111318;--fill-3:rgba(255,255,255,.05);--fill-4:rgba(255,255,255,.09);
--chrome:rgba(15,17,21,.68);--chrome-side:rgba(13,15,19,.62);
--hair:rgba(255,255,255,.09);--hair-2:rgba(255,255,255,.18);
--txt:#eef1f6;--txt-2:#adb3c0;--txt-3:#787f8d;
--acc:#e8916b;--acc-2:#d97757;--acc-ink:#20120c;
--claude:#e8916b;
--acc-soft:rgba(232,145,107,.14);--acc-line:rgba(232,145,107,.38);
--sky:#5aa4f5;--violet:#a78bfa;--emerald:#34d399;--amber:#fbbf24;
--rose:#fb7185;--teal:#2dd4bf;--slate:#4a515f;
--grid:rgba(255,255,255,.06);
--sh-1:0 1px 1px rgba(0,0,0,.3),0 1px 3px rgba(0,0,0,.3);
--sh-2:0 1px 2px rgba(0,0,0,.4),0 14px 30px -18px rgba(0,0,0,.8);
--sh-3:0 2px 10px rgba(0,0,0,.5),0 40px 80px -28px rgba(0,0,0,.85);
}

/* tool accents — Claude keeps the tokens above. Copilot is violet and All AI teal, in the
   same three blocks as dark (system, light, explicit dark), placed after them so they win */
:root[data-source="copilot"]{
--acc:#7355ea;--acc-2:#5f43d6;--acc-ink:#fff;
--acc-soft:rgba(115,85,234,.10);--acc-line:rgba(115,85,234,.32);--canvas-tint:rgba(115,85,234,.05)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"])[data-source="copilot"]{
--acc:#a78bfa;--acc-2:#8b6cf6;--acc-ink:#140f24;
--acc-soft:rgba(167,139,250,.14);--acc-line:rgba(167,139,250,.38);--canvas-tint:rgba(167,139,250,.07)}}
:root[data-theme="dark"][data-source="copilot"]{
--acc:#a78bfa;--acc-2:#8b6cf6;--acc-ink:#140f24;
--acc-soft:rgba(167,139,250,.14);--acc-line:rgba(167,139,250,.38);--canvas-tint:rgba(167,139,250,.07)}
:root[data-source="all"]{
--acc:#0d8f89;--acc-2:#0a7570;--acc-ink:#fff;
--acc-soft:rgba(13,143,137,.10);--acc-line:rgba(13,143,137,.32);--canvas-tint:rgba(13,143,137,.05)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"])[data-source="all"]{
--acc:#2dd4bf;--acc-2:#14b8a6;--acc-ink:#04201d;
--acc-soft:rgba(45,212,191,.14);--acc-line:rgba(45,212,191,.38);--canvas-tint:rgba(45,212,191,.07)}}
:root[data-theme="dark"][data-source="all"]{
--acc:#2dd4bf;--acc-2:#14b8a6;--acc-ink:#04201d;
--acc-soft:rgba(45,212,191,.14);--acc-line:rgba(45,212,191,.38);--canvas-tint:rgba(45,212,191,.07)}

*{box-sizing:border-box;margin:0;padding:0}
[hidden]{display:none!important}
body{
background:var(--canvas);color:var(--txt);
font:100%/1.5 var(--font);font-size:.875rem;
-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
text-rendering:optimizeLegibility;font-variant-numeric:tabular-nums;
min-height:100vh;
background-image:radial-gradient(1100px 620px at 8% -12%,var(--canvas-tint),transparent 62%);
background-attachment:fixed}
::selection{background:var(--acc-soft)}
::-webkit-scrollbar{width:12px;height:12px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--fill-4);border-radius:99px;border:3.5px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:var(--hair-2);background-clip:padding-box;border:3.5px solid transparent}
code{font:12px/1.5 var(--mono);color:var(--txt-2);background:var(--fill-3);
padding:1px 5px;border-radius:5px;letter-spacing:0}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px;border-radius:6px}
button{font:inherit;color:inherit}

/* ── Typography: tracking and leading are size-specific, never one value ── */
h1{font-size:1.55rem;line-height:1.12;letter-spacing:-.024em;font-weight:640}
h2{font-size:1.1rem;line-height:1.22;letter-spacing:-.018em;font-weight:620}
h3{font-size:.875rem;line-height:1.3;letter-spacing:-.008em;font-weight:620}
.eyebrow{font-size:.6875rem;line-height:1.3;letter-spacing:.07em;
text-transform:uppercase;font-weight:660;color:var(--txt-3)}
.num-xl{font-size:1.75rem;line-height:1;letter-spacing:-.03em;font-weight:660}
.num-lg{font-size:1.375rem;line-height:1;letter-spacing:-.024em;font-weight:660}

/* ═══════════════════════════ Shell ═══════════════════════════ */
.shell{display:grid;grid-template-columns:236px minmax(0,1fr);align-items:start}

/* ── Sidebar: the heavier structural material ── */
.side{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;
padding:20px 14px 16px;gap:18px;
background:var(--chrome-side);
backdrop-filter:blur(30px) saturate(180%);-webkit-backdrop-filter:blur(30px) saturate(180%);
border-right:1px solid var(--hair);z-index:30}
.ident{display:flex;align-items:center;gap:10px;padding:0 6px}
.mark{width:34px;height:34px;border-radius:10px;flex:none;display:grid;place-items:center;
background:linear-gradient(145deg,var(--acc),var(--acc-2));color:var(--acc-ink);
box-shadow:0 5px 14px -6px var(--acc-line),inset 0 1px 0 rgba(255,255,255,.28)}
.mark svg{width:19px;height:19px}
.ident .nm{font-size:.8125rem;font-weight:620;letter-spacing:-.01em;line-height:1.25}
.ident .sub{font-size:.6875rem;color:var(--txt-3);line-height:1.3;margin-top:1px}

.nav{position:relative;display:flex;flex-direction:column;gap:2px}
.nav .thumb{position:absolute;left:0;top:0;border-radius:var(--r-ctl);
background:var(--fill);box-shadow:var(--sh-1);border:1px solid var(--hair);
pointer-events:none;visibility:hidden;transform-origin:0 0;will-change:transform,height}
.nav button{position:relative;z-index:1;display:flex;align-items:center;gap:10px;
background:none;border:0;cursor:pointer;padding:9px 11px;border-radius:var(--r-ctl);
color:var(--txt-2);font-size:.8125rem;font-weight:520;letter-spacing:-.005em;
text-align:left;transition:color .15s ease}
.nav button svg{width:16px;height:16px;flex:none;color:var(--txt-3);transition:color .15s ease}
.nav button:hover{color:var(--txt)}
.nav button.on{color:var(--txt);font-weight:600}
.nav button.on svg{color:var(--acc)}
.nav button:active{transform:scale(.985)}
/* All AI has four pages; the others stay in place, dimmed, so the sidebar does not jump */
.nav button.dim{opacity:.4;cursor:not-allowed}
.nav button.dim:hover{color:var(--txt-2)}
.nav button.dim:active{transform:none}

.sidefoot{margin-top:auto;display:flex;flex-direction:column;gap:7px;padding:0 4px}
.chip{display:inline-flex;align-items:center;gap:7px;font-size:.6875rem;color:var(--txt-3);
letter-spacing:.005em;padding:5px 9px;border-radius:var(--r-pill);
background:var(--fill-3);border:1px solid transparent;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip svg{width:12px;height:12px;flex:none}
.dot{width:6px;height:6px;border-radius:50%;background:var(--emerald);flex:none;
box-shadow:0 0 0 0 rgba(52,211,153,.5);animation:breathe 2.6s ease-out infinite}
@keyframes breathe{0%{box-shadow:0 0 0 0 rgba(52,211,153,.45)}70%{box-shadow:0 0 0 6px rgba(52,211,153,0)}100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}
.themebtn{display:flex;align-items:center;gap:8px;width:100%;margin-top:3px;
background:var(--fill-3);border:1px solid transparent;border-radius:var(--r-ctl);
padding:7px 10px;cursor:pointer;color:var(--txt-2);font-size:.75rem;font-weight:520;
transition:background .15s ease,color .15s ease}
.themebtn:hover{background:var(--fill-4);color:var(--txt)}
.themebtn:active{transform:scale(.98)}
.themebtn svg{width:14px;height:14px}

/* ── Main column & floating toolbar ── */
.main{min-width:0}
.topbar{position:sticky;top:0;z-index:20;
background:var(--chrome);
backdrop-filter:blur(28px) saturate(180%);-webkit-backdrop-filter:blur(28px) saturate(180%)}
.topbar .barin{max-width:1280px;margin:0 auto;padding:16px 30px 14px;
display:flex;align-items:flex-end;gap:18px;flex-wrap:wrap}
.titles{min-width:0}
.titles p{font-size:.75rem;color:var(--txt-3);margin-top:3px;letter-spacing:.002em}
.acts{margin-left:auto;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
/* scroll edge effect instead of a hard 1px rule */
.edge{height:14px;margin-top:-1px;pointer-events:none;
background:linear-gradient(var(--chrome),transparent);
border-top:1px solid var(--hair);
opacity:var(--sc,0);transition:opacity .2s ease}

.content{max-width:1280px;margin:0 auto;padding:6px 30px 56px}
#alerts:not(:empty){margin:10px 0 4px;display:flex;flex-direction:column;gap:8px}
.warn{display:inline-flex;gap:9px;align-items:center;font-size:.75rem;color:var(--amber);
background:var(--fill);border:1px solid var(--hair);border-left:3px solid var(--amber);
padding:10px 14px;border-radius:var(--r-ctl);box-shadow:var(--sh-1)}
.warn svg{width:15px;height:15px;flex:none}
.warn.notify{color:var(--sky);border-left-color:var(--sky)}
.notifyX{margin-left:auto;background:none;border:0;color:inherit;cursor:pointer;
font-size:1rem;line-height:1;padding:0 2px;flex:none}
/* a tool switch in flight, or a failed build: the numbers below are not the picked tool's, so they fade */
#srcstate{margin:10px 0 4px}
#srcstate[hidden]{display:none}
.content>section{transition:opacity .18s ease,filter .18s ease}
.content.busy>section{opacity:.32;filter:saturate(.3);pointer-events:none}

/* ═══════════════════════════ Controls ═══════════════════════════ */
.seg{position:relative;display:inline-flex;background:var(--fill-3);
border-radius:var(--r-ctl);padding:3px;gap:0}
.seg .thumb{position:absolute;top:0;left:0;
border-radius:7px;background:var(--fill);box-shadow:var(--sh-1);
pointer-events:none;visibility:hidden;will-change:transform,width;transition:opacity .2s ease}
.seg button{position:relative;z-index:1;background:none;border:0;cursor:pointer;
padding:6px 13px;border-radius:7px;color:var(--txt-3);
font-size:.75rem;font-weight:560;letter-spacing:-.002em;white-space:nowrap;
transition:color .15s ease}
.seg button:hover{color:var(--txt-2)}
.seg button.on{color:var(--txt)}
.seg button:active{transform:scale(.97)}

.btn{display:inline-flex;align-items:center;gap:7px;
background:var(--fill);border:1px solid var(--hair);color:var(--txt-2);
padding:7px 12px;border-radius:var(--r-ctl);cursor:pointer;
font-size:.75rem;font-weight:540;letter-spacing:-.002em;white-space:nowrap;
box-shadow:var(--sh-1);
transition:border-color .15s ease,color .15s ease,transform .1s ease-out,background .15s ease}
.btn svg{width:14px;height:14px;flex:none}
.btn:hover{color:var(--txt);border-color:var(--hair-2)}
.btn:active{transform:scale(.97)}
.btn:disabled{opacity:.35;cursor:default;transform:none}
.btn.accent{background:var(--acc-soft);border-color:var(--acc-line);color:var(--acc)}
.btn.accent:hover{background:var(--acc-soft);border-color:var(--acc)}
.btn.icon{padding:7px 9px}

/* custom-range popover — anchored to its trigger, materialises from it */
.pop-wrap{position:relative}
.pop{position:absolute;top:calc(100% + 8px);right:0;z-index:40;
transform-origin:top right;width:max-content;
background:var(--fill);border:1px solid var(--hair);border-radius:14px;
box-shadow:var(--sh-3);padding:14px;display:none}
.pop.open{display:block}
.pop .row{display:flex;align-items:center;gap:8px}
.pop label{font-size:.6875rem;color:var(--txt-3);display:block;margin-bottom:5px;
letter-spacing:.05em;text-transform:uppercase;font-weight:640}
.pop input[type=date]{background:var(--fill-2);border:1px solid var(--hair);color:var(--txt);
border-radius:8px;padding:7px 9px;font:inherit;font-size:.75rem}
.pop input[type=date]:focus{outline:none;border-color:var(--acc)}
.pop .dash{color:var(--txt-3);align-self:flex-end;padding-bottom:9px}
.pop .foot{display:flex;gap:8px;margin-top:12px;justify-content:flex-end}

/* ═══════════════════════════ Cards ═══════════════════════════ */
.stack{display:flex;flex-direction:column;gap:22px}
.row{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(400px,1fr))}
.row.half{grid-template-columns:repeat(auto-fit,minmax(330px,1fr))}
.row.split{grid-template-columns:minmax(0,2fr) minmax(260px,1fr)}
.row.split.flip{grid-template-columns:minmax(260px,1fr) minmax(0,2fr)}
.card{background:var(--fill);border:1px solid var(--hair);border-radius:var(--r-card);
padding:18px 18px 16px;box-shadow:var(--sh-2);min-width:0;
transition:border-color .2s ease}
.card:hover{border-color:var(--hair-2)}
.card.flat{box-shadow:var(--sh-1)}
.chead{display:flex;align-items:center;gap:9px;margin-bottom:15px}
.chead h3{color:var(--txt)}
.chead .gl{width:26px;height:26px;border-radius:8px;flex:none;display:grid;place-items:center;
background:var(--acc-soft);color:var(--acc)}
.chead .gl svg{width:15px;height:15px}
.chead .hint{margin-left:auto;font-size:.6875rem;color:var(--txt-3);font-weight:420;
text-align:right;letter-spacing:.002em}
.cvs{position:relative;height:280px;min-width:0}
.cvs.short{height:196px}
.cvs.mini{height:220px}
canvas{display:block}
.note{color:var(--txt-3);font-size:.6875rem;margin-top:13px;line-height:1.6}

/* entrance — cheap, non-gesture, first render only */
.enter>*{animation:rise .55s cubic-bezier(.18,.72,.2,1) backwards;
animation-delay:calc(var(--i,0)*45ms)}
@keyframes rise{from{opacity:0;transform:translateY(10px) scale(.992)}to{opacity:1;transform:none}}

/* ── Momentum strip ── */
.mom{display:flex;align-items:center;gap:14px 30px;flex-wrap:wrap}
.mblock{display:flex;align-items:center;gap:12px}
.mico{width:40px;height:40px;border-radius:12px;flex:none;display:grid;place-items:center;
background:var(--acc-soft);color:var(--acc)}
.mico svg{width:21px;height:21px}
.mblock .ml{font-size:.6875rem;color:var(--txt-3);margin-top:4px;letter-spacing:.01em}
.vsep{width:1px;align-self:stretch;min-height:38px;background:var(--hair)}
.goals{display:flex;gap:22px;flex-wrap:wrap;margin-left:auto}
.goalw{min-width:158px}
.goalw .gt{display:flex;justify-content:space-between;gap:10px;
font-size:.6875rem;color:var(--txt-3);margin-bottom:6px;letter-spacing:.01em}
.goalw .gt b{color:var(--txt);font-weight:620}
.bar{height:7px;background:var(--fill-4);border-radius:99px;overflow:hidden}
.bar>i{display:block;height:100%;border-radius:99px;
background:linear-gradient(90deg,var(--acc),var(--acc-2));
transition:width .55s cubic-bezier(.2,.7,.2,1)}
.bar.over>i{background:linear-gradient(90deg,var(--amber),var(--rose))}

/* ── Plan usage (est.) ── */
.planw{display:flex;flex-direction:column;gap:12px}
.plant{display:flex;align-items:baseline;gap:8px;font-weight:650}
.plant .hint{margin-left:0}
.plant .hint.stale{color:var(--amber);font-weight:650}
.planrow{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.plancol{min-width:140px}
.pcs{font-weight:600;font-size:.8125rem}
.pcr{font-size:.6875rem;color:var(--txt-3);margin-top:2px;letter-spacing:.01em}
.planbar{flex:1;min-width:160px;height:8px}
.pcpct{font-size:.75rem;color:var(--txt-3);white-space:nowrap}

/* ── KPI tiles ── */
.kgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(196px,1fr));gap:12px}
.kpi{position:relative;overflow:hidden;background:var(--fill);
border:1px solid var(--hair);border-radius:14px;padding:14px 14px 0;
box-shadow:var(--sh-1);
transition:transform .18s cubic-bezier(.2,.7,.2,1),border-color .18s ease,box-shadow .18s ease}
.kpi:hover{transform:translateY(-2px);border-color:var(--hair-2);box-shadow:var(--sh-2)}
.khead{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:11px}
.kpi .ic{width:26px;height:26px;border-radius:8px;flex:none;display:grid;place-items:center;
background:var(--kbg,var(--acc-soft));color:var(--kc,var(--acc))}
.kpi .ic svg{width:14px;height:14px}
.kpi .s{color:var(--txt-3);font-size:.6875rem;margin-top:5px;letter-spacing:.005em}
.kpi .kd{font-size:.6875rem;margin-top:6px;font-weight:560}
.kpi .spkwrap{height:26px;margin:11px -14px 0;opacity:.9}
.spk{width:100%;height:100%;display:block}
.kpi.plain{padding-bottom:14px}
/* All AI cost: one line per tool, each in its own unit, never added */
.kpi .costs{display:flex;flex-direction:column;gap:5px}
.kpi .cl{display:flex;justify-content:space-between;align-items:baseline;gap:10px;
font-size:.78125rem;color:var(--txt-2);line-height:1.25}
.kpi .cl b{color:var(--txt);font-weight:660;font-size:.9375rem;letter-spacing:-.012em}
.kpi .cl.sm{justify-content:flex-end;font-size:.6875rem;color:var(--txt-3)}
.tdot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;flex:none}
/* "Not available for Copilot": a KPI tile that says so, in the same footprint, muted */
.kpi.na .ic{background:var(--fill-3);color:var(--txt-3)}
.kpi.na .num-lg{font-size:.9375rem;line-height:22px;font-weight:620;letter-spacing:-.01em;color:var(--txt-2)}
.nabox{display:grid;place-items:center;height:220px}
.nabox .kpi{width:min(100%,280px)}
.nabox .kpi:hover{transform:none}
.surf{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px 32px;align-items:center}
.surf .facts{margin-bottom:0;flex-direction:column;gap:18px}
.up{color:var(--emerald)}.down{color:var(--rose)}.flat-d{color:var(--txt-3)}

/* ── Wellness facts ── */
.facts{display:flex;gap:14px 34px;flex-wrap:wrap;margin-bottom:16px}
.facts .fl{font-size:.6875rem;color:var(--txt-3);margin-top:3px;letter-spacing:.01em}

/* ── Tables ── */
.tbl{width:100%;border-collapse:collapse;font-size:.78125rem}
.tbl th,.tbl td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--hair)}
.tbl thead th{color:var(--txt-3);font-weight:640;font-size:.6875rem;
text-transform:uppercase;letter-spacing:.06em;padding-top:0}
.tbl tbody tr:last-child td{border-bottom:none}
.tbl tbody tr{transition:background .12s ease}
.tbl tbody tr:hover{background:var(--fill-3)}
.tbl td.num,.tbl th.num{text-align:right}
.tbl td b{color:var(--txt);font-weight:600}
.tbl td{color:var(--txt-2)}
.scrollx{overflow-x:auto;margin:0 -4px;padding:0 4px}
#tProj tbody tr{cursor:pointer}
#tProj tbody tr:hover{background:var(--acc-soft)}
.rowarrow{color:var(--txt-3);opacity:0;transition:opacity .15s ease,transform .15s ease}
#tProj tbody tr:hover .rowarrow{opacity:1;transform:translateX(2px)}
#tAProj tbody tr.one{cursor:pointer}
#tAProj tbody tr.one:hover{background:var(--acc-soft)}
#tAProj tbody tr.one:hover .rowarrow{opacity:1;transform:translateX(2px)}

/* ── Heatmap ── */
.hm{display:grid;grid-template-columns:34px repeat(24,1fr);gap:3px;align-items:center;
font-size:.625rem;color:var(--txt-3);min-width:660px}
.hm .cell{aspect-ratio:1;border-radius:4px;background:var(--fill-3);
transition:outline-color .12s ease,transform .12s cubic-bezier(.2,.7,.2,1);
outline:1.5px solid transparent;outline-offset:1px}
.hm .cell:hover{outline-color:var(--acc);transform:scale(1.18)}
.hm .hh{text-align:center;font-size:.5625rem;letter-spacing:.02em}
.hm .wd{font-size:.625rem;font-weight:560;color:var(--txt-3);letter-spacing:.02em}
.hleg{display:flex;align-items:center;gap:6px;margin-top:14px;
font-size:.6875rem;color:var(--txt-3);justify-content:flex-end}
.hleg i{width:12px;height:12px;border-radius:3px;display:inline-block}

/* ── Prompt list / work list ── */
.plist{list-style:none;display:flex;flex-direction:column;gap:9px}
.plist li{color:var(--txt-2);font-size:.78125rem;line-height:1.55;padding:11px 13px;
background:var(--fill-2);border:1px solid var(--hair);border-radius:11px;
border-left:2.5px solid var(--acc)}
.plist b{color:var(--txt);font-weight:600}
.plist .mt{color:var(--txt-3);font-size:.6875rem;margin-top:4px}
.worklist .grp{margin-bottom:18px}
.worklist .grp:last-child{margin-bottom:0}
.worklist h4{color:var(--acc);font-size:.6875rem;font-weight:660;margin-bottom:8px;
text-transform:uppercase;letter-spacing:.06em}
.worklist ul{list-style:none}
.worklist li{padding:9px 0;border-bottom:1px solid var(--hair);font-size:.8125rem;color:var(--txt-2)}
.worklist li:last-child{border-bottom:none}
.worklist li .meta{color:var(--txt-3);font-size:.6875rem;margin-top:3px}
.empty{color:var(--txt-3);font-size:.8125rem;padding:6px 0}

/* ── Fluency ── */
.flgrid{display:grid;grid-template-columns:minmax(0,300px) minmax(0,1fr);gap:26px;align-items:center}
.flchart{position:relative;height:272px;min-width:0}
.flrow{display:flex;align-items:center;gap:14px;padding:11px 0;
border-bottom:1px solid var(--hair);flex-wrap:wrap}
.flrow:last-child{border-bottom:none}
.flrow .fln{width:104px;flex:none;font-weight:620;font-size:.8125rem;letter-spacing:-.008em}
.flrow .flb{width:116px;flex:none}
.flrow .fls{width:32px;flex:none;text-align:right;font-weight:660;font-size:.9375rem;letter-spacing:-.02em}
.flrow .flt{flex:1;min-width:210px;color:var(--txt-3);font-size:.6875rem;line-height:1.55}
.flrow .flt b{color:var(--txt-2);font-weight:620}

/* ── Reflection / AI output ── */
.reflq{font-size:.9375rem;line-height:1.6;letter-spacing:-.005em;color:var(--txt);
background:var(--acc-soft);border-left:3px solid var(--acc);
padding:14px 16px;border-radius:12px;margin-bottom:14px}
.aiout{white-space:pre-wrap;font-size:.84375rem;line-height:1.72;color:var(--txt-2);
letter-spacing:.002em}
.aiout:empty{display:none}

/* ── Weekly nav ── */
.wknav{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.wknav h2{min-width:210px}

/* ── My Adoption ── */
.ad-hero{display:grid;grid-template-columns:minmax(0,440px) minmax(0,1fr);gap:30px;align-items:center;
background:radial-gradient(520px 260px at 0% 0%,var(--acc-soft),transparent 70%),var(--fill)}
.ad-top{display:flex;align-items:center;gap:24px}
.ad-ring{position:relative;width:156px;height:156px;flex:none}
.ad-ring svg{width:100%;height:100%;transform:rotate(-90deg);overflow:visible}
.ad-ring .trk{fill:none;stroke:var(--fill-4);stroke-width:11}
.ad-ring .val{fill:none;stroke-width:11;stroke-linecap:round;
filter:drop-shadow(0 4px 10px var(--acc-line))}
.ad-ring.anim .val{animation:adring 1.1s cubic-bezier(.2,.7,.2,1) backwards}
@keyframes adring{from{stroke-dashoffset:var(--circ)}}
.ad-ring .mid{position:absolute;inset:0;display:grid;place-content:center;text-align:center}
.ad-ring .big{font-size:2.9rem;line-height:1;letter-spacing:-.045em;font-weight:680}
.ad-ring .of{font-size:.6875rem;color:var(--txt-3);margin-top:5px;letter-spacing:.02em}
.ad-lvl .eyebrow{margin-bottom:5px}
.ad-lvl h2{font-size:1.65rem;letter-spacing:-.03em;line-height:1.1}
.ad-lvl .d{color:var(--txt-2);font-size:.8125rem;margin-top:6px;line-height:1.5}
.ad-chg{display:inline-flex;align-items:center;gap:6px;margin-top:10px;font-size:.6875rem;
font-weight:600;padding:4px 10px;border-radius:var(--r-pill);background:var(--fill-3);color:var(--txt-2)}
.ad-chg.up{color:var(--emerald)}.ad-chg.down{color:var(--rose)}
.ad-ladder{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:6px;margin-top:20px}
.ad-ladder .st{font-size:.625rem;color:var(--txt-3);letter-spacing:.02em;text-align:left}
.ad-ladder .st i{display:block;height:6px;border-radius:99px;background:var(--fill-4);margin-bottom:6px;overflow:hidden}
.ad-ladder .st i b{display:block;height:100%;border-radius:99px;
background:linear-gradient(90deg,var(--acc),var(--acc-2));transition:width .6s cubic-bezier(.2,.7,.2,1)}
.ad-ladder .st.on{color:var(--txt);font-weight:640}
.ad-next{font-size:.6875rem;color:var(--txt-3);margin-top:10px}
.ad-next b{color:var(--txt-2);font-weight:620}
.ad-sum{grid-column:1/-1;font-size:.8125rem;color:var(--txt-2);line-height:1.6;
padding-top:16px;border-top:1px solid var(--hair)}
.ad-sum b{color:var(--txt);font-weight:620}
.ad-pills{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.ad-pill{position:relative;background:var(--fill);border:1px solid var(--hair);border-radius:14px;
padding:14px;box-shadow:var(--sh-1);
transition:transform .18s cubic-bezier(.2,.7,.2,1),border-color .18s ease,box-shadow .18s ease}
.ad-pill:hover{transform:translateY(-2px);border-color:var(--hair-2);box-shadow:var(--sh-2)}
.ad-pill .khead{margin-bottom:9px}
.ad-pill .ic{width:26px;height:26px;border-radius:8px;display:grid;place-items:center;
background:var(--kbg);color:var(--kc)}
.ad-pill .ic svg{width:14px;height:14px}
.ad-pill .sc{display:flex;align-items:baseline;gap:6px}
.ad-pill .sc small{font-size:.6875rem;color:var(--txt-3);font-weight:520}
.ad-pill .bar{margin:10px 0 9px;height:6px}
.ad-pill .bar>i{background:var(--kc)}
.ad-pill .bl{font-size:.6875rem;color:var(--txt-3);line-height:1.45}
.ad-pill .dl{font-size:.6875rem;font-weight:600;margin-top:6px}
.ad-tag{font-size:.5625rem;font-weight:680;letter-spacing:.06em;text-transform:uppercase;
padding:2px 7px;border-radius:var(--r-pill);background:var(--kbg);color:var(--kc)}
.ad-tag.grow{background:var(--fill-3);color:var(--txt-3)}

.ad-coach{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.ad-step{position:relative;background:var(--fill-2);border:1px solid var(--hair);border-radius:14px;
padding:15px 15px 14px;display:flex;flex-direction:column;gap:7px;
border-top:3px solid var(--kc,var(--acc))}
.ad-step .nb{width:24px;height:24px;border-radius:50%;display:grid;place-items:center;
font-size:.6875rem;font-weight:700;background:var(--kbg,var(--acc-soft));color:var(--kc,var(--acc))}
.ad-step .top{display:flex;align-items:center;gap:9px}
.ad-step h4{font-size:.875rem;letter-spacing:-.01em;font-weight:640;line-height:1.3}
.ad-step .why{font-size:.75rem;color:var(--txt-3);line-height:1.5}
.ad-step .how{font-size:.78125rem;color:var(--txt-2);line-height:1.55;background:var(--fill);
border:1px solid var(--hair);border-radius:10px;padding:9px 11px;margin-top:2px}
.ad-allgood{display:flex;align-items:center;gap:12px;color:var(--txt-2);font-size:.8125rem}

.ad-facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:14px 22px;margin-bottom:16px}
.ad-facts .fl{font-size:.6875rem;color:var(--txt-3);margin-top:3px}
.ad-facts .fd{font-size:.6875rem;margin-top:3px;font-weight:560}
.ad-cal{max-width:100%}
.ad-calm,.ad-calg{display:grid;grid-template-columns:repeat(17,minmax(0,1fr));gap:3px}
.ad-calm{font-size:.5625rem;color:var(--txt-3);margin-bottom:5px;height:12px}
.ad-calm span{white-space:nowrap;overflow:visible}
.ad-calg{grid-template-rows:repeat(7,auto);grid-auto-flow:column}
.ad-calb{display:grid;grid-template-columns:26px minmax(0,1fr);gap:6px}
.ad-calw{display:grid;grid-template-rows:repeat(7,1fr);font-size:.5625rem;color:var(--txt-3)}
.ad-calw span{display:flex;align-items:center}
.ad-c{aspect-ratio:1;border-radius:4px;background:var(--fill-3);outline:1.5px solid transparent;
outline-offset:1px;transition:outline-color .12s ease,transform .12s cubic-bezier(.2,.7,.2,1)}
.ad-c:hover{outline-color:var(--acc);transform:scale(1.2)}
.ad-c.fut{visibility:hidden}

.ad-feats{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.ad-feat{display:flex;gap:11px;align-items:flex-start;padding:12px;border-radius:12px;
border:1px solid var(--hair);background:var(--fill-2)}
.ad-feat .fi{width:24px;height:24px;border-radius:50%;flex:none;display:grid;place-items:center;
background:var(--fill-4);color:var(--txt-3)}
.ad-feat .fi svg{width:13px;height:13px}
.ad-feat.on{border-color:color-mix(in srgb,var(--sky) 38%,transparent);
background:color-mix(in srgb,var(--sky) 7%,var(--fill))}
.ad-feat.on .fi{background:var(--sky);color:#fff}
.ad-feat.off{border-style:dashed}
.ad-feat b{display:block;font-size:.8125rem;font-weight:620;letter-spacing:-.008em}
.ad-feat span{display:block;font-size:.6875rem;color:var(--txt-3);line-height:1.45;margin-top:2px}
.ad-feat em{display:block;font-style:normal;font-size:.6875rem;font-weight:600;margin-top:6px;color:var(--sky)}
.ad-feat.off em{color:var(--txt-2)}
.ad-feat.lapsed em{color:var(--amber)}
.ad-feat .tipl{margin-top:5px;color:var(--txt-2)}
.ad-craft .flrow .flb .bar{display:block}
.ad-craft .flrow .bar>i{background:var(--violet)}
.ad-craft .flrow .fls small{display:block;font-size:.5625rem;font-weight:600;letter-spacing:0;margin-top:1px}
.ad-how summary{cursor:pointer;font-weight:620;font-size:.8125rem;list-style:none;display:flex;
align-items:center;gap:9px}
.ad-how summary::-webkit-details-marker{display:none}
.ad-how summary::after{content:"";margin-left:auto;width:8px;height:8px;border-right:2px solid var(--txt-3);
border-bottom:2px solid var(--txt-3);transform:rotate(45deg);transition:transform .2s ease}
.ad-how[open] summary::after{transform:rotate(-135deg)}
.ad-how .body{margin-top:14px;display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.ad-how .it{font-size:.75rem;color:var(--txt-3);line-height:1.6}
.ad-how .it b{display:block;color:var(--txt);font-size:.8125rem;margin-bottom:2px}
.ad-prog{height:9px;margin:14px 0 10px}

/* ── Engineering insight tabs ── */
.navgrp{font-size:.625rem;font-weight:680;letter-spacing:.08em;text-transform:uppercase;
color:var(--txt-3);padding:14px 11px 4px}
.cb{display:inline-block;font-size:.5625rem;font-weight:680;letter-spacing:.05em;text-transform:uppercase;
padding:2px 7px;border-radius:var(--r-pill);cursor:help;white-space:nowrap;line-height:1.4}
.cb.observed{background:color-mix(in srgb,var(--emerald) 14%,transparent);color:var(--emerald)}
.cb.derived{background:color-mix(in srgb,var(--sky) 14%,transparent);color:var(--sky)}
.cb.proxy{background:color-mix(in srgb,var(--amber) 16%,transparent);color:var(--amber)}
.cb.estimated{background:color-mix(in srgb,var(--violet) 14%,transparent);color:var(--violet)}
.cb.na{background:var(--fill-3);color:var(--txt-3)}
.ins-hero{display:grid;grid-template-columns:minmax(0,420px) minmax(0,1fr);gap:26px;align-items:center;
background:radial-gradient(480px 240px at 0% 0%,var(--acc-soft),transparent 70%),var(--fill)}
.ins-left{display:flex;align-items:center;gap:20px}
.ins-left h2{font-size:1.35rem;letter-spacing:-.025em;line-height:1.15;margin-top:3px}
.ins-left .d{color:var(--txt-2);font-size:.78125rem;line-height:1.55;margin-top:6px}
.ins-left .cb{margin-top:9px}
.ad-ring.sm{width:124px;height:124px}
.ad-ring.sm .big{font-size:2.15rem}
.ins-tiles{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.ins-tiles .kpi{padding-bottom:13px}
.ins-tiles .khead{margin-bottom:9px}
.ins-empty{color:var(--txt-2);font-size:.8125rem;line-height:1.6}
.ins-empty b{color:var(--txt)}
.cap{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
.cap .it{display:flex;gap:11px;align-items:flex-start;padding:12px;border:1px solid var(--hair);
border-radius:12px;background:var(--fill-2)}
.cap .dot{width:9px;height:9px;border-radius:50%;margin-top:5px;flex:none;background:var(--slate)}
.cap .it.ok .dot{background:var(--emerald)}.cap .it.partial .dot{background:var(--amber)}
.cap .it.missing .dot{background:var(--slate)}
.cap b{display:block;font-size:.8125rem;font-weight:620}
.cap span{display:block;font-size:.6875rem;color:var(--txt-3);line-height:1.5;margin-top:2px}
.cap code{overflow-wrap:anywhere}
.lim{list-style:none;display:flex;flex-direction:column;gap:9px}
.lim li{font-size:.78125rem;color:var(--txt-2);line-height:1.55;padding-left:16px;position:relative}
.lim li::before{content:"";position:absolute;left:0;top:.55em;width:6px;height:6px;border-radius:50%;background:var(--slate)}
.lim.good li::before{background:var(--emerald)}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip2{font-size:.6875rem;padding:4px 10px;border-radius:var(--r-pill);background:var(--fill-3);color:var(--txt-2)}
.chip2 b{color:var(--txt);font-weight:640}
.chip2.warn{background:color-mix(in srgb,var(--rose) 12%,transparent);color:var(--rose)}
.mbar{display:flex;height:10px;border-radius:99px;overflow:hidden;background:var(--fill-4);margin:8px 0 10px}
.mbar i{display:block;height:100%}
.exp-card{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.exp-card .big{font-size:1.5rem;font-weight:660;letter-spacing:-.025em}
.ad-step .acts{display:flex;gap:8px;margin-top:4px}
.ad-step .meta{display:flex;align-items:center;gap:8px;justify-content:space-between}
.ad-block{font-size:.6875rem;color:var(--amber);margin-top:6px;line-height:1.5;font-weight:560}
.lnk{color:var(--acc);cursor:pointer;font-size:.6875rem;font-weight:620}
.lnk:hover{text-decoration:underline}
.ad-hl{list-style:none;display:flex;flex-direction:column;gap:11px}
.ad-hl li{display:flex;gap:11px;align-items:flex-start;font-size:.8125rem;color:var(--txt-2);line-height:1.5}
.ad-hl .dt{width:9px;height:9px;border-radius:50%;flex:none;margin-top:6px;background:var(--slate)}
.ad-hl .up .dt{background:var(--emerald)}.ad-hl .down .dt{background:var(--rose)}
.ad-hl .new .dt{background:var(--sky)}.ad-hl .level .dt{background:var(--violet)}
.ad-funnel{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.ad-funnel .fs{position:relative;padding:12px;border:1px solid var(--hair);border-radius:12px;background:var(--fill-2)}
.ad-funnel .fs .n{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:.6875rem;
font-weight:700;background:var(--fill-4);color:var(--txt-3);margin-bottom:8px}
.ad-funnel .fs.on{border-color:color-mix(in srgb,var(--emerald) 40%,transparent);
background:color-mix(in srgb,var(--emerald) 7%,var(--fill))}
.ad-funnel .fs.on .n{background:var(--emerald);color:#fff}
.ad-funnel b{display:block;font-size:.8125rem;font-weight:630}
.ad-funnel span{display:block;font-size:.6875rem;color:var(--txt-3);line-height:1.5;margin-top:3px}
.tbl td.wrapc{white-space:normal;max-width:260px}
.sel{background:var(--fill-2);border:1px solid var(--hair);color:var(--txt);border-radius:8px;padding:6px 9px;font:inherit;font-size:.75rem}
.privrow{display:flex;align-items:center;gap:12px;flex-wrap:wrap}

/* ═══════════════════════════ Sheet (project deep-dive) ═══════════════════════════ */
#scrim{position:fixed;inset:0;z-index:90;display:none;
background:rgba(10,11,14,.42);
backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}
#sheet{position:fixed;inset:0;z-index:91;display:none;
align-items:flex-start;justify-content:center;padding:44px 16px;
overflow-y:auto;overscroll-behavior:contain}
.sbox{position:relative;width:100%;max-width:880px;
background:var(--fill);border:1px solid var(--hair);border-radius:var(--r-sheet);
box-shadow:var(--sh-3);will-change:transform,opacity;transform-origin:50% 0}
.sgrab{padding:16px 24px 0;cursor:grab;touch-action:none;-webkit-user-select:none;user-select:none}
.sgrab:active{cursor:grabbing}
.sgrab .handle{width:38px;height:4px;border-radius:99px;background:var(--fill-4);margin:0 auto 14px}
.sbox .sbody{padding:0 24px 24px}
#sTitle{font-size:1.1875rem;letter-spacing:-.02em;font-weight:640;line-height:1.2;padding-right:44px}
.ssub{color:var(--txt-3);font-size:.75rem;margin:5px 0 18px;line-height:1.55}
.sbox h3.sh{font-size:.6875rem;font-weight:660;margin:22px 0 10px;color:var(--txt-3);
text-transform:uppercase;letter-spacing:.06em}
#sClose{position:absolute;top:16px;right:18px;width:30px;height:30px;border-radius:9px;
background:var(--fill-3);border:0;color:var(--txt-3);cursor:pointer;
display:grid;place-items:center;transition:background .15s ease,color .15s ease}
#sClose svg{width:15px;height:15px}
#sClose:hover{background:var(--fill-4);color:var(--txt)}
#sClose:active{transform:scale(.92)}

footer{color:var(--txt-3);font-size:.6875rem;margin-top:34px;padding-top:20px;
border-top:1px solid var(--hair);line-height:1.75;max-width:760px}

/* ═══════════════════════════ Responsive ═══════════════════════════ */
@media (max-width:1040px){
.shell{grid-template-columns:1fr}
.side{position:sticky;top:0;height:auto;flex-direction:row;align-items:center;
gap:12px;padding:10px 16px;border-right:0;border-bottom:1px solid var(--hair);
overflow-x:auto;scrollbar-width:none}
.side::-webkit-scrollbar{display:none}
.ident .txt{display:none}
.nav{flex-direction:row;gap:2px}

.nav button{white-space:nowrap}
.sidefoot{margin-top:0;margin-left:auto;flex-direction:row;align-items:center;gap:6px}
.sidefoot .chip.hideable{display:none}
.themebtn{width:auto}.themebtn span{display:none}
.barin,.content{padding-left:20px;padding-right:20px}
}
@media (max-width:1180px){.ad-hero,.ins-hero{grid-template-columns:1fr}}
@media (max-width:760px){
.ad-top{flex-direction:column;align-items:flex-start}
.ad-pills,.ins-tiles{grid-template-columns:1fr}
.ad-funnel{grid-template-columns:1fr 1fr}
.ins-left{flex-direction:column;align-items:flex-start}
.row,.row.half,.row.split,.row.split.flip{grid-template-columns:1fr}
.flgrid{grid-template-columns:1fr}
.flchart{height:236px}
.acts{width:100%;margin-left:0}
.goals{margin-left:0;width:100%}
.barin,.content{padding-left:14px;padding-right:14px}
h1{font-size:1.3125rem}
#sheet{padding:20px 10px}
}

/* ═══════════════════════════ Accessibility ═══════════════════════════ */
@media (prefers-reduced-motion:reduce){
*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;
transition-duration:.12s!important;scroll-behavior:auto!important}
.kpi:hover{transform:none}.hm .cell:hover{transform:none}
.enter>*{animation:none}
}
@media (prefers-reduced-transparency:reduce){
.side,.topbar{backdrop-filter:none;-webkit-backdrop-filter:none;
background:var(--fill)}
#scrim{backdrop-filter:none;-webkit-backdrop-filter:none;background:rgba(10,11,14,.62)}
}
@media (prefers-contrast:more){
:root{--hair:rgba(0,0,0,.28);--hair-2:rgba(0,0,0,.5);--txt-3:#5f626b}
:root[data-theme="dark"]{--hair:rgba(255,255,255,.3);--hair-2:rgba(255,255,255,.52);--txt-3:#a4aab6}
.card,.kpi,.btn{border-width:1px;border-color:var(--hair-2)}
.side,.topbar{background:var(--fill);backdrop-filter:none;-webkit-backdrop-filter:none}
}
@media print{.side,.topbar,.acts{display:none}.content{padding:0}.card{break-inside:avoid}}
</style></head><body>
<div class="shell">

<aside class="side">
  <div class="ident">
    <div class="mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z"/></svg></div>
    <div class="txt"><div class="nm">AI Fluency Insights</div><div class="sub"><span class="tf">Claude Code</span> analytics</div></div>
  </div>

  <nav class="nav" id="nav" aria-label="Sections">
    <span class="thumb" id="navThumb"></span>
    <button data-s="overview" class="on"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>Overview</button>
    <button data-s="focus"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Time &amp; Focus</button>
    <button data-s="tools"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a4 4 0 0 0 5 5l-10 10a2.8 2.8 0 0 1-4-4z"/><path d="m17 3 4 4"/></svg>Tools &amp; Models</button>
    <button data-s="projects"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="m3 7 9-4 9 4-9 4-9-4z"/><path d="m3 12 9 4 9-4M3 17l9 4 9-4"/></svg>Projects</button>
    <button data-s="weekly"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2.5"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>Weekly Report</button>
    <button data-s="adoption"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="6"/><path d="M15.5 13.5 17 22l-5-3-5 3 1.5-8.5"/></svg>My Adoption</button>
    <div class="navgrp">Engineering insight</div>
    <button data-s="flow"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l3-9 4 18 3-9h4"/></svg>Flow</button>
    <button data-s="quality"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>Quality</button>
    <button data-s="value"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>Value</button>
    <button data-s="advisor"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.8.7 1 1.5 1 2.5h6c0-1 .2-1.8 1-2.5A6 6 0 0 0 12 3z"/></svg>Advisor</button>
    <button data-s="health"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>Data Health</button>
  </nav>

  <div class="sidefoot">
    <span class="chip hideable" id="liveChip" title="Reads the logs Claude Code already writes locally &mdash; makes no API calls"><span class="dot"></span>Live &middot; 0 tokens</span>
    <span class="chip hideable"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Updated <span id="upd">&hellip;</span></span>
    <button class="themebtn" id="themeBtn" title="Switch appearance"><span id="themeIcon"></span><span id="themeLabel">System</span></button>
  </div>
</aside>

<main class="main">
<header class="topbar">
  <div class="barin">
    <div class="titles"><h1 id="secTitle">Overview</h1><p id="secSub">&hellip;</p></div>
    <div class="acts">

      <div class="seg" id="srcSeg" role="group" aria-label="Tool" hidden>
        <span class="thumb" id="srcThumb"></span>
        <button data-src="claude" class="on" aria-pressed="true">Claude Code</button><button data-src="copilot" aria-pressed="false">GitHub Copilot</button><button data-src="all" aria-pressed="false">All AI</button>
      </div>

      <div id="actsData" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <div class="seg" id="range" role="group" aria-label="Date range">
          <span class="thumb" id="rangeThumb"></span>
          <button data-d="7">7d</button><button data-d="30" class="on">30d</button>
          <button data-d="90">90d</button><button data-d="0">All</button>
        </div>
        <div class="pop-wrap">
          <button class="btn icon" id="cBtn" title="Custom date range" aria-haspopup="dialog"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2.5"/><path d="M16 2v4M8 2v4M3 10h18"/></svg></button>
          <div class="pop" id="cPop" role="dialog" aria-label="Custom date range">
            <div class="row">
              <div><label for="cFrom">From</label><input type="date" id="cFrom"></div>
              <span class="dash">&ndash;</span>
              <div><label for="cTo">To</label><input type="date" id="cTo"></div>
            </div>
            <div class="foot"><button class="btn" id="cClear">Clear</button><button class="btn accent" id="cApply">Apply</button></div>
          </div>
        </div>
        <button class="btn icon" id="expCopy" title="Copy a text summary to the clipboard"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>
        <button class="btn icon" id="expCsv" title="Download the daily table as CSV"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M12 18v-6M9 15l3 3 3-3"/></svg></button>
        <button class="btn icon" id="expJson" title="Download the full stats payload as JSON"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg></button>
      </div>

      <div id="actsWeek" class="wknav" style="display:none">
        <button class="btn icon" id="wPrev" title="Previous week"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg></button>
        <button class="btn icon" id="wNext" title="Next week"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg></button>
        <button class="btn" id="wCopy"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy Markdown</button>
        <button class="btn accent" id="wAI"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/></svg>AI summary</button>
      </div>

    </div>
  </div>
  <div class="edge"></div>
</header>

<div class="content" id="content">
<div id="alerts"></div>
<div id="srcstate" class="warn" role="status" aria-live="polite" hidden></div>

<!-- ══════════════ Overview ══════════════ -->
<section id="s-overview" class="stack">
  <div class="card flat" id="momCard" style="display:none;--i:0"><div class="mom" id="mom"></div></div>
  <div class="card flat" id="planCard" style="display:none;--i:0"><div id="planUsage"></div></div>
  <div class="kgrid" id="kpis" style="--i:1"></div>
  <div class="card" style="--i:2"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><rect x="7" y="10" width="3" height="8" rx="1"/><rect x="12" y="6" width="3" height="12" rx="1"/><rect x="17" y="13" width="3" height="5" rx="1"/></svg></span>
    <h3>Daily tokens</h3><span class="hint" id="tokHint">input &middot; output &middot; cache read</span></div>
    <div class="cvs"><canvas id="cTokens"></canvas></div></div>
  <div class="card" style="--i:3"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l3-9 4 18 3-9h4"/></svg></span>
    <h3>You vs <span class="tn">Claude</span></h3><span class="hint">tokens you typed vs <span class="tn">Claude</span> produced &middot; log scale</span></div>
    <div class="cvs"><canvas id="cBalance"></canvas></div></div>
  <div class="row" style="--i:4">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/></svg></span>
      <h3>Lines of code / day</h3><span class="hint">written by <span class="tn">Claude</span></span></div>
      <div class="cvs mini"><canvas id="cLoc"></canvas></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 3 8 21M16 3l-3 18M4 9h16M3 15h16"/></svg></span>
      <h3>Exploration vs building</h3><span class="hint">tool calls / day</span></div>
      <div class="cvs mini"><canvas id="cRW"></canvas></div></div>
  </div>
  <div class="card" id="surfCard" style="--i:5" hidden><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg></span>
    <h3>VS Code vs CLI</h3><span class="hint">prompts by where you used Copilot</span></div>
    <div class="surf"><div class="cvs mini" id="surfCvs"><canvas id="cSurf"></canvas></div><div class="facts" id="surfFacts"></div></div></div>
</section>

<!-- ══════════════ Time & Focus ══════════════ -->
<section id="s-focus" class="stack" hidden>
  <div class="card" style="--i:0"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg></span>
    <h3>Time &amp; wellness</h3><span class="hint">active time from 10-min buckets &middot; quiet hours 23:00&ndash;06:00</span></div>
    <div class="facts" id="wellFacts"></div>
    <div class="cvs short"><canvas id="cActive"></canvas></div></div>
  <div class="card" style="--i:1"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2.5"/><path d="M3 9h18M9 21V9"/></svg></span>
    <h3>Activity heatmap</h3><span class="hint">messages by weekday &times; hour</span></div>
    <div class="scrollx"><div class="hm" id="heat"></div></div>
    <div class="hleg">Less<i id="lg1"></i><i id="lg2"></i><i id="lg3"></i><i id="lg4"></i>More</div></div>
  <div class="row" style="--i:2">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg></span>
      <h3>Task mix / day</h3><span class="hint">sessions classified by title</span></div>
      <div class="cvs mini"><canvas id="cTaskMix"></canvas></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg></span>
      <h3>Friction / day</h3><span class="hint" id="fricHint">interruptions &amp; tool errors</span></div>
      <div class="cvs mini"><canvas id="cFrict"></canvas></div></div>
  </div>
</section>

<!-- ══════════════ Tools & Models ══════════════ -->
<section id="s-tools" class="stack" hidden>
  <div class="row" style="--i:0">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 9 9h-9z"/></svg></span>
      <h3>Model usage</h3><span class="hint">output tokens</span></div>
      <div class="cvs mini"><canvas id="cModels"></canvas></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h16M4 18h10"/></svg></span>
      <h3>Model breakdown</h3></div>
      <div class="scrollx"><table class="tbl" id="tModels"><thead><tr><th>Model</th><th class="num">Msgs</th><th class="num">In</th><th class="num">Out</th><th class="num" id="thModCost">Est. $</th></tr></thead><tbody></tbody></table></div></div>
  </div>
  <div class="row" style="--i:1">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/></svg></span>
      <h3>Autonomy mode</h3><span class="hint">how much you let <span class="tn">Claude</span> act without asking</span></div>
      <div class="cvs mini" id="autoCvs"><canvas id="cAutonomy"></canvas></div>
      <div class="nabox" id="autoNa" hidden></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="12" r="3"/><path d="M6 9v6M9 6h4a5 5 0 0 1 5 5"/></svg></span>
      <h3>Delegation</h3><span class="hint">subagents spawned, by type</span></div>
      <div class="cvs mini"><canvas id="cDelegation"></canvas></div></div>
  </div>
  <div class="row" style="--i:2">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h7v7M4 20 21 4M4 4l16 16"/></svg></span>
      <h3>Tool usage</h3><span class="hint">calls in range</span></div>
      <div class="cvs"><canvas id="cTools"></canvas></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6 3 12l6 6M15 6l6 6-6 6"/></svg></span>
      <h3>Slash commands</h3><span class="hint">invocations in range</span></div>
      <div class="cvs"><canvas id="cSlash"></canvas></div></div>
  </div>
</section>

<!-- ══════════════ Projects ══════════════ -->
<section id="s-projects" class="stack" hidden>
  <div class="card" style="--i:0"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m3 7 9-4 9 4-9 4-9-4z"/><path d="m3 12 9 4 9-4M3 17l9 4 9-4"/></svg></span>
    <h3>Projects</h3><span class="hint">select a row for the deep dive</span></div>
    <div class="scrollx"><table class="tbl" id="tProj"><thead><tr><th>Project</th><th class="num">In</th><th class="num">Out</th><th class="num">LoC</th><th class="num" id="thProjCost">Est. $</th><th id="thProjIns" title="Has a CLAUDE.md or AGENTS.md">Instructions</th><th></th></tr></thead><tbody></tbody></table></div></div>
  <div class="row half" style="--i:1">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="8" r="3"/><path d="M6 9v6M18 11c0 4-6 3-6 7"/></svg></span>
      <h3>Git branches</h3></div>
      <div class="scrollx"><table class="tbl" id="tBranch"><thead><tr><th>Branch</th><th class="num">Msgs</th><th class="num">Out</th><th class="num">LoC</th></tr></thead><tbody></tbody></table></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg></span>
      <h3>Most-edited files</h3><span class="hint">by LoC written</span></div>
      <div class="scrollx"><table class="tbl" id="tFiles"><thead><tr><th>File</th><th class="num">LoC written</th></tr></thead><tbody></tbody></table></div></div>
  </div>
  <div class="card" style="--i:2"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></span>
    <h3>Longest prompts</h3><span class="hint">your most detailed briefs</span></div>
    <ul class="plist" id="lPrompts"></ul></div>
</section>

<!-- ══════════════ All AI: Overview, Tools & Models, Projects ══════════════
     (Time & Focus reuses #s-focus) -->
<section id="sa-overview" class="stack" hidden>
  <div class="kgrid" id="aKpis" style="--i:0"></div>
  <div class="row split" style="--i:1">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><rect x="7" y="10" width="3" height="8" rx="1"/><rect x="12" y="6" width="3" height="12" rx="1"/><rect x="17" y="13" width="3" height="5" rx="1"/></svg></span>
      <h3>Daily tokens</h3><span class="hint">by tool &middot; input + cache + output</span></div>
      <div class="cvs"><canvas id="cATokens"></canvas></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 9 9h-9z"/></svg></span>
      <h3>Share by tool</h3><span class="hint">tokens</span></div>
      <div class="cvs"><canvas id="cAShare"></canvas></div></div>
  </div>
</section>

<section id="sa-tools" class="stack" hidden>
  <div class="row split flip" style="--i:0">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 9 9h-9z"/></svg></span>
      <h3>Model usage</h3><span class="hint">output tokens</span></div>
      <div class="cvs"><canvas id="cAModels"></canvas></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h16M4 18h10"/></svg></span>
      <h3>Model breakdown</h3><span class="hint">each model once per tool</span></div>
      <div class="scrollx"><table class="tbl" id="tAModels"><thead><tr><th>Model</th><th>Tool</th><th class="num">Calls</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cost</th></tr></thead><tbody></tbody></table></div></div>
  </div>
</section>

<section id="sa-projects" class="stack" hidden>
  <div class="card" style="--i:0"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m3 7 9-4 9 4-9 4-9-4z"/><path d="m3 12 9 4 9-4M3 17l9 4 9-4"/></svg></span>
    <h3>Projects</h3><span class="hint">tokens include cache &middot; open a row that only one tool worked on</span></div>
    <div class="scrollx"><table class="tbl" id="tAProj"><thead><tr><th>Project</th><th class="num">Total tokens</th><th class="num">Claude tokens</th><th class="num">Copilot tokens</th><th class="num">Prompts</th><th></th></tr></thead><tbody></tbody></table></div></div>
</section>

<!-- ══════════════ Weekly Report ══════════════ -->
<section id="s-weekly" class="stack" hidden>
  <div class="kgrid" id="wKpis" style="--i:0"></div>
  <div class="card" style="--i:1"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></span>
    <h3>What you worked on</h3></div>
    <div class="worklist" id="wWork"></div></div>
  <div class="row" style="--i:2">
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 9 9h-9z"/></svg></span>
      <h3>Exploration vs building</h3></div>
      <div class="cvs mini"><canvas id="cSplit"></canvas></div></div>
    <div class="card"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><rect x="7" y="10" width="3" height="8" rx="1"/><rect x="14" y="6" width="3" height="12" rx="1"/></svg></span>
      <h3>Day by day</h3><span class="hint">output tokens &amp; LoC</span></div>
      <div class="cvs mini"><canvas id="cWeekDays"></canvas></div></div>
  </div>
  <div class="card" style="--i:3"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/></svg></span>
    <h3>AI Fluency report</h3><span class="hint">delegation &middot; description &middot; discernment &middot; diligence</span></div>
    <div class="flgrid"><div class="flchart"><canvas id="cFluency"></canvas></div><div id="flList"></div></div>
    <div class="note">Heuristic scores computed locally from your logs, adapted for coding from Anthropic&rsquo;s 4D AI-fluency framework. Formulas in <code>docs/METRICS.md</code>.</div></div>
  <div class="card" style="--i:4"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.8.7 1 1.5 1 2.5h6c0-1 .2-1.8 1-2.5A6 6 0 0 0 12 3z"/></svg></span>
    <h3>Reflection</h3><span class="hint">a question this week&rsquo;s data raises</span></div>
    <div class="reflq" id="reflQ">&hellip;</div>
    <button class="btn accent" id="reflBtn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>Discuss with Claude</button>
    <div class="aiout" id="reflOut" style="margin-top:14px"></div>
    <div class="note">Discussing runs <code>claude -p</code> locally &mdash; like the AI summary, it costs tokens only when you press the button.</div></div>
  <div class="card" id="wAIcard" style="display:none;--i:5"><div class="chead">
    <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/></svg></span>
    <h3>AI-written summary</h3></div>
    <div class="aiout" id="wAItext"></div>
    <div class="note">Generated locally via <code>claude -p</code> &mdash; the only dashboard feature that consumes tokens, and only when you press the button.</div></div>
</section>

<!-- ══════════════ My Adoption ══════════════ -->
<section id="s-adoption" class="stack" hidden>
  <div class="card" id="adoEmpty" hidden style="--i:0"></div>
  <div class="stack" id="adoFull">
    <div class="card ad-hero" id="adoHero" style="--i:0"></div>
    <div class="row" style="--i:1">
      <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/></svg></span><h3>What changed lately</h3><span class="hint">vs the 4 weeks before</span></div>
        <ul class="ad-hl" id="adoHl"></ul></div>
      <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="12" r="3"/><path d="M6 9v6M9 6h4a5 5 0 0 1 5 5"/></svg></span><h3>Your adoption journey</h3><span class="hint">four stages, each with its evidence</span></div>
        <div class="ad-funnel" id="adoFunnel"></div></div>
    </div>
    <div class="card" style="--i:1"><div class="chead">
      <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/></svg></span><h3>Your next 3 moves</h3><span class="hint">picked from your own data</span></div>
      <div class="ad-coach" id="adoCoach"></div></div>
    <div class="row" style="--i:2">
      <div class="card"><div class="chead">
        <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2.5"/><path d="M16 2v4M8 2v4M3 10h18"/></svg></span><h3>Habit</h3><span class="hint">how regularly you show up</span></div>
        <div class="ad-facts" id="adoHabitFacts"></div>
        <div class="ad-cal" id="adoCal"></div>
        <div class="hleg" style="justify-content:flex-start;margin-top:12px">Less<i id="adg1"></i><i id="adg2"></i><i id="adg3"></i><i id="adg4"></i>More active time</div>
        <div class="eyebrow" style="margin:18px 0 8px">Active days per week</div>
        <div class="cvs" style="height:120px"><canvas id="cAdoWeeks"></canvas></div></div>
      <div class="card"><div class="chead">
        <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/></svg></span><h3>Range</h3><span class="hint" id="adoRangeHint">features you use</span></div>
        <div class="ad-feats" id="adoFeats"></div></div>
    </div>
    <div class="row" style="--i:3">
      <div class="card ad-craft"><div class="chead">
        <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/></svg></span><h3>Craft</h3><span class="hint">how well you work with <span class="tn">Claude</span></span></div>
        <div id="adoCraft"></div>
        <div class="note">The same four AI-fluency skills as the Weekly Report, measured over the last 4 weeks.</div></div>
      <div class="card"><div class="chead">
        <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 6l-9.5 9.5-5-5L1 18"/><path d="M17 6h6v6"/></svg></span><h3>Impact</h3><span class="hint">what <span class="tn">Claude</span> gets done for you</span></div>
        <div class="ad-facts" id="adoImpactFacts"></div>
        <div class="cvs short"><canvas id="cAdoLoc"></canvas></div>
        <div class="note">Lines <span class="tn">Claude</span> wrote for you each week, over the last 12 weeks.</div></div>
    </div>
    <div class="row" style="--i:4">
      <div class="card"><div class="chead">
        <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 5-6"/></svg></span><h3>Your adoption over time</h3><span class="hint">score as of each week, 4-week window</span></div>
        <div class="cvs short"><canvas id="cAdoTrend"></canvas></div></div>
      <div class="card"><div class="chead">
        <span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1 1.1L12 21l7.8-7.5 1-1.1a5.5 5.5 0 0 0 0-7.8z"/></svg></span><h3>Healthy habits</h3><span class="hint">sustainable beats intense</span></div>
        <div class="ad-facts" id="adoWell"></div>
        <div class="ad-allgood" id="adoWellNote"></div></div>
    </div>
    <details class="card ad-how" style="--i:5"><summary><span class="gl" style="width:26px;height:26px;border-radius:8px;display:grid;place-items:center;background:var(--acc-soft);color:var(--acc)"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/></svg></span>How your score is worked out</summary>
      <div class="body" id="adoHow"></div></details>
  </div>
</section>

<!-- ══════════════ Flow ══════════════ -->
<section id="s-flow" class="stack" hidden>
  <div class="card ins-hero" id="flowHero" style="--i:0"></div>
  <div class="row" style="--i:1">
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg></span><h3>Turn time</h3><span class="hint">prompt to finished answer, per day</span></div>
      <div class="cvs short"><canvas id="cFlowTurn"></canvas></div>
      <div class="note">Includes any time spent waiting for you to approve an action. <span id="cbTurn"></span></div></div>
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></span><h3>Clean first-pass turns</h3><span class="hint">no error, interruption or correction</span></div>
      <div class="cvs short"><canvas id="cFlowClean"></canvas></div>
      <div class="note"><span id="cbClean"></span></div></div>
  </div>
  <div class="row" style="--i:2">
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a4 4 0 0 0 5 5l-10 10a2.8 2.8 0 0 1-4-4z"/></svg></span><h3>Where tool time goes</h3><span class="hint">share of time between a tool call and its result</span></div>
      <div class="cvs mini"><canvas id="cFlowTools"></canvas></div>
      <div class="note" id="flowToolNote"></div></div>
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/></svg></span><h3>Slowest tools and commands</h3><span id="cbTool"></span></div>
      <div class="scrollx"><table class="tbl" id="tFlowTools"><thead><tr><th>Tool or command</th><th class="num">Calls</th><th class="num">Median</th><th class="num">90th pct</th><th class="num">Slowest</th><th class="num">Share</th></tr></thead><tbody></tbody></table></div></div>
  </div>
  <div class="row" style="--i:3">
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg></span><h3>Failures and recovery</h3><span id="cbRec" class="hint"></span></div>
      <div class="ad-facts" id="flowRecFacts"></div><div class="chips" id="flowFailTools"></div>
      <div class="note">An episode is the same tool failing; it counts as recovered when that tool later succeeds in the same session. Three failures in a row is a stuck loop.</div></div>
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="12" r="3"/><path d="M6 9v6M9 6h4a5 5 0 0 1 5 5"/></svg></span><h3>Session shape</h3><span class="hint">how your sessions run</span></div>
      <div class="ad-facts" id="flowShapeFacts"></div>
      <div class="eyebrow" style="margin:6px 0 4px">Effort level of <span class="tn">Claude</span>'s responses</div>
      <div class="mbar" id="flowEffortBar"></div><div class="chips" id="flowEffortChips"></div></div>
  </div>
</section>

<!-- ══════════════ Quality ══════════════ -->
<section id="s-quality" class="stack" hidden>
  <div class="card ins-hero" id="qualHero" style="--i:0"></div>
  <div class="row" style="--i:1">
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></span><h3>Verification loop</h3><span class="hint">tests, builds and lint after edits</span></div>
      <div class="scrollx"><table class="tbl" id="tQRuns"><thead><tr><th>Check</th><th class="num">Runs</th><th class="num">Failed</th><th class="num">First run passed</th><th class="num">Fail → fixed</th></tr></thead><tbody></tbody></table></div>
      <div class="eyebrow" style="margin:16px 0 8px">Edit sessions that ended with a check, by week</div>
      <div class="cvs" style="height:140px"><canvas id="cQVerified"></canvas></div></div>
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg></span><h3>Rework hotspots</h3><span id="cbHot" class="hint"></span></div>
      <div class="ad-facts" id="qualReFacts"></div>
      <div class="scrollx"><table class="tbl" id="tQHot"><thead><tr><th>File</th><th class="num">Edits</th><th class="num">Days</th><th class="num">Sessions</th></tr></thead><tbody></tbody></table></div><div class="note" id="qualHotNote"></div></div>
  </div>
  <div class="row" style="--i:2">
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg></span><h3>Friction</h3><span id="cbFric" class="hint"></span></div>
      <div class="ad-facts" id="qualFricFacts"></div>
      <div class="note">A failed command is often a normal step, so look at the trend rather than the number.</div></div>
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg></span><h3>Safety signals</h3><span id="cbSafe" class="hint"></span></div>
      <div id="qualSafety"></div>
      <div class="eyebrow" style="margin:16px 0 8px">How actions were permitted</div>
      <div class="chips" id="qualPerms"></div></div>
  </div>
</section>

<!-- ══════════════ Value ══════════════ -->
<section id="s-value" class="stack" hidden>
  <div class="card ins-hero" id="valHero" style="--i:0"></div>
  <div class="row" style="--i:1">
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg></span><h3 id="valDailyT">Daily cost</h3><span class="hint" id="valDailyH">estimated at list prices; spikes in red</span></div>
      <div class="cvs short"><canvas id="cValDaily"></canvas></div></div>
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 5-6"/></svg></span><h3 id="valWeeklyT">Cost per 100 lines</h3><span class="hint">by week; lower is better</span></div>
      <div class="cvs short"><canvas id="cValWeekly"></canvas></div></div>
  </div>
  <div class="row" style="--i:2">
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg></span><h3>Caching</h3><span id="cbCache" class="hint"></span></div>
      <div class="ad-facts" id="valCacheFacts"></div>
      <div class="note" id="valCacheNote">Savings assume you would otherwise have sent the same tokens uncached, at list prices.</div></div>
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/></svg></span><h3>Model mix</h3><span id="cbMix" class="hint"></span></div>
      <div class="scrollx"><table class="tbl" id="tValModels"><thead><tr><th>Model</th><th class="num" id="thValCost">Cost</th><th class="num">Share</th><th class="num">Sessions</th></tr></thead><tbody></tbody></table></div><div class="note" id="valMixNote"></div></div>
  </div>
  <div class="card" id="valSpikeCard" style="--i:3"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg></span><h3>Cost spikes</h3><span id="cbSpike" class="hint"></span></div>
    <ul class="plist" id="valSpikes"></ul></div>
  <div class="note" id="valNote" style="margin-top:-8px">No score on this tab by design: a dollar figure has no universal "good". Each number is compared with your own previous period.</div>
</section>

<!-- ══════════════ Advisor ══════════════ -->
<section id="s-advisor" class="stack" hidden>
  <div class="card" style="--i:0"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.8.7 1 1.5 1 2.5h6c0-1 .2-1.8 1-2.5A6 6 0 0 0 12 3z"/></svg></span><h3>Your top moves</h3><span class="hint" id="advWindow"></span></div>
    <div class="ad-coach" id="advRecs"></div></div>
  <div class="card" id="advExpCard" style="--i:1"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/></svg></span><h3>Your experiment</h3><span class="hint">try one change, check it next week</span></div>
    <div id="advExp"></div></div>
  <div class="row" style="--i:2">
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></span><h3>What is going well</h3></div><ul class="lim good" id="advWins"></ul></div>
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/></svg></span><h3>Also noticed</h3><span class="hint">lower priority</span></div><div id="advMore"></div></div>
  </div>
  <div class="note">Everything here is rule-based and computed locally from your logs, with the numbers shown. No AI is called and no tokens are used.</div>
</section>

<!-- ══════════════ Data Health ══════════════ -->
<section id="s-health" class="stack" hidden>
  <div class="card" style="--i:0"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg></span><h3>What your logs let us measure</h3><span class="hint">green = available, amber = partial, grey = not in your logs</span></div>
    <div class="cap" id="healthCaps"></div></div>
  <div class="row" style="--i:1">
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg></span><h3>Log inventory</h3></div>
      <div class="chips" id="healthMeta" style="margin-bottom:14px"></div>
      <div class="scrollx"><table class="tbl" id="tHealth"><thead><tr><th>Log entry type</th><th class="num">Count</th></tr></thead><tbody></tbody></table></div></div>
    <div class="card"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg></span><h3>What these logs cannot tell us</h3></div>
      <ul class="lim" id="healthLimits"></ul>
      <div class="chead" style="margin:22px 0 12px"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg></span><h3>Privacy</h3></div>
      <div class="privrow"><button class="btn" id="privBtn"></button>
        <span class="note" style="margin:0">Hides session titles, prompt previews and file names on screen. Everything stays on your machine either way. This dashboard is for your own reflection, not for ranking people or performance reviews.</span></div></div>
  </div>
  <div class="card" style="--i:2"><div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/></svg></span><h3>Metric catalog</h3>
    <select class="sel" id="catTab" style="margin-left:auto"><option value="">All tabs</option></select></div>
    <div class="chips" style="margin-bottom:12px">
      <span class="cb observed">Observed</span><span class="chip2" style="background:none;padding:0">read straight from the log</span>
      <span class="cb derived">Derived</span><span class="chip2" style="background:none;padding:0">computed with a stated formula</span>
      <span class="cb proxy">Proxy</span><span class="chip2" style="background:none;padding:0">an indirect signal</span>
      <span class="cb estimated">Estimated</span><span class="chip2" style="background:none;padding:0">depends on list prices or a model</span></div>
    <div class="scrollx"><table class="tbl" id="tCat"><thead><tr><th>Metric</th><th>Tab</th><th>Confidence</th><th>How it is computed</th><th>Limitation</th></tr></thead><tbody></tbody></table></div></div>
</section>

<footer>100% local &middot; reads <span id="footSrc">Claude Code</span> logs<span id="rootWrap"> from <code id="root">~/.claude/projects</code></span> &middot; auto-refreshes every 15s.<br>
<span id="footCost">Costs are estimates at public API pricing (incl. cache read/write rates) &mdash; on a subscription plan the real marginal cost is $0. Edit the <code>PRICING</code> table in the script to tune.</span></footer>
</div>
</main>
</div>

<div id="scrim"></div>
<div id="sheet" role="dialog" aria-modal="true" aria-labelledby="sTitle"><div class="sbox" id="sBox">
  <div class="sgrab" id="sGrab"><div class="handle"></div>
    <button id="sClose" aria-label="Close"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
    <h2 id="sTitle"></h2><div class="ssub" id="sSub"></div>
  </div>
  <div class="sbody">
    <div class="cvs short"><canvas id="cProj"></canvas></div>
    <h3 class="sh">Sessions</h3><ul class="plist" id="sSess"></ul>
    <h3 class="sh">Top files</h3>
    <div class="scrollx"><table class="tbl" id="sFiles"><thead><tr><th>File</th><th class="num">LoC written</th></tr></thead><tbody></tbody></table></div>
  </div>
</div></div>
<script>
/* ══════════════════════════════════════════════════════════════════════
   AI Fluency Insights — front end
   Motion model: springs, not durations. Every animation starts from the
   value currently on screen, so it can be grabbed and reversed mid-flight.
   ══════════════════════════════════════════════════════════════════════ */

const $=id=>document.getElementById(id);
const fmt=n=>n>=1e9?(n/1e9).toFixed(2)+'B':n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':''+n;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const fmtD=s=>{const p=String(s).split('-');return MON[+p[1]-1]+' '+(+p[2]);};
const fmtMin=m=>m>=60?Math.floor(m/60)+'h'+(m%60?' '+(m%60)+'m':''):m+'m';
const TT=['feature','bugfix','refactor','docs','explore','other'];
const cssv=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const hexa=(h,a)=>{const n=parseInt(h.replace('#',''),16);
return 'rgba('+(n>>16&255)+','+(n>>8&255)+','+(n&255)+','+a+')';};
const RM=matchMedia('(prefers-reduced-motion: reduce)');

let days=30,charts={},D=null,wIdx=null,adoAnim=false,section='overview',customFrom='',customTo='',booted=false;
/* Which tool's numbers are on screen: 'claude' | 'copilot' | 'all'. TN() is the short name for
   sentences ("sent to Claude"), TF() the full one. */
const SRCN={claude:['Claude','Claude Code'],copilot:['Copilot','GitHub Copilot'],all:['AI','All AI']};
let SOURCE='claude';
try{const sv=localStorage.getItem('dtd-source');if(['claude','copilot','all'].includes(sv))SOURCE=sv;}catch(e){}
const TN=()=>SRCN[SOURCE][0],TF=()=>SRCN[SOURCE][1];

/* ─────────────────────────── Spring ───────────────────────────
   Apple's two designer parameters: damping ratio and response (seconds).
   Interruptible by construction — `to()` re-targets from the live value
   and carries the current velocity through, so a reversal has no seam. */
class Spring{
  constructor(v,onUpdate,opt){opt=opt||{};
    this.v=v;this.vel=0;this.target=v;this.on=onUpdate;this.raf=0;this.last=0;
    this.zeta=opt.damping===undefined?1:opt.damping;
    this.w=2*Math.PI/(opt.response===undefined?.4:opt.response);}
  jump(t){this.target=this.v=t;this.vel=0;
    if(this.raf){cancelAnimationFrame(this.raf);this.raf=0;}
    this.on(this.v);}
  to(t,vel){
    if(RM.matches){this.jump(t);this.done&&this.done();return;}
    this.target=t;if(vel!==undefined)this.vel=vel;this.run();}
  run(){if(this.raf)return;this.last=performance.now();
    const step=now=>{
      const dt=Math.min(.05,(now-this.last)/1000);this.last=now;
      const n=Math.max(1,Math.ceil(dt/.004)),h=dt/n;
      for(let i=0;i<n;i++){
        const f=-this.w*this.w*(this.v-this.target)-2*this.zeta*this.w*this.vel;
        this.vel+=f*h;this.v+=this.vel*h;}
      this.on(this.v);
      if(Math.abs(this.v-this.target)<.004&&Math.abs(this.vel)<.04){
        this.v=this.target;this.vel=0;this.on(this.v);this.raf=0;
        this.done&&this.done();return;}
      this.raf=requestAnimationFrame(step);};
    this.raf=requestAnimationFrame(step);}
}
/* Apple's momentum projection (exponential decay), not v²/2a */
const projectTo=(v,rate)=>(v/1000)*(rate||.998)/(1-(rate||.998));
/* progressive resistance past a boundary instead of a hard stop */
const rubber=(over,dim,c)=>{c=c||.55;return (over*dim*c)/(dim+c*Math.abs(over));};

/* ─── Selection indicator: 4 independent springs (never one 2D spring) ─── */
function Indicator(thumb){
  let x=0,y=0,w=0,h=0,seeded=false;
  const apply=()=>{thumb.style.transform='translate3d('+x+'px,'+y+'px,0)';
    thumb.style.width=w+'px';thumb.style.height=h+'px';
    thumb.style.visibility='visible';};
  const o={damping:1,response:.36};
  const sx=new Spring(0,v=>{x=v;apply();},o), sy=new Spring(0,v=>{y=v;apply();},o),
        sw=new Spring(0,v=>{w=v;apply();},o), sh=new Spring(0,v=>{h=v;apply();},o);
  return{move(el,instant){if(!el||!el.offsetParent&&!el.offsetWidth)return;
    const t=[el.offsetLeft,el.offsetTop,el.offsetWidth,el.offsetHeight];
    if(!seeded||instant){seeded=true;sx.jump(t[0]);sy.jump(t[1]);sw.jump(t[2]);sh.jump(t[3]);}
    else{sx.to(t[0]);sy.to(t[1]);sw.to(t[2]);sh.to(t[3]);}}};
}
const navInd=Indicator($('navThumb')),rangeInd=Indicator($('rangeThumb')),srcInd=Indicator($('srcThumb'));

/* ─────────────────────────── Appearance ─────────────────────────── */
const SUN='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
const AUTO='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3v18a9 9 0 0 0 0-18z" fill="currentColor" stroke="none"/></svg>';
const THEMES=[['system','System',AUTO],['light','Light',SUN],['dark','Dark',MOON]];
let themeIx=0;
function applyTheme(persist){
  const t=THEMES[themeIx];
  if(t[0]==='system')document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme',t[0]);
  $('themeIcon').innerHTML=t[2];$('themeLabel').textContent=t[1];
  if(persist){try{localStorage.setItem('dtd-theme',t[0]);}catch(e){}}
  themeCharts();if(D)render();paintLegend();
}
try{const sv=localStorage.getItem('dtd-theme');
  const i=THEMES.findIndex(t=>t[0]===sv);if(i>=0)themeIx=i;}catch(e){}
$('themeBtn').onclick=()=>{themeIx=(themeIx+1)%THEMES.length;applyTheme(true);};
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{
  if(THEMES[themeIx][0]==='system'){themeCharts();if(D)render();paintLegend();}});

/* ─────────────────────────── Chart.js theming ─────────────────────────── */
let C={},PIE=[],EFF=[],GRID={},XGRID={},ANIM={duration:0};
function themeCharts(){
  C={sky:cssv('--sky'),emerald:cssv('--emerald'),violet:cssv('--violet'),amber:cssv('--amber'),
     rose:cssv('--rose'),teal:cssv('--teal'),coral:cssv('--acc'),slate:cssv('--slate'),claude:cssv('--claude'),
     txt:cssv('--txt'),txt2:cssv('--txt-2'),txt3:cssv('--txt-3'),
     grid:cssv('--grid'),fill:cssv('--fill'),hair:cssv('--hair')};
  /* the Copilot / All AI accent is the same hex as --violet / --teal, so a palette that has the
     accent first drops any later entry equal to it, keeping every slice distinct */
  const uniq=a=>a.filter((c,i)=>!i||c.toLowerCase()!==C.coral.toLowerCase());
  PIE=uniq([C.coral,C.sky,C.violet,C.emerald,C.amber,C.teal,C.rose]);
  EFF=uniq([C.coral,C.sky,C.violet,C.teal,C.emerald,C.amber]).slice(0,4);
  GRID={grid:{color:C.grid,drawTicks:false},border:{display:false},ticks:{padding:8}};
  XGRID={grid:{display:false},border:{display:false},
         ticks:{padding:6,maxRotation:0,autoSkip:true,maxTicksLimit:12}};
  const F=cssv('--font');
  Chart.defaults.font.family=F;Chart.defaults.font.size=11;
  Chart.defaults.font.weight=500;
  Chart.defaults.color=C.txt3;Chart.defaults.borderColor=C.grid;
  const L=Chart.defaults.plugins.legend.labels;
  L.usePointStyle=true;L.boxWidth=7;L.boxHeight=7;L.padding=16;L.color=C.txt3;
  const T=Chart.defaults.plugins.tooltip;
  T.backgroundColor=C.fill;T.borderColor=C.hair;T.borderWidth=1;T.padding=11;
  T.cornerRadius=11;T.titleColor=C.txt;T.bodyColor=C.txt2;T.usePointStyle=true;
  T.boxPadding=5;T.displayColors=true;T.titleFont={weight:'600',size:12};
  Chart.defaults.elements.bar.borderRadius=5;Chart.defaults.elements.bar.borderSkipped=false;
  Chart.defaults.elements.point.radius=0;Chart.defaults.elements.point.hoverRadius=5;
  Chart.defaults.elements.point.hitRadius=14;
  Chart.defaults.elements.line.tension=.36;Chart.defaults.elements.line.borderWidth=2;
  Chart.defaults.maintainAspectRatio=false;
  ANIM=RM.matches?{duration:1}:{duration:520,easing:'easeOutQuart'};
}
function mk(id,cfg){const el=$(id);if(!el)return;
  if(charts[id]){charts[id].destroy();delete charts[id];}
  cfg.options=cfg.options||{};
  if(!('animation' in cfg.options))cfg.options.animation=ANIM;
  const ch=charts[id]=new Chart(el,cfg);
  /* Chart.js's construction-time auto-draw can silently no-op (its internal
     animate/update pipeline occasionally never fires the first paint) —
     force one explicit synchronous draw so the chart is never left blank. */
  ch.draw();}
function areaFill(hex){return ctx=>{const ch=ctx.chart,a=ch.chartArea;
  if(!a)return hexa(hex,.14);
  const g=ch.ctx.createLinearGradient(0,a.top,0,a.bottom);
  g.addColorStop(0,hexa(hex,.34));g.addColorStop(1,hexa(hex,0));return g;};}
function paintLegend(){const a=cssv('--acc');
  [['lg1',.14],['lg2',.38],['lg3',.64],['lg4',.92]].forEach(p=>{
    const e=$(p[0]);if(e)e.style.background=hexa(a,p[1]);});}

/* ─────────────────────────── Sparklines ─────────────────────────── */
let spkSeq=0;
function spark(vals,color){
  if(!vals||vals.length<2)return'';
  const w=100,h=26,mx=Math.max.apply(null,vals),mn=Math.min.apply(null,vals),r=(mx-mn)||1;
  const pts=vals.map((v,i)=>[i/(vals.length-1)*w,h-2-((v-mn)/r)*(h-5)]);
  const d=pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
  const id='spk'+(++spkSeq);
  return '<div class="spkwrap"><svg class="spk" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none" aria-hidden="true">'+
  '<defs><linearGradient id="'+id+'" x1="0" y1="0" x2="0" y2="1">'+
  '<stop offset="0" stop-color="'+color+'" stop-opacity=".26"/>'+
  '<stop offset="1" stop-color="'+color+'" stop-opacity="0"/></linearGradient></defs>'+
  '<path d="'+d+' L'+w+' '+h+' L0 '+h+' Z" fill="url(#'+id+')"/>'+
  '<path d="'+d+'" fill="none" stroke="'+color+'" stroke-width="1.5" stroke-linecap="round" '+
  'stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg></div>';
}

/* ─────────────────────────── Icons ─────────────────────────── */
const IC={in:'<path d="M12 3v13m0 0 4-4m-4 4-4-4M4 21h16"/>',
out:'<path d="M12 21V8m0 0 4 4m-4-4-4 4M4 3h16"/>',
cache:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
cost:'<path d="M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
code:'<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/>',
prompt:'<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
session:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
alert:'<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
user:'<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
lever:'<path d="M23 6l-9.5 9.5-5-5L1 18"/><path d="M17 6h6v6"/>',
flame:'<path d="M12 2s5 4.5 5 9a5 5 0 0 1-10 0c0-1.2.4-2.3.4-2.3S5 10 5 13a7 7 0 0 0 14 0c0-5.5-7-11-7-11z"/>',
cal:'<rect x="3" y="4" width="18" height="18" rx="2.5"/><path d="M16 2v4M8 2v4M3 10h18"/>',
fix:'<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
layers:'<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/>',
target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
check:'<path d="M20 6 9 17l-5-5"/>',
plus:'<path d="M12 5v14M5 12h14"/>',
spark:'<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/>'};
const ic=k=>'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+IC[k]+'</svg>';

/* Money in the selected tool's unit: Claude's estimate in dollars, Copilot's AI credits as logged.
   `dp` fixes the decimals of a dollar amount; credits are rounded to two. The two units are never added. */
const credits=()=>!!D&&D.cost_unit==='credits';
const fmtCost=(v,dp,cr)=>(cr===undefined?credits():cr)?fmt(Math.round(v*100)/100)+' cr':'$'+(dp==null?v:(+v).toFixed(dp));
/* a table cell: dollars stay bare because the header already says "Est. $" */
const costCell=v=>credits()?fmtCost(v):v;
/* A metric the selected tool's logs cannot give. Same footprint as a KPI tile; the reason is one line from
   the server (D.na), never a made-up zero. */
const naCard=(label,reason,icon)=>
  '<div class="kpi plain na"><div class="khead"><span class="eyebrow">'+label+'</span><span class="ic">'+ic(icon||'alert')+'</span></div>'+
  '<div class="num-lg">Not available for '+TN()+'</div><div class="s">'+esc(reason)+'</div></div>';

/* ─────────────────────────── Navigation ─────────────────────────── */
const META={
  overview:['Overview','The headline numbers and how they move.'],
  focus:['Time &amp; Focus','When you work, how long, and what it costs you.'],
  tools:['Tools &amp; Models','Which models answered and which tools they reached for.'],
  projects:['Projects','Where the work landed — by repo, branch and file.'],
  weekly:['Weekly Report','Always full history — the range filter does not apply here.'],
  get adoption(){return ['My Adoption','How you use '+TF()+' day to day — last 4 weeks vs the 4 before. The range filter does not apply here.'];},
  flow:['Flow','How smoothly work moves: turn time, first-pass success and recovery.'],
  quality:['Quality','Whether speed comes with checking: tests, builds, rework and safety.'],
  get value(){return ['Value',credits()?'What the usage costs in AI credits, as logged, and what you get back.'
    :'What the usage costs and what you get back, at list prices with no guesses.'];},
  advisor:['Advisor','Concrete next steps picked from your own data. The range filter does not apply here.'],
  health:['Data Health','What your logs let us measure, and what they cannot. The range filter does not apply here.']};
const NORANGE=['weekly','adoption','advisor','health'];
/* All AI has four pages. Time & Focus is the Claude/Copilot page (#s-focus) fed by slot-based days;
   the other three have their own containers (#sa-overview, #sa-tools, #sa-projects). */
const ALL_SECTIONS=['overview','focus','tools','projects'];
const ALL_OWN=['overview','tools','projects'];
const ALL_TIP='Pick Claude Code or GitHub Copilot to see this.';
const SECTIONS=['overview','focus','tools','projects','weekly','adoption','flow','quality','value','advisor','health'];

$('nav').addEventListener('click',e=>{
  const b=e.target.closest('button[data-s]');if(!b||b.classList.contains('dim'))return;setSection(b.dataset.s);});

/* the items All AI has no page for are dimmed, not removed, so the sidebar does not jump */
function paintNav(){
  const all=SOURCE==='all';
  [...$('nav').querySelectorAll('button[data-s]')].forEach(b=>{
    const dim=all&&ALL_SECTIONS.indexOf(b.dataset.s)<0;
    b.classList.toggle('dim',dim);
    if(dim){b.title=ALL_TIP;b.setAttribute('aria-disabled','true');}
    else{b.removeAttribute('title');b.removeAttribute('aria-disabled');}});}
function paintSections(anim){
  SECTIONS.forEach(k=>{
    const own=ALL_OWN.indexOf(k)>=0;
    if(own)$((SOURCE==='all'?'s-':'sa-')+k).hidden=true;     // the other tool set's container for this page
    const el=$((own&&SOURCE==='all'?'sa-':'s-')+k);el.hidden=k!==section;
    if(anim&&k===section){el.classList.remove('enter');void el.offsetWidth;el.classList.add('enter');
      setTimeout(()=>el.classList.remove('enter'),900);}});}

function setSection(s){
  if(SOURCE==='all'&&ALL_SECTIONS.indexOf(s)<0)return;       // dimmed
  section=s;
  [...$('nav').querySelectorAll('button[data-s]')].forEach(b=>{
    const on=b.dataset.s===s;b.classList.toggle('on',on);
    b.setAttribute('aria-current',on?'page':'false');
    if(on)navInd.move(b);});
  paintSections(true);
  $('actsData').style.display=NORANGE.indexOf(s)>=0?'none':'flex';
  $('actsWeek').style.display=s==='weekly'?'flex':'none';
  if(s==='adoption')adoAnim=true;
  updateChrome();
  if(D)render();
  if(NORANGE.indexOf(s)<0)requestAnimationFrame(()=>rangeInd.move($('range').querySelector('.on'),true));
}
function rangeText(){
  if(customFrom||customTo)return (customFrom||'the start')+' → '+(customTo||'today');
  return days?('Last '+days+' days'):'All time';}
const FOOT_USD=$('footCost').innerHTML;
function updateChrome(){
  const m=META[section];$('secTitle').innerHTML=m[0];
  let sub=m[1];
  if(NORANGE.indexOf(section)>=0&&section!=='weekly'){}
  else if(section!=='weekly')sub=rangeText()+' · '+sub;
  else if(D&&D.weeks&&D.weeks.length&&wIdx!==null&&D.weeks[wIdx])
    sub='Week of '+fmtD(D.weeks[wIdx].start)+' – '+fmtD(D.weeks[wIdx].end)+' · '+sub;
  $('secSub').innerHTML=sub;
  /* the footer's pricing note is about Claude's dollar estimate; Copilot's money is credits as logged */
  $('footCost').innerHTML=credits()?'Credits are as '+SRCN.copilot[0]+'’s logs report them &mdash; nothing is priced from model rates.'
    :SOURCE==='all'?'Claude Code’s cost is an estimate at public API pricing; GitHub Copilot’s credits are as its logs report them. The two are never added together.':FOOT_USD;}

/* ─────────────────────────── Tool switch ─────────────────────────── */
/* data-source on <html> swaps the accent tokens (see the CSS), so the colour says whose
   numbers these are; TN()/TF() carry the name into every sentence. */
const srcOn=(d,id)=>{const s=((d&&d.sources)||[]).find(x=>x.id===id);return !!(s&&s.available);};
function paintNames(){
  const all=SOURCE==='all',names=all?'Claude Code and GitHub Copilot':TF();
  document.querySelectorAll('.tn').forEach(e=>{e.textContent=TN();});
  document.querySelectorAll('.tf').forEach(e=>{e.textContent=TF();});
  $('footSrc').textContent=names;
  $('liveChip').title='Reads the logs '+names+(all?' already write':' already writes')+' locally — makes no API calls';}
function paintSwitch(){
  const seg=$('srcSeg');let n=0,cur=null;
  seg.querySelectorAll('button[data-src]').forEach(b=>{
    const id=b.dataset.src,is=id===SOURCE,show=srcOn(D,id)||is;   // never strand the current pick
    b.hidden=!show;if(show)n++;
    b.classList.toggle('on',is);b.setAttribute('aria-pressed',is);if(is)cur=b;});
  seg.hidden=!D||n<2;                 // one tool on this machine: nothing to switch
  if(!seg.hidden&&cur)requestAnimationFrame(()=>srcInd.move(cur));}
function applySource(persist){
  document.documentElement.setAttribute('data-source',SOURCE);
  if(persist){try{localStorage.setItem('dtd-source',SOURCE);}catch(e){}}
  paintNames();themeCharts();paintLegend();paintSwitch();
  /* a page All AI does not have: go to Overview (setSection draws nothing until load() brings the data) */
  if(SOURCE==='all'&&ALL_SECTIONS.indexOf(section)<0)setSection('overview');
  paintNav();paintSections(false);updateChrome();}
/* section and range stay; `auto` is the fall-back below, which must not overwrite the saved pick */
function setSource(id,auto){
  if(id===SOURCE)return;
  SOURCE=id;wIdx=null;                // each tool has its own weeks
  $('wAIcard').style.display='none';$('reflOut').textContent='';
  /* the page re-themes and renames now, but the numbers stay the old tool's until load() brings the new
     ones (seconds on a long history): dim them and say so, so the two tools never read as one */
  srcState('loading','Loading '+esc(SRCN[id][1])+'…',true);
  applySource(!auto);load();}
/* the line above the page that says a switch is loading or the stats could not be built; `dim` fades the
   numbers below, for when they belong to another tool (or there are none yet). kind null clears both. */
function srcState(kind,msg,dim){
  const el=$('srcstate');
  $('content').className='content'+(dim?' busy':'');
  if(!kind){el.hidden=true;el.innerHTML='';return;}
  el.className='warn'+(kind==='loading'?' notify':'');
  el.innerHTML=ic(kind==='loading'?'session':'alert')+'<span>'+msg+'</span>';
  el.hidden=false;}
$('srcSeg').addEventListener('click',e=>{
  const b=e.target.closest('button[data-src]');if(b)setSource(b.dataset.src);});
function paintEmptySrc(){
  let el=$('emptysrc');
  if(D.source!=='copilot'||srcOn(D,'copilot')){if(el)el.remove();return;}
  if(!el){el=document.createElement('div');el.id='emptysrc';el.className='warn notify';$('alerts').appendChild(el);}
  el.innerHTML=ic('prompt')+'<span>No GitHub Copilot chat history found on this computer.'+
    (D.root?' Looked in: '+esc(D.root)+'.':'')+
    ' It appears here after you use Copilot Chat in VS Code or the Copilot CLI.</span>';}

/* ─────────────────────────── Range controls ─────────────────────────── */
$('range').addEventListener('click',e=>{
  const b=e.target.closest('button[data-d]');if(!b)return;
  days=+b.dataset.d;customFrom='';customTo='';
  $('cFrom').value='';$('cTo').value='';
  [...$('range').querySelectorAll('button')].forEach(x=>x.classList.remove('on'));
  b.classList.add('on');$('rangeThumb').style.opacity=1;
  rangeInd.move(b);updateChrome();load();});

/* custom-range popover — scales out of its own trigger, not the page centre */
const popS=new Spring(0,v=>{const p=$('cPop');
  p.style.opacity=v;p.style.transform='scale('+(.92+.08*v)+') translateY('+((1-v)*-5)+'px)';},
  {damping:1,response:.3});
let popOpen=false;
popS.done=()=>{if(!popOpen&&popS.target===0)$('cPop').classList.remove('open');};
function togglePop(open){
  popOpen=open;const p=$('cPop');
  if(open){p.classList.add('open');popS.to(1);}else{popS.to(0);
    if(RM.matches)p.classList.remove('open');}}
$('cBtn').onclick=e=>{e.stopPropagation();togglePop(!popOpen);};
$('cPop').onclick=e=>e.stopPropagation();
document.addEventListener('click',()=>{if(popOpen)togglePop(false);});
$('cApply').onclick=()=>{
  const f=$('cFrom').value,t=$('cTo').value;if(!f&&!t)return;
  customFrom=f;customTo=t;
  [...$('range').querySelectorAll('button')].forEach(x=>x.classList.remove('on'));
  $('rangeThumb').style.opacity=0;          // no segment owns the selection now
  togglePop(false);updateChrome();load();};
$('cClear').onclick=()=>{
  customFrom='';customTo='';$('cFrom').value='';$('cTo').value='';
  const b=$('range').querySelector('[data-d="'+days+'"]');
  $('rangeThumb').style.opacity=1;
  if(b){b.classList.add('on');rangeInd.move(b);}
  togglePop(false);updateChrome();load();};

/* ─────────────────────────── Scroll edge ─────────────────────────── */
let edgeTick=false;
addEventListener('scroll',()=>{if(edgeTick)return;edgeTick=true;
  requestAnimationFrame(()=>{edgeTick=false;
    document.documentElement.style.setProperty('--sc',Math.min(1,scrollY/26).toFixed(3));});},
  {passive:true});
addEventListener('resize',()=>{
  navInd.move($('nav').querySelector('.on'),true);
  const rb=$('range').querySelector('.on');if(rb)rangeInd.move(rb,true);
  const sb=$('srcSeg').querySelector('.on');if(sb&&!$('srcSeg').hidden)srcInd.move(sb,true);});

/* ─────────────────────────── Data ─────────────────────────── */
async function load(){
  const src=SOURCE;
  const qs=((customFrom||customTo)
    ?('from='+encodeURIComponent(customFrom)+'&to='+encodeURIComponent(customTo))
    :('days='+days))+'&source='+src;
  let data,built=null;                 // built: the server answered, but could not build the stats
  try{
    const r=await fetch('/api/stats?'+qs);
    if(!r.ok){
      if(r.status>=500){built={detail:''};try{built.detail=(await r.json()).detail||'';}catch(e){}}
      throw new Error('HTTP '+r.status);}
    data=await r.json();
  }catch(err){
    if(src!==SOURCE)return;
    console.error('Failed to load dashboard data:',err);
    const mixed=!D||D.source!==SOURCE;   // what is on screen is another tool's numbers (or nothing yet)
    if(built){
      const cw=$('connwarn');if(cw)cw.remove();   // the server is there: it is the stats that failed
      srcState('error','Couldn’t build the stats for '+esc(TF())+(built.detail?' ('+esc(built.detail)+')':'')+
        ' — retrying every 15s.'+(mixed?'':' Showing the last data loaded.'),mixed);
      return;}
    let cw=$('connwarn');
    if(!cw){cw=document.createElement('div');cw.id='connwarn';cw.className='warn';
      $('alerts').appendChild(cw);}
    cw.innerHTML=ic('alert')+'<span>Can’t reach the dashboard server — retrying every 15s. '+
      (D&&!mixed?'Showing the last data loaded.':'')+'</span>';
    if(mixed)srcState('error','Couldn’t load '+esc(TF())+' yet.',true);
    else srcState(null,null,false);   // this tool's own numbers are on screen: no loading dim
    return;   // keep whatever was last on screen (dimmed when it is not this tool's); setInterval(load,...) will retry
  }
  if(src!==SOURCE)return;            // the tool changed while this was in flight
  const cw=$('connwarn');if(cw)cw.remove();
  /* the saved tool may be gone (its logs deleted): move to the first one that is on.
     Copilot stays put when the server did serve it, so the empty state can say where it looked. */
  const av=(data.sources||[]).filter(s=>s.available);
  if(av.length&&!av.some(s=>s.id===SOURCE)&&!(SOURCE==='copilot'&&data.source==='copilot')){
    setSource(av[0].id,true);return;}
  D=data;
  $('upd').textContent=D.generated;
  const rt=D.root||(D.source==='claude'?'~/.claude/projects':'');
  $('root').textContent=rt;$('rootWrap').hidden=!rt;
  paintSwitch();paintEmptySrc();
  let pw=$('parsewarn');
  if(D.totals.parse_errors>0){
    if(!pw){pw=document.createElement('div');pw.id='parsewarn';pw.className='warn';
      $('alerts').appendChild(pw);}
    pw.innerHTML=ic('alert')+'<span>'+
      D.totals.parse_errors.toLocaleString()+
      ' log entries could not be parsed — the log format may have changed; stats may be incomplete.</span>';
  }else if(pw){pw.remove();}
  if(SOURCE==='claude'&&D.source==='claude'&&D.notifications&&D.notifications.length){
    const seen=window.__notifySeen||(window.__notifySeen=new Set());
    D.notifications.forEach(n=>{
      if(seen.has(n.id))return;
      seen.add(n.id);
      const el=document.createElement('div');
      el.className='warn notify';
      el.innerHTML=ic('alert')+'<span>'+esc(n.project)+' — '+esc(n.title)+': '+
        esc(n.message)+'</span><button class="notifyX" aria-label="Dismiss">&times;</button>';
      el.querySelector('.notifyX').onclick=()=>el.remove();
      $('alerts').appendChild(el);
      setTimeout(()=>el.remove(),20000);
    });
  }
  const nwk=(D.weeks||[]).length;                   // the All AI payload has no weeks
  if(wIdx===null&&nwk)wIdx=nwk-1;
  if(wIdx!==null&&wIdx>=nwk)wIdx=nwk-1;
  updateChrome();
  try{render();}finally{srcState(null,null,false);}   // the numbers on screen are this tool's now
  if(!booted){booted=true;
    requestAnimationFrame(()=>{navInd.move($('nav').querySelector('.on'),true);
      rangeInd.move($('range').querySelector('.on'),true);});}
}
function render(){
  if((D.source==='all')!==(SOURCE==='all'))return;     // the other tool set's numbers are still here: load() is on its way
  const all=SOURCE==='all';
  ({overview:all?renderAllOverview:renderOverview,focus:renderFocus,tools:all?renderAllTools:renderTools,
    projects:all?renderAllProjects:renderProjects,weekly:renderWeek,adoption:renderAdoption,
    flow:renderFlow,quality:renderQuality,value:renderValue,advisor:renderAdvisor,health:renderHealth}[section])();}

function delta(cur,prev){
  if(prev==null||prev===0||!isFinite(cur/prev))return'';
  const pc=Math.round((cur-prev)/prev*100);
  if(pc===0)return'<span class="flat-d">no change vs last period</span>';
  return '<span class="'+(pc>0?'up':'down')+'">'+(pc>0?'▲':'▼')+' '+
    Math.abs(pc)+'% vs last period</span>';}

/* ─────────────────────────── Overview ─────────────────────────── */
function renderMomentum(){
  const card=$('momCard');
  if(D.streak===undefined){card.style.display='none';return;}
  card.style.display='';
  const g=D.goals||{},today=D.today||{};
  let h='<div class="mblock"><div class="mico">'+ic('flame')+'</div><div>'+
    '<div class="num-xl">'+D.streak+'</div><div class="ml">day streak · best '+D.best_streak+'</div></div></div>'+
    '<div class="vsep"></div>'+
    '<div class="mblock"><div class="mico">'+ic('cal')+'</div><div>'+
    '<div class="num-xl">'+D.active_days+'</div><div class="ml">active days all-time</div></div></div>';
  const bars=[];
  const bar=(label,val,goal)=>{if(!goal)return;
    const pc=Math.min(100,Math.round(val/goal*100)),over=val>goal;
    bars.push('<div class="goalw"><div class="gt"><span>'+label+'</span><b>'+fmt(val)+' / '+fmt(goal)+
      '</b></div><div class="bar'+(over?' over':'')+'"><i style="width:'+pc+'%"></i></div></div>');};
  bar("Today's LoC",today.loc||0,g.daily_loc);
  bar("Today's output",today.tokens||0,g.daily_tokens);
  bar('This week output',D.week_tokens||0,g.weekly_tokens);
  if(bars.length)h+='<div class="goals">'+bars.join('')+'</div>';
  $('mom').innerHTML=h;}

function renderPlanUsage(){
  const card=$('planCard'),pw=D.plan_window;
  const active=SOURCE==='claude'&&D.source==='claude'&&pw&&(pw.source==='official'||pw.ceiling);
  if(!active){card.style.display='none';return;}
  card.style.display='';
  const isOfficial=pw.source==='official';
  const barRow=(title,pct,mins)=>{
    const p=Math.min(100,pct),over=pct>=100,hasMins=mins!=null;
    const rh=hasMins?Math.floor(mins/60):0,rm=hasMins?mins%60:0;
    return '<div class="planrow"><div class="plancol"><div class="pcs">'+title+'</div>'+
      (hasMins?'<div class="pcr">Resets in '+rh+'h '+rm+'m</div>':'')+'</div>'+
      '<div class="bar planbar'+(over?' over':'')+'"><i style="width:'+p+'%"></i></div>'+
      '<div class="pcpct">'+pct+'% used</div></div>';};
  let rows=barRow('Current session',pw.pct,pw.resets_in_min);
  if(isOfficial&&pw.week_pct!=null)rows+=barRow('This week',pw.week_pct,pw.week_resets_in_min);
  // The statusline only re-captures on Claude Code activity (see statusline.js),
  // so a gap here means no session on this machine has made an API call
  // recently -- the number shown is real, just not necessarily current.
  // Above STALE_AGE_MIN, say so loudly rather than a quiet gray hint.
  const STALE_AGE_MIN=2;
  const fmtAge=m=>m<60?m+'m':Math.floor(m/60)+'h '+(m%60)+'m';
  let age='';
  if(isOfficial&&pw.captured_age_min!=null){
    const stale=pw.captured_age_min>=STALE_AGE_MIN;
    age=' <span class="hint'+(stale?' stale':'')+'" title="Updates only when a Claude Code session on this machine is active. It can lag the live /usage number during idle stretches.">'+
      (stale?'⚠ last updated '+fmtAge(pw.captured_age_min)+' ago, may be behind'
            :(pw.captured_age_min<1?'as of just now':'as of '+fmtAge(pw.captured_age_min)+' ago'))+
      '</span>';
  }
  $('planUsage').innerHTML=
    '<div class="planw"><div class="plant">Plan usage limits<span class="hint">'+
    (isOfficial?'(live)':'(estimate)')+'</span>'+age+'</div>'+rows+'</div>';}

function renderOverview(){
  const d=D,t=d.totals;
  renderMomentum();
  renderPlanUsage();
  const S=k=>d.days.map(x=>x[k]||0);
  const na=d.na||{},cr=credits(),cop=SOURCE==='copilot'&&d.copilot?d.copilot:null;
  const apx=!!(cop&&cop.token_modes&&cop.token_modes.approx>0);     // some sessions have estimated tokens
  const kpis=[
    ['Input tokens',fmt(t.input),'sent to '+TN()+(apx?' · approx.':''),'in',C.sky,t.input,'input',S('input')],
    ['Output tokens',fmt(t.output),'generated by '+TN()+(apx?' · approx.':''),'out',C.emerald,t.output,'output',S('output')],
    ['Cache read',fmt(t.cache_read),
      (t.cache_reset_pct>5?'⚠ cache resetting mid-session (~'+t.cache_reset_pct+'% of turns)'
                          :t.cache_efficiency+'% cache hit rate'+(apx?' · sessions with a cache split':'')),
      'cache',C.teal,t.cache_read,'cache_read',S('cache_read')],
    [cr?'Credits used':'Est. API cost',fmtCost(t.cost),cr?'AI credits, as logged':'$0 on a subscription plan','cost',C.amber,t.cost,'cost',S('cost')],
    ...(cop&&cop.premium_requests>0?[['Premium requests',fmt(Math.round(cop.premium_requests*100)/100),
      'billed per request, separate from credits','layers',C.sky,null,null,null]]:[]),
    ['Cold-start tax',t.cold_start_pct+'%','of cost is priming the cache on a session’s first call','cost',C.rose,t.cold_start_pct,'cold_start_pct',null],
    ['Lines of code',fmt(t.loc),'written by '+TN(),'code',C.violet,t.loc,'loc',S('loc')],
    ['Prompts',fmt(t.prompts),t.avg_prompt_lines+' lines avg','prompt',C.coral,t.prompts,'prompts',S('prompts')],
    ['Sessions',t.sessions,t.avg_session_min+' min avg','session',C.sky,t.sessions,'sessions',null],
    na.interruptions?['Tool errors',t.tool_errors,'tool calls that failed','alert',C.rose,null,null,S('tool_errors')]   // no interruption count to add
    :['Friction',t.interruptions,t.tool_errors+' tool errors','alert',C.rose,null,null,
      d.days.map(x=>(x.interruptions||0)+(x.tool_errors||0)),'interruptions'],
    ['Corrections',fmt(t.corrections),(t.prompts?Math.round(t.corrections/t.prompts*100):0)+'% of prompts','fix',C.rose,t.corrections,'corrections',S('corrections')],
    ['Your tokens',fmt(t.human_tokens),'typed in prompts (est.)','user',C.amber,t.human_tokens,'human_tokens',S('human_tokens')],
    ['Leverage',t.leverage+'×',TN()+' tokens per typed token','lever',C.emerald,null,null,null]];
  $('kpis').innerHTML=kpis.map(k=>{
    const nk=k[8]||k[6];                                  // the key D.na is filed under
    if(nk&&na[nk])return naCard(k[0],na[nk],k[3]);
    const dh=(k[6]&&D.prev)?delta(k[5],D.prev[k[6]]):'';
    const sp=k[7]&&k[7].some(v=>v>0)?spark(k[7],k[4]):'';
    return '<div class="kpi'+(sp?'':' plain')+'" style="--kc:'+k[4]+';--kbg:'+hexa(k[4],.13)+'">'+
      '<div class="khead"><span class="eyebrow">'+k[0]+'</span><span class="ic">'+ic(k[3])+'</span></div>'+
      '<div class="num-lg">'+k[1]+'</div>'+
      (dh?'<div class="kd">'+dh+'</div>':'')+
      '<div class="s">'+k[2]+'</div>'+sp+'</div>';}).join('');

  const labels=d.days.map(x=>x.day.slice(5));
  $('tokHint').innerHTML=(na.cache_read?'input &middot; output':'input &middot; output &middot; cache read')+(apx?' &middot; approx.':'');
  mk('cTokens',{type:'bar',data:{labels,datasets:[
    {label:'Input',data:S('input'),backgroundColor:C.sky},
    {label:'Output',data:S('output'),backgroundColor:C.emerald},
    ...(na.cache_read?[]:[{label:'Cache read',data:S('cache_read'),backgroundColor:C.slate}])]},
    options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});
  mk('cBalance',{type:'line',data:{labels,datasets:[
    {label:'You (typed)',data:S('human_tokens'),borderColor:C.amber,backgroundColor:areaFill(C.amber),fill:true},
    {label:TN()+' (output)',data:S('output'),borderColor:C.coral,backgroundColor:areaFill(C.coral),fill:true}]},
    options:{scales:{x:XGRID,y:{type:'logarithmic',...GRID}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});
  mk('cLoc',{type:'line',data:{labels,datasets:[{label:'LoC',data:S('loc'),
    borderColor:C.violet,backgroundColor:areaFill(C.violet),fill:true}]},
    options:{scales:{x:XGRID,y:GRID},plugins:{legend:{display:false}}}});
  mk('cRW',{type:'bar',data:{labels,datasets:[
    {label:'Exploration (reads/searches)',data:S('reads'),backgroundColor:C.sky},
    {label:'Building (file edits)',data:S('writes'),backgroundColor:C.emerald}]},
    options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});
  renderSurfaces(cop,apx);
}

/* Copilot only: where the prompts were typed, VS Code against the Copilot CLI */
function renderSurfaces(cop,apx){
  const card=$('surfCard'),sf=cop&&cop.surfaces;
  card.hidden=!sf;
  if(!sf){if(charts.cSurf){charts.cSurf.destroy();delete charts.cSurf;}return;}
  const SL={vscode:'VS Code',cli:'Copilot CLI'},ks=Object.keys(sf);
  const tot=ks.reduce((a,k)=>a+(sf[k].prompts||0),0);
  $('surfFacts').innerHTML=ks.map(k=>
    '<div><div class="num-lg">'+fmt(sf[k].prompts||0)+'</div><div class="fl">'+(SL[k]||esc(k))+' prompts · '+
    (sf[k].sessions||0)+' session'+(sf[k].sessions===1?'':'s')+' · '+fmt(sf[k].output||0)+' tokens out'+(apx?' · approx.':'')+'</div></div>').join('');
  if(!tot){
    if(charts.cSurf){charts.cSurf.destroy();delete charts.cSurf;}
    $('surfCvs').innerHTML='<div class="empty">No Copilot prompts in this range.</div>';return;}
  if(!$('cSurf'))$('surfCvs').innerHTML='<canvas id="cSurf"></canvas>';
  mk('cSurf',{type:'doughnut',data:{labels:ks.map(k=>SL[k]||k),
    datasets:[{data:ks.map(k=>sf[k].prompts||0),backgroundColor:ks.map((k,i)=>[C.coral,C.slate][i]||PIE[i%PIE.length]),
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'}}}});
}

/* ─────────────────────────── Time & Focus ─────────────────────────── */
function renderFocus(){
  const d=D,labels=d.days.map(x=>x.day.slice(5));
  const sum=k=>d.days.reduce((a,x)=>a+(x[k]||0),0);
  const actTot=sum('active_min'),msgTot=sum('messages'),lateTot=sum('late_msgs');
  const focusMax=d.days.reduce((a,x)=>Math.max(a,x.focus_max||0),0);
  const wknd=d.days.reduce((a,x)=>{const g=new Date(x.day+'T12:00:00').getDay();
    return a+((g===0||g===6)?x.messages:0);},0);
  const actDays=d.days.filter(x=>x.messages>0).length;
  const wf=[[fmtMin(actTot),'active time in range'],
    [actDays?fmtMin(Math.round(actTot/actDays)):'0m','avg per active day'],
    [fmtMin(focusMax),focusMax>=180?'longest focus block — take breaks':'longest focus block'],
    [(msgTot?Math.round(lateTot/msgTot*100):0)+'%','in quiet hours (23–06)'],
    [(msgTot?Math.round(wknd/msgTot*100):0)+'%','on weekends']];
  $('wellFacts').innerHTML=wf.map(f=>
    '<div><div class="num-lg">'+f[0]+'</div><div class="fl">'+f[1]+'</div></div>').join('');
  mk('cActive',{type:'bar',data:{labels,datasets:[{label:'Active minutes',
    data:d.days.map(x=>x.active_min||0),
    backgroundColor:d.days.map(x=>((x.late_msgs||0)>x.messages*.25&&x.messages)?C.rose:C.teal)}]},
    options:{scales:{x:XGRID,y:GRID},plugins:{legend:{display:false},tooltip:{callbacks:{
      label:c=>fmtMin(c.parsed.y)+' active'+
        ((d.days[c.dataIndex].late_msgs||0)>d.days[c.dataIndex].messages*.25?' · heavy quiet-hours use':'')}}}}});

  const byDay={};d.days.forEach(x=>byDay[x.day]=Object.fromEntries(TT.map(t=>[t,0])));
  (D.sessions_list||[]).forEach(s=>{if(byDay[s.day]&&s.type)byDay[s.day][s.type]++;});
  const TC={feature:C.emerald,bugfix:C.rose,refactor:C.violet,docs:C.amber,explore:C.sky,other:C.slate};
  mk('cTaskMix',{type:'bar',data:{labels,datasets:TT.map(t=>
    ({label:t,data:d.days.map(x=>byDay[x.day][t]),backgroundColor:TC[t]}))},
    options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID,ticks:{precision:0}}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});
  /* All AI: interruptions are summed only over the tools that log them (interruptions_from), and the
     card says which, so a tool that does not log them never reads as a zero */
  const af=d.source==='all'?(d.interruptions_from||[]):null,afN=af?Object.keys(d.by_tool).length:0;
  const afOnly=af&&af.length&&af.length<afN?af.map(k=>SRCN[k][1]).join(' and ')+' only':'';
  const naI=!!(d.na&&d.na.interruptions)||!!(af&&!af.length);   // never a row of zeros for what is not logged
  $('fricHint').innerHTML=af&&!af.length?'tool errors &middot; interruptions are not logged by these tools'
    :afOnly?'interruptions: '+afOnly+' &middot; tool errors'
    :naI?'tool errors &middot; interruptions not available for '+TN():'interruptions &amp; tool errors';
  mk('cFrict',{type:'bar',data:{labels,datasets:[
    ...(naI?[]:[{label:'Interruptions'+(afOnly?' ('+afOnly+')':''),data:d.days.map(x=>x.interruptions),backgroundColor:C.rose}]),
    {label:'Tool errors',data:d.days.map(x=>x.tool_errors),backgroundColor:C.amber}]},
    options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});

  const wd=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  /* All AI cells count active 10-minute slots (time), not log lines, so a tooltip says so */
  const unit=d.source==='all'?' active 10-min slot':' message';
  $('heat').closest('.card').querySelector('.hint').innerHTML=(d.source==='all'?'active 10-min slots':'messages')+' by weekday &times; hour';
  const max=Math.max(1,...d.heatmap.flat()),acc=cssv('--acc'),base=cssv('--fill-3');
  let hm='<div></div>'+[...Array(24).keys()].map(h=>'<div class="hh">'+(h%2?'':h)+'</div>').join('');
  d.heatmap.forEach((row,i)=>{hm+='<div class="wd">'+wd[i]+'</div>'+row.map((v,h)=>
    '<div class="cell" title="'+wd[i]+' '+h+':00 — '+v+unit+(v===1?'':'s')+
    '" style="background:'+(v?hexa(acc,(.14+.78*v/max).toFixed(3)):base)+'"></div>').join('');});
  $('heat').innerHTML=hm;paintLegend();
}

/* ─────────────────────────── Tools & Models ─────────────────────────── */
function renderTools(){
  const d=D;
  mk('cModels',{type:'doughnut',data:{labels:d.models.map(m=>m.name),
    datasets:[{data:d.models.map(m=>m.output),backgroundColor:PIE,
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'}}}});
  const AUTO_LBL={auto:'Auto (full autonomy)',default:'Default (asks each time)',
    acceptEdits:'Accept edits (asks before commands)',unknown:'Unknown'};
  const AUTO_CLR={auto:C.emerald,default:C.sky,acceptEdits:C.violet,unknown:C.slate};
  const pm=d.permission_modes||[],pmNa=d.na&&d.na.permission_modes;
  $('autoCvs').hidden=!!pmNa;$('autoNa').hidden=!pmNa;
  if(pmNa){                                    // the tool's logs have no approval modes: say so, no empty ring
    if(charts.cAutonomy){charts.cAutonomy.destroy();delete charts.cAutonomy;}
    $('autoNa').innerHTML=naCard('Autonomy mode',pmNa,'user');}
  else mk('cAutonomy',{type:'doughnut',data:{labels:pm.map(x=>AUTO_LBL[x[0]]||x[0]),
    datasets:[{data:pm.map(x=>x[1]),backgroundColor:pm.map(x=>AUTO_CLR[x[0]]||C.slate),
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'}}}});
  const sub=d.subagents||[];
  mk('cDelegation',{type:'doughnut',data:{labels:sub.map(x=>x[0]),
    datasets:[{data:sub.map(x=>x[1]),backgroundColor:PIE,
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'}}}});
  mk('cTools',{type:'bar',data:{labels:d.tools.map(x=>x[0]),
    datasets:[{data:d.tools.map(x=>x[1]),backgroundColor:C.coral,borderRadius:5}]},
    options:{indexAxis:'y',scales:{x:GRID,y:XGRID},plugins:{legend:{display:false}}}});
  mk('cSlash',{type:'bar',data:{labels:d.slash.map(x=>x[0]),
    datasets:[{data:d.slash.map(x=>x[1]),backgroundColor:C.teal,borderRadius:5}]},
    options:{indexAxis:'y',scales:{x:GRID,y:XGRID},plugins:{legend:{display:false}}}});
  $('thModCost').textContent=credits()?'Credits':'Est. $';
  document.querySelector('#tModels tbody').innerHTML=d.models.length?d.models.map(m=>
    '<tr><td><b>'+esc(m.name)+'</b></td><td class="num">'+m.msgs+'</td><td class="num">'+
    fmt(m.input)+'</td><td class="num">'+fmt(m.output)+'</td><td class="num">'+costCell(m.cost)+'</td></tr>').join('')
    :'<tr><td colspan="5" class="empty">No model activity in this range.</td></tr>';
}

/* ─────────────────────────── Projects ─────────────────────────── */
const ARROW='<svg class="rowarrow" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>';
function renderProjects(){
  const d=D,cop=SOURCE==='copilot'&&d.source==='copilot';
  const insFiles=cop?'.github/copilot-instructions.md or AGENTS.md':'CLAUDE.md or AGENTS.md';
  $('thProjCost').textContent=credits()?'Credits':'Est. $';
  $('thProjIns').title='Has a '+insFiles;
  document.querySelector('#tProj tbody').innerHTML=d.projects.length?d.projects.map(p=>
    '<tr tabindex="0"><td title="'+esc(p.name)+'"><b>'+esc(p.name.split('/').pop()||p.name)+
    '</b></td><td class="num">'+fmt(p.input)+'</td><td class="num">'+fmt(p.output)+
    '</td><td class="num">'+fmt(p.loc)+'</td><td class="num">'+costCell(p.cost)+
    '</td><td>'+(p.has_instructions?(cop?'<span class="up" title="Has '+insFiles+'">&#10003; instructions</span>'
      :'<span class="up">&#10003; CLAUDE.md</span>'):'<span class="flat-d">&mdash;</span>')+
    '</td><td class="num">'+ARROW+'</td></tr>').join('')
    :'<tr><td colspan="7" class="empty">No projects in this range.</td></tr>';
  document.querySelectorAll('#tProj tbody tr').forEach((tr,i)=>{
    if(!d.projects[i])return;
    tr.onclick=()=>openProject(d.projects[i]);
    tr.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openProject(d.projects[i]);}};});
  document.querySelector('#tBranch tbody').innerHTML=d.branches.length?d.branches.map(b=>
    '<tr><td><b>'+esc(b.name)+'</b></td><td class="num">'+b.msgs+'</td><td class="num">'+
    fmt(b.output)+'</td><td class="num">'+fmt(b.loc)+'</td></tr>').join('')
    :'<tr><td colspan="4" class="empty">No branch data in this range.</td></tr>';
  document.querySelector('#tFiles tbody').innerHTML=d.top_files.length?d.top_files.map(f=>
    '<tr><td>'+esc(pv(f[0]))+'</td><td class="num">'+fmt(f[1])+'</td></tr>').join('')
    :'<tr><td colspan="2" class="empty">No files edited in this range.</td></tr>';
  $('lPrompts').innerHTML=d.longest_prompts.length?d.longest_prompts.map(p=>
    '<li>'+esc(pv(p.preview))+'…<div class="mt"><b>'+p.lines+' lines</b> · '+p.words+
    ' words · '+p.day+'</div></li>').join('')
    :'<li class="empty">No prompts in this range.</li>';
}

/* ─────────────────────────── All AI ─────────────────────────── */
/* Both tools in one view. What means the same in each is added (tokens, prompts, sessions); time is the
   union of 10-minute slots; money is never added: each tool keeps its own unit. Series use the tool colours
   (Claude's own --claude token, Copilot's violet), whatever accent the page has. */
const toolClr=t=>({claude:C.claude,copilot:C.violet}[t]||C.slate);
const toolDot=t=>'<span class="tdot" style="background:'+toolClr(t)+'"></span>';
const r2=v=>Math.round(v*100)/100;
const NODASH='<span class="flat-d">&mdash;</span>';                // a missing figure is a dash, never a made-up zero
const allCost=(v,unit)=>v==null?NODASH:unit==='credits'?fmt(r2(v))+' cr':'$'+v;
const pctTxt=p=>p>0&&p<1?p.toFixed(2):''+Math.round(p);
function allKpi(k){                                  // [label, value html, sub, icon, colour, delta, spark values]
  const sp=k[6]&&k[6].some(v=>v>0)?spark(k[6],k[4]):'';
  return '<div class="kpi'+(sp?'':' plain')+'" style="--kc:'+k[4]+';--kbg:'+hexa(k[4],.13)+'">'+
    '<div class="khead"><span class="eyebrow">'+k[0]+'</span><span class="ic">'+ic(k[3])+'</span></div>'+
    k[1]+(k[5]?'<div class="kd">'+k[5]+'</div>':'')+'<div class="s">'+k[2]+'</div>'+sp+'</div>';}

function renderAllOverview(){
  const d=D,t=d.totals,bt=d.by_tool,tools=Object.keys(bt),pr=d.prev||{};
  const big=v=>'<div class="num-lg">'+v+'</div>';
  const per=f=>tools.map(k=>SRCN[k][0]+' '+f(bt[k])).join(' · ');
  const cost='<div class="costs">'+tools.map(k=>{const b=bt[k];
    return '<div class="cl"><span>'+SRCN[k][1]+'</span><b>'+(b.cost==null?NODASH:b.cost_unit==='credits'?fmt(r2(b.cost))+' credits'
      :'$'+(+b.cost).toFixed(2)+' est.')+'</b></div>'+
      (b.premium_requests>0?'<div class="cl sm"><span>'+fmt(r2(b.premium_requests))+' premium requests</span></div>':'');}).join('')+'</div>';
  const tokDay=d.days.map(x=>tools.reduce((a,k)=>a+(x.tokens_by_tool[k]||0),0));
  /* design 5.5: a tool whose sessions include estimated token counts says so beside its tokens */
  const apx=k=>{const m=bt[k].token_modes;return m&&m.approx>0?' · approx.':'';};
  $('aKpis').innerHTML=[
    ['Total tokens',big(fmt(t.tokens)),tools.map(k=>SRCN[k][0]+' '+fmt(bt[k].tokens)+apx(k)).join(' · '),
      'lever',C.coral,delta(t.tokens,pr.tokens),tokDay],
    ['Prompts',big(fmt(t.prompts)),per(b=>fmt(b.prompts)),'prompt',C.sky,delta(t.prompts,pr.prompts),null],
    ['Sessions',big(t.sessions),per(b=>b.sessions),'layers',C.emerald,delta(t.sessions,pr.sessions),null],
    ['Active time',big(fmtMin(t.active_min)),per(b=>fmtMin(b.active_min))+' · overlap counted once','session',C.amber,
      delta(t.active_min,pr.active_min),d.days.map(x=>x.active_min||0)],
    ['Active days',big(t.active_days),'days with any AI activity','cal',C.sky,'',null],
    ['Cost',cost,'Not added together: different units','cost',C.rose,'',null]].map(allKpi).join('');

  const labels=d.days.map(x=>x.day.slice(5));
  mk('cATokens',{type:'bar',data:{labels,datasets:tools.map(k=>
    ({label:SRCN[k][1]+apx(k),data:d.days.map(x=>x.tokens_by_tool[k]||0),backgroundColor:toolClr(k)}))},
    options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID,ticks:{...GRID.ticks,callback:v=>fmt(v)}}},
      plugins:{legend:{position:'bottom'},tooltip:{callbacks:{label:c=>c.dataset.label+': '+fmt(c.parsed.y)}}},
      interaction:{mode:'index',intersect:false}}});
  mk('cAShare',{type:'doughnut',data:{labels:tools.map(k=>SRCN[k][1]),
    datasets:[{data:tools.map(k=>bt[k].tokens),backgroundColor:tools.map(toolClr),
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'},tooltip:{callbacks:{
      label:c=>c.label+': '+fmt(c.parsed)+' ('+pctTxt(t.tokens?c.parsed/t.tokens*100:0)+'%)'}}}}});
}

function renderAllTools(){
  const d=D,nm=t=>SRCN[t]?SRCN[t][1]:t;
  /* each slice is a shade of its tool's colour: the models of one tool, fading from the full colour */
  const seen={},shade=m=>{const i=seen[m.tool]=(seen[m.tool]===undefined?0:seen[m.tool]+1);
    return hexa(toolClr(m.tool),Math.max(.3,1-.17*i).toFixed(2));};
  mk('cAModels',{type:'doughnut',data:{labels:d.models.map(m=>m.name+' · '+(SRCN[m.tool]?SRCN[m.tool][0]:m.tool)),
    datasets:[{data:d.models.map(m=>m.output),backgroundColor:d.models.map(shade),
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'}}}});
  document.querySelector('#tAModels tbody').innerHTML=d.models.length?d.models.map(m=>
    '<tr><td><b>'+esc(m.name)+'</b></td><td>'+toolDot(m.tool)+esc(nm(m.tool))+'</td><td class="num">'+m.msgs+
    '</td><td class="num">'+fmt(m.input)+'</td><td class="num">'+fmt(m.output)+
    '</td><td class="num">'+allCost(m.cost,m.cost_unit)+'</td></tr>').join('')
    :'<tr><td colspan="6" class="empty">No model activity in this range.</td></tr>';
}

/* one tool's slice of a project row, in the shape the project sheet reads */
function allProject(p){
  const tool=Object.keys(p.by_tool)[0],b=p.by_tool[tool];
  return {name:p.name,input:b.input,output:b.output,loc:b.loc,cost:b.cost,cost_unit:b.cost_unit,
    sessions:b.sessions,days:b.days||[],files:null,tool:tool};}
const allToks=b=>(b.input||0)+(b.cache_read||0)+(b.cache_write||0)+(b.output||0);   // same sum as totals.tokens
function renderAllProjects(){
  const d=D,dash='<span class="flat-d">&mdash;</span>';
  const tokCell=(p,k)=>'<td class="num">'+(p.by_tool[k]?fmt(allToks(p.by_tool[k])):dash)+'</td>';
  document.querySelector('#tAProj tbody').innerHTML=d.projects.length?d.projects.map(p=>{
    const one=Object.keys(p.by_tool).length===1,prompts=Object.values(p.by_tool).reduce((a,b)=>a+(b.prompts||0),0);
    return '<tr'+(one?' class="one" tabindex="0"':' title="Open it from Claude Code or GitHub Copilot"')+'><td title="'+esc(p.name)+'"><b>'+
      esc(p.name.split('/').pop()||p.name)+'</b></td><td class="num">'+fmt(p.tokens)+'</td>'+
      tokCell(p,'claude')+tokCell(p,'copilot')+'<td class="num">'+fmt(prompts)+'</td><td class="num">'+(one?ARROW:'')+'</td></tr>';}).join('')
    :'<tr><td colspan="6" class="empty">No projects in this range.</td></tr>';
  document.querySelectorAll('#tAProj tbody tr.one').forEach(tr=>{
    const p=d.projects[[...tr.parentNode.children].indexOf(tr)];
    tr.onclick=()=>openProject(allProject(p));
    tr.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openProject(allProject(p));}};});
}

/* ─────────────────────── Engineering insight tabs ─────────────────────── */
let PRIV=false;
try{PRIV=localStorage.getItem('dtd_priv')==='1';}catch(e){}
const pv=s=>PRIV?'•••':s;
function setPriv(v){PRIV=v;try{localStorage.setItem('dtd_priv',v?'1':'0');}catch(e){}
  if(D)render();paintPriv();}
function paintPriv(){const b=$('privBtn');if(b)b.textContent=PRIV?'Privacy mode: on':'Privacy mode: off';}

const fmtS=s=>s==null?'–':s<10?s.toFixed(1)+'s':s<60?Math.round(s)+'s':s<3600?Math.floor(s/60)+'m '+Math.round(s%60)+'s':Math.floor(s/3600)+'h '+Math.round(s%3600/60)+'m';
const CONF={observed:'Observed',derived:'Derived',proxy:'Proxy',estimated:'Estimated',na:'Not available'};
function cb(id){const m=D&&D.catalog&&D.catalog[id];if(!m)return'';
  return '<span class="cb '+m.confidence+'" title="'+esc(m.name+' · '+CONF[m.confidence]+'. '+m.how+' Limit: '+m.limit)+'">'+CONF[m.confidence]+'</span>';}
function insDelta(cur,prev,lowerBetter,f){
  if(cur==null||prev==null)return'';
  const d=cur-prev;if(Math.abs(d)<1e-9)return'<span class="flat-d">same as previous period</span>';
  const good=lowerBetter?d<0:d>0;
  return '<span class="'+(good?'up':'down')+'">'+(d>0?'▲ ':'▼ ')+(f?f(Math.abs(d)):Math.abs(Math.round(d*10)/10))+' vs previous period</span>';}
function tile(label,val,sub,color,cid,dl){
  return '<div class="kpi plain" style="--kc:'+color+';--kbg:'+hexa(color,.13)+'"><div class="khead"><span class="eyebrow">'+label+'</span>'+(cid?cb(cid):'')+
    '</div><div class="num-lg">'+val+'</div>'+(dl?'<div class="kd">'+dl+'</div>':'')+'<div class="s">'+sub+'</div></div>';}
function ringHtml(score,gid){
  const R=52,CIRC=2*Math.PI*R,off=CIRC*(1-score/100);
  return '<div class="ad-ring sm" style="--circ:'+CIRC.toFixed(1)+'"><svg viewBox="0 0 120 120"><defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="1" y2="1">'+
    '<stop offset="0" style="stop-color:var(--acc)"/><stop offset="1" style="stop-color:var(--acc-2)"/></linearGradient></defs>'+
    '<circle class="trk" cx="60" cy="60" r="'+R+'"/><circle class="val" cx="60" cy="60" r="'+R+'" stroke="url(#'+gid+')" stroke-dasharray="'+CIRC.toFixed(1)+'" stroke-dashoffset="'+off.toFixed(1)+'"/></svg>'+
    '<div class="mid"><div class="big">'+score+'</div><div class="of">out of 100</div></div></div>';}
function insHero(el,gid,eyebrow,cid,score,words,sentence,tiles,emptyMsg){
  if(score==null){
    el.innerHTML='<div class="ins-left"><div><div class="eyebrow">'+eyebrow+'</div><h2>Not enough data yet</h2><div class="ins-empty" style="margin-top:6px">'+emptyMsg+'</div></div></div><div class="ins-tiles">'+tiles+'</div>';return;}
  el.innerHTML='<div class="ins-left">'+ringHtml(score,gid)+'<div><div class="eyebrow">'+eyebrow+'</div><h2>'+words+'</h2><div class="d">'+sentence+'</div>'+cb(cid)+'</div></div><div class="ins-tiles">'+tiles+'</div>';}
const band=(sc,w)=>sc>=80?w[0]:sc>=60?w[1]:sc>=40?w[2]:w[3];
const P=(n)=>n==null?'–':n+'%';
const dpts=v=>Math.round(v)+' pts';

function renderFlow(){
  const f=D.flow,pr=(D.prev_insight||{}).flow||{};
  const tiles=
    tile('Median turn',fmtS(f.turn_median_s),'slowest 10%: '+fmtS(f.turn_p90_s),C.sky,'turn_time',insDelta(f.turn_median_s,pr.turn_median_s,true,fmtS))+
    tile('Clean first-pass turns',P(f.clean_pct),f.corrected+' corrected · '+(D.na&&D.na.interruptions?'':f.interrupted+' interrupted · ')+f.with_errors+' with errors',C.emerald,'clean_turns',insDelta(f.clean_pct,pr.clean_pct,false,dpts))+
    tile('Failures fixed',P(f.recovery_pct),f.episodes?f.recovered+' of '+f.episodes+' failures recovered · '+f.stuck+' stuck loop'+(f.stuck===1?'':'s'):'no failures in this range',C.amber,'recovery',insDelta(f.recovery_pct,pr.recovery_pct,false,dpts))+
    tile('Time to first edit',fmtS(f.first_edit_median_s),f.first_edit_n+' session'+(f.first_edit_n===1?'':'s')+' with edits',C.violet,'first_edit',insDelta(f.first_edit_median_s,pr.first_edit_median_s,true,fmtS));
  insHero($('flowHero'),'flowGrad','Flow score','flow_score',f.score,band(f.score||0,['Strong flow','Mostly smooth','Bumpy','Rough going']),
    f.clean_pct+'% of your turns landed first time, and '+(f.recovery_pct==null?'no failures needed fixing':f.recovery_pct+'% of failures got fixed')+'.',
    tiles,'Needs 5 timed turns in this range; you have '+f.turns+'. Pick a longer range or keep working.');
  const lb=f.series.map(x=>x.day.slice(5));
  mk('cFlowTurn',{type:'line',data:{labels:lb,datasets:[
    {label:'Median',data:f.series.map(x=>x.median),borderColor:C.sky,backgroundColor:areaFill(C.sky),fill:true},
    {label:'Slowest 10%',data:f.series.map(x=>x.p90),borderColor:C.slate,borderDash:[4,4],fill:false}]},
    options:{scales:{x:XGRID,y:{...GRID,ticks:{padding:8,callback:v=>fmtS(v)}}},plugins:{legend:{position:'bottom'},tooltip:{callbacks:{label:c=>c.dataset.label+': '+fmtS(c.parsed.y)}}},interaction:{mode:'index',intersect:false}}});
  mk('cFlowClean',{type:'bar',data:{labels:lb,datasets:[{label:'Clean turns %',data:f.series.map(x=>x.clean_pct),backgroundColor:C.emerald}]},
    options:{scales:{x:XGRID,y:{min:0,max:100,...GRID}},plugins:{legend:{display:false}}}});
  $('cbTurn').innerHTML=cb('turn_time');$('cbClean').innerHTML=cb('clean_turns')+' A correction is read from the start of your next prompt.';
  $('cbTool').innerHTML=cb('tool_time');$('cbRec').innerHTML=cb('recovery');
  $('flowToolNote').textContent=f.tool_share_pct==null?'':'Tools account for about '+f.tool_share_pct+'% of your turn time. The rest is '+TN()+' thinking and writing, plus any wait for your approval.';
  const tl=f.tools.slice(0,8);
  mk('cFlowTools',{type:'bar',data:{labels:tl.map(t=>t.family),datasets:[{label:'Share of tool time %',data:tl.map(t=>t.share_pct),backgroundColor:C.coral}]},
    options:{indexAxis:'y',scales:{x:{...GRID,ticks:{padding:6,callback:v=>v+'%'}},y:{grid:{display:false},border:{display:false}}},plugins:{legend:{display:false}}}});
  document.querySelector('#tFlowTools tbody').innerHTML=f.tools.length?f.tools.map(t=>
    '<tr><td><b>'+esc(t.family)+'</b></td><td class="num">'+t.calls+'</td><td class="num">'+fmtS(t.median_s)+'</td><td class="num">'+fmtS(t.p90_s)+'</td><td class="num">'+fmtS(t.max_s)+'</td><td class="num">'+t.share_pct+'%</td></tr>').join('')
    :'<tr><td colspan="6" class="empty">No timed tool calls in this range.</td></tr>';
  $('flowRecFacts').innerHTML=
    '<div><div class="num-lg">'+f.episodes+'</div><div class="fl">failure episodes</div></div>'+
    '<div><div class="num-lg">'+f.recovered+'</div><div class="fl">recovered</div></div>'+
    '<div><div class="num-lg">'+(f.recover_steps_median==null?'–':f.recover_steps_median)+'</div><div class="fl">steps to recover (median)</div></div>'+
    '<div><div class="num-lg">'+f.stuck+'</div><div class="fl">stuck loops (3+ in a row)</div></div>';
  $('flowFailTools').innerHTML=f.fail_tools.length?'<span class="chip2">Fails most:</span>'+f.fail_tools.map(x=>'<span class="chip2"><b>'+esc(x.tool)+'</b> ×'+x.episodes+'</span>').join(''):'';
  $('flowShapeFacts').innerHTML=
    '<div><div class="num-lg">'+(f.session_median_min==null?'–':fmtMin(Math.round(f.session_median_min)))+'</div><div class="fl">typical session '+cb('session_len')+'</div></div>'+
    '<div><div class="num-lg">'+(f.session_p90_min==null?'–':fmtMin(Math.round(f.session_p90_min)))+'</div><div class="fl">longest 10% of sessions</div></div>'+
    '<div><div class="num-lg">'+fmtS(f.thinking_median_s)+'</div><div class="fl">median thinking time '+cb('thinking')+'</div></div>'+
    '<div><div class="num-lg">'+(f.tool_share_pct==null?'–':f.tool_share_pct+'%')+'</div><div class="fl">of turn time in tools</div></div>';
  const tot=f.effort.reduce((a,x)=>a+x.n,0),ec=EFF;
  $('flowEffortBar').innerHTML=tot?f.effort.map((x,i)=>'<i style="width:'+(x.n/tot*100)+'%;background:'+ec[i%4]+'" title="'+esc(x.level)+'"></i>').join(''):'';
  $('flowEffortChips').innerHTML=tot?f.effort.map((x,i)=>'<span class="chip2"><b style="color:'+ec[i%4]+'">●</b> '+esc(x.level)+' <b>'+Math.round(x.n/tot*100)+'%</b></span>').join(''):'<span class="chip2">Effort level is not in your logs.</span>';
}

function renderQuality(){
  const q=D.quality,pr=(D.prev_insight||{}).quality||{};
  const tiles=
    tile('Verified after last edit',P(q.verified_pct),q.verified+' of '+q.edited_sessions+' edit sessions ended with a check',C.emerald,'verified',insDelta(q.verified_pct,pr.verified_pct,false,dpts))+
    tile('First-run pass rate',P(q.first_run_pass_pct),q.run_groups?q.run_groups+' session-and-check pairs':'no test, build or lint runs',C.sky,'first_run',insDelta(q.first_run_pass_pct,pr.first_run_pass_pct,false,dpts))+
    tile('Tests alongside code',P(q.tests_alongside_pct),q.with_tests+' of '+q.edited_sessions+' edit sessions touched a test file',C.violet,'tests_alongside',insDelta(q.tests_alongside_pct,pr.tests_alongside_pct,false,dpts))+
    tile('Re-edit share',P(q.reedit_pct),'of '+q.edits+' edits revisit a file edited earlier in the session',C.amber,'reedit',insDelta(q.reedit_pct,pr.reedit_pct,true,dpts));
  insHero($('qualHero'),'qualGrad','Quality score','quality_score',q.score,band(q.score||0,['Well checked','Reasonably checked','Lightly checked','Rarely checked']),
    q.verified_pct+'% of edit sessions ended with a test, build or lint, and '+q.tests_alongside_pct+'% touched a test file.',
    tiles,'Needs 3 sessions where '+TN()+' edited files in this range; you have '+q.edited_sessions+'.');
  const rk=Object.values(q.runs),KL={test:'Tests',build:'Builds',lint:'Lint and type checks'};
  document.querySelector('#tQRuns tbody').innerHTML=rk.length?rk.map(r=>
    '<tr><td><b>'+KL[r.kind]+'</b></td><td class="num">'+r.runs+'</td><td class="num">'+r.fails+'</td><td class="num">'+P(r.first_pass_pct)+'</td><td class="num">'+(r.fail_groups?r.recovered_groups+' of '+r.fail_groups:'–')+'</td></tr>').join('')
    :'<tr><td colspan="5" class="empty">No test, build or lint commands detected in this range.</td></tr>';
  mk('cQVerified',{type:'bar',data:{labels:q.weekly.map(w=>fmtD(w.start)),datasets:[{label:'Verified %',data:q.weekly.map(w=>w.pct),backgroundColor:C.emerald}]},
    options:{scales:{x:XGRID,y:{min:0,max:100,...GRID}},plugins:{legend:{display:false},tooltip:{callbacks:{afterBody:i=>{const w=q.weekly[i[0].dataIndex];return w.verified+' of '+w.edited+' edit sessions';}}}}}});
  $('cbHot').innerHTML=cb('hotspots');
  $('qualReFacts').innerHTML='<div><div class="num-lg">'+P(q.reedit_pct)+'</div><div class="fl">of edits are re-edits '+cb('reedit')+'</div></div><div><div class="num-lg">'+q.hotspots.length+'</div><div class="fl">hotspot files</div></div>';
  document.querySelector('#tQHot tbody').innerHTML=q.hotspots.length?q.hotspots.map(h=>
    '<tr><td><b>'+esc(pv(h.file))+'</b><div class="mt" style="font-size:.6875rem;color:var(--txt-3)">'+esc(h.project)+'</div></td><td class="num">'+h.edits+'</td><td class="num">'+h.days+'</td><td class="num">'+h.sessions+'</td></tr>').join('')
    :'<tr><td colspan="4" class="empty">No file was edited over and over in this range.</td></tr>';
  $('qualHotNote').textContent='A hotspot is a file edited 5+ times that was edited 3+ times in one session or on 3+ days. Some files are simply central to your work.';
  $('cbFric').innerHTML=cb('friction');
  const naI=!!(D.na&&D.na.interruptions);
  $('qualFricFacts').innerHTML=
    '<div><div class="num-lg">'+(q.errors_per_100_edits==null?'–':q.errors_per_100_edits)+'</div><div class="fl">failed tool results per 100 edits</div></div>'+
    (naI?'<div title="'+esc(D.na.interruptions)+'"><div class="num-lg">–</div><div class="fl">interruptions per 100 prompts · not available for '+TN()+'</div></div>'
        :'<div><div class="num-lg">'+(q.interrupts_per_100_prompts==null?'–':q.interrupts_per_100_prompts)+'</div><div class="fl">interruptions per 100 prompts</div></div>')+
    '<div><div class="num-lg">'+q.tool_errors+'</div><div class="fl">failed tool results</div></div>'+
    (naI?'<div title="'+esc(D.na.interruptions)+'"><div class="num-lg">–</div><div class="fl">interruptions · not available for '+TN()+'</div></div>'
        :'<div><div class="num-lg">'+q.interruptions+'</div><div class="fl">interruptions</div></div>');
  $('cbSafe').innerHTML=cb('safety');
  const SL={force_push:'Force-pushes',hard_reset:'Hard resets',git_clean:'git clean -f',recursive_delete:'Recursive deletes',pipe_to_shell:'Downloads piped into a shell',skip_hooks:'Skipped git hooks',wide_permissions:'chmod 777',sql_drop:'SQL drops',secret_file_touched:'Secret-like files read or edited'};
  const sk=Object.keys(q.safety);
  $('qualSafety').innerHTML=sk.length?'<ul class="lim">'+sk.map(k=>'<li><b>'+(SL[k]||k)+'</b> ×'+q.safety[k]+'</li>').join('')+'</ul>'
    :'<div class="ad-allgood">'+ic('check').replace('<svg','<svg width="18" height="18" style="color:var(--emerald);flex:none"')+'No risky commands or secret-file touches detected in this range.</div>';
  $('qualPerms').innerHTML=q.permissions.length?q.permissions.map(p=>'<span class="chip2"><b>'+esc(p.decision)+'</b> · '+esc(p.source)+' ×'+p.n+'</span>').join('')
    :'<span class="chip2">Permission decisions are not in your logs.</span>';
}

function renderValue(){
  const v=D.value,pr=(D.prev_insight||{}).value||{};
  const cr=credits(),noCache=!!(D.na&&D.na.cold_start_pct);     // Copilot bills credits per request: no cache pricing
  const money=x=>fmtCost(Math.round(x*100)/100,2);
  const cacheNa='Copilot bills credits per request, so there is no cache saving to price.';
  const tiles=
    tile(cr?'Credits used':'Estimated cost',v.ready?money(v.cost):'–',cr?'AI credits, as logged':'at list prices; $0 on a subscription',C.amber,'cost',insDelta(v.cost,pr.cost,false,money))+
    tile('Per 100 lines',v.per_100_loc==null?'–':money(v.per_100_loc),fmt(v.loc)+' lines written',C.violet,'cost_100_loc',insDelta(v.per_100_loc,pr.per_100_loc,true,money))+
    tile('Per verified session',v.cost_per_verified==null?'–':money(v.cost_per_verified),v.verified_sessions+' edit session'+(v.verified_sessions===1?'':'s')+' ended with a check',C.emerald,'cost_verified',insDelta(v.cost_per_verified,pr.cost_per_verified,true,money))+
    (noCache?naCard('Caching saved',cacheNa,'cache')
      :tile('Caching saved',v.ready?money(v.cache_savings):'–',v.cache_saved_pct+'% of what uncached would cost',C.teal,'cache_savings',insDelta(v.cache_savings,pr.cache_savings,false,money)));
  const el=$('valHero');
  el.innerHTML='<div class="ins-left"><div><div class="eyebrow">What it costs, and what you get back</div><h2>'+(v.ready?money(v.cost)+(cr?' used':' estimated'):'No cost data yet')+'</h2><div class="d">'+
    (v.ready?'Median session '+(v.session_median_cost==null?'–':money(v.session_median_cost))+' · '+v.sessions+' sessions. '+(cr?'Credits are as '+TN()+'’s logs report them; none are guessed from model prices.':'On a subscription plan your real marginal cost is $0; this shows the value you are getting.'):'Nothing was recorded in this range.')+'</div></div></div><div class="ins-tiles">'+tiles+'</div>';
  $('valDailyT').textContent=cr?'Daily credits':'Daily cost';
  $('valDailyH').textContent=cr?'credits as logged; spikes in red':'estimated at list prices; spikes in red';
  $('valWeeklyT').textContent=cr?'Credits per 100 lines':'Cost per 100 lines';
  $('valNote').textContent=cr?'No score on this tab by design: a credit figure has no universal "good". Each number is compared with your own previous period.'
    :'No score on this tab by design: a dollar figure has no universal "good". Each number is compared with your own previous period.';
  const tick=x=>fmtCost(Math.round(x*100)/100);
  mk('cValDaily',{type:'bar',data:{labels:v.daily.map(x=>x.day.slice(5)),datasets:[{label:cr?'Credits used':'Estimated cost $',data:v.daily.map(x=>x.cost),backgroundColor:v.daily.map(x=>x.spike?C.rose:C.amber)}]},
    options:{scales:{x:XGRID,y:{...GRID,ticks:{padding:8,callback:tick}}},plugins:{legend:{display:false}}}});
  mk('cValWeekly',{type:'line',data:{labels:v.weekly.map(w=>fmtD(w.start)),datasets:[{label:cr?'Credits per 100 lines':'$ per 100 lines',data:v.weekly.map(w=>w.per_100_loc),borderColor:C.violet,backgroundColor:areaFill(C.violet),fill:true,spanGaps:true,pointRadius:3}]},
    options:{scales:{x:XGRID,y:{...GRID,ticks:{padding:8,callback:tick}}},plugins:{legend:{display:false}}}});
  $('cbCache').innerHTML=noCache?'':cb('cache_savings');
  $('valCacheNote').hidden=noCache;
  $('valCacheFacts').innerHTML=noCache?naCard('Cache savings and cold-start tax',D.na.cold_start_pct,'cache'):
    '<div><div class="num-lg">'+(v.ready?money(v.cache_savings):'–')+'</div><div class="fl">saved by caching</div></div>'+
    '<div><div class="num-lg">'+(v.cold_start_pct==null?'–':v.cold_start_pct+'%')+'</div><div class="fl">cold-start tax '+cb('cold_start')+'</div></div>'+
    '<div><div class="num-lg">'+(v.cache_reset_pct==null?'–':v.cache_reset_pct+'%')+'</div><div class="fl">calls that rebuilt the cache</div></div>';
  $('cbMix').innerHTML=cb('model_mix');
  $('thValCost').textContent=cr?'Credits':'Cost';
  document.querySelector('#tValModels tbody').innerHTML=v.models.length?v.models.map(m=>
    '<tr><td><b>'+esc(m.model)+'</b></td><td class="num">'+money(m.cost)+'</td><td class="num">'+m.share_pct+'%</td><td class="num">'+m.sessions+'</td></tr>').join('')
    :'<tr><td colspan="4" class="empty">No model usage in this range.</td></tr>';
  $('valMixNote').innerHTML=v.top_tier_nocode_pct!=null?'<b>'+v.top_tier_nocode_pct+'%</b> of top-tier-model cost went to sessions where '+TN()+' made no edits '+cb('top_tier_nocode')+'.':'';
  $('cbSpike').innerHTML=cb('cost_spikes');
  $('valSpikeCard').style.display=v.spikes.length?'':'none';
  $('valSpikes').innerHTML=v.spikes.map(s=>'<li><b>'+fmtD(s.day)+' · '+money(s.cost)+'</b> ('+(s.x_typical||'?')+'× a typical day)<div class="mt">Biggest session: '+esc(pv(s.session||''))+' ('+money(s.session_cost||0)+')</div></li>').join('');
}

/* Advisor: rule-based recommendations, plus one experiment kept in this browser */
const EXP_KEY='dtd_experiment';
const getExp=()=>{try{return JSON.parse(localStorage.getItem(EXP_KEY)||'null');}catch(e){return null;}};
const putExp=v=>{try{if(v)localStorage.setItem(EXP_KEY,JSON.stringify(v));else localStorage.removeItem(EXP_KEY);}catch(e){}};
const MLABEL={verified_pct:'Edit sessions verified',first_run_pass_pct:'First-run pass rate',clean_pct:'Clean first-pass turns',stuck:'Stuck loops',reedit_pct:'Re-edit share',tests_alongside_pct:'Tests alongside code',recovery_pct:'Failures fixed',cold_start_pct:'Cold-start tax',cache_reset_pct:'Cache rebuilds',per_100_loc:'Cost per 100 lines'};
const AREAC=()=>({quality:C.emerald,flow:C.sky,value:C.amber,habit:C.emerald,range:C.sky,craft:C.violet,impact:C.amber});
function maskQuoted(t){return PRIV?String(t).replace(/"[^"]*"/g,'"•••"'):t;}

function renderAdvisor(){
  const A=D.advisor,ac=AREAC();
  if(!A||!A.ready){$('advRecs').innerHTML='<div class="ins-empty">Not enough activity yet. Keep using '+TF()+' and this page fills in on its own.</div>';
    $('advWins').innerHTML='';$('advMore').innerHTML='';$('advExp').innerHTML='';return;}
  $('advWindow').textContent=fmtD(A.start)+' – '+fmtD(A.end);
  const exp=getExp();
  $('advRecs').innerHTML=A.recs.length?A.recs.map(r=>{
    const k=ac[r.area]||C.coral,active=exp&&exp.id===r.id;
    return '<div class="ad-step" style="--kc:'+k+';--kbg:'+hexa(k,.13)+'"><div class="meta"><span class="nb">'+r.rank+'</span><span class="cb '+r.confidence+'">'+CONF[r.confidence]+'</span></div>'+
      '<h4>'+esc(r.title)+'</h4><div class="why">'+esc(maskQuoted(r.evidence))+'</div><div class="how">'+esc(r.action)+'</div>'+
      (r.experiment?'<div class="acts"><button class="btn'+(active?'':' accent')+'" data-exp="'+esc(r.id)+'"'+(active?' disabled':'')+'>'+(active?'Experiment running':'Try it as an experiment')+'</button></div>':'')+'</div>';}).join('')
    :'<div class="ad-allgood">'+ic('check').replace('<svg','<svg width="18" height="18" style="color:var(--emerald);flex:none"')+'Nothing stands out. Your data looks healthy in this window.</div>';
  const recById={};A.recs.concat(A.more).forEach(r=>recById[r.id]=r);
  if(exp){
    const cur=A.metrics[exp.metric],base=exp.baseline,better=exp.better;
    const d=(cur!=null&&base!=null)?cur-base:null,good=d==null?null:(better==='down'?d<0:d>0);
    const days=Math.max(0,Math.round((Date.now()-new Date(exp.started).getTime())/864e5));
    $('advExp').innerHTML='<div class="exp-card"><div style="flex:1;min-width:240px"><div class="eyebrow">Running · day '+days+'</div><h4 style="font-size:.9375rem;margin:4px 0 6px">'+esc(exp.label)+'</h4>'+
      '<div class="ad-block" style="color:var(--txt-3);font-weight:500">Tracking: '+esc(MLABEL[exp.metric]||exp.metric)+'. The figure uses a rolling 4-week window, so it moves slowly; give it a week or two.</div></div>'+
      '<div><div class="eyebrow">Before</div><div class="big">'+(base==null?'–':base)+'</div></div><div><div class="eyebrow">Now</div><div class="big">'+(cur==null?'–':cur)+'</div></div>'+
      '<div><div class="eyebrow">Change</div><div class="big '+(good==null?'':good?'up':'down')+'">'+(d==null?'–':(d>0?'+':'')+Math.round(d*10)/10)+'</div></div>'+
      '<button class="btn" id="expStop">Stop experiment</button></div>';
    $('expStop').onclick=()=>{putExp(null);renderAdvisor();};
  }else $('advExp').innerHTML='<div class="ins-empty">No experiment running. Pick <b>Try it as an experiment</b> on any recommendation above. The dashboard records where that number is now, and shows how it moves.</div>';
  document.querySelectorAll('[data-exp]').forEach(b=>b.onclick=()=>{
    const r=recById[b.dataset.exp];if(!r||!r.experiment)return;
    putExp({id:r.id,label:r.experiment.label,metric:r.experiment.metric,better:r.experiment.better,
      baseline:A.metrics[r.experiment.metric],started:new Date().toISOString()});renderAdvisor();});
  $('advWins').innerHTML=A.wins.length?A.wins.map(w=>'<li>'+esc(w)+'</li>').join(''):'<li>Wins show up here as your numbers improve.</li>';
  $('advMore').innerHTML=A.more.length?'<ul class="lim">'+A.more.map(r=>'<li><b>'+esc(r.title)+'</b>: '+esc(maskQuoted(r.evidence))+'</li>').join('')+'</ul>':'<div class="ins-empty">Nothing else of note.</div>';
}

function renderHealth(){
  const H=D.health;
  /* the "turn on exact tokens" hint is a command and a setting name: set them apart */
  $('healthCaps').innerHTML=H.capabilities.map(c=>'<div class="it '+c.status+'"><span class="dot"></span><div><b>'+esc(c.label)+'</b><span>'+
    esc(c.detail).replace(/(python ai_fluency_insights\.py --setup-copilot|github\.copilot\.chat\.agentDebugLog\.fileLogging\.enabled)/g,'<code>$1</code>')+'</span></div></div>').join('');
  const unk=(H.unknown_types||[]).reduce((a,x)=>a+x.n,0);
  $('healthMeta').innerHTML=
    '<span class="chip2">Sessions <b>'+H.sessions+'</b></span>'+
    (H.versions.length?'<span class="chip2">'+TF()+' '+H.versions.map(v=>'<b>'+esc(v.version)+'</b>').join(', ')+'</span>':'')+
    (H.entrypoints.length?'<span class="chip2">Via '+H.entrypoints.map(v=>'<b>'+esc(v.name)+'</b>').join(', ')+'</span>':'')+
    '<span class="chip2'+(H.parse_errors?' warn':'')+'">Unparsed lines <b>'+H.parse_errors+'</b></span>'+
    (H.bad_lines?'<span class="chip2 warn">Bad log lines skipped <b>'+H.bad_lines+'</b></span>':'')+
    (unk?'<span class="chip2 warn" title="'+esc(H.unknown_types.map(x=>x.type+' ×'+x.n).join(', '))+'">Unrecognised log records: <b>'+unk+'</b>, new '+TN()+' version?</span>':'')+
    '<span class="chip2">Duplicate lines dropped <b>'+H.duplicate_lines+'</b></span>';
  document.querySelector('#tHealth tbody').innerHTML=H.entry_types.map(t=>'<tr><td><code>'+esc(t.type)+'</code></td><td class="num">'+t.n.toLocaleString()+'</td></tr>').join('');
  $('healthLimits').innerHTML=H.limits.map(l=>'<li>'+esc(l)+'</li>').join('');
  const sel=$('catTab'),tabs=[...new Set(Object.values(D.catalog).map(m=>m.tab))];
  if(sel.options.length===1)tabs.forEach(t=>{const o=document.createElement('option');o.value=t;o.textContent=t.charAt(0).toUpperCase()+t.slice(1);sel.appendChild(o);});
  const f=sel.value;
  document.querySelector('#tCat tbody').innerHTML=Object.values(D.catalog).filter(m=>!f||m.tab===f).map(m=>
    '<tr><td><b>'+esc(m.name)+'</b></td><td>'+esc(m.tab)+'</td><td><span class="cb '+m.confidence+'">'+CONF[m.confidence]+'</span></td><td class="wrapc">'+esc(m.how)+'</td><td class="wrapc">'+esc(m.limit)+'</td></tr>').join('');
  paintPriv();
}
$('catTab').onchange=()=>{if(D)renderHealth();};
$('privBtn').onclick=()=>setPriv(!PRIV);
document.addEventListener('click',e=>{const a=e.target.closest('[data-go]');if(a)setSection(a.dataset.go);});

/* ─────────────────────────── My Adoption ─────────────────────────── */
const AD_COL=()=>({habit:C.emerald,range:C.sky,craft:C.violet,impact:C.amber});
const AD_ICON={habit:'cal',range:'layers',craft:'target',impact:'lever'};
const adDelta=(cur,prev,unit)=>{
  if(prev==null)return'';
  const d=cur-prev;
  if(d===0)return'<span class="flat-d">same as the 4 weeks before</span>';
  return '<span class="'+(d>0?'up':'down')+'">'+(d>0?'▲ ':'▼ ')+Math.abs(d)+(unit||'')+' vs the 4 weeks before</span>';};

function renderAdoption(){
  const A=D.adoption,empty=!A||!A.ready;
  $('adoEmpty').hidden=!empty;$('adoFull').hidden=empty;
  if(empty){
    const n=A?A.active_days:0,need=A?A.min_days:3;
    $('adoEmpty').innerHTML='<div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+IC.spark+'</svg></span><h3>Building your baseline</h3></div>'+
      '<div style="font-size:.9375rem;color:var(--txt);line-height:1.6;max-width:560px">Your Adoption Score needs at least <b>'+need+' active days</b> in the last 4 weeks to be fair to you. You have <b>'+n+'</b> so far — keep using '+TF()+' and this page fills in on its own.</div>'+
      '<div class="bar ad-prog" style="max-width:420px"><i style="width:'+Math.min(100,Math.round(n/need*100))+'%"></i></div>'+
      '<div class="note" style="margin-top:0">Nothing is sent anywhere. This is computed from the logs on your machine.</div>';
    return;}
  const col=AD_COL(),L=A.level,pl={};A.pillars.forEach(p=>pl[p.id]=p);
  const R=52,CIRC=2*Math.PI*R,off=CIRC*(1-A.score/100);
  /* ladder: each step fills as the score climbs through it */
  const steps=L.all.map((s,i)=>{
    const hi=i+1<L.all.length?L.all[i+1].floor:100;
    const fill=Math.max(0,Math.min(100,(A.score-s.floor)/(hi-s.floor)*100));
    return '<div class="st'+(i===L.index?' on':'')+'"><i><b style="width:'+Math.round(fill)+'%"></b></i>'+s.name+'</div>';}).join('');
  let chg='';
  if(A.prev_score!=null){const d=A.score-A.prev_score;
    chg='<span class="ad-chg '+(d>0?'up':d<0?'down':'')+'">'+(d>0?'▲ +'+d:d<0?'▼ '+d:'No change')+' vs the 4 weeks before</span>';}
  else chg='<span class="ad-chg">First full window — nothing to compare with yet</span>';
  const nxt=L.next_name?'<b>'+L.to_next+' point'+(L.to_next===1?'':'s')+'</b> to reach '+L.next_name:'You’ve reached the top level — keep it steady';
  const H=A.habit,Rg=A.range,I=A.impact;
  const summary='In the last '+A.window_days+' days you were active on <b>'+H.active_days+'</b> days, used <b>'+Rg.used+' of '+Rg.total+'</b> '+TF()+' features, and '+TN()+' wrote <b>'+fmt(I.loc)+'</b> lines for you. '+
    'Strongest pillar: <b>'+pl[A.strength].label+'</b>. Most room to grow: <b>'+pl[A.growth].label+'</b>.';
  const tiles=A.pillars.map(p=>{
    const c=col[p.id],tag=p.id===A.strength?'<span class="ad-tag">Strongest</span>':(p.id===A.growth?'<span class="ad-tag grow">Room to grow</span>':'');
    return '<div class="ad-pill" style="--kc:'+c+';--kbg:'+hexa(c,.13)+'">'+
      '<div class="khead"><span class="eyebrow">'+p.label+'</span><span class="ic">'+ic(AD_ICON[p.id])+'</span></div>'+
      '<div class="sc"><span class="num-lg">'+p.score+'</span><small>/ 100</small>'+(tag?'<span style="margin-left:auto">'+tag+'</span>':'')+'</div>'+
      '<div class="bar"><i style="width:'+p.score+'%"></i></div>'+
      '<div class="bl">'+p.blurb+'</div>'+
      (p.prev!=null?'<div class="dl">'+adDelta(p.score,p.prev)+'</div>':'')+'</div>';}).join('');
  $('adoHero').innerHTML=
    '<div><div class="ad-top"><div class="ad-ring'+(adoAnim?' anim':'')+'" style="--circ:'+CIRC.toFixed(1)+'">'+
      '<svg viewBox="0 0 120 120"><defs><linearGradient id="adGrad" x1="0" y1="0" x2="1" y2="1">'+
      '<stop offset="0" style="stop-color:var(--acc)"/><stop offset="1" style="stop-color:var(--acc-2)"/></linearGradient></defs>'+
      '<circle class="trk" cx="60" cy="60" r="'+R+'"/>'+
      '<circle class="val" cx="60" cy="60" r="'+R+'" stroke="url(#adGrad)" stroke-dasharray="'+CIRC.toFixed(1)+'" stroke-dashoffset="'+off.toFixed(1)+'"/></svg>'+
      '<div class="mid"><div class="big">'+A.score+'</div><div class="of">out of 100</div></div></div>'+
      '<div class="ad-lvl"><div class="eyebrow">Your level</div><h2>'+L.name+'</h2><div class="d">'+L.desc+'</div>'+chg+'</div></div>'+
      '<div class="ad-ladder">'+steps+'</div><div class="ad-next">'+nxt+'</div>'+(L.blocker?'<div class="ad-block">'+esc(L.blocker)+'</div>':'')+'</div>'+
    '<div class="ad-pills">'+tiles+'</div>'+
    '<div class="ad-sum">'+summary+'</div>';
  adoAnim=false;

  $('adoHl').innerHTML=A.highlights.map(h=>'<li class="'+h.kind+'"><span class="dt"></span><span>'+esc(h.text)+'</span></li>').join('');
  $('adoFunnel').innerHTML=A.funnel.map((x,i)=>'<div class="fs'+(x.achieved?' on':'')+'"><div class="n">'+(x.achieved?'✓':i+1)+'</div><b>'+esc(x.label)+'</b><span>'+esc(x.detail)+'</span></div>').join('');

  /* coach */
  const AREA={habit:'habit',range:'range',craft:'craft',impact:'impact',wellbeing:'habit'};
  $('adoCoach').innerHTML=A.coach.length?A.coach.map((c,i)=>{
    const k=col[AREA[c.area]]||C.coral;
    return '<div class="ad-step" style="--kc:'+k+';--kbg:'+hexa(k,.13)+'"><div class="top"><span class="nb">'+(i+1)+'</span><h4>'+esc(c.title)+'</h4></div>'+
      '<div class="why">'+esc(c.why)+'</div><div class="how">'+esc(c.how)+'</div></div>';}).join('')
    :'<div class="ad-allgood">'+ic('check').replace('<svg','<svg width="18" height="18"')+'You’re covering the bases — nothing to fix right now. Keep going.</div>';

  /* habit */
  $('adoHabitFacts').innerHTML=
    '<div><div class="num-lg">'+H.active_days+'<span style="font-size:.8125rem;color:var(--txt-3);font-weight:520"> / '+A.window_days+'</span></div><div class="fl">active days</div><div class="fd">'+adDelta(H.active_days,H.prev_active_days)+'</div></div>'+
    '<div><div class="num-lg">'+H.weeks_covered+'<span style="font-size:.8125rem;color:var(--txt-3);font-weight:520"> / '+H.weeks+'</span></div><div class="fl">weeks with 2+ active days</div></div>'+
    '<div><div class="num-lg">'+H.streak+'</div><div class="fl">day streak · best '+H.best_streak+'</div></div>';
  const cal=A.calendar,cs=cal.start.split('-').map(Number),mx=[];
  let mh='',ch='';
  for(let c=0;c<17;c++){
    const d0=new Date(cs[0],cs[1]-1,cs[2]+c*7);
    mh+='<span>'+((c===0||d0.getDate()<=7)?MON[d0.getMonth()]:'')+'</span>';}
  cal.cells.forEach((m,i)=>{
    if(m<0){ch+='<div class="ad-c fut"></div>';return;}
    const d=new Date(cs[0],cs[1]-1,cs[2]+i),a=m===0?0:m<40?.24:m<90?.46:m<180?.72:.95;
    ch+='<div class="ad-c" style="'+(a?'background:'+hexa(C.emerald,a):'')+'" title="'+MON[d.getMonth()]+' '+d.getDate()+' · '+(m?fmtMin(m)+' active':'no activity')+'"></div>';});
  $('adoCal').innerHTML='<div class="ad-calm" style="margin-left:32px">'+mh+'</div><div class="ad-calb"><div class="ad-calw"><span>Mon</span><span></span><span>Wed</span><span></span><span>Fri</span><span></span><span></span></div><div class="ad-calg">'+ch+'</div></div>';
  [['adg1',.24],['adg2',.46],['adg3',.72],['adg4',.95]].forEach(x=>{const e=$(x[0]);if(e)e.style.background=hexa(C.emerald,x[1]);});

  mk('cAdoWeeks',{type:'bar',data:{labels:A.weekly.map(w=>fmtD(w.start)),datasets:[
    {label:'Active days',data:A.weekly.map(w=>w.active_days),backgroundColor:C.emerald}]},
    options:{scales:{x:XGRID,y:{min:0,max:7,...GRID,ticks:{stepSize:1,padding:6}}},plugins:{legend:{display:false}}}});

  /* range */
  $('adoRangeHint').textContent=Rg.used+' of '+Rg.total+' in the last '+A.window_days+' days';
  $('adoFeats').innerHTML=Rg.features.map(f=>{
    const on=f.recent>0,lapsed=!on&&f.ever,cls=on?'on':(lapsed?'off lapsed':'off');
    let st;
    if(f.id==='memory')st=on?'CLAUDE.md found in '+f.recent+' active project'+(f.recent===1?'':'s'):'No CLAUDE.md in your active projects';
    else st=on?'Used on '+f.recent+' of the last '+A.window_days+' days':(lapsed?'Used before · not in the last '+A.window_days+' days':'Not tried yet');
    return '<div class="ad-feat '+cls+'"><span class="fi"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">'+(on?IC.check:IC.plus)+'</svg></span>'+
      '<div><b>'+esc(f.label)+'</b><span>'+esc(f.desc)+'</span><em>'+st+'</em>'+(on?'':'<span class="tipl">'+esc(f.tip)+'</span>')+'</div></div>';}).join('');

  /* craft */
  $('adoCraft').innerHTML=A.craft.map(d=>
    '<div class="flrow"><span class="fln">'+d.label+'</span>'+
    '<span class="flb"><span class="bar"><i style="width:'+d.score+'%"></i></span></span>'+
    '<span class="fls">'+d.score+(d.prev!=null&&d.prev!==d.score?'<small class="'+(d.score>d.prev?'up':'down')+'">'+(d.score>d.prev?'▲':'▼')+Math.abs(d.score-d.prev)+'</small>':'')+'</span>'+
    '<span class="flt">'+esc(d.detail)+'<br>'+(d.link?(d.score>=70?'Looking good. ':'')+'<a class="lnk" data-go="'+d.link+'">Open the '+d.label+' tab →</a>':(d.score<70?esc(d.tip):'Looking good.'))+'</span></div>').join('');

  /* impact */
  $('adoImpactFacts').innerHTML=
    '<div><div class="num-lg">'+fmt(I.loc)+'</div><div class="fl">lines '+TN()+' wrote</div><div class="fd">'+(I.prev_loc!=null?delta(I.loc,I.prev_loc).replace('vs last period','vs 4 weeks before'):'')+'</div></div>'+
    '<div><div class="num-lg">'+I.code_day_pct+'%</div><div class="fl">of active days shipped code</div></div>'+
    '<div><div class="num-lg">'+I.leverage+'×</div><div class="fl">leverage</div></div>'+
    '<div><div class="num-lg">'+I.verified_pct+'%</div><div class="fl">of code sessions verified</div></div>';
  mk('cAdoLoc',{type:'bar',data:{labels:A.weekly.map(w=>fmtD(w.start)),datasets:[
    {label:'Lines of code',data:A.weekly.map(w=>w.loc),backgroundColor:C.amber}]},
    options:{scales:{x:XGRID,y:GRID},plugins:{legend:{display:false},tooltip:{callbacks:{
      afterBody:i=>{const w=A.weekly[i[0].dataIndex];return w.active_days+' active day'+(w.active_days===1?'':'s')+' · '+w.sessions+' session'+(w.sessions===1?'':'s');}}}}}});

  /* trend */
  const names={};L.all.forEach(x=>{names[x.floor]=x.floor?x.name:'';});
  const stops=L.all.map(x=>x.floor);
  mk('cAdoTrend',{type:'line',data:{labels:A.history.map(h=>fmtD(h.end)),datasets:[{label:'Adoption Score',
    data:A.history.map(h=>h.score),borderColor:C.coral,backgroundColor:areaFill(C.coral),fill:true,
    pointRadius:3,pointBackgroundColor:C.coral,pointBorderColor:C.fill,pointBorderWidth:2}]},
    options:{scales:{x:XGRID,y:{min:0,max:100,...GRID,afterBuildTicks:sc=>{sc.ticks=stops.map(v=>({value:v}));},
      ticks:{padding:8,callback:v=>names[v]!==undefined?names[v]:v}}},plugins:{legend:{display:false}}}});

  /* wellbeing */
  const W=A.wellbeing;
  $('adoWell').innerHTML=
    '<div><div class="num-lg">'+fmtMin(W.avg_active_min)+'</div><div class="fl">active per active day</div></div>'+
    '<div><div class="num-lg">'+fmtMin(W.longest_focus_min)+'</div><div class="fl">longest focus block</div></div>'+
    '<div><div class="num-lg">'+W.late_pct+'%</div><div class="fl">late-night (11pm–6am)</div></div>'+
    '<div><div class="num-lg">'+W.weekend_pct+'%</div><div class="fl">on weekends</div></div>';
  $('adoWellNote').innerHTML=W.late_pct>20?ic('alert').replace('<svg','<svg width="18" height="18" style="color:var(--amber);flex:none"')+'A lot of your activity is late at night. '+TN()+' will still be there tomorrow.'
    :(W.weekend_pct>35?ic('alert').replace('<svg','<svg width="18" height="18" style="color:var(--amber);flex:none"')+'A third of your activity lands on weekends. Make sure you’re getting real breaks.'
    :ic('check').replace('<svg','<svg width="18" height="18" style="color:var(--emerald);flex:none"')+'Your rhythm looks sustainable. Steady daytime use beats late-night bursts.');

  /* how it works */
  const wt=A.weights,T=A.targets;
  $('adoHow').innerHTML=[
    ['Habit · '+Math.round(wt.habit*100)+'% of your score','Counts how many days you used '+TF()+' (full marks at '+T.target_days+' of the last '+A.window_days+', about 3 a week) and how evenly they spread across the 4 weeks.'],
    ['Range · '+Math.round(wt.range*100)+'%','Counts which '+TF()+' features you actually used (full marks at '+T.features_full+' of '+Rg.total+'), plus how many sessions were substantial ('+T.substantial_actions+'+ tool actions).'],
    ['Craft · '+Math.round(wt.craft*100)+'%','Mostly your four AI-fluency skills (Delegation, Description, Discernment, Diligence, as in the Weekly Report). When there is enough data, 20% each from your Flow and Quality scores are blended in.'],
    ['Impact · '+Math.round(wt.impact*100)+'%','How many lines '+TN()+' wrote per active day (full marks at '+T.loc_target+') and on how many of your active days it shipped code.'],
    ['Levels','Explorer 0–19 · Practitioner 20–39 · Power user 40–59 · Expert 60–74 · Pro 75–89 · Pro Max 90+. Pro also needs every pillar at 50+; Pro Max needs every pillar at 70+ and weekly scores of 85+ for 4 weeks running. Scores compare you with your own past, never with other people.'],
    ['Good to know','All heuristics, computed locally from your '+TF()+' logs. Trends matter more than the exact number. Formulas and thresholds live in docs/METRICS.md and the ADOPTION table in the script.']
  ].map(x=>'<div class="it"><b>'+x[0]+'</b>'+x[1]+'</div>').join('');
}

/* ─────────────────────────── Weekly report ─────────────────────────── */
function buildPct(w){const t=w.reads+w.writes;return t?Math.round(w.writes/t*100):0;}

function fluencyOf(w){
  const sess=w.sessions,code=sess.filter(s=>s.loc>0);
  const lev=w.human_tokens?w.output/w.human_tokens:0;
  const codeShare=sess.length?code.length/sess.length:0;
  const delegation=Math.round(100*(.5*codeShare+.5*Math.min(1,lev/50)));
  const avgW=w.prompts?(w.prompt_words||0)/w.prompts:0;
  const oneShot=w.prompts?Math.max(0,1-((w.corrections||0)+w.interruptions)/w.prompts):0;
  const sweet=avgW<=0?0:avgW<8?avgW/8:avgW<=150?1:Math.max(0,1-(avgW-150)/300);
  const description=Math.round(100*(.65*oneShot+.35*sweet));
  const engaged=Math.min(1,((w.corrections||0)+w.interruptions)/Math.max(1,w.prompts*.05));
  const errCtl=Math.max(0,1-w.tool_errors/Math.max(1,w.writes||1));
  const discernment=Math.round(100*(.5*engaged+.5*errCtl));
  const verified=code.length?code.filter(s=>s.bash>0).length/code.length:0;
  const diligence=code.length?Math.round(100*verified):0;
  return{delegation,description,discernment,diligence,lev,codeShare,avgW,oneShot,engaged,verified,ncode:code.length};}

function fluencyTips(f){return{
  delegation:f.delegation<50?'Try handing '+TN()+' whole tasks (implement + verify), not just questions.':'Healthy mix of asking and delegating real work.',
  description:f.description<50?'Front-load constraints, file paths and expected behavior in the first prompt.':'Most prompts land without needing a correction.',
  discernment:f.engaged<.3?'You rarely push back — spot-check diffs and say so when output misses.':(f.discernment<50?'High error rate — slow down and review output before continuing.':'You actively steer and catch issues.'),
  diligence:f.ncode===0?'No code sessions this week.':(f.diligence<50?'Ask '+TN()+' to run tests/commands after edits — most code sessions never verified.':'Most code sessions verified their work with commands.')};}

function renderFluency(w,prev){
  const f=fluencyOf(w),tips=fluencyTips(f),fp=prev?fluencyOf(prev):null;
  const dims=[
    ['Delegation',f.delegation,'<b>'+Math.round(f.codeShare*100)+'%</b> of sessions shipped code · <b>'+f.lev.toFixed(1)+'×</b> leverage'],
    ['Description',f.description,'<b>'+Math.round(f.oneShot*100)+'%</b> of prompts needed no correction · <b>'+Math.round(f.avgW)+'</b> words avg'],
    ['Discernment',f.discernment,'<b>'+(w.corrections||0)+'</b> corrections '+(D.na&&D.na.interruptions?'· ':'+ <b>'+w.interruptions+'</b> interruptions · ')+'<b>'+w.tool_errors+'</b> tool errors'],
    ['Diligence',f.diligence,'<b>'+Math.round(f.verified*100)+'%</b> of '+f.ncode+' code session'+(f.ncode===1?'':'s')+' ran commands after edits']];
  const key=['delegation','description','discernment','diligence'];
  $('flList').innerHTML=dims.map((d,i)=>
    '<div class="flrow"><span class="fln">'+d[0]+'</span>'+
    '<span class="flb"><span class="bar"><i style="width:'+d[1]+'%"></i></span></span>'+
    '<span class="fls">'+d[1]+'</span>'+
    '<span class="flt">'+d[2]+'<br>'+tips[key[i]]+'</span></div>').join('');
  const ds=[{label:'This week',data:dims.map(d=>d[1]),borderColor:C.coral,
    backgroundColor:hexa(C.coral,.17),pointBackgroundColor:C.coral,borderWidth:2}];
  if(fp)ds.push({label:'Last week',data:[fp.delegation,fp.description,fp.discernment,fp.diligence],
    borderColor:C.sky,backgroundColor:hexa(C.sky,.07),pointBackgroundColor:C.sky,borderWidth:1.5});
  mk('cFluency',{type:'radar',data:{labels:dims.map(d=>d[0]),datasets:ds},
    options:{scales:{r:{min:0,max:100,ticks:{display:false,stepSize:25},
      grid:{color:C.grid},angleLines:{color:C.grid},
      pointLabels:{color:C.txt2,font:{size:11.5,weight:'600'}}}},
      plugins:{legend:{position:'bottom'}}}});}

let reflQtext='',reflWeekKey=null;
function pickReflection(w,idx){
  const c=[];
  const corrRate=w.prompts?(w.corrections||0)/w.prompts:0;
  if(corrRate>0.08)c.push('You corrected '+TN()+' '+w.corrections+' times this week ('+Math.round(corrRate*100)+'% of prompts). What context could your first prompts include so the second try isn’t needed?');
  const late=w.messages?(w.late_msgs||0)/w.messages:0;
  if(late>0.2)c.push(Math.round(late*100)+'% of this week’s activity happened between 11pm and 6am. Is late-night coding a deliberate choice — or a habit worth questioning?');
  const tot=w.reads+w.writes,ex=tot?w.reads/tot:0;
  if(tot&&ex>0.65)c.push(Math.round(ex*100)+'% of tool calls this week were exploration. Are you using '+TN()+' mostly to understand code — and is there building you could delegate too?');
  if(tot&&ex<0.15)c.push(Math.round((1-ex)*100)+'% of tool calls this week were edits. Are you reading and verifying what gets written — or shipping on trust?');
  const code=w.sessions.filter(s=>s.loc>0),ver=code.length?code.filter(s=>s.bash>0).length/code.length:1;
  if(code.length>2&&ver<0.4)c.push('Only '+Math.round(ver*100)+'% of code-writing sessions ran a command afterwards. How do you know this week’s '+fmt(w.loc)+' new lines actually work?');
  c.push('What’s one thing you want to keep doing yourself, even if '+TN()+' could do it faster?');
  return c[idx%c.length];}

function renderWeek(){
  const wk=D.weeks;
  if(!wk.length){$('secSub').textContent='No data yet.';
    $('wKpis').innerHTML='<div class="empty">No weeks recorded yet.</div>';return;}
  if(wIdx===null)wIdx=wk.length-1;
  const w=wk[wIdx],prev=wIdx>0?wk[wIdx-1]:null;
  $('wPrev').disabled=wIdx===0;$('wNext').disabled=wIdx===wk.length-1;
  updateChrome();
  const bp=buildPct(w);
  const kpis=[
    ['Sessions',w.sessions.length,delta(w.sessions.length,prev&&prev.sessions.length),C.sky],
    ['Focus hours',(w.minutes/60).toFixed(1),delta(w.minutes,prev&&prev.minutes),C.teal],
    ['Output tokens',fmt(w.output),delta(w.output,prev&&prev.output),C.emerald],
    ['Lines of code',fmt(w.loc),delta(w.loc,prev&&prev.loc),C.violet],
    ['Prompts',fmt(w.prompts),delta(w.prompts,prev&&prev.prompts),C.coral],
    ['Building',bp+'%',(100-bp)+'% exploration',C.amber]];
  $('wKpis').innerHTML=kpis.map(k=>
    '<div class="kpi plain" style="--kc:'+k[3]+'"><div class="khead"><span class="eyebrow">'+k[0]+
    '</span></div><div class="num-lg">'+k[1]+'</div><div class="s">'+(k[2]||'')+'</div></div>').join('');
  const g={};w.sessions.forEach(s=>{(g[s.project]=g[s.project]||[]).push(s)});
  const keys=Object.keys(g);
  $('wWork').innerHTML=keys.length?keys.map(p=>
    '<div class="grp"><h4>'+esc(p)+'</h4><ul>'+g[p].map(s=>
      '<li>'+esc(pv(s.title))+'<div class="meta">'+fmtD(s.day)+' · '+s.min+' min · '+
      fmt(s.loc)+' LoC · '+s.prompts+' prompts</div></li>').join('')+'</ul></div>').join('')
    :'<div class="empty">No sessions logged this week.</div>';
  mk('cSplit',{type:'doughnut',data:{labels:['Building (file edits)','Exploration (reads/searches)'],
    datasets:[{data:[w.writes,w.reads],backgroundColor:[C.emerald,C.sky],
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'}}}});
  mk('cWeekDays',{type:'bar',data:{labels:w.days.map(x=>fmtD(x.day)),datasets:[
    {label:'Output tokens',data:w.days.map(x=>x.output),backgroundColor:C.sky,yAxisID:'y'},
    {label:'LoC',data:w.days.map(x=>x.loc),backgroundColor:C.violet,yAxisID:'y1'}]},
    options:{scales:{x:XGRID,y:{position:'left',...GRID},
      y1:{position:'right',grid:{drawOnChartArea:false},border:{display:false}}},
      plugins:{legend:{position:'bottom'}}}});
  renderFluency(w,prev);
  reflQtext=pickReflection(w,wIdx);
  $('reflQ').textContent=reflQtext;
  /* clear the discussion only when switching weeks — not on the 15s refresh */
  if(reflWeekKey!==w.key){reflWeekKey=w.key;$('reflOut').textContent='';}
}
$('wPrev').onclick=()=>{if(wIdx>0){wIdx--;renderWeek();}};
$('wNext').onclick=()=>{if(D&&wIdx<D.weeks.length-1){wIdx++;renderWeek();}};

$('reflBtn').onclick=async()=>{
  if(!D||!D.weeks.length||!reflQtext)return;
  const btn=$('reflBtn'),out=$('reflOut'),old=btn.innerHTML;
  btn.disabled=true;btn.textContent='Thinking…';
  out.textContent='Running claude -p — this can take a minute…';
  try{const r=await fetch('/api/reflect?week='+encodeURIComponent(D.weeks[wIdx].key)+
    '&q='+encodeURIComponent(reflQtext)+'&source='+SOURCE);
    const j=await r.json();out.textContent=j.ok?j.text:('Failed: '+j.error);}
  catch(e){out.textContent='Failed: '+e;}
  btn.disabled=false;btn.innerHTML=old;};

function weekMarkdown(w){
  const g={};w.sessions.forEach(s=>{(g[s.project]=g[s.project]||[]).push(s)});
  let md='# Weekly Update — '+fmtD(w.start)+' to '+fmtD(w.end)+'\n\n## What I worked on\n';
  for(const p in g){md+='\n### '+p+'\n';g[p].forEach(s=>{md+='- '+s.title+' ('+s.day+', ~'+s.min+' min)\n';});}
  const bp=buildPct(w);
  md+='\n## Numbers\n';
  md+='- '+w.sessions.length+' sessions, ~'+(w.minutes/60).toFixed(1)+' focus-hours\n';
  md+='- '+w.loc.toLocaleString()+' lines of code written with '+TN()+'\n';
  md+='- '+w.prompts.toLocaleString()+' prompts · '+w.output.toLocaleString()+' tokens generated\n';
  md+='- Work split: '+bp+'% building / '+(100-bp)+'% exploration\n';
  const tc={};w.sessions.forEach(s=>{const t=s.type||'other';tc[t]=(tc[t]||0)+1});
  const mix=TT.filter(t=>tc[t]).map(t=>tc[t]+' '+t).join(', ');
  if(mix)md+='- Task mix: '+mix+'\n';
  return md;}

async function copyText(txt){
  try{await navigator.clipboard.writeText(txt);}
  catch(e){const ta=document.createElement('textarea');ta.value=txt;
    document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();}}
function flash(btn,label){const old=btn.innerHTML;btn.textContent=label;
  setTimeout(()=>{btn.innerHTML=old;},1600);}

$('wCopy').onclick=async()=>{if(!D||!D.weeks.length)return;
  await copyText(weekMarkdown(D.weeks[wIdx]));flash($('wCopy'),'✓ Copied');};

$('wAI').onclick=async()=>{
  if(!D||!D.weeks.length)return;
  const btn=$('wAI'),card=$('wAIcard'),out=$('wAItext'),old=btn.innerHTML;
  btn.disabled=true;btn.textContent='Generating…';card.style.display='';
  out.textContent='Running claude -p — this can take a minute…';
  try{const r=await fetch('/api/ai_summary?week='+encodeURIComponent(D.weeks[wIdx].key)+'&source='+SOURCE);
    const j=await r.json();out.textContent=j.ok?j.text:('Failed: '+j.error);}
  catch(e){out.textContent='Failed: '+e;}
  btn.disabled=false;btn.innerHTML=old;};

/* ═══════════════════ Project sheet: grabbable, velocity-aware ═══════════════════
   Open/close is a spring on `openP`; the drag offset is a second, independent
   spring. Dragging jumps the offset 1:1 with the finger (cancelling any
   in-flight animation), release projects the momentum forward and either
   dismisses or springs home carrying the release velocity. */
const sheet=$('sheet'),scrim=$('scrim'),sbox=$('sBox');
let sheetOn=false;
function paintSheet(){
  const p=openP.v,y=dragY.v;
  sbox.style.transform='translate3d(0,'+(y+(1-p)*22).toFixed(2)+'px,0) scale('+(.965+.035*p).toFixed(4)+')';
  sbox.style.opacity=Math.max(0,Math.min(1,p)).toFixed(3);
  sbox.style.filter=p<.999?'blur('+((1-p)*7).toFixed(2)+'px)':'none';
  scrim.style.opacity=(Math.max(0,Math.min(1,p))*Math.max(0,1-Math.max(0,y)/420)).toFixed(3);
}
const openP=new Spring(0,paintSheet,{damping:1,response:.36});
const dragY=new Spring(0,paintSheet,{damping:.8,response:.34});
openP.done=()=>{if(openP.target===0&&!sheetOn){sheet.style.display='none';scrim.style.display='none';}};

function openSheet(){sheetOn=true;sheet.style.display='flex';scrim.style.display='block';
  sheet.scrollTop=0;dragY.jump(0);paintSheet();openP.to(1);
  setTimeout(()=>$('sClose').focus(),60);}
function closeSheet(vel){
  if(!sheetOn)return;sheetOn=false;
  openP.to(0);dragY.to(Math.max(260,innerHeight*.45),vel);}
$('sClose').onclick=()=>closeSheet();
scrim.onclick=()=>closeSheet();
sheet.addEventListener('click',e=>{if(e.target===sheet)closeSheet();});
addEventListener('keydown',e=>{if(e.key==='Escape'){if(popOpen)togglePop(false);else if(sheetOn)closeSheet();}});

/* 1:1 tracking with grab offset, rubber-banding upward, momentum on release */
(function(){
  const grab=$('sGrab');let pid=null,startY=0,base=0,hist=[];
  grab.addEventListener('pointerdown',e=>{
    if(e.target.closest('#sClose'))return;
    pid=e.pointerId;grab.setPointerCapture(pid);
    startY=e.clientY;base=dragY.v;hist=[[e.clientY,performance.now()]];});
  grab.addEventListener('pointermove',e=>{
    if(pid===null||e.pointerId!==pid)return;
    let dy=base+(e.clientY-startY);
    if(dy<0)dy=-rubber(-dy,innerHeight);          // soft boundary, never a hard stop
    dragY.jump(dy);                                // continuous feedback, not on release
    hist.push([e.clientY,performance.now()]);if(hist.length>6)hist.shift();});
  const end=e=>{
    if(pid===null||(e&&e.pointerId!==pid))return;
    try{grab.releasePointerCapture(pid);}catch(err){}
    pid=null;
    let v=0;
    if(hist.length>1){const a=hist[0],b=hist[hist.length-1],dt=(b[1]-a[1])/1000;
      if(dt>0.001)v=(b[0]-a[0])/dt;}
    const projected=dragY.v+projectTo(v);          // where the flick is going
    if(projected>170||v>900)closeSheet(v);
    else dragY.to(0,v);};                          // hand the velocity to the spring
  grab.addEventListener('pointerup',end);
  grab.addEventListener('pointercancel',end);
})();

function openProject(p){
  const cr=p.cost_unit?p.cost_unit==='credits':credits();     // an All AI row carries its own tool's unit
  $('sTitle').textContent=p.name.split('/').pop()||p.name;
  $('sSub').innerHTML=esc(p.name)+' · '+fmt(p.input)+' in · '+fmt(p.output)+' out · '+
    fmt(p.loc)+' LoC · '+p.sessions+' sessions · '+(p.cost==null?'—':(cr?'':'est. ')+fmtCost(p.cost,undefined,cr));
  openSheet();
  mk('cProj',{type:'bar',data:{labels:p.days.map(x=>x.day.slice(5)),datasets:[
    {label:'Output tokens',data:p.days.map(x=>x.output),backgroundColor:C.sky,yAxisID:'y'},
    {label:'LoC',data:p.days.map(x=>x.loc),backgroundColor:C.violet,yAxisID:'y1'}]},
    options:{scales:{x:XGRID,y:{position:'left',...GRID},
      y1:{position:'right',grid:{drawOnChartArea:false},border:{display:false}}},
      plugins:{legend:{position:'bottom'}}}});
  const sess=(D.sessions_list||[]).filter(s=>s.project===p.name&&(!p.tool||s.tool===p.tool));
  /* All AI keeps only the most recent sessions of all tools: say so when this project has more */
  $('sSess').previousElementSibling.textContent=p.tool?'Recent sessions'+(sess.length<p.sessions?' (showing '+sess.length+' of '+p.sessions+')':''):'Sessions';
  $('sSess').innerHTML=sess.length?sess.map(s=>
    '<li><b>'+esc(pv(s.title))+'</b><div class="mt">'+s.day+' · '+s.min+' min · '+fmt(s.loc)+
    ' LoC · '+s.prompts+' prompts · '+fmt(s.output)+' tokens out</div></li>').join('')
    :'<li class="empty">No session details available.</li>';
  document.querySelector('#sFiles tbody').innerHTML=(p.files||[]).length?(p.files||[]).map(f=>
    '<tr><td>'+esc(pv(f[0]))+'</td><td class="num">'+fmt(f[1])+'</td></tr>').join('')
    :'<tr><td colspan="2" class="empty">'+(p.files===null?'File details are on the tool’s own pages.':'No files recorded.')+'</td></tr>';
}

/* ─────────────────────────── Exports ─────────────────────────── */
function download(name,text,type){
  const b=new Blob([text],{type}),u=URL.createObjectURL(b);
  const a=document.createElement('a');a.href=u;a.download=name;
  document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(u);}
function rangeLabel(){return (customFrom||customTo)
  ?((customFrom||'start')+'_to_'+(customTo||'now'))
  :(days?('last'+days+'d'):'alltime');}
const expName=()=>SOURCE==='all'?'all-ai':TN().toLowerCase();       // one file-name slug per tool set
$('expJson').onclick=()=>{if(D)download(expName()+'-usage_'+rangeLabel()+'.json',
  JSON.stringify(D,null,2),'application/json');};
$('expCsv').onclick=()=>{if(!D)return;
  if(D.source==='all'){const tl=Object.keys(D.by_tool);     // tokens per tool and the union of active time
    download(expName()+'-usage_'+rangeLabel()+'.csv',[['date',...tl.map(k=>k+'_tokens'),'active_min']]
      .concat(D.days.map(x=>[x.day,...tl.map(k=>x.tokens_by_tool[k]||0),x.active_min])).map(r=>r.join(',')).join('\n'),'text/csv');return;}
  const rows=[['date','input','output','cache_read','cache_write','loc','prompts','reads','writes',credits()?'credits':'est_cost_usd']];
  const nc=D.na&&D.na.cache_read;                   // no cache split in the logs: blank cells, not zeros
  D.days.forEach(x=>rows.push([x.day,x.input,x.output,nc?'':x.cache_read,nc?'':x.cache_write,
    x.loc,x.prompts,x.reads,x.writes,x.cost]));
  download(expName()+'-usage_'+rangeLabel()+'.csv',rows.map(r=>r.join(',')).join('\n'),'text/csv');};
$('expCopy').onclick=async()=>{if(!D)return;const t=D.totals;
  if(D.source==='all'){await copyText('All AI usage — '+rangeLabel().replace(/_/g,' ')+'\nTotal tokens: '+t.tokens.toLocaleString()+'\n'+
    Object.keys(D.by_tool).map(k=>SRCN[k][1]+': '+D.by_tool[k].tokens.toLocaleString()+' tokens, '+
      (D.by_tool[k].cost_unit==='credits'?D.by_tool[k].cost+' credits':'$'+D.by_tool[k].cost+' est.')).join('\n')+
    '\nPrompts: '+t.prompts.toLocaleString()+'\nSessions: '+t.sessions+'\nActive time: '+fmtMin(t.active_min));
    flash($('expCopy'),'✓');return;}
  await copyText(TF()+' usage — '+rangeLabel().replace(/_/g,' ')+'\n'+
    'Output tokens: '+t.output.toLocaleString()+'\nInput tokens: '+t.input.toLocaleString()+'\n'+
    (D.na&&D.na.cache_read?'Cache read: n/a\n':'Cache read: '+t.cache_read.toLocaleString()+' ('+t.cache_efficiency+'% hit)\n')+
    'Lines of code: '+t.loc.toLocaleString()+'\nPrompts: '+t.prompts.toLocaleString()+'\n'+
    'Sessions: '+t.sessions+' ('+t.avg_session_min+' min avg)\n'+
    (credits()?'Credits used: ':'Est. API cost: ')+fmtCost(t.cost)+'\nLeverage: '+t.leverage+'x');
  flash($('expCopy'),'✓');};

/* ─────────────────────────── Boot ─────────────────────────── */
/* Chart.js has a rare first-paint race in its animator (creating several
   charts in one tick can throw asynchronously inside its own rAF loop,
   "this._fn is not a function"). It's not catchable at the call site since
   it happens after mk() returns — but a plain re-render always recovers,
   so treat it as a one-shot self-heal rather than a fatal error. */
let chartRecoverAt=0;
addEventListener('error',e=>{
  if(!/chart\.umd\.min\.js/.test(e.filename||''))return;
  const now=Date.now();
  if(now-chartRecoverAt<1000)return;   // avoid a retry storm if it keeps failing
  chartRecoverAt=now;
  if(D)render();
});
applySource(false);
applyTheme(false);
updateChrome();
load();
setInterval(load,15000);
</script></body></html>"""


def range_window(days, today):
    """Calendar-day windows for a rolling "Last N days" range: today and the N-1
    days before it, plus the N days before that (the baseline for deltas).
    Returns (cur0, cur1, prev0, prev1) as ISO dates; `days` must be > 0."""
    cur0 = today - timedelta(days=days - 1)
    prev1 = cur0 - timedelta(days=1)
    prev0 = prev1 - timedelta(days=days - 1)
    return cur0.isoformat(), today.isoformat(), prev0.isoformat(), prev1.isoformat()


# What the catalog text calls the tool on the All AI pages: the wording that names Claude or CLAUDE.md
# is swapped for neutral words, since those figures belong to every tool at once.
ALL_AI_TOOL = {"label": "your AI tools", "short": "your AI tools", "instructions": ("AGENTS.md",)}


def union_slots(stats, d0, d1):
    """day -> the set of 10-minute slots (hour*6 + minute//10) in which ANY of `stats` was active, for
    the days from d0 through d1 (either may be None: open end). The one place All AI time comes from:
    a slot where both tools were active counts once, so time is never summed across tools."""
    out = {}
    for st in stats:
        for day, slots in st.day_buckets.items():
            if slots and not (d0 and day < d0) and not (d1 and day > d1):
                out.setdefault(day, set()).update(slots)
    return out


def _all_tokens(d):
    """Everything the model processed: input + cache read + cache write + output (design 5.1)."""
    return d["input"] + d["cache_read"] + d["cache_write"] + d["output"]


def _session_day(s):
    """The local day a session started, "" when its start time is not readable."""
    try:
        return datetime.fromisoformat(s["start"].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except Exception:
        return ""


def interruptions_available(tool, st) -> bool:
    """True when `tool`'s logs (Stats `st`) record interrupted requests. Claude's do; Copilot's only when
    the scanned chats record stopped requests, so the All AI sum never includes a Copilot zero it made up."""
    if tool == "copilot":
        return copilot_stops_logged(st)
    return not SOURCES.get(tool, {}).get("no_interruptions")


def build_all_ai(by_tool, d0, d1, prev, history=None):
    """The All AI payload (design 5.5). `by_tool` is tool id -> Stats already scoped to the window d0..d1
    (the same calendar window for every tool). What means the same in every tool is summed (tokens,
    prompts, sessions); time, focus, the heatmap and late-night come from the union of 10-minute slots;
    cost is never added (each tool keeps its own unit); interruptions are summed only over the tools
    that log them (`interruptions_from`). `prev` is the (first, last) day of the window before, for the
    deltas, read from `history` (tool id -> Stats over full history), which is required with `prev`: the
    scoped Stats hold no days before the window, so without it the deltas would be made-up zeros.
    `history` also decides which tools log interruptions, so that does not depend on the window."""
    if prev and history is None:
        raise ValueError("build_all_ai: prev needs history (the tools' full-history Stats)")
    stats = list(by_tool.values())
    slots = union_slots(stats, d0, d1)
    days = sorted(set(slots) | {day for st in stats for day in st.days
                                if not (d0 and day < d0) and not (d1 and day > d1)})
    from_tools = [t for t, st in by_tool.items() if interruptions_available(t, (history or by_tool).get(t, st))]

    def tot(st, key):
        return sum(v[key] for v in st.days.values())

    tools = {}
    for tool, st in by_tool.items():
        parts = {k: tot(st, k) for k in ("input", "output", "cache_read", "cache_write")}
        premium = getattr(st, "premium_days", None)          # Copilot's own unit; Claude has none
        tools[tool] = {
            "tokens": sum(parts.values()), **parts,
            "prompts": tot(st, "prompts"), "sessions": len(st.sessions),
            "active_min": sum(len(s) for s in union_slots([st], d0, d1).values()) * 10,
            "cost": round(tot(st, "cost"), 2), "cost_unit": SOURCES[tool]["cost_unit"],
            "premium_requests": None if premium is None else round(float(sum(premium.values())), 2),
        }
        modes = getattr(st, "token_modes", None)              # Copilot's: how many sessions have approx. tokens
        if modes is not None:
            tools[tool]["token_modes"] = {m: modes[m] for m in ("exact", "approx", "none")}

    rows = []
    heat = [[0] * 24 for _ in range(7)]
    for day in days:
        s = slots.get(day, set())
        wd = date.fromisoformat(day).weekday()
        for slot in s:
            heat[wd][slot // 6] += 1
        rows.append({
            "day": day,
            "tokens_by_tool": {t: _all_tokens(st.days[day]) if day in st.days else 0 for t, st in by_tool.items()},
            "messages": len(s),
            "late_msgs": sum(1 for x in s if x // 6 >= QUIET_START or x // 6 < QUIET_END),
            "active_min": len(s) * 10, "focus_max": longest_focus(s),
            "interruptions": sum(by_tool[t].days[day]["interruptions"] for t in from_tools if day in by_tool[t].days),
            "tool_errors": sum(st.days[day]["tool_errors"] for st in stats if day in st.days),
        })

    models = [{"name": name, "tool": tool, "msgs": m["msgs"], "input": m["input"], "output": m["output"],
               "cost": round(m["cost"], 2), "cost_unit": SOURCES[tool]["cost_unit"]}
              for tool, st in by_tool.items() for name, m in st.models.items()]
    models.sort(key=lambda x: -x["output"])

    projects = {}                         # labels already match across tools (copilot_source.project_label)
    for tool, st in by_tool.items():
        for name, p in st.projects.items():
            row = projects.setdefault(name, {"name": name, "tokens": 0, "by_tool": {}})
            row["tokens"] += _all_tokens(p)
            row["by_tool"][tool] = {"input": p["input"], "output": p["output"], "cache_read": p["cache_read"],
                                    "cache_write": p["cache_write"], "prompts": p["prompts"], "loc": p["loc"], "cost": round(p["cost"], 2),
                                    "cost_unit": SOURCES[tool]["cost_unit"],
                                    # exact, like stats_to_json's project rows: the project sheet reads these
                                    "sessions": len(p["sessions"]),
                                    "days": [{"day": d, **p["days"][d]} for d in sorted(p["days"])]}
    projects = sorted(projects.values(), key=lambda x: -x["tokens"])[:10]

    sessions_list = [dict(r, tool=tool) for tool, st in by_tool.items() for r in session_rows(st)]
    sessions_list.sort(key=lambda x: x["day"], reverse=True)
    del sessions_list[300:]

    data = {
        "source": "all", "cost_unit": None,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "totals": {**{k: sum(t[k] for t in tools.values()) for k in
                      ("tokens", "input", "output", "cache_read", "cache_write", "prompts", "sessions")},
                   "active_min": sum(len(s) for s in slots.values()) * 10, "active_days": len(days)},
        "by_tool": tools, "days": rows, "heatmap": heat, "models": models,
        "projects": projects, "sessions_list": sessions_list,
        "interruptions_from": from_tools,
    }
    if prev:
        p0, p1 = prev
        past = history
        data["prev"] = {
            "tokens": sum(_all_tokens(v) for st in past.values() for day, v in st.days.items() if p0 <= day <= p1),
            "prompts": sum(v["prompts"] for st in past.values() for day, v in st.days.items() if p0 <= day <= p1),
            "sessions": sum(1 for st in past.values() for s in st.sessions.values() if p0 <= _session_day(s) <= p1),
            "active_min": sum(len(s) for s in union_slots(past.values(), p0, p1).values()) * 10,
        }
    return data


MAX_DAYS = 36500        # a ?days= above this (100 years) is this: a huge value would overflow the date maths


def query_range(q):
    """(from, to, days) of a /api/stats query: the custom range when either date is given, else the
    rolling window of `days` (0 = all time; 30 when missing or not a number; at most MAX_DAYS)."""
    frm = (q.get("from") or [""])[0].strip()
    to = (q.get("to") or [""])[0].strip()
    try:
        days = int(q.get("days", ["30"])[0])
    except ValueError:
        days = 30
    return frm, to, min(days, MAX_DAYS)


def build_all_payload(scanners, q, today):
    """/api/stats?source=all: each tool that is on, scoped to the one window of the query, combined by
    build_all_ai, plus the page extras. Streaks and active days use every tool's full history."""
    frm, to, days = query_range(q)
    tools = [t for t in SOURCES if t in scanners and scanners[t].available()]
    full = {t: scanners[t].build_stats() for t in tools}
    prev = None
    if frm or to:                       # custom date range
        b0, b1 = frm or None, to or None
    elif days > 0:                      # rolling N-day window, in calendar days
        b0, b1, prev0, prev1 = range_window(days, today)
        prev = (prev0, prev1)
    else:                               # all time
        b0 = b1 = None
    ranged = {t: scanners[t].build_stats(b0, b1) if (b0 or b1) else full[t] for t in tools}
    data = build_all_ai(ranged, b0, b1, prev, full)
    data["sources"] = sources_status(scanners)
    alldays = sorted(set().union(*(st.days for st in full.values())))
    data["streak"], data["best_streak"] = active_streak(alldays)
    data["active_days"] = len(alldays)
    data["catalog"] = catalog_for("all", ALL_AI_TOOL)
    return data


def build_payload(scanner, q, today=None, source="claude"):
    """Everything /api/stats returns for the query `q` (parse_qs output), except
    the live-watcher extras (notifications, plan_window), Claude's log root and the
    list of sources, which the HTTP handler adds. `source` names the tool the
    scanner reads, so the page knows which cost unit to show. For source="all", `scanner` is the dict of
    scanners (id -> scanner) and the result is the All AI payload (build_all_payload). A Copilot payload also
    gets `copilot` (copilot_extras of the range), its own `root` (its log folders), its own
    feature list in My Adoption, its own Data Health, and `na` (KPI -> why it is not available).
    My Adoption, the Advisor and Data Health always read the full history."""
    today = today or date.today()
    if source == "all":
        return build_all_payload(scanner, q, today)
    source = source if source in SOURCES else "claude"
    frm, to, days = query_range(q)
    # Build an all-time Stats (powers the Weekly tab, streaks and deltas)
    # plus a range-scoped Stats so every card on the dashboard respects
    # the selected window — not just the headline KPIs.
    full = scanner.build_stats()
    alldays = sorted(full.days.keys())
    prev = []
    if frm or to:                       # custom date range
        ranged = scanner.build_stats(frm or None, to or None)
        b0, b1 = frm or None, to or None
    elif days > 0:                       # rolling N-day window, in calendar days
        cur0, cur1, prev0, prev1 = range_window(days, today)
        ranged = scanner.build_stats(cur0, cur1)
        b0, b1 = cur0, cur1
        # the window just before it (for deltas)
        prev = [d for d in alldays if prev0 <= d <= prev1]
    else:                                # all time
        ranged = full
        b0 = b1 = None
    tool = SOURCES[source]
    if source == "copilot":      # interruptions exist only when the scanned logs record stopped requests
        tool = dict(tool, no_interruptions=not copilot_stops_logged(full))
    data = stats_to_json(ranged, None, tool["instructions"])
    data["source"] = source
    data["cost_unit"] = tool["cost_unit"]
    if source == "copilot":
        data["copilot"] = copilot_extras(ranged)
        data["root"] = copilot_root_label(getattr(scanner, "user_roots", ()),
                                          getattr(scanner, "copilot_home", None))
    data["weeks"] = build_weeks(full)
    data["adoption"] = build_adoption(
        full, COPILOT_ADOPTION_FEATURES if source == "copilot" else ADOPTION_FEATURES, tool["instructions"], tool)
    # Flow / Quality / Value follow the selected range, sliced from full history
    data["flow"] = build_flow(full, b0, b1)
    data["quality"] = build_quality(full, b0, b1)
    data["value"] = build_value(full, b0, b1)
    if prev:
        keep = {"flow": ("turn_median_s", "clean_pct", "recovery_pct", "first_edit_median_s", "score"),
                "quality": ("verified_pct", "first_run_pass_pct", "tests_alongside_pct", "reedit_pct", "score"),
                "value": ("cost", "per_100_loc", "cost_per_verified", "cache_savings")}
        pf = {"flow": build_flow(full, prev0, prev1), "quality": build_quality(full, prev0, prev1),
              "value": build_value(full, prev0, prev1)}
        data["prev_insight"] = {k: {m: pf[k][m] for m in ms} for k, ms in keep.items()}
    data["advisor"] = build_advisor(full, data["adoption"], tool)
    if source == "copilot":
        data["health"] = build_health_copilot(full, scanner.files())
        data["na"] = copilot_na(data["health"])                  # KPI -> why Copilot has no figure
        copilot_cache_rate(data, ranged)
    else:
        data["health"] = build_health(full)
        data["na"] = {}
    # the catalog is static METRIC_CATALOG text (no user data): name the selected tool in it, as the
    # advisor and My Adoption do, and give Copilot its credit wording. data["weeks"] has no fixed
    # Claude wording to swap, only session titles.
    data["catalog"] = catalog_for(source, tool)

    def sd(dl, k):
        return sum(full.days[d][k] for d in dl)

    if prev:
        data["prev"] = {k: sd(prev, k) for k in
                        ("output", "input", "loc", "prompts", "human_tokens", "cache_read",
                         "corrections")}
        data["prev"]["cost"] = round(sd(prev, "cost"), 2)
        pc = 0
        for _sid, se in full.sessions.items():
            try:
                sday = datetime.fromisoformat(
                    se["start"].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
            except Exception:
                continue
            if prev0 <= sday <= prev1:
                pc += 1
        data["prev"]["sessions"] = pc

    data["streak"], data["best_streak"] = active_streak(alldays)
    data["active_days"] = len(alldays)
    data["goals"] = GOALS
    if alldays:
        last = alldays[-1]
        data["today"] = {"day": last, "loc": full.days[last]["loc"],
                         "tokens": full.days[last]["output"]}
        data["week_tokens"] = sum(full.days[d]["output"] for d in alldays[-7:])
    return data


class Handler(BaseHTTPRequestHandler):
    scanners: dict = {}          # source id -> scanner, for the sources that are on
    scanner: Scanner = None      # the Claude scanner; used when `scanners` is not set
    root: str = ""

    def _scanners(self):
        if self.scanners:
            return self.scanners
        return {"claude": self.scanner} if self.scanner else {}

    def _source(self, q):
        """(source id, scanner) for the request's source=. A missing or unknown value
        means Claude, or the first source that is on when Claude is not."""
        scanners = self._scanners()
        src = pick_source(scanners, (q.get("source") or [""])[0])
        return src, scanners.get(src)

    def _week(self, q):
        """(week, None) for the week= of one tool's history, which /api/ai_summary and
        /api/reflect work on, or (None, error payload) when there is no such week."""
        if (q.get("source") or [""])[0] == "all":
            return None, {"ok": False, "error": "pick one tool"}
        wkey = (q.get("week") or [""])[0]
        weeks = build_weeks(self._source(q)[1].build_stats())
        week = next((w for w in weeks if w["key"] == wkey), None)
        if not week:
            return None, {"ok": False, "error": "unknown week %r" % wkey}
        return week, None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = HTML.encode()
            self._send(200, "text/html; charset=utf-8", body)
        elif parsed.path == "/chart.umd.min.js":
            local = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "chart.umd.min.js")
            try:
                with open(local, "rb") as f:
                    self._send(200, "application/javascript", f.read())
            except OSError:
                # fall back to the CDN if the local copy is missing
                self.send_response(302)
                self.send_header("Location",
                                 "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js")
                self.end_headers()
        elif parsed.path == "/api/stats":
            q = parse_qs(parsed.query)
            asked = (q.get("source") or [""])[0]
            label = "All AI" if asked == "all" else SOURCES.get(asked, {}).get("label", "the dashboard")
            try:
                if wants_all_ai(self._scanners(), asked):
                    src, label = "all", "All AI"
                    data = build_payload(self._scanners(), q, source="all")
                else:
                    src, scanner = self._source(q)
                    label = SOURCES[src]["label"]
                    data = build_payload(scanner, q, source=src)
                data["sources"] = sources_status(self._scanners())
                if src == "claude":          # the log root and live watcher are Claude-only
                    data["root"] = self.root
                    watcher = getattr(self, "watcher", None)
                    data["notifications"] = list(watcher.recent_events[-20:]) if watcher else []
                    data["plan_window"] = watcher.plan_window_snapshot() if watcher else None
                body = json.dumps(data).encode()
            except Exception as e:       # the page shows this instead of "can't reach the server"
                traceback.print_exc()
                self._send(500, "application/json", json.dumps({
                    "ok": False, "error": "Couldn't build the stats for %s" % label,
                    "detail": ("%s: %s" % (type(e).__name__, e))[:200]}).encode())
                return
            self._send(200, "application/json", body)
        elif parsed.path == "/api/ai_summary":
            week, payload = self._week(parse_qs(parsed.query))
            if week:
                payload = ai_week_summary(week)
            self._send(200, "application/json", json.dumps(payload).encode())
        elif parsed.path == "/api/reflect":
            q = parse_qs(parsed.query)
            question = (q.get("q") or [""])[0].strip()[:500]
            week, payload = self._week(q)
            if week and not question:
                payload = {"ok": False, "error": "no question provided"}
            elif week:
                payload = ai_reflection(week, question)
            self._send(200, "application/json", json.dumps(payload).encode())
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence request logging
        pass


# ---------------------------------------------------------------------------
# Setup wizard (--setup-notifications): wires the statusLine hook so plan-
# usage % and toast milestones use real Anthropic numbers instead of the
# local estimate. See docs/superpowers/specs/2026-09-09-setup-notifications-
# onboarding-design.md for the full design.
# ---------------------------------------------------------------------------

STATUSLINE_MARKER = "ai_fluency_insights_statusline.js"
# Installs made before the rename point at this file; still treated as ours.
LEGACY_STATUSLINE_MARKER = "dev_token_dashboard_statusline.js"
# Windows logon task. It has its own name so it never replaces (or removes) the original
# Claude-only dashboard's "AIFluencyInsights" / "DevTokenDashboard" task on a laptop that runs both.
STARTUP_TASK = "AIFluencyInsightsMultiAI"
# The original Claude-only dashboard (dev-token-dashboard) serves on 8787; this one defaults to
# 8788 so both can run on the same laptop.
DEFAULT_PORT = 8788
ORIGINAL_DASHBOARD_PORT = 8787


def claude_config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def load_settings_json(path):
    """Like load_notify_state, but distinguishes a missing file (fine, an
    empty settings.json is valid) from a malformed one (must abort rather
    than silently treat a broken config as empty and overwrite it)."""
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return None, f"{path} is not valid JSON: {e}"
    except OSError as e:
        return None, f"could not read {path}: {e}"


def classify_statusline(settings):
    sl = settings.get("statusLine")
    command = sl.get("command") if isinstance(sl, dict) else None
    if not isinstance(command, str) or not command:
        return "missing"
    if STATUSLINE_MARKER in command or LEGACY_STATUSLINE_MARKER in command:
        return "ours"
    return "foreign"


def merge_statusline_config(settings, statusline_js_path):
    new_settings = dict(settings)
    kind = classify_statusline(settings)
    if kind == "missing":
        new_settings["statusLine"] = {
            "type": "command",
            "command": f'node "{statusline_js_path}"',
            "refreshInterval": 30,
        }
    else:  # "ours" -- caller must not call this for "foreign"
        sl = dict(settings["statusLine"], refreshInterval=30)
        if LEGACY_STATUSLINE_MARKER in sl["command"]:
            # Pre-rename install: point it at the new script path.
            sl["command"] = f'node "{statusline_js_path}"'
        new_settings["statusLine"] = sl
    return new_settings


def node_available():
    return shutil.which("node") is not None


def write_settings_with_backup(settings_path, new_settings):
    backup_path = None
    if os.path.exists(settings_path):
        backup_path = f"{settings_path}.bak-{int(time.time())}"
        shutil.copy2(settings_path, backup_path)
    tmp = settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(new_settings, f, indent=2)
    os.replace(tmp, settings_path)
    return backup_path


# Written verbatim to <claude_config_dir>/ai_fluency_insights_statusline.js by
# run_setup_notifications() -- never to statusline.js or any user-chosen path.
# Trimmed from the hand-written ~/.claude/statusline.js already in use on the
# maintainer's machine: same merge-onto-previous-snapshot side effect, plus a
# minimal one-line status output for users who had no statusLine at all.
BUNDLED_STATUSLINE_JS = r"""// ai-fluency-insights:managed-statusline
// Captures Anthropic's real rate_limits (five_hour/seven_day) for
// ai_fluency_insights.py's plan-usage panel and toast notifications.
// See docs/DASHBOARD_GUIDE.md#plan-usage-limits.
const fs = require('fs');
const os = require('os');
const path = require('path');

let raw = '';
process.stdin.on('data', d => raw += d);
process.stdin.on('end', () => {
  let j = {};
  try { j = JSON.parse(raw); } catch (e) { /* fall through with j={} */ }

  if (j.rate_limits) {
    const outPath = path.join(os.homedir(), '.claude', 'rate_limits_latest.json');
    let merged = {};
    try {
      const prev = JSON.parse(fs.readFileSync(outPath, 'utf8'));
      merged = Object.assign({}, prev.rate_limits);
    } catch (e) { /* no previous snapshot, or unreadable -- start fresh */ }
    Object.assign(merged, j.rate_limits);
    const payload = JSON.stringify({
      rate_limits: merged,
      captured_at: Date.now() / 1000,
    });
    try {
      const tmp = outPath + '.tmp';
      fs.writeFileSync(tmp, payload);
      fs.renameSync(tmp, outPath);
    } catch (e) { /* best-effort; never break the status line over this */ }
  }

  const dir = (j.workspace && j.workspace.current_dir) || j.cwd || '';
  const model = (j.model && j.model.display_name) || '';
  const ctx = j.context_window && j.context_window.used_percentage;
  const rl = j.rate_limits || {};
  const five = rl.five_hour && rl.five_hour.used_percentage;
  const seven = rl.seven_day && rl.seven_day.used_percentage;

  let line = dir + '  ' + model;
  if (ctx != null) line += `  ctx ${Math.round(ctx)}%`;
  if (five != null) line += `  5h ${Math.round(five)}%`;
  if (seven != null) line += `  wk ${Math.round(seven)}%`;
  process.stdout.write(line);
});
"""


def _print_final_report(print_fn):
    fresh = load_rate_limits(RATE_LIMITS_PATH) is not None
    print_fn("")
    print_fn("Rate-limit capture:  " + ("ok, fresh" if fresh else
              "not yet captured (will appear after your next Claude Code message)"))
    if os.name != "nt":
        print_fn("Toast notifications: OFF (Windows only)")
    else:
        print_fn("Toast notifications: " + ("ON" if should_run_notifications() else "OFF") +
                  '  (edit NOTIFY["enabled"] in the script to change)')


def run_setup_notifications(claude_dir=None, dry_run=False, input_fn=input,
                             print_fn=print, isatty_fn=None):
    isatty_fn = isatty_fn or sys.stdin.isatty
    claude_dir = claude_dir or claude_config_dir()
    settings_path = os.path.join(claude_dir, "settings.json")
    statusline_js_path = os.path.join(claude_dir, STATUSLINE_MARKER)

    if os.name != "nt":
        print_fn("Toast notifications aren't available on this OS yet -- "
                  "setting up rate-limit capture only.")

    settings, err = load_settings_json(settings_path)
    if err:
        print_fn(f"[!] {err}")
        print_fn("    Fix or remove this file, then re-run --setup-notifications.")
        return 1

    kind = classify_statusline(settings)

    if kind == "foreign":
        _report_foreign_statusline(settings, print_fn)
        _print_final_report(print_fn)
        return 0

    if not node_available():
        print_fn("[!] `node` was not found on PATH. Claude Code itself requires "
                  "Node.js, so this is unexpected -- install it and re-run.")
        return 1

    new_settings = merge_statusline_config(settings, statusline_js_path)
    if new_settings == settings and os.path.isfile(statusline_js_path):
        print_fn(f"Already configured -- {settings_path} needs no changes.")
        _print_final_report(print_fn)
        return 0

    print_fn("This will write:")
    print_fn(f"  {statusline_js_path}")
    print_fn(f"  statusLine block in {settings_path}:")
    print_fn(json.dumps(new_settings["statusLine"], indent=2))

    if dry_run:
        print_fn("Dry run -- nothing written.")
        return 0

    if not isatty_fn():
        print_fn("[!] Not running interactively -- re-run from a terminal, "
                  "or pass --dry-run to preview only.")
        return 1

    answer = input_fn("Proceed? [y/N] ").strip().lower()
    if answer != "y":
        print_fn("Cancelled -- nothing written.")
        return 0

    with open(statusline_js_path, "w", encoding="utf-8") as f:
        f.write(BUNDLED_STATUSLINE_JS)
    try:
        backup_path = write_settings_with_backup(settings_path, new_settings)
    except OSError as e:
        print_fn(f"[!] Could not write {settings_path}: {e}")
        print_fn(f"    {statusline_js_path} was written, but the statusLine hook "
                  "isn't wired in yet -- fix the permissions and re-run.")
        return 1
    if backup_path:
        print_fn(f"Backed up previous settings to {backup_path}")
    print_fn(f"Wrote {statusline_js_path} and updated {settings_path}.")
    _print_final_report(print_fn)
    return 0


def _extract_script_path(command):
    m = re.search(r'"([^"]+)"', command)
    return m.group(1) if m else None


def _report_foreign_statusline(settings, print_fn):
    cmd = settings["statusLine"]["command"]
    print_fn(f"An existing statusLine is already configured: {cmd}")
    script_path = _extract_script_path(cmd)
    try:
        if (script_path and os.path.isfile(script_path)
                and os.path.getsize(script_path) < 1_000_000):
            with open(script_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if "rate_limits" in content:
                print_fn("Your existing script already looks like it references rate_limits.")
    except Exception:
        pass
    if load_rate_limits(RATE_LIMITS_PATH) is not None:
        print_fn(f"{RATE_LIMITS_PATH} already has a fresh capture -- you may already be covered.")
    else:
        print_fn(f"{RATE_LIMITS_PATH} has no fresh capture yet.")
    print_fn("To add rate-limit capture to your own script, make it write this JSON")
    print_fn("shape to ~/.claude/rate_limits_latest.json whenever `rate_limits` is")
    print_fn("present in its stdin input (see the reference script below):")
    print_fn(BUNDLED_STATUSLINE_JS)
    print_fn("Full explanation: docs/DASHBOARD_GUIDE.md#plan-usage-limits")


# ---------------------------------------------------------------------------
# Setup (--setup-copilot): turns on VS Code Copilot's official OpenTelemetry file export so
# new chats record exact per-call token counts. Opt-in; edits the user's real settings.json,
# so it asks first, backs the file up, and refuses anything but strict JSON.
# ---------------------------------------------------------------------------

COPILOT_OTEL_KEYS = ("github.copilot.chat.otel.enabled",
                     "github.copilot.chat.otel.exporterType",
                     "github.copilot.chat.otel.outfile")


def _is_stable_code_user(root):
    parts = os.path.normpath(root).replace("\\", "/").lower().split("/")
    return parts[-2:] == ["code", "user"]


def _copilot_setup_target(user_roots):
    """settings.json of the first stable VS Code user folder (not a profile, not Insiders),
    else of the first root given."""
    roots = [r for r in user_roots if r]
    for r in roots:
        if _is_stable_code_user(r):
            return os.path.join(r, "settings.json")
    return os.path.join(roots[0], "settings.json") if roots else None


def _read_strict_settings(path):
    """(settings dict, newline style, error). A missing or blank file is an empty settings
    object. VS Code writes a BOM on some machines, and CRLF on Windows; both are read through."""
    if not os.path.exists(path):
        return {}, "\n", None
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return None, "\n", f"could not read {path}: {e}"
    newline = "\r\n" if "\r\n" in text else "\n"
    if not text.strip():
        return {}, newline, None
    try:
        data = json.loads(text)
    except ValueError as e:
        return None, newline, f"{path} is not plain JSON ({e}); it probably has comments or trailing commas"
    if not isinstance(data, dict):
        return None, newline, f"{path} does not hold a JSON object"
    return data, newline, None


def _write_copilot_settings(path, data, newline):
    """Copy the old file to settings.json.bak-aifi-<time>, then replace it. Returns the backup
    path. If the write fails, the temp file is removed and the original and backup stay as they are.
    A symlinked settings.json (a dotfiles repo) is backed up and replaced at its target, so the link stays."""
    path = os.path.realpath(path)
    backup = None
    if os.path.exists(path):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{path}.bak-aifi-{stamp}"
        n = 1
        while os.path.exists(backup):
            n += 1
            backup = f"{path}.bak-aifi-{stamp}-{n}"
        shutil.copy2(path, backup)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps(data, indent=4, ensure_ascii=False).replace("\n", newline) + newline)
        os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return backup


def _console_safe(print_fn, encoding=None):
    """Wrap print_fn so a path with a character the console can't show (a cp1252 console and a
    CJK folder name) prints as '?' instead of raising UnicodeEncodeError."""
    def safe(*args):
        enc = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
        text = " ".join(str(a) for a in args)
        try:
            text = text.encode(enc, errors="replace").decode(enc, errors="replace")
        except LookupError:
            pass
        print_fn(text)
    return safe


def run_setup_copilot(user_roots, dry_run=False, undo=False, input_fn=input,
                      print_fn=print, home=None, encoding=None):
    """Turn on (or, with undo, off) Copilot's exact-token OTel file export. Returns an exit code:
    0 done / nothing to do / declined, 1 nothing to edit, 2 settings.json is not plain JSON."""
    print_fn = _console_safe(print_fn, encoding)
    settings_path = _copilot_setup_target(user_roots)
    if not settings_path:
        print_fn("[!] No VS Code user folder found, so there is no settings.json to edit.")
        print_fn("    Pass one explicitly:  python ai_fluency_insights.py --setup-copilot "
                 "--copilot-root <path to VS Code's User folder>")
        return 1

    k_on, k_exp, k_out = COPILOT_OTEL_KEYS
    our_outfile = os.path.join(home or os.path.expanduser("~"), ".copilot", "otel",
                               "vscode-copilot-otel.jsonl")

    settings, newline, err = _read_strict_settings(settings_path)
    if err:
        print_fn(f"[!] {err}")
        print_fn("    Not editing it.")
        if undo:
            print_fn("    Remove these three settings by hand "
                     "(Ctrl+Shift+P -> Preferences: Open User Settings (JSON)):")
            for k in COPILOT_OTEL_KEYS:
                print_fn(f"    {k}")
        else:
            print_fn("    Paste these into your VS Code settings "
                     "(Ctrl+Shift+P -> Preferences: Open User Settings (JSON)).")
            for k, v in zip(COPILOT_OTEL_KEYS, (True, "file", our_outfile)):
                print_fn(f"    {json.dumps(k)}: {json.dumps(v)},")
        return 2

    exporter = settings.get(k_exp)
    foreign_exporter = exporter not in (None, "", "file")
    kept = []                                    # (key, why) shown as kept, not changed
    new_settings = dict(settings)               # a copy keeps key order; updates stay in place
    if undo:
        if foreign_exporter:
            print_fn(f"Your {k_exp} is {json.dumps(exporter)}, which this tool did not set, "
                     "so nothing was changed.")
            print_fn("    Remove the github.copilot.chat.otel.* settings by hand if you want them gone.")
            return 0
        new_settings.pop(k_on, None)
        new_settings.pop(k_exp, None)
        if new_settings.get(k_out) == our_outfile:
            new_settings.pop(k_out)
        elif k_out in new_settings:
            kept.append((k_out, "not the path this tool sets"))
    elif foreign_exporter:
        print_fn(f"[!] Your {k_exp} is {json.dumps(exporter)}. Exact-token logging here needs "
                 f'{k_exp} "file".')
        print_fn("    Left your exporter and the other github.copilot.chat.otel.* settings as they are.")
        print_fn(f'    To use it, change {k_exp} to "file" by hand (Ctrl+Shift+P -> '
                 "Preferences: Open User Settings (JSON)), then re-run --setup-copilot.")
        return 0
    else:
        new_settings[k_on] = True
        new_settings[k_exp] = "file"
        mine = settings.get(k_out)
        if isinstance(mine, str) and mine.strip():
            kept.append((k_out, "your own setting; the dashboard follows it"))
        else:
            new_settings[k_out] = our_outfile

    if new_settings == settings:
        print_fn(f"Nothing to remove -- {settings_path} has no Copilot OTel settings."
                 if undo else f"Already configured -- {settings_path} needs no changes.")
        return 0

    print_fn(("This will remove these settings from " if undo else "This will set these in ")
             + settings_path + ":")
    for k in COPILOT_OTEL_KEYS:
        old, new = settings.get(k), new_settings.get(k)
        if k in dict(kept):
            print_fn(f"  = {json.dumps(k)}: {json.dumps(old)}   (kept: {dict(kept)[k]})")
        elif k in settings and old == new:
            continue
        elif undo:
            print_fn(f"  - {json.dumps(k)}: {json.dumps(old)}")
        elif k in settings:
            print_fn(f"  ~ {json.dumps(k)}: {json.dumps(new)}   (was {json.dumps(old)})")
        else:
            print_fn(f"  + {json.dumps(k)}: {json.dumps(new)}")
    if not undo and new_settings.get(k_out) == our_outfile:
        print_fn(f"  The folder {os.path.dirname(our_outfile)} will be created for the log file.")
    print_fn("  Every other setting stays as it is, and the old file is backed up first.")

    if dry_run:
        print_fn("Dry run -- nothing written.")
        return 0

    try:
        answer = input_fn("Apply these changes? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        print_fn("Cancelled -- nothing written.")
        return 0

    try:
        if not undo and new_settings.get(k_out) == our_outfile:
            os.makedirs(os.path.dirname(our_outfile), exist_ok=True)
        backup = _write_copilot_settings(settings_path, new_settings, newline)
    except OSError as e:
        print_fn(f"[!] Could not update {settings_path}: {e}")
        print_fn("    Your settings.json was left as it was.")
        return 1
    if backup:
        print_fn(f"Backed up previous settings to {backup}")
    if undo:
        print_fn(f"Removed the Copilot OTel settings from {settings_path}.")
        if backup:
            print_fn(f"To restore what you had before this undo, copy {backup} over settings.json. "
                     "Earlier backups are named settings.json.bak-aifi-*.")
        print_fn("Reload VS Code windows (Developer: Reload Window) to stop the logging.")
    else:
        print_fn(f"Updated {settings_path}.")
        print_fn("Reload VS Code windows (Developer: Reload Window). Chats after that get exact token counts.")
        print_fn('Once new chats are logged, Data Health will show "Exact tokens".')
    return 0


def startup_hint(rate_limits_path=None, is_windows=None, statusline_js_path=None):
    rate_limits_path = rate_limits_path or RATE_LIMITS_PATH
    is_windows = (os.name == "nt") if is_windows is None else is_windows
    statusline_js_path = statusline_js_path or os.path.join(claude_config_dir(), STATUSLINE_MARKER)
    # Right after a successful --setup-notifications run, rate_limits_latest.json
    # doesn't exist yet (only the *next* Claude Code message populates it), but
    # our managed script does -- checking for it too avoids nudging someone who
    # just finished the setup this tip is telling them to run.
    if (is_windows and not os.path.exists(rate_limits_path)
            and not os.path.exists(statusline_js_path)):
        return "  Tip: plan-usage % is a local estimate. Run --setup-notifications for real Anthropic numbers."
    return None


class DashboardServer(ThreadingHTTPServer):
    """The dashboard's HTTP server, holding its port exclusively. Python's default lets a second
    program open a port that is already listening on Windows, and the browser then gets either
    one at random; this way a busy port fails at startup with a clear message instead."""
    allow_reuse_address = os.name != "nt"     # POSIX: a quick restart may reuse a closing port

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def original_dashboard_running(port=ORIGINAL_DASHBOARD_PORT, timeout=2.0):
    """True when an AI Fluency Insights page answers on localhost:`port` (the original Claude-only
    dashboard). Localhost only; any error means no."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read()               # read it all: closing early would log an error over there
        return resp.status == 200 and b"<title>AI Fluency Insights</title>" in body[:8192]
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def default_root():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return os.path.join(env, "projects")
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def main():
    ap = argparse.ArgumentParser(description="Local Claude Code usage dashboard (zero token cost)")
    ap.add_argument("--dir", default=default_root(), help="Claude projects log dir")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"default {DEFAULT_PORT} (the original Claude-only dashboard uses "
                         f"{ORIGINAL_DASHBOARD_PORT}, so both can run)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--dump", action="store_true", help="print stats JSON and exit (for testing)")
    ap.add_argument("--install-startup", action="store_true",
                    help="Windows: run dashboard automatically at logon (Task Scheduler)")
    ap.add_argument("--uninstall-startup", action="store_true",
                    help="Windows: remove the logon task")
    ap.add_argument("--setup-notifications", action="store_true",
                    help="Wire up the statusLine hook for accurate plan-usage %% "
                         "(toast notifications: Windows only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --setup-notifications or --setup-copilot, show what would "
                         "change without writing anything")
    ap.add_argument("--setup-copilot", action="store_true",
                    help="Turn on GitHub Copilot's OpenTelemetry file export in your VS Code "
                         "user settings.json (the stable Code/User one when found) so new "
                         "chats log exact token counts (asks first, backs the file up)")
    ap.add_argument("--undo", action="store_true",
                    help="With --setup-copilot, remove those settings again")
    ap.add_argument("--copilot-root", action="append", default=[], metavar="PATH",
                    help="extra VS Code user folder to read GitHub Copilot logs from "
                         "(repeatable)")
    ap.add_argument("--no-copilot", action="store_true",
                    help="ignore GitHub Copilot logs")
    ap.add_argument("--source", choices=["claude", "copilot", "all"],
                    help="with --dump: which tool's stats to print "
                         "(default: claude, or whichever tool has logs)")
    args = ap.parse_args()

    if args.undo and not args.setup_copilot:
        ap.error("--undo only works together with --setup-copilot")

    if args.setup_notifications:
        sys.exit(run_setup_notifications(dry_run=args.dry_run))

    if args.setup_copilot:
        sys.exit(run_setup_copilot(copilot_source.find_user_roots(args.copilot_root),
                                   dry_run=args.dry_run, undo=args.undo))

    if args.install_startup or args.uninstall_startup:
        if os.name != "nt":
            sys.exit("Startup install is Windows-only.")
        import subprocess
        task = STARTUP_TASK
        if args.uninstall_startup:
            subprocess.run(["schtasks", "/Delete", "/TN", task, "/F"])
            print("Removed logon task.")
            return
        pyw = sys.executable.replace("python.exe", "pythonw.exe")
        if not os.path.exists(pyw):
            pyw = sys.executable
        script = os.path.abspath(__file__)
        cmd = f'"{pyw}" "{script}" --no-browser --port {args.port}'
        r = subprocess.run(["schtasks", "/Create", "/TN", task, "/TR", cmd,
                            "/SC", "ONLOGON", "/F"])
        if r.returncode == 0:
            print(f"Installed: dashboard will start at logon on port {args.port}.")
            print("Remove anytime with:  python ai_fluency_insights.py --uninstall-startup")
        else:
            print("Failed — try running the terminal as Administrator.")
        return

    copilot = None if args.no_copilot else CopilotScanner(
        copilot_source.find_user_roots(args.copilot_root), copilot_source.default_copilot_home())
    enabled = enabled_sources(args.dir, copilot)
    if not enabled:
        print(f"[!] Log directory not found: {args.dir}")
        print("    Pass it explicitly:  python ai_fluency_insights.py --dir <path>")
        if copilot is not None:
            print("    No GitHub Copilot logs found either; point at a VS Code user folder "
                  "with --copilot-root <path>")
        sys.exit(1)

    # Copilot stays listed once it is not turned off, so logs that appear later are picked up.
    scanners = {}
    if "claude" in enabled:
        scanners["claude"] = Scanner(args.dir)
    if copilot is not None:
        scanners["copilot"] = copilot

    if args.dump:
        # "all" needs every tool; with one missing it falls back like an unknown source does.
        if args.source not in (None, "all") and args.source not in scanners:
            sys.exit(f"[!] No {SOURCES[args.source]['label']} logs to dump "
                     "(see --dir, --copilot-root, --no-copilot).")
        if wants_all_ai(scanners, args.source):
            data = build_payload(scanners, parse_qs("days=0"), source="all")
        else:
            src = pick_source(scanners, args.source or "claude")
            data = build_payload(scanners[src], parse_qs("days=0"), source=src)
        data["sources"] = sources_status(scanners)
        print(json.dumps(data, indent=2))
        return

    claude = scanners.get("claude")
    watcher = NotificationWatcher(claude) if claude else None

    Handler.scanners = scanners
    Handler.scanner = claude
    Handler.root = args.dir
    Handler.watcher = watcher
    try:
        srv = DashboardServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(f"[!] Port {args.port} is already in use ({e.strerror or e}). Is another dashboard "
                 f"running? Open it, stop it, or pick another port:  "
                 f"python ai_fluency_insights.py --port {args.port + 1}")
    url = f"http://localhost:{args.port}"
    print(f"  AI Fluency Insights running at {url}")
    if claude:
        print(f"  Reading logs from: {args.dir}")
    if copilot is not None:
        print("  GitHub Copilot logs: " + ("found" if "copilot" in enabled else "none found yet"))
    hint = startup_hint() if claude else None
    if hint:
        print(hint)
    print("  Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    def _notify_loop():
        checked = 0.0
        while True:
            try:
                if args.port != ORIGINAL_DASHBOARD_PORT and time.time() - checked >= 60:
                    checked = time.time()
                    muted = original_dashboard_running()
                    if muted != watcher.muted:
                        print("  Toast notifications: " + (
                            f"paused, the original dashboard on port {ORIGINAL_DASHBOARD_PORT} shows them"
                            if muted else "back on"))
                    watcher.muted = muted
                watcher.poll_once()
            except Exception as e:
                print(f"[warn] notification watcher error: {e}")
            time.sleep(NOTIFY["poll_seconds"])

    if watcher is None:
        print("  Toast notifications: OFF (they watch Claude Code, which has no logs here)")
    elif should_run_notifications():
        threading.Thread(target=_notify_loop, daemon=True).start()
    else:
        print('  Toast notifications: OFF (NOTIFY["enabled"] is False)')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Bye!")


if __name__ == "__main__":
    main()
