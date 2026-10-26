# GitHub Copilot Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add GitHub Copilot (VS Code + Copilot CLI) as a second data source to AI Fluency Insights, with a Claude Code / GitHub Copilot / All AI switch, without mixing the two tools' numbers.

**Architecture:**
- A new stdlib module `copilot_source.py` finds Copilot's local logs and translates every Copilot session into the same Claude-shaped events that `parse_entry()` already consumes.
- A `CopilotScanner` in the main file feeds those events into its **own** `Stats`. Every existing `build_*` engine and `stats_to_json()` then work on Copilot data unchanged.
- `/api/stats` gains `source=claude|copilot|all`. `all` is a separate, smaller payload built by combining rules: tokens summed with a per-tool split, time as a union of 10-minute slots, cost never summed.

**Tech Stack:** Python 3.8+ standard library only (`http.server`, `json`, `sqlite3`, `unittest`). Vanilla JS + bundled Chart.js inside the `HTML` string in `ai_fluency_insights.py`.

**Spec:** `docs/superpowers/specs/2026-10-24-copilot-source-design.md`. Read it first; this plan argues from it.

## Global Constraints

- Python 3.8+, **standard library only**. No pip installs, no network calls, works offline. Chart.js stays bundled.
- **Read-only on Copilot's files.** Open SQLite with `?mode=ro&immutable=1`; if a `-wal` file exists, read a temp copy instead (as `tools/copilot_log_check.py` does).
- Work only in repo `dev-token-dashboard-multi-ai`, branch `copilot-source`. **Never touch the original `dev-token-dashboard` repo.**
- **Claude view regression.** For "All time" and custom from–to ranges, `source=claude` output must equal the original dashboard's (Task 1 golden test). Only rolling N-day windows change (D8).
- Rolling "Last N days" = **today and the N−1 days before it**, local time. The previous window (for Δ) = the N days before that. The same window applies to every source.
- **Never fake zeros**: an unavailable Copilot metric renders "Not available for Copilot" plus a reason.
- **Never sum cost across tools**: Claude cost is `usd` (estimate); Copilot cost is `credits`; premium requests are a third, separate unit.
- **All AI time** = union of 10-minute activity slots across tools, never a sum.
- Accent colours:

  | Source | Light | Dark |
  |---|---|---|
  | Claude | existing `--acc` (unchanged) | existing `--acc` (unchanged) |
  | Copilot | `#7355ea` | `#a78bfa` |
  | All AI | `#0d8f89` | `#2dd4bf` |
- The tool choice persists in `localStorage` key `dtd-source`. Every storage call is wrapped in try/catch like the existing `dtd-theme`.
- Copilot credits = `nanoAiu / 1e9`. `copilotCredits` in chat sessions is already in credits.
- Test command: `python -m unittest -v`, run from the repo root. It discovers `test_*.py`.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

These failure modes are implied by the spec but no feature test naturally hits them. Each line names the task whose tests pin it.

1. **A chat or CLI file being appended to while it's scanned** (half-written last line). Expect: that line is skipped and counted, no crash, the rest still parsed. *Pinned in Task 5 (`test_replay_skips_truncated_last_line`) and Task 6.*
2. **Months of Copilot history refreshed every 15 s.** Expect: unchanged files are not re-read. *Pinned in Task 8 (`test_unchanged_files_not_reparsed`).*
3. **Epoch-millisecond vs ISO timestamps near midnight.** Expect: a request lands on the same local day the Claude side would use. *Pinned in Task 5 (`test_iso_utc_formats`).*
4. **A machine with Copilot logs but no `~/.claude/projects`** (a teammate without Claude Code). Expect: the dashboard still starts with only the Copilot view. *Pinned in Task 2 (`test_enabled_sources_copilot_only`).*
5. **A new Copilot release adds record types or moves a field.** Expect: unknown lines are ignored, counted, and shown in Copilot Data Health. *Pinned in Task 6 (`test_unknown_event_types_counted`) and Task 9.*

---

## File Structure

| File | Responsibility |
|---|---|
| `copilot_source.py` (new) | Copilot only, pure functions, no import of the main module. Discovery of roots and stores, chat-session replay, translators to Claude-shaped events, exact-usage readers (OTel, debug logs), session-store reader, project labels, tool vocabulary and lines-of-code counting. |
| `ai_fluency_insights.py` (modify) | Engine and UI. Gets the `parse_entry` hooks, `SOURCES` registry, `range_window`, `build_payload`, `CopilotScanner`, `build_health_copilot`, `COPILOT_ADOPTION_FEATURES`, `build_all_ai`, `run_setup_copilot`, server routing, and all HTML/CSS/JS changes. |
| `copilot_fixtures.py` (new) | Test helpers that write synthetic Copilot files into a temp tree. |
| `test_fixtures/claude/-demo-proj/s1.jsonl`, `s2.jsonl` (new) | Claude fixture for the regression test. |
| `test_fixtures/claude_golden_all.json`, `claude_golden_custom.json` (new) | Output captured from the **original** code. |
| `test_copilot_source.py` (new) | Tests for `copilot_source.py`. |
| `test_multi_source.py` (new) | Tests for the payload, scanner, All AI and setup. |
| Docs (modify) | README, docs/HOW_IT_WORKS.md, docs/METRICS.md, docs/DASHBOARD_GUIDE.md, SETUP.md (Task 14). |

**Event shapes the translators emit.** Every task that produces or consumes events uses these exactly.

```python
# user prompt
{"type": "user", "timestamp": ISO, "sessionId": sid, "cwd": cwd, "gitBranch": branch,
 "message": {"content": "prompt text"}}
# tool result (a user-type entry, like Claude's)
{"type": "user", "timestamp": ISO, "sessionId": sid,
 "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": bool, "content": ""}]}}
# assistant step / LLM call
{"type": "assistant", "timestamp": ISO, "sessionId": sid, "requestId": rid, "cwd": cwd, "gitBranch": branch,
 "isSidechain": bool,                       # only when True
 "message": {"id": unique_id, "model": model_or_"unknown",
             "usage": {} | {"input_tokens": int, "output_tokens": int,
                            "cache_read_input_tokens": int, "cache_creation_input_tokens": int},
             "stop_reason": "end_turn" | None,
             "content": [{"type": "tool_use", "id": tid, "name": tool, "input": {..., "_loc": int, "file_path": str}}]},
 "_cost_override": float,                   # credits; only when known
 "_premium": float}                         # premium-request multiplier; only when the log reports one
```

`ISO` is always `iso_utc(...)` (Task 5): UTC with milliseconds and a `+00:00` offset, for
example `2026-10-01T12:00:00.000+00:00`.

---

### Task 1: Calendar-day windows and a reusable payload builder (Claude only)

**Files:**
- Create: `test_fixtures/claude/-demo-proj/s1.jsonl`, `test_fixtures/claude/-demo-proj/s2.jsonl`
- Create: `test_fixtures/claude_golden_all.json`, `test_fixtures/claude_golden_custom.json` (generated, Step 2)
- Create: `test_multi_source.py`
- Modify: `ai_fluency_insights.py`, the `/api/stats` branch of `Handler.do_GET` (around lines 5072–5145)

**Interfaces:**
- Produces: `range_window(days: int, today: date) -> tuple[str, str, str, str]`. Returns `(cur0, cur1, prev0, prev1)` as ISO dates; `days > 0`.
- Produces: `build_payload(scanner, q: dict, today: date | None = None) -> dict`.
  - `q` is `parse_qs` output; `today` defaults to `date.today()`.
  - It returns everything the old `/api/stats` built **except** `notifications`, `plan_window` and `root`. `Handler` adds those afterwards, and only for Claude.

- [ ] **Step 1: Write the Claude fixture.**

`s1.jsonl` has session `s1`, cwd `/demo/proj`, branch `main`:
- **2026-10-01 12:00:00Z**, user: "Add a hello function"
- **12:00:05Z**, assistant: id `m1`, requestId `r1`, model `claude-sonnet-4-5`.
  - usage: input 100, output 50, cache_read 1000, cache_creation 200
  - `Write` tool_use `t1`, `file_path` `/demo/proj/hello.py`, content `"def hello():\n    return 'hi'\n"`
- **12:00:06Z**, tool_result for `t1`, `is_error` false
- **12:00:10Z**, assistant: id `m2`, requestId `r2`, `stop_reason` `end_turn`.
  - usage: 20 / 10 / 1200 / 0
- an `ai-title` line: `"Add hello function"`
- **2026-10-03 12:00:00Z**, user: "Run the tests"
- **12:00:04Z**, assistant: id `m3`.
  - usage: 30 / 15 / 500 / 0
  - `Bash` tool_use `t2`, `{"command": "pytest -q"}`
- **12:00:09Z**, tool_result for `t2`, `is_error` true, content `"Exit code 1"`
- **12:00:12Z**, assistant: id `m4`, `end_turn`.
  - usage: 10 / 5 / 600 / 0

`s2.jsonl` has session `s2`, same cwd:
- **2026-10-10 12:00:00Z**, user: "Explain the code"
- **12:00:08Z**, assistant: id `m5`, model `claude-opus-4-1`, `end_turn`.
  - usage: 40 / 80 / 0 / 100

Use the full Claude entry shape that `parse_entry` reads: `type`, `timestamp`, `sessionId`, `cwd`, `gitBranch`, `requestId`, `message.{id, model, usage, stop_reason, content}`.

- [ ] **Step 2: Capture golden output from the CURRENT code. Do this before any refactor.**

```bash
python - <<'EOF'
import json, threading, urllib.request
from http.server import ThreadingHTTPServer
import ai_fluency_insights as afi
afi.Handler.scanner = afi.Scanner("test_fixtures/claude")
afi.Handler.root = "test_fixtures/claude"
srv = ThreadingHTTPServer(("127.0.0.1", 0), afi.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
for name, qs in (("all", "days=0"), ("custom", "from=2026-10-01&to=2026-10-05")):
    d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/api/stats?{qs}"))
    for k in ("generated", "notifications", "plan_window", "root"):
        d.pop(k, None)
    with open(f"test_fixtures/claude_golden_{name}.json", "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1, sort_keys=True)
srv.shutdown()
EOF
```

Expected: two JSON files. `claude_golden_all.json` has `totals.prompts == 3` and `totals.loc == 3`.

- [ ] **Step 3: Write the failing tests in `test_multi_source.py`.**

