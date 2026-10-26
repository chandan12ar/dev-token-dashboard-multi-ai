"""Tests for copilot_source.py (the pure, Copilot-only half of the dashboard).

Run with:  python -m unittest test_copilot_source -v
"""
import ast
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import unittest.mock

import ai_fluency_insights as afi          # for the round-trip check only
import copilot_fixtures as cf
import copilot_source as cs


def norm(paths):
    return [os.path.normcase(os.path.realpath(p)) for p in paths]


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cps_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class DiscoverTests(TmpCase):
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

    def test_discover_nothing_found(self):
        f = cs.discover([os.path.join(self.tmp, "no", "User")], os.path.join(self.tmp, "no", ".copilot"))
        self.assertEqual(f, {"chat": {}, "transcripts": {}, "debug": {}, "otel": [], "cli": {},
                             "stores": [], "settings": []})

    def test_discover_chat_formats_and_empty_window(self):
        ur = cf.make_user_root(self.tmp)
        ws = cf.add_workspace(ur, "h1", "/work/demo")
        p_jsonl = cf.add_chat_jsonl(ws, "new1", [{"kind": 0, "v": {"requests": []}}])
        p_json = cf.add_chat_json(ws, "old1", {"requests": []})
        p_empty = cf.add_empty_window_chat(ur, "e1", {"requests": []})
        f = cs.discover([ur], os.path.join(self.tmp, "home"))
        self.assertEqual(set(f["chat"]), {"new1", "old1", "e1"})
        self.assertEqual(norm([f["chat"]["new1"]["path"]]), norm([p_jsonl]))
        self.assertEqual(norm([f["chat"]["old1"]["path"]]), norm([p_json]))
        self.assertEqual(norm([f["chat"]["e1"]["path"]]), norm([p_empty]))
        self.assertEqual(f["chat"]["e1"]["workspace"], "")        # no folder was open
        self.assertEqual(f["chat"]["old1"]["workspace"], ws)

    def test_discover_ignores_foreign_files(self):
        ur = cf.make_user_root(self.tmp)
        ws = cf.add_workspace(ur, "h1", "/work/demo")
        cf.add_chat_json(ws, "c1", {})
        with open(os.path.join(ws, "chatSessions", "notes.txt"), "w") as fh:
            fh.write("x")
        f = cs.discover([ur], os.path.join(self.tmp, "home"))
        self.assertEqual(set(f["chat"]), {"c1"})

    def test_discover_debug_logs_keep_every_file_per_session(self):
        ur = cf.make_user_root(self.tmp)
        ws = cf.add_workspace(ur, "h1", "/work/demo")
        main = cf.add_debug_log(ws, "s1", [{"type": "session_start"}])
        extra = os.path.join(os.path.dirname(main), "sub-agent.jsonl")
        with open(extra, "w", encoding="utf-8") as fh:
            fh.write('{"type": "session_start"}\n')
        f = cs.discover([ur], os.path.join(self.tmp, "home"))
        self.assertEqual(sorted(norm(f["debug"]["s1"])), sorted(norm([main, extra])))

    def test_discover_cli_sid_is_folder_name(self):
        home = os.path.join(self.tmp, "home", ".copilot")
        p = cf.add_cli_session(home, "abc-123", [{"type": "session.start"}])
        f = cs.discover([], home)
        self.assertEqual(norm([f["cli"]["abc-123"]]), norm([p]))
        self.assertEqual(os.path.basename(p), "events.jsonl")

    def test_discover_otel_dir_under_copilot_home(self):
        home = os.path.join(self.tmp, "home", ".copilot")
        p = cf.add_otel(os.path.join(home, "otel", "vscode-copilot-otel.jsonl"), [])
        f = cs.discover([], home)
        self.assertEqual(norm(f["otel"]), norm([p]))

    def test_otel_outfile_read_from_jsonc_settings(self):
        ur = cf.make_user_root(self.tmp)
        out = cf.add_otel(os.path.join(self.tmp, "my traces", "t.jsonl"), [])
        text = ('{\n  // my settings\n  "editor.fontSize": 14,\n'
                '  "github.copilot.chat.otel.outfile": %s,\n}\n' % json.dumps(out))
        cf.write_settings(ur, text)
        f = cs.discover([ur], os.path.join(self.tmp, "home"))
        self.assertEqual(norm(f["otel"]), norm([out]))

    def test_otel_outfile_missing_file_is_skipped(self):
        ur = cf.make_user_root(self.tmp)
        cf.write_settings(ur, {"github.copilot.chat.otel.outfile": os.path.join(self.tmp, "gone.jsonl")})
        self.assertEqual(cs.discover([ur], os.path.join(self.tmp, "home"))["otel"], [])

    def test_discover_dedupes_repeated_files(self):
        ur = cf.make_user_root(self.tmp)
        cf.add_session_store(ur, [])
        p = cf.write_settings(ur, {"editor.fontSize": 14})
        f = cs.discover([ur, ur], os.path.join(self.tmp, "home"))
        self.assertEqual(len(f["stores"]), 1)
        self.assertEqual(norm(f["settings"]), norm([p]))

    def test_discover_walks_profiles_listed_as_roots(self):
        ur = cf.make_user_root(self.tmp)
        prof = os.path.join(ur, "profiles", "p1"); os.makedirs(prof)
        ws = cf.add_workspace(prof, "h9", "/work/other")
        cf.add_chat_jsonl(ws, "pc1", [{"kind": 0, "v": {"requests": []}}])
        f = cs.discover(cs.find_user_roots(home=self.tmp, appdata=self.tmp), os.path.join(self.tmp, "home"))
        self.assertEqual(set(f["chat"]), {"pc1"})


class FindUserRootsTests(TmpCase):
    def test_find_user_roots_includes_profiles(self):
        ur = cf.make_user_root(self.tmp)
        prof = os.path.join(ur, "profiles", "p1"); os.makedirs(prof)
        roots = cs.find_user_roots(home=self.tmp, appdata=self.tmp)
        self.assertEqual(norm(roots), norm([ur, prof]))

    def test_find_user_roots_covers_variants_and_remote(self):
        a = cf.make_user_root(self.tmp, "Code")
        b = cf.make_user_root(self.tmp, "Code - Insiders")
        c = cf.make_user_root(self.tmp, "VSCodium")
        remote = os.path.join(self.tmp, ".vscode-server", "data", "User"); os.makedirs(remote)
        roots = cs.find_user_roots(home=self.tmp, appdata=self.tmp)
        self.assertEqual(sorted(norm(roots)), sorted(norm([a, b, c, remote])))

    def test_find_user_roots_skips_missing_and_dedupes_extra(self):
        ur = cf.make_user_root(self.tmp)
        roots = cs.find_user_roots(extra=[ur, os.path.join(self.tmp, "nope")],
                                   home=self.tmp, appdata=self.tmp)
        self.assertEqual(norm(roots), norm([ur]))

    def test_find_user_roots_adds_extra_with_its_profiles(self):
        extra = os.path.join(self.tmp, "portable", "User")
        os.makedirs(os.path.join(extra, "profiles", "x"))
        roots = cs.find_user_roots(extra=[extra], home=self.tmp, appdata=os.path.join(self.tmp, "empty"))
        self.assertEqual(norm(roots), norm([extra, os.path.join(extra, "profiles", "x")]))


class ToolVocabularyTests(unittest.TestCase):
    def test_tool_name_sets(self):
        self.assertEqual(cs.COPILOT_READ_TOOLS, {
            "read_file", "grep_search", "file_search", "semantic_search", "list_dir",
            "fetch_webpage", "get_errors", "view", "grep", "glob", "web_fetch"})
        self.assertEqual(cs.COPILOT_WRITE_TOOLS, {
            "create_file", "replace_string_in_file", "multi_replace_string_in_file", "apply_patch",
            "insert_edit_into_file", "edit_notebook_file", "edit", "create", "str_replace_editor"})
        self.assertEqual(cs.COPILOT_CMD_TOOLS,
                         {"run_in_terminal", "bash", "powershell", "shell", "write_bash"})
        self.assertEqual(cs.COPILOT_TOOL_FEATURES, {
            "run_in_terminal": "verify", "bash": "verify", "powershell": "verify",
            "fetch_webpage": "web", "web_fetch": "web", "runSubagent": "subagents",
            "task": "subagents", "manage_todo_list": "tasks", "update_todo": "tasks"})


class ToolLocTests(unittest.TestCase):
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

    def test_remaining_tool_shapes(self):
        self.assertEqual(cs.tool_loc("create", {"path": "h.py", "file_text": "1\n2"}), (2, "h.py"))
        self.assertEqual(cs.tool_loc("str_replace_editor", {"path": "i.py", "new_str": "a\nb\nc"}), (3, "i.py"))
        self.assertEqual(cs.tool_loc("edit_notebook_file", {"filePath": "n.ipynb", "newCode": "x\ny"}), (2, "n.ipynb"))

    def test_apply_patch_forms(self):
        txt = ("*** Begin Patch\n*** Add File: new.py\n+a\n+b\n+++ not a line\n"
               "*** Update File: later.py\n+c\n*** End Patch")
        self.assertEqual(cs.tool_loc("apply_patch", txt), (3, "new.py"))         # raw string, first path wins, +++ skipped
        self.assertEqual(cs.tool_loc("apply_patch", {"input": "+x"}), (1, ""))   # no file line, no path

    def test_blank_and_malformed_arguments_count_nothing(self):
        self.assertEqual(cs.tool_loc("create_file", {}), (0, ""))
        self.assertEqual(cs.tool_loc("create_file", {"filePath": "z.py", "content": ""}), (0, "z.py"))
        self.assertEqual(cs.tool_loc("create_file", {"content": 5}), (0, ""))
        self.assertEqual(cs.tool_loc("create_file", None), (0, ""))
        self.assertEqual(cs.tool_loc("multi_replace_string_in_file", {"replacements": "oops"}), (0, ""))
        self.assertEqual(cs.tool_loc("multi_replace_string_in_file", {"replacements": [None, {"newString": "k"}]}), (1, ""))
        self.assertEqual(cs.tool_loc("unknown_tool", {"filePath": "q.py", "content": "a\nb"}), (0, "q.py"))


class ChatReplayTests(TmpCase):
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

    def test_set_creates_dicts_and_pads_lists(self):
        sess, bad = cs.replay_chat_jsonl([json.dumps(x) for x in (
            {"kind": 0, "v": {"requests": []}},
            {"kind": 1, "k": ["inputState", "mode", "kind"], "v": "agent"},
            {"kind": 1, "k": ["requests", 2, "result"], "v": {"details": "d"}})])
        self.assertEqual(sess["inputState"], {"mode": {"kind": "agent"}})
        self.assertEqual(sess["requests"], [None, None, {"result": {"details": "d"}}])
        self.assertEqual(bad, 0)

    def test_set_to_null_and_overwrite(self):
        sess, bad = cs.replay_chat_jsonl([json.dumps(x) for x in (
            {"kind": 0, "v": {"a": 1, "b": {"c": 2}}},
            {"kind": 1, "k": ["a"], "v": None},
            {"kind": 1, "k": ["b", "c"], "v": 3})])
        self.assertEqual((sess, bad), ({"a": None, "b": {"c": 3}}, 0))

    def test_push_creates_missing_list_and_appends_non_list(self):
        sess, bad = cs.replay_chat_jsonl([json.dumps(x) for x in (
            {"kind": 0, "v": {}},
            {"kind": 2, "k": ["requests", 0, "response"], "v": [{"kind": "a"}]},
            {"kind": 2, "k": ["requests", 0, "response"], "v": {"kind": "b"}})])
        self.assertEqual(sess["requests"][0]["response"], [{"kind": "a"}, {"kind": "b"}])
        self.assertEqual(bad, 0)

    def test_splice_index_past_the_end_leaves_the_list_alone(self):
        sess, bad = cs.replay_chat_jsonl([json.dumps(x) for x in (
            {"kind": 0, "v": {"xs": [1, 2]}}, {"kind": 2, "k": ["xs"], "i": 9, "v": [3]})])
        self.assertEqual((sess["xs"], bad), ([1, 2, 3], 0))
        sess, bad = cs.replay_chat_jsonl([json.dumps(x) for x in (
            {"kind": 0, "v": {"xs": [1, 2]}}, {"kind": 2, "k": ["xs"], "i": 0, "v": []})])
        self.assertEqual((sess["xs"], bad), ([], 0))

    def test_delete_dict_key_and_list_item(self):
        sess, bad = cs.replay_chat_jsonl([json.dumps(x) for x in (
            {"kind": 0, "v": {"a": 1, "b": 2, "xs": [10, 20, 30]}},
            {"kind": 3, "k": ["a"]},
            {"kind": 3, "k": ["xs", 1]},
            {"kind": 3, "k": ["nope", "deeper"]},          # nothing there: a no-op, not an error
            {"kind": 3, "k": ["xs", 99]})])
        self.assertEqual((sess, bad), ({"b": 2, "xs": [10, 30]}, 0))

    def test_a_later_snapshot_replaces_the_session(self):
        sess, bad = cs.replay_chat_jsonl([json.dumps(x) for x in (
            {"kind": 0, "v": {"sessionId": "old"}}, {"kind": 0, "v": {"sessionId": "new"}})])
        self.assertEqual((sess, bad), ({"sessionId": "new"}, 0))

    def test_blank_lines_are_not_bad_but_junk_is(self):
        lines = ["", json.dumps({"kind": 0, "v": {"n": 1}}), "   \n", "[1, 2]", "not json", '"str"',
                 json.dumps({"kind": 9, "k": ["n"], "v": 1}), json.dumps({"kind": 1, "k": ["n"], "v": 2})]
        sess, bad = cs.replay_chat_jsonl(lines)
        self.assertEqual((sess, bad), ({"n": 2}, 4))             # list, junk, string, unknown kind

    def test_operations_that_cannot_apply_are_counted_and_skipped(self):
        lines = [json.dumps(x) for x in (
            {"kind": 0, "v": {"n": 1, "xs": [1], "s": "text"}},
            {"kind": 1, "k": ["xs", "name"], "v": 1},          # a name into a list
            {"kind": 1, "k": ["n", "deeper"], "v": 1},         # into a number
            {"kind": 2, "k": ["s"], "v": [1]},                 # push onto a string
            {"kind": 2, "k": ["xs"], "i": "x", "v": [1]},      # splice index is not a number
            {"kind": 1, "k": [], "v": 1}, {"kind": 1, "k": "n", "v": 1}, {"kind": 1, "v": 1},
            {"kind": 1, "k": ["n"]},                           # no value
            {"kind": 1, "k": ["xs", 10 ** 9], "v": 1},         # absurd padding
            {"kind": 0, "v": [1]},                             # a snapshot must be an object
            {"kind": 1, "k": ["n"], "v": 5})]
        sess, bad = cs.replay_chat_jsonl(lines)
        self.assertEqual((sess, bad), ({"n": 5, "xs": [1], "s": "text"}, 10))

    def test_operations_before_any_snapshot_are_counted(self):
        sess, bad = cs.replay_chat_jsonl([json.dumps({"kind": 1, "k": ["a"], "v": 1}), "{"])
        self.assertEqual((sess, bad), (None, 2))
        self.assertEqual(cs.replay_chat_jsonl([]), (None, 0))

    def test_load_chat_session_reads_both_formats(self):
        ur = cf.make_user_root(self.tmp); ws = cf.add_workspace(ur, "h1", "/work/demo")
        req = cf.chat_request("r1", 1759320000000, "hello")
        j = cf.add_chat_json(ws, "old", {"sessionId": "old", "requests": [req]})
        jl = cf.add_chat_jsonl(ws, "new", [{"kind": 0, "v": {"sessionId": "new", "requests": []}},
                                          {"kind": 2, "k": ["requests"], "v": [req]}])
        self.assertEqual(cs.load_chat_session(j), ({"sessionId": "old", "requests": [req]}, 0))
        self.assertEqual(cs.load_chat_session(jl), ({"sessionId": "new", "requests": [req]}, 0))

    def test_load_chat_session_survives_broken_files(self):
        ur = cf.make_user_root(self.tmp); ws = cf.add_workspace(ur, "h1", "/work/demo")
        half = os.path.join(ws, "chatSessions", "half.json")
        os.makedirs(os.path.dirname(half), exist_ok=True)
        with open(half, "w", encoding="utf-8") as fh:
            fh.write('{"requests": [{"requestId"')
        self.assertEqual(cs.load_chat_session(half), (None, 1))
        listy = cf.add_chat_json(ws, "listy", [1, 2])
        self.assertEqual(cs.load_chat_session(listy), (None, 1))
        self.assertEqual(cs.load_chat_session(os.path.join(ws, "chatSessions", "missing.jsonl")), (None, 1))
        raw = os.path.join(ws, "chatSessions", "raw.jsonl")
        with open(raw, "wb") as fh:
            fh.write(b'\xef\xbb\xbf{"kind": 0, "v": {"n": "\xff"}}\n{"kind": 1, "k": ["n"]\n')
        sess, bad = cs.load_chat_session(raw)                # BOM and bad UTF-8 are tolerated
        self.assertEqual((sorted(sess), bad), (["n"], 1))


