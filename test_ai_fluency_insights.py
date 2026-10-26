"""Tests for ai_fluency_insights.py: notifications and setup, plus the My Adoption,
Flow, Quality, Value, Advisor and Data Health engines.

Run with:  python -m unittest test_ai_fluency_insights -v
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ai_fluency_insights as afi


class MilestonesCrossedTests(unittest.TestCase):
    def test_single_milestone_crossed(self):
        # 50000/200000 = 25%
        self.assertEqual(afi.milestones_crossed(0, 50_000, 200_000), [25])

    def test_no_milestone_crossed_when_still_below(self):
        self.assertEqual(afi.milestones_crossed(0, 10_000, 200_000), [])

    def test_multiple_milestones_crossed_in_one_jump(self):
        self.assertEqual(afi.milestones_crossed(0, 200_000, 200_000),
                          [25, 50, 75, 85, 100])

    def test_already_past_milestone_does_not_refire(self):
        # prev is already above 50% threshold -> no new crossing
        self.assertEqual(afi.milestones_crossed(120_000, 130_000, 200_000), [])

    def test_zero_total_disables_milestones(self):
        self.assertEqual(afi.milestones_crossed(0, 999_999, 0), [])

    def test_drop_then_regrowth_refires_same_milestone(self):
        # simulates a context compaction: usage drops, then climbs back past 50%
        self.assertEqual(afi.milestones_crossed(90_000, 110_000, 200_000), [50])


class StepsCrossedTests(unittest.TestCase):
    def test_single_step_crossed(self):
        self.assertEqual(afi.steps_crossed(8_000, 12_000, 10_000), [10_000])

    def test_multiple_steps_crossed_in_one_jump(self):
        self.assertEqual(afi.steps_crossed(0, 25_000, 10_000),
                          [10_000, 20_000])

    def test_no_step_crossed_when_still_below(self):
        self.assertEqual(afi.steps_crossed(1_000, 4_999, 5_000), [])

    def test_no_step_crossed_on_no_increase(self):
        self.assertEqual(afi.steps_crossed(10_000, 10_000, 5_000), [])

    def test_zero_step_disables(self):
        self.assertEqual(afi.steps_crossed(0, 999_999, 0), [])


class WeightedTokensTests(unittest.TestCase):
    """Plan-window accounting must track quota-weighted usage, not a raw
    token sum — cache reads are ~0.1x the cost of fresh input (see PRICING),
    so a chat-heavy session with heavy tool-call caching would otherwise look
    like it burned far more quota than it actually did."""

    def test_input_and_output_weighted_by_pricing_ratio(self):
        # every PRICING tier prices output at 5x input, so output carries a
        # flat 5x weight regardless of model
        self.assertEqual(afi.weighted_tokens(1_000, 100, 0, 0), 1_000 + 500)

    def test_cache_write_weighted_at_1_25x(self):
        self.assertEqual(afi.weighted_tokens(0, 0, 1_000, 0), 1_250)

    def test_cache_read_weighted_at_0_1x(self):
        self.assertEqual(afi.weighted_tokens(0, 0, 0, 1_000), 100)


class SessionTokenTrackerTests(unittest.TestCase):
    def make_tracker(self, **notify_overrides):
        notify = dict(afi.NOTIFY)
        notify.update(notify_overrides)
        return afi.SessionTokenTracker(notify=notify)

    def test_context_pct_event_on_usage(self):
        t = self.make_tracker(context_window=100_000,
                               session_step=0, task_step=0)
        events = t.on_usage(input_tokens=25_000, output_tokens=1_000,
                             cache_write=0, cache_read=0)
        self.assertIn(("context_pct", 25, 25_000), events)

    def test_session_step_event_on_cumulative_usage(self):
        t = self.make_tracker(context_window=0,
                               session_step=10_000, task_step=0)
        t.on_usage(8_000, 0, 0, 0)
        events = t.on_usage(4_000, 0, 0, 0)
        self.assertIn(("session_step", 10_000, 12_000), events)

    def test_task_step_resets_on_new_user_message(self):
        t = self.make_tracker(context_window=0,
                               session_step=0, task_step=5_000)
        events = t.on_usage(6_000, 0, 0, 0)
        self.assertIn(("task_step", 5_000, 6_000), events)
        t.on_user_message()
        # after reset, a small usage shouldn't immediately refire the step
        events2 = t.on_usage(1_000, 0, 0, 0)
        self.assertEqual(events2, [])

    def test_resuming_from_saved_state_does_not_refire(self):
        # simulates restart: tracker rebuilt from persisted totals
        t = afi.SessionTokenTracker(notify=dict(afi.NOTIFY, context_window=0,
                                                 session_step=10_000,
                                                 task_step=0),
                                     session_total=9_500)
        events = t.on_usage(400, 0, 0, 0)  # crosses to 9900, still under 10k
        self.assertEqual(events, [])


class PlanWindowTrackerTests(unittest.TestCase):
    """Approximates the Pro/Max app's rolling ~5h 'Current session' limit:
    a single global window (not per Claude Code session) that starts on
    first usage and resets fully 5h later."""

    WINDOW_SECONDS = 5 * 3600

    def test_first_event_starts_window_and_may_cross_milestone(self):
        t = afi.PlanWindowTracker(ceiling=100_000)
        events = t.add(ts_epoch=1_000, tokens=30_000)
        self.assertEqual(events, [25])
        self.assertEqual(t.window_start, 1_000)
        self.assertEqual(t.window_total, 30_000)

    def test_second_event_within_window_accumulates(self):
        t = afi.PlanWindowTracker(ceiling=100_000)
        t.add(1_000, 30_000)
        events = t.add(1_000 + 3600, 30_000)  # 60% total now
        self.assertEqual(events, [50])
        self.assertEqual(t.window_total, 60_000)

    def test_event_at_exactly_5h_starts_a_new_window(self):
        t = afi.PlanWindowTracker(ceiling=100_000)
        t.add(0, 90_000)
        events = t.add(self.WINDOW_SECONDS, 10_000)
        self.assertEqual(t.window_start, self.WINDOW_SECONDS)
        self.assertEqual(t.window_total, 10_000)
        self.assertEqual(events, [])  # 10% of the fresh window, no crossing

    def test_event_just_under_5h_stays_in_same_window(self):
        t = afi.PlanWindowTracker(ceiling=100_000)
        t.add(0, 10_000)
        t.add(self.WINDOW_SECONDS - 1, 10_000)
        self.assertEqual(t.window_start, 0)
        self.assertEqual(t.window_total, 20_000)

    def test_zero_ceiling_disables_milestones(self):
        t = afi.PlanWindowTracker(ceiling=0)
        events = t.add(0, 50_000)
        self.assertEqual(events, [])

    def test_pct_caps_at_100(self):
        t = afi.PlanWindowTracker(ceiling=100_000)
        t.add(0, 150_000)
        self.assertEqual(t.pct(now_epoch=100), 100)

    def test_pct_returns_zero_once_window_has_expired_without_a_new_event(self):
        # add() only resets window_start/window_total when a *new* usage
        # event arrives -- during an idle stretch past the 5h boundary,
        # nothing has called add() yet, so pct() must not keep reporting
        # the previous (now-expired) window's leftover percentage. This is
        # the exact "8% used, resets in 0h 0m" glitch seen in production:
        # resets_in_seconds() correctly says the window is over, but pct()
        # was still dividing the stale window_total by the ceiling.
        t = afi.PlanWindowTracker(ceiling=100_000)
        t.add(0, 80_000)
        expired_now = self.WINDOW_SECONDS + 500
        self.assertEqual(t.resets_in_seconds(expired_now), 0)
        self.assertEqual(t.pct(now_epoch=expired_now), 0)

    def test_resets_in_seconds_counts_down_from_window_start(self):
        t = afi.PlanWindowTracker(ceiling=100_000)
        t.add(1_000, 10_000)
        self.assertEqual(t.resets_in_seconds(now_epoch=1_000 + 3600),
                          self.WINDOW_SECONDS - 3600)

    def test_resets_in_seconds_floors_at_zero_past_the_boundary(self):
        t = afi.PlanWindowTracker(ceiling=100_000)
        t.add(0, 10_000)
        self.assertEqual(t.resets_in_seconds(now_epoch=self.WINDOW_SECONDS + 500), 0)

    def test_state_round_trips_into_a_new_tracker(self):
        t = afi.PlanWindowTracker(ceiling=100_000, window_start=500, window_total=20_000)
        restored = afi.PlanWindowTracker(ceiling=100_000, **t.state())
        self.assertEqual(restored.window_start, 500)
        self.assertEqual(restored.window_total, 20_000)


class NotifyStatePersistenceTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(afi.load_notify_state("/no/such/path.json"), {})

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            state = {"sess-1": {"session_total": 12_345, "task_total": 0,
                                 "last_context_usage": 12_345}}
            afi.save_notify_state(path, state)
            self.assertEqual(afi.load_notify_state(path), state)


class NotificationWatcherTests(unittest.TestCase):
    @staticmethod
    def _write_jsonl(path, entries):
        with open(path, "w", encoding="utf-8") as f:
            for obj in entries:
                f.write(afi.json.dumps(obj) + "\n")

    @staticmethod
    def _usage_entry(sid, input_tokens, output_tokens):
        return {"type": "assistant", "sessionId": sid,
                "message": {"usage": {"input_tokens": input_tokens,
                                       "output_tokens": output_tokens,
                                       "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0}}}

    def test_session_spanning_two_files_does_not_fire_on_first_poll(self):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            sid = "sess-multi"
            # same session split across two log files, as can happen with
            # resumed/rotated Claude Code logs
            self._write_jsonl(os.path.join(proj, "a.jsonl"),
                               [self._usage_entry(sid, 20_000, 0)])
            self._write_jsonl(os.path.join(proj, "b.jsonl"),
                               [self._usage_entry(sid, 20_000, 0)])
            scanner = afi.Scanner(root)
            notify = dict(afi.NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)
            watcher = afi.NotificationWatcher(
                scanner, notify=notify,
                state_path=os.path.join(root, "state.json"))
            watcher.poll_once()
            self.assertEqual(watcher.recent_events, [])

    def test_restart_does_not_reprocess_already_seen_entries(self):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            sid = "sess-restart"
            path = os.path.join(proj, "a.jsonl")
            self._write_jsonl(path, [self._usage_entry(sid, 5_000, 0)])
            state_path = os.path.join(root, "state.json")
            notify = dict(afi.NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)

            scanner1 = afi.Scanner(root)
            w1 = afi.NotificationWatcher(scanner1, notify=notify, state_path=state_path)
            w1.poll_once()  # baseline poll: session_total becomes 5000

            # simulate a dashboard restart: a brand-new watcher and Scanner,
            # same on-disk logs and same persisted state file
            scanner2 = afi.Scanner(root)
            w2 = afi.NotificationWatcher(scanner2, notify=notify, state_path=state_path)
            w2.poll_once()

            # no new entries since the restart, so w2 shouldn't need to
            # touch this session at all — its persisted total must survive
            # unchanged (not doubled) in the state file it re-saves
            saved = afi.load_notify_state(state_path)
            self.assertEqual(saved["sessions"][sid]["session_total"], 5_000)
            self.assertEqual(w2.recent_events, [])

    def test_save_state_preserves_sessions_not_touched_this_poll(self):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            sid = "sess-quiet"
            path = os.path.join(proj, "a.jsonl")
            self._write_jsonl(path, [self._usage_entry(sid, 5_000, 0)])
            state_path = os.path.join(root, "state.json")
            notify = dict(afi.NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)

            scanner1 = afi.Scanner(root)
            w1 = afi.NotificationWatcher(scanner1, notify=notify, state_path=state_path)
            w1.poll_once()  # sid becomes known, session_total=5000

            # this poll only looks at files modified in the last 0 minutes,
            # so sid's file is now "outside the active window" and won't be
            # touched — its persisted total must not be lost
            scanner2 = afi.Scanner(root)
            w2 = afi.NotificationWatcher(
                scanner2, notify=dict(notify, active_window_min=0),
                state_path=state_path)
            w2.poll_once()

            saved = afi.load_notify_state(state_path)
            self.assertEqual(saved["sessions"][sid]["session_total"], 5_000)

    @patch.object(afi, "send_windows_toast")
    def test_new_usage_after_baseline_fires_normally(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            sid = "sess-1"
            path = os.path.join(proj, "a.jsonl")
            self._write_jsonl(path, [self._usage_entry(sid, 5_000, 0)])
            scanner = afi.Scanner(root)
            notify = dict(afi.NOTIFY, context_window=0,
                          session_step=10_000, task_step=0,
                          active_window_min=999_999)
            watcher = afi.NotificationWatcher(
                scanner, notify=notify,
                state_path=os.path.join(root, "state.json"))
            watcher.poll_once()
            self.assertEqual(watcher.recent_events, [])  # baseline poll

            with open(path, "a", encoding="utf-8") as f:
                f.write(afi.json.dumps(self._usage_entry(sid, 6_000, 0)) + "\n")
            watcher.poll_once()
            kinds = [e["message"] for e in watcher.recent_events]
            self.assertTrue(any("11.0k" in m or "10.0k" in m for m in kinds),
                             kinds)


class PlanWindowNotificationTests(unittest.TestCase):
    """The plan-usage panel is GLOBAL (every session combined), unlike the
    per-session signals above."""

    @staticmethod
    def _write_jsonl(path, entries):
        with open(path, "w", encoding="utf-8") as f:
            for obj in entries:
                f.write(afi.json.dumps(obj) + "\n")

    @staticmethod
    def _usage_entry(sid, input_tokens, output_tokens, ts):
        return {"type": "assistant", "sessionId": sid, "timestamp": ts,
                "message": {"usage": {"input_tokens": input_tokens,
                                       "output_tokens": output_tokens,
                                       "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0}}}

    @patch.object(afi, "send_windows_toast")
    def test_plan_pct_combines_sessions_and_coalesces_per_poll(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            path_a = os.path.join(proj, "a.jsonl")
            path_b = os.path.join(proj, "b.jsonl")
            self._write_jsonl(path_a, [self._usage_entry(
                "sess-a", 1_000, 0, "2026-01-01T00:00:00Z")])
            self._write_jsonl(path_b, [self._usage_entry(
                "sess-b", 1_000, 0, "2026-01-01T00:00:01Z")])
            notify = dict(afi.NOTIFY, context_window=0, session_step=0, task_step=0,
                          window_ceiling=10_000, active_window_min=999_999)
            state_path = os.path.join(root, "state.json")
            scanner = afi.Scanner(root)
            watcher = afi.NotificationWatcher(scanner, notify=notify, state_path=state_path)
            watcher.poll_once()  # baseline: both sessions newly discovered, silent
            self.assertEqual(watcher.recent_events, [])

            # new usage lands on both sessions between polls; combined they
            # cross 25/50/75% of the ceiling in one poll -> one toast, not three
            with open(path_a, "a", encoding="utf-8") as f:
                f.write(afi.json.dumps(self._usage_entry(
                    "sess-a", 4_000, 0, "2026-01-01T00:01:00Z")) + "\n")
            with open(path_b, "a", encoding="utf-8") as f:
                f.write(afi.json.dumps(self._usage_entry(
                    "sess-b", 4_000, 0, "2026-01-01T00:01:01Z")) + "\n")
            watcher.poll_once()

            plan_events = [e for e in watcher.recent_events
                           if e["title"].startswith("Plan usage")]
            self.assertEqual(len(plan_events), 1, watcher.recent_events)
            self.assertIn("75%", plan_events[0]["message"])
            self.assertEqual(mock_toast.call_count, 1)

    @patch.object(afi, "send_windows_toast")
    def test_plan_window_state_persists_across_restart(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            path = os.path.join(proj, "a.jsonl")
            self._write_jsonl(path, [self._usage_entry(
                "sess-1", 1_000, 0, "2026-01-01T00:00:00Z")])
            notify = dict(afi.NOTIFY, context_window=0, session_step=0, task_step=0,
                          window_ceiling=10_000, active_window_min=999_999)
            state_path = os.path.join(root, "state.json")

            scanner1 = afi.Scanner(root)
            w1 = afi.NotificationWatcher(scanner1, notify=notify, state_path=state_path)
            w1.poll_once()  # baseline: sess-1 newly discovered, plan window untouched

            with open(path, "a", encoding="utf-8") as f:
                f.write(afi.json.dumps(self._usage_entry(
                    "sess-1", 3_000, 0, "2026-01-01T00:01:00Z")) + "\n")
            w1.poll_once()  # sess-1 now known -> 3000 tokens added to the plan window

            saved = afi.load_notify_state(state_path)
            self.assertEqual(saved["plan_window"]["window_total"], 3_000)

            # simulate restart: fresh watcher, same on-disk state
            scanner2 = afi.Scanner(root)
            w2 = afi.NotificationWatcher(scanner2, notify=notify, state_path=state_path)
            self.assertEqual(w2.plan_tracker.window_total, 3_000)


class LoadRateLimitsTests(unittest.TestCase):
    """load_rate_limits() reads the file the statusLine hook writes -- see
    ~/.claude/statusline.js -- and must treat a stale or missing file as
    unavailable rather than serve a number that's stopped moving."""

    def test_missing_file_returns_none(self):
        self.assertIsNone(afi.load_rate_limits("/no/such/path.json"))

    def test_fresh_file_returns_data(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rl.json")
            data = {"rate_limits": {"five_hour": {"used_percentage": 23.5}},
                     "captured_at": 1000.0}
            afi.save_notify_state(path, data)
            self.assertEqual(afi.load_rate_limits(path, now=1000.0 + 60), data)

    def test_stale_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rl.json")
            data = {"rate_limits": {"five_hour": {"used_percentage": 23.5}},
                     "captured_at": 1000.0}
            afi.save_notify_state(path, data)
            stale_now = 1000.0 + afi.RATE_LIMITS_STALE_SECONDS + 1
            self.assertIsNone(afi.load_rate_limits(path, now=stale_now))

    def test_corrupt_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rl.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertIsNone(afi.load_rate_limits(path))


class OfficialRateLimitNotificationTests(unittest.TestCase):
    """The official-source path (poll_once -> _poll_official_rate_limits and
    plan_window_snapshot) must prefer real Anthropic numbers over the local
    estimate whenever a fresh statusline capture exists, and must not refire
    milestones already seen before a restart."""

    def _watcher(self, root, rate_limits_path):
        scanner = afi.Scanner(root)
        return afi.NotificationWatcher(
            scanner, notify=dict(afi.NOTIFY, active_window_min=999_999),
            state_path=os.path.join(root, "state.json"),
            rate_limits_path=rate_limits_path)

    def _write_rl(self, path, five_pct, seven_pct=None, captured_at=None):
        rl = {"five_hour": {"used_percentage": five_pct, "resets_at": 9_999_999_999}}
        if seven_pct is not None:
            rl["seven_day"] = {"used_percentage": seven_pct, "resets_at": 9_999_999_999}
        afi.save_notify_state(path, {
            "rate_limits": rl,
            "captured_at": captured_at if captured_at is not None else afi.time.time(),
        })

    def test_snapshot_prefers_official_over_estimate_when_fresh(self):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 81, seven_pct=11)
            w = self._watcher(root, rl_path)
            snap = w.plan_window_snapshot()
            self.assertEqual(snap["source"], "official")
            self.assertEqual(snap["pct"], 81)
            self.assertEqual(snap["week_pct"], 11)

    def test_snapshot_reports_capture_age_for_transparency(self):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 81, seven_pct=11, captured_at=afi.time.time() - 300)
            w = self._watcher(root, rl_path)
            snap = w.plan_window_snapshot()
            self.assertEqual(snap["captured_age_min"], 5)

    @patch.object(afi, "send_windows_toast")
    def test_85pct_milestone_fires_not_90(self, mock_toast):
        # the dashboard's requested milestones are 25/50/75/85/100, not the
        # generic MILESTONES default of ...90... from earlier iterations
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 80)
            w = self._watcher(root, rl_path)
            w.poll_once()  # baseline at 80%

            self._write_rl(rl_path, 87)  # crosses 85%, not 90%
            w.poll_once()

            official_events = [e for e in w.recent_events if e["id"].startswith("official:")]
            self.assertEqual(len(official_events), 1, w.recent_events)
            self.assertIn("85%", official_events[0]["message"])

    @patch.object(afi, "send_windows_toast")
    def test_weekly_uses_20_40_60_80_100_not_5h_milestones(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 10, seven_pct=10)
            w = self._watcher(root, rl_path)
            w.poll_once()  # baseline: five_hour=10%, seven_day=10%

            # 10 -> 41%: crosses the weekly 20% and 40% milestones, but no
            # 5h-style milestone (25/50/75/85/100) up to 41
            self._write_rl(rl_path, 10, seven_pct=41)
            w.poll_once()

            weekly_events = [e for e in w.recent_events if e["id"].startswith("official:seven_day")]
            self.assertEqual(len(weekly_events), 1, w.recent_events)
            self.assertIn("40%", weekly_events[0]["message"])

    @patch.object(afi, "send_windows_toast")
    def test_5h_and_weekly_milestones_are_independent(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 20, seven_pct=15)
            w = self._watcher(root, rl_path)
            w.poll_once()  # baseline

            # five_hour 20->26 crosses its 25% milestone; seven_day 15->22
            # crosses its 20% milestone -- both should fire independently
            self._write_rl(rl_path, 26, seven_pct=22)
            w.poll_once()

            ids = sorted(e["id"] for e in w.recent_events if e["id"].startswith("official:"))
            self.assertEqual(ids, ["official:five_hour:25", "official:seven_day:20"])

    def test_snapshot_falls_back_to_estimate_only_when_never_captured(self):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")  # never written -> missing
            w = self._watcher(root, rl_path)
            snap = w.plan_window_snapshot()
            self.assertEqual(snap["source"], "estimate")

    def test_snapshot_prefers_stale_official_over_local_estimate(self):
        # A real (if hours-old) Anthropic percentage is still more
        # trustworthy than PlanWindowTracker's ceiling guess -- a stale
        # capture should not make the dashboard fall all the way back to
        # the local estimate.
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            stale_at = afi.time.time() - afi.RATE_LIMITS_STALE_SECONDS - 3600
            self._write_rl(rl_path, 42, seven_pct=17, captured_at=stale_at)
            w = self._watcher(root, rl_path)
            snap = w.plan_window_snapshot()
            self.assertEqual(snap["source"], "official")
            self.assertEqual(snap["pct"], 42)
            self.assertEqual(snap["week_pct"], 17)
            self.assertGreaterEqual(snap["captured_age_min"], 60)

    def test_snapshot_treats_a_stale_capture_past_its_reset_as_zero(self):
        # If the window's resets_at has already passed, the account-side
        # window has reset even though our last capture predates that --
        # showing the old leftover percentage here would be worse than
        # showing nothing.
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            afi.save_notify_state(rl_path, {
                "rate_limits": {"five_hour": {"used_percentage": 91,
                                               "resets_at": afi.time.time() - 60}},
                "captured_at": afi.time.time() - afi.RATE_LIMITS_STALE_SECONDS - 3600,
            })
            w = self._watcher(root, rl_path)
            snap = w.plan_window_snapshot()
            self.assertEqual(snap["source"], "official")
            self.assertEqual(snap["pct"], 0)

    @patch.object(afi, "send_windows_toast")
    def test_first_sighting_sets_baseline_without_firing(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 60)
            w = self._watcher(root, rl_path)
            w.poll_once()
            self.assertEqual(w.recent_events, [])
            mock_toast.assert_not_called()

    @patch.object(afi, "send_windows_toast")
    def test_crossing_a_milestone_fires_once(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 60)
            w = self._watcher(root, rl_path)
            w.poll_once()  # baseline at 60%

            self._write_rl(rl_path, 76)  # crosses 75%
            w.poll_once()

            official_events = [e for e in w.recent_events if e["id"].startswith("official:")]
            self.assertEqual(len(official_events), 1, w.recent_events)
            self.assertIn("75%", official_events[0]["message"])
            self.assertEqual(mock_toast.call_count, 1)

    @patch.object(afi, "send_windows_toast")
    def test_same_window_does_not_refire_on_noisy_official_pct(self, mock_toast):
        # Anthropic's reported used_percentage for the *same* window (same
        # resets_at) can wobble by a point or two between statusline
        # captures rather than climbing monotonically. Once 85% has fired,
        # dipping back to 84% and re-crossing 85% moments later — still
        # inside the same window — must not fire a second toast; that's
        # exactly the "same alert 6-8 times in a row" bug, not a real new
        # crossing like a context compaction resetting usage.
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            self._write_rl(rl_path, 80)
            w = self._watcher(root, rl_path)
            w.poll_once()  # baseline at 80%

            self._write_rl(rl_path, 87)  # crosses 85%
            w.poll_once()
            self.assertEqual(mock_toast.call_count, 1)

            self._write_rl(rl_path, 84)  # wobbles back under 85%
            w.poll_once()
            self._write_rl(rl_path, 86)  # ...and back over — same window
            w.poll_once()
            self._write_rl(rl_path, 84)
            w.poll_once()
            self._write_rl(rl_path, 85)
            w.poll_once()

            official_events = [e for e in w.recent_events if e["id"].startswith("official:")]
            self.assertEqual(len(official_events), 1, w.recent_events)
            self.assertEqual(mock_toast.call_count, 1)

    @patch.object(afi, "send_windows_toast")
    def test_new_window_refires_milestones_from_zero(self, mock_toast):
        # A genuinely new 5h window (different resets_at) must be free to
        # fire 25/50/75/85 again from scratch, even though the previous
        # window already fired them all.
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            resets1 = afi.time.time() + 1000
            afi.save_notify_state(rl_path, {
                "rate_limits": {"five_hour": {"used_percentage": 80, "resets_at": resets1}},
                "captured_at": afi.time.time()})
            w = self._watcher(root, rl_path)
            w.poll_once()  # baseline at 80%

            afi.save_notify_state(rl_path, {
                "rate_limits": {"five_hour": {"used_percentage": 87, "resets_at": resets1}},
                "captured_at": afi.time.time()})
            w.poll_once()  # crosses 85% in window 1
            self.assertEqual(mock_toast.call_count, 1)

            resets2 = resets1 + 5 * 3600
            afi.save_notify_state(rl_path, {
                "rate_limits": {"five_hour": {"used_percentage": 30, "resets_at": resets2}},
                "captured_at": afi.time.time()})
            w.poll_once()  # new window: crosses 25%
            self.assertEqual(mock_toast.call_count, 2)

            official_events = [e for e in w.recent_events if e["id"] == "official:five_hour:25"]
            self.assertEqual(len(official_events), 1, w.recent_events)

    @patch.object(afi, "send_windows_toast")
    def test_restart_does_not_refire_already_crossed_milestone(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            rl_path = os.path.join(root, "rl.json")
            state_path = os.path.join(root, "state.json")
            self._write_rl(rl_path, 60)
            scanner1 = afi.Scanner(root)
            w1 = afi.NotificationWatcher(
                scanner1, notify=dict(afi.NOTIFY, active_window_min=999_999),
                state_path=state_path, rate_limits_path=rl_path)
            w1.poll_once()  # baseline at 60%

            self._write_rl(rl_path, 76)  # crosses 75%
            w1.poll_once()
            self.assertEqual(mock_toast.call_count, 1)

            # restart: fresh watcher loads the persisted official_prev=76,
            # same 76% value on disk -> no new crossing, no refire
            scanner2 = afi.Scanner(root)
            w2 = afi.NotificationWatcher(
                scanner2, notify=dict(afi.NOTIFY, active_window_min=999_999),
                state_path=state_path, rate_limits_path=rl_path)
            w2.poll_once()
            self.assertEqual(w2.recent_events, [])
            self.assertEqual(mock_toast.call_count, 1)


class StatuslineConfigTests(unittest.TestCase):
    def test_classify_missing_when_no_statusline_key(self):
        self.assertEqual(afi.classify_statusline({}), "missing")

    def test_classify_missing_when_statusline_not_a_dict(self):
        self.assertEqual(afi.classify_statusline({"statusLine": "oops"}), "missing")

    def test_classify_missing_when_command_absent(self):
        self.assertEqual(afi.classify_statusline({"statusLine": {"type": "command"}}), "missing")

    def test_classify_ours_when_marker_in_command(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "C:\\Users\\x\\.claude\\ai_fluency_insights_statusline.js"'}}
        self.assertEqual(afi.classify_statusline(settings), "ours")

    def test_classify_foreign_when_other_command(self):
        settings = {"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}
        self.assertEqual(afi.classify_statusline(settings), "foreign")

    def test_classify_missing_when_command_not_a_string(self):
        # A non-string command (e.g. an int, from a hand-edited settings.json)
        # must not raise TypeError from `in` on a non-string. It's treated as
        # "missing" (no usable command string present), consistent with the
        # existing convention that any malformed shape -- statusLine not a
        # dict, command key absent -- also classifies as "missing" rather
        # than "foreign".
        settings = {"statusLine": {"type": "command", "command": 12345}}
        self.assertEqual(afi.classify_statusline(settings), "missing")

    def test_merge_adds_full_block_when_missing(self):
        new = afi.merge_statusline_config({}, "/home/x/.claude/ai_fluency_insights_statusline.js")
        self.assertEqual(new["statusLine"], {
            "type": "command",
            "command": 'node "/home/x/.claude/ai_fluency_insights_statusline.js"',
            "refreshInterval": 30,
        })

    def test_merge_preserves_other_top_level_keys(self):
        settings = {"model": "opusplan", "enabledPlugins": {"foo": True}}
        new = afi.merge_statusline_config(settings, "/x/ai_fluency_insights_statusline.js")
        self.assertEqual(new["model"], "opusplan")
        self.assertEqual(new["enabledPlugins"], {"foo": True})

    def test_merge_patches_refresh_interval_only_when_ours(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "/x/ai_fluency_insights_statusline.js"'},
                    "model": "opusplan"}
        new = afi.merge_statusline_config(settings, "/x/ai_fluency_insights_statusline.js")
        self.assertEqual(new["statusLine"]["refreshInterval"], 30)
        self.assertEqual(new["statusLine"]["command"], settings["statusLine"]["command"])
        self.assertEqual(new["model"], "opusplan")

    def test_merge_is_a_noop_when_already_correct(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "/x/ai_fluency_insights_statusline.js"',
                                    "refreshInterval": 30}}
        new = afi.merge_statusline_config(settings, "/x/ai_fluency_insights_statusline.js")
        self.assertEqual(new, settings)

    def test_classify_legacy_pre_rename_install_as_ours(self):
        settings = {"statusLine": {"type": "command",
                                    "command": 'node "/x/dev_token_dashboard_statusline.js"'}}
        self.assertEqual(afi.classify_statusline(settings), "ours")

    def test_merge_migrates_legacy_install_to_new_script_path(self):
        settings = {"statusLine": {"type": "command", "padding": 2,
                                    "command": 'node "/x/dev_token_dashboard_statusline.js"'}}
        new = afi.merge_statusline_config(settings, "/x/ai_fluency_insights_statusline.js")
        self.assertEqual(new["statusLine"], {
            "type": "command", "padding": 2, "refreshInterval": 30,
            "command": 'node "/x/ai_fluency_insights_statusline.js"',
        })


class LoadSettingsJsonTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict_no_error(self):
        settings, err = afi.load_settings_json("/no/such/settings.json")
        self.assertEqual(settings, {})
        self.assertIsNone(err)

    def test_valid_json_returns_parsed_dict(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"model": "opusplan"}')
            settings, err = afi.load_settings_json(path)
            self.assertEqual(settings, {"model": "opusplan"})
            self.assertIsNone(err)

    def test_malformed_json_returns_error_not_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            settings, err = afi.load_settings_json(path)
            self.assertIsNone(settings)
            self.assertIsNotNone(err)


class ClaudeConfigDirTests(unittest.TestCase):
    def test_uses_env_var_when_set(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/custom/dir"}):
            self.assertEqual(afi.claude_config_dir(), "/custom/dir")

    def test_falls_back_to_home_claude(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            expected = os.path.join(os.path.expanduser("~"), ".claude")
            self.assertEqual(afi.claude_config_dir(), expected)


class NodeAvailableTests(unittest.TestCase):
    def test_true_when_which_finds_node(self):
        with patch.object(afi.shutil, "which", return_value=r"C:\nodejs\node.exe"):
            self.assertTrue(afi.node_available())

    def test_false_when_which_finds_nothing(self):
        with patch.object(afi.shutil, "which", return_value=None):
            self.assertFalse(afi.node_available())


class WriteSettingsWithBackupTests(unittest.TestCase):
    def test_no_backup_when_file_did_not_exist(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            backup = afi.write_settings_with_backup(path, {"a": 1})
            self.assertIsNone(backup)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"a": 1})

    def test_backs_up_exact_prior_content_then_writes_new(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            # Use multi-line JSON with embedded newlines to detect text-mode translation bugs
            original_content = '{\n  "old": true\n}'
            with open(path, "w", encoding="utf-8") as f:
                f.write(original_content)
            # Read the original bytes before calling the function
            with open(path, "rb") as f:
                original_bytes = f.read()
            backup = afi.write_settings_with_backup(path, {"new": True})
            self.assertIsNotNone(backup)
            self.assertTrue(os.path.exists(backup))
            # Check backup filename format
            self.assertRegex(backup, r".*\.bak-\d+$")
            # Verify backup preserves exact bytes (binary comparison to catch newline translation)
            with open(backup, "rb") as f_backup:
                backup_bytes = f_backup.read()
            self.assertEqual(backup_bytes, original_bytes)
            # Verify new settings were written
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"new": True})

    def test_written_file_is_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            afi.write_settings_with_backup(path, {"statusLine": {"type": "command"}})
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"statusLine": {"type": "command"}})


