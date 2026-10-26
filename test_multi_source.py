"""Tests for the multi-source dashboard: calendar-day windows, the reusable
payload builder and (in later tasks) the Copilot source.

Run with:  python -m unittest test_multi_source -v
"""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs

import ai_fluency_insights as afi
import copilot_fixtures as cf
import copilot_source as cs
from test_copilot_source import CLI as CLI_EVENTS      # the Copilot CLI session fixture from Task 6

FIX = os.path.join(os.path.dirname(__file__), "test_fixtures", "claude")
FIX_DAYS = (1, 3, 10)           # the October 2026 days the Claude fixture has activity on
FIX_TZ_SKIP = ("Claude fixture timestamps (12:00Z) fall at night or on another date in this "
               "timezone; golden output was captured for UTC-6..UTC+10")


def fixture_clock_ok(tz=None):
    """True when each fixture day's 12:00Z lands on the same calendar date and
    between 06:00 and 23:00 in `tz` (default: the machine's local zone). Outside
    that, parse_entry buckets the messages as late-night or on another day, so the
    golden output (captured in a daytime zone) no longer applies."""
    for day in FIX_DAYS:
        utc = datetime(2026, 10, day, 12, tzinfo=timezone.utc)
        loc = utc.astimezone(tz)
        if loc.date() != utc.date() or not 6 <= loc.hour < 23:
            return False
    return True


def comparable(d):
    """JSON round-trip, then drop clock- and timezone-sensitive parts."""
    d = json.loads(json.dumps(d))
    for k in ("generated", "notifications", "plan_window", "root", "heatmap"):
        d.pop(k, None)
    for day in d.get("days", []):
        day.pop("late_msgs", None)
    return d


class PayloadRegressionTests(unittest.TestCase):
    def setUp(self):
        if not fixture_clock_ok():
            self.skipTest(FIX_TZ_SKIP)

    def check(self, qs, golden):
        new = comparable(afi.build_payload(afi.Scanner(FIX), parse_qs(qs)))
        with open(os.path.join(os.path.dirname(FIX), golden), encoding="utf-8") as f:
            old = comparable(json.load(f))
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
        if not fixture_clock_ok():
            self.skipTest(FIX_TZ_SKIP)
        d = afi.build_payload(afi.Scanner(FIX), parse_qs("days=7"), today=date(2026, 10, 10))
        self.assertEqual((d["totals"]["prompts"], d["totals"]["output"]), (1, 80))  # only 10-10
        self.assertEqual((d["prev"]["prompts"], d["prev"]["output"]), (2, 80))      # 10-01 + 10-03


class FixtureClockGuardTests(unittest.TestCase):
    """The skip guard lets fixture-based tests run only where 12:00Z is daytime."""
    def test_guard_boundaries(self):
        def zone(hours):
            return timezone(timedelta(hours=hours))
        for h in (-6, 0, 5.5, 10):             # 06:00 .. 22:00 on the same date
            self.assertTrue(fixture_clock_ok(zone(h)), h)
        for h in (-7, -12, 11, 13):            # early morning, late night, next date
            self.assertFalse(fixture_clock_ok(zone(h)), h)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cps_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def copilot_with_chat(self):
        """(user root, CopilotScanner) over a temp tree holding one chat session."""
        ur = cf.make_user_root(self.tmp)
        ws = cf.add_workspace(ur, "h1", "/work/demo")
        cf.add_chat_jsonl(ws, "c1", [{"kind": 0, "v": {"sessionId": "c1", "requests": []}}])
        return ur, afi.CopilotScanner([ur], os.path.join(self.tmp, "home"))


class SourceRegistryTests(TmpCase):
    def test_registry_contents(self):
        self.assertEqual(list(afi.SOURCES), ["claude", "copilot"])
        self.assertEqual(afi.SOURCES["claude"], {
            "label": "Claude Code", "short": "Claude", "cost_unit": "usd",
            "instructions": ("CLAUDE.md", "AGENTS.md"), "skip_recs": ()})
        copilot = dict(afi.SOURCES["copilot"])
        self.assertEqual(copilot.pop("rec_actions").keys(), {"longsess", "risky"})   # tips that name Claude-only features
        self.assertEqual(copilot, {
            "label": "GitHub Copilot", "short": "Copilot", "cost_unit": "credits",
            "instructions": (".github/copilot-instructions.md", "AGENTS.md"),
            "skip_recs": ("coldstart", "cachereset"),
            "no_interruptions": True})                              # its logs never record a cancelled request

    def test_enabled_sources_copilot_only(self):       # Review Focus 4
        sc = afi.CopilotScanner([cf.make_user_root(self.tmp)], self.tmp)
        with patch.object(sc, "available", return_value=True):
            self.assertEqual(afi.enabled_sources(os.path.join(self.tmp, "missing"), sc), ["copilot"])

    def test_enabled_sources_combinations(self):
        sc = afi.CopilotScanner([cf.make_user_root(self.tmp)], self.tmp)
        missing = os.path.join(self.tmp, "missing")
        self.assertEqual(afi.enabled_sources(FIX, None), ["claude"])
        self.assertEqual(afi.enabled_sources(missing, None), [])
        self.assertEqual(afi.enabled_sources(FIX, sc), ["claude"])          # no Copilot logs yet
        with patch.object(sc, "available", return_value=True):
            self.assertEqual(afi.enabled_sources(FIX, sc), ["claude", "copilot"])

    def test_pick_source(self):
        both = {"claude": object(), "copilot": object()}
        self.assertEqual(afi.pick_source(both, "copilot"), "copilot")
        self.assertEqual(afi.pick_source(both, ""), "claude")
        self.assertEqual(afi.pick_source(both, "bogus"), "claude")
        self.assertEqual(afi.pick_source(both, "all"), "claude")     # "all" is not one tool: callers that want it check first
        self.assertEqual(afi.pick_source({"copilot": object()}, "claude"), "copilot")
        self.assertEqual(afi.pick_source({"copilot": object()}, ""), "copilot")

    def test_sources_status(self):
        _ur, sc = self.copilot_with_chat()
        claude = afi.Scanner(FIX)
        rows = afi.sources_status({"claude": claude, "copilot": sc})
        self.assertEqual([(r["id"], r["label"], r["short"], r["available"]) for r in rows],
                         [("claude", "Claude Code", "Claude", True),
                          ("copilot", "GitHub Copilot", "Copilot", True),
                          ("all", "All AI", "AI", True)])
        rows = afi.sources_status({"copilot": sc})            # Claude logs missing
        self.assertEqual([(r["id"], r["available"]) for r in rows],
                         [("claude", False), ("copilot", True), ("all", False)])
        rows = afi.sources_status({"claude": claude})         # --no-copilot
        self.assertEqual([(r["id"], r["available"]) for r in rows],
                         [("claude", True), ("copilot", False), ("all", False)])


class SourcePayloadTests(TmpCase):
    def test_payload_has_source_fields(self):
        d = afi.build_payload(afi.Scanner(FIX), parse_qs("days=0"))
        self.assertEqual((d["source"], d["cost_unit"]), ("claude", "usd"))

    def test_copilot_payload_empty_when_no_logs(self):
        sc = afi.CopilotScanner([cf.make_user_root(self.tmp)], self.tmp)
        d = afi.build_payload(sc, parse_qs("days=0"), source="copilot")
        self.assertEqual((d["source"], d["cost_unit"], d["totals"]["prompts"]), ("copilot", "credits", 0))

    def test_copilot_scanner_available_for_each_session_kind(self):
        ur = cf.make_user_root(self.tmp)
        home = os.path.join(self.tmp, "home", ".copilot")
        self.assertFalse(afi.CopilotScanner([ur], home).available())
        ws = cf.add_workspace(ur, "h1", "/work/demo")
        cf.add_debug_log(ws, "d1", [{"type": "session_start"}])    # debug logs alone are not sessions
        cf.add_session_store(ur, [])
        self.assertFalse(afi.CopilotScanner([ur], home).available())
        cf.add_cli_session(home, "k1", [{"type": "session.start"}])
        self.assertTrue(afi.CopilotScanner([ur], home).available())
        cf.add_transcript(ws, "t1", [{"type": "session.start"}])
        self.assertTrue(afi.CopilotScanner([ur], os.path.join(self.tmp, "nohome")).available())

    def test_copilot_files_rediscovered_at_most_every_15_seconds(self):
        _ur, sc = self.copilot_with_chat()
        self.assertEqual(sc.files_ttl, 15)
        with patch.object(afi.copilot_source, "discover", wraps=cs.discover) as disc:
            first = sc.files()
            self.assertIs(sc.files(), first)
            self.assertEqual(disc.call_count, 1)
            sc.files_ttl = 0
            sc.files()
            self.assertEqual(disc.call_count, 2)


class HandlerRoutingTests(TmpCase):
    def serve(self, **attrs):
        """Run a Handler subclass with these class attributes; returns get(path) -> JSON."""
        handler = type("TestHandler", (afi.Handler,), dict(attrs, root=FIX))
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = "http://127.0.0.1:%d" % srv.server_address[1]

        def get(path):
            with opener.open(base + path, timeout=30) as r:
                return json.loads(r.read().decode())
        return get

    def both(self):
        _ur, sc = self.copilot_with_chat()
        return {"claude": afi.Scanner(FIX), "copilot": sc}

    def test_stats_default_is_claude_with_extras(self):
        get = self.serve(scanners=self.both())
        for path in ("/api/stats?days=0", "/api/stats?days=0&source=claude",
                     "/api/stats?days=0&source=bogus", "/api/stats?days=0&source="):
            d = get(path)
            self.assertEqual((d["source"], d["cost_unit"]), ("claude", "usd"), path)
            self.assertEqual(d["root"], FIX)
            self.assertIn("notifications", d); self.assertIn("plan_window", d)
            self.assertGreater(d["totals"]["prompts"], 0)

    def test_handler_scanner_alone_still_serves_claude(self):
        d = self.serve(scanner=afi.Scanner(FIX))("/api/stats?days=0&source=copilot")
        self.assertEqual((d["source"], d["totals"]["prompts"] > 0), ("claude", True))
        self.assertEqual([(s["id"], s["available"]) for s in d["sources"]],
                         [("claude", True), ("copilot", False), ("all", False)])

    def test_stats_lists_sources(self):
        d = self.serve(scanners=self.both())("/api/stats?days=0")
        self.assertEqual([(s["id"], s["available"]) for s in d["sources"]],
                         [("claude", True), ("copilot", True), ("all", True)])

    def test_stats_source_copilot(self):
        scanners = self.both()
        d = self.serve(scanners=scanners)("/api/stats?days=0&source=copilot")
        self.assertEqual((d["source"], d["cost_unit"], d["totals"]["prompts"]), ("copilot", "credits", 0))
        for claude_only in ("notifications", "plan_window"):
            self.assertNotIn(claude_only, d)
        sc = scanners["copilot"]                     # the root chip shows the Copilot paths instead
        self.assertEqual(d["root"], afi.copilot_root_label(sc.user_roots, sc.copilot_home))

    def test_copilot_only_machine_serves_copilot(self):
        _ur, sc = self.copilot_with_chat()
        get = self.serve(scanners={"copilot": sc})
        for path in ("/api/stats?days=0", "/api/stats?days=0&source=claude",
                     "/api/stats?days=0&source=copilot"):
            d = get(path)
            self.assertEqual((d["source"], d["cost_unit"]), ("copilot", "credits"), path)
            self.assertEqual([(s["id"], s["available"]) for s in d["sources"]],
                             [("claude", False), ("copilot", True), ("all", False)])

    def test_ai_endpoints_refuse_all(self):
        get = self.serve(scanners=self.both())
        for path in ("/api/ai_summary?week=x&source=all", "/api/reflect?week=x&q=hi&source=all"):
            self.assertEqual(get(path), {"ok": False, "error": "pick one tool"}, path)

    def test_ai_endpoints_read_the_chosen_tools_weeks(self):
        get = self.serve(scanners=self.both())
        wk = afi.build_weeks(afi.Scanner(FIX).build_stats())[0]["key"]
        for path in ("/api/ai_summary?week=%s&source=copilot", "/api/reflect?week=%s&q=hi&source=copilot"):
            r = get(path % wk)                       # a real Claude week is unknown to Copilot
            self.assertEqual(r, {"ok": False, "error": "unknown week %r" % wk}, path)