```python
FIX = os.path.join(os.path.dirname(__file__), "test_fixtures", "claude")

def comparable(d):
    """JSON round-trip, then drop clock- and timezone-sensitive parts."""
    d = json.loads(json.dumps(d))
    for k in ("generated", "notifications", "plan_window", "root", "heatmap"):
        d.pop(k, None)
    for day in d.get("days", []):
        day.pop("late_msgs", None)
    return d

class PayloadRegressionTests(unittest.TestCase):
    def check(self, qs, golden):
        new = comparable(afi.build_payload(afi.Scanner(FIX), parse_qs(qs)))
        old = comparable(json.load(open(os.path.join(os.path.dirname(FIX), golden), encoding="utf-8")))
        for k in old:                      # new keys may be added; old ones must not change
            self.assertEqual(new.get(k), old[k], k)

    def test_all_time_matches_golden(self):
        self.check("days=0", "claude_golden_all.json")

    def test_custom_range_matches_golden(self):
        self.check("from=2026-10-01&to=2026-10-05", "claude_golden_custom.json")

class RangeWindowTests(unittest.TestCase):
    def test_range_window_calendar(self):
        self.assertEqual(afi.range_window(7, date(2026, 10, 24)),
                         ("2026-10-18", "2026-10-24", "2026-10-11", "2026-10-17"))

    def test_rolling_window_counts_calendar_days(self):
        d = afi.build_payload(afi.Scanner(FIX), parse_qs("days=7"), today=date(2026, 10, 10))
        self.assertEqual((d["totals"]["prompts"], d["totals"]["output"]), (1, 80))  # only 10-10
        self.assertEqual((d["prev"]["prompts"], d["prev"]["output"]), (2, 80))      # 10-01 + 10-03
```

- [ ] **Step 4: Run them and confirm they fail.**

Run: `python -m unittest test_multi_source -v`
Expected: FAIL / ERROR with `AttributeError: module 'ai_fluency_insights' has no attribute 'build_payload'`.

- [ ] **Step 5: Implement `range_window` and `build_payload`.**
  - Move the `/api/stats` body out of `Handler.do_GET` into `build_payload(scanner, q, today=None)`.
  - Replace the rolling branch: `cur0, cur1, prev0, prev1 = range_window(days, today)`.
    - `ranged = scanner.build_stats(cur0, cur1)`.
    - Flow/Quality/Value window `b0, b1 = cur0, cur1`. `prev_insight` uses `(prev0, prev1)`.
    - `data["prev"]` sums `full.days` over days in `[prev0, prev1]`. The session count uses the same range.
  - The all-time and custom branches are unchanged.
  - `do_GET` becomes: `data = build_payload(self.scanner, q)`, then add `notifications`, `plan_window`, `root` as before.

- [ ] **Step 6: Run the new and the existing tests.**

Run: `python -m unittest -v`
Expected: all PASS (the 144 existing tests plus 4 new ones).

- [ ] **Step 7: Commit.**

```bash
git add test_fixtures test_multi_source.py ai_fluency_insights.py
git commit -m "Use calendar days for rolling ranges and extract build_payload"
```

---

### Task 2: Source registry, Copilot discovery, and `source=` routing

**Files:**
- Create: `copilot_source.py` (discovery part), `copilot_fixtures.py`
- Create: `test_copilot_source.py`
- Modify: `ai_fluency_insights.py` (`SOURCES`, `CopilotScanner` shell, `Handler`, `main()`); `test_multi_source.py`

**Interfaces:**
- Produces in `copilot_source.py`:
  - `VARIANTS = ["Code", "Code - Insiders", "Code - Exploration", "VSCodium"]`
  - `EXT_FOLDERS = ["GitHub.copilot-chat", "github.copilot-chat"]`
  - `find_user_roots(extra=(), home=None, appdata=None) -> list[str]`
    - Every existing `<base>/<variant>/User`, plus each `User/profiles/<id>`, plus `~/.vscode-server/data/User`, plus `extra`.
    - `base` is `appdata` on Windows, `~/Library/Application Support` on macOS, `$XDG_CONFIG_HOME` or `~/.config` on Linux.
    - De-duplicated by `os.path.normcase(os.path.realpath())`.
  - `discover(user_roots: list[str], copilot_home: str) -> dict` returning:
    ```python
    {"chat": {sid: {"path": str, "workspace": ws_dir_or_""}},        # chatSessions/*.json(l), emptyWindowChatSessions/*
     "transcripts": {sid: {"path": str, "workspace": ws_dir}},
     "debug": {sid: [paths]},                                          # debug-logs/<sid>/*.jsonl
     "otel": [paths],                                                  # settings outfile, COPILOT_OTEL_FILE_EXPORTER_PATH, <copilot_home>/otel/*.jsonl
     "cli": {sid: path},                                               # <copilot_home>/session-state/<sid>/events.jsonl
     "stores": [db_paths],                                             # globalStorage/<ext>/session-store.db
     "settings": [settings.json paths]}
    ```
    `sid` = the file's base name without extension (for CLI, the folder name).
- Produces in the main file:
  - `SOURCES` registry:
    ```python
    {"claude": {"label": "Claude Code", "short": "Claude", "cost_unit": "usd",
                "instructions": ("CLAUDE.md", "AGENTS.md"), "skip_recs": ()},
     "copilot": {"label": "GitHub Copilot", "short": "Copilot", "cost_unit": "credits",
                 "instructions": (".github/copilot-instructions.md", "AGENTS.md"),
                 "skip_recs": ("coldstart", "cachereset")}}
    ```
  - `class CopilotScanner`:
    - `__init__(self, user_roots, copilot_home)`
    - `.files() -> dict`: the `discover()` result, re-run at most every 15 s
    - `.available() -> bool`: any chat, transcript or CLI session
    - `.build_stats(min_day=None, max_day=None) -> Stats`: an empty `Stats()` in this task; filled in Task 8
  - `enabled_sources(claude_dir: str, copilot: CopilotScanner | None) -> list[str]`
  - `build_payload(scanner, q, today=None, source="claude")`: adds `data["source"]`, `data["cost_unit"]`, `data["sources"] = [{"id", "label", "short", "available"}]`
  - `Handler.scanners: dict[str, scanner]`, chosen by `q["source"]`. A missing or unknown value means `claude`.
  - `/api/ai_summary` and `/api/reflect` take the same `source` and read the weeks of that tool's scanner. For `source=all` they return `{"ok": False, "error": "pick one tool"}`.
- `copilot_fixtures.py` builders. Each writes UTF-8 JSON / JSONL and returns the path it wrote:
  - `make_user_root(tmp) -> str`
  - `add_workspace(user_root, ws_hash, folder) -> ws_dir` (writes `workspace.json` `{"folder": "file:///..."}`)
  - `add_chat_jsonl(ws_dir, sid, lines: list[dict])`
  - `add_chat_json(ws_dir, sid, obj)`
  - `add_empty_window_chat(user_root, sid, obj)`
  - `add_transcript(ws_dir, sid, events)`
  - `add_debug_log(ws_dir, sid, events)`
  - `add_cli_session(copilot_home, sid, events)`
  - `add_session_store(user_root, rows: list[dict])` (WAL mode; table `sessions(id, cwd, repository, branch, summary, created_at, updated_at)`)
  - `write_settings(user_root, obj_or_text)`
  - `add_otel(path, spans)`

- [ ] **Step 1: Write the failing tests.**

```python
# test_copilot_source.py
def test_discover_finds_every_store(self):
    ur = cf.make_user_root(self.tmp); home = os.path.join(self.tmp, "home", ".copilot")
    ws = cf.add_workspace(ur, "h1", "/work/demo")
    cf.add_chat_jsonl(ws, "c1", [{"kind": 0, "v": {"sessionId": "c1", "requests": []}}])
    cf.add_transcript(ws, "t1", [{"type": "session.start", "data": {"sessionId": "t1"}}])
    cf.add_debug_log(ws, "c1", [{"type": "session_start"}])
    cf.add_cli_session(home, "k1", [{"type": "session.start", "data": {"sessionId": "k1"}}])
    cf.add_session_store(ur, [])
    out = os.path.join(self.tmp, "otel.jsonl"); cf.add_otel(out, [])
    cf.write_settings(ur, {"github.copilot.chat.otel.outfile": out})
    f = cs.discover([ur], home)
    self.assertEqual(set(f["chat"]), {"c1"}); self.assertEqual(f["chat"]["c1"]["workspace"], ws)
    self.assertEqual(set(f["transcripts"]), {"t1"}); self.assertEqual(set(f["debug"]), {"c1"})
    self.assertEqual(set(f["cli"]), {"k1"}); self.assertEqual(len(f["stores"]), 1)
    self.assertIn(os.path.normcase(out), [os.path.normcase(p) for p in f["otel"]])

def test_find_user_roots_includes_profiles(self): ...   # Code/User and Code/User/profiles/p1 both returned

# test_multi_source.py
def test_enabled_sources_copilot_only(self):       # Review Focus 4
    sc = afi.CopilotScanner([cf.make_user_root(self.tmp)], self.tmp)
    with patch.object(sc, "available", return_value=True):
        self.assertEqual(afi.enabled_sources(os.path.join(self.tmp, "missing"), sc), ["copilot"])

def test_payload_has_source_fields(self):
    d = afi.build_payload(afi.Scanner(FIX), parse_qs("days=0"))
    self.assertEqual((d["source"], d["cost_unit"]), ("claude", "usd"))

def test_copilot_payload_empty_when_no_logs(self):
    sc = afi.CopilotScanner([cf.make_user_root(self.tmp)], self.tmp)
    d = afi.build_payload(sc, parse_qs("days=0"), source="copilot")
    self.assertEqual((d["source"], d["cost_unit"], d["totals"]["prompts"]), ("copilot", "credits", 0))
```

- [ ] **Step 2: Run them and confirm they fail.**

Run: `python -m unittest test_copilot_source test_multi_source -v`
Expected: ERROR `No module named 'copilot_source'` / `copilot_fixtures`.

- [ ] **Step 3: Implement discovery in `copilot_source.py`, the fixture builders, `SOURCES`, `CopilotScanner` (shell), `enabled_sources`, the `source` handling in `build_payload` / `Handler`, and the `main()` flags.**
  - New flags:
    - `--copilot-root PATH` (repeatable)
    - `--no-copilot`
    - `--source {claude,copilot,all}` for `--dump` (default `claude`)
  - `main()` exits with the "Log directory not found" message only when `enabled_sources()` is empty.
  - Port the root/store walking from `tools/copilot_log_check.py` (`user_roots`, `ext_dirs`, `scan_user_root`, `scan_copilot_home`), dropping its report code.
  - The OTel outfile comes from `settings.json`: regex `"github.copilot.chat.otel.outfile"\s*:\s*"([^"]*)"`, so JSONC is fine.

- [ ] **Step 4: Run the tests.**

Run: `python -m unittest -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add copilot_source.py copilot_fixtures.py test_copilot_source.py test_multi_source.py ai_fluency_insights.py
git commit -m "Add source registry, Copilot log discovery and source= routing"
```

---

### Task 3: Tool switch, accent colours, tool-aware labels, Copilot empty state (UI)

**Files:**
- Modify: `ai_fluency_insights.py`, inside the `HTML` string:
  - CSS after the dark token block (~line 2932)
  - the top-bar markup in `.acts` (~line 3522)
  - the JS `load()` / `updateChrome()` and the strings that name Claude

**Interfaces:**
- Consumes: `D.source`, `D.sources`, `D.cost_unit` (Task 2).
- Produces (JS):
  - `let SOURCE`: `'claude'|'copilot'|'all'`, persisted in `localStorage['dtd-source']`
  - `TN()`: short name, `'Claude'|'Copilot'|'AI'`
  - `TF()`: full name, `'Claude Code'|'GitHub Copilot'|'All AI'`
  - `setSource(id)`
  - `<html data-source="...">`