class ChatHelperTests(unittest.TestCase):
    def test_iso_utc_formats(self):                            # Review Focus 3
        self.assertEqual(cs.iso_utc(1759320000000), "2025-10-01T12:00:00.000+00:00")
        self.assertEqual(cs.iso_utc("2025-10-01T12:00:00Z"), "2025-10-01T12:00:00.000+00:00")
        self.assertIsNone(cs.iso_utc("garbage"))

    def test_iso_utc_more_inputs(self):
        self.assertEqual(cs.iso_utc(1759320000123.0), "2025-10-01T12:00:00.123+00:00")
        self.assertEqual(cs.iso_utc("2025-10-01T17:30:00.5+05:30"), "2025-10-01T12:00:00.500+00:00")
        self.assertEqual(cs.iso_utc("2025-10-01T12:00:00.123456789Z"), "2025-10-01T12:00:00.123+00:00")
        self.assertEqual(cs.iso_utc("2025-10-01 12:00:00"), "2025-10-01T12:00:00.000+00:00")   # no zone: UTC
        self.assertEqual(cs.iso_utc("2025-10-01T14:00:00+0200"), "2025-10-01T12:00:00.000+00:00")
        self.assertEqual(cs.iso_utc(0), "1970-01-01T00:00:00.000+00:00")
        # a request just before local midnight in UTC+5:30 is still the earlier UTC day
        self.assertEqual(cs.iso_utc("2025-10-02T00:10:00+05:30"), "2025-10-01T18:40:00.000+00:00")

    def test_iso_utc_rejects_what_is_not_a_time(self):
        for bad in (None, "", "   ", True, False, [], {}, float("nan"), float("inf"), 1e30, -1e30, "1759320000000"):
            self.assertIsNone(cs.iso_utc(bad), repr(bad))

    def test_normalize_model(self):
        self.assertEqual(cs.normalize_model("copilot/GPT-5.6-Luna"), "gpt-5.6-luna")
        self.assertEqual(cs.normalize_model("gpt-5.6-luna"), "gpt-5.6-luna")
        self.assertEqual(cs.normalize_model("  Claude-Opus-4.5 "), "claude-opus-4.5")
        for none in ("auto", "copilot/auto", "Auto", "", "copilot/", None, 5, {}):
            self.assertIsNone(cs.normalize_model(none), repr(none))

    def test_normalize_model_gives_one_name_per_model(self):
        # the real spellings of one model: a dated id, a dotted id, the prefixed request id
        for raw in ("claude-haiku-4-5-20251001", "claude-haiku-4.5", "copilot/claude-haiku-4.5",
                    "Claude-Haiku-4-5", "claude-haiku-4-5-2025-10-01"):
            self.assertEqual(cs.normalize_model(raw), "claude-haiku-4.5", raw)
        for raw in ("claude-haiku-5-5", "claude-haiku-5.5", "copilot/claude-haiku-5.5"):
            self.assertEqual(cs.normalize_model(raw), "claude-haiku-5.5", raw)
        for raw in ("gpt-5.5-2026-04-23", "gpt-5.5", "copilot/gpt-5.5", "gpt-5.5-20260423"):
            self.assertEqual(cs.normalize_model(raw), "gpt-5.5", raw)
        self.assertEqual(cs.normalize_model("claude-opus-4-1-20250805"), "claude-opus-4.1")
        self.assertEqual(cs.normalize_model("claude-sonnet-4-20250514"), "claude-sonnet-4")

    def test_normalize_model_leaves_other_ids_alone(self):
        for raw in ("claude-sonnet-5", "gpt-6-luna", "gpt-5.6-luna", "mai-code-1.1-flash", "claude-3-5-sonnet",
                    "claude-3-7-sonnet", "gpt-4.1", "o4-mini", "gemini-2.5-pro"):
            self.assertEqual(cs.normalize_model(raw), raw, raw)
        self.assertIsNone(cs.normalize_model("copilot/auto"))
        self.assertIsNone(cs.normalize_model("-20251001"))              # a date alone is not a model

    def test_parse_credits(self):
        self.assertEqual(cs.parse_credits("GPT-5.6 Luna • 0.5 credits"), ("credits", 0.5))
        self.assertEqual(cs.parse_credits("Claude Opus 4.5 • 3x"), ("premium", 3.0))
        self.assertIsNone(cs.parse_credits("GPT-4.1"))

    def test_parse_credits_more_shapes(self):
        self.assertEqual(cs.parse_credits("GPT-4.1 • 0x"), ("premium", 0.0))
        self.assertEqual(cs.parse_credits("Claude Sonnet • 1.5x"), ("premium", 1.5))
        self.assertEqual(cs.parse_credits("Model • 1 credit"), ("credits", 1.0))
        self.assertEqual(cs.parse_credits("Model • 12 Credits • 2.1s"), ("credits", 12.0))
        self.assertEqual(cs.parse_credits("0.25 credits"), ("credits", 0.25))
        for none in (None, "", 5, "GPT-4.1 • fast", "GPT 4x", "Model • credits", "Model • x credits",
                     "Model • -1x"):
            self.assertIsNone(cs.parse_credits(none), repr(none))

    def test_chat_session_meta(self):
        sess = {"creationDate": 1759320000000, "customTitle": "Fix the build",
                "inputState": {"mode": {"kind": "agent"}}}
        self.assertEqual(cs.chat_session_meta(sess),
                         {"mode": "agent", "title": "Fix the build", "creation": "2025-10-01T12:00:00.000+00:00"})
        self.assertEqual(cs.chat_session_meta({}), {"mode": None, "title": None, "creation": None})
        odd = {"inputState": {"mode": "agent"}, "customTitle": "", "creationDate": "x"}
        self.assertEqual(cs.chat_session_meta(odd), {"mode": None, "title": None, "creation": None})
        self.assertEqual(cs.chat_session_meta(None), {"mode": None, "title": None, "creation": None})


T0 = 1759320000000          # 2025-10-01T12:00:00Z


def _round(offset_ms, calls, model="gpt-5.6-luna"):
    return {"timestamp": T0 + offset_ms, "modelId": model, "toolCalls": calls}