class RunSetupNotificationsTests(unittest.TestCase):
    def _run(self, claude_dir, **kwargs):
        prints = []
        kwargs.setdefault("print_fn", prints.append)
        kwargs.setdefault("isatty_fn", lambda: True)
        code = afi.run_setup_notifications(claude_dir=claude_dir, **kwargs)
        return code, prints

    @patch.object(afi, "node_available", return_value=True)
    def test_dry_run_writes_nothing(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, dry_run=True, input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertFalse(os.path.exists(os.path.join(d, afi.STATUSLINE_MARKER)))
            self.assertTrue(any("Dry run" in p for p in prints))

    @patch.object(afi, "node_available", return_value=True)
    def test_missing_statusline_writes_after_yes(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: "y")
            self.assertEqual(code, 0)
            settings_path = os.path.join(d, "settings.json")
            js_path = os.path.join(d, afi.STATUSLINE_MARKER)
            self.assertTrue(os.path.exists(settings_path))
            self.assertTrue(os.path.exists(js_path))
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
            self.assertIn(afi.STATUSLINE_MARKER, settings["statusLine"]["command"])
            self.assertEqual(settings["statusLine"]["refreshInterval"], 30)

    @patch.object(afi, "node_available", return_value=True)
    def test_missing_statusline_declines_on_no(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: "n")
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertTrue(any("Cancelled" in p for p in prints))

    @patch.object(afi, "node_available", return_value=True)
    def test_ours_already_correct_reports_no_changes(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            js_path = os.path.join(d, afi.STATUSLINE_MARKER)
            existing = {"statusLine": {"type": "command",
                                        "command": f'node "{js_path}"', "refreshInterval": 30}}
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(existing, f)
            # The script file must actually exist on disk for this to be a true
            # no-op (see test_ours_correct_but_script_file_missing_falls_through_to_write
            # for the case where it doesn't).
            with open(js_path, "w", encoding="utf-8") as f:
                f.write("// existing script")
            code, prints = self._run(d, input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 0)
            self.assertTrue(any("Already configured" in p for p in prints))
            with open(settings_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), existing)  # untouched

    @patch.object(afi, "node_available", return_value=True)
    def test_ours_correct_but_script_file_missing_falls_through_to_write(self, _node):
        # Regression test for Finding 1: settings already match (refreshInterval
        # 30, "ours" command) but the .js file itself is missing from disk --
        # e.g. deleted, or a settings.json that arrived on a second device
        # without its companion script. The wizard must NOT claim "Already
        # configured" (the hook is actually broken -- node has nothing to run)
        # and must instead proceed to the write flow so it can be repaired.
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            js_path = os.path.join(d, afi.STATUSLINE_MARKER)
            existing = {"statusLine": {"type": "command",
                                        "command": f'node "{js_path}"', "refreshInterval": 30}}
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(existing, f)
            self.assertFalse(os.path.exists(js_path))  # sanity: script truly absent

            code, prints = self._run(d, input_fn=lambda _: "y")

            self.assertEqual(code, 0)
            self.assertFalse(any("Already configured" in p for p in prints))
            self.assertTrue(any("This will write" in p for p in prints))
            self.assertTrue(os.path.exists(js_path))  # repaired

    @patch.object(afi, "node_available", return_value=False)
    def test_missing_node_aborts_without_writing(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 1)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertTrue(any("node" in p.lower() for p in prints))

    def test_malformed_settings_aborts_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                f.write("{not json")
            code, prints = self._run(d, input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 1)
            self.assertTrue(any("not valid JSON" in p for p in prints))

    @patch.object(afi, "node_available", return_value=True)
    def test_non_interactive_without_dry_run_aborts(self, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, isatty_fn=lambda: False,
                                      input_fn=lambda _: self.fail("must not prompt"))
            self.assertEqual(code, 1)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
            self.assertTrue(any("not running interactively" in p.lower() for p in prints))

    @patch.object(afi, "node_available", return_value=True)
    def test_final_report_shown_after_successful_write(self, _node):
        with tempfile.TemporaryDirectory() as d:
            _, prints = self._run(d, input_fn=lambda _: "y")
            self.assertTrue(any("Rate-limit capture:" in p for p in prints))
            self.assertTrue(any("Toast notifications:" in p for p in prints))

    @patch.object(afi, "node_available", return_value=True)
    @patch.object(afi, "write_settings_with_backup", side_effect=OSError("Permission denied"))
    def test_write_permission_error_reported_not_crashed(self, _write, _node):
        with tempfile.TemporaryDirectory() as d:
            code, prints = self._run(d, input_fn=lambda _: "y")
            self.assertEqual(code, 1)
            self.assertTrue(any("permission denied" in p.lower() for p in prints))
            # the statusline script write happens before the settings write in
            # source order, so it may exist -- but settings.json must not, since
            # write_settings_with_backup is what raised before touching it
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))