- [ ] **Step 1: Add the CSS.**
  - Three blocks per non-Claude source, after the existing token blocks so they win:
    - `:root[data-source="copilot"]{...}` (light)
    - `@media (prefers-color-scheme:dark){:root:not([data-theme="light"])[data-source="copilot"]{...}}`
    - `:root[data-theme="dark"][data-source="copilot"]{...}`
  - Copilot values:
    - light: `--acc:#7355ea; --acc-2:#5f43d6; --acc-ink:#fff; --acc-soft:rgba(115,85,234,.10); --acc-line:rgba(115,85,234,.32); --canvas-tint:rgba(115,85,234,.05)`
    - dark: `--acc:#a78bfa; --acc-2:#8b6cf6; --acc-ink:#140f24; --acc-soft:rgba(167,139,250,.14); --acc-line:rgba(167,139,250,.38); --canvas-tint:rgba(167,139,250,.07)`
  - All AI values (selector `[data-source="all"]`):
    - light: `--acc:#0d8f89; --acc-2:#0a7570; --acc-ink:#fff; --acc-soft:rgba(13,143,137,.10); --acc-line:rgba(13,143,137,.32); --canvas-tint:rgba(13,143,137,.05)`
    - dark: `--acc:#2dd4bf; --acc-2:#14b8a6; --acc-ink:#04201d; --acc-soft:rgba(45,212,191,.14); --acc-line:rgba(45,212,191,.38); --canvas-tint:rgba(45,212,191,.07)`

- [ ] **Step 2: Add the switch.**
  - A `.seg` group `id="srcSeg"` with `button[data-src]`: "Claude Code", "GitHub Copilot", "All AI".
  - Place it inside `#actsData`, before `#range`, with the same styling and a sliding thumb (reuse `Indicator`).
  - Hide the whole group when fewer than two entries of `D.sources` are `available` (All AI needs both).
  - Hide individual unavailable buttons.

- [ ] **Step 3: Wire it.**
  - Read `SOURCE` from storage, falling back to `'claude'`.
  - `load()` appends `'&source='+SOURCE`.
  - `setSource` stores the choice, sets `data-source`, calls `load()` and `updateChrome()`, keeps the section and range, and re-runs `themeCharts()` so chart accents follow.
  - After each load, if `D.sources` says the stored source is unavailable, fall back to the first available one.
  - The Weekly Report's AI-summary and reflection fetches (`/api/ai_summary`, `/api/reflect`) also append `'&source='+SOURCE`.

- [ ] **Step 4: Make the labels tool-aware.**
  - Replace user-visible tool names in the JS strings and in `META` with `TN()` / `TF()`. Run `grep -n "Claude" ai_fluency_insights.py` within the HTML range; about 39 hits.
  - Examples: "sent to Claude", "generated by Claude", "written by Claude", "Claude tokens per typed token", "Claude (output)", the sidebar `.sub` "Claude Code analytics", and the `adoption` META text.
  - **Keep** strings about Claude-only features: plan usage limits, statusline, `claude -p`, `--setup-notifications`.
  - The root chip shows `D.root` as given; Task 8 sets it for Copilot.
  - Hide `#planCard` and the notification toasts unless `D.source==='claude'`.

- [ ] **Step 5: Add the Copilot empty state.**
  - When `D.source==='copilot'` and `D.sources` marks Copilot unavailable, show an `#alerts` panel: "No GitHub Copilot chat history found on this computer. Looked in: <D.root>. It appears here after you use Copilot Chat in VS Code or the Copilot CLI."
  - Charts render empty states as they already do.

- [ ] **Step 6: Verify by running the app.**

Run: `python ai_fluency_insights.py --no-browser --port 8790`. Open `http://localhost:8790` in the browser pane. Check:
- (a) The switch sits left of the 7d/30d/90d control.
- (b) Clicking GitHub Copilot turns accents violet, All AI teal (empty pages are fine until Tasks 8 and 11). Check both light and dark via the theme button.
- (c) A reload keeps the choice.
- (d) `document.body.innerText` on the Copilot view has no "Claude" except in Claude-only feature text.
- (e) `python -m unittest -v` still passes.

- [ ] **Step 7: Commit.**

```bash
git add ai_fluency_insights.py
git commit -m "Add Claude Code / GitHub Copilot / All AI switch with per-tool colours"
```

---

### Task 4: `parse_entry` hooks and Copilot tool vocabulary

**Files:**
- Modify: `copilot_source.py` (tool constants, `tool_loc`)
- Modify: `ai_fluency_insights.py`: tool sets (~lines 128–145) and `parse_entry` (cost line ~660; lines-of-code block ~770; MCP feature check ~700)
- Test: `test_copilot_source.py`, `test_multi_source.py`

**Interfaces:**
- Produces in `copilot_source.py`:
  - `COPILOT_READ_TOOLS = {"read_file", "grep_search", "file_search", "semantic_search", "list_dir", "fetch_webpage", "get_errors", "view", "grep", "glob", "web_fetch"}`
  - `COPILOT_WRITE_TOOLS = {"create_file", "replace_string_in_file", "multi_replace_string_in_file", "apply_patch", "insert_edit_into_file", "edit_notebook_file", "edit", "create", "str_replace_editor"}`
  - `COPILOT_CMD_TOOLS = {"run_in_terminal", "bash", "powershell", "shell", "write_bash"}`
  - `COPILOT_TOOL_FEATURES = {"run_in_terminal": "verify", "bash": "verify", "powershell": "verify", "fetch_webpage": "web", "web_fetch": "web", "runSubagent": "subagents", "task": "subagents", "manage_todo_list": "tasks", "update_todo": "tasks"}`
  - `tool_loc(name: str, args: dict) -> tuple[int, str]`: `(added_lines, file_path_or_"")`. Counts:
    - `create_file.content`, `create.file_text`
    - `replace_string_in_file.newString`, `edit.new_str`, `str_replace_editor.new_str`
    - the sum over `multi_replace_string_in_file.replacements[].newString`
    - `+` lines (not `+++`) of `apply_patch` (`args["input"]`, or the raw string when args is a string). Path from the first `*** Update File:` / `*** Add File:` line.
    - `insert_edit_into_file.code` lines, minus those containing `...existing code...`
    - `edit_notebook_file.newCode`
  - Paths come from `filePath` / `path`.
- Produces in the main file:
  - `READ_TOOLS |= COPILOT_READ_TOOLS`, likewise for write and cmd. `WRITE_TOOLS` keeps its definition, then adds Copilot's.
  - `TOOL_FEATURES.update(COPILOT_TOOL_FEATURES)`
  - `parse_entry` honours `entry["_cost_override"]`: that number replaces the computed cost, and that call adds 0 to `cache_savings`.
  - `parse_entry` honours `block["input"]["_loc"]`: that int replaces the computed lines of code.
  - The MCP feature also matches the `mcp_` prefix.

- [ ] **Step 1: Write the failing tests.**

```python
def test_tool_loc_counts_each_copilot_tool(self):
    self.assertEqual(cs.tool_loc("create_file", {"filePath": "a.py", "content": "a\nb\nc"}), (3, "a.py"))
    self.assertEqual(cs.tool_loc("replace_string_in_file", {"filePath": "b.py", "newString": "x\ny"}), (2, "b.py"))
    self.assertEqual(cs.tool_loc("multi_replace_string_in_file",
        {"replacements": [{"filePath": "c.py", "newString": "1"}, {"filePath": "c.py", "newString": "2\n3"}]}), (3, "c.py"))
    patch_txt = "*** Begin Patch\n*** Update File: d.py\n@@\n-old\n+new1\n+new2\n*** End Patch"
    self.assertEqual(cs.tool_loc("apply_patch", {"input": patch_txt}), (2, "d.py"))
    self.assertEqual(cs.tool_loc("insert_edit_into_file",
        {"filePath": "e.py", "code": "// ...existing code...\nfoo\n// ...existing code...\nbar"}), (2, "e.py"))
    self.assertEqual(cs.tool_loc("edit", {"path": "f.py", "new_str": "q"}), (1, "f.py"))
    self.assertEqual(cs.tool_loc("read_file", {"filePath": "g.py"}), (0, "g.py"))

# test_multi_source.py
def test_cost_override_and_loc_hooks(self):
    st = afi.Stats()
    afi.parse_entry({"type": "assistant", "timestamp": "2026-10-01T12:00:00.000+00:00", "sessionId": "s",
        "message": {"id": "a", "model": "gpt-5", "usage": {"input_tokens": 10, "output_tokens": 5},
                    "content": [{"type": "tool_use", "id": "t", "name": "create_file",
                                 "input": {"file_path": "x.py", "_loc": 7}}]},
        "_cost_override": 0.5}, "p", st)
    day = st.days["2026-10-01"]
    self.assertEqual((day["cost"], day["cache_savings"], day["loc"], day["writes"]), (0.5, 0.0, 7, 1))
```

- [ ] **Step 2: Run them and confirm they fail.**

Run: `python -m unittest test_copilot_source test_multi_source -v`
Expected: FAIL (`tool_loc` missing; cost computed from prices, not 0.5).

- [ ] **Step 3: Implement the constants, `tool_loc`, the set unions, and the two `parse_entry` hooks.**
  - Keep them as small `if` branches.
  - Claude logs never carry `_cost_override` or `_loc`, so the Claude path is untouched.

- [ ] **Step 4: Run all tests, including Task 1's golden tests.**

Run: `python -m unittest -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add copilot_source.py ai_fluency_insights.py test_copilot_source.py test_multi_source.py
git commit -m "Teach parse_entry Copilot tool names, credit costs and patch line counts"
```

---

### Task 5: VS Code chat-session translator

**Files:** Modify `copilot_source.py`, `copilot_fixtures.py`, `test_copilot_source.py`