class MainFlagTests(TmpCase):
    def run_main(self, *args):
        # An empty home and config base, so only --copilot-root (not this machine) supplies logs.
        home, conf = os.path.join(self.tmp, "userhome"), os.path.join(self.tmp, "appdata")
        env = dict(os.environ, HOME=home, USERPROFILE=home, APPDATA=conf, XDG_CONFIG_HOME=conf,
                   CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "nope"))
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_fluency_insights.py")
        return subprocess.run([sys.executable, script] + list(args), env=env, cwd=self.tmp,
                              capture_output=True, text=True, timeout=120)

    def test_dump_source_copilot_with_copilot_root(self):
        ur, _sc = self.copilot_with_chat()
        r = self.run_main("--dump", "--source", "copilot", "--dir", FIX, "--copilot-root", ur)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual((d["source"], d["cost_unit"]), ("copilot", "credits"))
        self.assertEqual([(s["id"], s["available"]) for s in d["sources"]],
                         [("claude", True), ("copilot", True), ("all", True)])

    def test_dump_defaults_to_claude_and_no_copilot_disables_it(self):
        ur, _sc = self.copilot_with_chat()
        r = self.run_main("--dump", "--dir", FIX, "--copilot-root", ur, "--no-copilot")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["source"], "claude")
        self.assertEqual([(s["id"], s["available"]) for s in d["sources"]],
                         [("claude", True), ("copilot", False), ("all", False)])
        self.assertIn("totals", d)                     # the old --dump keys are still there

    def test_dump_of_a_disabled_source_fails(self):
        r = self.run_main("--dump", "--source", "copilot", "--no-copilot", "--dir", FIX)
        self.assertNotEqual(r.returncode, 0)

    def test_exits_only_when_no_source_is_enabled(self):
        r = self.run_main("--dump", "--dir", os.path.join(self.tmp, "missing"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("Log directory not found", r.stdout)
        ur, _sc = self.copilot_with_chat()              # Copilot alone is enough to start
        r = self.run_main("--dump", "--dir", os.path.join(self.tmp, "missing"), "--copilot-root", ur)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["source"], "copilot")


T0 = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
MS = int(T0.timestamp() * 1000)                       # epoch milliseconds, as the chat files write them


def local_day(dt):
    return dt.astimezone().strftime("%Y-%m-%d")


def total(st, key):
    return sum(d[key] for d in st.days.values())


class CopilotTreeCase(TmpCase):
    """TmpCase over the Task 8 Copilot tree: four sessions, shared by the Task 8 and Task 9 tests.

    Built per test (not once per class) because some tests rewrite its files. Sets self.ur, self.home,
    self.c1, self.e1 and self.scanner."""

    def setUp(self):
        super().setUp()
        ur = self.ur = cf.make_user_root(self.tmp)
        home = self.home = os.path.join(self.tmp, "home", ".copilot")
        ws = cf.add_workspace(ur, "h1", "/work/demo")
        # c1: a chat (approx 1200/300, 0.5 credits) whose debug log holds the exact call (5000 in, 3000 cached, 400 out)
        self.c1 = cf.add_chat_jsonl(ws, "c1", [{"kind": 0, "v": {
            "sessionId": "c1", "customTitle": "Demo chat", "inputState": {"mode": {"kind": "agent"}},
            "requests": [cf.chat_request("r1", MS, "fix the login bug", prompt=1200, completion=300, credits=0.5)]}}])
        cf.add_debug_log(ws, "c1", [{"type": "session_start"}, cf.llm_request("sp1", MS + 2000, 5000, 400, 3000)])
        # t1: a transcript only: one prompt, no tokens anywhere
        cf.add_transcript(ws, "t1", [
            {"type": "session.start", "timestamp": "2026-10-24T11:00:00Z",
             "data": {"sessionId": "t1", "copilotVersion": "0.67.0", "vscodeVersion": "1.139.0"}},
            {"type": "user.message", "timestamp": "2026-10-24T11:00:01Z", "data": {"content": "explain the parser"}}])
        # k1: a Copilot CLI session, plus a VS Code chat file with the same id that must never be counted
        cf.add_cli_session(home, "k1", CLI_EVENTS)
        cf.add_chat_jsonl(ws, "k1", [{"kind": 0, "v": {"requests": [
            cf.chat_request("rk", MS, "never counted", prompt=99999, completion=777, credits=9.0)]}}])
        # e1: an empty-window chat billed in premium requests ("• 3x"), approx tokens 10/5
        self.e1 = cf.add_empty_window_chat(ur, "e1", {"requests": [
            cf.chat_request("re", MS + 3600000, "/explain what 3x means", model="copilot/claude-opus-4.5",
                            prompt=10, completion=5, details="Claude Opus 4.5 • 3x")]})
        cf.add_session_store(ur, [{"id": "c1", "branch": "feature/login", "summary": "Store title"},
                                  {"id": "t1", "summary": "Explain the parser"}])
        otel = os.path.join(self.tmp, "otel", "copilot.jsonl")      # an OTel file with no call for these sessions
        cf.add_otel(otel, [cf.otel_span("zz", "s1", 10, 1)])
        cf.write_settings(ur, {"github.copilot.chat.otel.outfile": otel})
        self.scanner = afi.CopilotScanner([ur], home)


class CopilotBuildStatsTests(CopilotTreeCase):
    """CopilotScanner.build_stats over one tree with four sessions (Task 8)."""

    def test_one_session_one_token_source(self):
        st = self.scanner.build_stats()
        self.assertEqual(len(st.sessions), 4)
        self.assertEqual(sum(d["output"] for d in st.days.values()), 400 + 20 + 5)   # debug, CLI, e1; never 300 or the 99999 chat
        self.assertEqual(dict(st.token_modes), {"exact": 2, "approx": 1, "none": 1})
        self.assertEqual(sum(st.premium_days.values()), 3.0)
        self.assertIn("(no folder)", st.projects)

    def test_every_store_counted_once(self):
        st = self.scanner.build_stats()
        self.assertEqual(total(st, "prompts"), 4)                       # c1, t1, k1 (CLI), e1
        self.assertEqual(total(st, "input"), 2000 + 135 + 10)           # debug 5000-3000, CLI, e1
        self.assertEqual(total(st, "cache_read"), 3000 + 14976)
        self.assertAlmostEqual(total(st, "cost"), 0.5 + 0.211036)       # c1 chat credits, k1 CLI credits; e1 none
        self.assertEqual(st.no_cache_split, 1)
        self.assertEqual(st.no_credit_calls, 0)        # c1's credits come from its chat file; e1 is premium-billed
        self.assertEqual(st.sessions["k1"]["prompts"], 1)
        self.assertNotIn("never counted", json.dumps(st.longest_prompts))

    def test_projects_branches_titles(self):
        st = self.scanner.build_stats()
        self.assertEqual(st.project_cwd["work/demo"], "/work/demo")
        self.assertEqual({sid: s["project"] for sid, s in st.sessions.items()},
                         {"c1": "work/demo", "t1": "work/demo", "k1": "work/demo", "e1": "(no folder)"})
        self.assertEqual(st.branches["feature/login"]["output"], 400)      # c1: the session store's branch
        self.assertEqual(st.branches["main"]["output"], 20)                # k1: the CLI's own branch
        self.assertEqual(st.branches["(none)"]["output"], 5)               # e1: no branch anywhere
        self.assertEqual(st.session_titles, {"c1": "Demo chat", "t1": "Explain the parser",
                                             "k1": "run tests", "e1": "/explain what 3x means"})

    def test_surfaces_modes_slash_versions(self):
        st = self.scanner.build_stats()
        self.assertEqual(dict(st.surfaces["vscode"]), {"sessions": 3, "prompts": 3, "output": 405})
        self.assertEqual(dict(st.surfaces["cli"]), {"sessions": 1, "prompts": 1, "output": 20})
        self.assertEqual(st.feature_days["agent"], {local_day(T0)})
        self.assertEqual(st.slash["/explain"], 1)
        self.assertIn(local_day(T0 + timedelta(hours=1)), st.feature_days["slash"])
        self.assertEqual(dict(st.copilot_versions), {"0.67.0": 1, "1.0.100-1": 1})
        self.assertEqual(dict(st.unknown_types), {"brand.new_event": 1})
        self.assertEqual((st.bad_lines, st.parse_errors), (0, 0))
        self.assertEqual(st.premium_days, {local_day(T0 + timedelta(hours=1)): 3.0})

    def test_range_outside_the_activity_counts_nothing(self):
        st = self.scanner.build_stats("2020-01-01", "2020-01-02")
        self.assertEqual((len(st.sessions), sum(st.premium_days.values()), st.no_cache_split), (0, 0, 0))
        self.assertEqual(dict(st.token_modes), {})
        self.assertEqual(sum(sum(c.values()) for c in st.surfaces.values()), 0)
        self.assertEqual((st.slash, dict(st.feature_days)), ({}, {}))

    def test_extras_are_not_in_claude_stats(self):
        st = afi.Stats()
        for attr in ("premium_days", "surfaces", "token_modes", "no_cache_split", "copilot_versions",
                     "unknown_types", "bad_lines", "no_credit_calls"):
            self.assertFalse(hasattr(st, attr), attr)

    def test_unchanged_files_not_reparsed(self):              # Review Focus 2
        self.scanner.build_stats()
        with patch("copilot_source.load_chat_session", side_effect=AssertionError("re-read")):
            self.scanner.build_stats()

    def test_unchanged_sessions_read_no_file_at_all(self):
        first = afi.stats_to_json(self.scanner.build_stats())
        boom = AssertionError("re-read")
        readers = ("load_chat_session", "read_lines", "agent_log_to_events", "chat_session_to_events",
                   "read_debug_calls", "read_otel_calls", "read_session_store", "workspace_folder")
        with patch.multiple("copilot_source", **{name: unittest.mock.DEFAULT for name in readers}) as mocks:
            for m in mocks.values():
                m.side_effect = boom
            again = afi.stats_to_json(self.scanner.build_stats())
        for d in (first, again):
            d.pop("generated")
        self.assertEqual(again, first)

    def test_a_changed_file_re_reads_only_its_session(self):
        before = self.scanner.build_stats()
        cf.add_empty_window_chat(self.ur, "e1", {"requests": [
            cf.chat_request("re", MS + 3600000, "/explain what 3x means", prompt=10, completion=5,
                            details="Claude Opus 4.5 • 3x"),
            cf.chat_request("re2", MS + 3700000, "and now 1x", prompt=20, completion=7, details="GPT • 1x")]})
        with patch("copilot_source.load_chat_session", wraps=cs.load_chat_session) as load:
            after = self.scanner.build_stats()
        self.assertEqual([os.path.normcase(c.args[0]) for c in load.call_args_list], [os.path.normcase(self.e1)])
        self.assertEqual(total(after, "output") - total(before, "output"), 7)
        self.assertEqual(sum(after.premium_days.values()), 4.0)

    def test_a_new_session_store_row_updates_branch_and_title(self):
        self.scanner.build_stats()
        db = sqlite3.connect(os.path.join(self.ur, "globalStorage", cf.GLOBAL_EXT, "session-store.db"))
        db.execute("INSERT INTO sessions (id, cwd, branch, summary) VALUES ('e1', '/srv/app', 'rel', 'Ship it')")
        db.commit(); db.close()
        st = self.scanner.build_stats()
        self.assertEqual((st.sessions["e1"]["project"], st.project_cwd["srv/app"]), ("srv/app", "/srv/app"))
        self.assertEqual(st.branches["rel"]["output"], 5)
        self.assertEqual(st.session_titles["e1"], "Ship it")

    def test_copilot_payload_extras(self):
        d = afi.build_payload(self.scanner, parse_qs("days=0"), source="copilot")
        self.assertEqual(d["copilot"]["premium_requests"], 3.0)
        self.assertEqual(d["copilot"]["surfaces"]["cli"]["sessions"], 1)
        self.assertEqual(d["cost_unit"], "credits")
        self.assertEqual(d["copilot"]["token_modes"], {"exact": 2, "approx": 1, "none": 1})
        self.assertEqual(d["copilot"]["surfaces"]["vscode"], {"prompts": 3, "output": 405, "sessions": 3})
        self.assertEqual(d["root"], afi.copilot_root_label([self.ur], self.home))
        self.assertEqual(d["totals"]["sessions"], 4)
        json.dumps(d)                                   # the payload stays JSON
        self.assertNotIn("copilot", afi.build_payload(afi.Scanner(FIX), parse_qs("days=0")))

    def test_copilot_root_label(self):
        home = os.path.expanduser("~")
        roots = [os.path.join(home, "AppData", "Roaming", "Code", "User"), home + "2" + os.sep + "x"]
        outside = os.path.join(os.path.splitdrive(home)[0] + os.sep, "opt", "User")
        self.assertEqual(afi.copilot_root_label(roots + [outside], os.path.join(home, ".copilot")),
                         " · ".join([os.path.join("~", "AppData", "Roaming", "Code", "User"),
                                     home + "2" + os.sep + "x", outside, os.path.join("~", ".copilot")]))
        self.assertEqual(afi.copilot_root_label([], None), "")


class CopilotSessionRulesTests(TmpCase):
    """One session at a time: where its credits, project, branch and surface come from."""

    def chat_with_debug(self, debug_rows, **chat_kw):
        ur = cf.make_user_root(self.tmp)
        ws = cf.add_workspace(ur, "h1", "/w")
        cf.add_chat_jsonl(ws, "c1", [{"kind": 0, "v": {"requests": [
            cf.chat_request("r1", MS, "go", prompt=1200, completion=300, **chat_kw)]}}])
        if debug_rows:
            cf.add_debug_log(ws, "c1", debug_rows)
        return afi.CopilotScanner([ur], os.path.join(self.tmp, "home")).build_stats()

    def test_exact_credits_replace_the_chat_credits(self):           # ruling 1
        st = self.chat_with_debug([cf.llm_request("a", MS + 1000, 5000, 400, 3000, nano_aiu=200000000)],
                                  credits=0.5)
        self.assertAlmostEqual(total(st, "cost"), 0.2)
        self.assertEqual((total(st, "output"), st.no_credit_calls), (400, 0))

    def test_exact_credits_on_some_calls_still_win_and_the_rest_are_missing(self):
        st = self.chat_with_debug([cf.llm_request("a", MS + 1000, 5000, 400, 3000, nano_aiu=200000000),
                                   cf.llm_request("b", MS + 2000, 100, 10, 0)], credits=0.5)
        self.assertAlmostEqual(total(st, "cost"), 0.2)
        self.assertEqual(st.no_credit_calls, 1)

    def test_chat_credits_stand_when_the_exact_calls_have_none(self):
        st = self.chat_with_debug([cf.llm_request("a", MS + 1000, 5000, 400, 3000)], credits=0.5)
        self.assertAlmostEqual(total(st, "cost"), 0.5)
        self.assertEqual((total(st, "output"), total(st, "input"), st.no_credit_calls), (400, 2000, 0))

    def test_no_credits_anywhere_counts_the_exact_calls_as_missing(self):   # ruling 2
        st = self.chat_with_debug([cf.llm_request("a", MS + 1000, 5000, 400, 3000),
                                   cf.llm_request("b", MS + 2000, 100, 10, 0)])
        self.assertEqual((total(st, "cost"), st.no_credit_calls), (0.0, 2))

    def test_premium_requests_are_not_missing_credits(self):
        st = self.chat_with_debug([], details="Claude Opus 4.5 • 3x")
        self.assertEqual((st.no_credit_calls, sum(st.premium_days.values())), (0, 3.0))
        self.assertEqual(dict(st.token_modes), {"approx": 1})
        st = self.chat_with_debug([])
        self.assertEqual((st.no_credit_calls, total(st, "cost")), (1, 0.0))

    def test_exact_credits_keep_the_chat_premium_multiplier(self):
        st = self.chat_with_debug([cf.llm_request("a", MS + 1000, 5000, 400, 3000, nano_aiu=200000000)],
                                  details="Claude Opus 4.5 • 3x")
        self.assertAlmostEqual(total(st, "cost"), 0.2)
        self.assertEqual(sum(st.premium_days.values()), 3.0)

    def test_otel_wins_over_debug_and_chat(self):
        ur = cf.make_user_root(self.tmp)
        ws = cf.add_workspace(ur, "h1", "/w")
        cf.add_chat_jsonl(ws, "c1", [{"kind": 0, "v": {"requests": [
            cf.chat_request("r1", MS, "go", prompt=1200, completion=300)]}}])
        cf.add_debug_log(ws, "c1", [cf.llm_request("a", MS + 1000, 5000, 400, 3000)])
        otel = os.path.join(self.tmp, "o.jsonl")
        cf.add_otel(otel, [cf.otel_span("c1", "s1", 900, 50, cache_read=400, nano_aiu=10 ** 8,
                                        start_ns=(MS + 1000) * 1000000)])
        cf.write_settings(ur, {"github.copilot.chat.otel.outfile": otel})
        st = afi.CopilotScanner([ur], os.path.join(self.tmp, "home")).build_stats()
        self.assertEqual((total(st, "output"), total(st, "input"), total(st, "cache_read")), (50, 500, 400))
        self.assertAlmostEqual(total(st, "cost"), 0.1)
        self.assertEqual(dict(st.token_modes), {"exact": 1})

    def test_a_store_without_tokens_is_mode_none(self):
        ur = cf.make_user_root(self.tmp)
        home = os.path.join(self.tmp, "home")
        cf.add_empty_window_chat(ur, "e1", {"requests": [cf.chat_request("r1", MS, "hi")]})   # no token counts
        cf.add_cli_session(home, "k1", CLI_EVENTS[:3])                                    # no usage record
        st = afi.CopilotScanner([ur], home).build_stats()
        self.assertEqual(dict(st.token_modes), {"none": 2})
        self.assertEqual(st.no_cache_split, 0)

    def test_cli_log_with_a_byte_order_mark_keeps_its_session_start(self):   # ruling 4
        ur = cf.make_user_root(self.tmp)
        home = os.path.join(self.tmp, "home")
        text = "\ufeff" + "".join(json.dumps(r) + "\n" for r in CLI_EVENTS) + '{"type": "user.mess'
        cf.add_raw(os.path.join(home, "session-state", "k1", "events.jsonl"), text)
        st = afi.CopilotScanner([ur], home).build_stats()
        self.assertEqual(st.sessions["k1"]["project"], "work/demo")
        self.assertEqual(st.branches["main"]["output"], 20)
        self.assertEqual(dict(st.copilot_versions), {"1.0.100-1": 1})
        self.assertEqual((dict(st.surfaces["cli"])["sessions"], st.bad_lines), (1, 1))

    def test_cli_started_from_vscode_counts_as_vscode(self):
        ur = cf.make_user_root(self.tmp)
        home = os.path.join(self.tmp, "home")
        events = json.loads(json.dumps(CLI_EVENTS))
        events[0]["data"]["context"]["hostType"] = "vscode"
        del events[0]["data"]["context"]["branch"]
        cf.add_cli_session(home, "k1", events)
        cf.add_session_store(ur, [{"id": "k1", "branch": "from-store"}])
        st = afi.CopilotScanner([ur], home).build_stats()
        self.assertEqual(st.surfaces["vscode"]["sessions"], 1)
        self.assertEqual(st.surfaces["cli"]["sessions"], 0)
        self.assertEqual(st.branches["from-store"]["output"], 20)     # no CLI branch: the store's

    def test_empty_window_chat_falls_back_to_the_store_cwd(self):
        ur = cf.make_user_root(self.tmp)
        cf.add_empty_window_chat(ur, "e1", {"requests": [cf.chat_request("r1", MS, "hi", prompt=3, completion=1)]})
        cf.add_session_store(ur, [{"id": "e1", "cwd": "E:\\god_ai\\AI_ALL"}])
        st = afi.CopilotScanner([ur], os.path.join(self.tmp, "home")).build_stats()
        self.assertEqual(st.sessions["e1"]["project"], "E/god/ai/AI/ALL")
        self.assertEqual(st.project_cwd["E/god/ai/AI/ALL"], "E:\\god_ai\\AI_ALL")

    def test_title_falls_back_to_the_first_prompt_trimmed(self):
        ur = cf.make_user_root(self.tmp)
        long = "please   refactor\nthe whole " + "x" * 80
        cf.add_empty_window_chat(ur, "e1", {"requests": [cf.chat_request("r1", MS, long)]})
        st = afi.CopilotScanner([ur], os.path.join(self.tmp, "home")).build_stats()
        self.assertEqual(st.session_titles["e1"], ("please refactor the whole " + "x" * 80)[:60])

    def test_a_broken_event_counts_as_a_parse_error(self):
        ur = cf.make_user_root(self.tmp)
        cf.add_empty_window_chat(ur, "e1", {"requests": [cf.chat_request("r1", MS, "hi", prompt=3, completion=1)]})
        sc = afi.CopilotScanner([ur], os.path.join(self.tmp, "home"))
        real = afi.parse_entry

        def flaky(e, *a, **kw):
            if e.get("type") == "user":
                raise ValueError("format drift")
            return real(e, *a, **kw)
        with patch.object(afi, "parse_entry", side_effect=flaky):
            st = sc.build_stats()
        self.assertEqual((st.parse_errors, total(st, "output")), (1, 1))


HEALTH_IDS = ["chat", "tokens", "exact", "credits", "stopped", "cli", "projects", "branches", "titles", "time"]
COPILOT_FEATURE_IDS = ["memory", "verify", "agent", "plan", "subagents", "tasks", "mcp", "slash", "web"]
SETUP_HINT = "Turn on with: python ai_fluency_insights.py --setup-copilot"
DEBUG_SETTING = "github.copilot.chat.agentDebugLog.fileLogging.enabled"       # the setting that writes the debug log


def hand_stats(sessions=0, exact=0, approx=0, none=0, calls=0, missing=0, cli=0):
    """A Copilot Stats with only the counters Data Health reads set by hand."""
    st = afi.copilot_stats()
    st.sessions = {"s%d" % i: {} for i in range(sessions)}
    st.token_modes.update({"exact": exact, "approx": approx, "none": none})
    st.hc["assistant_msgs"], st.no_credit_calls = calls, missing
    st.surfaces["cli"]["sessions"] = cli
    return st


NO_FILES = {"chat": {}, "transcripts": {}, "cli": {}, "debug": {}, "otel": [], "stores": [], "settings": []}


class CopilotHealthTests(CopilotTreeCase):
    """build_health_copilot / copilot_na: what the Copilot logs on this machine contain (Task 9)."""

    def health(self):
        return afi.build_health_copilot(self.scanner.build_stats(), self.scanner.files())

    def test_copilot_health_and_na(self):
        d = afi.build_payload(self.scanner, parse_qs("days=0"), source="copilot")
        caps = {c["id"]: c["status"] for c in d["health"]["capabilities"]}
        self.assertEqual((caps["chat"], caps["exact"], caps["cli"]), ("ok", "partial", "ok"))
        self.assertIn("cold_start_pct", d["na"]); self.assertEqual(afi.build_payload(afi.Scanner(FIX), parse_qs("days=0"))["na"], {})

    def test_capabilities_on_the_fixture_tree(self):
        h = self.health()
        self.assertEqual([c["id"] for c in h["capabilities"]], HEALTH_IDS)
        self.assertEqual({c["id"]: c["status"] for c in h["capabilities"]},
                         {"chat": "ok", "tokens": "partial",       # 3 of 4 sessions have any usage
                          "exact": "partial",                      # 2 of 4 are exact
                          "credits": "ok",                         # the premium-billed e1 is not missing credits
                          "stopped": "missing",                    # these requests carry no modelState
                          "cli": "ok", "projects": "partial",      # e1 has no folder
                          "branches": "partial", "titles": "ok", "time": "ok"})
        for c in h["capabilities"]:
            self.assertEqual(sorted(c), ["detail", "id", "label", "status", "tab"])
            self.assertTrue(c["label"] and c["detail"], c["id"])
        chat = next(c for c in h["capabilities"] if c["id"] == "chat")
        for part in ("4 sessions", "2 VS Code chat", "1 transcript", "1 Copilot CLI"):
            self.assertIn(part, chat["detail"])

    def test_health_has_everything_the_claude_one_has(self):
        h = self.health()
        self.assertLessEqual(set(afi.build_health(afi.Stats())), set(h))     # the Data Health page reads these keys
        self.assertEqual((h["sessions"], h["parse_errors"], h["bad_lines"]), (4, 0, 0))
        self.assertEqual({v["version"]: v["n"] for v in h["versions"]}, {"0.67.0": 1, "1.0.100-1": 1})
        self.assertEqual(h["unknown_types"], [{"type": "brand.new_event", "n": 1}])
        self.assertEqual({e["name"]: e["n"] for e in h["entrypoints"]}, {"VS Code": 3, "Copilot CLI": 1})
        self.assertTrue(h["entry_types"])
        json.dumps(h)

    def test_unknown_types_reach_health(self):                 # Review Focus 5
        d = afi.build_payload(self.scanner, parse_qs("days=0"), source="copilot")
        self.assertEqual(d["health"]["unknown_types"], [{"type": "brand.new_event", "n": 1}])

    def test_health_is_over_full_history_whatever_the_range(self):
        d = afi.build_payload(self.scanner, parse_qs("from=2020-01-01&to=2020-01-02"), source="copilot")
        self.assertEqual(d["health"]["sessions"], 4)
        self.assertEqual(d["health"]["unknown_types"], [{"type": "brand.new_event", "n": 1}])

    def test_four_copilot_limits(self):
        limits = self.health()["limits"]
        self.assertEqual(len(limits), 4)
        for needle, text in zip(("inline", "last", "credits", "not a task"), limits):
            self.assertIn(needle, text.lower())
        self.assertNotIn("Claude", " ".join(limits))

    def test_exact_status_boundaries_and_setup_hint(self):
        def exact(**kw):
            h = afi.build_health_copilot(hand_stats(**kw), NO_FILES)
            return next(c for c in h["capabilities"] if c["id"] == "exact")
        for kw, status in (({"sessions": 10, "exact": 9, "approx": 1}, "ok"),       # 90% covers it
                           ({"sessions": 10, "exact": 8, "approx": 2}, "partial"),
                           ({"sessions": 10, "exact": 1, "approx": 9}, "partial"),
                           ({"sessions": 10, "approx": 10}, "missing"),
                           ({"sessions": 0}, "missing")):
            c = exact(**kw)
            self.assertEqual(c["status"], status, kw)
            self.assertEqual(SETUP_HINT in c["detail"], status != "ok", kw)

    def test_tokens_status_counts_sessions_with_any_usage(self):
        def tokens(**kw):
            h = afi.build_health_copilot(hand_stats(**kw), NO_FILES)
            return next(c["status"] for c in h["capabilities"] if c["id"] == "tokens")
        self.assertEqual(tokens(sessions=10, exact=3, approx=6, none=1), "ok")
        self.assertEqual(tokens(sessions=10, exact=3, approx=2, none=5), "partial")
        self.assertEqual(tokens(sessions=10, none=10), "missing")
        self.assertEqual(tokens(sessions=0), "missing")

    def test_credits_status_uses_the_calls_without_credits(self):       # ruling 2
        def credits(**kw):
            h = afi.build_health_copilot(hand_stats(**kw), NO_FILES)
            return next(c["status"] for c in h["capabilities"] if c["id"] == "credits")
        self.assertEqual(credits(sessions=2, calls=5, missing=0), "ok")
        self.assertEqual(credits(sessions=2, calls=5, missing=2), "partial")
        self.assertEqual(credits(sessions=2, calls=5, missing=5), "missing")
        self.assertEqual(credits(sessions=2, calls=0, missing=0), "missing")       # no calls: nothing carries credits

    def test_credits_missing_when_the_logs_carry_none(self):
        ur = cf.make_user_root(self.tmp, "CodeNoCredits")
        ws = cf.add_workspace(ur, "h1", "/w")
        cf.add_chat_jsonl(ws, "c1", [{"kind": 0, "v": {"requests": [cf.chat_request("r1", MS, "go")]}}])
        cf.add_debug_log(ws, "c1", [cf.llm_request("a", MS + 1000, 5000, 400, 3000),
                                    cf.llm_request("b", MS + 2000, 100, 10, 0)])
        sc = afi.CopilotScanner([ur], os.path.join(self.tmp, "nohome"))
        h = afi.build_health_copilot(sc.build_stats(), sc.files())
        self.assertEqual(next(c["status"] for c in h["capabilities"] if c["id"] == "credits"), "missing")

    def test_cli_capability(self):
        def cli(**kw):
            h = afi.build_health_copilot(hand_stats(**kw), NO_FILES)
            return next(c for c in h["capabilities"] if c["id"] == "cli")
        self.assertEqual(cli(sessions=3, cli=2)["status"], "ok")
        self.assertIn("2 Copilot CLI sessions", cli(sessions=3, cli=2)["detail"])
        self.assertEqual(cli(sessions=3, cli=0)["status"], "missing")

    def test_empty_stats_never_raise_and_nothing_is_ok(self):
        h = afi.build_health_copilot(afi.copilot_stats(), NO_FILES)
        self.assertEqual([c["id"] for c in h["capabilities"]], HEALTH_IDS)
        self.assertEqual({c["status"] for c in h["capabilities"]}, {"missing"})
        self.assertEqual((h["sessions"], h["versions"], h["unknown_types"], h["bad_lines"]), (0, [], [], 0))

    def test_a_claude_shaped_stats_is_accepted_too(self):
        # a Stats without the Copilot-only counters must not crash the builder
        h = afi.build_health_copilot(afi.Stats(), NO_FILES)
        self.assertEqual(len(h["capabilities"]), len(HEALTH_IDS))

    def test_na_always_names_the_three_gaps(self):
        always = {"cold_start_pct": "Copilot bills per request, so there is no cache set-up cost to measure.",
                  "interruptions": "Your Copilot logs don't record stopped requests (older VS Code).",
                  "permission_modes": "Copilot doesn't log approval modes."}
        for status in ("ok", "partial"):
            self.assertEqual(afi.copilot_na({"capabilities": [{"id": "exact", "status": status}]}), always)
        for status in ("ok", "partial"):            # a VS Code that records stopped requests: interruptions are real
            na = afi.copilot_na({"capabilities": [{"id": "stopped", "status": status}]})
            self.assertEqual(sorted(na), ["cold_start_pct", "permission_modes"])
        self.assertEqual(afi.copilot_na({"capabilities": []}), always)
        na = afi.copilot_na({"capabilities": [{"id": "exact", "status": "missing"}]})
        self.assertEqual(na, dict(always, cache_read="Copilot's chat history has no cache split; turn on exact tokens "
                                                      "(--setup-copilot, or the VS Code setting " + DEBUG_SETTING + ")."))

    def test_na_in_the_payload(self):
        d = afi.build_payload(self.scanner, parse_qs("days=0"), source="copilot")
        self.assertEqual(set(d["na"]), {"cold_start_pct", "interruptions", "permission_modes"})   # exact is partial
        ur = cf.make_user_root(self.tmp, "CodeApproxOnly")
        cf.add_empty_window_chat(ur, "e1", {"requests": [cf.chat_request("r1", MS, "hi", prompt=3, completion=1)]})
        sc = afi.CopilotScanner([ur], os.path.join(self.tmp, "nohome"))
        d = afi.build_payload(sc, parse_qs("days=0"), source="copilot")      # approx tokens only: exact is missing
        self.assertEqual(set(d["na"]), {"cold_start_pct", "interruptions", "permission_modes", "cache_read"})
        self.assertIn("--setup-copilot", d["na"]["cache_read"])

    def test_claude_payload_keeps_its_own_health_and_an_empty_na(self):
        d = afi.build_payload(afi.Scanner(FIX), parse_qs("days=0"))
        self.assertEqual(d["na"], {})
        self.assertEqual(d["health"]["capabilities"][0]["id"], "tokens")
        self.assertNotIn("unknown_types", d["health"])


def tool_round(ts_ms, name, args):
    return {"timestamp": ts_ms, "modelId": "copilot/gpt-5.6-luna",
            "toolCalls": [{"id": "%s-%d" % (name, ts_ms), "name": name, "arguments": json.dumps(args)}]}


class CopilotAdoptionTests(CopilotTreeCase):
    """My Adoption over Copilot's own feature list, instructions files and advisor wording (Task 9)."""

    DAY_MS = 24 * 3600 * 1000

    projects_made = 0

    def active_project(self, instruction_files, days=4):
        """A scanner over a new project folder holding `instruction_files` (relative paths), with an
        agent-mode chat on each of `days` days that ran a terminal command: enough days for My Adoption.
        Each call makes its own folder and VS Code user root; self.proj is the folder."""
        self.projects_made += 1
        tag = str(self.projects_made)
        proj = self.proj = os.path.join(self.tmp, "proj_" + tag)
        for rel in instruction_files:
            path = os.path.join(proj, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# notes\n")
        os.makedirs(proj, exist_ok=True)
        ur = cf.make_user_root(self.tmp, "Code_" + tag)
        ws = cf.add_workspace(ur, "h9", proj)
        for i in range(days):
            ts = MS - (days - 1 - i) * self.DAY_MS
            cf.add_chat_jsonl(ws, "p%d" % i, [{"kind": 0, "v": {
                "sessionId": "p%d" % i, "inputState": {"mode": {"kind": "agent"}},
                "requests": [cf.chat_request(
                    "r%d" % i, ts, "run the tests and fix what fails", prompt=900, completion=120, credits=0.2,
                    rounds=[tool_round(ts + 1000, "run_in_terminal", {"command": "python -m unittest"}),
                            tool_round(ts + 2000, "create_file", {"filePath": "a.py", "content": "x\ny\n"})])]}}])
        return afi.CopilotScanner([ur], os.path.join(self.tmp, "nohome"))

    def test_feature_table(self):
        feats = afi.COPILOT_ADOPTION_FEATURES
        self.assertEqual([f[0] for f in feats], COPILOT_FEATURE_IDS)
        self.assertEqual([f[4] for f in feats], [58, 60, 55, 50, 45, 38, 42, 40, 35])
        self.assertEqual(feats[0][1], "Custom instructions (.github/copilot-instructions.md or AGENTS.md)")
        self.assertEqual([f[1] for f in feats[1:]], ["Run & verify", "Agent mode", "Plan mode", "Subagents", "Todo lists",
                                                    "Connected tools (MCP)", "Prompt files & slash commands", "Web fetch"])
        for f in feats:                                   # the same tuple shape as the Claude list
            self.assertEqual(len(f), 5)
            self.assertTrue(all(isinstance(x, str) and x for x in f[:4]) and isinstance(f[4], int))
            self.assertNotIn("Claude", " ".join(f[:4]))
        self.assertEqual(len(afi.ADOPTION_FEATURES[0]), 5)

    def test_copilot_adoption_uses_copilot_features(self):
        st = self.scanner.build_stats()
        ad = afi.build_adoption(st, features=afi.COPILOT_ADOPTION_FEATURES,
                                instructions=afi.SOURCES["copilot"]["instructions"])
        if ad["ready"]:                         # fixture may have too few active days for a full window
            ids = {f["id"] for f in ad["range"]["features"]}
            self.assertEqual(ids, {"memory", "verify", "agent", "plan", "subagents", "tasks", "mcp", "slash", "web"})

    def test_ready_copilot_adoption_lists_the_copilot_features(self):
        st = self.active_project([".github/copilot-instructions.md"]).build_stats()
        ad = afi.build_adoption(st, afi.COPILOT_ADOPTION_FEATURES, afi.SOURCES["copilot"]["instructions"])
        self.assertTrue(ad["ready"])                      # so the checks below really run
        rows = {f["id"]: f for f in ad["range"]["features"]}
        self.assertEqual([f["id"] for f in ad["range"]["features"]], COPILOT_FEATURE_IDS)
        self.assertEqual(ad["range"]["total"], 9)
        self.assertEqual((rows["memory"]["recent"], rows["memory"]["ever"]), (1, True))
        self.assertGreater(rows["verify"]["recent"], 0)
        self.assertGreater(rows["agent"]["recent"], 0)
        self.assertEqual((rows["plan"]["recent"], rows["plan"]["ever"], rows["plan"]["first"]), (0, False, None))
        self.assertEqual(ad["range"]["used"], 3)          # memory, verify, agent
        picked = {c["feature"] for c in ad["coach"] if c["feature"]}
        self.assertTrue(picked and picked <= set(COPILOT_FEATURE_IDS), picked)
        self.assertTrue(all(isinstance(c["how"], str) for c in ad["coach"]))

    def test_default_arguments_are_the_claude_features(self):
        st = self.active_project([]).build_stats()
        ad = afi.build_adoption(st)
        self.assertTrue(ad["ready"])
        self.assertEqual([f["id"] for f in ad["range"]["features"]], [f[0] for f in afi.ADOPTION_FEATURES])
        self.assertEqual(ad["range"]["total"], len(afi.ADOPTION_FEATURES))
        self.assertEqual(afi.build_adoption(st, afi.ADOPTION_FEATURES, ("CLAUDE.md", "AGENTS.md")), ad)

    def test_memory_checks_the_instructions_files_given(self):
        def memory(files, instructions):
            st = self.active_project(files).build_stats()
            ad = afi.build_adoption(st, afi.COPILOT_ADOPTION_FEATURES, instructions)
            return next(f["recent"] for f in ad["range"]["features"] if f["id"] == "memory")
        copilot, claude = afi.SOURCES["copilot"]["instructions"], afi.SOURCES["claude"]["instructions"]
        self.assertEqual(memory([".github/copilot-instructions.md"], copilot), 1)
        self.assertEqual(memory(["AGENTS.md"], copilot), 1)
        self.assertEqual(memory(["CLAUDE.md"], copilot), 0)             # CLAUDE.md means nothing to Copilot
        self.assertEqual(memory([".github/copilot-instructions.md"], claude), 0)
        self.assertEqual(memory(["CLAUDE.md"], claude), 1)
        self.assertEqual(memory([], copilot), 0)

    def test_copilot_project_instructions_files(self):
        # a project folder holding .github/copilot-instructions.md is has_instructions=True on Copilot
        sc = self.active_project([".github/copilot-instructions.md"])
        d = afi.build_payload(sc, parse_qs("days=0"), source="copilot")
        self.assertEqual([(p["name"], p["has_instructions"]) for p in d["projects"]],
                         [(cs.project_label(self.proj), True)])
        # ... while CLAUDE.md alone is not one for Copilot, and AGENTS.md is for both
        for files, copilot, claude in (([".github/copilot-instructions.md"], True, False), (["CLAUDE.md"], False, True),
                                       (["AGENTS.md"], True, True), ([], False, False)):
            st = self.active_project(files).build_stats()

            def got(*args):
                return [p["has_instructions"] for p in afi.stats_to_json(st, None, *args)["projects"]]
            self.assertEqual(got(afi.SOURCES["copilot"]["instructions"]), [copilot], files)
            self.assertEqual(got(), [claude], files)                                 # the default is Claude's
            self.assertEqual(got(("CLAUDE.md", "AGENTS.md")), [claude], files)

    def test_retool_text(self):                                                         # ruling R1
        copilot, claude = afi.SOURCES["copilot"], afi.SOURCES["claude"]
        self.assertEqual(
            afi.retool_text({"t": "Put your test command in CLAUDE.md so Claude Code runs it, then ask Claude"}, copilot),
            {"t": "Put your test command in .github/copilot-instructions.md so GitHub Copilot runs it, then ask Copilot"})
        src = {"a": ["Claude wrote it", {"b": "Add a CLAUDE.md"}, 3, None, 1.5, True], "n": 7, "Claude": "Claude Code's"}
        before = json.dumps(src, sort_keys=True)
        out = afi.retool_text(src, copilot)
        self.assertEqual(out, {"a": ["Copilot wrote it", {"b": "Add a .github/copilot-instructions.md"}, 3, None, 1.5, True],
                               "n": 7, "Claude": "GitHub Copilot's"})              # values only; keys are kept
        self.assertEqual(json.dumps(src, sort_keys=True), before)                  # a deep copy, the input is untouched
        self.assertIsNot(out["a"], src["a"]); self.assertIsNot(out["a"][1], src["a"][1])
        self.assertEqual(afi.retool_text("Claude Code and claude-opus", copilot), "GitHub Copilot and claude-opus")
        self.assertEqual(afi.retool_text(src, claude), src)                         # Claude -> Claude changes nothing
        self.assertEqual(afi.retool_text(["x", ("Claude",)], copilot), ["x", ("Copilot",)])

    def spike_stats(self):
        """Six costed days, the last ten times the others: one "spike" rec."""
        st = afi.Stats()
        for i in range(6):
            day = "2026-10-%02d" % (i + 1)
            st.days[day]["messages"] = 1
            st.days[day]["cost"] = 10.0 if i == 5 else 1.0
        return st

    def rec_ids(self, advisor):
        return [r["id"] for r in advisor["recs"] + advisor["more"]]

    def test_advisor_drops_the_recs_in_skip_recs(self):
        st, ad = self.spike_stats(), {"end": "2026-10-06"}
        self.assertIn("spike", self.rec_ids(afi.build_advisor(st, ad)))
        self.assertEqual(afi.build_advisor(st, ad, afi.SOURCES["claude"]), afi.build_advisor(st, ad))
        self.assertIn("spike", self.rec_ids(afi.build_advisor(st, ad, afi.SOURCES["copilot"])))   # not in its list
        tool = dict(afi.SOURCES["claude"], skip_recs=("spike",))
        self.assertNotIn("spike", self.rec_ids(afi.build_advisor(st, ad, tool)))

    def test_copilot_skips_the_cache_recs(self):
        st, ad = self.spike_stats(), {"end": "2026-10-06"}
        cache_heavy = dict(afi.build_value(st, "2026-09-09", "2026-10-06"),
                           cold_start_pct=60.0, sessions=10, cache_reset_pct=20.0)
        with patch.object(afi, "build_value", return_value=cache_heavy):
            claude = self.rec_ids(afi.build_advisor(st, ad))
            copilot = self.rec_ids(afi.build_advisor(st, ad, afi.SOURCES["copilot"]))
        self.assertTrue({"coldstart", "cachereset"} <= set(claude), claude)
        self.assertFalse({"coldstart", "cachereset"} & set(copilot), copilot)
        self.assertIn("spike", copilot)

    def test_advisor_skip_happens_before_the_top_five_cut(self):
        st = afi.Scanner(FIX).build_stats()
        ad = {"end": "2026-10-10", "coach": [
            {"area": "range", "feature": "f%d" % i, "severity": 80 - i, "title": "t", "why": "w", "how": "h"}
            for i in range(8)]}
        self.assertEqual(len(afi.build_advisor(st, ad)["recs"]), 5)
        out = afi.build_advisor(st, ad, dict(afi.SOURCES["claude"], skip_recs=("adopt_f0", "adopt_f1")))
        self.assertFalse({"adopt_f0", "adopt_f1"} & set(self.rec_ids(out)))
        self.assertEqual(len(out["recs"]), 5)                       # the next ones moved up
        self.assertEqual([r["rank"] for r in out["recs"]], [1, 2, 3, 4, 5])
        self.assertEqual(out["recs"][0]["id"], "adopt_f2")

    def test_advisor_text_names_copilot(self):
        d = afi.build_payload(self.scanner, parse_qs("days=0"), source="copilot")
        self.assertNotIn("Claude", json.dumps(d["advisor"]))

    def test_ready_advisor_text_names_copilot_and_skips_cache_recs(self):
        d = afi.build_payload(self.active_project([]), parse_qs("days=0"), source="copilot")
        self.assertTrue(d["adoption"]["ready"] and d["advisor"]["ready"])
        text = json.dumps(d["advisor"])
        self.assertNotIn("Claude", text)
        self.assertNotIn("CLAUDE", text)
        ids = self.rec_ids(d["advisor"])
        self.assertFalse({"coldstart", "cachereset"} & set(ids))
        self.assertTrue(any(i.startswith("adopt_") for i in ids), ids)       # the coach tips reach the advisor
        self.assertIn("Copilot", text)
        c = afi.build_payload(afi.Scanner(FIX), parse_qs("days=0"))          # Claude's own advisor still says Claude
        self.assertIn("Claude", json.dumps(c["advisor"]))

    def test_advisor_money_follows_the_cost_unit(self):
        st, ad = self.spike_stats(), {"end": "2026-10-06"}

        def evidence(tool):
            adv = afi.build_advisor(st, ad, tool)
            return next(r for r in adv["recs"] + adv["more"] if r["id"] == "spike")["evidence"]
        self.assertIn("$10.00 was", evidence(afi.SOURCES["claude"]))
        credits = evidence(afi.SOURCES["copilot"])
        self.assertIn("10.00 credits was", credits)
        self.assertNotIn("$", credits)

    # --- fix round 1: templates are retooled before user data goes in ---------------------------

    def tooled(self, flow=None, quality=None, value=None):
        """build_advisor over an empty Stats whose Flow / Quality / Value dicts are the real ones plus these
        overrides, so any rec can be provoked. Returns a function tool -> {rec id: rec}."""
        st, ad = afi.Stats(), {"end": "2026-10-06"}
        real = {"build_flow": afi.build_flow, "build_quality": afi.build_quality, "build_value": afi.build_value}
        over = {"build_flow": flow or {}, "build_quality": quality or {}, "build_value": value or {}}

        def patched(name):
            return lambda s, d0=None, d1=None: dict(real[name](s, d0, d1), **over[name])

        def run(tool):
            with patch.multiple(afi, **{n: patched(n) for n in real}):
                adv = afi.build_advisor(st, ad, tool)
            return {r["id"]: r for r in adv["recs"] + adv["more"]}
        return run

    def test_a_quoted_session_title_and_file_name_are_not_retooled(self):
        run = self.tooled(
            value={"spikes": [{"day": "2026-10-06", "cost": 10.0, "x_typical": 5.0,
                               "session": "Fix Claude CLAUDE.md bug", "session_cost": 8.0}]},
            quality={"reedit_pct": 50, "edits": 40, "hotspots": [{"file": "CLAUDE.md"}]})
        recs = run(afi.SOURCES["copilot"])
        self.assertIn('biggest session: "Fix Claude CLAUDE.md bug"', recs["spike"]["evidence"])
        self.assertIn('most reworked: "CLAUDE.md"', recs["rework"]["evidence"])
        self.assertTrue(recs["rework"]["action"].startswith("Give Copilot the full requirement"))
        self.assertIn("before Copilot edits", recs["rework"]["experiment"]["label"])
        claude = run(afi.SOURCES["claude"])                                        # Claude: exactly as before
        self.assertIn('biggest session: "Fix Claude CLAUDE.md bug"', claude["spike"]["evidence"])
        self.assertTrue(claude["rework"]["action"].startswith("Give Claude the full requirement"))

    def test_the_clean_rec_has_no_interrupted_clause_on_copilot(self):
        run = self.tooled(flow={"turns": 10, "clean_pct": 40, "corrected": 2, "interrupted": 0, "with_errors": 3})
        copilot = run(afi.SOURCES["copilot"])["clean"]["evidence"]
        self.assertEqual(copilot, "40% of turns were clean first tries: 2 needed a correction and 3 hit tool errors (of 10 turns).")
        self.assertNotIn("interrupt", copilot)
        self.assertEqual(run(afi.SOURCES["claude"])["clean"]["evidence"],
                         "40% of turns were clean first tries: 2 needed a correction, 0 were interrupted and 3 hit tool errors (of 10 turns).")

    def test_tips_that_name_claude_only_features_are_tool_aware(self):
        run = self.tooled(flow={"sessions": 6, "session_p90_min": 200, "session_median_min": 100},
                          quality={"safety": {"force_push": 2}})
        copilot, claude = run(afi.SOURCES["copilot"]), run(afi.SOURCES["claude"])
        self.assertEqual(claude["longsess"]["action"],
                         "Use /clear between unrelated tasks, and /compact when one long task has to continue.")
        self.assertEqual(claude["risky"]["action"],
                         "Add deny rules in your Claude Code permission settings for the ones you never want to run unattended.")
        self.assertEqual(copilot["longsess"]["action"], "Start a new chat for each task.")
        self.assertIn("terminal auto-approve settings", copilot["risky"]["action"])
        self.assertIn("Copilot", copilot["risky"]["action"])
        self.assertNotIn("Claude", json.dumps(copilot))

    def test_verify_tip_names_the_copilot_instructions_file(self):
        run = self.tooled(quality={"edited_sessions": 5, "verified_pct": 20, "verified": 1})
        tip = run(afi.SOURCES["copilot"])["verify"]
        self.assertEqual(tip["action"], 'End prompts with "then run the tests", or put your test command in '
                                        ".github/copilot-instructions.md so Copilot runs it on its own.")
        self.assertEqual(tip["title"], "Make Copilot check its own work")

    def test_retooling_claude_templates_for_claude_changes_nothing(self):
        claude = afi.SOURCES["claude"]
        texts = list(afi.FLUENCY_TIPS.values()) + [d for _f, _n, d in afi.ADOPTION_LEVELS]
        texts += [f[i] for f in afi.ADOPTION_FEATURES for i in (1, 2, 3)]
        for text in texts:
            self.assertEqual(afi.retool_text(text, claude), text)

    def test_registry_flags_for_the_advisor(self):
        self.assertFalse(afi.SOURCES["claude"].get("no_interruptions"))
        self.assertTrue(afi.SOURCES["copilot"]["no_interruptions"])
        self.assertEqual(sorted(afi.SOURCES["copilot"]["rec_actions"]), ["longsess", "risky"])
        self.assertNotIn("rec_actions", afi.SOURCES["claude"])

    def quiet_cur(self, **over):
        """The bits of a pillar-score dict _coach reads, with nothing used and every pillar low."""
        fluency = {"delegation": 10, "description": 20, "discernment": 30, "diligence": 40}
        cur = {"feats": {}, "habit": 10, "impact": 10, "fluency": fluency, "code_day_share": 0.1}
        cur.update(over)
        return cur

    def test_coach_text_follows_the_tool(self):
        w = {"active_days": 5}
        cur = self.quiet_cur()
        copilot, claude = afi.SOURCES["copilot"], afi.SOURCES["claude"]
        picks = {}
        for late in (50, 0):                      # with the evenings tip (drops impact), without it
            for tool in (copilot, claude):
                picks[(late, tool["short"])] = afi._coach(afi.Stats(), cur, w, {"late_pct": late}, {}, [], tool)
        text = json.dumps(picks[(50, "Copilot")] + picks[(0, "Copilot")])
        self.assertNotIn("Claude", text)
        self.assertIn("Copilot will still be there tomorrow.", text)
        self.assertIn("give it to Copilot", text)
        self.assertIn("Aim Copilot at real changes", text)
        self.assertIn("Copilot wrote code on 10% of your active days.", text)
        self.assertIn("handing Copilot one concrete change", text)
        self.assertIn("Try handing Copilot whole tasks", text)                    # FLUENCY_TIPS
        claude_text = json.dumps(picks[(50, "Claude")] + picks[(0, "Claude")])
        for phrase in ("Claude will still be there tomorrow.", "give it to Claude.", "Aim Claude at real changes",
                       "Claude wrote code on 10% of your active days.", "handing Claude one concrete change",
                       "Try handing Claude whole tasks"):
            self.assertIn(phrase, claude_text)

    def test_memory_coach_names_the_instructions_file(self):
        cur = self.quiet_cur(feats={"memory": 0})
        features = [f for f in afi.COPILOT_ADOPTION_FEATURES if f[0] == "memory"]
        out = afi._coach(afi.Stats(), cur, {"active_days": 5}, {"late_pct": 0}, {}, features, afi.SOURCES["copilot"])
        memory = next(c for c in out if c["feature"] == "memory")
        self.assertEqual(memory["title"], "Add a .github/copilot-instructions.md")
        self.assertEqual(memory["why"], "You haven't tried custom instructions yet.")
        self.assertIn(".github/copilot-instructions.md", memory["how"])
        claude = afi._coach(afi.Stats(), cur, {"active_days": 5}, {"late_pct": 0}, {},
                            [f for f in afi.ADOPTION_FEATURES if f[0] == "memory"])
        self.assertEqual(next(c["title"] for c in claude if c["feature"] == "memory"), "Add a CLAUDE.md")

    def test_levels_follow_the_tool(self):
        copilot = afi.SOURCES["copilot"]
        self.assertEqual(afi.adoption_level(10, tool=copilot)["desc"], "You're getting to know GitHub Copilot.")
        self.assertEqual(afi.adoption_level(10)["desc"], "You're getting to know Claude Code.")
        for floor, _name, _desc in afi.ADOPTION_LEVELS:
            self.assertNotIn("Claude", afi.adoption_level(floor, tool=copilot)["desc"])

    def test_funnel_and_highlights_follow_the_tool(self):
        cur = {"n_used": 0, "quality_d": {"edited_sessions": 0, "verified_pct": None}}
        days = ["2026-10-01", "2026-10-02"]
        copilot, claude = afi.SOURCES["copilot"], afi.SOURCES["claude"]

        def detail(tool):
            return next(f["detail"] for f in afi._funnel(afi.Stats(), days, cur, {}, tool) if f["id"] == "outcome")
        self.assertEqual(detail(copilot), "Needs 3+ sessions where Copilot edited files")
        self.assertEqual(detail(claude), "Needs 3+ sessions where Claude edited files")
        rows = [{"first": "2026-10-05", "label": "Connected tools (MCP)"}]
        w = {"start": "2026-10-01", "end": "2026-10-28"}
        out = afi._highlights({}, None, w, w, rows, days, {})
        self.assertEqual(out, [{"kind": "new", "text": "First time using connected tools (2026-10-05)"}])

    def test_copilot_adoption_payload_never_names_claude(self):
        d = afi.build_payload(self.active_project([]), parse_qs("days=0"), source="copilot")
        ad = d["adoption"]
        self.assertTrue(ad["ready"])
        text = json.dumps(ad)
        self.assertNotIn("Claude", text)
        self.assertNotIn("CLAUDE", text)
        memory = next(c for c in ad["coach"] if c["feature"] == "memory")      # this project has no instructions file
        self.assertEqual(memory["title"], "Add a .github/copilot-instructions.md")
        self.assertEqual(ad["level"]["desc"],
                         afi.retool_text(afi.ADOPTION_LEVELS[ad["level"]["index"]][2], afi.SOURCES["copilot"]))
        self.assertEqual({p["id"]: p["blurb"] for p in ad["pillars"]}["range"], "How much of GitHub Copilot you use")
        # the advisor shows the same coach item with the same words
        adv = {r["id"]: r for r in d["advisor"]["recs"] + d["advisor"]["more"]}
        self.assertEqual(adv["adopt_memory"]["title"], memory["title"])
        self.assertEqual(adv["adopt_memory"]["action"], memory["how"])

    def test_discernment_detail_does_not_claim_interruptions_for_copilot(self):
        ad = afi.build_payload(self.active_project([]), parse_qs("days=0"), source="copilot")["adoption"]
        detail = next(c["detail"] for c in ad["craft"] if c["id"] == "discernment")
        self.assertRegex(detail, r"^\d+ corrections; \d+ tool errors$")
        st = self.active_project([]).build_stats()
        claude = afi.build_adoption(st)                                            # the default tool is Claude
        self.assertRegex(next(c["detail"] for c in claude["craft"] if c["id"] == "discernment"),
                         r"^\d+ corrections and interruptions; \d+ tool errors$")


class CopilotCatalogTests(CopilotTreeCase):
    """The Data Health metric catalog names the selected tool (Task 10)."""

    def test_copilot_catalog_names_no_claude(self):
        cat = afi.build_payload(self.scanner, parse_qs("days=0"), source="copilot")["catalog"]
        self.assertEqual(set(cat), set(afi.METRIC_CATALOG_BY_ID))        # same metrics, only the words change
        self.assertNotIn("Claude", json.dumps(cat))
        self.assertIn("Copilot", cat["turn_time"]["how"])               # "Claude finishing" -> "Copilot finishing"

    def test_claude_catalog_is_unchanged(self):
        cat = afi.build_payload(afi.Scanner(FIX), parse_qs("days=0"))["catalog"]
        self.assertEqual(cat, afi.catalog_payload())
        self.assertIn("Claude", json.dumps(cat))
        self.assertEqual(afi.catalog_for("claude", afi.SOURCES["claude"]), afi.catalog_payload())
        for mid in ("cost", "cost_100_loc", "cost_verified", "cache_savings", "cold_start"):
            self.assertEqual(cat[mid]["confidence"], "estimated", mid)       # dollars at list prices, as before
        self.assertIn("PRICING", cat["cost"]["how"])

    def test_copilot_credit_metrics_are_observed_or_derived_not_priced(self):
        cat = afi.build_payload(self.scanner, parse_qs("days=0"), source="copilot")["catalog"]
        self.assertEqual({m: cat[m]["confidence"] for m in ("cost", "cost_100_loc", "cost_verified", "model_mix")},
                         {"cost": "observed", "cost_100_loc": "derived", "cost_verified": "derived", "model_mix": "derived"})
        for mid in ("cost", "cost_100_loc", "cost_verified", "model_mix"):
            text = " ".join((cat[mid]["name"], cat[mid]["how"], cat[mid]["limit"]))
            self.assertNotRegex(text, r"PRICING|list price|\$|[Ee]stimated|subscription", mid)
            self.assertRegex(text, r"[Cc]redits", mid)
        self.assertIn("AI credits as Copilot's logs report them", cat["cost"]["how"])
        self.assertIn("Nothing is priced from model rates", cat["cost"]["how"])

    def test_copilot_cache_price_metrics_are_marked_not_available(self):
        cat = afi.build_payload(self.scanner, parse_qs("days=0"), source="copilot")["catalog"]
        for mid in ("cache_savings", "cold_start"):
            self.assertEqual(cat[mid]["confidence"], "na", mid)
            self.assertTrue(cat[mid]["how"].startswith("Not available for Copilot"), mid)
            self.assertIn("per request", cat[mid]["how"] + cat[mid]["limit"], mid)

    def test_copilot_catalog_overrides_only_touch_value_metrics_and_known_fields(self):
        base = afi.catalog_payload()
        cat = afi.catalog_for("copilot", afi.SOURCES["copilot"])
        self.assertEqual(set(cat), set(base))
        self.assertLessEqual(set(afi.COPILOT_CATALOG), set(base))
        for mid, over in afi.COPILOT_CATALOG.items():
            self.assertEqual(base[mid]["tab"], "value")
            self.assertLessEqual(set(over), {"name", "confidence", "how", "limit"})
        for mid in set(base) - set(afi.COPILOT_CATALOG):                    # the rest is only retooled
            self.assertEqual(cat[mid], afi.retool_text(base[mid], afi.SOURCES["copilot"]), mid)


class CopilotStoppedRequestTests(TmpCase):
    """Interruptions for Copilot come from requests[].modelState and are decided per payload (Task 15)."""

    def scanner(self, states, name="CodeStops", text="no, redo it", step=60000):
        """A scanner over one chat of len(states) requests `step` ms apart, each with that modelState value
        (None: the request has no modelState)."""
        ur = cf.make_user_root(self.tmp, name)
        ws = cf.add_workspace(ur, "h1", "/work/demo")
        reqs = [cf.chat_request("r%d" % i, MS + i * step, text, prompt=100, completion=10, credits=0.1,
                                elapsed=3000, state=state, completed=MS + i * step + 2000)
                for i, state in enumerate(states)]
        cf.add_chat_jsonl(ws, "c1", [{"kind": 0, "v": {"sessionId": "c1", "requests": reqs}}])
        return afi.CopilotScanner([ur], os.path.join(self.tmp, "nohome"))

    def payload(self, sc):
        return afi.build_payload(sc, parse_qs("days=0"), source="copilot")

    def cap(self, d, cid):
        return next(c for c in d["health"]["capabilities"] if c["id"] == cid)

    def test_registry_default_stays_no_interruptions(self):
        self.assertTrue(afi.SOURCES["copilot"]["no_interruptions"])         # the answer when no modelState is logged

    def test_scanner_counts_requests_and_states_over_all_history(self):
        sc = self.scanner([1, 2, None, 3])
        st = sc.build_stats("2020-01-01", "2020-01-02")                      # a range with nothing in it
        self.assertEqual((st.chat_requests, st.chat_states), (4, 3))
        self.assertEqual((afi.copilot_stats().chat_requests, afi.copilot_stats().chat_states), (0, 0))

    def test_no_model_state_anywhere_keeps_interruptions_unavailable(self):
        d = self.payload(self.scanner([None] * 10))
        self.assertEqual(d["na"]["interruptions"], "Your Copilot logs don't record stopped requests (older VS Code).")
        self.assertEqual(self.cap(d, "stopped")["status"], "missing")
        self.assertEqual(sum(x["interruptions"] for x in d["days"]), 0)

    def test_stopped_requests_become_interruptions(self):
        d = self.payload(self.scanner([1, 2, 1, 2, 2, 1, 1, 1, 1, 1]))
        self.assertNotIn("interruptions", d["na"])
        self.assertEqual(sum(x["interruptions"] for x in d["days"]), 3)
        self.assertEqual(d["flow"]["interrupted"], 3)
        self.assertEqual(sum(x["prompts"] for x in d["days"]), 10)           # the markers are not prompts
        self.assertEqual(d["cost_unit"], "credits")

    def test_available_is_decided_over_all_data_not_the_range(self):
        sc = self.scanner([1, 2])
        d = afi.build_payload(sc, parse_qs("from=2020-01-01&to=2020-01-02"), source="copilot")
        self.assertNotIn("interruptions", d["na"])
        self.assertEqual(sum(x["interruptions"] for x in d["days"]), 0)      # real zeros: this range had none

    def test_any_numeric_state_counts_as_logged(self):
        self.assertIn("interruptions", self.payload(self.scanner([None, None]))["na"])
        d = self.payload(self.scanner([0, 4], "CodeLogged"))                 # logged, whatever the value
        self.assertNotIn("interruptions", d["na"])
        self.assertEqual(sum(x["interruptions"] for x in d["days"]), 0)

    def test_the_wording_follows_the_decision(self):
        day = 24 * 3600 * 1000                                   # one request a day: enough days for My Adoption
        logged = self.payload(self.scanner([1] * 10, step=day))
        none = self.payload(self.scanner([None] * 10, "CodeNone", step=day))

        def rec(d):
            return next(r for r in d["advisor"]["recs"] + d["advisor"]["more"] if r["id"] == "clean")

        def detail(d):
            return next(c["detail"] for c in d["adoption"]["craft"] if c["id"] == "discernment")

        self.assertIn("were interrupted", rec(logged)["evidence"])
        self.assertNotIn("interrupted", rec(none)["evidence"])
        self.assertRegex(detail(logged), r"^\d+ corrections and interruptions; \d+ tool errors$")
        self.assertRegex(detail(none), r"^\d+ corrections; \d+ tool errors$")

    def test_stopped_row_statuses(self):
        for i, (states, status) in enumerate((([1, 1, 2], "ok"), ([1, None, None], "partial"),
                                              ([None, None], "missing"), ([], "missing"))):
            row = self.cap(self.payload(self.scanner(states, "CodeRow%d" % i)), "stopped")
            self.assertEqual((row["label"], row["status"]), ("Stopped requests", status), states)
            self.assertTrue(row["detail"])

    def test_stopped_row_wording(self):
        ok = self.cap(self.payload(self.scanner([1, 2, 1])), "stopped")["detail"]
        self.assertIn("All 3", ok)
        part = self.cap(self.payload(self.scanner([1, None, None], "CodeP")), "stopped")["detail"]
        self.assertIn("1 of 3", part)
        none = self.cap(self.payload(self.scanner([None, None], "CodeN")), "stopped")["detail"]
        self.assertIn("older VS Code", none)

    def test_claude_payload_has_no_stopped_row(self):
        d = afi.build_payload(afi.Scanner(FIX), parse_qs("days=0"))
        self.assertNotIn("stopped", [c["id"] for c in d["health"]["capabilities"]])
        self.assertEqual(d["na"], {})


class CopilotExactHintTests(unittest.TestCase):
    """Where the dashboard tells you how to get exact tokens, it names the setting that works (Task 15)."""

    def test_exact_row_names_the_command_and_the_setting(self):
        h = afi.build_health_copilot(afi.copilot_stats(), NO_FILES)
        detail = next(c["detail"] for c in h["capabilities"] if c["id"] == "exact")
        self.assertIn(SETUP_HINT, detail)                               # the OTel option stays
        self.assertIn(DEBUG_SETTING, detail)
        self.assertIn("Agent Debug Log", detail)
        self.assertIn("full prompts", detail)                           # the trade-off of the debug log

    def test_no_hint_when_exact_tokens_are_there(self):
        h = afi.build_health_copilot(hand_stats(sessions=10, exact=10), NO_FILES)
        detail = next(c["detail"] for c in h["capabilities"] if c["id"] == "exact")
        self.assertNotIn(DEBUG_SETTING, detail)
        self.assertNotIn("--setup-copilot", detail)

    def test_the_page_sets_the_setting_apart_like_the_command(self):
        self.assertIn(r"agentDebugLog\.fileLogging\.enabled", afi.HTML)

    def test_cache_na_reason_names_both(self):
        na = afi.copilot_na({"capabilities": [{"id": "exact", "status": "missing"}]})
        self.assertIn("--setup-copilot", na["cache_read"])
        self.assertIn(DEBUG_SETTING, na["cache_read"])


class CopilotOfficeShapeEndToEndTests(TmpCase):
    """A fake VS Code user root plus ~/.copilot shaped like the three office reports, through the scanner
    and the payload (Task 15)."""

    def setUp(self):
        super().setUp()
        ur = self.ur = cf.make_user_root(self.tmp)
        home = os.path.join(self.tmp, "home", ".copilot")
        ws = cf.add_workspace(ur, "h1", "/work/office")
        # c1: three requests (the 2nd stopped) and a debug log with two calls: exact, one model row
        rounds = [{"timestamp": MS + 1000, "modelId": "claude-haiku-4.5", "toolCalls": []}]
        cf.add_chat_jsonl(ws, "c1", [{"kind": 0, "v": {"sessionId": "c1", "requests": [
            cf.realistic_chat_request("r1", MS, "first prompt", rounds=rounds),
            cf.realistic_chat_request("r2", MS + 60000, "second prompt", state=2, rounds=rounds),
            cf.realistic_chat_request("r3", MS + 120000, "third prompt", rounds=rounds)]}}])
        cf.add_debug_session(ws, "c1", [
            cf.llm_request("a", MS + 1500, 5000, 400, 3000, "claude-haiku-4.5", 2e9),
            cf.llm_request("b", MS + 61500, 100, 10, 0, "claude-haiku-4-5-20251001", 5e8)])
        # c2: a gpt-5.5 chat with only its own numbers (approx.), stopped too
        cf.add_chat_jsonl(ws, "c2", [{"kind": 0, "v": {"sessionId": "c2", "requests": [
            cf.realistic_chat_request("q1", MS + 180000, "fourth prompt", state=2, model="copilot/gpt-5.5",
                                      resolved="gpt-5.5-2026-04-23", details="GPT-5.5", prompt=1200, completion=300),
            cf.realistic_chat_request("q2", MS + 200000, "next prompt", model="copilot/gpt-5.5",
                                      resolved="gpt-5.5-2026-04-23", details="GPT-5.5", prompt=100, completion=20)]}}])
        # e1: an empty-window chat, the model spelled with a dash
        cf.add_empty_window_chat(ur, "e1", {"requests": [cf.realistic_chat_request(
            "w1", MS + 240000, "fifth prompt", resolved="claude-haiku-5-5", details="Claude Haiku 5.5",
            prompt=700, completion=70,
            rounds=[{"timestamp": MS + 241000, "modelId": "claude-haiku-5-5", "toolCalls": []}])]})
        # t1: a transcript only, with the routine record types
        cf.add_transcript(ws, "t1", [
            {"type": "session.start", "timestamp": "2026-10-24T11:00:00Z",
             "data": {"sessionId": "t1", "copilotVersion": "0.67.0", "vscodeVersion": "1.139.0"}},
            {"type": "user.message", "timestamp": "2026-10-24T11:00:01Z", "data": {"content": "explain it"}}]
            + [{"type": t, "timestamp": "2026-10-24T11:00:02Z", "data": {}} for t in cf.ROUTINE_AGENT_TYPES])
        # k1: a CLI session on gpt-5.5-2026-04-23 with every routine type
        cf.add_cli_session(home, "k1", cf.cli_events())
        self.scanner = afi.CopilotScanner([ur], home)
        self.d = afi.build_payload(self.scanner, parse_qs("days=0"), source="copilot")

    def model(self, name):
        return next(m for m in self.d["models"] if m["name"] == name)

    def test_one_row_per_model(self):
        names = [m["name"] for m in self.d["models"]]
        self.assertEqual(sorted(names), ["claude-haiku-4.5", "claude-haiku-5.5", "gpt-5.5"])
        self.assertEqual(self.model("gpt-5.5")["msgs"], 3)                 # the chat's two requests and the CLI call

    def test_interruptions_are_available_with_the_right_count(self):
        self.assertNotIn("interruptions", self.d["na"])
        self.assertEqual(sum(x["interruptions"] for x in self.d["days"]), 2)       # r2 and q1
        self.assertEqual(self.d["flow"]["interrupted"], 2)                 # r3 and q2 close the stopped turns
        stopped = next(c for c in self.d["health"]["capabilities"] if c["id"] == "stopped")
        self.assertEqual(stopped["status"], "ok")                          # every chat request has a modelState

    def test_exact_for_the_sessions_with_a_log_and_approx_for_the_rest(self):
        self.assertEqual(self.d["copilot"]["token_modes"], {"exact": 2, "approx": 2, "none": 1})
        haiku = self.model("claude-haiku-4.5")                             # c1: the debug log, not the chat's numbers
        self.assertEqual((haiku["input"], haiku["output"], haiku["cache_read"], haiku["msgs"]), (2100, 410, 3000, 2))
        self.assertAlmostEqual(haiku["cost"], 2.5)                         # 2.0 + 0.5 credits
        gpt = self.model("gpt-5.5")                                        # q1, q2 approx. 1300/320 plus the CLI call 2000/400
        self.assertEqual((gpt["input"], gpt["output"]), (3300, 720))
        approx = self.model("claude-haiku-5.5")                            # e1: the chat's own numbers
        self.assertEqual((approx["input"], approx["output"], approx["cache_read"]), (700, 70, 0))

    def test_no_unknown_types_chip(self):
        self.assertEqual(self.d["health"]["unknown_types"], [])
        self.assertEqual(self.d["health"]["bad_lines"], 0)
        self.assertEqual(self.d["health"]["parse_errors"], 0)

    def test_the_debug_logs_other_files_are_found_and_harmless(self):
        self.assertEqual(len(self.scanner.files()["debug"]["c1"]), 3)      # main.jsonl, the model list, the prompt file
        self.assertEqual(self.d["health"]["bad_lines"], 0)

    def test_the_cli_totals_are_counted_once(self):
        cli = self.d["copilot"]["surfaces"]["cli"]
        self.assertEqual((cli["sessions"], cli["output"]), (1, 400))       # the checkpoint adds nothing next to the usage record

    def test_credits_stay_credits(self):
        self.assertEqual(self.d["cost_unit"], "credits")
        self.assertAlmostEqual(sum(m["cost"] for m in self.d["models"]), 4.5)    # 2.5 from c1, 2.0 from the CLI


class AllAITests(CopilotTreeCase):
    """union_slots and build_all_ai (Task 11): what the All AI pages may combine (design 5.5)."""

    def setUp(self):
        super().setUp()
        ws = cf.add_workspace(self.ur, "h2", "/demo/proj")         # the folder the Claude fixture also works in
        cf.add_chat_jsonl(ws, "d1", [{"kind": 0, "v": {"sessionId": "d1", "requests": [
            cf.chat_request("rd", MS + 7200000, "add a test", prompt=100, completion=40, credits=0.25)]}}])
        self.claude_st = afi.Scanner(FIX).build_stats()
        self.copilot_st = self.scanner.build_stats()
        self.both = {"claude": self.claude_st, "copilot": self.copilot_st}

    def test_union_slots_count_overlap_once(self):
        a, b = afi.Stats(), afi.Stats()
        a.days["2026-10-01"]["messages"] = 1; a.day_buckets["2026-10-01"] |= {60, 61}
        b.days["2026-10-01"]["messages"] = 1; b.day_buckets["2026-10-01"] |= {61, 62}
        self.assertEqual(afi.union_slots([a, b], None, None), {"2026-10-01": {60, 61, 62}})

    def test_union_slots_keeps_to_the_window(self):
        a = afi.Stats()
        for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
            a.day_buckets[day] |= {6}
        self.assertEqual(sorted(afi.union_slots([a], "2026-10-02", None)), ["2026-10-02", "2026-10-03"])
        self.assertEqual(sorted(afi.union_slots([a], None, "2026-10-02")), ["2026-10-01", "2026-10-02"])
        self.assertEqual(afi.union_slots([afi.Stats()], None, None), {})

    def test_all_ai_sums_tokens_never_cost(self):
        d = afi.build_all_ai(self.both, None, None, None)
        t, bt = d["totals"], d["by_tool"]
        self.assertNotIn("cost", t)
        self.assertEqual((d["source"], d["cost_unit"]), ("all", None))
        self.assertEqual(t["tokens"], bt["claude"]["tokens"] + bt["copilot"]["tokens"])
        for k in ("input", "output", "cache_read", "cache_write", "prompts", "sessions"):
            self.assertEqual(t[k], bt["claude"][k] + bt["copilot"][k], k)
        self.assertEqual(t["tokens"], t["input"] + t["output"] + t["cache_read"] + t["cache_write"])
        self.assertEqual((bt["claude"]["cost_unit"], bt["copilot"]["cost_unit"]), ("usd", "credits"))
        self.assertEqual(t["active_min"], sum(len(s) for s in afi.union_slots(
            [self.claude_st, self.copilot_st], None, None).values()) * 10)
        self.assertEqual(bt["claude"]["cost"], round(total(self.claude_st, "cost"), 2))
        self.assertEqual(bt["copilot"]["cost"], round(total(self.copilot_st, "cost"), 2))
        self.assertGreater(bt["copilot"]["cost"], 0)

    def test_all_ai_premium_requests_are_their_own_unit(self):
        bt = afi.build_all_ai(self.both, None, None, None)["by_tool"]
        self.assertEqual(bt["copilot"]["premium_requests"], afi.copilot_extras(self.copilot_st)["premium_requests"])
        self.assertGreater(bt["copilot"]["premium_requests"], 0)
        self.assertIsNone(bt["claude"]["premium_requests"])          # Claude has no such unit: no zero is made up

    def test_all_ai_active_time_is_a_union_not_a_sum(self):
        day = sorted(self.claude_st.day_buckets)[0]
        twin = afi.Stats()                   # a second tool active in the very slots Claude was: counted once
        twin.days[day]["messages"] = 1
        twin.day_buckets[day] |= set(self.claude_st.day_buckets[day])
        only_claude = afi.build_all_ai({"claude": self.claude_st}, None, None, None)
        d = afi.build_all_ai({"claude": self.claude_st, "copilot": twin}, None, None, None)
        self.assertEqual(d["by_tool"]["copilot"]["active_min"], len(twin.day_buckets[day]) * 10)
        self.assertEqual(d["totals"]["active_min"], only_claude["totals"]["active_min"])
        row = next(r for r in d["days"] if r["day"] == day)
        self.assertEqual(row["active_min"], len(self.claude_st.day_buckets[day]) * 10)
        self.assertEqual(row["messages"], len(self.claude_st.day_buckets[day]))

    def test_all_ai_days_and_heatmap_come_from_union_slots(self):
        d = afi.build_all_ai(self.both, None, None, None)
        slots = afi.union_slots([self.claude_st, self.copilot_st], None, None)
        self.assertEqual([r["day"] for r in d["days"]], sorted(slots))
        self.assertEqual(d["totals"]["active_days"], len(slots))
        for r in d["days"]:
            s = slots[r["day"]]
            self.assertEqual((r["messages"], r["active_min"], r["focus_max"]),
                             (len(s), len(s) * 10, afi.longest_focus(s)))
            self.assertEqual(r["late_msgs"], sum(1 for x in s if x // 6 >= afi.QUIET_START or x // 6 < afi.QUIET_END))
            self.assertEqual(set(r["tokens_by_tool"]), {"claude", "copilot"})
        self.assertEqual(sum(r["tokens_by_tool"]["claude"] for r in d["days"]),
                         d["by_tool"]["claude"]["tokens"])
        heat = d["heatmap"]
        self.assertEqual((len(heat), {len(r) for r in heat}), (7, {24}))
        self.assertEqual(sum(map(sum, heat)), sum(len(s) for s in slots.values()))
        day, s = next(iter(slots.items()))
        wd = datetime.strptime(day, "%Y-%m-%d").weekday()
        self.assertGreaterEqual(heat[wd][min(s) // 6], 1)

    def test_all_ai_late_slots_follow_the_quiet_hours(self):
        a = afi.Stats()
        a.days["2026-10-01"]["messages"] = 1
        a.day_buckets["2026-10-01"] |= {22 * 6, 23 * 6, 23 * 6 + 5, 5 * 6 + 5, 6 * 6, 12 * 6}
        row = afi.build_all_ai({"claude": a}, None, None, None)["days"][0]
        self.assertEqual((row["messages"], row["late_msgs"]), (6, 3))        # 23:00, 23:50, 05:50 are late; 22:00, 06:00 are not

    def test_all_ai_interruptions_only_from_tools_that_log_them(self):
        for st in self.both.values():
            for day in st.days:
                st.days[day]["interruptions"] = 2
        self.assertFalse(afi.copilot_stops_logged(self.copilot_st))
        d = afi.build_all_ai(self.both, None, None, None)
        self.assertEqual(d["interruptions_from"], ["claude"])
        self.assertEqual(sum(r["interruptions"] for r in d["days"]), 2 * len(self.claude_st.days))
        self.copilot_st.chat_states = 1                          # now the logs record stopped requests
        d = afi.build_all_ai(self.both, None, None, None)
        self.assertEqual(d["interruptions_from"], ["claude", "copilot"])
        self.assertEqual(sum(r["interruptions"] for r in d["days"]),
                         2 * (len(self.claude_st.days) + len(self.copilot_st.days)))

    def test_all_ai_tool_errors_are_summed(self):
        base = total(self.claude_st, "tool_errors") + total(self.copilot_st, "tool_errors")
        self.claude_st.days["2026-10-01"]["tool_errors"] += 3
        self.copilot_st.days[local_day(T0)]["tool_errors"] += 4
        d = afi.build_all_ai(self.both, None, None, None)
        self.assertEqual(sum(r["tool_errors"] for r in d["days"]), base + 7)

    def test_all_ai_models_tagged_and_projects_merged(self):
        d = afi.build_all_ai(self.both, None, None, None)
        self.assertEqual({m["tool"] for m in d["models"]}, {"claude", "copilot"})
        self.assertEqual({m["tool"]: m["cost_unit"] for m in d["models"]}, {"claude": "usd", "copilot": "credits"})
        self.assertEqual(sum(1 for m in d["models"] if m["tool"] == "claude"), len(self.claude_st.models))
        demo = next(p for p in d["projects"] if p["name"] == "demo/proj")   # Claude fixture cwd /demo/proj + a Copilot chat in workspace /demo/proj
        self.assertEqual(set(demo["by_tool"]), {"claude", "copilot"})
        self.assertEqual(demo["tokens"], sum(v["input"] + v["cache_read"] + v["cache_write"] + v["output"] for v in demo["by_tool"].values()))
        self.assertEqual((demo["by_tool"]["claude"]["cost_unit"], demo["by_tool"]["copilot"]["cost_unit"]),
                         ("usd", "credits"))
        names = [p["name"] for p in d["projects"]]
        self.assertEqual(len(names), len(set(names)))           # one row per label
        self.assertEqual([p["tokens"] for p in d["projects"]], sorted((p["tokens"] for p in d["projects"]), reverse=True))

    def test_all_ai_sessions_list_rows_carry_their_tool(self):
        d = afi.build_all_ai(self.both, None, None, None)
        rows = d["sessions_list"]
        self.assertEqual({r["tool"] for r in rows}, {"claude", "copilot"})
        self.assertEqual(len(rows), len(self.claude_st.sessions) + len(self.copilot_st.sessions))
        self.assertEqual([r["day"] for r in rows], sorted((r["day"] for r in rows), reverse=True))
        self.assertIn("title", rows[0]); self.assertIn("type", rows[0])

    def test_all_ai_prev_needs_history_never_a_fake_zero(self):
        with self.assertRaises(ValueError):
            afi.build_all_ai(self.both, "2026-10-04", "2026-10-10", ("2026-09-27", "2026-10-03"))
        d = afi.build_all_ai(self.both, "2026-10-04", "2026-10-10", ("2026-09-27", "2026-10-03"), self.both)
        self.assertGreater(d["prev"]["prompts"], 0)

    def test_all_ai_prev_unions_time_and_adds_both_tools(self):
        day = "2026-10-01"
        claude, twin = afi.Stats(), afi.Stats()
        for st, slots, n in ((claude, {60, 61}, 3), (twin, {61, 62}, 2)):
            st.day_buckets[day] |= slots
            v = st.days[day]
            v["input"], v["output"], v["cache_read"], v["cache_write"], v["prompts"] = n, 10 * n, 100 * n, 1000 * n, n
            st.sessions["s%d" % n] = {"start": "2026-10-01T12:00:00Z", "end": "2026-10-01T12:30:00Z"}
        both = {"claude": claude, "copilot": twin}
        d = afi.build_all_ai({"claude": afi.Stats(), "copilot": afi.Stats()}, "2026-10-04", "2026-10-10",
                             ("2026-09-27", "2026-10-03"), both)
        self.assertEqual(d["prev"]["active_min"], 30)                  # slots 60, 61, 62: the overlap counts once
        self.assertEqual(d["prev"]["prompts"], 5)                      # Copilot adds to prompts ...
        self.assertEqual(d["prev"]["tokens"], 3 * 1111 + 2 * 1111)     # ... and to tokens (input+output+cache)
        self.assertEqual(d["prev"]["sessions"], 2)

    def test_all_ai_interruptions_from_follows_full_history_not_the_window(self):
        window = {"claude": afi.Stats(), "copilot": afi.Stats()}       # a window with no Copilot requests in it
        history = {"claude": afi.Stats(), "copilot": afi.Stats()}
        history["copilot"].chat_states = 1                             # but its logs do record a stopped request
        self.assertEqual(afi.build_all_ai(window, None, None, None, history)["interruptions_from"],
                         ["claude", "copilot"])
        self.assertEqual(afi.build_all_ai(window, None, None, None, {"claude": history["claude"],
                         "copilot": afi.Stats()})["interruptions_from"], ["claude"])
        self.assertEqual(afi.build_all_ai(window, None, None, None)["interruptions_from"], ["claude"])

    def test_all_ai_interruptions_from_a_real_window_without_copilot_requests(self):
        stopped = cf.chat_request("rs", MS, "stop me", prompt=10, completion=5, credits=0.1, state=1)
        ws = cf.add_workspace(self.ur, "h3", "/work/stopped")
        cf.add_chat_jsonl(ws, "z1", [{"kind": 0, "v": {"sessionId": "z1", "requests": [stopped]}}])
        sc = afi.CopilotScanner([self.ur], self.home)
        d = afi.build_payload({"claude": afi.Scanner(FIX), "copilot": sc}, parse_qs("days=7"),
                              today=date(2026, 10, 10), source="all")       # Copilot only has 2026-10-24 activity
        self.assertEqual(d["by_tool"]["copilot"]["prompts"], 0)
        self.assertTrue(afi.copilot_stops_logged(sc.build_stats()))
        self.assertEqual(d["interruptions_from"], ["claude", "copilot"])

    def test_all_ai_project_tokens_include_cache(self):
        d = afi.build_all_ai(self.both, None, None, None)
        for p in d["projects"]:
            self.assertEqual(p["tokens"], sum(v["input"] + v["cache_read"] + v["cache_write"] + v["output"]
                                              for v in p["by_tool"].values()), p["name"])
        demo = next(p for p in d["projects"] if p["name"] == "demo/proj")
        want = sum(self.claude_st.projects["demo/proj"][k] for k in ("input", "output", "cache_read", "cache_write"))
        self.assertEqual(sum(demo["by_tool"]["claude"][k] for k in ("input", "output", "cache_read", "cache_write")), want)
        self.assertGreater(want, self.claude_st.projects["demo/proj"]["input"] + self.claude_st.projects["demo/proj"]["output"])
        self.assertEqual([p["tokens"] for p in d["projects"]], sorted((p["tokens"] for p in d["projects"]), reverse=True))

    def test_all_ai_prev_only_for_rolling_windows(self):
        self.assertNotIn("prev", afi.build_all_ai(self.both, None, None, None))
        self.assertNotIn("prev", afi.build_all_ai(self.both, "2026-10-04", "2026-10-10", None))

    def test_all_ai_has_only_the_listed_keys(self):
        d = afi.build_all_ai(self.both, None, None, None)
        self.assertEqual(set(d), {"source", "cost_unit", "generated", "totals", "by_tool", "days", "heatmap",
                                  "models", "projects", "sessions_list", "interruptions_from"})
        self.assertEqual(set(d["totals"]), {"tokens", "input", "output", "cache_read", "cache_write", "prompts",
                                            "sessions", "active_min", "active_days"})
        self.assertEqual(set(d["by_tool"]["claude"]), {"tokens", "input", "output", "cache_read", "cache_write",
                                                      "prompts", "sessions", "active_min", "cost", "cost_unit",
                                                      "premium_requests"})


SMOKE_JS = r"""// node smoke.js page.js payloads.json : run the All AI renderers against stub payloads with a stub DOM
const vm = require('vm'), fs = require('fs');
const [,, pagePath, payloadPath] = process.argv;
const S = new Proxy(function () {}, {
  get: (t, k) => k === Symbol.toPrimitive ? () => '' : k === 'then' ? undefined : k === 'length' ? 0 : S,
  set: () => true, apply: () => S, construct: () => S, has: () => true });
const cfgs = [], els = {};
// an element that remembers what is written to it (innerHTML etc.) and is a no-op otherwise
const el = key => els[key] || (els[key] = new Proxy({}, {
  get: (t, k) => k in t ? t[k] : S, set: (t, k, v) => { t[k] = v; return true; } }));
const doc = new Proxy({}, { get: (t, k) => k === 'getElementById' || k === 'querySelector' ? el : S, set: () => true });
function Chart(el, cfg) { cfgs.push(cfg); return S; }
Chart.defaults = S;
const ctx = vm.createContext({
  document: doc, window: S, navigator: S, location: S, history: S, Blob: S, URL: S, console,
  localStorage: { getItem: () => null, setItem() {} }, matchMedia: () => S, getComputedStyle: () => ({ getPropertyValue: () => '#336699' }),
  fetch: () => new Promise(() => {}), requestAnimationFrame: () => 0, cancelAnimationFrame() {}, setTimeout: () => 0, clearTimeout() {},
  setInterval: () => 0, addEventListener() {}, performance: { now: () => 0 }, Chart, Intl, Math, Date, JSON, Object, Array, Number, String, Set, Map });
vm.runInContext(fs.readFileSync(pagePath, 'utf8'), ctx);
const payloads = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
let n = 0;
for (const [name, p] of Object.entries(payloads)) {
  ctx.__p = p;
  vm.runInContext("D=__p;SOURCE='all';section='overview';renderAllOverview();renderAllTools();renderAllProjects();", ctx);
  // the first row that one tool owns, the way a click would open it
  vm.runInContext("(()=>{const r=D.projects.find(p=>Object.keys(p.by_tool).length===1);if(r)allProject(r);})()", ctx);
  const bad = Object.entries(els).filter(([k, e]) => typeof e.innerHTML === 'string' && /NaN|undefined|null/.test(e.innerHTML));
  if (bad.length) throw new Error(name + ': a placeholder reached the page in ' + bad.map(b => b[0] + ' ...' + (()=>{const h=b[1].innerHTML,i=h.search(/NaN|undefined|null/);return h.slice(Math.max(0,i-10),i+120);})()).join(', '));
  if (name === 'no costs') {   // a missing cost is a dash, not a zero
    const html = els.aKpis.innerHTML + els['#tAModels tbody'].innerHTML;
    if (/0\.00 est|0 credits|\$0|0 cr/.test(html) || !html.includes('flat-d')) throw new Error('missing costs are not dashes');
  }
  n++;
}
console.log('ok ' + n + ' payloads, ' + cfgs.length + ' charts');
"""


class AllAIPayloadTests(CopilotTreeCase):
    """build_payload(source="all") and /api/stats?source=all (Task 11)."""
    TODAY = date(2026, 10, 10)

    def setUp(self):
        super().setUp()
        ws = cf.add_workspace(self.ur, "h2", "/demo/proj")
        cf.add_chat_jsonl(ws, "d1", [{"kind": 0, "v": {"sessionId": "d1", "requests": [
            cf.chat_request("rd", MS + 7200000, "add a test", prompt=100, completion=40, credits=0.25)]}}])
        self.scanners = {"claude": afi.Scanner(FIX), "copilot": self.scanner}

    def payload(self, qs, **kw):
        return afi.build_payload(self.scanners, parse_qs(qs), source="all", **kw)

    def test_payload_adds_the_page_extras(self):
        d = self.payload("days=0")
        self.assertEqual((d["source"], d["cost_unit"]), ("all", None))
        self.assertEqual([(s["id"], s["available"]) for s in d["sources"]],
                         [("claude", True), ("copilot", True), ("all", True)])
        union = sorted(set(afi.Scanner(FIX).build_stats().days) | set(self.scanner.build_stats().days))
        self.assertEqual(d["active_days"], len(union))
        self.assertEqual((d["streak"], d["best_streak"]), afi.active_streak(union))
        self.assertIn("catalog", d)
        self.assertNotIn("Claude", json.dumps(d["catalog"]))           # worded for the AI tools, not one of them
        for single_tool in ("notifications", "plan_window", "weeks", "adoption", "advisor", "health", "root"):
            self.assertNotIn(single_tool, d)

    def test_project_rows_carry_exact_sessions_and_days_per_tool(self):
        """The project sheet reads these (sessions_list is capped), so they must match the single-tool rows."""
        for qs in ("days=0", "days=7"):
            d = self.payload(qs, today=self.TODAY)
            singles = {t: {p["name"]: p for p in afi.build_payload(self.scanners[t], parse_qs(qs), today=self.TODAY,
                                                                   source=t)["projects"]} for t in self.scanners}
            seen = 0
            for p in d["projects"]:
                for tool, row in p["by_tool"].items():
                    one = singles[tool][p["name"]]
                    self.assertEqual(row["sessions"], one["sessions"], (qs, p["name"], tool))
                    self.assertEqual(row["days"], one["days"], (qs, p["name"], tool))
                    seen += 1
            self.assertTrue(seen, qs)

    def test_project_sessions_are_not_the_capped_list(self):
        many = afi.Stats()
        for i in range(320):                      # more sessions than sessions_list keeps (300)
            many.projects["big"]["sessions"].add("s%d" % i)
            many.sessions["s%d" % i] = {"project": "big", "first": "2026-10-01T10:00:00", "last": "2026-10-01T10:05:00",
                                        "title": "t", "loc": 0, "reads": 0, "writes": 0, "output": 1, "prompts": 1,
                                        "bash": 0, "type": "other"}
        many.projects["big"]["days"]["2026-10-01"]["output"] = 5
        row = afi.build_all_ai({"claude": many}, None, None, None)["projects"][0]["by_tool"]["claude"]
        self.assertEqual(row["sessions"], 320)
        self.assertEqual(row["days"], [{"day": "2026-10-01", "output": 5, "loc": 0}])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_page_renderers_run_on_stub_payloads(self):
        """Smoke: the page script runs in node with a stub DOM and Chart, and renderAllOverview /
        renderAllTools / renderAllProjects (and the project sheet's row builder) do not throw on real
        All AI payloads, on a range where one tool has no data, a one-tool payload, and missing costs."""
        import copy
        import re
        base = self.payload("days=0")
        empty_copilot = self.payload("days=7", today=self.TODAY)
        self.assertEqual(empty_copilot["by_tool"]["copilot"]["prompts"], 0)       # the edge case is real
        one_tool = copy.deepcopy(base)
        del one_tool["by_tool"]["copilot"]
        for day in one_tool["days"]:
            day["tokens_by_tool"].pop("copilot", None)
        one_tool["models"] = [m for m in one_tool["models"] if m["tool"] == "claude"]
        one_tool["projects"] = [dict(p, by_tool={"claude": p["by_tool"]["claude"]}) for p in one_tool["projects"]
                                if "claude" in p["by_tool"]]
        no_cost = copy.deepcopy(base)
        for row in no_cost["by_tool"].values():
            row["cost"] = None
        for m in no_cost["models"]:
            m["cost"] = None
        for p in no_cost["projects"]:
            for row in p["by_tool"].values():
                row["cost"] = None
        payloads = {"all": base, "one tool idle": empty_copilot, "one tool only": one_tool, "no costs": no_cost,
                    "no prev": {k: v for k, v in base.items() if k != "prev"}}
        page = re.findall(r"<script>(.*?)</script>", afi.HTML, re.S)[-1]
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in (("page.js", page), ("smoke.js", SMOKE_JS), ("payloads.json", json.dumps(payloads))):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                    f.write(text)
            r = subprocess.run(["node", os.path.join(tmp, "smoke.js"), os.path.join(tmp, "page.js"),
                                os.path.join(tmp, "payloads.json")], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok 5 payloads", r.stdout)

    def test_rolling_window_is_the_same_for_every_tool(self):
        d = self.payload("days=7", today=self.TODAY)
        claude = afi.build_payload(self.scanners["claude"], parse_qs("days=7"), today=self.TODAY)
        copilot = afi.build_payload(self.scanners["copilot"], parse_qs("days=7"), today=self.TODAY, source="copilot")
        self.assertEqual(d["by_tool"]["claude"]["prompts"], claude["totals"]["prompts"])
        self.assertEqual(d["by_tool"]["claude"]["output"], claude["totals"]["output"])
        self.assertEqual(d["by_tool"]["copilot"]["prompts"], copilot["totals"]["prompts"])
        self.assertEqual(d["by_tool"]["copilot"]["sessions"], copilot["totals"]["sessions"])
        self.assertEqual(d["by_tool"]["claude"]["cost"], claude["totals"]["cost"])

    def test_prev_window_combines_with_the_same_rules(self):
        d = self.payload("days=7", today=self.TODAY)
        full = self.scanners["claude"].build_stats()
        prev = [x for x in full.days if "2026-09-27" <= x <= "2026-10-03"]
        want = {"prompts": sum(full.days[x]["prompts"] for x in prev),
                "tokens": sum(full.days[x][k] for x in prev for k in ("input", "output", "cache_read", "cache_write")),
                "active_min": sum(len(full.day_buckets[x]) for x in prev) * 10}
        self.assertTrue(prev and want["prompts"] > 0)               # the fixture has activity before the window
        self.assertEqual(set(d["prev"]), {"tokens", "prompts", "sessions", "active_min"})
        for k, v in want.items():
            self.assertEqual(d["prev"][k], v, k)
        self.assertGreater(d["prev"]["sessions"], 0)
        self.assertNotIn("prev", self.payload("days=0"))
        self.assertNotIn("prev", self.payload("from=2026-10-01&to=2026-10-10"))

    def test_custom_range_and_all_time(self):
        full = self.payload("days=0")
        part = self.payload("from=2026-10-01&to=2026-10-03")
        self.assertEqual([r["day"] for r in part["days"]], ["2026-10-01", "2026-10-03"])
        self.assertLess(part["totals"]["tokens"], full["totals"]["tokens"])
        self.assertEqual(part["by_tool"]["copilot"]["prompts"], 0)

    def test_handler_serves_all_when_both_tools_are_on(self):
        get = HandlerRoutingTests.serve(self, scanners=self.scanners)
        d = get("/api/stats?days=0&source=all")
        self.assertEqual((d["source"], d["cost_unit"]), ("all", None))
        self.assertEqual(set(d["by_tool"]), {"claude", "copilot"})
        self.assertEqual([(s["id"], s["available"]) for s in d["sources"]],
                         [("claude", True), ("copilot", True), ("all", True)])
        for path, src in (("/api/stats?days=0&source=claude", "claude"), ("/api/stats?days=0&source=copilot", "copilot")):
            self.assertEqual(get(path)["source"], src)

    def test_handler_falls_back_when_all_is_not_available(self):
        get = HandlerRoutingTests.serve(self, scanners={"copilot": self.scanner})
        self.assertEqual(get("/api/stats?days=0&source=all")["source"], "copilot")
        get = HandlerRoutingTests.serve(self, scanner=afi.Scanner(FIX))
        d = get("/api/stats?days=0&source=all")
        self.assertEqual((d["source"], d["root"]), ("claude", FIX))

    def test_dump_source_all(self):
        r = MainFlagTests.run_main(self, "--dump", "--source", "all", "--dir", FIX, "--copilot-root", self.ur)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["source"], "all")
        r = MainFlagTests.run_main(self, "--dump", "--source", "all", "--dir", FIX, "--copilot-root", self.ur, "--no-copilot")
        self.assertEqual(json.loads(r.stdout)["source"], "claude")      # one tool alone: nothing to combine

    def test_single_tool_payloads_are_untouched_by_an_all_request(self):
        def snap():
            out = {}
            for s, sc in self.scanners.items():
                d = json.loads(json.dumps(afi.build_payload(sc, parse_qs("days=0"), today=self.TODAY, source=s),
                                          default=str))
                d.pop("generated")
                out[s] = d
            return out
        before = snap()
        self.payload("days=0")
        self.assertEqual(snap(), before)


class AllAIPageTests(unittest.TestCase):
    """The All AI pages (Task 12): the page carries the containers, routing and wording the design asks
    for. The rendering itself is checked in a browser; these keep the pieces from drifting apart."""
    JS = None

    @classmethod
    def script(cls):
        if cls.JS is None:
            import re
            cls.JS = re.findall(r"<script>(.*?)</script>", afi.HTML, re.S)[-1]
        return cls.JS

    def test_containers_and_chart_ids_exist_once(self):
        for ident in ("sa-overview", "sa-tools", "sa-projects", "aKpis", "cATokens", "cAShare",
                      "cAModels", "tAModels", "tAProj"):
            self.assertEqual(afi.HTML.count('id="%s"' % ident), 1, ident)

    def test_all_sections_and_the_dim_title(self):
        js = self.script()
        self.assertIn("const ALL_SECTIONS=['overview','focus','tools','projects'];", js)
        self.assertIn("Pick Claude Code or GitHub Copilot to see this.", js)
        self.assertIn("button.dim{opacity:.4;cursor:not-allowed}", afi.HTML)

    def test_time_and_focus_is_reused_with_slot_wording(self):
        js = self.script()
        self.assertNotIn("sa-focus", afi.HTML)                          # #s-focus serves All AI too
        self.assertIn("' active 10-min slot'", js)
        self.assertIn("' message'", js)                                 # Claude and Copilot keep their word

    def test_renderers_are_wired_into_render(self):
        js = self.script()
        for fn in ("renderAllOverview", "renderAllTools", "renderAllProjects"):
            self.assertIn("function %s(" % fn, js)
        self.assertIn("all?renderAllOverview:renderOverview", js)
        self.assertIn("all?renderAllTools:renderTools", js)
        self.assertIn("all?renderAllProjects:renderProjects", js)

    def test_cost_is_two_lines_never_one_sum(self):
        js = self.script()
        self.assertIn("Not added together: different units", js)
        self.assertIn("' credits'", js)
        self.assertIn("' est.'", js)
        self.assertIn("premium_requests>0", js)                         # shown only when there are some

    def test_interruptions_are_labelled_with_the_tools_that_log_them(self):
        js = self.script()
        self.assertIn("d.interruptions_from", js)
        self.assertIn("' only'", js)

    def test_project_rows_open_only_when_one_tool_has_data(self):
        js = self.script()
        self.assertIn("Open it from Claude Code or GitHub Copilot", js)
        self.assertIn("Object.keys(p.by_tool).length===1", js)

    def test_series_colours_come_from_tokens_not_hex_literals(self):
        css = afi.HTML.split("</style>")[0]
        self.assertEqual(css.count("--claude:"), 3)                     # light, system dark, forced dark
        self.assertIn("claude:cssv('--claude')", afi.HTML)
        js = self.script()
        block = js[js.index("const toolClr="):js.index("function renderAllOverview")]
        self.assertNotRegex(block, r"#[0-9a-fA-F]{6}")                  # no hex literal in the series colours
        self.assertIn("{claude:C.claude,copilot:C.violet}", block)

    def test_one_export_slug_and_recent_sessions_label(self):
        js = self.script()
        self.assertEqual(js.count("'all-ai'"), 1)
        self.assertEqual(js.count("expName()+'-usage_'"), 3)            # the JSON and both CSVs share it
        self.assertIn("'Recent sessions'+(sess.length<p.sessions?' (showing '", js)

    def test_payload_without_weeks_does_not_break_load(self):
        self.assertIn("(D.weeks||[]).length", self.script())

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_page_script_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "page.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.script())
            r = subprocess.run(["node", "--check", path], capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)


class ParseEntryHookTests(unittest.TestCase):
    """The Copilot hooks in parse_entry: cost override, _loc, Copilot tool names, mcp_ prefix."""
    TS = "2026-10-01T12:00:00.000+00:00"
    DAY = datetime(2026, 10, 1, 12, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d")

    def entry(self, tools, usage=None, **extra):
        e = {"type": "assistant", "timestamp": self.TS, "sessionId": "s",
             "message": {"id": "a", "model": "gpt-5",
                         "usage": {"input_tokens": 10, "output_tokens": 5} if usage is None else usage,
                         "content": [{"type": "tool_use", "id": "t%d" % i, "name": n, "input": inp}
                                     for i, (n, inp) in enumerate(tools)]}}
        e.update(extra)
        return e

    def test_cost_override_and_loc_hooks(self):
        st = afi.Stats()
        afi.parse_entry({"type": "assistant", "timestamp": "2026-10-01T12:00:00.000+00:00", "sessionId": "s",
            "message": {"id": "a", "model": "gpt-5", "usage": {"input_tokens": 10, "output_tokens": 5},
                        "content": [{"type": "tool_use", "id": "t", "name": "create_file",
                                     "input": {"file_path": "x.py", "_loc": 7}}]},
            "_cost_override": 0.5}, "p", st)
        day = st.days[self.DAY]
        self.assertEqual((day["cost"], day["cache_savings"], day["loc"], day["writes"]), (0.5, 0.0, 7, 1))

    def test_cost_override_reaches_every_cost_total_and_skips_cache_savings(self):
        st = afi.Stats()
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 1000000}
        afi.parse_entry(self.entry([], usage, _cost_override=0.25), "p", st)
        self.assertEqual(st.models["gpt-5"]["cost"], 0.25)
        self.assertEqual(st.projects["p"]["cost"], 0.25)
        self.assertEqual(st.sessions["s"]["cost"], 0.25)
        self.assertEqual(st.days[self.DAY]["cache_savings"], 0.0)

    def test_zero_cost_override_is_honoured(self):
        st = afi.Stats()
        usage = {"input_tokens": 1000000, "output_tokens": 1000000}
        afi.parse_entry(self.entry([], usage, _cost_override=0), "p", st)
        self.assertEqual(st.days[self.DAY]["cost"], 0)

    def test_cost_only_entry_with_empty_usage_adds_credits_and_nothing_else(self):
        # exact-token mode: the chat translation has no usage but still carries each request's credits
        st = afi.Stats()
        afi.parse_entry(self.entry([], {}, _cost_override=0.5), "p", st)
        day = st.days[self.DAY]
        self.assertEqual((day["cost"], day["input"], day["output"], day["cache_savings"]), (0.5, 0, 0, 0.0))
        self.assertEqual(st.models["gpt-5"]["cost"], 0.5)
        self.assertEqual(st.models["gpt-5"]["msgs"], 0)
        self.assertEqual(st.projects["p"]["cost"], 0.5)
        self.assertEqual(st.projects["p"]["output"], 0)
        sess = st.sessions["s"]
        self.assertEqual((sess["cost"], sess["api_calls"], sess["first_call_cost"], sess["cache_resets"],
                          sess["output"]), (0.5, 0, 0.0, 0, 0))
        self.assertEqual(sess["model_cost"]["gpt-5"], 0.5)
        self.assertEqual(st.hc["assistant_msgs"], 0)
        self.assertEqual(st.branches["(none)"]["msgs"], 0)

    def test_cost_only_entry_is_counted_once_when_relogged(self):
        st = afi.Stats()
        afi.parse_entry(self.entry([], {}, _cost_override=0.5), "p", st)
        afi.parse_entry(self.entry([], {}, _cost_override=0.5), "p", st)
        self.assertEqual(st.days[self.DAY]["cost"], 0.5)
        self.assertEqual(st.models["gpt-5"]["cost"], 0.5)
        self.assertEqual(st.projects["p"]["cost"], 0.5)
        self.assertEqual(st.sessions["s"]["cost"], 0.5)

    def test_cost_only_entries_with_different_ids_add_up(self):
        st = afi.Stats()
        afi.parse_entry(self.entry([], {}, _cost_override=0.5), "p", st)
        other = self.entry([], {}, _cost_override=0.25)
        other["message"]["id"] = "b"
        afi.parse_entry(other, "p", st)
        self.assertEqual(st.days[self.DAY]["cost"], 0.75)

    def test_cost_only_entry_still_counts_its_tool_calls(self):
        st = afi.Stats()
        afi.parse_entry(self.entry([("create_file", {"file_path": "x.py", "_loc": 3})], {}, _cost_override=0.5), "p", st)
        day = st.days[self.DAY]
        self.assertEqual((day["cost"], day["loc"], day["writes"]), (0.5, 3, 1))

    def test_empty_usage_without_a_numeric_override_still_counts_nothing(self):
        for extra in ({}, {"_cost_override": None}, {"_cost_override": "3"}, {"_cost_override": True}):
            st = afi.Stats()
            afi.parse_entry(self.entry([], {}, **extra), "p", st)
            self.assertEqual(st.days[self.DAY]["cost"], 0, extra)
            self.assertNotIn("gpt-5", st.models, extra)

    def test_without_override_cost_comes_from_prices(self):
        st = afi.Stats()
        usage = {"input_tokens": 1000000, "output_tokens": 0, "cache_read_input_tokens": 1000000}
        afi.parse_entry(self.entry([], usage), "p", st)
        p = afi.price_for("gpt-5")
        self.assertAlmostEqual(st.days[self.DAY]["cost"], p["in"] * 1.1)
        self.assertAlmostEqual(st.days[self.DAY]["cache_savings"], p["in"] * 0.9)

    def test_loc_hook_replaces_computed_lines_and_feeds_every_total(self):
        st = afi.Stats()
        afi.parse_entry(self.entry([
            ("create_file", {"file_path": "src/x.py", "_loc": 7}),
            ("Write", {"file_path": "src/y.py", "content": "a\nb", "_loc": 4}),   # hook wins over computed 2
        ]), "p", st)
        self.assertEqual(st.days[self.DAY]["loc"], 11)
        self.assertEqual(st.projects["p"]["loc"], 11)
        self.assertEqual(st.sessions["s"]["loc"], 11)
        self.assertEqual(dict(st.files_touched), {"x.py": 7, "y.py": 4})

    def test_claude_write_without_hook_still_counts_content(self):
        st = afi.Stats()
        afi.parse_entry(self.entry([("Write", {"file_path": "y.py", "content": "a\nb"})]), "p", st)
        self.assertEqual(st.days[self.DAY]["loc"], 2)

    def test_copilot_tool_names_are_classified(self):
        st = afi.Stats()
        afi.parse_entry(self.entry([
            ("read_file", {}), ("grep_search", {}), ("replace_string_in_file", {"file_path": "a.py"}),
            ("run_in_terminal", {"command": "python -m unittest"}), ("fetch_webpage", {}),
            ("runSubagent", {}), ("manage_todo_list", {}), ("mcp_github_search", {})]), "p", st)
        day = st.days[self.DAY]
        self.assertEqual((day["reads"], day["writes"]), (3, 1))        # fetch_webpage reads too
        self.assertEqual(st.sessions["s"]["bash"], 1)
        for feat in ("verify", "web", "subagents", "tasks", "mcp"):
            self.assertIn(self.DAY, st.feature_days[feat], feat)

    def test_mcp_double_underscore_prefix_still_counts(self):
        st = afi.Stats()
        afi.parse_entry(self.entry([("mcp__server__tool", {})]), "p", st)
        self.assertIn(self.DAY, st.feature_days["mcp"])

    def test_tool_sets_extend_the_claude_ones(self):
        self.assertIn("Read", afi.READ_TOOLS)
        self.assertIn("MultiEdit", afi.WRITE_TOOLS)
        self.assertIn("Bash", afi.CMD_TOOLS)
        self.assertLessEqual(cs.COPILOT_READ_TOOLS, afi.READ_TOOLS)
        self.assertLessEqual(cs.COPILOT_WRITE_TOOLS, afi.WRITE_TOOLS)
        self.assertLessEqual(cs.COPILOT_CMD_TOOLS, afi.CMD_TOOLS)
        self.assertEqual(afi.TOOL_FEATURES["Bash"], "verify")
        self.assertEqual(afi.TOOL_FEATURES["run_in_terminal"], "verify")


class SetupCopilotTests(TmpCase):
    """--setup-copilot edits a user's real settings.json, so every test passes home= and temp roots."""

    def setUp(self):
        super().setUp()
        self.out = []

    def run_setup(self, roots, answer="y", **kw):
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return answer
        rc = afi.run_setup_copilot(roots, input_fn=ask, print_fn=lambda *a: self.out.append(" ".join(map(str, a))),
                                   home=self.tmp, **kw)
        self.asked = asked
        return rc

    def text(self):
        return "\n".join(self.out)

    def read(self, p):
        with open(p, encoding="utf-8", newline="") as fh:
            return fh.read()

    def raw(self, p):
        with open(p, "rb") as fh:
            return fh.read()

    def load(self, p):
        return json.loads(self.read(p))

    def baks(self, p):
        return sorted(n for n in os.listdir(os.path.dirname(p)) if n.startswith("settings.json.bak-aifi-"))

    def test_setup_copilot_writes_keys_with_backup(self):
        ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, {"editor.fontSize": 14})
        rc = afi.run_setup_copilot([ur], input_fn=lambda _: "y", print_fn=lambda *a: None, home=self.tmp)
        s = self.load(p)
        self.assertEqual((rc, s["github.copilot.chat.otel.enabled"], s["github.copilot.chat.otel.exporterType"]), (0, True, "file"))
        self.assertTrue(any(n.startswith("settings.json.bak-aifi-") for n in os.listdir(os.path.dirname(p))))

    def test_outfile_is_absolute_under_home_and_folder_is_created(self):
        ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, {})
        self.assertEqual(self.run_setup([ur]), 0)
        want = os.path.join(self.tmp, ".copilot", "otel", "vscode-copilot-otel.jsonl")
        s = self.load(p)
        self.assertEqual(s["github.copilot.chat.otel.outfile"], want)
        self.assertTrue(os.path.isabs(want))
        self.assertTrue(os.path.isdir(os.path.dirname(want)))
        self.assertEqual(cs._outfile_setting(p), want)         # the reader finds what was written
        self.assertIn("Exact tokens", self.text())
        self.assertIn("Reload", self.text())

    def test_other_keys_and_their_order_are_untouched(self):
        ur = cf.make_user_root(self.tmp)
        orig = {"zeta": 1, "github.copilot.chat.agentDebugLog.fileLogging.enabled": True,
                "alpha": {"b": [1, 2], "a": "é"}, "github.copilot.chat.otel.enabled": False}
        p = cf.write_settings(ur, orig)
        self.assertEqual(self.run_setup([ur]), 0)
        s = json.loads(self.read(p))
        self.assertEqual(list(s), ["zeta", "github.copilot.chat.agentDebugLog.fileLogging.enabled", "alpha",
                                   "github.copilot.chat.otel.enabled", "github.copilot.chat.otel.exporterType",
                                   "github.copilot.chat.otel.outfile"])
        for k in ("zeta", "github.copilot.chat.agentDebugLog.fileLogging.enabled", "alpha"):
            self.assertEqual(s[k], orig[k])
        self.assertIs(s["github.copilot.chat.otel.enabled"], True)
        bak = os.path.join(os.path.dirname(p), self.baks(p)[0])
        self.assertEqual(self.load(bak), orig)      # the backup is the old file

    def test_setup_copilot_refuses_jsonc(self):
        ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, '{\n  // mine\n  "a": 1,\n}\n')
        before = self.read(p)
        self.assertEqual(afi.run_setup_copilot([ur], input_fn=lambda _: "y", print_fn=lambda *a: None, home=self.tmp), 2)
        self.assertEqual(self.read(p), before)

    def test_jsonc_prints_the_lines_to_paste_and_writes_nothing(self):
        ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, '{\n  "a": 1, // note\n}')
        before = self.read(p)
        self.assertEqual(self.run_setup([ur]), 2)
        t = self.text()
        self.assertIn('"github.copilot.chat.otel.enabled": true,', t)
        self.assertIn('"github.copilot.chat.otel.exporterType": "file",', t)
        self.assertIn('"github.copilot.chat.otel.outfile": ', t)
        self.assertIn("Paste these into your VS Code settings (Ctrl+Shift+P", t)
        self.assertEqual((self.read(p), self.baks(p), self.asked), (before, [], []))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".copilot")))

    def test_setup_copilot_dry_run_and_decline_change_nothing(self):
        ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, {"editor.fontSize": 14})
        before = self.raw(p)
        self.assertEqual(self.run_setup([ur], dry_run=True), 0)
        self.assertEqual((self.raw(p), self.asked), (before, []))     # never even asks
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".copilot")))   # no otel folder
        self.assertEqual(os.listdir(ur), ["settings.json"])
        for answer in ("n", "", "no", "maybe", "Y es"):
            self.assertEqual(self.run_setup([ur], answer=answer), 0, answer)
            self.assertEqual(self.raw(p), before, answer)
        self.assertEqual(self.asked, ["Apply these changes? [y/N] "])
        self.assertEqual(os.listdir(ur), ["settings.json"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".copilot")))

    def test_yes_answers_apply(self):
        for answer in ("y", "yes", " YES "):
            ur = cf.make_user_root(self.tmp, variant="V" + answer.strip()); p = cf.write_settings(ur, {})
            self.assertEqual(self.run_setup([ur], answer=answer), 0)
            self.assertIs(json.loads(self.read(p))["github.copilot.chat.otel.enabled"], True, answer)

    def test_end_of_input_counts_as_no(self):
        ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, {"a": 1}); before = self.read(p)

        def eof(_):
            raise EOFError
        rc = afi.run_setup_copilot([ur], input_fn=eof, print_fn=lambda *a: None, home=self.tmp)
        self.assertEqual((rc, self.read(p)), (0, before))

    def test_setup_copilot_undo_removes_keys(self):
        ur = cf.make_user_root(self.tmp)
        p = cf.write_settings(ur, {"keep": 1, "github.copilot.chat.agentDebugLog.fileLogging.enabled": True})
        self.assertEqual(self.run_setup([ur]), 0)
        self.assertEqual(self.run_setup([ur], undo=True), 0)
        s = self.load(p)
        for k in afi.COPILOT_OTEL_KEYS:
            self.assertNotIn(k, s)
        self.assertEqual(s, {"keep": 1, "github.copilot.chat.agentDebugLog.fileLogging.enabled": True})
        self.assertGreaterEqual(len(self.baks(p)), 1)

    def test_undo_dry_run_decline_and_jsonc_change_nothing(self):
        ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, dict(zip(afi.COPILOT_OTEL_KEYS, (True, "file", os.path.join(self.tmp, "o.jsonl")))))
        before = self.read(p)
        self.assertEqual(self.run_setup([ur], undo=True, dry_run=True), 0)
        self.assertEqual(self.run_setup([ur], undo=True, answer="n"), 0)
        self.assertEqual((self.read(p), self.baks(p)), (before, []))
        p2 = cf.write_settings(ur, '{ // c\n "github.copilot.chat.otel.enabled": true }')
        before2 = self.read(p2)
        self.assertEqual(self.run_setup([ur], undo=True), 2)
        self.assertEqual((self.read(p2), self.baks(p2)), (before2, []))

    def test_undo_with_nothing_to_remove_is_a_no_op(self):
        ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, {"a": 1}); before = self.read(p)
        self.assertEqual(self.run_setup([ur], undo=True), 0)
        self.assertEqual((self.read(p), self.baks(p), self.asked), (before, [], []))

    def test_already_configured_is_a_no_op(self):
        ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, {})
        self.assertEqual(self.run_setup([ur]), 0)
        before, baks = self.read(p), self.baks(p)
        self.out.clear()
        self.assertEqual(self.run_setup([ur]), 0)
        self.assertEqual((self.read(p), self.baks(p)), (before, baks))
        self.assertIn("Already", self.text())

    def test_picks_stable_code_user_not_its_profile(self):
        ur = cf.make_user_root(self.tmp)
        prof = os.path.join(ur, "profiles", "builtin"); os.makedirs(prof)
        p = cf.write_settings(ur, {"a": 1}); pp = cf.write_settings(prof, {"b": 2})
        for roots in ([prof, ur], [ur, prof]):                 # whatever the order
            self.assertEqual(self.run_setup(roots, dry_run=True), 0)
        self.assertEqual(self.run_setup([prof, ur]), 0)
        self.assertIn("github.copilot.chat.otel.enabled", json.loads(self.read(p)))
        self.assertEqual(json.loads(self.read(pp)), {"b": 2})

    def test_picks_stable_code_over_insiders_and_falls_back_to_first_root(self):
        ins = cf.make_user_root(self.tmp, variant="Code - Insiders"); st = cf.make_user_root(self.tmp)
        self.assertEqual(self.run_setup([ins, st]), 0)
        self.assertTrue(os.path.exists(os.path.join(st, "settings.json")))
        self.assertFalse(os.path.exists(os.path.join(ins, "settings.json")))
        other = cf.make_user_root(os.path.join(self.tmp, "x"), variant="VSCodium")
        other2 = cf.make_user_root(os.path.join(self.tmp, "x"), variant="Cursor")
        self.assertEqual(self.run_setup([other, other2]), 0)
        self.assertTrue(os.path.exists(os.path.join(other, "settings.json")))
        self.assertFalse(os.path.exists(os.path.join(other2, "settings.json")))

    def test_no_user_root_returns_1(self):
        self.assertEqual(self.run_setup([]), 1)
        self.assertIn("VS Code", self.text())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".copilot")))

    def test_missing_settings_file_is_created_fresh(self):
        ur = cf.make_user_root(self.tmp); p = os.path.join(ur, "settings.json")
        self.assertEqual(self.run_setup([ur]), 0)
        self.assertIs(json.loads(self.read(p))["github.copilot.chat.otel.enabled"], True)
        self.assertEqual(self.baks(p), [])                     # nothing to back up

    def test_bom_and_crlf_files_are_handled(self):
        ur = cf.make_user_root(self.tmp)
        p = os.path.join(ur, "settings.json")
        with open(p, "wb") as fh:
            fh.write(b'\xef\xbb\xbf{\r\n  "a": 1\r\n}\r\n')
        self.assertEqual(self.run_setup([ur]), 0)
        raw = self.raw(p)
        self.assertNotIn(b"\xef\xbb\xbf", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))        # CRLF kept throughout
        self.assertEqual(json.loads(raw.decode("utf-8"))["a"], 1)

    def test_non_object_settings_are_refused(self):
        ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, "[1, 2]")
        self.assertEqual(self.run_setup([ur]), 2)
        self.assertEqual(self.read(p), "[1, 2]")

    def test_existing_outfile_is_kept(self):
        ur = cf.make_user_root(self.tmp)
        mine = os.path.join(self.tmp, "mine", "otel.jsonl")
        p = cf.write_settings(ur, {"github.copilot.chat.otel.outfile": mine})
        self.assertEqual(self.run_setup([ur]), 0)
        s = self.load(p)
        self.assertEqual((s["github.copilot.chat.otel.outfile"], s["github.copilot.chat.otel.exporterType"],
                          s["github.copilot.chat.otel.enabled"]), (mine, "file", True))
        self.assertIn("kept", self.text())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".copilot")))    # our folder not needed
        self.assertEqual(cs._outfile_setting(p), mine)

    def test_empty_outfile_is_replaced_but_other_exporter_is_not_touched(self):
        ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, {"github.copilot.chat.otel.outfile": ""})
        self.assertEqual(self.run_setup([ur]), 0)
        self.assertTrue(self.load(p)["github.copilot.chat.otel.outfile"].endswith("vscode-copilot-otel.jsonl"))
        ur2 = cf.make_user_root(self.tmp, variant="Other")
        orig = {"github.copilot.chat.otel.enabled": True, "github.copilot.chat.otel.exporterType": "otlp-http",
                "github.copilot.chat.otel.otlpEndpoint": "http://localhost:4318"}
        p2 = cf.write_settings(ur2, orig); before = self.read(p2)
        self.out.clear()
        self.assertEqual(self.run_setup([ur2]), 0)
        self.assertEqual((self.read(p2), self.baks(p2), self.asked), (before, [], []))
        t = self.text()
        self.assertIn('"otlp-http"', t)
        self.assertIn('exporterType "file"', t)
        self.assertIn("by hand", t)

    def test_undo_leaves_a_foreign_exporter_and_a_custom_outfile(self):
        ur = cf.make_user_root(self.tmp)
        orig = {"github.copilot.chat.otel.enabled": True, "github.copilot.chat.otel.exporterType": "otlp-grpc"}
        p = cf.write_settings(ur, orig); before = self.read(p)
        self.assertEqual(self.run_setup([ur], undo=True), 0)
        self.assertEqual((self.read(p), self.baks(p)), (before, []))
        ur2 = cf.make_user_root(self.tmp, variant="Other"); mine = os.path.join(self.tmp, "mine.jsonl")
        p2 = cf.write_settings(ur2, {"github.copilot.chat.otel.enabled": True,
                                     "github.copilot.chat.otel.exporterType": "file",
                                     "github.copilot.chat.otel.outfile": mine})
        self.assertEqual(self.run_setup([ur2], undo=True), 0)
        self.assertEqual(self.load(p2), {"github.copilot.chat.otel.outfile": mine})

    def test_undo_message_names_the_backup_it_made(self):
        ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, {})
        self.assertEqual(self.run_setup([ur]), 0)
        self.out.clear()
        before = set(self.baks(p))
        # the backup name has a one-second clock; if the apply's backup is the same second the
        # undo's gets a -2 suffix, so compare by what the undo added
        self.assertEqual(self.run_setup([ur], undo=True), 0)
        new = sorted(set(self.baks(p)) - before)
        self.assertEqual(len(new), 1)
        t = self.text()
        self.assertIn(os.path.join(os.path.dirname(p), new[0]), t)
        self.assertIn("settings.json.bak-aifi-*", t)
        self.assertIn("restore", t)

    def test_output_survives_a_cp1252_console(self):
        import io
        from contextlib import redirect_stdout
        ur = cf.make_user_root(os.path.join(self.tmp, "中文"))
        p = cf.write_settings(ur, {"a": 1})
        buf = io.BytesIO()
        stream = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
        with redirect_stdout(stream):
            rc = afi.run_setup_copilot([ur], input_fn=lambda _: "y", print_fn=print, home=self.tmp)
            stream.flush()
        self.assertEqual(rc, 0)
        self.assertIn(b"?", buf.getvalue())
        self.assertIn("github.copilot.chat.otel.enabled", self.read(p))
        # an explicit encoding, with a print_fn that is strict about it
        out = []

        def strict(text):
            text.encode("cp1252")                       # raises if anything non-cp1252 got through
            out.append(text)
        ur2 = cf.make_user_root(os.path.join(self.tmp, "中"), variant="Code")
        cf.write_settings(ur2, "// c")
        self.assertEqual(afi.run_setup_copilot([ur2], print_fn=strict, home=self.tmp, encoding="cp1252"), 2)
        self.assertTrue(out)

    def test_failed_write_leaves_no_tmp_and_the_original_intact(self):
        ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, {"a": 1})
        before = self.raw(p)
        with patch("os.replace", side_effect=OSError("disk says no")):
            self.assertEqual(self.run_setup([ur]), 1)
        self.assertEqual(self.raw(p), before)
        self.assertFalse(os.path.exists(p + ".tmp"))
        self.assertEqual([self.raw(os.path.join(ur, n)) for n in self.baks(p)], [before])
        self.assertIn("disk says no", self.text())

    def test_undo_flag_alone_is_an_error(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_fluency_insights.py")
        home = os.path.join(self.tmp, "userhome")
        env = dict(os.environ, HOME=home, USERPROFILE=home, APPDATA=home, XDG_CONFIG_HOME=home)
        r = subprocess.run([sys.executable, script, "--undo"], env=env, cwd=self.tmp,
                           capture_output=True, text=True, timeout=120, input="")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--undo", r.stderr)
        self.assertIn("--setup-copilot", r.stderr)

    def test_cli_flags_run_against_a_temp_root(self):
        ur = cf.make_user_root(self.tmp); p = cf.write_settings(ur, {"a": 1}); before = self.read(p)
        home, conf = os.path.join(self.tmp, "userhome"), os.path.join(self.tmp, "appdata")
        env = dict(os.environ, HOME=home, USERPROFILE=home, APPDATA=conf, XDG_CONFIG_HOME=conf,
                   CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "nope"))
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_fluency_insights.py")
        r = subprocess.run([sys.executable, script, "--setup-copilot", "--dry-run", "--copilot-root", ur],
                           env=env, cwd=self.tmp, capture_output=True, text=True, timeout=120, input="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("github.copilot.chat.otel.enabled", r.stdout)
        self.assertEqual(self.read(p), before)
        self.assertFalse(os.path.exists(home))
        r = subprocess.run([sys.executable, script, "--setup-copilot", "--undo", "--copilot-root", ur],
                           env=env, cwd=self.tmp, capture_output=True, text=True, timeout=120, input="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(p), before)


SWITCH_JS = r"""// node switch.js page.js payload.json : drive a tool switch with a stub DOM and a fetch the test controls
const vm = require('vm'), fs = require('fs');
const [,, pagePath, payloadPath] = process.argv;
const S = new Proxy(function () {}, {
  get: (t, k) => k === Symbol.toPrimitive ? () => '' : k === 'then' ? undefined : k === 'length' ? 0 : S,
  set: () => true, apply: () => S, construct: () => S, has: () => true });
const els = {};
const el = key => els[key] || (els[key] = new Proxy({}, {
  get: (t, k) => k in t ? t[k] : S, set: (t, k, v) => { t[k] = v; return true; } }));
const doc = new Proxy({}, { get: (t, k) => k === 'getElementById' || k === 'querySelector' ? el : S, set: () => true });
function Chart() { return S; }
Chart.defaults = S;
let next = null;                                   // the reply the next fetch gets; null: it never answers
const fetch = () => next ? Promise.resolve(next) : new Promise(() => {});
const errors = [];
const ctx = vm.createContext({
  document: doc, window: S, navigator: S, location: S, history: S, Blob: S, URL: S,
  console: { log() {}, warn() {}, error: (...a) => errors.push(a.join(' ')) },
  localStorage: { getItem: () => null, setItem() {} }, matchMedia: () => S, getComputedStyle: () => ({ getPropertyValue: () => '#336699' }),
  fetch, requestAnimationFrame: () => 0, cancelAnimationFrame() {}, setTimeout: () => 0, clearTimeout() {},
  setInterval: () => 0, addEventListener() {}, performance: { now: () => 0 }, Chart, Intl, Math, Date, JSON, Object, Array,
  Number, String, Set, Map, Promise });
vm.runInContext(fs.readFileSync(pagePath, 'utf8'), ctx);
const tick = () => new Promise(r => setImmediate(r));
const state = () => ({ text: els.srcstate.innerHTML || '', hidden: els.srcstate.hidden, cls: els.content.className || '' });
(async () => {
  const out = {};
  vm.runInContext("setSource('copilot')", ctx);                         // the fetch is still out
  await tick(); out.loading = state();
  next = { ok: false, status: 500, json: () => Promise.resolve({ ok: false, error: "Couldn't build the stats for GitHub Copilot",
                                                                  detail: 'ValueError: bad <uri>' }) };
  vm.runInContext("load()", ctx);
  await tick(); await tick(); out.error = state();
  const p = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
  next = { ok: true, status: 200, json: () => Promise.resolve(p) };
  vm.runInContext("load()", ctx);
  await tick(); await tick(); out.done = state();
  out.errors = errors;
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


class FinalReviewFixTests(TmpCase):
    """The final whole-branch review's fixes on the dashboard side (Critical 1, Important 3-5, Minors)."""

    def tree(self):
        """(user root, copilot home) of an empty Copilot tree."""
        return cf.make_user_root(self.tmp), os.path.join(self.tmp, "home", ".copilot")

    @staticmethod
    def transcript(ws, sid, text="do a thing", sec=0):
        ts = "2026-10-24T10:%02d:%02dZ"
        return cf.add_transcript(ws, sid, [
            {"type": "session.start", "timestamp": ts % (0, sec), "data": {"copilotVersion": "0.40"}},
            {"type": "user.message", "timestamp": ts % (1, sec), "data": {"content": text}},
            {"type": "assistant.turn_end", "timestamp": ts % (2, sec), "data": {}}])

    # -- Critical 1: one bad session never takes the view down --------------------------------------
    def test_bracket_uri_session_still_gives_a_payload(self):
        ur, home = self.tree()
        self.transcript(cf.add_workspace(ur, "h1", "/work/demo"), "good")
        bad = os.path.join(ur, "workspaceStorage", "h2")
        os.makedirs(bad)
        with open(os.path.join(bad, "workspace.json"), "w", encoding="utf-8") as f:
            json.dump({"folder": "vscode-remote://[bad/home/x"}, f)
        self.transcript(bad, "odd", sec=5)
        d = afi.build_payload(afi.CopilotScanner([ur], home), parse_qs("days=0"), source="copilot")
        self.assertEqual(d["totals"]["sessions"], 2)
        self.assertIn("(no folder)", [p["name"] for p in d["projects"]])

    def test_a_session_that_fails_to_translate_is_counted_bad_and_not_retried(self):
        ur, home = self.tree()
        ws = cf.add_workspace(ur, "h1", "/work/demo")
        self.transcript(ws, "good")
        self.transcript(ws, "bad", sec=7)
        real, calls = afi._translate_copilot_session, []

        def flaky(sid, *a):
            calls.append(sid)
            if sid == "bad":
                raise KeyError("a shape nobody expected")
            return real(sid, *a)
        sc = afi.CopilotScanner([ur], home)
        with patch.object(afi, "_translate_copilot_session", flaky):
            d = afi.build_payload(sc, parse_qs("days=0"), source="copilot")
            self.assertEqual(d["totals"]["sessions"], 1)                  # the good one is still there
            self.assertGreaterEqual(d["health"]["bad_lines"], 1)          # Data Health counts the bad one
            calls.clear()
            st = sc.build_stats()
        self.assertEqual(calls, [])                                       # nothing changed: neither is re-read
        self.assertEqual(sorted(st.sessions), ["good"])
        self.assertEqual(st.bad_lines, 1)

    def test_stats_build_error_is_a_json_500(self):
        class Broken:
            user_roots, copilot_home = (), None

            def available(self):
                return True

            def build_stats(self, *a):
                raise RuntimeError("boom")
        handler = type("TestHandler", (afi.Handler,), {"root": FIX, "scanners": {"claude": afi.Scanner(FIX),
                                                                                  "copilot": Broken()}})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = "http://127.0.0.1:%d/api/stats?days=0&source=copilot" % srv.server_address[1]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                opener.open(url, timeout=30)
        self.assertEqual(cm.exception.code, 500)
        self.assertEqual(cm.exception.headers.get("Content-Type"), "application/json")
        body = json.loads(cm.exception.read().decode())
        self.assertEqual((body["ok"], body["error"]), (False, "Couldn't build the stats for GitHub Copilot"))
        self.assertIn("RuntimeError", body["detail"])
        self.assertIn("boom", err.getvalue())                             # the traceback goes to the console
        with opener.open(url.replace("copilot", "claude"), timeout=30) as r:   # the other tool still serves
            self.assertEqual(json.loads(r.read().decode())["source"], "claude")

    # -- Important 3: the cache hit rate comes from sessions with a cache split ---------------------
    def cache_tree(self):
        ur, home = self.tree()
        ws = cf.add_workspace(ur, "h1", "/work/demo")
        # x1: exact from its debug log (5000 in of which 3000 cached -> input 2000, cache read 3000)
        cf.add_chat_jsonl(ws, "x1", [{"kind": 0, "v": {"sessionId": "x1", "requests": [
            cf.chat_request("r1", MS, "exact one", prompt=6000, completion=100)]}}])
        cf.add_debug_log(ws, "x1", [{"type": "session_start"}, cf.llm_request("sp1", MS + 2000, 5000, 400, 3000)])
        # a1: approx, same day (1000 in, no cache split); a2: approx, three days earlier (500 in)
        cf.add_chat_jsonl(ws, "a1", [{"kind": 0, "v": {"sessionId": "a1", "requests": [
            cf.chat_request("r2", MS + 60000, "approx one", prompt=1000, completion=50)]}}])
        cf.add_chat_jsonl(ws, "a2", [{"kind": 0, "v": {"sessionId": "a2", "requests": [
            cf.chat_request("r3", MS - 3 * 86400000, "approx two", prompt=500, completion=20)]}}])
        return afi.CopilotScanner([ur], home)

    def test_cache_hit_rate_counts_only_split_sessions(self):
        d = afi.build_payload(self.cache_tree(), parse_qs("days=0"), source="copilot")
        self.assertEqual(d["totals"]["cache_efficiency"], 60.0)          # 3000 / (3000 + 2000), not / 6500
        self.assertNotIn("cache_read", d["na"])

    def test_cache_hit_rate_is_na_for_a_range_without_split_sessions(self):
        day = local_day(T0 - timedelta(days=3))
        d = afi.build_payload(self.cache_tree(), parse_qs("from=%s&to=%s" % (day, day)), source="copilot")
        self.assertEqual(d["totals"]["prompts"], 1)
        self.assertIn("cache_read", d["na"])
        self.assertIn("this range", d["na"]["cache_read"])
        self.assertIsNone(d["totals"]["cache_efficiency"])               # never a made-up 0%

    # -- Important 4: All AI says Copilot's tokens are approx. --------------------------------------
    def test_all_ai_copilot_row_carries_token_modes(self):
        sc = self.cache_tree()
        d = afi.build_payload({"claude": afi.Scanner(FIX), "copilot": sc}, parse_qs("days=0"), source="all")
        self.assertEqual(d["by_tool"]["copilot"]["token_modes"], {"exact": 1, "approx": 2, "none": 0})
        self.assertNotIn("token_modes", d["by_tool"]["claude"])
        js = AllAIPageTests.script()
        block = js[js.index("function renderAllOverview"):js.index("function renderAllTools")]
        self.assertIn("token_modes", block)
        self.assertIn("' · approx.'", block)

    # -- Important 5: a project's sessions count sessions without tokens ----------------------------
    def test_transcript_only_project_counts_its_session(self):
        ur, home = self.tree()
        self.transcript(cf.add_workspace(ur, "h1", "/work/demo"), "t1")
        d = afi.build_payload(afi.CopilotScanner([ur], home), parse_qs("days=0"), source="copilot")
        self.assertEqual([(p["prompts"], p["sessions"]) for p in d["projects"]], [(1, 1)])

    # -- Minor 1: a failed session-store read is tried again ----------------------------------------
    def test_failed_session_store_read_is_not_cached(self):
        ur, home = self.tree()
        self.transcript(cf.add_workspace(ur, "h1", "/work/demo"), "t1")
        cf.add_session_store(ur, [{"id": "t1", "branch": "dev"}])
        sc = afi.CopilotScanner([ur], home)
        stores = sc.files()["stores"]
        with patch.object(cs, "_open_store", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(sc._store_rows(stores), {})
        self.assertEqual(sc._store_rows(stores)["t1"]["branch"], "dev")

    # -- Minor 3: a huge ?days= is clamped ----------------------------------------------------------
    def test_huge_days_is_clamped(self):
        self.assertEqual(afi.query_range(parse_qs("days=999999999999"))[2], 36500)
        big = afi.build_payload(afi.Scanner(FIX), parse_qs("days=999999999999"), today=date(2026, 10, 10))
        cap = afi.build_payload(afi.Scanner(FIX), parse_qs("days=36500"), today=date(2026, 10, 10))
        for d in (big, cap):
            d.pop("generated")
        self.assertEqual(big, cap)

    # -- Minor 4: --setup-copilot edits a symlinked settings.json at its target ---------------------
    def test_setup_copilot_writes_through_realpath(self):
        ur = cf.make_user_root(self.tmp)
        target = cf.write_settings(cf.make_user_root(os.path.join(self.tmp, "dotfiles")), {"editor.fontSize": 14})
        link = os.path.join(ur, "settings.json")
        real = os.path.realpath
        with patch.object(afi.os.path, "realpath",
                          lambda p, *a, **k: target if os.path.normcase(p) == os.path.normcase(link) else real(p, *a, **k)):
            afi._write_copilot_settings(link, {"editor.fontSize": 15}, "\n")
        self.assertFalse(os.path.exists(link))                            # nothing written beside the link
        with open(target, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"editor.fontSize": 15})
        self.assertTrue(any(n.startswith("settings.json.bak-aifi-") for n in os.listdir(os.path.dirname(target))))

    def test_setup_copilot_keeps_a_real_symlink(self):
        ur = cf.make_user_root(self.tmp)
        target = cf.write_settings(cf.make_user_root(os.path.join(self.tmp, "dotfiles")), {"editor.fontSize": 14})
        link = os.path.join(ur, "settings.json")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks cannot be created here")
        rc = afi.run_setup_copilot([ur], input_fn=lambda _: "y", print_fn=lambda *a: None, home=self.tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.islink(link))
        with open(target, encoding="utf-8") as f:
            self.assertTrue(json.load(f)["github.copilot.chat.otel.enabled"])

    # -- Minor 6: the interrupt marker is never a session title ------------------------------------
    def test_first_prompt_skips_the_interrupt_marker(self):
        ev = [{"type": "user", "message": {"content": "[Request interrupted by user]"}},
              {"type": "user", "message": {"content": "  the real   question "}}]
        self.assertEqual(afi._first_prompt(ev), "the real question")
        self.assertIsNone(afi._first_prompt(ev[:1]))

    # -- Minor 9 + Critical 1d: the page says what it is loading, and why it failed -----------------
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_tool_switch_shows_loading_then_error_then_clears(self):
        import re
        ur, home = self.tree()
        self.transcript(cf.add_workspace(ur, "h1", "/work/demo"), "t1")
        payload = afi.build_payload(afi.CopilotScanner([ur], home), parse_qs("days=30"), source="copilot")
        payload["sources"] = afi.sources_status({"claude": afi.Scanner(FIX), "copilot": afi.CopilotScanner([ur], home)})
        page = re.findall(r"<script>(.*?)</script>", afi.HTML, re.S)[-1]
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in (("page.js", page), ("switch.js", SWITCH_JS), ("payload.json", json.dumps(payload))):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                    f.write(text)
            r = subprocess.run(["node", os.path.join(tmp, "switch.js"), os.path.join(tmp, "page.js"),
                                os.path.join(tmp, "payload.json")], capture_output=True, text=True, encoding="utf-8",
                               timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertIn("Loading GitHub Copilot", out["loading"]["text"])
        self.assertFalse(out["loading"]["hidden"])
        self.assertIn("busy", out["loading"]["cls"])                      # the old tool's numbers are dimmed
        self.assertIn("Couldn’t build the stats for GitHub Copilot", out["error"]["text"])
        self.assertIn("ValueError: bad &lt;uri&gt;", out["error"]["text"])    # escaped
        self.assertNotIn("Can’t reach", out["error"]["text"])
        self.assertIn("busy", out["error"]["cls"])                        # still not Copilot's numbers
        self.assertTrue(out["done"]["hidden"])
        self.assertNotIn("busy", out["done"]["cls"])

    def test_page_has_the_switch_state_pieces(self):
        self.assertEqual(afi.HTML.count('id="srcstate"'), 1)
        self.assertEqual(afi.HTML.count('id="content"'), 1)
        self.assertIn("#srcstate[hidden]{display:none}", afi.HTML)


if __name__ == "__main__":
    unittest.main()