def _call(cid, name, args):
    return {"id": cid, "name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}


def _events(req, with_usage=True, sid="s1"):
    return cs.chat_session_to_events({"requests": [req]}, sid, "work/demo", "/work/demo", "main", with_usage)


class ChatTranslateTests(unittest.TestCase):
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

    def test_event_fields(self):
        req = cf.chat_request("r1", T0, "fix it", prompt=1200, completion=300, credits=0.5, elapsed=4000,
            rounds=[_round(1000, [_call("c1", "apply_patch", {"input": "*** Update File: a.py\n+x\n+y"}),
                                  _call("c2", "read_file", {"filePath": "b.py"})]),
                    _round(2000, [])])
        ev = _events(req)
        self.assertEqual([e["type"] for e in ev], ["user", "assistant", "user", "user", "assistant", "assistant"])
        user, r0, res1, res2, r1, final = ev
        self.assertEqual(user, {"type": "user", "timestamp": "2025-10-01T12:00:00.000+00:00", "sessionId": "s1",
                                "cwd": "/work/demo", "gitBranch": "main", "message": {"content": "fix it"}})
        self.assertEqual((r0["timestamp"], r0["sessionId"], r0["requestId"], r0["cwd"], r0["gitBranch"]),
                         ("2025-10-01T12:00:01.000+00:00", "s1", "r1", "/work/demo", "main"))
        self.assertNotIn("isSidechain", r0)
        self.assertEqual(r0["message"]["id"], "s1:r1:r0")
        self.assertEqual((r0["message"]["model"], r0["message"]["usage"], r0["message"]["stop_reason"]),
                         ("gpt-5.6-luna", {}, None))
        self.assertNotIn("_cost_override", r0)
        tu1, tu2 = r0["message"]["content"]
        self.assertEqual((tu1["type"], tu1["id"], tu1["name"]), ("tool_use", "c1", "apply_patch"))
        self.assertEqual(tu1["input"], {"input": "*** Update File: a.py\n+x\n+y", "_loc": 2, "file_path": "a.py"})
        self.assertEqual(tu2["input"], {"filePath": "b.py", "_loc": 0, "file_path": "b.py"})
        # results ride back on user entries stamped at the next round, one block each
        self.assertEqual(res1, {"type": "user", "timestamp": "2025-10-01T12:00:02.000+00:00", "sessionId": "s1",
                                "message": {"content": [{"type": "tool_result", "tool_use_id": "c1",
                                                         "is_error": False, "content": ""}]}})
        self.assertEqual(res2["message"]["content"][0]["tool_use_id"], "c2")
        self.assertEqual((r1["message"]["id"], r1["message"]["content"]), ("s1:r1:r1", []))
        self.assertEqual((final["message"]["id"], final["requestId"], final["sessionId"]), ("s1:r1:final", "r1", "s1"))
        self.assertEqual((final["cwd"], final["gitBranch"], final["message"]["content"]), ("/work/demo", "main", []))
        self.assertEqual(final["message"]["usage"], {"input_tokens": 1200, "output_tokens": 300,
                                                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
        ids = [e["message"]["id"] for e in ev if e["type"] == "assistant"]
        self.assertEqual(len(ids), len(set(ids)))

    def test_last_round_results_land_at_the_request_end(self):
        req = cf.chat_request("r1", T0, "go", elapsed=5000, rounds=[_round(1000, [_call("c1", "read_file", {})])])
        ev = _events(req)
        results = [e for e in ev if e["type"] == "user" and isinstance(e["message"]["content"], list)]
        self.assertEqual([e["timestamp"] for e in results], ["2025-10-01T12:00:05.000+00:00"])
        self.assertEqual(ev[-1]["timestamp"], "2025-10-01T12:00:05.000+00:00")

    def test_round_without_timestamp_uses_the_request_time(self):
        req = cf.chat_request("r1", T0, "go", elapsed=1000,
                              rounds=[{"modelId": "m", "toolCalls": [_call("c1", "read_file", {})]}])
        self.assertEqual(_events(req)[1]["timestamp"], "2025-10-01T12:00:00.000+00:00")

    def test_tool_errors_are_flagged_from_the_result_text(self):
        req = cf.chat_request("r1", T0, "go", elapsed=3000, rounds=[_round(1000, [
            _call("ok", "read_file", {}), _call("err", "run_in_terminal", {"command": "pytest"}),
            _call("patch", "apply_patch", {}), _call("plain", "create_file", {}), _call("none", "list_dir", {})])])
        req["result"]["metadata"]["toolCallResults"] = {
            "ok": {"$mid": 20, "content": [{"$mid": 21, "value": "file text, with an ERROR later"}]},
            "err": {"$mid": 20, "content": [{"$mid": 21, "value": "  ERROR: command not found"}]},
            "patch": {"$mid": 20, "content": [{"$mid": 21, "value": "x"},
                                              {"$mid": 21, "value": "Applying patch failed: bad hunk"}]},
            "plain": "ERROR: as a bare string"}
        flags = {e["message"]["content"][0]["tool_use_id"]: e["message"]["content"][0]["is_error"]
                 for e in _events(req) if isinstance(e["message"]["content"], list)
                 and e["message"]["content"] and e["message"]["content"][0]["type"] == "tool_result"}
        self.assertEqual(flags, {"ok": False, "err": True, "patch": True, "plain": True, "none": False})
        st = afi.Stats()
        for e in _events(req):
            afi.parse_entry(e, "work/demo", st)
        self.assertEqual(st.tool_errors, 3)

    def test_calls_without_ids_or_names_or_json_still_pair_up(self):
        req = cf.chat_request("r1", T0, "go", elapsed=2000, rounds=[_round(500, [
            {"name": "read_file", "arguments": "{not json"}, {"id": "c2", "arguments": None}, "junk", None,
            {"id": "c3", "name": "apply_patch", "arguments": "*** Update File: z.py\n+a\n+b\n+c"},
            {"id": "c4", "name": "create_file", "arguments": {"filePath": "n.py", "content": "p\nq"}}])])
        r0 = _events(req)[1]
        blocks = r0["message"]["content"]
        self.assertEqual([b["name"] for b in blocks], ["read_file", "?", "apply_patch", "create_file"])
        self.assertEqual(blocks[0]["id"], "s1:r1:r0:c0")
        self.assertEqual(blocks[0]["input"], {"_loc": 0, "file_path": ""})
        self.assertEqual(blocks[2]["input"], {"_loc": 3, "file_path": "z.py"})
        self.assertEqual(blocks[3]["input"], {"filePath": "n.py", "content": "p\nq", "_loc": 2, "file_path": "n.py"})
        results = [e["message"]["content"][0]["tool_use_id"] for e in _events(req)
                   if isinstance(e["message"]["content"], list) and e["message"]["content"]
                   and e["message"]["content"][0]["type"] == "tool_result"]
        self.assertEqual(results, [b["id"] for b in blocks])

    def test_final_model_chain(self):
        rounds = [_round(100, [], model="copilot/GPT-5.6-Mid"), _round(200, [], model=None)]
        self.assertEqual(_events(cf.chat_request("r", T0, "x", model="copilot/Claude-Opus-4.5", rounds=rounds))[-1]
                         ["message"]["model"], "claude-opus-4.5")
        self.assertEqual(_events(cf.chat_request("r", T0, "x", model="copilot/auto", rounds=rounds))[-1]
                         ["message"]["model"], "gpt-5.6-mid")
        req = cf.chat_request("r", T0, "x", model="auto")
        self.assertEqual(_events(req)[-1]["message"]["model"], "unknown")
        req["result"]["metadata"]["resolvedModel"] = "GPT-5.6-Luna"
        self.assertEqual(_events(req)[-1]["message"]["model"], "gpt-5.6-luna")
        no_model = cf.chat_request("r", T0, "x", rounds=[_round(1, [], model=None)])
        self.assertEqual(_events(no_model)[1]["message"]["model"], "unknown")

    def test_token_fallback_chain(self):
        def usage(req):
            u = _events(req)[-1]["message"]["usage"]
            return u["input_tokens"], u["output_tokens"], u["cache_read_input_tokens"], u["cache_creation_input_tokens"]
        self.assertEqual(usage(cf.chat_request("r", T0, "x", prompt=10, completion=5)), (10, 5, 0, 0))
        req = cf.chat_request("r", T0, "x")
        self.assertEqual(_events(req)[-1]["message"]["usage"], {})        # no tokens anywhere: no usage at all
        req["result"]["usage"] = {"promptTokens": 30, "completionTokens": 7}
        self.assertEqual(usage(req), (30, 7, 0, 0))
        req["result"]["metadata"].update({"promptTokens": 20, "outputTokens": 6})
        self.assertEqual(usage(req), (20, 6, 0, 0))
        req["promptTokens"], req["completionTokens"] = 10, 5
        self.assertEqual(usage(req), (10, 5, 0, 0))
        req["promptTokens"], req["completionTokens"] = 0, True            # a zero, or a non-number, falls through
        self.assertEqual(usage(req), (20, 6, 0, 0))
        req["promptTokens"] = 12.0
        self.assertEqual(usage(req)[0], 12)

    def test_one_side_of_the_tokens_is_enough_for_usage(self):
        self.assertEqual(_events(cf.chat_request("r", T0, "x", prompt=10))[-1]["message"]["usage"],
                         {"input_tokens": 10, "output_tokens": 0,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
        self.assertEqual(_events(cf.chat_request("r", T0, "x", completion=4))[-1]["message"]["usage"]["output_tokens"], 4)

    def test_without_usage_only_credits_survive(self):
        req = cf.chat_request("r1", T0, "x", prompt=10, completion=5, credits=0.5, details="M • 3x", elapsed=1000)
        final = _events(req, with_usage=False)[-1]
        self.assertEqual((final["message"]["usage"], final["_cost_override"], final["_premium"]), ({}, 0.5, 3.0))
        st = afi.Stats()
        for e in _events(req, with_usage=False):
            afi.parse_entry(e, "work/demo", st)
        day = next(iter(st.days.values()))
        self.assertEqual((day["cost"], day["input"], day["output"]), (0.5, 0, 0))

    def test_credits_and_premium_sources(self):
        def final(**kw):
            return _events(cf.chat_request("r", T0, "x", **kw))[-1]
        f = final(credits=0.25, details="M • 9 credits")
        self.assertEqual(f["_cost_override"], 0.25)                       # copilotCredits wins
        self.assertNotIn("_premium", f)
        f = final(details="GPT-5.6 Luna • 0.5 credits")
        self.assertEqual(f["_cost_override"], 0.5)
        self.assertNotIn("_premium", f)
        f = final(details="Claude Opus 4.5 • 3x")
        self.assertEqual(f["_premium"], 3.0)
        self.assertNotIn("_cost_override", f)                              # a premium count is not credits
        f = final(credits=0, details="GPT-4.1 • 0x")
        self.assertEqual((f["_cost_override"], f["_premium"]), (0, 0.0))   # a zero is a real value
        f = final(credits="0.5", details="M • 2 credits")                  # not numeric: fall back to the details
        self.assertEqual(f["_cost_override"], 2.0)
        for none in (final(), final(credits=True), final(credits=float("nan")), final(credits=-1, details="plain")):
            self.assertNotIn("_cost_override", none)
            self.assertNotIn("_premium", none)

    def test_tokens_without_credits_are_never_priced_in_dollars(self):
        req = cf.chat_request("r1", T0, "x", prompt=100000, completion=5000, elapsed=1000)
        final = _events(req)[-1]
        self.assertEqual(final["message"]["usage"]["input_tokens"], 100000)
        self.assertEqual(final["_cost_override"], 0.0)
        self.assertIs(final["_no_credits"], True)
        st = afi.Stats()
        for e in _events(req):
            afi.parse_entry(e, "work/demo", st)
        day = next(iter(st.days.values()))
        self.assertEqual((day["cost"], day["input"], day["output"]), (0.0, 100000, 5000))
        self.assertEqual(sum(m["cost"] for m in st.models.values()), 0.0)
        self.assertEqual(sum(s["cost"] for s in st.sessions.values()), 0.0)

    def test_known_credits_carry_no_missing_credits_marker(self):
        for kw in ({"credits": 0.5}, {"credits": 0}, {"details": "GPT-5.6 Luna • 0.5 credits"}):
            final = _events(cf.chat_request("r", T0, "x", prompt=10, completion=5, **kw))[-1]
            self.assertNotIn("_no_credits", final)
            self.assertIn("_cost_override", final)

    def test_a_premium_multiplier_is_not_credits(self):
        final = _events(cf.chat_request("r", T0, "x", prompt=10, completion=5, details="Claude Opus 4.5 • 3x"))[-1]
        self.assertEqual((final["_cost_override"], final["_no_credits"], final["_premium"]), (0.0, True, 3.0))

    def test_only_events_with_usage_get_the_credits_keys(self):
        req = cf.chat_request("r", T0, "x", prompt=10, completion=5, elapsed=2000,
                              rounds=[_round(500, [_call("c1", "read_file", {})])])
        for e in _events(req)[:-1]:
            self.assertNotIn("_cost_override", e)
            self.assertNotIn("_no_credits", e)
        self.assertNotIn("_no_credits", _events(req, with_usage=False)[-1])       # no usage: nothing to price
        self.assertNotIn("_cost_override", _events(req, with_usage=False)[-1])

    def test_no_tokens_means_no_usage_and_credits_land_as_cost_only(self):
        req = cf.chat_request("r1", T0, "x", credits=0.5, elapsed=1000)
        final = _events(req)[-1]
        self.assertEqual((final["message"]["usage"], final["_cost_override"]), ({}, 0.5))
        self.assertNotIn("_no_credits", final)
        st = afi.Stats()
        for e in _events(req):
            afi.parse_entry(e, "work/demo", st)
        day = next(iter(st.days.values()))
        self.assertEqual((day["cost"], day["input"], day["output"]), (0.5, 0, 0))
        self.assertEqual(st.hc["assistant_msgs"], 0)
        self.assertEqual(sum(m["msgs"] for m in st.models.values()), 0)
        self.assertEqual(st.sessions["s1"]["api_calls"], 0)
        self.assertEqual(sum(m["cost"] for m in st.models.values()), 0.5)

    def test_no_tokens_and_no_credits_contributes_nothing(self):
        req = cf.chat_request("r1", T0, "x", elapsed=1000)
        final = _events(req)[-1]
        self.assertEqual(final["message"]["usage"], {})
        self.assertNotIn("_cost_override", final)
        self.assertNotIn("_no_credits", final)
        st = afi.Stats()
        for e in _events(req):
            afi.parse_entry(e, "work/demo", st)
        day = next(iter(st.days.values()))
        self.assertEqual((day["cost"], day["input"], day["output"], st.hc["assistant_msgs"]), (0, 0, 0, 0))

    def test_final_event_never_precedes_the_last_round(self):
        req = cf.chat_request("r1", T0, "go",
                              rounds=[_round(1000, [_call("c1", "read_file", {})]),
                                      _round(3000, [_call("c2", "read_file", {})])])        # no elapsed at all
        ev = _events(req)
        self.assertEqual(ev[-1]["timestamp"], "2025-10-01T12:00:03.000+00:00")
        results = [e for e in ev if e["type"] == "user" and isinstance(e["message"]["content"], list)]
        self.assertEqual([e["timestamp"] for e in results],
                         ["2025-10-01T12:00:03.000+00:00", "2025-10-01T12:00:03.000+00:00"])
        st = afi.Stats()
        for e in ev:
            afi.parse_entry(e, "work/demo", st)
        self.assertEqual(st.turns[0]["secs"], 3.0)
        self.assertEqual([c[3] for c in st.tool_calls], [2.0, 0.0])        # (day, name, family, secs, ok)

    def test_an_elapsed_shorter_than_the_rounds_is_lifted_to_the_last_round(self):
        req = cf.chat_request("r1", T0, "go", elapsed=500, rounds=[_round(2000, [_call("c1", "read_file", {})])])
        ev = _events(req)
        self.assertEqual(ev[-1]["timestamp"], "2025-10-01T12:00:02.000+00:00")
        self.assertEqual(ev[2]["timestamp"], "2025-10-01T12:00:02.000+00:00")             # the last round's result
        # a longer elapsed still wins
        req["elapsedMs"] = 9000
        self.assertEqual(_events(req)[-1]["timestamp"], "2025-10-01T12:00:09.000+00:00")

    def test_round_without_a_timestamp_does_not_move_the_end(self):
        req = cf.chat_request("r1", T0, "go", elapsed=4000, rounds=[{"modelId": "m", "toolCalls": []}])
        self.assertEqual(_events(req)[-1]["timestamp"], "2025-10-01T12:00:04.000+00:00")

    def test_duration_sources(self):
        req = cf.chat_request("r", T0, "x", elapsed=4000)
        self.assertEqual(_events(req)[-1]["timestamp"], "2025-10-01T12:00:04.000+00:00")
        req["result"]["timings"] = {"totalElapsed": 9000}
        self.assertEqual(_events(req)[-1]["timestamp"], "2025-10-01T12:00:04.000+00:00")    # elapsedMs first
        del req["elapsedMs"]
        self.assertEqual(_events(req)[-1]["timestamp"], "2025-10-01T12:00:09.000+00:00")
        req["result"]["timings"] = {"totalElapsed": "soon"}
        self.assertEqual(_events(req)[-1]["timestamp"], "2025-10-01T12:00:00.000+00:00")
        req["elapsedMs"] = -50
        self.assertEqual(_events(req)[-1]["timestamp"], "2025-10-01T12:00:00.000+00:00")
        req["elapsedMs"] = 1e30
        self.assertEqual(_events(req)[-1]["timestamp"], "2025-10-01T12:00:00.000+00:00")

    def test_request_text_and_timestamp_forms(self):
        req = cf.chat_request("r1", "2025-10-01T12:00:00Z", "unused", elapsed=1500)
        req["message"] = "plain string prompt"
        ev = _events(req)
        self.assertEqual((ev[0]["message"]["content"], ev[0]["timestamp"], ev[-1]["timestamp"]),
                         ("plain string prompt", "2025-10-01T12:00:00.000+00:00", "2025-10-01T12:00:01.500+00:00"))
        req["message"] = {"parts": []}
        self.assertEqual(_events(req)[0]["message"]["content"], "")

    def test_requests_without_a_timestamp_are_skipped(self):
        good = cf.chat_request("r2", T0, "kept")
        bad1 = cf.chat_request("r1", None, "dropped")
        bad2 = cf.chat_request("r3", "never", "dropped too")
        del bad1["timestamp"]
        ev = cs.chat_session_to_events({"requests": [bad1, good, bad2]}, "s", "p", "/c", "b")
        self.assertEqual([e["message"]["content"] for e in ev if e["type"] == "user"], ["kept"])
        self.assertEqual(len(ev), 2)

    def test_missing_request_id_gets_a_positional_one(self):
        a, b = cf.chat_request(None, T0, "a"), cf.chat_request(None, T0 + 1, "b")
        ev = cs.chat_session_to_events({"requests": [a, b]}, "s", "p", "/c", "b")
        ids = [e["message"]["id"] for e in ev if e["type"] == "assistant"]
        self.assertEqual(ids, ["s:req0:final", "s:req1:final"])

    def test_malformed_sessions_never_raise(self):
        for sess in (None, [], "x", 5, {}, {"requests": None}, {"requests": "x"}, {"requests": [None, 5, "x", [], {}]}):
            self.assertEqual(cs.chat_session_to_events(sess, "s", "p", "/c", "b"), [], repr(sess))
        weird = {"requestId": 7, "timestamp": T0, "message": 5, "modelId": 3, "promptTokens": "many",
                 "result": {"metadata": {"toolCallRounds": [None, 3, {"toolCalls": "x", "timestamp": "no"},
                                                            {"toolCalls": [{"id": 5, "name": 6, "arguments": 7}],
                                                             "modelId": []}],
                                         "toolCallResults": [1]},
                            "timings": 4, "usage": "x", "details": 9},
                 "elapsedMs": "slow", "copilotCredits": {}}
        ev = cs.chat_session_to_events({"requests": [weird]}, "s", "p", "/c", "b")
        self.assertEqual([e["type"] for e in ev], ["user", "assistant", "assistant", "user", "assistant"])
        st = afi.Stats()
        for e in ev:
            afi.parse_entry(e, "p", st)                     # and the main engine takes them

    def test_two_requests_in_one_session_do_not_collide(self):
        reqs = [cf.chat_request("r1", T0, "one", prompt=10, completion=1, elapsed=1000,
                                rounds=[_round(100, [_call("c1", "read_file", {})])]),
                cf.chat_request("r2", T0 + 60000, "two", prompt=20, completion=2, elapsed=1000,
                                rounds=[_round(100, [_call("c2", "read_file", {})])])]
        ev = cs.chat_session_to_events({"requests": reqs}, "s", "p", "/c", "b")
        ids = [e["message"]["id"] for e in ev if e["type"] == "assistant"]
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4)
        st = afi.Stats()
        for e in ev:
            afi.parse_entry(e, "p", st)
        day = next(iter(st.days.values()))
        self.assertEqual((day["prompts"], day["input"], day["output"]), (2, 30, 3))
        self.assertEqual(len(st.turns), 2)

    def test_replayed_file_translates_end_to_end(self):
        req = cf.chat_request("r1", T0, "hello", prompt=100, completion=10, credits=0.1, elapsed=2000,
                              rounds=[_round(500, [_call("c1", "read_file", {"filePath": "a.py"})])])
        pushed = dict(req)
        for key in ("promptTokens", "completionTokens", "copilotCredits", "elapsedMs"):
            del pushed[key]                                # VS Code sets these later, one line each
        lines = [{"kind": 0, "v": {"sessionId": "s1", "creationDate": T0, "requests": []}},
                 {"kind": 2, "k": ["requests"], "v": [pushed]}]
        lines += [{"kind": 1, "k": ["requests", 0, key], "v": req[key]}
                  for key in ("promptTokens", "completionTokens", "copilotCredits", "elapsedMs")]
        sess, bad = cs.replay_chat_jsonl([json.dumps(x) for x in lines])
        self.assertEqual(bad, 0)
        self.assertEqual(cs.chat_session_to_events(sess, "s1", "p", "/c", "b"),
                         cs.chat_session_to_events({"requests": [req]}, "s1", "p", "/c", "b"))


INTERRUPT = "[Request interrupted by user]"


class ModelStateTests(unittest.TestCase):
    """requests[].modelState: 2 Cancelled is the one value that becomes an interruption (Task 15)."""

    def req(self, state=None, completed=None, **kw):
        kw.setdefault("elapsed", 4000)
        return cf.chat_request("r1", T0, "go", prompt=100, completion=10, state=state, completed=completed, **kw)

    def test_a_cancelled_request_gets_an_interrupt_event_after_the_final_one(self):
        ev = _events(self.req(state=2))
        final, last = ev[-2], ev[-1]
        self.assertEqual(final["message"]["id"], "s1:r1:final")
        self.assertEqual(last, {"type": "user", "timestamp": final["timestamp"], "sessionId": "s1",
                                "cwd": "/work/demo", "gitBranch": "main", "message": {"content": INTERRUPT}})

    def test_the_interrupt_reaches_the_turn_and_the_day_in_parse_entry(self):
        st = afi.Stats()
        both = cs.chat_session_to_events({"requests": [
            self.req(state=2), cf.chat_request("r2", T0 + 60000, "next", state=1)]}, "s1", "p", "/w", "main")
        for e in both:
            afi.parse_entry(e, "p", st)
        day = next(iter(st.days.values()))
        self.assertEqual((day["prompts"], day["interruptions"]), (2, 1))   # the marker is not a prompt
        self.assertEqual([t["intr"] for t in st.turns], [True, False])    # the cancelled turn is marked
        self.assertIsNone(st.turns[0]["secs"])                           # and never finished: no end_turn

    def test_a_cancelled_request_keeps_its_tokens_and_credits(self):
        ev = _events(self.req(state=2, credits=0.5))
        final = ev[-2]
        self.assertEqual(final["message"]["usage"]["input_tokens"], 100)
        self.assertEqual(final["_cost_override"], 0.5)

    def test_other_states_add_nothing(self):
        plain = _events(self.req())
        for state in (0, 1, 3, 4, 2.5, "2", True, False, None, [2], {"v": 2}):
            req = self.req()
            req["modelState"] = {"value": state, "completedAt": T0 + 1000}
            ev = _events(req)
            self.assertEqual([e["type"] for e in ev], [e["type"] for e in plain], repr(state))
            self.assertNotIn(INTERRUPT, json.dumps(ev), repr(state))
        for odd in (None, 2, "x", [], {}):
            req = self.req()
            req["modelState"] = odd
            self.assertEqual(len(_events(req)), len(plain), repr(odd))

    def test_a_finished_request_still_ends_its_turn(self):
        self.assertEqual(_events(self.req(state=1))[-1]["message"]["stop_reason"], "end_turn")
        self.assertEqual(_events(self.req())[-1]["message"]["stop_reason"], "end_turn")

    def test_completed_at_is_the_request_end(self):
        ev = _events(self.req(state=1, completed=T0 + 2500))            # earlier than start + elapsed
        self.assertEqual(ev[-1]["timestamp"], "2025-10-01T12:00:02.500+00:00")
        ev = _events(self.req(state=1, completed=T0 + 9000))
        self.assertEqual(ev[-1]["timestamp"], "2025-10-01T12:00:09.000+00:00")
        ev = _events(self.req(state=2, completed=T0 + 2500))            # the interrupt is stamped the same
        self.assertEqual((ev[-2]["timestamp"], ev[-1]["timestamp"]), ("2025-10-01T12:00:02.500+00:00",) * 2)

    def test_completed_at_as_a_time_string(self):
        ev = _events(self.req(state=1, completed="2025-10-01T12:00:03Z"))
        self.assertEqual(ev[-1]["timestamp"], "2025-10-01T12:00:03.000+00:00")

    def test_a_bad_completed_at_falls_back_to_start_plus_elapsed(self):
        for bad in (None, "garbage", True, [], {}, float("nan"), T0 - 1, T0 - 5000):    # before the start is bad too
            req = self.req(state=1)
            req["modelState"]["completedAt"] = bad
            self.assertEqual(_events(req)[-1]["timestamp"], "2025-10-01T12:00:04.000+00:00", repr(bad))
        self.assertEqual(_events(self.req(state=1))[-1]["timestamp"], "2025-10-01T12:00:04.000+00:00")
        self.assertEqual(_events(self.req(state=1, completed=T0))[-1]["timestamp"], "2025-10-01T12:00:00.000+00:00")

    def test_the_end_is_never_earlier_than_the_last_round(self):
        req = self.req(state=1, completed=T0 + 1000, rounds=[_round(3000, [_call("c1", "read_file", {})])])
        ev = _events(req)
        self.assertEqual(ev[-1]["timestamp"], "2025-10-01T12:00:03.000+00:00")
        req = self.req(state=2, completed=T0 + 1000, rounds=[_round(3000, [_call("c1", "read_file", {})])])
        ev = _events(req)
        self.assertEqual((ev[-2]["timestamp"], ev[-1]["timestamp"]), ("2025-10-01T12:00:03.000+00:00",) * 2)

    def test_chat_state_counts(self):
        reqs = [self.req(state=1), self.req(state=2), self.req(), {"requestId": "no-time", "modelState": {"value": 1}},
                self.req(state=True), self.req(state=3), "junk", None]
        self.assertEqual(cs.chat_state_counts({"requests": reqs}), (5, 3))   # timestamped requests, with a numeric state
        self.assertEqual(cs.chat_state_counts({"requests": []}), (0, 0))
        for bad in (None, {}, {"requests": "x"}, []):
            self.assertEqual(cs.chat_state_counts(bad), (0, 0), repr(bad))


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


def _log(*records, **kw):
    """agent_log_to_events over dicts (or raw strings) as session k1 of work/demo."""
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    return cs.agent_log_to_events(lines, kw.pop("sid", "k1"), "work/demo", "/work/demo", "main", **kw)


def _used(events):
    return [e for e in events if e["type"] == "assistant" and e["message"]["usage"]]


def _rec(rtype, sec, data=None, **extra):
    rec = {"type": rtype, "timestamp": "2026-10-24T10:00:%02dZ" % sec, "data": data if data is not None else {}}
    rec.update(extra)
    return rec


def _call_rec(sec, mid, tid="t1", name="read_file", arguments="{}", **data):
    return _rec("assistant.message", sec, dict(data, messageId=mid, toolRequests=[
        {"toolCallId": tid, "name": name, "arguments": arguments}]))


def _usage_rec(sec, call_id="u1", inp=100, out=10, cache_read=0, cache_write=0, nano=None, model="m1", **data):
    usage = {"apiCallId": call_id, "model": model, "inputTokens": inp, "outputTokens": out,
             "cacheReadTokens": cache_read, "cacheWriteTokens": cache_write}
    if nano is not None:
        usage["copilotUsage"] = {"totalNanoAiu": nano}
    return _rec("session.usage_record", sec, dict(data, usage=usage))


def _checkpoint(sec, inp, out=10, cache_read=0, nano=None, model="m1", rtype="session.usage_checkpoint", nested=True):
    mm = {"usage": {"inputTokens": inp, "outputTokens": out, "cacheReadTokens": cache_read, "cacheWriteTokens": 0}}
    if nano is not None:
        mm["totalNanoAiu"] = nano
    metrics = {model: mm}
    return _rec(rtype, sec, {"accountingSnapshot": {"modelMetrics": metrics}} if nested else {"modelMetrics": metrics})


class AgentLogTests(unittest.TestCase):
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

    def test_checkpoint_fallback_uses_last_snapshot_only(self):
        ev, meta = _log(_rec("session.start", 0, {"context": {"hostType": "cli"}}),
                        _rec("user.message", 1, {"content": "go"}),
                        _checkpoint(2, 100, out=5, cache_read=40, nano=1e8),
                        _checkpoint(3, 300, out=30, cache_read=200, nano=5e8))
        use = _used(ev)
        self.assertEqual(len(use), 1)
        self.assertEqual(use[0]["message"]["usage"], {"input_tokens": 100, "output_tokens": 30,
                                                      "cache_read_input_tokens": 200, "cache_creation_input_tokens": 0})
        self.assertAlmostEqual(use[0]["_cost_override"], 0.5)
        self.assertNotIn("_no_credits", use[0])
        self.assertEqual((use[0]["message"]["model"], use[0]["timestamp"]), ("m1", "2026-10-24T10:00:03.000+00:00"))
        self.assertTrue(meta["has_usage"])

    def test_transcript_has_prompts_and_tools_but_no_tokens(self):
        ev, meta = _log(
            _rec("session.start", 0, {"sessionId": "t1", "copilotVersion": "0.45.0", "vscodeVersion": "1.139.0"}),
            _rec("user.message", 1, {"content": "look at a.py"}),
            _call_rec(2, "m1", "t1", "read_file", json.dumps({"filePath": "a.py"})),
            _rec("tool.execution_complete", 3, {"toolCallId": "t1", "success": True}))
        prompts = [e for e in ev if e["type"] == "user" and isinstance(e["message"]["content"], str)]
        self.assertEqual([p["message"]["content"] for p in prompts], ["look at a.py"])
        tools = [b for e in ev if e["type"] == "assistant" for b in e["message"]["content"]]
        self.assertEqual([(b["name"], b["id"]) for b in tools], [("read_file", "t1")])
        self.assertEqual(_used(ev), [])
        self.assertFalse(any("_cost_override" in e for e in ev))
        self.assertEqual((meta["vscode_version"], meta["copilot_version"], meta["has_usage"]),
                         ("1.139.0", "0.45.0", False))

    def test_subagent_records_are_sidechain(self):
        ev, _meta = _log(_rec("user.message", 1, {"content": "go"}),
                         _call_rec(2, "m1", "t1", "read_file", "{}", agentId="sub1"),
                         _call_rec(3, "m2", "t2", "read_file", "{}"),
                         _rec("tool.execution_complete", 4, {"toolCallId": "t1", "success": True, "agentId": "sub1"}),
                         _usage_rec(5, "u9", agentId="sub1"))
        self.assertEqual([e.get("isSidechain") for e in ev], [None, True, None, True, True])

    # ---- event shapes ------------------------------------------------------------------------
    def test_event_shapes(self):
        ev, _meta = _log(
            _rec("user.message", 1, {"content": "edit it"}),
            _call_rec(2, "m1", "x1", "create_file", json.dumps({"filePath": "a.py", "content": "a\nb"}),
                      model="Copilot/GPT-5"),
            _rec("tool.execution_complete", 3, {"toolCallId": "x1", "success": False}),
            _rec("assistant.turn_end", 4, {"turnId": "0"}))
        user, asst, res, end = ev
        self.assertEqual(user, {"type": "user", "timestamp": "2026-10-24T10:00:01.000+00:00", "sessionId": "k1",
                                "cwd": "/work/demo", "gitBranch": "main", "message": {"content": "edit it"}})
        self.assertEqual((asst["type"], asst["sessionId"], asst["cwd"], asst["gitBranch"]),
                         ("assistant", "k1", "/work/demo", "main"))
        self.assertEqual(asst["timestamp"], "2026-10-24T10:00:02.000+00:00")
        self.assertEqual((asst["message"]["model"], asst["message"]["usage"], asst["message"]["stop_reason"]),
                         ("gpt-5", {}, None))
        self.assertNotIn("isSidechain", asst)
        self.assertNotIn("_cost_override", asst)
        self.assertEqual(asst["message"]["content"], [{"type": "tool_use", "id": "x1", "name": "create_file", "input": {
            "filePath": "a.py", "content": "a\nb", "_loc": 0, "file_path": "a.py"}}])     # its result failed: no lines
        self.assertEqual(res, {"type": "user", "timestamp": "2026-10-24T10:00:03.000+00:00", "sessionId": "k1",
                               "message": {"content": [{"type": "tool_result", "tool_use_id": "x1",
                                                        "is_error": True, "content": ""}]}})
        self.assertEqual((end["message"]["id"], end["message"]["stop_reason"], end["message"]["usage"],
                          end["message"]["content"]), ("k1:end:0", "end_turn", {}, []))
        self.assertEqual((end["requestId"], end["message"]["model"]), ("k1:end:0", "gpt-5"))   # the model the log last named

    def test_tool_arguments(self):
        patch = "*** Begin Patch\n*** Update File: d.py\n+x\n+y\n*** End Patch"
        ev, _meta = _log(
            _rec("assistant.message", 1, {"messageId": "m1", "toolRequests": [
                {"toolCallId": "a", "name": "bash", "arguments": {"command": "ls"}},                 # CLI: a dict
                {"toolCallId": "b", "name": "apply_patch", "arguments": patch},                      # the bare patch text
                {"toolCallId": "c", "name": "view", "arguments": "{not json"},
                {"toolCallId": "d", "name": "edit", "arguments": json.dumps({"path": "e.py", "new_str": "q"})},
                {"name": "read_file"}, "junk", None]}))
        calls = {b["id"]: (b["name"], b["input"]) for b in ev[0]["message"]["content"]}
        self.assertEqual(calls["a"], ("bash", {"command": "ls", "_loc": 0, "file_path": ""}))
        self.assertEqual(calls["b"], ("apply_patch", {"input": patch, "_loc": 2, "file_path": "d.py"}))
        self.assertEqual(calls["c"], ("view", {"_loc": 0, "file_path": ""}))
        self.assertEqual(calls["d"], ("edit", {"path": "e.py", "new_str": "q", "_loc": 1, "file_path": "e.py"}))
        unnamed = [v for k, v in calls.items() if k not in set("abcd")]       # no id: one is made up
        self.assertEqual(unnamed, [("read_file", {"_loc": 0, "file_path": ""})])

    def test_tool_result_error_flag(self):
        ev, _meta = _log(_rec("tool.execution_complete", 1, {"toolCallId": "a", "success": True}),
                         _rec("tool.execution_complete", 2, {"toolCallId": "b"}),
                         _rec("tool.execution_complete", 3, {"toolCallId": "c", "success": False}),
                         _rec("tool.execution_complete", 4, {"success": False}))
        blocks = [e["message"]["content"][0] for e in ev]
        self.assertEqual([(b["tool_use_id"], b["is_error"]) for b in blocks],
                         [("a", False), ("b", False), ("c", True), ("", True)])

    def test_assistant_message_without_tools_is_still_an_event(self):
        ev, _meta = _log(_rec("assistant.message", 1, {"messageId": "m1", "content": "done"}))
        self.assertEqual([(e["type"], e["message"]["content"]) for e in ev], [("assistant", [])])

    # ---- usage -------------------------------------------------------------------------------
    def test_usage_without_credits_never_reaches_the_price_table(self):
        ev, meta = _log(_usage_rec(1, "u1", inp=500, out=50, cache_read=100, cache_write=50))
        e = ev[0]
        self.assertEqual(e["message"]["usage"], {"input_tokens": 350, "output_tokens": 50,
                                                 "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50})
        self.assertEqual((e["_cost_override"], e["_no_credits"]), (0.0, True))
        self.assertTrue(meta["has_usage"])

    def test_input_never_goes_negative(self):
        ev, _meta = _log(_usage_rec(1, inp=10, out=5, cache_read=30, cache_write=5, nano=1))
        self.assertEqual(ev[0]["message"]["usage"]["input_tokens"], 0)

    def test_zero_token_record_is_not_a_zero_usage_call(self):
        ev, meta = _log(_usage_rec(1, "u1", inp=0, out=0), _usage_rec(2, "u2", inp=None, out=None, nano=2e8),
                        _usage_rec(3, "u3", inp=0, out=0, nano=0))
        self.assertEqual([e["message"]["usage"] for e in ev], [{}, {}, {}])
        self.assertNotIn("_cost_override", ev[0])
        self.assertNotIn("_no_credits", ev[0])
        self.assertAlmostEqual(ev[1]["_cost_override"], 0.2)           # credits survive without tokens
        self.assertNotIn("_no_credits", ev[1])
        self.assertEqual(ev[2]["_cost_override"], 0.0)                 # the log said 0 credits
        self.assertNotIn("_no_credits", ev[2])
        self.assertFalse(meta["has_usage"])

    def test_bad_credit_figures_count_as_missing(self):
        ev, _meta = _log(_usage_rec(1, "u1", nano=-5), _usage_rec(2, "u2", nano="12"), _usage_rec(3, "u3", nano=True))
        self.assertEqual([(e["_cost_override"], e.get("_no_credits")) for e in ev], [(0.0, True)] * 3)

    def test_repeated_api_call_id_shares_one_id_and_is_counted_once(self):
        ev, _meta = _log(_rec("user.message", 0, {"content": "go"}), _usage_rec(1, "same", nano=1e8),
                         _usage_rec(2, "same", nano=1e8), _usage_rec(3, "other", nano=1e8))
        self.assertEqual([(e["message"]["id"], e["requestId"]) for e in _used(ev)],
                         [("same", "same"), ("same", "same"), ("other", "other")])
        st = afi.Stats()
        for e in ev:
            afi.parse_entry(e, "p", st)
        self.assertEqual(sum(d["output"] for d in st.days.values()), 20)
        self.assertAlmostEqual(sum(d["cost"] for d in st.days.values()), 0.2)

    def test_usage_record_without_api_call_id_falls_back_to_the_record_id(self):
        ev, _meta = _log(dict(_usage_rec(1, call_id=None), id="rec-1"))
        self.assertEqual((ev[0]["message"]["id"], ev[0]["requestId"]), ("rec-1", "rec-1"))
        ev, _meta = _log(_usage_rec(1, call_id=""))
        self.assertEqual(ev[0]["message"]["id"], "k1:usage:0")        # no id of any kind: made from the line number

    def test_with_usage_false_drops_every_usage_event_but_keeps_the_rest(self):
        ev, meta = cs.agent_log_to_events([json.dumps(x) for x in CLI], "k1", "p", "/p", "main", with_usage=False)
        self.assertEqual(_used(ev), [])
        self.assertFalse(any("_cost_override" in e for e in ev))
        self.assertEqual([e["type"] for e in ev], ["user", "assistant", "user", "assistant"])   # prompt, call, result, end
        self.assertTrue(meta["has_usage"])                  # the file has usage; the caller chose not to take it
        ev, meta = _log(_checkpoint(1, 100, nano=1e8), with_usage=False)
        self.assertEqual(ev, [])
        self.assertTrue(meta["has_usage"])

    def test_usage_records_win_over_checkpoints(self):
        ev, _meta = _log(_usage_rec(1, "u1", inp=100, out=10, nano=1e8), _checkpoint(2, 9999, out=999, nano=9e9),
                         _checkpoint(3, 8888, rtype="session.shutdown"))
        self.assertEqual([e["message"]["usage"]["output_tokens"] for e in _used(ev)], [10])

    def test_shutdown_snapshot_and_its_shape(self):
        ev, meta = _log(_checkpoint(1, 100, nano=1e8),
                        _checkpoint(2, 400, out=40, nano=3e8, rtype="session.shutdown", nested=False))
        use = _used(ev)
        self.assertEqual([(e["message"]["usage"]["input_tokens"], e["message"]["usage"]["output_tokens"])
                          for e in use], [(400, 40)])
        self.assertAlmostEqual(use[0]["_cost_override"], 0.3)
        self.assertTrue(meta["has_usage"])

    def test_snapshot_without_metrics_does_not_replace_an_earlier_one(self):
        ev, _meta = _log(_checkpoint(1, 100, nano=1e8), _rec("session.shutdown", 2, {"shutdownType": "routine"}),
                         _rec("session.usage_checkpoint", 3, {"totalNanoAiu": 5}))
        self.assertEqual([e["message"]["usage"]["input_tokens"] for e in ev], [100])

    def test_snapshot_one_event_per_model_with_credit_rules(self):
        mm = {"mA": {"usage": {"inputTokens": 50, "outputTokens": 5, "cacheReadTokens": 20, "cacheWriteTokens": 10},
                     "totalNanoAiu": 4e8},
              "mB": {"usage": {"inputTokens": 70, "outputTokens": 7}},                      # tokens, no credits
              "mC": {"usage": {"inputTokens": 0, "outputTokens": 0}, "totalNanoAiu": 1e8},  # credits, no tokens
              "mD": {"usage": {}}, "mE": "junk"}                                            # nothing: no event
        ev, meta = _log(_rec("session.usage_checkpoint", 1, {"accountingSnapshot": {"modelMetrics": mm}}))
        by = {e["message"]["model"]: e for e in ev}
        self.assertEqual(set(by), {"ma", "mb", "mc"})
        self.assertEqual(by["ma"]["message"]["usage"], {"input_tokens": 20, "output_tokens": 5,
                                                        "cache_read_input_tokens": 20, "cache_creation_input_tokens": 10})
        self.assertAlmostEqual(by["ma"]["_cost_override"], 0.4)
        self.assertEqual((by["mb"]["_cost_override"], by["mb"]["_no_credits"]), (0.0, True))
        self.assertEqual(by["mc"]["message"]["usage"], {})
        self.assertAlmostEqual(by["mc"]["_cost_override"], 0.1)
        self.assertNotIn("_no_credits", by["mc"])
        self.assertEqual(len({e["message"]["id"] for e in ev}), 3)
        self.assertTrue(meta["has_usage"])

    def test_snapshot_of_empty_models_is_no_usage(self):
        ev, meta = _log(_checkpoint(1, 0, out=0))
        self.assertEqual(ev, [])
        self.assertFalse(meta["has_usage"])

    def test_real_checkpoint_shape(self):
        snap = {"totalNanoAiu": 246088000, "totalPremiumRequests": 1, "accountingSnapshot": {
            "totalNanoAiu": 246088000.0, "modelMetrics": {"mai-code-1.1-flash": {
                "requests": {"count": 2, "cost": 1.0},
                "usage": {"inputTokens": 30056, "outputTokens": 171, "cacheReadTokens": 20864,
                          "cacheWriteTokens": 0, "reasoningTokens": 64},
                "totalNanoAiu": 246088000.0, "aiCreditsStatus": "complete"}}}}
        ev, _meta = _log(_rec("session.usage_checkpoint", 1, snap))
        self.assertEqual(ev[0]["message"]["usage"], {"input_tokens": 9192, "output_tokens": 171,
                                                     "cache_read_input_tokens": 20864, "cache_creation_input_tokens": 0})
        self.assertAlmostEqual(ev[0]["_cost_override"], 0.246088)

    # ---- turn ends ---------------------------------------------------------------------------
    def test_only_the_last_turn_end_per_prompt_survives(self):
        ev, _meta = _log(
            _rec("user.message", 1, {"content": "one"}),
            _rec("assistant.turn_end", 2), _rec("assistant.message", 3, {"messageId": "m"}),
            _rec("assistant.turn_end", 4),
            _rec("user.message", 5, {"content": "two"}),
            _rec("assistant.turn_end", 6),
            _rec("user.message", 7, {"content": "three, never finished"}),
            _rec("user.message", 8, {"content": "four"}),
            _rec("assistant.turn_end", 9), _rec("assistant.turn_end", 10))
        ends = [(e["timestamp"][17:19], e["message"]["id"]) for e in ev if e["message"].get("stop_reason") == "end_turn"]
        self.assertEqual(ends, [("04", "k1:end:1"), ("06", "k1:end:2"), ("10", "k1:end:4")])
        # each end stays where its record was, after the events it follows
        self.assertEqual([e["type"] for e in ev],
                         ["user", "assistant", "assistant", "user", "assistant", "user", "user", "assistant"])

    def test_subagent_turn_end_does_not_stand_in_for_the_main_one(self):
        ev, _meta = _log(_rec("user.message", 1, {"content": "go"}),
                         _rec("assistant.turn_end", 2), _rec("assistant.turn_end", 3, {"agentId": "sub"}))
        ends = [e for e in ev if e["message"].get("stop_reason") == "end_turn"]
        self.assertEqual([(e["timestamp"][17:19], e.get("isSidechain")) for e in ends], [("02", None)])
        ev, _meta = _log(_rec("assistant.turn_end", 1, {"agentId": "sub"}))
        self.assertEqual(ev, [])

    def test_a_subagent_prompt_does_not_reopen_the_main_turn(self):
        ev, _meta = _log(_rec("user.message", 1, {"content": "go"}), _rec("assistant.turn_end", 2),
                         _rec("user.message", 3, {"content": "sub task", "agentId": "sub"}),
                         _rec("assistant.turn_end", 4))
        ends = [e["timestamp"][17:19] for e in ev if e["message"].get("stop_reason") == "end_turn"]
        self.assertEqual(ends, ["04"])
        self.assertEqual(ev[1]["isSidechain"], True)               # the subagent prompt itself is sidechain

    def test_turn_end_closes_the_turn_in_parse_entry(self):
        ev, _meta = cs.agent_log_to_events([json.dumps(x) for x in CLI], "k1", "p", "/p", "main")
        st = afi.Stats()
        for e in ev:
            afi.parse_entry(e, "p", st)
        self.assertEqual([t["secs"] for t in st.turns], [7.0])         # prompt at :01, last turn end at :08

    # ---- records that are not events ---------------------------------------------------------
    def test_ignored_types_are_neither_events_nor_unknown(self):
        ev, meta = _log(*[_rec(t, 1) for t in (
            "hook.start", "hook.end", "system.message", "session.auto_mode_resolved", "session.permissions_changed",
            "session.model_change", "external_tool.requested", "external_tool.completed",
            "assistant.turn_start", "tool.execution_start")])
        self.assertEqual((ev, meta["unknown_types"]), ([], {}))
        _ev, meta = _log(_rec("system.notification", 1), _rec("tool.something", 2), _rec("tool.something", 3))
        self.assertEqual(meta["unknown_types"], {"system.notification": 1, "tool.something": 2})

    def test_routine_cli_and_transcript_types_are_not_unknown(self):
        ev, meta = _log(*[_rec(t, 1) for t in cf.ROUTINE_AGENT_TYPES])
        self.assertEqual((ev, meta["unknown_types"]), ([], {}))
        ev, meta = cs.agent_log_to_events([json.dumps(x) for x in cf.cli_events()], "k1", None, None, None)
        self.assertEqual(meta["unknown_types"], {})

    def test_genuinely_new_types_are_still_counted(self):
        _ev, meta = _log(_rec("permissions.requested", 1), _rec("models.turn", 2), _rec("skills", 3),
                         _rec("subagent_selected", 4), _rec("brand.new_event", 5), _rec("model", 6))
        self.assertEqual(meta["unknown_types"], {"permissions.requested": 1, "models.turn": 1, "skills": 1,
                                                 "subagent_selected": 1, "brand.new_event": 1, "model": 1})

    def test_real_cli_log_shape(self):
        ev, meta = cs.agent_log_to_events([json.dumps(x) for x in cf.cli_events()], "k1", None, None, None)
        use = _used(ev)
        self.assertEqual(len(use), 1)                       # the checkpoint (twice over) adds nothing next to a usage record
        self.assertEqual(use[0]["message"]["model"], "gpt-5.5")
        self.assertEqual(use[0]["message"]["usage"], {"input_tokens": 2000, "output_tokens": 400,
                                                      "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 0})
        self.assertAlmostEqual(use[0]["_cost_override"], 2.0)
        self.assertEqual((meta["cwd"], meta["branch"], meta["repository"], meta["host_type"]),
                         ("/work/cli", None, None, None))
        self.assertEqual(sum(1 for e in ev if e["message"].get("stop_reason") == "end_turn"), 1)

    def test_real_cli_checkpoint_with_both_metric_blocks_is_counted_once(self):
        events = [e for e in cf.cli_events() if e["type"] != "session.usage_record"]
        ev, _meta = cs.agent_log_to_events([json.dumps(x) for x in events], "k1", None, None, None)
        use = _used(ev)
        self.assertEqual(len(use), 1)                       # modelMetrics and agentMetrics.main.modelMetrics are one total
        self.assertEqual(use[0]["message"]["usage"]["output_tokens"], 400)

    def test_bad_lines_are_counted_and_skipped(self):
        truncated = json.dumps(_rec("user.message", 2, {"content": "cut off here"}))[:-9]
        ev, meta = _log(_rec("user.message", 1, {"content": "hello"}), "", "   \n",
                        "not json", "[1, 2]", "42", '"text"', '{"data": {}}', '{"type": 5}',
                        _rec("user.message", 3, {"content": "after"}), truncated)
        self.assertEqual([e["message"]["content"] for e in ev], ["hello", "after"])
        self.assertEqual(meta["bad_lines"], 7)      # blank lines are not bad; the other seven are
        self.assertEqual(meta["unknown_types"], {})

    def test_truncated_last_line(self):
        lines = [json.dumps(x) for x in CLI]
        lines[-1] = lines[-1][:20]                            # the writer was cut off mid-record
        ev, meta = cs.agent_log_to_events(lines, "k1", "p", "/p", "main")
        self.assertEqual((meta["bad_lines"], dict(meta["unknown_types"])), (1, {}))
        self.assertEqual(len(_used(ev)), 1)

    def test_junk_data_never_raises(self):
        recs = [{"type": t, "timestamp": "2026-10-24T10:00:01Z", "data": d}
                for t in ("user.message", "assistant.message", "tool.execution_complete", "session.usage_record",
                          "session.usage_checkpoint", "session.shutdown", "session.start", "assistant.turn_end")
                for d in (None, 5, "x", [], {"usage": 3, "toolRequests": 7, "context": 1, "modelMetrics": [1],
                                              "accountingSnapshot": "z"})]
        ev, meta = _log(*recs)
        self.assertEqual(meta["bad_lines"], 0)
        self.assertEqual(_used(ev), [])

    def test_absurd_numbers_count_as_missing(self):
        huge = 10 ** 400                       # valid JSON, too big for a float
        ev, _meta = _log(_usage_rec(1, "u1", inp=huge, out=5, nano=huge), _usage_rec(2, "u2", inp=huge, out=huge))
        self.assertEqual(ev[0]["message"]["usage"], {"input_tokens": 0, "output_tokens": 5,
                                                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
        self.assertEqual((ev[0]["_cost_override"], ev[0]["_no_credits"]), (0.0, True))
        self.assertEqual(ev[1]["message"]["usage"], {})

    def test_record_without_timestamp_takes_the_previous_one_or_is_skipped(self):
        nots = {"type": "user.message", "data": {"content": "late"}}
        ev, _meta = _log(nots, _rec("user.message", 4, {"content": "first"}), nots, dict(nots, timestamp="garbage"))
        self.assertEqual([(e["message"]["content"], e["timestamp"][17:19]) for e in ev],
                         [("first", "04"), ("late", "04"), ("late", "04")])

    # ---- session.start and meta --------------------------------------------------------------
    def test_session_start_meta(self):
        _ev, meta = _log(_rec("session.start", 0, {
            "copilotVersion": "1.0.100-1", "vscodeVersion": "1.139.0",
            "context": {"gitRoot": "/work/repo", "repository": "me/repo", "hostType": "github", "branch": "dev"}}))
        self.assertEqual({k: v for k, v in meta.items() if k != "unknown_types"},
                         {"host_type": "github", "cwd": "/work/repo", "branch": "dev", "repository": "me/repo",
                          "copilot_version": "1.0.100-1", "vscode_version": "1.139.0",
                          "has_usage": False, "bad_lines": 0})
        _ev, meta = _log(_rec("session.start", 0, {"context": {"cwd": "/w/a", "gitRoot": "/w"}}),
                         _rec("session.start", 1, {"context": {"cwd": "/w/b", "branch": "x"}}))
        self.assertEqual((meta["cwd"], meta["branch"], meta["host_type"]), ("/w/a", "x", None))
        _ev, meta = _log()
        self.assertEqual((meta["unknown_types"], meta["cwd"], meta["repository"]), ({}, None, None))

    def test_events_fall_back_to_the_logs_own_cwd_and_branch(self):
        lines = [json.dumps(_rec("session.start", 0, {"context": {"cwd": "/w/a", "branch": "dev"}})),
                 json.dumps(_rec("user.message", 1, {"content": "hi"}))]
        ev, _meta = cs.agent_log_to_events(lines, "k1", "p", "", "")
        self.assertEqual((ev[0]["cwd"], ev[0]["gitBranch"]), ("/w/a", "dev"))
        ev, _meta = cs.agent_log_to_events(lines, "k1", "p", "/given", "given")
        self.assertEqual((ev[0]["cwd"], ev[0]["gitBranch"]), ("/given", "given"))
        ev, _meta = cs.agent_log_to_events(lines[1:], "k1", "p", None, None)
        self.assertEqual((ev[0]["cwd"], ev[0]["gitBranch"]), ("", ""))

    def test_accepts_any_iterable_of_lines(self):
        ev, meta = cs.agent_log_to_events((json.dumps(x) + "\n" for x in CLI), "k1", "p", "/p", "main")
        self.assertEqual(len(ev), 5)
        self.assertEqual(meta["bad_lines"], 0)

    def test_cli_log_feeds_parse_entry(self):
        ev, _meta = cs.agent_log_to_events([json.dumps(x) for x in CLI], "k1", "work/demo", "/work/demo", "main")
        st = afi.Stats()
        for e in ev:
            afi.parse_entry(e, "work/demo", st)
        day = next(iter(st.days.values()))
        self.assertEqual((day["input"], day["cache_read"], day["output"], day["prompts"]), (135, 14976, 20, 1))
        self.assertAlmostEqual(day["cost"], 0.211036)
        self.assertEqual(st.tools["bash"], 1)
        self.assertEqual(st.models["mai-code-1.1-flash"]["msgs"], 1)
        self.assertEqual(st.branches["main"]["msgs"], 1)
        self.assertEqual(st.project_cwd["work/demo"], "/work/demo")


T0_MS = 1759320000000          # 2025-10-01T12:00:00Z


class ExactTokenTests(TmpCase):
    """OpenTelemetry file export and debug logs: exact per-call tokens, one source per session."""

    def debug_log(self, sid, rows):
        ws = cf.add_workspace(cf.make_user_root(self.tmp), "h", "/w")
        return cf.add_debug_log(ws, sid, rows)

    def otel_file(self, spans, name="o.jsonl"):
        return cf.add_otel(os.path.join(self.tmp, name), spans)

    def test_debug_session_folder_with_every_line_type_and_stray_files(self):
        ws = cf.add_workspace(cf.make_user_root(self.tmp), "h", "/w")
        folder = cf.add_debug_session(ws, "s1", [cf.llm_request("a", 1759320000000, 5000, 400, 3000, "claude-haiku-4.5", 2e9),
                                                 cf.llm_request("b", 1759320005000, 100, 10, 0, "gpt-5.5-2026-04-23")])
        names = sorted(os.listdir(folder))
        self.assertEqual(names, ["main.jsonl", "models.jsonl", "prompt_0.jsonl"])
        calls = cs.read_debug_calls([os.path.join(folder, n) for n in names])
        self.assertEqual([(c["id"], c["input"], c["cache_read"], c["output"], c["model"]) for c in calls],
                         [("a", 2000, 3000, 400, "claude-haiku-4.5"), ("b", 100, 0, 10, "gpt-5.5")])
        self.assertEqual(calls[0]["credits"], 2.0)
        found = cs.discover([os.path.join(self.tmp, "Code", "User")], None)
        self.assertEqual(cs.exact_calls_for("s1", found["debug"], {})[0], "debug")

    def test_debug_session_with_no_llm_request_is_none(self):
        ws = cf.add_workspace(cf.make_user_root(self.tmp), "h", "/w")
        folder = cf.add_debug_session(ws, "s1", [])
        files = [os.path.join(folder, n) for n in os.listdir(folder)]
        self.assertEqual(cs.exact_calls_for("s1", {"s1": files}, {}), ("none", []))

    # ---- the plan's examples ---------------------------------------------------------------------
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
        self.assertEqual(calls, [{"id": "a", "ts": "2025-10-01T12:00:00.000+00:00", "model": "gpt-4.1",
                                  "input": 2000, "output": 400, "cache_read": 3000, "cache_write": 0,
                                  "credits": 0.12}])

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
        p = self.debug_log("s1", [cf.llm_request("a", T0_MS, 5000, 400, 3000, nano_aiu=120000000)])
        idx = cs.read_otel_calls([self.otel_file([cf.otel_span("s1", "1", 900, 50, cache_read=400)])])
        debug = {"s1": [p], "s2": [p]}
        mode, calls = cs.exact_calls_for("s1", debug, idx)
        self.assertEqual((mode, [c["id"] for c in calls]), ("otel", ["1"]))          # never the sum of both
        mode, calls = cs.exact_calls_for("s2", debug, idx)                            # no OTel call for s2
        self.assertEqual((mode, [c["id"] for c in calls]), ("debug", ["a"]))
        self.assertEqual(cs.exact_calls_for("s3", debug, idx), ("none", []))
        self.assertEqual(cs.exact_calls_for("s1", debug, {"s1": []})[0], "debug")     # an empty list is no calls

    def test_nothing_to_read(self):
        gone = os.path.join(self.tmp, "gone.jsonl")
        self.assertEqual((cs.read_debug_calls([]), cs.read_otel_calls([])), ([], {}))
        self.assertEqual((cs.read_debug_calls([gone, self.tmp]), cs.read_otel_calls([gone, self.tmp])), ([], {}))
        self.assertEqual(cs.exact_calls_for("s1", {"s1": [gone]}, {}), ("none", []))
        self.assertEqual(cs.exact_calls_for("s1", {}, {}), ("none", []))

    # ---- debug log -------------------------------------------------------------------------------
    def test_debug_only_llm_request_lines_count(self):
        p = self.debug_log("s1", [
            {"type": "session_start", "attrs": {"copilotVersion": "0.67.0"}},
            {"type": "tool_call", "spanId": "t", "ts": T0_MS, "attrs": {"inputTokens": 99}},
            cf.llm_request("a", T0_MS, 100, 10)])
        self.assertEqual([c["id"] for c in cs.read_debug_calls([p])], ["a"])

    def test_debug_model_credits_and_floor(self):
        p = self.debug_log("s1", [
            cf.llm_request("a", T0_MS, 100, 5, cached=250, model="Copilot/GPT-4.1", nano_aiu=2500000000),
            cf.llm_request("b", T0_MS + 1000, 100, 5, model="auto"),
            {"type": "llm_request", "spanId": "c", "ts": T0_MS + 2000, "attrs": {"copilotUsageNanoAiu": -5}}])
        a, b, c = cs.read_debug_calls([p])
        self.assertEqual((a["input"], a["cache_read"], a["cache_write"], a["model"], a["credits"]), (0, 250, 0, "gpt-4.1", 2.5))
        self.assertEqual((b["model"], b["credits"]), (None, None))
        self.assertEqual((c["input"], c["output"], c["credits"]), (0, 0, None))

    def test_debug_calls_without_a_time_are_dropped_and_the_rest_ordered(self):
        p = self.debug_log("s1", [
            cf.llm_request("late", T0_MS + 5000, 10, 1),
            {"type": "llm_request", "spanId": "nots", "attrs": {"inputTokens": 10}},
            cf.llm_request("badts", "x", 10, 1),
            cf.llm_request("early", T0_MS, 10, 1),
            cf.llm_request("iso", "2025-10-01T12:00:02Z", 10, 1)])
        self.assertEqual([c["id"] for c in cs.read_debug_calls([p])], ["early", "iso", "late"])

    def test_debug_call_without_span_id_gets_a_stable_id(self):
        row = cf.llm_request("x", T0_MS, 10, 1)
        del row["spanId"]
        p = self.debug_log("s1", [row, dict(row, ts=T0_MS + 1000)])
        ids = [c["id"] for c in cs.read_debug_calls([p])]
        self.assertEqual((len(ids), len(set(ids))), (2, 2))
        self.assertEqual(ids, [c["id"] for c in cs.read_debug_calls([p])])

    # ---- OpenTelemetry file ----------------------------------------------------------------------
    def test_otel_cache_split_credits_model_and_time(self):
        idx = cs.read_otel_calls([self.otel_file([
            cf.otel_span("s1", "1", 1000, 40, cache_read=300, cache_write=200, nano_aiu=500000000,
                         model="GPT-4.1", start_ns=1759320000123456789),
            cf.otel_span("s1", "2", 100, 5, cache_read=300, cache_write=200, as_list=True,
                         start_ns=1759320001000000000)])])
        first, second = idx["s1"]
        self.assertEqual(first, {"id": "1", "ts": "2025-10-01T12:00:00.123+00:00", "model": "gpt-4.1",
                                 "input": 500, "output": 40, "cache_read": 300, "cache_write": 200, "credits": 0.5})
        self.assertEqual((second["input"], second["credits"]), (0, None))           # floored at 0

    def test_otel_attribute_values_in_otlp_json(self):
        span = {"name": "chat x", "spanId": "1", "startTimeUnixNano": str(1759320000 * 10**9),    # int64 is a string
                "attributes": [{"key": "gen_ai.conversation.id", "value": {"stringValue": "s1"}},
                               {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "900"}},
                               {"key": "gen_ai.usage.output_tokens", "value": {"doubleValue": 50.0}},
                               {"key": "github.copilot.nano_aiu", "value": {"doubleValue": 2e8}},
                               {"key": "bogus"}, "junk", {"value": {"intValue": 1}}]}
        (call,) = cs.read_otel_calls([self.otel_file([span])])["s1"]
        self.assertEqual((call["ts"], call["input"], call["output"], call["credits"]),
                         ("2025-10-01T12:00:00.000+00:00", 900, 50, 0.2))

    def test_otel_time_fields(self):
        def at(**kw):
            span = {"name": "chat x", "spanId": "1",
                    "attributes": {"gen_ai.conversation.id": "s1", "gen_ai.usage.output_tokens": 1}}
            span.update(kw)
            idx = cs.read_otel_calls([self.otel_file([span])])
            return idx["s1"][0]["ts"] if idx else None
        want = "2025-10-01T12:00:00.000+00:00"
        self.assertEqual(at(startTimeUnixNano=1759320000000000000), want)
        self.assertEqual(at(startTime=[1759320000, 0]), want)
        self.assertEqual(at(startTime=[1759320000, 250000000]), "2025-10-01T12:00:00.250+00:00")
        self.assertEqual(at(startTime="2025-10-01T12:00:00Z"), want)
        self.assertEqual(at(timestamp=T0_MS), want)
        self.assertEqual(at(startTime=[1759320000, 0], startTimeUnixNano=0), want)       # 0 is no time
        self.assertIsNone(at(), "a span with no time cannot be placed")
        self.assertIsNone(at(startTimeUnixNano="soon", startTime="x", timestamp=[]))

    def test_otel_span_selection(self):
        conv = {"gen_ai.conversation.id": "s1", "gen_ai.usage.input_tokens": 5}      # a call carries token counts
        idx = cs.read_otel_calls([self.otel_file([
            {"name": "chat gpt-4.1", "spanId": "1", "startTimeUnixNano": 10**18, "attributes": dict(conv)},
            {"name": "invoke_agent copilot", "spanId": "2", "startTimeUnixNano": 10**18,
             "attributes": dict(conv, **{"gen_ai.operation.name": "chat"})},                    # chat by attribute
            {"name": "execute_tool read_file", "spanId": "3", "startTimeUnixNano": 10**18, "attributes": dict(conv)},
            {"name": "invoke_agent copilot", "spanId": "4", "startTimeUnixNano": 10**18,
             "attributes": dict(conv, **{"gen_ai.operation.name": "invoke_agent"})},
            {"name": "chat gpt-4.1", "spanId": "5", "startTimeUnixNano": 10**18, "attributes": {}},    # no conversation
            {"spanId": "6", "startTimeUnixNano": 10**18, "attributes": dict(conv)},                  # no name
            {"name": "chat gpt-4.1", "spanId": "7", "startTimeUnixNano": 10**18, "attributes": "oops"}])])
        self.assertEqual({k: [c["id"] for c in v] for k, v in idx.items()}, {"s1": ["1", "2"]})

    def test_otel_model_from_response_request_or_name(self):
        def model(name, **attrs):
            span = {"name": name, "spanId": "1", "startTimeUnixNano": 10**18,
                    "attributes": dict({"gen_ai.conversation.id": "s1", "gen_ai.usage.input_tokens": 5}, **attrs)}
            return cs.read_otel_calls([self.otel_file([span])])["s1"][0]["model"]
        self.assertEqual(model("chat gpt-4.1", **{"gen_ai.response.model": "gpt-4.1-2025",
                                                  "gen_ai.request.model": "auto"}), "gpt-4.1-2025")
        self.assertEqual(model("chat x", **{"gen_ai.request.model": "gpt-5"}), "gpt-5")
        self.assertEqual(model("chat gpt-4.1"), "gpt-4.1")
        self.assertIsNone(model("chat auto", **{"gen_ai.request.model": "auto"}))
        self.assertIsNone(model("chat"))

    def test_otel_otlp_batch_records(self):
        batch = {"resourceSpans": [
            {"scopeSpans": [{"spans": [cf.otel_span("s1", "1", 100, 10), cf.otel_span("s2", "2", 50, 5), "junk"]},
                            "junk"]},
            {"instrumentationLibrarySpans": [{"spans": [cf.otel_span("s1", "3", 70, 7)]}]},
            "junk"]}
        idx = cs.read_otel_calls([self.otel_file([batch, {"resourceSpans": "oops"}])])
        self.assertEqual({k: [c["id"] for c in v] for k, v in idx.items()}, {"s1": ["1", "3"], "s2": ["2"]})

    def test_otel_calls_are_in_time_order_per_conversation(self):
        idx = cs.read_otel_calls([self.otel_file([
            cf.otel_span("s1", "late", 10, 1, start_ns=1759320090 * 10**9),
            cf.otel_span("s2", "other", 10, 1),
            cf.otel_span("s1", "early", 10, 1, start_ns=1759320000 * 10**9)])])
        self.assertEqual([c["id"] for c in idx["s1"]], ["early", "late"])
        self.assertEqual([c["id"] for c in idx["s2"]], ["other"])

    # ---- reading the same data twice, odd files (rulings 3 and 4) ----------------------------------
    def test_the_same_debug_file_listed_twice_counts_once(self):
        p = self.debug_log("s1", [cf.llm_request("a", T0_MS, 100, 10), cf.llm_request("b", T0_MS + 1000, 100, 10)])
        again = os.path.join(os.path.dirname(p), ".", os.path.basename(p))
        self.assertEqual([c["id"] for c in cs.read_debug_calls([p, again, p.upper()])], ["a", "b"])
        mode, calls = cs.exact_calls_for("s1", {"s1": [p, p]}, {})
        self.assertEqual((mode, len(calls)), ("debug", 2))

    def test_a_file_listed_twice_counts_once_even_when_its_lines_carry_no_span_id(self):
        row = cf.llm_request("x", T0_MS, 100, 10)
        del row["spanId"]
        p = self.debug_log("s1", [row])
        self.assertEqual(len(cs.read_debug_calls([p, p, os.path.join(os.path.dirname(p), ".", "main.jsonl")])), 1)
        span = cf.otel_span("s1", "x", 100, 10)
        del span["spanId"]
        o = self.otel_file([span])
        self.assertEqual(len(cs.read_otel_calls([o, o])["s1"]), 1)

    def test_the_same_otel_file_listed_twice_counts_once(self):
        path = self.otel_file([cf.otel_span("s1", "1", 100, 10), cf.otel_span("s1", "2", 100, 10)])
        again = os.path.join(os.path.dirname(path), ".", os.path.basename(path))
        idx = cs.read_otel_calls([path, again, path])
        self.assertEqual([c["id"] for c in idx["s1"]], ["1", "2"])

    def test_repeated_span_id_is_dropped_within_a_session_only(self):
        p1 = self.debug_log("s1", [cf.llm_request("a", T0_MS, 100, 10), cf.llm_request("a", T0_MS, 100, 10)])
        p2 = os.path.join(os.path.dirname(p1), "sub.jsonl")
        cf.add_raw(p2, json.dumps(cf.llm_request("a", T0_MS, 100, 10)) + "\n"
                   + json.dumps(cf.llm_request("z", T0_MS, 1, 1)) + "\n")
        self.assertEqual([c["id"] for c in cs.read_debug_calls([p1, p2])], ["a", "z"])
        o1 = self.otel_file([cf.otel_span("s1", "1", 100, 10), cf.otel_span("s1", "1", 100, 10),
                             cf.otel_span("s2", "1", 100, 10)], "o1.jsonl")
        o2 = self.otel_file([cf.otel_span("s1", "1", 100, 10), cf.otel_span("s1", "2", 100, 10)], "o2.jsonl")
        idx = cs.read_otel_calls([o1, o2])
        self.assertEqual({k: [c["id"] for c in v] for k, v in idx.items()}, {"s1": ["1", "2"], "s2": ["1"]})

    def test_byte_order_mark_and_bad_lines_are_survived(self):
        good = json.dumps(cf.llm_request("a", T0_MS, 100, 10))
        text = ("﻿" + good + "\n{not json\n\n[1, 2]\n\"str\"\n"
                + json.dumps(cf.llm_request("b", T0_MS + 1, 1, 1)) + "\n{\"type\": \"llm_requ")
        p = cf.add_raw(os.path.join(self.tmp, "main.jsonl"), text)
        self.assertEqual([c["id"] for c in cs.read_debug_calls([p])], ["a", "b"])
        one = cf.otel_span("s1", "1", 100, 10)
        two = dict(one, spanId="2")
        o = cf.add_raw(os.path.join(self.tmp, "o.jsonl"),
                       "﻿" + json.dumps(one) + "\nnot json\n" + json.dumps(two) + "\n{")
        self.assertEqual([c["id"] for c in cs.read_otel_calls([o])["s1"]], ["1", "2"])

    def test_bytes_that_are_not_utf8_do_not_raise(self):
        p = os.path.join(self.tmp, "bin.jsonl")
        with open(p, "wb") as fh:
            fh.write(b"\xff\xfe\x00garbage\n" + json.dumps(cf.llm_request("a", T0_MS, 10, 1)).encode() + b"\n")
        self.assertEqual([c["id"] for c in cs.read_debug_calls([p])], ["a"])
        self.assertEqual(cs.read_otel_calls([p]), {})

    def test_malformed_values_never_raise(self):
        bad = {"type": "llm_request", "spanId": 7, "ts": T0_MS, "attrs": {
            "inputTokens": "lots", "outputTokens": True, "cachedTokens": float("nan"), "model": 5,
            "copilotUsageNanoAiu": 10 ** 400}}
        p = self.debug_log("s1", [bad, {"type": "llm_request", "ts": T0_MS + 1, "attrs": "x"},
                                  {"type": "llm_request"}, {"type": "llm_request", "spanId": ["a"], "ts": T0_MS + 2}])
        calls = cs.read_debug_calls([p])
        self.assertEqual(len(calls), 3)
        self.assertEqual((calls[0]["id"], calls[0]["input"], calls[0]["output"], calls[0]["cache_read"],
                          calls[0]["model"], calls[0]["credits"]), ("7", 0, 0, 0, None, None))
        self.assertEqual(cs.read_debug_calls([p, None, 5, "", "a\0b"]), calls)
        self.assertEqual(cs.read_debug_calls(iter([p])), calls)             # any iterable of paths, or one path
        self.assertEqual(cs.read_debug_calls(p), calls)
        self.assertEqual((cs.read_debug_calls(None), cs.read_otel_calls(5)), ([], {}))

    # ---- usage_events ----------------------------------------------------------------------------
    def calls(self):
        p = self.debug_log("s1", [cf.llm_request("a", T0_MS, 5000, 400, 3000, nano_aiu=120000000),
                                  cf.llm_request("b", T0_MS + 60000, 1000, 100, 0)])
        return cs.read_debug_calls([p])

    def test_usage_events_shape(self):
        ev = cs.usage_events("s1", self.calls()[:1], "work/demo", "/work/demo", "main")
        self.assertEqual(ev, [{
            "type": "assistant", "timestamp": "2025-10-01T12:00:00.000+00:00", "sessionId": "s1",
            "requestId": "s1:x:a", "cwd": "/work/demo", "gitBranch": "main",
            "message": {"id": "s1:x:a", "model": "gpt-4.1",
                        "usage": {"input_tokens": 2000, "output_tokens": 400,
                                  "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 0},
                        "stop_reason": None, "content": []},
            "_cost_override": 0.12}])

    def test_usage_events_without_credits_are_pinned_to_zero_and_marked(self):
        ev = cs.usage_events("s1", self.calls(), "p", "/w", "main")
        self.assertEqual([e["_cost_override"] for e in ev], [0.12, 0.0])
        self.assertEqual(["_no_credits" in e for e in ev], [False, True])
        self.assertIs(ev[1]["_no_credits"], True)
        self.assertEqual([e["message"]["id"] for e in ev], ["s1:x:a", "s1:x:b"])
        self.assertTrue(all(isinstance(e["_cost_override"], float) for e in ev))

    def test_usage_events_no_fake_zeros(self):
        def call(cid, ts, **kw):
            return dict({"id": cid, "ts": "2025-10-01T12:00:0%d.000+00:00" % ts, "model": None, "input": 0,
                         "output": 0, "cache_read": 0, "cache_write": 0, "credits": None}, **kw)
        z, y, x = cs.usage_events("s1", [call("z", 0), call("y", 1, credits=0.25),
                                         call("x", 2, model="m", cache_read=40)], "p", "/w", "main")
        self.assertEqual((z["message"]["usage"], "_cost_override" in z, "_no_credits" in z), ({}, False, False))
        self.assertEqual((y["message"]["usage"], y["_cost_override"], "_no_credits" in y), ({}, 0.25, False))
        self.assertEqual(z["message"]["model"], "unknown")
        self.assertEqual(x["message"]["usage"]["cache_read_input_tokens"], 40)       # cached tokens are tokens
        self.assertEqual((x["_cost_override"], x["_no_credits"]), (0.0, True))

    def test_usage_events_skip_a_call_with_no_time_and_default_cwd_and_branch(self):
        calls = self.calls()
        calls[0]["ts"] = None
        ev = cs.usage_events("s1", calls, "p", None, None)
        self.assertEqual([e["message"]["id"] for e in ev], ["s1:x:b"])
        self.assertEqual((ev[0]["cwd"], ev[0]["gitBranch"]), ("", ""))
        self.assertEqual(cs.usage_events("s1", [], "p", "/w", "main"), [])
        (odd,) = cs.usage_events("s1", [{"id": "q", "ts": "2025-10-01T12:00:00Z"}, {"id": "r", "ts": "x"}, "junk"],
                                 "p", "/w", "main")
        self.assertEqual((odd["timestamp"], odd["message"]["usage"]), ("2025-10-01T12:00:00.000+00:00", {}))

    def test_usage_events_feed_parse_entry_without_a_dollar_price(self):
        st = afi.Stats()
        for e in cs.usage_events("s1", self.calls(), "work/demo", "/work/demo", "main"):
            afi.parse_entry(e, "work/demo", st)
        day = next(iter(st.days.values()))
        self.assertEqual((day["input"], day["cache_read"], day["output"]), (3000, 3000, 500))
        self.assertAlmostEqual(day["cost"], 0.12)           # call "b" has no credits: 0, not the Claude price
        self.assertEqual(st.models["gpt-4.1"]["msgs"], 2)

    def test_priced_nowhere_even_for_a_claude_looking_model(self):
        call = {"id": "a", "ts": "2025-10-01T12:00:00.000+00:00", "model": "claude-sonnet-4-5", "input": 10 ** 6,
                "output": 10 ** 6, "cache_read": 0, "cache_write": 0, "credits": None}
        st = afi.Stats()
        for e in cs.usage_events("s1", [call], "p", "/w", "main"):
            afi.parse_entry(e, "p", st)
        self.assertEqual(next(iter(st.days.values()))["cost"], 0.0)

    def test_the_same_files_twice_give_the_same_totals_through_parse_entry(self):
        p = self.debug_log("s1", [cf.llm_request("a", T0_MS, 5000, 400, 3000, nano_aiu=120000000)])
        o = self.otel_file([cf.otel_span("s2", "1", 900, 50, cache_read=400, nano_aiu=10 ** 8)])

        def totals(debug_paths, otel_paths, sid):
            idx = cs.read_otel_calls(otel_paths)
            _mode, calls = cs.exact_calls_for(sid, {sid: debug_paths}, idx)
            st = afi.Stats()
            for e in cs.usage_events(sid, calls, "p", "/w", "main") * 2:         # and each event fed twice
                afi.parse_entry(e, "p", st)
            day = next(iter(st.days.values()))
            return day["input"], day["cache_read"], day["output"], round(day["cost"], 6)
        self.assertEqual(totals([p], [], "s1"), totals([p, p], [], "s1"))
        self.assertEqual(totals([p], [], "s1"), (2000, 3000, 400, 0.12))
        self.assertEqual(totals([], [o, o], "s2"), (500, 400, 50, 0.1))


class ProjectAndStoreTests(TmpCase):
    def test_project_label_matches_claude_style(self):
        self.assertEqual(cs.project_label("E:\\god_ai\\AI_ALL"), "E/god/ai/AI/ALL")
        self.assertEqual(cs.project_label(None), "(no folder)")

    def test_project_label_same_folder_same_label_in_both_tools(self):
        # Claude Code names E:\god_ai\AI_ALL "E--god-ai-AI-ALL"; VS Code writes the drive in lower case
        claude_dir = "E--god-ai-AI-ALL"
        claude_label = claude_dir.lstrip("-").replace("--", "/").replace("-", "/")
        for path in ("E:\\god_ai\\AI_ALL", "e:/god_ai/AI_ALL", "e:\\god_ai\\AI_ALL\\"):
            self.assertEqual(cs.project_label(path), claude_label, path)
        self.assertEqual(cs.project_label("/work/demo"), "work/demo")
        self.assertEqual(cs.project_label(""), "(no folder)")

    def ws(self, obj):
        ws_dir = os.path.join(self.tmp, "ws%d" % len(os.listdir(self.tmp)))
        if obj is not None:
            cf.add_raw(os.path.join(ws_dir, "workspace.json"), obj if isinstance(obj, str) else json.dumps(obj))
        return ws_dir

    def test_workspace_folder_decodes_uri(self):
        self.assertEqual(cs.workspace_folder(self.ws({"folder": "file:///c%3A/work/demo"})), "c:/work/demo")
        self.assertEqual(cs.workspace_folder(self.ws({"folder": "file:///home/me/my%20app"})), "/home/me/my app")
        ur = cf.make_user_root(self.tmp)
        self.assertEqual(cs.workspace_folder(cf.add_workspace(ur, "h1", "/work/demo")), "/work/demo")

    def test_workspace_folder_of_a_code_workspace_is_its_folder(self):
        ws = self.ws({"workspace": "file:///c%3A/Users/me/AppData/agent-sessions.code-workspace"})
        self.assertEqual(cs.workspace_folder(ws), "c:/Users/me/AppData")

    def test_workspace_folder_missing_or_broken_is_none(self):
        for obj in (None, "{not json", "[]", {}, {"folder": ""}, {"folder": 3}):
            self.assertIsNone(cs.workspace_folder(self.ws(obj)), obj)
        self.assertIsNone(cs.workspace_folder(""))

    def test_read_session_store_wal(self):
        ur = cf.make_user_root(self.tmp)
        db_path = cf.add_session_store(ur, [{"id": "s1", "cwd": "/w", "branch": "dev", "summary": "Fix login"}])
        self.assertEqual(cs.read_session_store(cs.discover([ur], self.tmp)["stores"])["s1"]["branch"], "dev")
        # Keep a writer open so the newest row lives only in the -wal file, as while VS Code runs.
        db = sqlite3.connect(db_path)
        self.addCleanup(db.close)
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO sessions (id, cwd, repository, branch, summary) VALUES "
                   "('s2', '/x', 'me/x', 'main', 'Add tests')")
        db.commit()
        wal = db_path + "-wal"
        self.assertTrue(os.path.isfile(wal))
        before = [(os.path.getsize(p), os.path.getmtime(p)) for p in (db_path, wal)]
        rows = cs.read_session_store([db_path])
        self.assertEqual(rows["s2"], {"cwd": "/x", "repository": "me/x", "branch": "main", "summary": "Add tests"})
        self.assertEqual(rows["s1"], {"cwd": "/w", "repository": None, "branch": "dev", "summary": "Fix login"})
        self.assertEqual([(os.path.getsize(p), os.path.getmtime(p)) for p in (db_path, wal)], before)

    def test_read_session_store_missing_table_or_unreadable_file(self):
        empty = os.path.join(self.tmp, "empty.db")
        sqlite3.connect(empty).close()
        junk = cf.add_raw(os.path.join(self.tmp, "junk.db"), "this is not a database")
        missing = os.path.join(self.tmp, "missing.db")
        for paths in ([empty], [junk], [missing], []):
            self.assertEqual(cs.read_session_store(paths), {}, paths)
        self.assertFalse(os.path.exists(missing))           # reading never creates a file

    def test_read_session_store_tolerates_missing_columns_and_merges_stores(self):
        a = os.path.join(self.tmp, "a.db")
        db = sqlite3.connect(a)
        db.execute("CREATE TABLE sessions (id, summary)")
        db.execute("INSERT INTO sessions VALUES ('s1', 'From a')")
        db.commit(); db.close()
        b = cf.add_session_store(cf.make_user_root(self.tmp), [{"id": "s1", "cwd": "/b", "summary": "From b"},
                                                               {"id": "s9", "branch": "x"}])
        rows = cs.read_session_store([a, b])
        self.assertEqual(rows["s1"], {"cwd": "/b", "repository": None, "branch": None, "summary": "From a"})
        self.assertEqual(rows["s9"]["branch"], "x")

    def test_read_lines_drops_a_byte_order_mark(self):
        p = cf.add_raw(os.path.join(self.tmp, "e.jsonl"), "\ufeff{\"type\": \"session.start\"}\n{\"a\": 1}\n")
        lines = cs.read_lines(p)
        self.assertEqual(json.loads(lines[0]), {"type": "session.start"})
        self.assertEqual(len(lines), 2)
        self.assertIsNone(cs.read_lines(os.path.join(self.tmp, "missing.jsonl")))


class ModuleShapeTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(cs.VARIANTS, ["Code", "Code - Insiders", "Code - Exploration", "VSCodium"])
        self.assertEqual(cs.EXT_FOLDERS, ["GitHub.copilot-chat", "github.copilot-chat"])

    def test_module_does_not_import_the_main_file(self):
        with open(cs.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertNotIn("ai_fluency_insights", names)



class FinalReviewFixTests(TmpCase):
    """The final whole-branch review's fixes in copilot_source (Critical 1a, Important 1-2, Minor 7)."""

    # -- Critical 1a: a folder URI that urlsplit rejects ------------------------------------------
    def test_uri_with_a_bracket_is_no_folder_not_an_error(self):
        self.assertIsNone(cs._uri_path("vscode-remote://[bad/home/x"))
        ws = os.path.join(self.tmp, "ws")
        os.makedirs(ws)
        with open(os.path.join(ws, "workspace.json"), "w", encoding="utf-8") as f:
            json.dump({"folder": "vscode-remote://[bad/home/x"}, f)
        self.assertIsNone(cs.workspace_folder(ws))

    # -- Important 1: the -wal file appears between two checks --------------------------------------
    def test_session_store_wal_race_does_not_escape(self):
        ur = cf.make_user_root(self.tmp)
        db = cf.add_session_store(ur, [{"id": "s1", "branch": "main"}])
        self.assertFalse(os.path.exists(db + "-wal"))
        real, calls = os.path.exists, {"n": 0}

        def racy(path):
            if str(path).endswith("-wal"):
                calls["n"] += 1
                return calls["n"] >= 2          # absent at the first look, present at the next
            return real(path)
        with unittest.mock.patch.object(cs.os.path, "exists", racy):
            rows = cs.read_session_store([db])
        self.assertEqual(rows["s1"]["branch"], "main")

    def test_one_bad_session_store_never_fails_the_others(self):
        ur = cf.make_user_root(self.tmp)
        good = cf.add_session_store(ur, [{"id": "s1", "branch": "main"}])
        bad_dir = os.path.join(self.tmp, "other")
        os.makedirs(bad_dir)
        bad = os.path.join(bad_dir, "session-store.db")
        shutil.copyfile(good, bad)
        real = cs._store_rows

        def boom(con):
            if boom.first:
                boom.first = False
                raise TypeError("a driver bug")
            return real(con)
        boom.first = True
        with unittest.mock.patch.object(cs, "_store_rows", boom):
            rows = cs.read_session_store([bad, good])
        self.assertEqual(rows["s1"]["branch"], "main")

    def test_session_store_reports_the_databases_it_could_not_read(self):
        ur = cf.make_user_root(self.tmp)
        db = cf.add_session_store(ur, [{"id": "s1"}])
        problems = []
        with unittest.mock.patch.object(cs, "_open_store", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(cs.read_session_store([db], problems), {})
        self.assertEqual(problems, [db])
        problems = []
        self.assertIn("s1", cs.read_session_store([db], problems))
        self.assertEqual(problems, [])

    # -- Important 2: a failed write adds no lines (spec 5.1) ---------------------------------------
    @staticmethod
    def _loc_of(events):
        st = afi.copilot_stats()
        for e in events:
            afi.parse_entry(e, "x", st)
        return sum(d["loc"] for d in st.days.values()), sum(d["tool_errors"] for d in st.days.values())

    def _chat_req(self, results):
        calls = [{"id": "t1", "name": "replace_string_in_file",
                  "arguments": json.dumps({"filePath": "/x/a.py", "oldString": "a", "newString": "1\n2\n3\n4\n5"})},
                 {"id": "t2", "name": "create_file",
                  "arguments": json.dumps({"filePath": "/x/b.py", "content": "x\ny"})}]
        return {"requestId": "r1", "timestamp": 1792000000000, "message": {"text": "edit it"}, "elapsedMs": 3000,
                "result": {"metadata": {"toolCallRounds": [{"timestamp": 1792000001000, "modelId": "gpt-4.1",
                                                            "toolCalls": calls}],
                                        "toolCallResults": results}}}

    def test_chat_failed_write_counts_no_lines(self):
        req = self._chat_req({"t1": {"content": [{"value": "ERROR: Could not find matching text to replace"}]},
                              "t2": {"content": [{"value": "created"}]}})
        ev = cs.chat_session_to_events({"requests": [req]}, "s1", None, "/x", "")
        inputs = {b["id"]: b["input"] for e in ev if e["type"] == "assistant" for b in e["message"]["content"]}
        self.assertEqual((inputs["t1"]["_loc"], inputs["t2"]["_loc"]), (0, 2))
        self.assertEqual(self._loc_of(ev), (2, 1))          # the failed call is still a tool error

    def test_agent_log_failed_write_counts_no_lines(self):
        args = json.dumps({"filePath": "/x/a.py", "oldString": "a", "newString": "1\n2\n3"})
        ev, _meta = _log(_rec("user.message", 1, {"content": "edit"}),
                         _call_rec(2, "m1", "t1", "replace_string_in_file", args),
                         _rec("tool.execution_complete", 3, {"toolCallId": "t1", "success": False}),
                         _call_rec(4, "m2", "t2", "replace_string_in_file", args),
                         _rec("tool.execution_complete", 5, {"toolCallId": "t2", "success": True}),
                         _call_rec(6, "m3", "t3", "replace_string_in_file", args))     # no result yet: counted
        inputs = {b["id"]: b["input"] for e in ev if e["type"] == "assistant" for b in e["message"]["content"]}
        self.assertEqual((inputs["t1"]["_loc"], inputs["t2"]["_loc"], inputs["t3"]["_loc"]), (0, 3, 3))
        self.assertEqual(self._loc_of(ev), (6, 1))

    # -- Minor 7: an OTel span without token counts is not a call ----------------------------------
    def test_otel_span_without_usage_is_dropped(self):
        bare = {"name": "chat gpt-4.1", "spanId": "b1", "startTimeUnixNano": 1759320000000000000,
                "attributes": {"gen_ai.operation.name": "chat", "gen_ai.conversation.id": "s1",
                               "github.copilot.nano_aiu": 10 ** 8}}
        zero = cf.otel_span("s2", "z1", 0, 0)
        path = cf.add_otel(os.path.join(self.tmp, "o.jsonl"), [bare, zero, cf.otel_span("s3", "k1", 100, 10)])
        self.assertEqual(sorted(cs.read_otel_calls([path])), ["s3"])


if __name__ == "__main__":
    unittest.main()