**Interfaces:**
- Produces:
  - `iso_utc(value) -> str | None`
    - Accepts epoch ms (int/float) or an ISO string (`Z` or offset).
    - Returns UTC `isoformat(timespec="milliseconds")`, e.g. `2026-10-01T12:00:00.000+00:00`.
  - `replay_chat_jsonl(lines: Iterable[str]) -> tuple[dict | None, int]`. Returns `(session, bad_lines)`. Operations:
    - `kind 0`: snapshot `v`
    - `kind 1`: set `v` at path `k`, creating dicts and padding lists as needed
    - `kind 2`: at the list at `k`, truncate to `i` if given, then extend with `v` (or append when `v` isn't a list)
    - `kind 3`: delete at `k`
    - unparseable lines: counted in `bad_lines`
  - `load_chat_session(path: str) -> tuple[dict | None, int]`. `.json` → `json.load`; `.jsonl` → replay.
  - `normalize_model(raw: str | None) -> str | None`. Strips a `copilot/` prefix and lowercases; `"auto"` → `None`.
  - `parse_credits(details: str | None) -> tuple[str, float] | None`
    - `"GPT-5.6 Luna • 0.5 credits"` → `("credits", 0.5)`
    - `"Claude Opus 4.5 • 3x"` → `("premium", 3.0)`
    - `"GPT-4.1 • 0x"` → `("premium", 0.0)`
    - anything else → `None`
  - `chat_session_to_events(sess: dict, sid: str, project: str, cwd: str, branch: str, with_usage: bool = True) -> list[dict]`
  - `chat_session_meta(sess: dict) -> dict`: `{"mode": inputState.mode.kind or None, "title": customTitle or None, "creation": iso_utc(creationDate)}`
- Translation rules for each request in `sess["requests"]`; skip a request with no `timestamp`:
  1. User event at `iso_utc(request.timestamp)`. Text = `message.text`, or `message` when it's a string.
  2. For each `result.metadata.toolCallRounds[r]`:
     - an assistant event at `iso_utc(round.timestamp)` (fallback: the request time), id `f"{sid}:{requestId}:r{r}"`, model = `normalize_model(round.modelId)`, `usage {}`.
     - one `tool_use` per `toolCalls[j]`: `id` = the call id, `name`, `input` = `json.loads(arguments)` (or `{}`) plus `"_loc"` and `"file_path"` from `tool_loc`.
     - one tool_result per call, timestamped at the next round's timestamp (or the request end).
     - `is_error` = the call's `result.metadata.toolCallResults[id]` text starts with `"ERROR"` or contains `"Applying patch failed"`.
  3. Final assistant event at `end = request ts + (elapsedMs or result.timings.totalElapsed or 0)` ms, id `f"{sid}:{requestId}:final"`, `stop_reason "end_turn"`.
     - Model: `normalize_model(request.modelId)`, else the last round's model, else `result.metadata.resolvedModel`, else `"unknown"`.
     - When `with_usage`: input = `request.promptTokens`, else `result.metadata.promptTokens`, else `result.usage.promptTokens`. Output = `request.completionTokens`, else `result.metadata.outputTokens`, else `result.usage.completionTokens`. Cache fields 0.
     - `_cost_override` = `request.copilotCredits` if numeric, else `parse_credits(result.details)` when its unit is `credits`.
     - `_premium` = the multiplier when `parse_credits` returns `premium`.

- [ ] **Step 1: Add the fixture builder `chat_request(rid, ts_ms, text, model="copilot/gpt-5.6-luna", rounds=(), prompt=0, completion=0, credits=None, details=None, elapsed=0)`** in `copilot_fixtures.py`. It returns one request dict in the shape above.

- [ ] **Step 2: Write the failing tests.**

```python
def test_replay_snapshot_set_push_splice(self):
    lines = [json.dumps(x) for x in (
        {"kind": 0, "v": {"sessionId": "s", "requests": []}},
        {"kind": 2, "k": ["requests"], "v": [{"requestId": "a"}, {"requestId": "b"}]},
        {"kind": 2, "k": ["requests"], "i": 1, "v": [{"requestId": "c"}]},
        {"kind": 1, "k": ["requests", 1, "promptTokens"], "v": 42})]
    sess, bad = cs.replay_chat_jsonl(lines)
    self.assertEqual([r["requestId"] for r in sess["requests"]], ["a", "c"])
    self.assertEqual((sess["requests"][1]["promptTokens"], bad), (42, 0))

def test_replay_skips_truncated_last_line(self):          # Review Focus 1
    lines = [json.dumps({"kind": 0, "v": {"requests": []}}), '{"kind": 2, "k": ["requ']
    sess, bad = cs.replay_chat_jsonl(lines)
    self.assertEqual((sess["requests"], bad), ([], 1))

def test_iso_utc_formats(self):                            # Review Focus 3
    self.assertEqual(cs.iso_utc(1759320000000), "2025-10-01T12:00:00.000+00:00")
    self.assertEqual(cs.iso_utc("2025-10-01T12:00:00Z"), "2025-10-01T12:00:00.000+00:00")
    self.assertIsNone(cs.iso_utc("garbage"))

def test_parse_credits(self):
    self.assertEqual(cs.parse_credits("GPT-5.6 Luna • 0.5 credits"), ("credits", 0.5))
    self.assertEqual(cs.parse_credits("Claude Opus 4.5 • 3x"), ("premium", 3.0))
    self.assertIsNone(cs.parse_credits("GPT-4.1"))

def test_agent_request_becomes_claude_shaped_events(self):
    req = cf.chat_request("r1", 1759320000000, "fix it", prompt=1200, completion=300, credits=0.5, elapsed=4000,
        rounds=[{"timestamp": 1759320001000, "modelId": "gpt-5.6-luna",
                 "toolCalls": [{"id": "c1", "name": "read_file", "arguments": "{\"filePath\": \"a.py\"}"}]},
                {"timestamp": 1759320002000, "modelId": "gpt-5.6-luna",
                 "toolCalls": [{"id": "c2", "name": "apply_patch",
                                "arguments": json.dumps({"input": "*** Begin Patch\n*** Update File: a.py\n+x\n+y\n*** End Patch"})}]}])
    ev = cs.chat_session_to_events({"requests": [req]}, "s1", "work/demo", "/work/demo", "(none)")
    self.assertEqual([e["type"] for e in ev], ["user", "assistant", "user", "assistant", "user", "assistant"])
    final = ev[-1]
    self.assertEqual(final["message"]["stop_reason"], "end_turn")
    self.assertEqual(final["timestamp"], "2025-10-01T12:00:04.000+00:00")
    self.assertEqual(final["message"]["usage"]["input_tokens"], 1200)
    self.assertEqual(final["_cost_override"], 0.5)
    st = afi.Stats()
    for e in ev:
        afi.parse_entry(e, "work/demo", st)
    day = next(iter(st.days.values()))
    self.assertEqual((day["prompts"], day["reads"], day["writes"], day["loc"], day["output"]), (1, 1, 1, 2, 300))
    self.assertEqual(st.turns[0]["secs"], 4.0)
```

This file imports `ai_fluency_insights as afi` for the round-trip check only.

- [ ] **Step 3: Run them and confirm they fail.**

Run: `python -m unittest test_copilot_source -v`
Expected: FAIL (`replay_chat_jsonl` etc. missing).

- [ ] **Step 4: Implement the functions above in `copilot_source.py`.**

- [ ] **Step 5: Run all tests.**

Run: `python -m unittest -v`
Expected: all PASS.

- [ ] **Step 6: Commit.**

```bash
git add copilot_source.py copilot_fixtures.py test_copilot_source.py
git commit -m "Translate VS Code Copilot chat sessions into dashboard events"
```

---

### Task 6: Agent-log translator (VS Code transcripts and Copilot CLI)

**Files:** Modify `copilot_source.py`, `test_copilot_source.py`

**Interfaces:**
- Produces `agent_log_to_events(lines: Iterable[str], sid: str, project: str, cwd: str, branch: str, with_usage: bool = True) -> tuple[list[dict], dict]`. The meta dict:
  ```python
  {"host_type": context.hostType or None, "cwd": context.cwd or context.gitRoot or None,
   "branch": context.branch or None, "repository": ..., "copilot_version": ..., "vscode_version": ...,
   "has_usage": bool, "bad_lines": int, "unknown_types": Counter}
  ```
- Rules, all record types `{type, data, id, timestamp}`:
  - `session.start` → meta only.
  - `user.message` → a user event with `data.content`.
  - `assistant.message` → an assistant event with `usage {}`, plus one `tool_use` per `data.toolRequests[]`:
    - `arguments` is a JSON string → dict, or a raw string kept as `{"input": s}` for `apply_patch`
    - plus `_loc` / `file_path` from `tool_loc`
  - `tool.execution_complete` → a tool_result; `is_error = data.success is False`.
  - `session.usage_record` (when `with_usage`) → an assistant event with:
    - id `data.usage.apiCallId` or the record id
    - model `data.usage.model`
    - `input = max(0, inputTokens − cacheReadTokens − cacheWriteTokens)`
    - `cache_read_input_tokens = cacheReadTokens`, `cache_creation_input_tokens = cacheWriteTokens`, `output = outputTokens`
    - `_cost_override = copilotUsage.totalNanoAiu / 1e9` when present
  - With **no** usage records in the file (and `with_usage`): take only the **last** `session.usage_checkpoint` / `session.shutdown`. Emit one assistant event per model in `data.accountingSnapshot.modelMetrics` (or `data.modelMetrics`), with usage from `usage.{inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens}`, normalised the same way, and `_cost_override = totalNanoAiu / 1e9`.
  - `assistant.turn_end` → an assistant event, id `f"{sid}:end:{n}"`, `stop_reason "end_turn"`, `usage {}`, only for the **last** `turn_end` before the next `user.message` or the end of the file.
  - Any record whose `data.agentId` is set → its events get `"isSidechain": True`.
  - `hook.*`, `system.message`, `session.*` (other), `external_tool.*`, `assistant.turn_start`, `tool.execution_start` → ignored, not counted as unknown.
  - Any other `type` → `meta["unknown_types"][type] += 1`.

- [ ] **Step 1: Write the failing tests.**

```python
CLI = [
 {"type": "session.start", "timestamp": "2026-10-24T10:00:00Z",
  "data": {"sessionId": "k1", "copilotVersion": "1.0.100-1",
           "context": {"cwd": "/work/demo", "branch": "main", "hostType": "cli"}}},
 {"type": "user.message", "timestamp": "2026-10-24T10:00:01Z", "data": {"content": "run tests"}},
 {"type": "assistant.message", "timestamp": "2026-10-24T10:00:03Z",
  "data": {"toolRequests": [{"toolCallId": "x1", "name": "bash", "arguments": "{\"command\": \"pytest -q\"}"}]}},
 {"type": "session.usage_record", "timestamp": "2026-10-24T10:00:03Z",
  "data": {"usage": {"apiCallId": "u1", "model": "mai-code-1.1-flash", "inputTokens": 15111,
           "cacheReadTokens": 14976, "cacheWriteTokens": 0, "outputTokens": 20,
           "copilotUsage": {"totalNanoAiu": 211036000}}}},
 {"type": "assistant.turn_end", "timestamp": "2026-10-24T10:00:04Z", "data": {"turnId": "0"}},
 {"type": "tool.execution_complete", "timestamp": "2026-10-24T10:00:06Z", "data": {"toolCallId": "x1", "success": True}},
 {"type": "assistant.turn_end", "timestamp": "2026-10-24T10:00:08Z", "data": {"turnId": "1"}},
 {"type": "brand.new_event", "timestamp": "2026-10-24T10:00:09Z", "data": {}}]

def test_cli_usage_record_normalised(self):
    ev, meta = cs.agent_log_to_events([json.dumps(x) for x in CLI], "k1", "work/demo", "/work/demo", "main")
    use = [e for e in ev if e["type"] == "assistant" and e["message"]["usage"]]
    self.assertEqual(len(use), 1)
    u = use[0]["message"]["usage"]
    self.assertEqual((u["input_tokens"], u["cache_read_input_tokens"], u["output_tokens"]), (135, 14976, 20))
    self.assertAlmostEqual(use[0]["_cost_override"], 0.211036)
    self.assertEqual(sum(1 for e in ev if e["message"].get("stop_reason") == "end_turn"), 1)
    self.assertEqual((meta["host_type"], meta["branch"], meta["has_usage"]), ("cli", "main", True))

def test_unknown_event_types_counted(self):                # Review Focus 5
    _ev, meta = cs.agent_log_to_events([json.dumps(x) for x in CLI], "k1", "p", "/p", "main")
    self.assertEqual(meta["unknown_types"], {"brand.new_event": 1})

def test_checkpoint_fallback_uses_last_snapshot_only(self): ...
    # two session.usage_checkpoint records (inputTokens 100 then 300 for one model), no usage_record
    # -> exactly one usage event, input_tokens == 300 - its cache reads

def test_transcript_has_prompts_and_tools_but_no_tokens(self): ...
    # session.start (vscodeVersion 1.139.0) + user.message + assistant.message(read_file) + tool.execution_complete
    # -> 1 user prompt event, 1 tool_use named read_file, no event with non-empty usage

def test_subagent_records_are_sidechain(self): ...
    # assistant.message with data.agentId -> event["isSidechain"] is True
```

The three `...` tests get their bodies from the comment beneath each one.

- [ ] **Step 2: Run them and confirm they fail.**

Run: `python -m unittest test_copilot_source -v`
Expected: FAIL (`agent_log_to_events` missing).

- [ ] **Step 3: Implement `agent_log_to_events`.** Reuse `iso_utc` and `tool_loc`. A bad JSON line is counted in `bad_lines` and skipped.

- [ ] **Step 4: Run all tests.**

Run: `python -m unittest -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add copilot_source.py test_copilot_source.py
git commit -m "Translate Copilot CLI sessions and VS Code transcripts into dashboard events"
```

---

### Task 7: Exact token sources (OpenTelemetry file and debug logs)

**Files:** Modify `copilot_source.py`, `copilot_fixtures.py` (`add_otel` span shapes), `test_copilot_source.py`

**Interfaces:**
- Produces a call dict: `{"id": str, "ts": ISO, "model": str|None, "input": int, "output": int, "cache_read": int, "cache_write": int, "credits": float|None}`
- `read_debug_calls(paths: list[str]) -> list[dict]`
  - Uses `type == "llm_request"` lines.
  - `input = max(0, attrs.inputTokens − attrs.cachedTokens)`, `cache_read = cachedTokens`, `cache_write = 0`.
  - `credits = attrs.copilotUsageNanoAiu / 1e9`.
  - `ts = iso_utc(ts)`, `id = spanId`.
- `read_otel_calls(paths: list[str]) -> dict[str, list[dict]]`, keyed by `gen_ai.conversation.id`.
  - Keeps spans whose `name` starts with `"chat"`, or whose attribute `gen_ai.operation.name == "chat"`.
  - `attributes` may be a dict, or a list of `{"key", "value": {"intValue"|"doubleValue"|"stringValue": ...}}`.
  - `input = input_tokens − cache_read.input_tokens − cache_creation.input_tokens`, floored at 0.
  - `credits` from `github.copilot.nano_aiu / 1e9`.
  - `ts` from `startTimeUnixNano`, `startTime` (`[sec, nanos]` or ISO), or `timestamp`.
  - `id = spanId`.
- `exact_calls_for(sid: str, debug: dict, otel_index: dict) -> tuple[str, list[dict]]`. Returns `("otel", calls)` if `otel_index` has calls for `sid`, else `("debug", calls)` if the debug files have any `llm_request`, else `("none", [])`.
- `usage_events(sid, calls, project, cwd, branch) -> list[dict]`: one assistant event per call, id `f"{sid}:x:{call['id']}"`, `_cost_override` when credits is not None.

- [ ] **Step 1: Write the failing tests.**

```python
def test_debug_log_without_llm_requests_is_none(self):
    ws = cf.add_workspace(cf.make_user_root(self.tmp), "h", "/w")
    p = cf.add_debug_log(ws, "s1", [{"type": "session_start", "attrs": {"copilotVersion": "0.67.0"}}])
    self.assertEqual(cs.exact_calls_for("s1", {"s1": [p]}, {}), ("none", []))

def test_debug_llm_request_normalised(self):
    ws = cf.add_workspace(cf.make_user_root(self.tmp), "h", "/w")
    p = cf.add_debug_log(ws, "s1", [{"type": "llm_request", "spanId": "a", "ts": 1759320000000,
        "attrs": {"inputTokens": 5000, "outputTokens": 400, "cachedTokens": 3000,
                  "model": "gpt-4.1", "copilotUsageNanoAiu": 120000000}}])
    mode, calls = cs.exact_calls_for("s1", {"s1": [p]}, {})
    self.assertEqual((mode, calls[0]["input"], calls[0]["cache_read"], calls[0]["credits"]), ("debug", 2000, 3000, 0.12))

def test_otel_list_and_dict_attributes(self):
    path = os.path.join(self.tmp, "o.jsonl")
    cf.add_otel(path, [
        {"name": "chat gpt-4.1", "spanId": "1", "startTimeUnixNano": 1759320000000000000,
         "attributes": [{"key": "gen_ai.conversation.id", "value": {"stringValue": "s1"}},
                        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": 900}},
                        {"key": "gen_ai.usage.cache_read.input_tokens", "value": {"intValue": 400}},
                        {"key": "gen_ai.usage.output_tokens", "value": {"intValue": 50}}]},
        {"name": "chat gpt-4.1", "spanId": "2", "startTime": [1759320060, 0],
         "attributes": {"gen_ai.conversation.id": "s1", "gen_ai.usage.input_tokens": 100,
                        "gen_ai.usage.output_tokens": 10}},
        {"name": "execute_tool read_file", "spanId": "3", "attributes": {"gen_ai.conversation.id": "s1"}}])
    idx = cs.read_otel_calls([path])
    self.assertEqual([(c["input"], c["cache_read"], c["output"]) for c in idx["s1"]], [(500, 400, 50), (100, 0, 10)])

def test_otel_wins_over_debug(self):
    # same sid in both -> exact_calls_for returns ("otel", ...)
    ...
```

`test_otel_wins_over_debug` reuses the setup of the two tests above it.

- [ ] **Step 2: Run them and confirm they fail.**

Run: `python -m unittest test_copilot_source -v`
Expected: FAIL.

- [ ] **Step 3: Implement the four functions.**

- [ ] **Step 4: Run all tests.**

Run: `python -m unittest -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add copilot_source.py copilot_fixtures.py test_copilot_source.py
git commit -m "Read exact Copilot token counts from OpenTelemetry and debug logs"
```

---

### Task 8: `CopilotScanner.build_stats`: one session, one source, nothing counted twice

**Files:**
- Modify: `copilot_source.py` (`workspace_folder`, `project_label`, `read_session_store`), `ai_fluency_insights.py` (`CopilotScanner`, the Copilot extras in `build_payload`)
- Test: `test_copilot_source.py`, `test_multi_source.py`

**Interfaces:**
- Produces in `copilot_source.py`:
  - `workspace_folder(ws_dir: str) -> str | None`
    - Decodes `workspace.json` `folder` (`file:///c%3A/x` → `c:/x` on Windows, `/x` elsewhere).
    - For `workspace` (a `.code-workspace` URI), returns the folder that contains that file.
  - `project_label(path: str | None) -> str`
    - Claude-style: `re.sub(r"[^A-Za-z0-9]", "-", path)`, then the same prettify as `Scanner.build_stats`: `.lstrip("-").replace("--", "/").replace("-", "/")`.
    - `None` → `"(no folder)"`.
    - The same folder must get the same label in both tools.
  - `read_session_store(db_paths: list[str]) -> dict[str, dict]`
    - `id → {"cwd", "repository", "branch", "summary"}`.
    - Read-only (temp copy when a `-wal` exists); missing table or unreadable file → `{}`.
- Produces in the main file:
  - `CopilotScanner.build_stats(min_day=None, max_day=None) -> Stats` sets these extra attributes on the returned `Stats` (instance attributes, **not** in `Stats.__init__`, so Claude payloads don't change):
    - `st.premium_days: Counter` (day → premium requests)
    - `st.surfaces: {"vscode": Counter, "cli": Counter}` (keys `prompts`, `output`, `sessions`)
    - `st.token_modes: Counter` (`"exact"` / `"approx"` / `"none"` → sessions)
    - `st.no_cache_split: int`
    - `st.copilot_versions: Counter`
    - `st.unknown_types: Counter`
    - `st.bad_lines: int`
  - `copilot_extras(st: Stats) -> dict` → `{"premium_requests": float, "surfaces": {...}, "token_modes": {...}}`. `build_payload(source="copilot")` merges it into `data["copilot"]`. `data["root"]` = the user roots and copilot home joined with `" · "`.
- Session assembly, per `sid` in `chat ∪ transcripts ∪ cli`:
  1. **Primary store:** `cli` if present, else `chat`, else `transcripts`. A CLI log is the richest record of a session started from VS Code's Copilot CLI agent host, so it wins outright and the chat or transcript file for the same `sid` is not translated.
  2. **Tokens:**
     - Primary `cli` → its own usage records (`token_modes["exact"]`).
     - Otherwise, `exact_calls_for` = otel/debug → translate the primary with `with_usage=False`, plus `usage_events(...)` (`exact`).
     - Otherwise, chat per-request numbers (`approx`, and `no_cache_split += 1`).
     - A transcript-only session with no exact calls → `none`.
  3. **Project:**
     - chat/transcript → `workspace_folder(workspace)`
     - empty-window chats → `None`
     - CLI → `meta["cwd"]`
     - fallback: the session store's `cwd`
     - `label = project_label(folder)`, and set `st.project_cwd[label] = folder` when it exists.
  4. **Branch:** CLI `meta["branch"]`, else the session store's `branch`, else `"(none)"`.
  5. **Title:** `chat_session_meta.title`, else the session store's `summary`, else the first prompt trimmed to 60 characters → `st.session_titles[sid]`.
  6. **Surface:** `"cli"` if primary is `cli` and `meta["host_type"]` isn't `"vscode"`, else `"vscode"`.
  7. **Feed and record:**
     - Feed every event through `parse_entry(e, label, st, min_day, max_day)` in a try/except that counts `st.parse_errors`.
     - Add `_premium` values to `st.premium_days[day]`.
     - Add the chat mode (`agent`/`ask`/`edit`/`plan`) to `st.feature_days[mode]`, and a prompt starting with `/word` to `st.slash` + `st.feature_days["slash"]`, for days in range.
  8. **Cache:** translated events are cached per `sid`, keyed by the `(mtime, size)` tuple of every file used. `build_stats` re-runs only `parse_entry` for unchanged sessions.
     - Call the readers through the module (`import copilot_source as cs`, then `cs.load_chat_session(...)`) so tests can patch them.

- [ ] **Step 1: Write the failing tests.**

```python
# test_copilot_source.py
def test_project_label_matches_claude_style(self):
    self.assertEqual(cs.project_label("E:\\god_ai\\AI_ALL"), "E/god/ai/AI/ALL")
    self.assertEqual(cs.project_label(None), "(no folder)")

def test_workspace_folder_decodes_uri(self): ...
    # {"folder": "file:///c%3A/work/demo"} -> "c:/work/demo" (os.name == "nt") or "/c:/work/demo" stripped to "c:/work/demo"

def test_read_session_store_wal(self):
    ur = cf.make_user_root(self.tmp)
    cf.add_session_store(ur, [{"id": "s1", "cwd": "/w", "branch": "dev", "summary": "Fix login"}])
    self.assertEqual(cs.read_session_store(cs.discover([ur], self.tmp)["stores"])["s1"]["branch"], "dev")

# test_multi_source.py: one tree with four sessions
#  c1: chat jsonl, 1 request (prompt 1200, completion 300, credits 0.5) + a debug log with llm_request (input 5000 cached 3000 output 400)
#  t1: transcript only, 1 user message
#  k1: CLI session (the CLI fixture from Task 6) AND a chat jsonl with sid k1 (prompt 99999)
#  e1: empty-window chat, 1 request "• 3x" premium, prompt 10, completion 5
def test_one_session_one_token_source(self):
    st = self.scanner.build_stats()
    self.assertEqual(len(st.sessions), 4)
    self.assertEqual(sum(d["output"] for d in st.days.values()), 400 + 20 + 5)   # debug, CLI, e1; never 300 or the 99999 chat
    self.assertEqual(dict(st.token_modes), {"exact": 2, "approx": 1, "none": 1})
    self.assertEqual(sum(st.premium_days.values()), 3.0)
    self.assertIn("(no folder)", st.projects)

def test_unchanged_files_not_reparsed(self):              # Review Focus 2
    self.scanner.build_stats()
    with patch("copilot_source.load_chat_session", side_effect=AssertionError("re-read")):
        self.scanner.build_stats()

def test_copilot_payload_extras(self):
    d = afi.build_payload(self.scanner, parse_qs("days=0"), source="copilot")
    self.assertEqual(d["copilot"]["premium_requests"], 3.0)
    self.assertEqual(d["copilot"]["surfaces"]["cli"]["sessions"], 1)
    self.assertEqual(d["cost_unit"], "credits")
```

The `...` test gets its body from the comment beneath it.

- [ ] **Step 2: Run them and confirm they fail.**

Run: `python -m unittest test_copilot_source test_multi_source -v`
Expected: FAIL.

- [ ] **Step 3: Implement `workspace_folder`, `project_label`, `read_session_store`, `CopilotScanner.build_stats`, and `copilot_extras` per the rules above.**

- [ ] **Step 4: Run all tests.**

Run: `python -m unittest -v`
Expected: all PASS.

- [ ] **Step 5: Smoke-test on the real home-laptop data.**

Run: `python ai_fluency_insights.py --dump --source copilot`
Expected:
- JSON with `totals.prompts > 0`, `models` naming `gpt-5.6-luna` / `mai-code-1.1-flash`
- `copilot.token_modes` with both `approx` and `exact`
- no traceback

- [ ] **Step 6: Commit.**

```bash
git add copilot_source.py ai_fluency_insights.py test_copilot_source.py test_multi_source.py
git commit -m "Build Copilot stats with one token source per session"
```

---

### Task 9: Copilot Data Health, not-available map, My Adoption features, Advisor wording

**Files:** Modify `ai_fluency_insights.py` (`build_health_copilot`, `copilot_na`, `COPILOT_ADOPTION_FEATURES`, `build_adoption` signature, `build_advisor` signature, `retool_text`); `test_multi_source.py`

**Interfaces:**
- `build_health_copilot(st: Stats, files: dict) -> dict`. Same shape as `build_health`.
  - Capability ids:

    | id | Label | Status | Detail |
    |---|---|---|---|
    | `chat` | Chat history | ok if any session | sessions per store |
    | `tokens` | Token counts | ok / partial / missing by sessions with any usage | |
    | `exact` | Exact tokens | ok if `token_modes["exact"]` covers ≥90% of sessions, partial if any, missing otherwise | "Turn on with: python ai_fluency_insights.py --setup-copilot" when not ok |
    | `credits` | Credits | | |
    | `cli` | Copilot CLI | | |
    | `projects` | Project folders | | |
    | `branches` | Git branches | | |
    | `titles` | Session titles | | |
    | `time` | Timestamps | | |

  - Also returns `"versions"` from `st.copilot_versions`, `"unknown_types"` (list of `{type, n}`), `"bad_lines"`, `"parse_errors"`.
  - `"limits"`: Copilot-specific sentences:
    - inline completions not logged
    - agent turns may record only the last call without exact mode
    - credits as reported by the logs
    - a session is not a task
- `copilot_na(health: dict) -> dict[str, str]`. KPI key → reason, always containing:
  - `cold_start_pct`: "Copilot bills per request, so there is no cache set-up cost to measure."
  - `interruptions`: "Copilot's logs don't record cancelled requests yet."
  - `permission_modes`: "Copilot doesn't log approval modes."

  Plus `cache_read` → "Copilot's chat history has no cache split; turn on exact tokens (--setup-copilot)." when `exact` is missing.

  `build_payload(source="copilot")` sets `data["na"] = copilot_na(...)` and `data["health"] = build_health_copilot(...)`. Claude sets `data["na"] = {}`.
- `COPILOT_ADOPTION_FEATURES`, same tuple shape as `ADOPTION_FEATURES`: `(id, label, short description, how-to-start tip, weight)`.

  | id | label | weight |
  |---|---|---|
  | `memory` | Custom instructions (`.github/copilot-instructions.md` or `AGENTS.md`) | 58 |
  | `verify` | Run & verify | 60 |
  | `agent` | Agent mode | 55 |
  | `plan` | Plan mode | 50 |
  | `subagents` | Subagents | 45 |
  | `tasks` | Todo lists | 38 |
  | `mcp` | Connected tools (MCP) | 42 |
  | `slash` | Prompt files & slash commands | 40 |
  | `web` | Web fetch | 35 |
- `build_adoption(st, features=ADOPTION_FEATURES, instructions=("CLAUDE.md", "AGENTS.md"))`. The default arguments keep the Claude output identical.
  - Thread `features` through every place that reads `ADOPTION_FEATURES`: `_feature_status`, `_coach`, the feature-row and `ever` loops in `build_adoption`, and the `total` count.
  - Thread `instructions` through the `memory` file check.
- `stats_to_json(st, days_filter=None, instructions=("CLAUDE.md", "AGENTS.md"))`. `has_instructions` checks those files, where each entry is a path relative to the project folder. `build_payload` passes `SOURCES[source]["instructions"]`.
- `build_advisor(st, adoption, tool=SOURCES["claude"])` drops recs whose id is in `tool["skip_recs"]`.
- `retool_text(obj, tool: dict)` returns a deep copy where, in every string:
  - `"CLAUDE.md"` → `tool["instructions"][0]`
  - `"Claude Code"` → `tool["label"]`
  - `"Claude"` → `tool["short"]`

  Applied to the Copilot advisor output only.

- [ ] **Step 1: Write the failing tests.**

```python
def test_copilot_health_and_na(self):
    d = afi.build_payload(self.scanner, parse_qs("days=0"), source="copilot")
    caps = {c["id"]: c["status"] for c in d["health"]["capabilities"]}
    self.assertEqual((caps["chat"], caps["exact"], caps["cli"]), ("ok", "partial", "ok"))
    self.assertIn("cold_start_pct", d["na"]); self.assertEqual(afi.build_payload(afi.Scanner(FIX), parse_qs("days=0"))["na"], {})

def test_copilot_adoption_uses_copilot_features(self):
    st = self.scanner.build_stats()
    ad = afi.build_adoption(st, features=afi.COPILOT_ADOPTION_FEATURES,
                            instructions=afi.SOURCES["copilot"]["instructions"])
    if ad["ready"]:                         # fixture may have too few active days for a full window
        ids = {f["id"] for f in ad["range"]["features"]}
        self.assertEqual(ids, {"memory", "verify", "agent", "plan", "subagents", "tasks", "mcp", "slash", "web"})

def test_copilot_project_instructions_files(self):
    # a project folder holding .github/copilot-instructions.md is has_instructions=True on Copilot
    ...

def test_advisor_text_names_copilot(self):
    d = afi.build_payload(self.scanner, parse_qs("days=0"), source="copilot")
    self.assertNotIn("Claude", json.dumps(d["advisor"]))

def test_unknown_types_reach_health(self):                 # Review Focus 5
    d = afi.build_payload(self.scanner, parse_qs("days=0"), source="copilot")
    self.assertEqual(d["health"]["unknown_types"], [{"type": "brand.new_event", "n": 1}])
```

The adoption feature rows live at `adoption["range"]["features"]`, each with an `"id"`. `build_adoption` only fills them once `ready`.

For `test_copilot_project_instructions_files`: create a temp folder with `.github/copilot-instructions.md`, point a fixture chat's workspace at it, and assert that the payload's `projects` row for it has `has_instructions` True.

- [ ] **Step 2: Run them and confirm they fail.**

Run: `python -m unittest test_multi_source -v`
Expected: FAIL.

- [ ] **Step 3: Implement.** Use the same Task 8 fixture tree (move its setup into a shared `setUpClass` helper).

- [ ] **Step 4: Run all tests, including the Task 1 golden tests.** They prove the Claude adoption and advisor output didn't change.

Run: `python -m unittest -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add ai_fluency_insights.py test_multi_source.py
git commit -m "Add Copilot data health, adoption features and advisor wording"
```

---

### Task 10: Copilot-specific UI

**Files:** Modify `ai_fluency_insights.py` (HTML/JS: `renderOverview`, `renderTools`, `renderProjects`, `renderValue`, `renderHealth`, the KPI and model-table cost columns)

**Interfaces:**
- Consumes: `D.na`, `D.cost_unit`, `D.copilot.{premium_requests, surfaces, token_modes}`, `D.health` (Copilot shape), `SOURCES` short names via `TN()`.
- Produces (JS):
  - `fmtCost(v)`: `'$'+v` for `usd`; `fmt(v)+' cr'` for `credits`
  - `naCard(label, reason)`: same footprint as a KPI card, eyebrow = label, body "Not available for Copilot", `.s` = reason

- [ ] **Step 1: Overview.**
  - Any KPI whose key is in `D.na` renders as `naCard`.
  - The cost KPI label is "Credits used" with sub "AI credits, as logged" when `D.cost_unit==='credits'`.
  - Add a "Premium requests" KPI only when `D.copilot && D.copilot.premium_requests>0`.
  - Add a card "VS Code vs CLI": a doughnut of `D.copilot.surfaces[*].prompts` in the Copilot accent and `--slate`, shown only on Copilot.
  - Cache-dependent subtexts show "approx." when `D.copilot.token_modes.approx` is above 0.
- [ ] **Step 2: Tools & Models, Projects, Value.**
  - The model-table and project-table cost columns use `fmtCost`. The column header reads "Credits" on Copilot.
  - The project instructions column shows "✓ instructions" with a tooltip naming the file (`.github/copilot-instructions.md` / `AGENTS.md`) on Copilot.
  - The autonomy chart (permission modes) shows `naCard` text when `D.na.permission_modes`.
  - Value-page money figures use `fmtCost`.
- [ ] **Step 3: Data Health.** It renders `D.health.capabilities` as today, plus Copilot's `versions`, `unknown_types` (as "Unrecognised log records: N, new Copilot version?") and the `--setup-copilot` hint text.
- [ ] **Step 4: Verify by running the app on the home laptop's real Copilot data.**

Run: `python ai_fluency_insights.py --no-browser --port 8790`. In the browser pane, switch to GitHub Copilot and check:
- Overview, Time & Focus, Tools & Models, Projects, Weekly Report, My Adoption, Flow, Quality, Value, Advisor and Data Health all render without console errors (`read_console_messages`).
- Cold-start tax shows "Not available for Copilot".
- Credits show "cr".
- Models list `gpt-5.6-luna`.
- Switch back to Claude Code: the page looks exactly as before (compare with `docs/screenshots/overview.png` layout).
- Take screenshots of both.

- [ ] **Step 5: Commit.**

```bash
git add ai_fluency_insights.py
git commit -m "Show Copilot credits, not-available cards and VS Code vs CLI split"
```

---

### Task 11: All AI payload

**Files:** Modify `ai_fluency_insights.py` (`union_slots`, `build_all_ai`, the `source=all` branch in `Handler`/`build_payload`); `test_multi_source.py`

**Interfaces:**
- `union_slots(stats: list[Stats], d0: str | None, d1: str | None) -> dict[str, set[int]]`: day → union of `day_buckets` (slot = `hour*6 + minute//10`).
- `build_all_ai(by_tool: dict[str, Stats], d0, d1, prev: tuple[str, str] | None) -> dict`. Both `Stats` objects are already range-scoped, with the same calendar window from `range_window`. Payload:
  ```python
  {"source": "all", "cost_unit": None, "generated": ...,
   "totals": {"tokens", "input", "output", "cache_read", "cache_write", "prompts", "sessions",
              "active_min", "active_days"},          # no "cost"
   "by_tool": {tool: {"tokens", "input", "output", "cache_read", "cache_write", "prompts", "sessions",
                      "active_min", "cost", "cost_unit", "premium_requests"}},
   "days": [{"day", "tokens_by_tool": {tool: int}, "messages": slots, "late_msgs": late_slots,
             "active_min": slots*10, "focus_max": longest_focus(slots),
             "interruptions": sum, "tool_errors": sum}],
   "heatmap": 7x24,                                   # cell = count of union slots in that weekday-hour
   "models": [{"name", "tool", "msgs", "input", "output", "cost", "cost_unit"}],
   "projects": [{"name", "tokens", "by_tool": {tool: {"input", "output", "prompts", "loc", "cost", "cost_unit"}}}],
   "sessions_list": [... each row plus "tool"],
   "prev": {"tokens", "prompts", "sessions", "active_min"}}   # only for rolling windows
  ```
  - `tokens` = input + cache_read + cache_write + output.
  - Late = slot hour ≥ `QUIET_START` or < `QUIET_END`.
  - Projects are merged by label (labels already match across tools, Task 8).
- `build_payload(..., source="all")` builds each available tool's ranged `Stats` with the same window and calls `build_all_ai`. It adds `data["sources"]`, `data["streak"]`/`best_streak`/`active_days` from the union of all tools' active days, and `catalog`.

- [ ] **Step 1: Write the failing tests.**

```python
def test_union_slots_count_overlap_once(self):
    a, b = afi.Stats(), afi.Stats()
    a.days["2026-10-01"]["messages"] = 1; a.day_buckets["2026-10-01"] |= {60, 61}
    b.days["2026-10-01"]["messages"] = 1; b.day_buckets["2026-10-01"] |= {61, 62}
    self.assertEqual(afi.union_slots([a, b], None, None), {"2026-10-01": {60, 61, 62}})

def test_all_ai_sums_tokens_never_cost(self):
    d = afi.build_all_ai({"claude": self.claude_st, "copilot": self.copilot_st}, None, None, None)
    t, bt = d["totals"], d["by_tool"]
    self.assertNotIn("cost", t)
    self.assertEqual(t["tokens"], bt["claude"]["tokens"] + bt["copilot"]["tokens"])
    self.assertEqual((bt["claude"]["cost_unit"], bt["copilot"]["cost_unit"]), ("usd", "credits"))
    self.assertEqual(t["active_min"], sum(len(s) for s in afi.union_slots(
        [self.claude_st, self.copilot_st], None, None).values()) * 10)

def test_all_ai_models_tagged_and_projects_merged(self):
    d = afi.build_all_ai({"claude": self.claude_st, "copilot": self.copilot_st}, None, None, None)
    self.assertEqual({m["tool"] for m in d["models"]}, {"claude", "copilot"})
    demo = next(p for p in d["projects"] if p["name"] == "demo/proj")   # Claude fixture cwd /demo/proj + a Copilot chat in workspace /demo/proj
    self.assertEqual(set(demo["by_tool"]), {"claude", "copilot"})
```

`self.claude_st` = `afi.Scanner(FIX).build_stats()`. `self.copilot_st` = the Task 8 tree, plus one extra chat in a workspace whose folder is `/demo/proj`.

- [ ] **Step 2: Run them and confirm they fail.**

Run: `python -m unittest test_multi_source -v`
Expected: FAIL.

- [ ] **Step 3: Implement `union_slots`, `build_all_ai` and the `source=all` branch.**

- [ ] **Step 4: Run all tests.**

Run: `python -m unittest -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add ai_fluency_insights.py test_multi_source.py
git commit -m "Add All AI payload with like-for-like combining rules"
```

---

### Task 12: All AI UI

**Files:** Modify `ai_fluency_insights.py` (HTML: three new section containers; JS: `setSection`, the nav, `render`, three renderers)

**Interfaces:**
- Consumes: the Task 11 payload.
- Produces (JS):
  - `ALL_SECTIONS = ['overview', 'focus', 'tools', 'projects']`
  - containers `#sa-overview`, `#sa-tools`, `#sa-projects`
  - `renderAllOverview()`, `renderAllTools()`, `renderAllProjects()`
  - All AI Focus reuses `renderFocus()` and `#s-focus`. The payload's `days.messages` / `late_msgs` / `heatmap` are slot-based, so its shares are time shares. Change the heatmap tooltip word "message" to "active 10-min slot" when `D.source==='all'`.

- [ ] **Step 1: Sidebar and routing.**
  - While `SOURCE==='all'`, nav buttons outside `ALL_SECTIONS` get class `dim`: `opacity:.4`, `cursor:not-allowed`, `title="Pick Claude Code or GitHub Copilot to see this."`. Clicks on them are ignored.
  - Switching to All AI on such a section calls `setSection('overview')`.
  - `setSection` shows `#sa-<s>` instead of `#s-<s>` for `overview`, `tools` and `projects` when `SOURCE==='all'`.
- [ ] **Step 2: `renderAllOverview`.**
  - KPI cards:
    - Total tokens (sub: "Claude X · Copilot Y")
    - Prompts
    - Sessions
    - Active time (`fmtMin`)
    - Active days
    - a **Cost** card showing two lines, "Claude Code $X est." and "GitHub Copilot N credits" (+ "M premium requests" if > 0), with sub "Not added together: different units".
  - Deltas use `D.prev`.
  - A daily stacked bar `tokens_by_tool`: Claude = the Claude accent `#c2603d` light / `#e8916b` dark; Copilot = `#7355ea` / `#a78bfa`.
  - A "Share by tool" doughnut of tokens.
- [ ] **Step 3: `renderAllTools`.** Models table columns: Model | Tool | Calls | Input | Output | Cost (`'$'` or `' cr'` per row unit). A doughnut of output tokens by `name · tool`.
- [ ] **Step 4: `renderAllProjects`.** Table columns: Project | Total tokens | Claude tokens | Copilot tokens | Prompts. A row click opens the existing project sheet only when one tool has data; otherwise it shows a tooltip "Open it from Claude Code or GitHub Copilot".
- [ ] **Step 5: Verify by running the app.**

Run: `python ai_fluency_insights.py --no-browser --port 8790`. Switch to All AI and check:
- Teal accents.
- Only four sidebar items are active.
- Total tokens = the sum of each tool's total for the same range button. Check by switching tools on "Last 30 days" and comparing.
- The cost card shows two separate figures.
- No console errors.
- Screenshot it.

- [ ] **Step 6: Commit.**

```bash
git add ai_fluency_insights.py
git commit -m "Add All AI overview, time, models and projects pages"
```

---

### Task 13: `--setup-copilot` (exact tokens, opt-in)

**Files:** Modify `ai_fluency_insights.py` (`run_setup_copilot`, `main()` flags `--setup-copilot`, `--undo`, reusing `--dry-run`); `test_multi_source.py`

**Interfaces:**
- `COPILOT_OTEL_KEYS = ("github.copilot.chat.otel.enabled", "github.copilot.chat.otel.exporterType", "github.copilot.chat.otel.outfile")`
- `run_setup_copilot(user_roots: list[str], dry_run=False, undo=False, input_fn=input, print_fn=print, home=None) -> int`
  - **Target:** the `settings.json` of the first user root that is a stable `Code/User`, else the first root. Return code 1 with a message if none.
  - **Values:** `True`, `"file"`, and `os.path.join(home or ~, ".copilot", "otel", "vscode-copilot-otel.jsonl")` (absolute). Create the folder unless `dry_run`.
  - **Parse:** with strict `json.loads`. On failure (JSONC comments or trailing commas), print the three `"key": value,` lines and "Paste these into your VS Code settings (Ctrl+Shift+P → Preferences: Open User Settings (JSON)).", and return 2 without writing.
  - **Confirm:** show the changes and ask `"Apply these changes? [y/N] "`. Anything but `y`/`yes` → return 0, unchanged.
  - **Write:** copy to `settings.json.bak-aifi-YYYYmmdd-HHMMSS`, then write with `indent=4`. Print "Reload VS Code windows (Developer: Reload Window). Chats after that get exact token counts."
  - **`undo`:** removes the three keys the same way (strict JSON only, with a backup).
  - **`dry_run`:** prints only, never writes.

- [ ] **Step 1: Write the failing tests.**

```python
def test_setup_copilot_writes_keys_with_backup(self):
    ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, {"editor.fontSize": 14})
    rc = afi.run_setup_copilot([ur], input_fn=lambda _: "y", print_fn=lambda *a: None, home=self.tmp)
    s = json.load(open(p, encoding="utf-8"))
    self.assertEqual((rc, s["github.copilot.chat.otel.enabled"], s["github.copilot.chat.otel.exporterType"]), (0, True, "file"))
    self.assertTrue(any(n.startswith("settings.json.bak-aifi-") for n in os.listdir(os.path.dirname(p))))

def test_setup_copilot_refuses_jsonc(self):
    ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, '{\n  // mine\n  "a": 1,\n}\n')
    before = open(p, encoding="utf-8").read()
    self.assertEqual(afi.run_setup_copilot([ur], input_fn=lambda _: "y", print_fn=lambda *a: None, home=self.tmp), 2)
    self.assertEqual(open(p, encoding="utf-8").read(), before)

def test_setup_copilot_dry_run_and_decline_change_nothing(self): ...   # dry_run=True, then input "n": file bytes unchanged, rc 0
def test_setup_copilot_undo_removes_keys(self): ...                    # apply, then undo=True: none of COPILOT_OTEL_KEYS remain
```

The two `...` tests get their bodies from the comment beside each one.

- [ ] **Step 2: Run them and confirm they fail.**

Run: `python -m unittest test_multi_source -v`
Expected: FAIL.

- [ ] **Step 3: Implement `run_setup_copilot` and the flags.** Follow `run_setup_notifications` for prompting and printing style.

- [ ] **Step 4: Run all tests.**

Run: `python -m unittest -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add ai_fluency_insights.py test_multi_source.py
git commit -m "Add --setup-copilot to turn on exact Copilot token logging"
```

---

### Task 14: Docs and final verification

**Files:** Modify `README.md`, `docs/HOW_IT_WORKS.md`, `docs/METRICS.md`, `docs/DASHBOARD_GUIDE.md`, `SETUP.md`, `docs/superpowers/specs/2026-10-24-copilot-source-design.md` (only the "Last verified" line)

- [ ] **Step 1: Update the docs.**
  - **README:** what's new (Copilot + All AI), the switch, and the flags `--copilot-root`, `--no-copilot`, `--setup-copilot`, `--dump --source`.
  - **HOW_IT_WORKS:** the "data source" section lists the Copilot stores and the one-session-one-source rule.
  - **METRICS:**
    - A "Copilot" section: credits, premium requests, approx. vs exact tokens, the token convention.
    - An "All AI" section: the combining-rules table from spec §5.5.
    - The calendar-day range rule.
  - **DASHBOARD_GUIDE:** the switch, colours, not-available cards.
  - **SETUP:** `--setup-copilot`, and running `tools/copilot_log_check.py` when Copilot data looks wrong.
- [ ] **Step 2: Run the full test suite.**

Run: `python -m unittest -v`
Expected: all PASS. Note the count.

- [ ] **Step 3: Run the app end to end on the home laptop.**

Run: `python ai_fluency_insights.py --no-browser --port 8790`. Click through all three tools × all sections, on 7 / 30 / 90 / All time and one custom range. Check:
- no console errors
- the Claude view unchanged for All time
- All AI totals = Claude + Copilot for the same button
- Copilot "Not available" cards where expected
- `/api/stats` makes no outbound network requests (`read_network_requests` shows only localhost)
- [ ] **Step 4: Commit.**

```bash
git add README.md docs SETUP.md
git commit -m "Document GitHub Copilot and All AI views"
```

---

### Task 15: Office-log adjustments (added 2026-10-26, runs after Task 10, before Task 11)

Added 2026-10-26 from the reports of three office machines (`tools/copilot_log_check.py`
v1, structure only). It runs after Task 10 and before Task 11. It touches the Copilot reader, the scanner
and Data Health. **The Claude view must stay byte-identical, and the golden tests must stay green.**

#### What the reports confirmed (no change needed)

Every field the reader uses exists with the expected name and type. That covers:
- chat sessions: requests[].timestamp/requestId/modelId/message.text/promptTokens/completionTokens/
  copilotCredits/elapsedMs, result.details, result.metadata.toolCallRounds[].modelId/timestamp/toolCalls[].id/name/arguments,
  toolCallResults, result.timings.totalElapsed, creationDate, customTitle, inputState.mode.kind;
- the older whole-file `.json` format (Jan 2026, "• 1x" premium details, rounds without modelId/timestamp);
- debug logs: `llm_request` attrs.inputTokens/outputTokens/cachedTokens/copilotUsageNanoAiu/model, plus ts and spanId;
- transcripts;
- CLI `session.usage_record` data.usage.inputTokens/outputTokens/cacheReadTokens/cacheWriteTokens/copilotUsage.totalNanoAiu/apiCallId/model;
- session-store.db `sessions(cwd, repository, branch, summary)`.

Two facts were verified separately:
- CLI `inputTokens` includes cache read and write. On this laptop, tokenDetails input + cache_read + cache_write == inputTokens. The current formula is right.
- Chat `promptTokens` equals the last call's total input (the same max as the debug-log inputTokens).

#### Changes

##### 1. One name per model (`copilot_source.normalize_model`)
One model currently appears under several spellings, which splits it into separate rows.
Real examples:
- `claude-haiku-4-5-20251001` (resolvedModel) vs `claude-haiku-4.5` (round modelId) vs `copilot/claude-haiku-4.5` (request modelId);
- `claude-haiku-5-5` vs `claude-haiku-5.5`;
- `gpt-5.5-2026-04-23` vs `gpt-5.5`.

After the existing steps (lower case, strip `copilot/`, `auto` → None):
- (a) strip a trailing date `-YYYYMMDD` or `-YYYY-MM-DD`;
- (b) for ids starting `claude-` that end in `-<digits>-<1–2 digits>`, join the last two with a dot (`claude-haiku-4-5` → `claude-haiku-4.5`). Ids like `claude-3-5-sonnet` (not ending in digits) stay as they are.

This applies everywhere normalize_model is used (chat, debug, OTel, CLI). Add unit tests with the real examples, plus ids that must not change (`claude-sonnet-5`, `gpt-6-luna`, `gpt-5.6-luna`, `mai-code-1.1-flash`, `claude-3-5-sonnet`).

##### 2. Real "interrupted" figure for Copilot from `requests[].modelState`
The office chat sessions record `requests[].modelState = {value, completedAt}`. VS Code's enum, verified in
microsoft/vscode `chatService.ts` `ResponseModelState`, is: 0 Pending, 1 Complete, **2 Cancelled**, 3 Failed, 4 NeedsInput.
- In `_translate_request`, when `modelState.value == 2`, append a user event right after the request's final event, with the same timestamp and content `"[Request interrupted by user]"`. `parse_entry` already counts that text as an interruption: not a prompt, and it marks the turn as interrupted. Every other value adds nothing (3 Failed has no Claude-side metric; 4 NeedsInput is not final).
- **End time:** when `modelState.completedAt` is a valid time ≥ the start, use it as the request's end. Otherwise use start + elapsed, as now. The existing "never earlier than the last round" rule still applies.
- **Interruptions are no longer statically unavailable.** `SOURCES["copilot"]["no_interruptions"]` must become a per-payload decision:
  - Copilot's interruptions count as *available* when at least one scanned chat request has a `modelState` with a numeric value, over all scanned data, not just the selected range;
  - otherwise they stay "Not available for Copilot" as now, with the reason "Your Copilot logs don't record stopped requests (older VS Code)".
  - Everything that reads the flag must follow the computed value: the advisor "clean" rec, the weekly text, `na.interruptions`, the Friction vs "Tool errors" KPI from Task 10, and the friction hint.
  - Do not invent zeros: with no modelState anywhere, keep today's behaviour.
- **Data Health:** add a Copilot row "Stopped requests". Use ok when every chat request with a timestamp has a modelState, partial when some do, missing when none do. Phrase the reason the way the other rows are phrased.

##### 3. Agent-log types that are normal, not "unknown"
The CLI and transcript logs carry routine types that the reader currently counts in `meta["unknown_types"]`. That makes the Data Health "unknown types" chip fire on every CLI user. Real types seen:
- `permission.requested` / `permission.completed`;
- `skill.invoked` / `skill.context_delivered_ref`;
- `subagent.selected`;
- `model.turn_started` / `model.model_call_started` / `model.model_call_success` / `model.message` / `model.response` / `model.turn_ended` / `model.messages_snapshot` / `model.captured_assignment_context`;
- `session.info` / `session.model_change` / `session.binary_asset`, which are already ignored as `session.*`.

Add the prefixes `permission.`, `skill.`, `subagent.` and `model.` to `_AGENT_IGNORED_PREFIXES`. Truly new types must still be counted.

##### 4. Realistic fixtures and an end-to-end test
Update the builders in `copilot_fixtures.py`, and the tests that use them, to the real shapes:
- **chat requests:**
  - `modelState {value, completedAt}`;
  - `promptTokenDetails: [{category, label, percentageOfPrompt}]`;
  - `result.details` with no credits (`"Claude Haiku 4.5"`);
  - `result.metadata.resolvedModel` with a dated id;
  - `result.timings {firstProgress, totalElapsed}`;
  - request `modelId` `copilot/auto`.
- **a debug-log session folder:**
  - besides `llm_request`, it holds `session_start` (attrs.copilotVersion/vscodeVersion, `v`), `turn_start`/`turn_end`, `tool_call`, `agent_response`, `generic`, `discovery` and `user_message` lines, which must all be ignored;
  - also a single-line JSON array file (the model list) and a single-line object with only `content` (a prompt file), both ignored without error.
- **CLI:**
  - `session.usage_record` with `copilotUsage.tokenDetails` (four types), `accounting {sequence, sourceSessionId, usageId}`, `reasoningTokens`, `initiator`, `finishReason`;
  - `session.usage_checkpoint` with both `accountingSnapshot.modelMetrics.<model>` and `accountingSnapshot.agentMetrics.main.modelMetrics.<model>`;
  - `session.start` with `context.cwd` only (no branch or repository);
  - one record each of the types in change 3.
- **End to end:** add one test that builds a small fake VS Code user root plus `~/.copilot` in a temp dir, shaped like the reports (workspaceStorage/<hash>/workspace.json + chatSessions/*.jsonl + GitHub.copilot-chat/debug-logs/<sid>/*.jsonl + transcripts, globalStorage/emptyWindowChatSessions, a CLI session). Run `CopilotScanner` → `build_payload(source="copilot")` and assert:
  - one model row per model (change 1);
  - interruptions available with the right count (change 2);
  - exact tokens and credits from the debug log for the session that has one, approx for the others;
  - no unknown-types chip for the change 3 types.

##### 5. Exact-tokens hint names the real setting
Wherever Data Health or a hint tells the user how to get exact tokens, name the setting that the reports
confirm works: `github.copilot.chat.agentDebugLog.fileLogging.enabled` (VS Code Settings › "Agent Debug Log ›
File Logging"). Name it next to the OTel option (`--setup-copilot`, Task 13), not instead of it.
Note: the debug log stores full prompts locally, while the OTel export does not by default.

#### Constraints
- Global constraints apply (read-only, stdlib, no fake zeros, credits never summed with USD).
- Claude payload golden tests stay green; Claude DOM unchanged.
- `copilot_source.py` must not import the main module.
- `python -m unittest -v` all green with pristine output; `node --check` on the extracted JS if you touch it.
- Commit with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
