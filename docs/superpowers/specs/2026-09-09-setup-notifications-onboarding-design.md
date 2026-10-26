# Setup/onboarding path for accurate plan-usage notifications

**Status:** approved for implementation planning
**Author:** Claude (ai-fluency-insights session), with the project owner
**Date:** 2026-09-09

## Problem

The plan-usage panel (`docs/DASHBOARD_GUIDE.md#plan-usage-limits`) and the
Windows toast notifications (`NotificationWatcher`, added 2026-09-09) both
depend on `~/.claude/rate_limits_latest.json`, which only exists if the user
has manually:

1. Written a `statusLine` script that captures Anthropic's `rate_limits`
   field from the JSON Claude Code pipes to it, and
2. Added a `statusLine` block to `~/.claude/settings.json` pointing at it.

That's exactly what was done by hand on this machine today. It does not
scale to anyone this project ships to — a new user gets a silently
degraded experience (the local token-count *estimate* instead of real
Anthropic numbers, see `PlanWindowTracker`) with no indication that a
five-minute setup step would fix it.

This spec covers a `--setup-notifications` CLI flag that automates that
setup safely, plus a small nudge on normal startup, plus fixing a related
dead-code bug (`NOTIFY["enabled"]` is defined but never checked).

## Goals

- A new user can run one command and get real Anthropic rate-limit numbers
  in the plan-usage panel, without hand-editing `~/.claude/settings.json`.
- Never silently damage a user's existing `statusLine` setup — detect,
  don't clobber.
- `NOTIFY["enabled"]` actually gates the notification thread.
- Users who never run the wizard still find out it exists.

## Non-goals (deferred)

- A persisted user-preferences file for toast thresholds/on-off
  (Approach 3 from brainstorming — YAGNI for now; `NOTIFY` stays a
  source-level constant).
- macOS/Linux toast support (`osascript`, `notify-send`). Rate-limit
  *capture* is cross-platform already (it's just a JSON file); only the
  toast delivery mechanism (`send_windows_toast`) is Windows-only, and
  stays that way in this spec.
- A web UI banner/button for setup (explicitly declined in favor of a
  CLI flag).
- Any change to `PlanWindowTracker`'s estimate/`window_ceiling` — it
  remains the fallback for when the official path isn't available.

## New files owned by this feature

- `~/.claude/ai_fluency_insights_statusline.js` — the bundled statusline
  script this feature writes, **only** under this dashboard-namespaced
  filename. It never writes to `~/.claude/statusline.js` or any other
  path a user might have created by hand, so it can never silently
  overwrite a foreign script.
- `~/.claude/settings.json.bak-<unix_timestamp>` — a pre-write backup,
  created immediately before any modification to `settings.json`.

## Behavior

### Entry point

```
python ai_fluency_insights.py --setup-notifications [--dry-run]
```

Follows the existing early-return CLI-action pattern already used by
`--install-startup`/`--uninstall-startup` in `main()`: parsed, handled,
and returns before the HTTP server ever starts.

### Step 1 — platform check

If `os.name != "nt"`: print that toast notifications aren't available on
this OS yet, but continue to Step 2 anyway — rate-limit capture is a
plain JSON file and works the same everywhere Claude Code's `statusLine`
hook does.

### Step 2 — classify the current `statusLine` config

Read `~/.claude/settings.json` (the same path Claude Code itself uses;
respect `CLAUDE_CONFIG_DIR` the same way `default_root()` already does).
If the file doesn't exist, treat it as `{}` (a fresh install with no
settings yet is valid — Claude Code creates it lazily).

A pure function, `classify_statusline(settings: dict) -> str`, returns
one of:

- `"missing"` — no `statusLine` key, or it's not a dict with a `command`.
- `"ours"` — `statusLine.command` contains the substring
  `ai_fluency_insights_statusline.js`.
- `"foreign"` — a `statusLine.command` exists and doesn't contain that
  marker.

This function takes and returns plain dicts — no file I/O — so it's
directly unit-testable with fixture dicts, matching the existing style of
`milestones_crossed`/`steps_crossed`.

### Step 3 — act on the classification

**`missing`:**
- Print exactly what will be written: the new `ai_fluency_insights_statusline.js`
  content (a fixed, short bundled script — see below) and the new
  `statusLine` block:
  ```json
  {
    "type": "command",
    "command": "node \"<abs path>/ai_fluency_insights_statusline.js\"",
    "refreshInterval": 30
  }
  ```
- Check `shutil.which("node")` first. If node isn't found, stop here with
  a clear message — Claude Code itself requires Node, so this should be
  rare, but fail loudly rather than writing a `statusLine` that can never
  run.
- Ask `y/N` to proceed (skipped under `--dry-run`, which always stops
  after printing). If stdin isn't a TTY and `--dry-run` wasn't passed,
  abort with a message instead of hanging on `input()`.