class ShouldRunNotificationsTests(unittest.TestCase):
    def test_true_by_default(self):
        self.assertTrue(afi.should_run_notifications(dict(afi.NOTIFY, enabled=True)))

    def test_false_when_disabled(self):
        self.assertFalse(afi.should_run_notifications(dict(afi.NOTIFY, enabled=False)))

    def test_true_when_key_absent(self):
        notify = dict(afi.NOTIFY)
        del notify["enabled"]
        self.assertTrue(afi.should_run_notifications(notify))

    def test_defaults_to_module_level_notify(self):
        with patch.dict(afi.NOTIFY, {"enabled": False}):
            self.assertFalse(afi.should_run_notifications())


class ForeignStatuslineTests(unittest.TestCase):
    def test_extract_script_path_from_quoted_command(self):
        self.assertEqual(
            afi._extract_script_path('node "C:\\Users\\x\\.claude\\statusline.js"'),
            "C:\\Users\\x\\.claude\\statusline.js")

    def test_extract_script_path_returns_none_when_unquoted(self):
        self.assertIsNone(afi._extract_script_path("~/.claude/statusline.sh"))

    @patch.object(afi, "node_available", return_value=True)
    def test_foreign_statusline_does_not_write_settings(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            original = {"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(original, f)
            prints = []
            code = afi.run_setup_notifications(claude_dir=d, print_fn=prints.append,
                                                input_fn=lambda _: self.fail("must not prompt"),
                                                isatty_fn=lambda: True)
            self.assertEqual(code, 0)
            with open(settings_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), original)
            self.assertFalse(os.path.exists(os.path.join(d, afi.STATUSLINE_MARKER)))
            self.assertTrue(any("existing statusline" in p.lower() for p in prints))

    @patch.object(afi, "node_available", return_value=True)
    def test_foreign_statusline_reports_fresh_capture_if_present(self, _node):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"statusLine": {"type": "command", "command": "node ~/.claude/statusline.js"}}, f)
            rl_path = os.path.join(d, "rate_limits_latest.json")
            with open(rl_path, "w", encoding="utf-8") as f:
                json.dump({"rate_limits": {}, "captured_at": afi.time.time()}, f)
            prints = []
            with patch.object(afi, "RATE_LIMITS_PATH", rl_path):
                afi.run_setup_notifications(claude_dir=d, print_fn=prints.append,
                                             input_fn=lambda _: self.fail("must not prompt"),
                                             isatty_fn=lambda: True)
            self.assertTrue(any("already has a fresh capture" in p for p in prints))

    @patch.object(afi, "node_available", return_value=True)
    def test_foreign_statusline_catches_non_oserror_exceptions(self, _node):
        """Verify that non-OSError exceptions (e.g. ValueError from isfile) are caught."""
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"statusLine": {"type": "command", "command": 'node "/bad/path"'}}, f)
            prints = []
            # Mock os.path.isfile to raise ValueError (simulating a path with null bytes)
            with patch.object(afi.os.path, "isfile", side_effect=ValueError("null byte in path")):
                code = afi.run_setup_notifications(claude_dir=d, print_fn=prints.append,
                                                    input_fn=lambda _: self.fail("must not prompt"),
                                                    isatty_fn=lambda: True)
            # Should complete without raising the ValueError
            self.assertEqual(code, 0)
            # Should still print the existing statusline message and final report
            self.assertTrue(any("existing statusline" in p.lower() for p in prints))
            self.assertTrue(any("Rate-limit capture:" in p for p in prints))


class StartupHintTests(unittest.TestCase):
    def test_none_on_non_windows_even_if_missing(self):
        self.assertIsNone(afi.startup_hint("/no/such/path.json", is_windows=False,
                                            statusline_js_path="/no/such/statusline.js"))

    def test_hint_when_windows_and_never_captured(self):
        hint = afi.startup_hint("/no/such/path.json", is_windows=True,
                                 statusline_js_path="/no/such/statusline.js")
        self.assertIsNotNone(hint)
        self.assertIn("--setup-notifications", hint)

    def test_none_when_file_already_exists(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rate_limits_latest.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
            self.assertIsNone(afi.startup_hint(path, is_windows=True,
                                                statusline_js_path="/no/such/statusline.js"))

    def test_none_when_statusline_script_already_exists(self):
        # right after a successful --setup-notifications run, rate_limits_latest.json
        # doesn't exist yet (it only appears after the next Claude Code message) --
        # but the managed script does, so the nudge would be confusing here and
        # must be suppressed.
        with tempfile.TemporaryDirectory() as d:
            js_path = os.path.join(d, afi.STATUSLINE_MARKER)
            with open(js_path, "w", encoding="utf-8") as f:
                f.write("// managed")
            self.assertIsNone(afi.startup_hint("/no/such/rate_limits.json", is_windows=True,
                                                statusline_js_path=js_path))


class PrintFinalReportTests(unittest.TestCase):
    def test_toast_on_when_enabled_and_windows(self):
        prints = []
        with patch.object(afi.os, "name", "nt"), \
             patch.dict(afi.NOTIFY, {"enabled": True}):
            afi._print_final_report(prints.append)
        self.assertTrue(any("Toast notifications: ON" in p for p in prints), prints)

    def test_toast_off_when_disabled_on_windows(self):
        prints = []
        with patch.object(afi.os, "name", "nt"), \
             patch.dict(afi.NOTIFY, {"enabled": False}):
            afi._print_final_report(prints.append)
        self.assertTrue(any(p == "Toast notifications: OFF"
                             '  (edit NOTIFY["enabled"] in the script to change)'
                             for p in prints), prints)

    def test_toast_off_windows_only_on_non_windows(self):
        prints = []
        with patch.object(afi.os, "name", "posix"), \
             patch.dict(afi.NOTIFY, {"enabled": True}):
            afi._print_final_report(prints.append)
        self.assertTrue(any("Toast notifications: OFF (Windows only)" in p for p in prints), prints)


class ReportForeignStatuslineSizeGuardTests(unittest.TestCase):
    @staticmethod
    def _settings_for(command):
        return {"statusLine": {"type": "command", "command": command}}

    def test_skips_reading_a_large_foreign_script(self):
        with tempfile.TemporaryDirectory() as d:
            big_path = os.path.join(d, "huge.js")
            with open(big_path, "wb") as f:
                # the string IS present, so a guard-less read would find it --
                # this proves the size guard is what skips it, not the content
                f.write(b"// references rate_limits\n")
                f.seek(1_000_001)
                f.write(b"\0")
            prints = []
            afi._report_foreign_statusline(
                self._settings_for(f'node "{big_path}"'), prints.append)
            self.assertFalse(any("references rate_limits" in p for p in prints), prints)

    def test_still_reads_a_small_foreign_script(self):
        with tempfile.TemporaryDirectory() as d:
            small_path = os.path.join(d, "small.js")
            with open(small_path, "w", encoding="utf-8") as f:
                f.write("// writes rate_limits somewhere")
            prints = []
            afi._report_foreign_statusline(
                self._settings_for(f'node "{small_path}"'), prints.append)
            self.assertTrue(any("references rate_limits" in p for p in prints), prints)


class BundledStatuslineJsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_bundled_js_is_syntactically_valid(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "statusline.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(afi.BUNDLED_STATUSLINE_JS)
            result = subprocess.run(["node", "--check", path],
                                     capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class PricingTests(unittest.TestCase):
    def test_sonnet_rate_matches_current_anthropic_pricing(self):
        # Sonnet 5 is $2/$10 per MTok, confirmed permanent (the planned
        # $3/$15 rise was cancelled) -- see the token-economics brainstorm.
        self.assertEqual(afi.price_for("claude-sonnet-5"), {"in": 2.0, "out": 10.0})


class ColdStartAndCacheHealthTests(unittest.TestCase):
    """totals.cold_start_pct and totals.cache_reset_pct -- see the
    token-economics brainstorm's 'cold-start tax' and 'healthy loop'
    concepts. Both are derived from per-session cost tracking in
    touch_session/parse_entry and aggregated in stats_to_json."""

    @staticmethod
    def _write_jsonl(path, entries):
        with open(path, "w", encoding="utf-8") as f:
            for obj in entries:
                f.write(afi.json.dumps(obj) + "\n")

    @staticmethod
    def _usage_entry(sid, uid, ts, cache_write=0, cache_read=0,
                      input_tokens=0, output_tokens=0, model="claude-sonnet-5"):
        return {"type": "assistant", "sessionId": sid, "timestamp": ts, "uuid": uid,
                "message": {"id": uid, "model": model,
                            "usage": {"input_tokens": input_tokens,
                                      "output_tokens": output_tokens,
                                      "cache_creation_input_tokens": cache_write,
                                      "cache_read_input_tokens": cache_read}}}

    def test_cold_start_pct_weights_by_session_cost(self):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            entries = [
                # "solo": one call, entirely a cache write -> its whole cost
                # is cold-start cost. cost = 1_000_000 * 2.5/1e6 = $2.50
                self._usage_entry("solo", "u1", "2026-01-01T00:00:00Z",
                                   cache_write=1_000_000),
                # "warm": first call writes the prefix ($2.50), then two
                # cheap cache-read calls ($0.20 each) -> session cost $2.90,
                # of which $2.50 is the first call
                self._usage_entry("warm", "u2", "2026-01-01T01:00:00Z",
                                   cache_write=1_000_000),
                self._usage_entry("warm", "u3", "2026-01-01T01:01:00Z",
                                   cache_read=1_000_000),
                self._usage_entry("warm", "u4", "2026-01-01T01:02:00Z",
                                   cache_read=1_000_000),
            ]
            self._write_jsonl(os.path.join(proj, "a.jsonl"), entries)
            scanner = afi.Scanner(root)
            st = scanner.build_stats()
            totals = afi.stats_to_json(st)["totals"]
            # (2.50 + 2.50) / (2.50 + 2.90) * 100 = 92.6
            self.assertAlmostEqual(totals["cold_start_pct"], 92.6, places=1)
            # neither warm's follow-up call is a mid-session reset (reads
            # outweigh writes on both), so the health signal stays clean
            self.assertEqual(totals["cache_reset_pct"], 0.0)

    def test_cache_reset_pct_flags_mid_session_cache_invalidation(self):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            entries = [
                self._usage_entry("sess", "u1", "2026-01-01T00:00:00Z",
                                   cache_write=1_000_000),
                # mid-session reset: new material outweighs reused material
                self._usage_entry("sess", "u2", "2026-01-01T00:01:00Z",
                                   cache_write=500_000, cache_read=100_000),
                # back to a normal, healthy cache read
                self._usage_entry("sess", "u3", "2026-01-01T00:02:00Z",
                                   cache_read=1_000_000),
            ]
            self._write_jsonl(os.path.join(proj, "a.jsonl"), entries)
            scanner = afi.Scanner(root)
            st = scanner.build_stats()
            totals = afi.stats_to_json(st)["totals"]
            # 1 of the 2 non-first calls was a reset -> 50%
            self.assertEqual(totals["cache_reset_pct"], 50.0)


class AdoptionTests(unittest.TestCase):
    """build_adoption(): the My Adoption tab's score, pillars, features and coach."""

    @staticmethod
    def _stamp(days_ago, hour=10, minute=0):
        t = (afi.datetime.now().astimezone().replace(hour=hour, minute=minute, second=0, microsecond=0)
             - afi.timedelta(days=days_ago))
        return t.astimezone(afi.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def _session(self, sid, days_ago, tools=(), prompt="please add pagination to the orders list and keep the style",
                 hour=10, n=0):
        ts = self._stamp(days_ago, hour)
        es = [{"type": "user", "sessionId": sid, "timestamp": ts, "cwd": "/nonexistent/proj",
               "message": {"role": "user", "content": prompt}}]
        blocks = [{"type": "tool_use", "id": "tu_%s_%d" % (sid, i), "name": nm, "input": inp}
                  for i, (nm, inp) in enumerate(tools)]
        es.append({"type": "assistant", "sessionId": sid, "timestamp": ts, "uuid": "m_" + sid,
                   "requestId": "r_" + sid,
                   "message": {"id": "m_" + sid, "model": "claude-sonnet-5",
                               "content": blocks,
                               "usage": {"input_tokens": 10, "output_tokens": 500,
                                         "cache_creation_input_tokens": 0,
                                         "cache_read_input_tokens": 1000}}})
        return es

    def _build(self, sessions):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        proj = os.path.join(root, "proj")
        os.makedirs(proj)
        with open(os.path.join(proj, "a.jsonl"), "w", encoding="utf-8") as f:
            for es in sessions:
                for e in es:
                    f.write(afi.json.dumps(e) + "\n")
        return afi.build_adoption(afi.Scanner(root).build_stats())

    def test_no_data_is_not_ready(self):
        a = self._build([])
        self.assertFalse(a["ready"])
        self.assertEqual(a["active_days"], 0)

    def test_baseline_needed_below_min_active_days(self):
        a = self._build([self._session("a", 0), self._session("b", 1)])
        self.assertFalse(a["ready"])
        self.assertEqual(a["active_days"], 2)
        self.assertEqual(a["min_days"], afi.ADOPTION["min_active_days"])

    def test_ready_payload_shape_and_ranges(self):
        edit = ("Edit", {"file_path": "/x/a.py", "new_string": "\n".join("l%d" % i for i in range(40))})
        sessions = [self._session("s%d" % i, i * 2, tools=[edit, ("Bash", {"command": "pytest"})])
                    for i in range(8)]
        a = self._build(sessions)
        self.assertTrue(a["ready"])
        self.assertTrue(0 <= a["score"] <= 100)
        self.assertEqual([p["id"] for p in a["pillars"]], ["habit", "range", "craft", "impact"])
        for p in a["pillars"]:
            self.assertTrue(0 <= p["score"] <= 100)
        self.assertEqual(len(a["weekly"]), 12)
        self.assertEqual(len(a["calendar"]["cells"]), 17 * 7)
        self.assertLessEqual(len(a["coach"]), 3)
        self.assertEqual(a["level"]["name"], afi.adoption_level(a["score"])["name"])
        # the overall score really is the weighted blend of the four pillars
        wt = afi.ADOPTION["weights"]
        blend = sum(wt[p["id"]] * p["score"] for p in a["pillars"])
        self.assertLessEqual(abs(a["score"] - blend), 1)

    def test_feature_detection_from_tool_calls(self):
        tools = [("ExitPlanMode", {}), ("Agent", {"subagent_type": "Explore"}),
                 ("WebSearch", {"query": "x"}), ("mcp__github__list_issues", {}),
                 ("Skill", {"skill": "x"}), ("Bash", {"command": "ls"})]
        a = self._build([self._session("a", 0, tools=tools), self._session("b", 1),
                         self._session("c", 2)])
        used = {f["id"]: f["recent"] for f in a["range"]["features"]}
        for fid in ("plan", "subagents", "web", "mcp", "skills", "verify"):
            self.assertGreater(used[fid], 0, fid)
        self.assertEqual(used["slash"], 0)
        # unused features surface as coach suggestions, never more than two
        feats = [c for c in a["coach"] if c["feature"]]
        self.assertLessEqual(len(feats), 2)
        for c in feats:
            self.assertEqual(used[c["feature"]], 0)

    def test_slash_command_counts_as_a_feature(self):
        cmd = [{"type": "user", "sessionId": "z", "timestamp": self._stamp(0),
                "message": {"role": "user",
                            "content": "<command-name>/compact</command-name>"}}]
        a = self._build([cmd, self._session("b", 1), self._session("c", 2)])
        used = {f["id"]: f["recent"] for f in a["range"]["features"]}
        self.assertEqual(used["slash"], 1)

    def test_late_night_activity_triggers_wellbeing_coaching(self):
        sessions = [self._session("n%d" % i, i, hour=23 if i % 2 == 0 else 1) for i in range(6)]
        a = self._build(sessions)
        self.assertGreater(a["wellbeing"]["late_pct"], 20)
        self.assertTrue(any(c["area"] == "wellbeing" for c in a["coach"]))

    def test_old_activity_ages_out_of_the_window(self):
        a = self._build([self._session("a", 0), self._session("b", 1), self._session("c", 2),
                         self._session("old", 60, tools=[("WebSearch", {"query": "x"})])])
        used = {f["id"]: f["recent"] for f in a["range"]["features"]}
        self.assertEqual(used["web"], 0)
        self.assertEqual(next(f for f in a["range"]["features"] if f["id"] == "web")["ever"], True)


class AdoptionLevelAndFluencyTests(unittest.TestCase):
    def test_level_boundaries(self):
        names = {0: "Explorer", 19: "Explorer", 20: "Practitioner", 39: "Practitioner",
                 40: "Power user", 59: "Power user", 60: "Expert", 74: "Expert",
                 75: "Pro", 89: "Pro", 90: "Pro Max", 100: "Pro Max"}
        for score, name in names.items():
            self.assertEqual(afi.adoption_level(score)["name"], name, score)

    def test_points_to_next_level(self):
        lv = afi.adoption_level(65)
        self.assertEqual(lv["to_next"], 10)
        self.assertEqual(lv["next_name"], "Pro")
        self.assertEqual(afi.adoption_level(95)["to_next"], 0)

    def test_pro_needs_every_pillar_above_the_floor(self):
        strong = {"habit": 90, "range": 80, "craft": 85, "impact": 80}
        self.assertEqual(afi.adoption_level(80, strong)["name"], "Pro")
        weak = dict(strong, range=40)
        lv = afi.adoption_level(80, weak)
        self.assertEqual(lv["name"], "Expert")
        self.assertIn("Range", lv["blocker"])

    def test_pro_max_needs_a_sustained_run(self):
        pillars = {"habit": 95, "range": 90, "craft": 92, "impact": 88}
        steady = [{"score": 91}] * 4
        self.assertEqual(afi.adoption_level(93, pillars, steady)["name"], "Pro Max")
        lv = afi.adoption_level(93, pillars, [{"score": 70}, {"score": 91}, {"score": 91}, {"score": 92}])
        self.assertEqual(lv["name"], "Pro")
        self.assertIn("4 weeks", lv["blocker"])
        self.assertEqual(afi.adoption_level(93, pillars, [{"score": 95}])["name"], "Pro")

    def test_fluency_matches_documented_formulas(self):
        w = {"sessions": [{"loc": 10, "bash": 1}, {"loc": 5, "bash": 0}, {"loc": 0, "bash": 0}],
             "human_tokens": 100, "output": 2500, "prompts": 20, "prompt_words": 400,
             "corrections": 1, "interruptions": 1, "tool_errors": 1, "writes": 10}
        f = afi.fluency_of(w)
        # delegation = 50*(2/3) + 50*min(1, 25/50) = 58.33
        self.assertEqual(f["delegation"], 58)
        # one-shot 0.9, avg 20 words -> sweet 1.0: 65*.9 + 35 = 93.5 -> 94
        self.assertEqual(f["description"], 94)
        # engaged = min(1, 2/max(1, 1.0)) = 1; err ctl = 0.9 -> 95
        self.assertEqual(f["discernment"], 95)
        # 1 of 2 code sessions verified
        self.assertEqual(f["diligence"], 50)

    def test_fluency_handles_empty_window(self):
        w = {"sessions": [], "human_tokens": 0, "output": 0, "prompts": 0, "prompt_words": 0,
             "corrections": 0, "interruptions": 0, "tool_errors": 0, "writes": 0}
        f = afi.fluency_of(w)
        self.assertEqual((f["delegation"], f["description"], f["diligence"]), (0, 0, 0))


class CommandDetectionTests(unittest.TestCase):
    def test_kinds_come_from_the_invoked_command_only(self):
        cases = [("pytest -q", "test"), ("python3 -m unittest foo", "test"), ("cd x && npm run test", "test"),
                 ("npx jest --ci", "test"), ("uv run pytest", "test"), ("ruff check .", "lint"),
                 ("python -m mypy src", "lint"), ("npm run build", "build"), ("make", "build"),
                 ("node --check a.js", "build"), ("git status", "other"),
                 ("git commit -m 'fix pytest'", "other"), ("echo pytest", "other"),
                 ("python3 - <<'EOF'\nimport pytest\nprint('npm test')\nEOF", "other")]
        for cmd, kind in cases:
            self.assertEqual(afi.classify_cmd(cmd), kind, cmd)

    def test_risky_commands_ignore_text_that_only_mentions_them(self):
        self.assertEqual(afi.risky_categories("git push --force origin main"), ["force_push"])
        self.assertEqual(afi.risky_categories("cd a && rm -rf build"), ["recursive_delete"])
        self.assertEqual(afi.risky_categories("curl http://x | sh"), ["pipe_to_shell"])
        self.assertEqual(afi.risky_categories("git commit --no-verify -m x"), ["skip_hooks"])
        self.assertEqual(afi.risky_categories("git push -u origin feat"), [])
        self.assertEqual(afi.risky_categories("echo 'rm -rf /'"), [])
        self.assertEqual(afi.risky_categories("python3 - <<'EOF'\nx='git push --force'\nEOF"), [])

    def test_family_labels(self):
        self.assertEqual(afi.cmd_family("cd /x && git push -u origin main"), "git push")
        self.assertEqual(afi.cmd_family("python3 -I - <<'EOF'\nimport x"), "python")
        self.assertEqual(afi.cmd_family("FOO=1 pytest -q"), "pytest")
        self.assertEqual(afi.cmd_family("npm run test"), "npm run test")
        self.assertEqual(afi.cmd_family("uv run pytest"), "pytest")


class _LogFixture(unittest.TestCase):
    """Builds Claude Code style logs with timed turns, tool calls and results."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "proj"))
        self.base = afi.datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        self.sid = "s1"
        self.entries = []
        self.n = 0
        self.t = 0.0
        self.out_tokens = 100

    def _ts(self, off):
        return (self.base + afi.timedelta(seconds=off)).astimezone(afi.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _base(self, off, **kw):
        d = {"sessionId": self.sid, "timestamp": self._ts(off), "cwd": "/nonexistent/p", "version": "2.1.296"}
        d.update(kw)
        return d

    def prompt(self, text, gap=5):
        self.t += gap
        self.entries.append(self._base(self.t, type="user", message={"role": "user", "content": text}))

    def tool(self, name, inp, run=1.0, fail=False, text="ok", perm=None):
        self.n += 1
        tid = "tu%d" % self.n
        self.t += 1
        self.entries.append(self._base(self.t, type="assistant", uuid="m%d" % self.n, requestId="r%d" % self.n,
            message={"id": "m%d" % self.n, "model": "claude-sonnet-5", "stop_reason": "tool_use",
                     "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
                     "usage": {"input_tokens": 10, "output_tokens": self.out_tokens, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 1000}}))
        self.t += run
        res = {"type": "tool_result", "tool_use_id": tid, "content": text}
        if fail:
            res["is_error"] = True
        e = self._base(self.t, type="user", message={"role": "user", "content": [res]})
        if perm:
            e["permissionDecision"] = perm
        self.entries.append(e)

    def end_turn(self, gap=2):
        self.n += 1
        self.t += gap
        self.entries.append(self._base(self.t, type="assistant", uuid="m%d" % self.n, requestId="r%d" % self.n,
            message={"id": "m%d" % self.n, "model": "claude-sonnet-5", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "done"}],
                     "usage": {"input_tokens": 10, "output_tokens": 50, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 1000}}))

    def edit(self, path="/p/src/a.py", n=3, **kw):
        self.tool("Edit", {"file_path": path, "new_string": "\n".join("x" for _ in range(n))}, **kw)

    def stats(self):
        with open(os.path.join(self.root, "proj", "a.jsonl"), "w", encoding="utf-8") as f:
            for e in self.entries:
                f.write(afi.json.dumps(e) + "\n")
        return afi.Scanner(self.root).build_stats()


class FlowEngineTests(_LogFixture):
    def test_turn_time_steps_and_clean_flag(self):
        self.prompt("add pagination to the orders list")
        self.edit(run=2.0)
        self.end_turn(gap=3)
        st = self.stats()
        self.assertEqual(len(st.turns), 1)
        t = st.turns[0]
        # prompt at t=5; end_turn at 5 + (1+2) + 3 = 11  ->  6s
        self.assertAlmostEqual(t["secs"], 6.0, places=1)
        self.assertEqual(t["steps"], 1)
        self.assertFalse(t["err"] or t["intr"] or t["corrected"])

    def test_correction_marks_the_previous_turn(self):
        self.prompt("add pagination")
        self.end_turn()
        self.prompt("no, that's wrong, use the helper")
        self.end_turn()
        st = self.stats()
        self.assertTrue(st.turns[0]["corrected"])
        self.assertFalse(st.turns[1]["corrected"])

    def test_tool_latency_pairs_call_and_result(self):
        self.prompt("run it")
        self.tool("Bash", {"command": "pytest -q"}, run=4.0)
        self.end_turn()
        st = self.stats()
        rows = [r for r in st.tool_calls if r[2] == "pytest"]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][3], 4.0, places=1)
        self.assertEqual(st.hc["tool_results_matched"], st.hc["tool_results"])

    def test_failure_episode_recovers_and_counts_steps(self):
        self.prompt("fix the build")
        self.tool("Bash", {"command": "npm run build"}, fail=True, text="Exit code 1\nerr")
        self.tool("Read", {"file_path": "/p/a.py"})
        self.tool("Bash", {"command": "npm run build"})
        self.end_turn()
        st = self.stats()
        self.assertEqual(len(st.episodes), 1)
        day, tool, fails, steps = st.episodes[0]
        self.assertEqual((tool, fails, steps), ("Bash", 1, 2))
        self.assertEqual(st.hc["exit_code_text"], 1)

    def test_three_failures_in_a_row_is_a_stuck_loop_and_unrecovered(self):
        self.prompt("fix the build")
        for _ in range(3):
            self.tool("Bash", {"command": "pytest"}, fail=True, text="Exit code 1")
        self.end_turn()
        f = afi.build_flow(self.stats())
        self.assertEqual((f["episodes"], f["stuck"], f["unrecovered"], f["recovered"]), (1, 1, 1, 0))
        self.assertEqual(f["recovery_pct"], 0)

    def test_flow_needs_five_turns_for_a_score(self):
        for i in range(4):
            self.prompt("task %d" % i)
            self.end_turn()
        self.assertIsNone(afi.build_flow(self.stats())["score"])
        self.prompt("task 5")
        self.end_turn()
        f = afi.build_flow(self.stats())
        self.assertIsNotNone(f["score"])
        self.assertEqual(f["clean_pct"], 100)

    def test_interrupted_turn_is_not_clean(self):
        self.prompt("do the thing")
        self.entries.append(self._base(self.t + 1, type="user",
                                       message={"role": "user", "content": "[Request interrupted by user]"}))
        self.prompt("do it differently")
        self.end_turn()
        st = self.stats()
        self.assertEqual(len(st.turns), 2)
        self.assertTrue(st.turns[0]["intr"])
        self.assertIsNone(st.turns[0]["secs"])


class QualityEngineTests(_LogFixture):
    def _session(self, edit_first=True):
        self.prompt("change it")
        if edit_first:
            self.edit()
            self.tool("Bash", {"command": "pytest -q"})
        else:
            self.tool("Bash", {"command": "pytest -q"})
            self.edit()
        self.end_turn()

    def test_verified_means_a_check_ran_after_the_last_edit(self):
        self._session(edit_first=True)
        q = afi.build_quality(self.stats())
        self.assertEqual(q["edited_sessions"], 1)
        self.assertEqual(q["verified_pct"], 100)

    def test_check_before_the_edit_does_not_count(self):
        self._session(edit_first=False)
        q = afi.build_quality(self.stats())
        self.assertEqual(q["verified_pct"], 0)

    def test_first_run_pass_and_fail_then_pass_recovery(self):
        self.prompt("fix")
        self.edit()
        self.tool("Bash", {"command": "pytest"}, fail=True, text="Exit code 1")
        self.tool("Bash", {"command": "pytest"})
        self.end_turn()
        q = afi.build_quality(self.stats())
        self.assertEqual(q["first_run_pass_pct"], 0)
        self.assertEqual(q["runs"]["test"]["runs"], 2)
        self.assertEqual((q["runs"]["test"]["fail_groups"], q["runs"]["test"]["recovered_groups"]), (1, 1))

    def test_tests_alongside_counts_test_file_edits(self):
        self.prompt("add feature with tests")
        self.edit("/p/src/a.py")
        self.edit("/p/tests/test_a.py")
        self.end_turn()
        q = afi.build_quality(self.stats())
        self.assertEqual(q["tests_alongside_pct"], 100)

    def test_reedit_share_and_hotspot(self):
        self.prompt("iterate")
        for _ in range(6):
            self.edit("/p/src/hot.py")
        self.edit("/p/src/other.py")
        self.end_turn()
        q = afi.build_quality(self.stats())
        # 7 edits, 5 of them revisit a file already edited this session
        self.assertEqual(q["reedit_pct"], 71)
        self.assertEqual(q["hotspots"][0]["file"], "hot.py")
        self.assertEqual(q["hotspots"][0]["edits"], 6)

    def test_safety_signals_and_permission_decisions(self):
        self.prompt("clean up")
        self.tool("Bash", {"command": "git push --force origin main"},
                  perm={"decision": "accept", "source": "user", "reasonType": "rule"})
        self.tool("Read", {"file_path": "/p/.env"})
        self.tool("Bash", {"command": "python3 - <<'EOF'\nx = 'rm -rf /'\nEOF"})
        self.end_turn()
        q = afi.build_quality(self.stats())
        self.assertEqual(q["safety"], {"force_push": 1, "secret_file_touched": 1})
        self.assertEqual(q["permissions"], [{"decision": "accept", "source": "user", "n": 1}])

    def test_subagent_work_does_not_end_the_main_turn(self):
        self.prompt("research it")
        self.entries.append(self._base(self.t + 1, type="assistant", isSidechain=True, uuid="sc1", requestId="rsc",
            message={"id": "sc1", "model": "claude-sonnet-5", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "sub done"}],
                     "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0}}))
        self.t += 2
        self.end_turn(gap=10)
        st = self.stats()
        self.assertEqual(len(st.turns), 1)
        self.assertGreater(st.turns[0]["secs"], 10)


class ValueEngineTests(_LogFixture):
    def test_cache_savings_is_read_discount_minus_write_premium(self):
        self.prompt("hi")
        self.end_turn()
        st = self.stats()
        # one response reading 1000 cached tokens at sonnet $2/M input: 0.9 * 1000 * 2 / 1e6
        saved = sum(d["cache_savings"] for d in st.days.values())
        self.assertAlmostEqual(saved, 0.9 * 1000 * 2.0 / 1e6, places=9)

    def test_cost_per_100_loc_and_per_verified(self):
        self.out_tokens = 1_000_000          # $10 of output per tool response at sonnet pricing
        self.prompt("build")
        self.edit(n=50)
        self.tool("Bash", {"command": "pytest"})
        self.end_turn()
        v = afi.build_value(self.stats())
        self.assertEqual(v["loc"], 50)
        self.assertEqual(v["verified_sessions"], 1)
        self.assertGreater(v["cost_per_verified"], 0)
        self.assertAlmostEqual(v["per_100_loc"], v["cost"] / 50 * 100, places=2)


class HealthAndCatalogTests(_LogFixture):
    def test_missing_signals_are_reported_not_zeroed(self):
        self.prompt("hi")
        self.end_turn()
        h = afi.build_health(self.stats())
        caps = {c["id"]: c for c in h["capabilities"]}
        self.assertEqual(caps["perms"]["status"], "missing")
        self.assertEqual(caps["turns"]["status"], "ok")
        self.assertEqual(h["versions"][0]["version"], "2.1.296")

    def test_catalog_is_complete_and_every_badge_in_the_page_exists(self):
        ids = [m[0] for m in afi.METRIC_CATALOG]
        self.assertEqual(len(ids), len(set(ids)))
        for m in afi.METRIC_CATALOG:
            self.assertIn(m[3], ("observed", "derived", "proxy", "estimated"))
            self.assertTrue(all(m[1:3]) and m[4] and m[5])
        import re as _re
        used = set(_re.findall(r"cb\('([a-z_0-9]+)'\)", afi.HTML))
        used |= set(_re.findall(r",C\.\w+,'([a-z_0-9]+)'", afi.HTML))
        used |= set(_re.findall(r"insHero\([^,]+,[^,]+,[^,]+,'([a-z_0-9]+)'", afi.HTML))
        self.assertTrue(used, "no badge ids found in the page")
        self.assertFalse(used - set(ids), used - set(ids))

    def test_payloads_serialise_to_json(self):
        self.prompt("go")
        self.edit()
        self.tool("Bash", {"command": "pytest"}, fail=True, text="Exit code 1")
        self.end_turn()
        st = self.stats()
        for payload in (afi.build_flow(st), afi.build_quality(st), afi.build_value(st),
                        afi.build_health(st), afi.catalog_payload()):
            afi.json.dumps(payload)


class AdvisorTests(_LogFixture):
    def _many_sessions(self, verified):
        for i in range(6):
            self.sid = "s%d" % i
            self.prompt("make the change number %d to the orders module with tests" % i)
            self.edit()
            if verified:
                self.tool("Bash", {"command": "pytest -q"})
            self.end_turn()
            self.base = self.base  # sessions share the day; time keeps advancing via self.t
        return self.stats()

    def test_unverified_work_triggers_the_verify_recommendation_with_evidence(self):
        st = self._many_sessions(verified=False)
        adv = afi.build_advisor(st, afi.build_adoption(st) or {})
        self.assertTrue(adv["ready"])
        verify = [r for r in adv["recs"] + adv["more"] if r["id"] == "verify"]
        self.assertEqual(len(verify), 1)
        self.assertIn("0%", verify[0]["evidence"])
        self.assertEqual(verify[0]["experiment"]["metric"], "verified_pct")
        self.assertIn("verified_pct", adv["metrics"])

    def test_verified_work_earns_a_win_not_a_warning(self):
        st = self._many_sessions(verified=True)
        adv = afi.build_advisor(st, {"end": afi.build_adoption(st).get("end") or afi.date.today().isoformat()})
        self.assertFalse([r for r in adv["recs"] if r["id"] == "verify"])
        self.assertTrue(any("check the work" in w for w in adv["wins"]))



class SideBySideWithOriginalTests(unittest.TestCase):
    """This multi-AI copy must run next to the original Claude-only dashboard on one laptop."""

    def _serve(self, body):
        """A throwaway localhost server answering every GET with `body`; returns (server, port)."""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv, srv.server_address[1]

    def test_ports_and_task_name_differ_from_the_original(self):
        self.assertEqual((afi.DEFAULT_PORT, afi.ORIGINAL_DASHBOARD_PORT), (8788, 8787))
        self.assertNotIn(afi.STARTUP_TASK, ("AIFluencyInsights", "DevTokenDashboard"))

    def test_a_port_another_server_listens_on_is_refused(self):
        # The original dashboard uses Python's default server, which allows address reuse.
        _, port = self._serve("<title>AI Fluency Insights</title>")
        with self.assertRaises(OSError):
            afi.DashboardServer(("127.0.0.1", port), afi.Handler)

    def test_the_original_dashboard_is_recognised_by_its_page(self):
        _, port = self._serve("<!doctype html><html><head><meta charset=utf-8>"
                              "<title>AI Fluency Insights</title></head><body>" + "x" * 50000)
        self.assertTrue(afi.original_dashboard_running(port))
        _, other = self._serve("<title>Something else</title>")
        self.assertFalse(afi.original_dashboard_running(other))

    def test_nothing_listening_means_not_running(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(afi.original_dashboard_running(port, timeout=0.5))

    @patch.object(afi, "send_windows_toast")
    def test_a_muted_watcher_shows_no_toast(self, mock_toast):
        with tempfile.TemporaryDirectory() as root:
            watcher = afi.NotificationWatcher(afi.Scanner(root),
                                              state_path=os.path.join(root, "state.json"))
            watcher.muted = True
            watcher._toast("t", "m")
            mock_toast.assert_not_called()
            watcher.muted = False
            watcher._toast("t", "m")
            mock_toast.assert_called_once_with("t", "m")

    @unittest.skipUnless(os.name == "nt", "the logon task is Windows-only")
    def test_install_and_uninstall_touch_only_this_copys_task(self):
        for flag in ("--install-startup", "--uninstall-startup"):
            with patch("subprocess.run") as run, patch("sys.argv", ["afi", flag]),                     patch("builtins.print"):
                run.return_value.returncode = 0
                afi.main()
            names = [c.args[0][c.args[0].index("/TN") + 1] for c in run.call_args_list]
            self.assertEqual(names, [afi.STARTUP_TASK], flag)

    def test_a_busy_port_stops_with_a_clear_message(self):
        before = {k: vars(afi.Handler)[k] for k in ("scanners", "scanner", "root", "watcher")
                  if k in vars(afi.Handler)}

        def restore():                      # main() sets these on the class; put them back
            for k in ("scanners", "scanner", "root", "watcher"):
                if k in before:
                    setattr(afi.Handler, k, before[k])
                elif k in vars(afi.Handler):
                    delattr(afi.Handler, k)
        self.addCleanup(restore)
        with tempfile.TemporaryDirectory() as root,                 patch.object(afi, "DashboardServer", side_effect=OSError(10048, "in use")),                 patch("sys.argv", ["afi", "--dir", root, "--no-copilot", "--no-browser"]),                 patch("builtins.print"):
            with self.assertRaises(SystemExit) as cm:
                afi.main()
        self.assertIn("already in use", str(cm.exception.code))
        self.assertIn("--port 8789", str(cm.exception.code))

if __name__ == "__main__":
    unittest.main()