- On yes: back up `settings.json` (skip the backup step if the file
  doesn't exist yet — nothing to back up), write the new
  `ai_fluency_insights_statusline.js`, merge the `statusLine` block into
  the settings dict (touching no other top-level key), and write
  `settings.json` atomically (temp file + `os.replace`, mirroring
  `save_notify_state`'s existing pattern).

**`ours`:**
- If `refreshInterval` is already `30`, report "already configured,
  nothing to do."
- Otherwise, patch just that one field (same backup + atomic-write path
  as above) and report what changed.

**`foreign`:**
- Never modify `settings.json` or the user's script.
- Best-effort read the script file the `command` points at (only if it's
  a plain local path we can resolve) and check whether it contains the
  substring `rate_limits` — if so, note "your existing script already
  looks like it might reference rate_limits."
- Check `load_rate_limits(RATE_LIMITS_PATH)` (already exists in the
  codebase) and report whether a fresh capture is already happening
  regardless.
- Print the minimal Node snippet (extracted verbatim from today's
  `~/.claude/statusline.js` write-side-effect block) for the user to
  splice into their own script by hand, plus a pointer to
  `docs/DASHBOARD_GUIDE.md#plan-usage-limits` for the full explanation.

### Step 4 — final report

Regardless of branch, end with a plain-text summary block:

```
Rate-limit capture:  <ok | not configured | needs your action, see above>
Toast notifications: <ON | OFF>   (edit NOTIFY["enabled"] in the script to change)
```

The "ON/OFF" line reads the live `NOTIFY["enabled"]` constant — this is
where the dead-flag fix (below) makes this line actually true.

### The bundled statusline script

A short, fixed string constant (`BUNDLED_STATUSLINE_JS`) near the
existing `NOTIFY`/`RATE_LIMITS_PATH` constants. Content: a trimmed
version of the write-side-effect block from today's hand-written
`~/.claude/statusline.js` (merge-onto-previous-snapshot logic included,
so one window missing on a given render doesn't blank the other), plus a
minimal one-line status output (`dir  model  ctx X%  5h X%  wk X%`) so
users who had no status line at all get a reasonable default, not a
blank line. First line is a marker comment,
`// ai-fluency-insights:managed-statusline`, which is what
`classify_statusline` keys off of via the filename rather than parsing
the comment (the filename check is sufficient and simpler; the comment
is for human readers who open the file).

### Startup nudge (Approach 2)

In `main()`, in the normal (non-flag) run path, after resolving `args.dir`
and before starting the HTTP server: if `os.name == "nt"` and
`not os.path.exists(RATE_LIMITS_PATH)` — i.e. it has *never* been
captured, as opposed to existing-but-stale, which is a normal idle state
covered by the UI's own staleness warning, not a setup problem — print
one line:

```
  Tip: plan-usage % is a local estimate. Run --setup-notifications for real Anthropic numbers.
```

Printed once at process startup only — no repeated nagging, no popup.

### Fixing `NOTIFY["enabled"]`

Today, `NOTIFY["enabled"]` is defined but never read anywhere in the
file — toast notifications cannot actually be turned off via config. Fix:
wrap the existing `threading.Thread(target=_notify_loop, daemon=True).start()`
call in `main()` with `if NOTIFY["enabled"]:`. This is the only change
needed — `send_windows_toast` is only ever called from inside
`NotificationWatcher.poll_once()`/`_poll_official_rate_limits`, which only
run inside `_notify_loop`, so gating the thread gates everything
downstream.

## Error handling

- Malformed existing `settings.json` (invalid JSON): report the parse
  error and abort without writing anything — never guess at a user's
  broken config.
- `settings.json` not writable (permissions): catch the `OSError` on
  write, report it, leave any backup already made in place (harmless),
  don't crash the wizard.
- `node` missing: abort before any file writes (see Step 3).
- Any exception during the `foreign` branch's best-effort file read is
  swallowed — that branch is diagnostic-only and must never block the
  final report.

## Testing

- `classify_statusline`: unit tests over fixture dicts — missing key,
  `ours` via marker substring, `foreign`, and the edge case of a
  `statusLine` value that isn't a dict (e.g. accidentally a string).
- The settings-merge function (`merge_statusline_config(settings, path) -> settings`):
  pure transform, unit-tested for "adds when missing," "patches
  `refreshInterval` only when `ours`," and "leaves untouched other
  top-level keys" (e.g. `enabledPlugins` in the real file must survive
  byte-for-byte apart from the one key touched).
- Backup/write path: `tempfile.TemporaryDirectory()`-based tests (same
  style as `NotifyStatePersistenceTests`), asserting the `.bak-*` file
  exists with the pre-write content and the new file parses as valid
  JSON with the expected `statusLine` block.
- The confirmation prompt: `unittest.mock.patch("builtins.input", ...)`
  to test both the "y" and "n" paths write/don't-write.
- `NOTIFY["enabled"] = False` correctly prevents `_notify_loop` from ever
  starting: a smaller unit test around whatever helper wraps the
  thread-start check (extract it as a one-line testable predicate rather
  than inlining `if NOTIFY["enabled"]:` where it can't be asserted on
  directly, if that ends up cleaner during implementation).
- Manual verification before touching the real machine: run
  `--setup-notifications --dry-run` first, then a real run against a
  **copied** `settings.json` in a scratch `CLAUDE_CONFIG_DIR`, before
  ever running it unflagged against the real `~/.claude/settings.json`
  on this machine.

## Open questions for implementation time

None blocking — brainstorming already resolved scope (rate-limits +
toasts, Windows-only, CLI flag, detect-don't-clobber). Any new question
that comes up during implementation should be resolved by checking this
spec's Goals/Non-goals section first.
