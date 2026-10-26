"""
GitHub Copilot as a log source for AI Fluency Insights.

Copilot-only helpers, kept apart from the dashboard engine: this module never imports
ai_fluency_insights.py (the main file imports it, not the other way round). Files are only
ever read, never written.

For now it holds discovery (find the VS Code user folders and the Copilot CLI folder, and
list every Copilot store inside them), Copilot's tool vocabulary (which tool names read,
write or run commands, and how many lines a write adds) and the translators for VS Code chat
sessions (replay the incremental log, then turn each request into the Claude-shaped events
that parse_entry() understands) and for the agent runtime's event logs (VS Code transcripts
and Copilot CLI sessions), plus the readers for the two exact token sources (the OpenTelemetry
file export and the agent debug logs), and what names a session: its project folder (from
workspace.json), the Claude-style project label, and the Copilot Chat session-store database
(read-only). The main file's CopilotScanner puts these together.

Requires: Python 3.8+ (standard library only).
"""

import json
import math
import os
import pathlib
import posixpath
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

VARIANTS = ["Code", "Code - Insiders", "Code - Exploration", "VSCodium"]
REMOTE_ROOTS = [".vscode-server", ".vscode-server-insiders", ".vscode-remote"]   # ~/<this>/data/User
# The Copilot Chat extension's storage folder has been spelled both ways.
EXT_FOLDERS = ["GitHub.copilot-chat", "github.copilot-chat"]
OTEL_OUTFILE_RE = re.compile(r'"github\.copilot\.chat\.otel\.outfile"\s*:\s*"([^"]*)"')

# Copilot's tool names, by what they do. The main file adds these to its own READ_TOOLS /
# WRITE_TOOLS / CMD_TOOLS / TOOL_FEATURES, so the usual counters work on Copilot's real names.
COPILOT_READ_TOOLS = {"read_file", "grep_search", "file_search", "semantic_search", "list_dir",
                      "fetch_webpage", "get_errors", "view", "grep", "glob", "web_fetch"}
COPILOT_WRITE_TOOLS = {"create_file", "replace_string_in_file", "multi_replace_string_in_file",
                       "apply_patch", "insert_edit_into_file", "edit_notebook_file", "edit",
                       "create", "str_replace_editor"}
COPILOT_CMD_TOOLS = {"run_in_terminal", "bash", "powershell", "shell", "write_bash"}
COPILOT_TOOL_FEATURES = {"run_in_terminal": "verify", "bash": "verify", "powershell": "verify",
                         "fetch_webpage": "web", "web_fetch": "web", "runSubagent": "subagents",
                         "task": "subagents", "manage_todo_list": "tasks", "update_todo": "tasks"}


def default_copilot_home():
    """Where the Copilot CLI keeps its sessions."""
    return os.path.join(os.path.expanduser("~"), ".copilot")


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _real(path):
    """Same file, same key: symlinks resolved, case folded where the OS ignores case."""
    return os.path.normcase(os.path.realpath(path))


def _add_unique(paths, path):
    key = _real(path)
    if all(_real(p) != key for p in paths):
        paths.append(path)


def _ext_dirs(parent):
    """The Copilot Chat folders under parent, once each (Windows and macOS ignore case)."""
    out, seen = [], set()
    for ext in EXT_FOLDERS:
        d = os.path.join(parent, ext)
        key = _real(d)
        if os.path.isdir(d) and key not in seen:
            seen.add(key)
            out.append(d)
    return out


def find_user_roots(extra=(), home=None, appdata=None):
    """Every VS Code user folder on this machine that exists, plus each named profile.

    `appdata` replaces the per-OS config folder (%APPDATA%, ~/Library/Application Support,
    $XDG_CONFIG_HOME or ~/.config); `home` replaces ~. `extra` adds folders by hand."""
    home = home or os.path.expanduser("~")
    if appdata:
        base = appdata
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    paths = [os.path.join(base, v, "User") for v in VARIANTS]
    paths += [os.path.join(home, r, "data", "User") for r in REMOTE_ROOTS]
    paths += [os.path.expanduser(p) for p in extra]
    out, seen = [], set()
    for path in paths:
        key = _real(path)
        if not os.path.isdir(path) or key in seen:
            continue
        seen.add(key)
        out.append(path)
        # A named profile keeps its own globalStorage (and sometimes its own chats).
        for prof in _listdir(os.path.join(path, "profiles")):
            pp = os.path.join(path, "profiles", prof)
            if os.path.isdir(pp) and _real(pp) not in seen:
                seen.add(_real(pp))
                out.append(pp)
    return out


def _jsonl_files(folder):
    return [(fn[:-len(".jsonl")], os.path.join(folder, fn))
            for fn in _listdir(folder) if fn.endswith(".jsonl")]


def _chat_files(folder):
    """chatSessions-style folder: (session id, path) for *.json and *.jsonl. A .jsonl sorts
    after a .json twin of the same session, so a caller that overwrites keeps the newer format."""
    return [(os.path.splitext(fn)[0], os.path.join(folder, fn))
            for fn in _listdir(folder) if fn.endswith((".json", ".jsonl"))]


def _outfile_setting(settings_path):
    """The OTel file path set in settings.json, or None. The file is JSONC (comments,
    trailing commas), so the setting is pulled out with a regex rather than parsed."""
    try:
        with open(settings_path, "r", encoding="utf-8", errors="replace") as fh:
            m = OTEL_OUTFILE_RE.search(fh.read())
    except OSError:
        return None
    if not m or not m.group(1):
        return None
    try:
        raw = json.loads('"%s"' % m.group(1))       # undo JSON escapes (C:\\Users\\...)
    except ValueError:
        raw = m.group(1)
    return os.path.expanduser(raw)


def _scan_user_root(root, found, outfiles):
    ws_root = os.path.join(root, "workspaceStorage")
    for h in _listdir(ws_root):
        ws = os.path.join(ws_root, h)
        if not os.path.isdir(ws):
            continue
        for sid, path in _chat_files(os.path.join(ws, "chatSessions")):
            found["chat"][sid] = {"path": path, "workspace": ws}
        for ed in _ext_dirs(ws):
            for sid, path in _jsonl_files(os.path.join(ed, "transcripts")):
                found["transcripts"].setdefault(sid, {"path": path, "workspace": ws})
            dl = os.path.join(ed, "debug-logs")
            for name in _listdir(dl):
                p = os.path.join(dl, name)
                if os.path.isdir(p):
                    files = [path for _sid, path in _jsonl_files(p)]
                    if files:
                        found["debug"].setdefault(name, []).extend(files)
                elif name.endswith(".jsonl"):
                    found["debug"].setdefault(name[:-len(".jsonl")], []).append(p)

    gs = os.path.join(root, "globalStorage")
    for sid, path in _chat_files(os.path.join(gs, "emptyWindowChatSessions")):
        found["chat"][sid] = {"path": path, "workspace": ""}
    for ed in _ext_dirs(gs):
        db = os.path.join(ed, "session-store.db")
        if os.path.isfile(db):
            _add_unique(found["stores"], db)

    settings = os.path.join(root, "settings.json")
    if os.path.isfile(settings):
        _add_unique(found["settings"], settings)
        outfiles.append(_outfile_setting(settings))


def _scan_copilot_home(copilot_home, found):
    ss = os.path.join(copilot_home, "session-state")
    for name in _listdir(ss):
        p = os.path.join(ss, name)
        if os.path.isdir(p):
            ev = os.path.join(p, "events.jsonl")
            if os.path.isfile(ev):
                found["cli"][name] = ev
        elif name.endswith(".jsonl"):
            found["cli"].setdefault(name[:-len(".jsonl")], p)


def discover(user_roots, copilot_home):
    """Every Copilot store under the given VS Code user folders and the Copilot CLI folder.

    Returns {"chat": {sid: {"path", "workspace"}}, "transcripts": {sid: {"path", "workspace"}},
    "debug": {sid: [paths]}, "otel": [paths], "cli": {sid: path}, "stores": [db paths],
    "settings": [settings.json paths]}. `workspace` is the workspaceStorage/<hash> folder,
    or "" for a chat with no folder open. A session id is the file's name without its
    extension (for the CLI, the folder's name)."""
    found = {"chat": {}, "transcripts": {}, "debug": {}, "otel": [], "cli": {},
             "stores": [], "settings": []}
    outfiles = []
    for root in user_roots:
        _scan_user_root(root, found, outfiles)
    if copilot_home:
        _scan_copilot_home(copilot_home, found)
    outfiles.append(os.path.expanduser(os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH", "")) or None)
    if copilot_home:
        outfiles += [p for _sid, p in _jsonl_files(os.path.join(copilot_home, "otel"))]
    for p in outfiles:
        if p and os.path.isfile(p):
            _add_unique(found["otel"], p)
    return found


# ---------------------------------------------------------------------------
# Lines written by a tool call
# ---------------------------------------------------------------------------

# tool -> the argument that holds the new text (every line of it counts as added)
_NEW_TEXT_ARG = {"create_file": "content", "create": "file_text",
                 "replace_string_in_file": "newString", "edit": "new_str",
                 "str_replace_editor": "new_str", "edit_notebook_file": "newCode"}
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add) File:\s*(.+?)\s*$", re.MULTILINE)


def _text_lines(text):
    """Lines in a string, counted like the dashboard does: newlines + 1, and 0 for empty or non-text."""
    return text.count("\n") + 1 if isinstance(text, str) and text else 0


def _arg_path(args):
    path = args.get("filePath") or args.get("path")
    return path if isinstance(path, str) else ""


def tool_loc(name, args):
    """(added_lines, file_path) for one Copilot tool call; (0, path) for tools that write nothing.
    `args` is the tool's argument dict (apply_patch may also pass the raw patch text); the path is
    "" when the call names no file."""
    patch = args if isinstance(args, str) else None      # apply_patch may pass the raw patch text
    if not isinstance(args, dict):
        args = {}
    if name == "apply_patch" and patch is None:
        patch = args.get("input")
    path = _arg_path(args)
    if name in _NEW_TEXT_ARG:
        return _text_lines(args.get(_NEW_TEXT_ARG[name])), path
    if name == "multi_replace_string_in_file":
        reps = args.get("replacements")
        reps = [r for r in reps if isinstance(r, dict)] if isinstance(reps, list) else []
        path = path or next((_arg_path(r) for r in reps if _arg_path(r)), "")
        return sum(_text_lines(r.get("newString")) for r in reps), path
    if name == "apply_patch" and isinstance(patch, str):
        m = _PATCH_FILE_RE.search(patch)
        added = sum(1 for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        return added, (m.group(1) if m else path)
    if name == "insert_edit_into_file":
        code = args.get("code")
        if isinstance(code, str) and code:
            return sum(1 for ln in code.split("\n") if "...existing code..." not in ln), path
    return 0, path


# ---------------------------------------------------------------------------
# VS Code chat sessions -> Claude-shaped events
# ---------------------------------------------------------------------------
#
# A chatSessions file is either the whole session as one JSON document (older builds) or an
# incremental log, one mutation per line: {"kind": 0, "v": snapshot}, {"kind": 1, "k": path,
# "v": value} (set), {"kind": 2, "k": path, "v": [..], "i": n} (push, optionally cutting the list
# back to n items first) and {"kind": 3, "k": path} (delete). A path is a list of names and indexes.
# The log may be mid-write when it is read, so an unparseable line is skipped and counted.

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FRACTION_RE = re.compile(r"\.(\d+)")
_OFFSET_RE = re.compile(r"([+-]\d{2})(\d{2})$")
_PAD_LIMIT = 100000          # a corrupt index must not grow a list without bound


def _to_dt(value):
    """An epoch-millisecond number or an ISO 8601 string as an aware UTC datetime, else None.
    A string without a zone is read as UTC."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return _EPOCH + timedelta(milliseconds=value)
        if isinstance(value, str):
            text = value.strip()
            if not text or text.isdigit():
                return None
            if text[-1] in "Zz":
                text = text[:-1] + "+00:00"
            # fromisoformat() before 3.11 wants 3 or 6 fraction digits and a colon in the offset
            text = _FRACTION_RE.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
            text = _OFFSET_RE.sub(r"\1:\2", text)
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _fmt(dt):
    return dt.isoformat(timespec="milliseconds")


def iso_utc(value):
    """UTC ISO string with milliseconds and a +00:00 offset (2026-10-01T12:00:00.000+00:00) for an
    epoch-millisecond number or an ISO string (Z or an offset); None when it is not a time."""
    dt = _to_dt(value)
    return _fmt(dt) if dt is not None else None


def _is_index(key):
    return isinstance(key, int) and not isinstance(key, bool) and key >= 0


def _put(node, key, value):
    """node[key] = value for an object (name) or a list (index, padding with None as needed)."""
    if isinstance(node, dict):
        if not isinstance(key, str):
            raise ValueError("an object needs a name")
        node[key] = value
    elif isinstance(node, list):
        if not _is_index(key) or key - len(node) > _PAD_LIMIT:
            raise ValueError("bad list index")
        if key >= len(node):
            node.extend([None] * (key - len(node) + 1))
        node[key] = value
    else:
        raise ValueError("not a container")


def _child(node, key, nxt, create):
    """node[key], or None when it is not there. With `create`, a missing (or null) child becomes a
    new list when the next path step is an index, else a new object."""
    if isinstance(node, dict):
        if not isinstance(key, str):
            raise ValueError("an object needs a name")
        child = node.get(key)
    elif isinstance(node, list):
        if not _is_index(key):
            raise ValueError("a list needs an index")
        child = node[key] if key < len(node) else None
    else:
        raise ValueError("not a container")
    if child is None and create:
        child = [] if _is_index(nxt) else {}
        _put(node, key, child)
    return child


def _parent(root, path, create):
    """The container that holds path[-1] (None when `create` is off and the way there is missing)."""
    node = root
    for pos in range(len(path) - 1):
        node = _child(node, path[pos], path[pos + 1], create)
        if node is None:
            return None
    return node


def _apply_chat_op(sess, op):
    """Apply one mutation and return the session; raises ValueError when it cannot be applied."""
    kind = op.get("kind")
    if isinstance(kind, bool) or kind not in (0, 1, 2, 3):
        raise ValueError("unknown mutation kind")
    if kind == 0:
        if not isinstance(op.get("v"), dict):
            raise ValueError("a snapshot is an object")
        return op["v"]
    if sess is None:
        raise ValueError("no snapshot yet")
    path = op.get("k")
    if not isinstance(path, list) or not path:
        raise ValueError("no path")
    key = path[-1]
    if kind == 1:
        if "v" not in op:
            raise ValueError("no value")
        _put(_parent(sess, path, True), key, op["v"])
    elif kind == 2:
        if "v" not in op or ("i" in op and not _is_index(op["i"])):
            raise ValueError("bad push")
        target = _child(_parent(sess, path, True), key, 0, True)
        if not isinstance(target, list):
            raise ValueError("not a list")
        if "i" in op:
            del target[op["i"]:]
        if isinstance(op["v"], list):
            target.extend(op["v"])
        else:
            target.append(op["v"])
    else:
        parent = _parent(sess, path, False)
        if isinstance(parent, dict) and isinstance(key, str):
            parent.pop(key, None)
        elif isinstance(parent, list) and _is_index(key):
            if key < len(parent):
                del parent[key]
        elif parent is not None:
            raise ValueError("bad delete")
    return sess


def replay_chat_jsonl(lines):
    """Rebuild a chat session from its mutation log. Returns (session, bad_lines).

    `lines` is any iterable of text lines. A line that is not valid JSON (a half-written last
    line, say), not an object, or an operation that cannot be applied is skipped and counted in
    bad_lines; blank lines are ignored. The session is None when no snapshot (kind 0) was seen."""
    sess, bad = None, 0
    for line in lines:
        try:
            text = line.strip()
            if not text:
                continue
            op = json.loads(text)
            if not isinstance(op, dict):
                raise ValueError("not an object")
            sess = _apply_chat_op(sess, op)
        except (ValueError, TypeError, AttributeError, LookupError, RecursionError):
            bad += 1
    return sess, bad


def load_chat_session(path):
    """Read a chat-session file: `.jsonl` is replayed, anything else is one JSON document.
    Returns (session, bad_lines); a file that cannot be read or is not a session object gives
    (None, 1). Never raises."""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            if str(path).lower().endswith(".jsonl"):
                return replay_chat_jsonl(fh)
            data = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return None, 1
    return (data, 0) if isinstance(data, dict) else (None, 1)


_MODEL_DATE_RE = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")        # gpt-5.5-2026-04-23, ...-20251001
_CLAUDE_MINOR_RE = re.compile(r"^(claude-(?:.*-)?\d+)-(\d{1,2})$")      # claude-haiku-4-5 -> claude-haiku-4.5


def normalize_model(raw):
    """A model id as the dashboard names it, so one model is one row whatever the log spells: lower case,
    without the `copilot/` prefix and without a trailing date (-YYYYMMDD or -YYYY-MM-DD); a claude- id that
    ends in two numbers gets a dot between them (claude-haiku-4-5 -> claude-haiku-4.5, but claude-3-5-sonnet
    stays). None for "auto" (the model is picked per call, so the id says nothing) or when there is no id."""
    if not isinstance(raw, str):
        return None
    model = raw.strip().lower()
    if model.startswith("copilot/"):
        model = model[len("copilot/"):].strip()
    if model in ("", "auto"):
        return None
    model = _MODEL_DATE_RE.sub("", model)
    m = _CLAUDE_MINOR_RE.match(model)
    if m:
        model = m.group(1) + "." + m.group(2)
    return model or None


_CREDITS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*credits?", re.IGNORECASE)
_PREMIUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[x×]", re.IGNORECASE)


def parse_credits(details):
    """What a request's `result.details` ("GPT-5.6 Luna • 0.5 credits") says it cost:
    ("credits", 0.5) for credits, ("premium", 3.0) for a premium-request multiplier ("... • 3x"),
    None for anything else. Credits win when both appear."""
    if not isinstance(details, str):
        return None
    parts = [part.strip() for part in details.split("•")]
    for unit, pattern in (("credits", _CREDITS_RE), ("premium", _PREMIUM_RE)):
        for part in parts:
            m = pattern.fullmatch(part)
            if m and math.isfinite(float(m.group(1))):
                return unit, float(m.group(1))
    return None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _num(value):
    """value when it is a finite number (a bool is not one), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:            # an integer too big for a float
        return None


def _positive(value):
    n = _num(value)
    return n if n is not None and n > 0 else None


def _tokens(*candidates):
    """The first candidate that is a positive number, as an int; 0 when none is."""
    for value in candidates:
        if _positive(value) is not None:
            return int(value)
    return 0


def chat_session_meta(sess):
    """{"mode": agent/ask/edit, "title": custom title, "creation": ISO time}; None for what is missing."""
    sess = _dict(sess)
    mode = _dict(_dict(sess.get("inputState")).get("mode")).get("kind")
    title = sess.get("customTitle")
    return {"mode": mode if isinstance(mode, str) and mode else None,
            "title": title if isinstance(title, str) and title else None,
            "creation": iso_utc(sess.get("creationDate"))}


def _tool_args(raw):
    """(the call's input dict, what tool_loc should read) from a tool call's `arguments`, which is
    normally a JSON string. Text that is not a JSON object (apply_patch sometimes gets the bare patch)
    gives an empty input and is handed to tool_loc as it is."""
    if isinstance(raw, dict):
        return dict(raw), raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed, parsed
        return {}, raw
    return {}, {}


def _leaf_texts(value, depth=0):
    """Every string in a tool result: a bare string, or VS Code's {content: [{value: "..."}]}."""
    if isinstance(value, str):
        yield value
    elif depth < 5 and isinstance(value, list):
        for item in value:
            for text in _leaf_texts(item, depth + 1):
                yield text
    elif depth < 5 and isinstance(value, dict):
        for key in ("content", "value", "text"):
            if key in value:
                for text in _leaf_texts(value[key], depth + 1):
                    yield text


def _result_failed(value):
    """A tool result counts as an error when its text starts with ERROR or says a patch failed."""
    texts = [t for t in _leaf_texts(value) if t]
    return bool(texts) and (texts[0].lstrip().startswith("ERROR")
                            or any("Applying patch failed" in t for t in texts))


# VS Code's ResponseModelState (chatService.ts): 0 Pending, 1 Complete, 2 Cancelled, 3 Failed, 4 NeedsInput.
# Only Cancelled is a stopped request; Failed has no Claude-side metric and NeedsInput is not final.
MODEL_STATE_CANCELLED = 2
INTERRUPT_TEXT = "[Request interrupted by user]"


def chat_state_counts(sess):
    """(chat requests with a usable timestamp, how many of them record a modelState with a numeric value)
    in a chat session. This tells whether a VS Code version logs stopped requests at all."""
    with_time = with_state = 0
    for req in _list(_dict(sess).get("requests")):
        if isinstance(req, dict) and _to_dt(req.get("timestamp")) is not None:
            with_time += 1
            if _num(_dict(req.get("modelState")).get("value")) is not None:
                with_state += 1
    return with_time, with_state


def _translate_request(out, req, index, sid, cwd, branch, with_usage):
    start = _to_dt(req.get("timestamp"))
    if start is None:
        return
    rid = req.get("requestId")
    if not isinstance(rid, str) or not rid:
        rid = "req%d" % index
    result = _dict(req.get("result"))
    meta = _dict(result.get("metadata"))
    timings = _dict(result.get("timings"))
    elapsed = _positive(req.get("elapsedMs")) or _positive(timings.get("totalElapsed")) or 0
    try:
        end = start + timedelta(milliseconds=elapsed)
    except (OverflowError, ValueError):
        end = start
    state = _dict(req.get("modelState"))
    done = _to_dt(state.get("completedAt"))
    if done is not None and done >= start:        # newer VS Code says when the request finished
        end = done

    text = req.get("message")
    text = text.get("text") if isinstance(text, dict) else text
    out.append({"type": "user", "timestamp": _fmt(start), "sessionId": sid, "cwd": cwd,
                "gitBranch": branch, "message": {"content": text if isinstance(text, str) else ""}})

    rounds = [r for r in _list(meta.get("toolCallRounds")) if isinstance(r, dict)]
    stamps = [_to_dt(r.get("timestamp")) or start for r in rounds]
    if stamps and stamps[-1] > end:        # a missing or short elapsed time must not end the request early
        end = stamps[-1]
    call_results = _dict(meta.get("toolCallResults"))
    last_model = None
    for r, rnd in enumerate(rounds):
        model = normalize_model(rnd.get("modelId"))
        last_model = model or last_model
        back = _fmt(stamps[r + 1] if r + 1 < len(rounds) else end)     # when the results come back
        blocks, results = [], []
        for j, call in enumerate(c for c in _list(rnd.get("toolCalls")) if isinstance(c, dict)):
            tid = call.get("id")
            if not isinstance(tid, str) or not tid:
                tid = "%s:%s:r%d:c%d" % (sid, rid, r, j)
            name = call.get("name")
            name = name if isinstance(name, str) and name else "?"
            args, loc_args = _tool_args(call.get("arguments"))
            args["_loc"], args["file_path"] = tool_loc(name, loc_args)
            failed = _result_failed(call_results.get(tid))
            if failed:
                args["_loc"] = 0               # a write whose result failed added no lines (design 5.1)
            blocks.append({"type": "tool_use", "id": tid, "name": name, "input": args})
            results.append({"type": "user", "timestamp": back, "sessionId": sid, "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "is_error": failed, "content": ""}]}})
        out.append({"type": "assistant", "timestamp": _fmt(stamps[r]), "sessionId": sid, "requestId": rid,
                    "cwd": cwd, "gitBranch": branch,
                    "message": {"id": "%s:%s:r%d" % (sid, rid, r), "model": model or "unknown", "usage": {},
                                "stop_reason": None, "content": blocks}})
        out.extend(results)

    usage = {}
    if with_usage:
        used = _dict(result.get("usage"))
        usage = {"input_tokens": _tokens(req.get("promptTokens"), meta.get("promptTokens"),
                                         used.get("promptTokens")),
                 "output_tokens": _tokens(req.get("completionTokens"), meta.get("outputTokens"),
                                          used.get("completionTokens")),
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        if not usage["input_tokens"] and not usage["output_tokens"]:
            usage = {}                     # no tokens recorded: not a call with 0 tokens
    cancelled = _num(state.get("value")) == MODEL_STATE_CANCELLED       # a bool is not a number: True is not 1
    final = {"type": "assistant", "timestamp": _fmt(end), "sessionId": sid, "requestId": rid,
             "cwd": cwd, "gitBranch": branch,
             "message": {"id": "%s:%s:final" % (sid, rid),
                         "model": (normalize_model(req.get("modelId")) or last_model
                                   or normalize_model(meta.get("resolvedModel")) or "unknown"),
                         # a stopped request never finished: no end_turn, so the interruption below still finds its turn
                         "usage": usage, "stop_reason": None if cancelled else "end_turn", "content": []}}
    # Credits and the premium multiplier are kept with or without tokens: the caller decides which store
    # supplies the tokens, and the credits must not vanish with them.
    spent = parse_credits(result.get("details"))
    credits = _num(req.get("copilotCredits"))
    if credits is None or credits < 0:
        credits = spent[1] if spent and spent[0] == "credits" else None
    if credits is not None:
        final["_cost_override"] = float(credits)
    elif usage:
        # Tokens but no credits: parse_entry would price them from the Claude table and report dollars as
        # credits. Pin the cost to 0 and mark it, so a scanner can count the gap for Data Health.
        final["_cost_override"] = 0.0
        final["_no_credits"] = True
    if spent and spent[0] == "premium":
        final["_premium"] = spent[1]
    out.append(final)
    if cancelled:
        # parse_entry counts this text as an interruption (not a prompt) and marks the turn interrupted
        out.append({"type": "user", "timestamp": final["timestamp"], "sessionId": sid, "cwd": cwd,
                    "gitBranch": branch, "message": {"content": INTERRUPT_TEXT}})


def chat_session_to_events(sess, sid, project, cwd, branch, with_usage=True):
    """Turn a chat session (the dict from load_chat_session) into Claude-shaped events, in order:
    for each request a user event, then one assistant event per tool-call round followed by a
    tool_result event per call, then a final assistant event that carries the request's tokens and
    credits. A request without a usable timestamp is skipped.

    With with_usage=False the final event's usage is {} (the tokens come from a better store) but
    _cost_override and _premium stay. usage is also {} when the request recorded no tokens at all.
    An event that has usage but no known credits carries _cost_override 0.0 and _no_credits True, so
    tokens are never priced in dollars; with usage {} and no credits it carries neither. The final event
    is never earlier than the last round, whatever the elapsed time says. A request's end is
    modelState.completedAt when that is a valid time not before the start, else start + elapsed.
    A request with modelState.value 2 (Cancelled) has no end_turn on its final event and is followed
    by a user event "[Request interrupted by user]" with the same timestamp. `project` is accepted for
    symmetry with the other translators; events carry no project (parse_entry gets it separately).
    Never raises on a malformed session."""
    out = []
    for index, req in enumerate(_list(_dict(sess).get("requests"))):
        if isinstance(req, dict):
            _translate_request(out, req, index, sid, cwd, branch, with_usage)
    return out


# ---------------------------------------------------------------------------
# Agent logs (VS Code transcripts, Copilot CLI events.jsonl) -> Claude-shaped events
# ---------------------------------------------------------------------------
#
# Both are logs of {type, data, id, timestamp} records in one vocabulary: session.start,
# user.message, assistant.message (with toolRequests), tool.execution_start / _complete and
# assistant.turn_start / turn_end. A VS Code transcript has no token counts. A CLI log adds
# session.usage_record (one per LLM call, input counted including cached tokens) and the
# cumulative session.usage_checkpoint / session.shutdown snapshots.

_AGENT_IGNORED = frozenset(("system.message", "assistant.turn_start", "tool.execution_start"))
# session.* once the ones we use are taken; permission.*, skill.*, subagent.* and model.* are the routine
# records of the CLI and transcript logs (permission prompts, skill and sub-agent use, per-call model events)
_AGENT_IGNORED_PREFIXES = ("hook.", "external_tool.", "session.", "permission.", "skill.", "subagent.", "model.")
_AGENT_SNAPSHOTS = frozenset(("session.usage_checkpoint", "session.shutdown"))


def _str(value):
    """value when it is a non-blank string, else None."""
    return value if isinstance(value, str) and value.strip() else None


def _nano_credits(value):
    """Credits for a nanoAIU figure (1e9 nanoAIU = 1 credit); None when it is not a usable number."""
    nano = _num(value)
    return nano / 1e9 if nano is not None and nano >= 0 else None


def _cli_usage(input_tokens, output_tokens, cache_read, cache_write):
    """Claude-convention usage from a CLI log's counts, where input includes the cached tokens.
    {} when the call recorded neither input nor output tokens: that is not a call with 0 tokens."""
    inp, out = _tokens(input_tokens), _tokens(output_tokens)
    read, write = _tokens(cache_read), _tokens(cache_write)
    if not inp and not out:
        return {}
    return {"input_tokens": max(0, inp - read - write), "output_tokens": out,
            "cache_read_input_tokens": read, "cache_creation_input_tokens": write}


def _set_credits(event, usage, credits):
    """Credits when the log has them. Tokens without credits are pinned to 0.0 and marked, so
    parse_entry never prices Copilot tokens from the Claude table; no tokens and no credits: no keys."""
    if credits is not None:
        event["_cost_override"] = credits
    elif usage:
        event["_cost_override"] = 0.0
        event["_no_credits"] = True


class _AgentLog:
    """One agent_log_to_events call: feed() it the lines, then finish() for (events, meta)."""

    def __init__(self, sid, cwd, branch, with_usage):
        self.sid, self.cwd, self.branch, self.with_usage = sid, cwd, branch, with_usage
        self.meta = {"host_type": None, "cwd": None, "branch": None, "repository": None,
                     "copilot_version": None, "vscode_version": None,
                     "has_usage": False, "bad_lines": 0, "unknown_types": Counter()}
        self.out = []              # events in log order; None where a turn end was replaced by a later one
        self.pending_end = None    # index in out of the current prompt's last turn end
        self.ends = 0              # turn ends seen, for their ids
        self.last_ts = None        # the log's clock: a record without a time of its own gets this one
        self.last_model = None
        self.usage_records = 0
        self.snapshot = None       # (time, modelMetrics) of the last checkpoint / shutdown that has any
        self.tool_inputs = {}      # toolCallId -> its tool_use input, so a failed result can zero its lines

    # -- one line ------------------------------------------------------------------------------
    def feed(self, n, line):
        if isinstance(line, (str, bytes)) and not line.strip():
            return
        try:
            rec = json.loads(line)
        except (ValueError, TypeError, RecursionError):      # a half-written last line, or noise
            self.meta["bad_lines"] += 1
            return
        rtype = rec.get("type") if isinstance(rec, dict) else None
        if not isinstance(rtype, str) or not rtype:
            self.meta["bad_lines"] += 1
            return
        data = _dict(rec.get("data"))
        ts = iso_utc(rec.get("timestamp"))
        if ts:
            self.last_ts = ts
        else:
            ts = self.last_ts

        if rtype == "session.start":
            self.start(data)
        elif rtype in _AGENT_IGNORED:
            return
        elif ts is None:                  # nothing to place it in time by
            return
        elif rtype == "user.message":
            self.user(ts, data)
        elif rtype == "assistant.message":
            self.assistant(ts, data, rec, n)
        elif rtype == "tool.execution_complete":
            self.tool_result(ts, data)
        elif rtype == "session.usage_record":
            self.usage_record(ts, data, rec, n)
        elif rtype in _AGENT_SNAPSHOTS:
            snap = _dict(data.get("accountingSnapshot"))
            metrics = _dict(snap.get("modelMetrics")) or _dict(data.get("modelMetrics"))
            if metrics:
                self.snapshot = (ts, metrics)
        elif rtype == "assistant.turn_end":
            self.turn_end(ts, data)
        elif not rtype.startswith(_AGENT_IGNORED_PREFIXES):
            self.meta["unknown_types"][rtype] += 1

    # -- records -------------------------------------------------------------------------------
    def start(self, data):
        ctx = _dict(data.get("context"))
        found = {"host_type": _str(ctx.get("hostType")),
                 "cwd": _str(ctx.get("cwd")) or _str(ctx.get("gitRoot")),
                 "branch": _str(ctx.get("branch")),
                 "repository": _str(ctx.get("repository")),
                 "copilot_version": _str(data.get("copilotVersion")),
                 "vscode_version": _str(data.get("vscodeVersion"))}
        for key, value in found.items():
            if self.meta[key] is None:
                self.meta[key] = value

    def user(self, ts, data):
        if not data.get("agentId"):            # a subagent's task prompt does not start a new main turn
            self.pending_end = None
        content = data.get("content")
        self.add({"type": "user", "timestamp": ts, "sessionId": self.sid,
                  "cwd": self.cwd or self.meta["cwd"] or "", "gitBranch": self.branch or self.meta["branch"] or "",
                  "message": {"content": content if isinstance(content, str) else ""}}, data)

    def assistant(self, ts, data, rec, n):
        mid = _str(rec.get("id")) or "%s:msg:%d" % (self.sid, n)
        model = normalize_model(data.get("model"))
        self.last_model = model or self.last_model
        blocks = []
        for j, call in enumerate(c for c in _list(data.get("toolRequests")) if isinstance(c, dict)):
            name = _str(call.get("name")) or "?"
            args, loc_args = _tool_args(call.get("arguments"))
            if isinstance(loc_args, str) and name == "apply_patch":
                args = {"input": loc_args}                      # the bare patch text
            args["_loc"], args["file_path"] = tool_loc(name, loc_args)
            tid = _str(call.get("toolCallId"))
            if tid:
                self.tool_inputs[tid] = args
            blocks.append({"type": "tool_use", "id": tid or "%s:t%d" % (mid, j), "name": name, "input": args})
        self.add(self.llm_event(ts, mid, model, {}, content=blocks), data)

    def tool_result(self, ts, data):
        tid = _str(data.get("toolCallId"))
        if data.get("success") is False and tid in self.tool_inputs:
            self.tool_inputs[tid]["_loc"] = 0  # the earlier tool_use: a failed write added no lines (design 5.1)
        self.add({"type": "user", "timestamp": ts, "sessionId": self.sid, "message": {"content": [
            {"type": "tool_result", "tool_use_id": _str(data.get("toolCallId")) or "",
             "is_error": data.get("success") is False, "content": ""}]}}, data)

    def usage_record(self, ts, data, rec, n):
        used = data.get("usage")
        if not isinstance(used, dict):
            return
        self.usage_records += 1
        # One id per API call, so a record logged twice (or copied into a forked session) is counted once.
        mid = _str(used.get("apiCallId")) or _str(rec.get("id")) or "%s:usage:%d" % (self.sid, n)
        model = normalize_model(used.get("model"))
        self.last_model = model or self.last_model
        usage = _cli_usage(used.get("inputTokens"), used.get("outputTokens"),
                           used.get("cacheReadTokens"), used.get("cacheWriteTokens"))
        event = self.llm_event(ts, mid, model, usage)
        _set_credits(event, usage, _nano_credits(_dict(used.get("copilotUsage")).get("totalNanoAiu")))
        self.add_usage(event, usage, data)

    def turn_end(self, ts, data):
        if data.get("agentId"):               # a subagent finishing says nothing about the main turn
            return
        if self.pending_end is not None:
            self.out[self.pending_end] = None
        eid = "%s:end:%d" % (self.sid, self.ends)
        self.ends += 1
        self.out.append(self.llm_event(ts, eid, self.last_model, {}, stop_reason="end_turn"))
        self.pending_end = len(self.out) - 1

    # -- output --------------------------------------------------------------------------------
    def llm_event(self, ts, mid, model, usage, content=None, stop_reason=None):
        return {"type": "assistant", "timestamp": ts, "sessionId": self.sid, "requestId": mid,
                "cwd": self.cwd or self.meta["cwd"] or "", "gitBranch": self.branch or self.meta["branch"] or "",
                "message": {"id": mid, "model": model or "unknown", "usage": usage,
                            "stop_reason": stop_reason, "content": content or []}}

    def add(self, event, data):
        if data.get("agentId"):
            event["isSidechain"] = True
        self.out.append(event)

    def add_usage(self, event, usage, data):
        if usage:
            self.meta["has_usage"] = True      # what the file holds, whether or not the caller takes it
        if self.with_usage:
            self.add(event, data)

    def finish(self):
        if self.snapshot is not None and not self.usage_records:
            # Cumulative totals: only the last snapshot, and only when there are no per-call records.
            ts, metrics = self.snapshot
            for key, mm in metrics.items():
                if not isinstance(mm, dict):
                    continue
                used = _dict(mm.get("usage"))
                usage = _cli_usage(used.get("inputTokens"), used.get("outputTokens"),
                                   used.get("cacheReadTokens"), used.get("cacheWriteTokens"))
                credits = _nano_credits(mm.get("totalNanoAiu"))
                if not usage and credits is None:
                    continue
                event = self.llm_event(ts, "%s:snapshot:%s" % (self.sid, key), normalize_model(key), usage)
                _set_credits(event, usage, credits)
                self.add_usage(event, usage, {})
        return [e for e in self.out if e is not None], self.meta


def agent_log_to_events(lines, sid, project, cwd, branch, with_usage=True):
    """Turn an agent runtime's event log (a VS Code transcript or a Copilot CLI events.jsonl, as an
    iterable of lines) into Claude-shaped events for parse_entry(). Returns (events, meta);
    meta is {host_type, cwd, branch, repository, copilot_version, vscode_version (from
    session.start, None when absent), has_usage, bad_lines, unknown_types}.

    - user.message -> a user event; assistant.message -> an assistant event with usage {} and one
      tool_use per tool request (its input gets `_loc` and `file_path` from tool_loc);
      tool.execution_complete -> a tool_result, an error when success is False; the tool_use it answers
      then gets `_loc` 0, since a failed write added no lines.
    - session.usage_record -> an assistant event with exact tokens, input counted the Claude way
      (without cache reads and writes), its id the apiCallId so a repeat is counted once, and the
      credits as _cost_override. Tokens without credits get _cost_override 0.0 and _no_credits True
      (never a price from the Claude table). A record without input and output tokens has usage {}.
    - With no usage records at all, the last usage_checkpoint / shutdown that has model metrics
      stands in: one event per model, same rules. with_usage=False leaves out all of these (the
      tokens come from a better store); meta["has_usage"] still says whether the file had any.
    - assistant.turn_end -> an end_turn event, only the last one before the next prompt, and only
      the main agent's (a subagent's turn end closes nothing).
    - Records with data.agentId set give isSidechain events.
    - hook.*, system.message, other session.*, external_tool.*, permission.*, skill.*, subagent.*,
      model.*, assistant.turn_start and tool.execution_start are ignored; any other type is counted in meta["unknown_types"].
    - A line that is not JSON (a half-written last line), or not a record with a type, is counted
      in bad_lines and skipped; a blank line is skipped. A record with no usable time takes the
      previous record's, and is dropped when there is none.

    An event's cwd and gitBranch are the arguments, else what session.start said. `project` is
    accepted for symmetry with the other translators; events carry no project. Never raises on a
    malformed log."""
    log = _AgentLog(sid, cwd, branch, with_usage)
    for n, line in enumerate(lines):
        log.feed(n, line)
    return log.finish()


# ---------------------------------------------------------------------------
# Exact token sources: the OpenTelemetry file export and the agent debug logs
# ---------------------------------------------------------------------------
#
# Two places hold the exact counts of every LLM call, when the user has switched them on: the
# official OpenTelemetry file export (`chat` spans with gen_ai.usage.* attributes) and the
# experimental debug log (llm_request lines). Both count input including the cached tokens.
# Each reader returns calls in one shape, input counted the Claude way (without the cache):
#   {"id", "ts", "model", "input", "output", "cache_read", "cache_write", "credits"}
# `id` is the span id, `ts` an iso_utc() time, `model` None when unknown, `credits` None when
# the log does not say. A call that cannot be placed in time is dropped. The readers only read:
# a file listed twice is read once, a repeated span id inside a session is one call, a bad line
# or an unreadable file is skipped, and nothing raises.

def _unique_files(paths):
    """(index, path) for each distinct file in `paths`: the same file under another spelling
    (case, ".", a symlink) is listed once. Anything that is not a path is skipped."""
    try:
        paths = [paths] if isinstance(paths, str) else list(paths)
    except TypeError:
        return
    seen = set()
    for path in paths:
        if not isinstance(path, str) or not path:
            continue
        try:
            key = _real(path)
        except (OSError, ValueError):
            continue
        if key not in seen:
            seen.add(key)
            yield len(seen) - 1, path


def _read_records(path):
    """(line number, record) for each JSON-object line of a JSONL file. A byte order mark is
    ignored; a line that is not an object (a half-written last line, noise) is skipped, and so
    is a file that cannot be read."""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            for n, line in enumerate(fh):
                try:
                    rec = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(rec, dict):
                    yield n, rec
    except (OSError, ValueError):           # unreadable, or a name open() refuses (an embedded NUL)
        return


def _loose_num(value):
    """A finite number from a number or a numeric string (OTLP JSON writes int64 as a string), else None."""
    if isinstance(value, str):
        text = value.strip()
        try:
            value = int(text)
        except ValueError:
            try:
                value = float(text)
            except ValueError:
                return None
    return _num(value)


def _span_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return _str(value)


def _call(cid, ts, model, total_input, output, cache_read, cache_write, credits):
    """One call, with the input counted the Claude way: the cache reads and writes taken out of the
    total, floored at 0."""
    return {"id": cid, "ts": ts, "model": model, "input": max(0, total_input - cache_read - cache_write),
            "output": output, "cache_read": cache_read, "cache_write": cache_write, "credits": credits}


def read_debug_calls(paths):
    """The LLM calls in agent debug logs (`debug-logs/<session>/*.jsonl`), in time order.

    Only `llm_request` lines count (a log written with the setting off holds just `session_start`).
    From attrs: input = max(0, inputTokens - cachedTokens), cache_read = cachedTokens, cache_write 0,
    credits = copilotUsageNanoAiu / 1e9, model normalised. `ts` is the line's epoch-millisecond or ISO
    time, `id` its spanId (a line without one gets an id from its place in the files). Pass the files of
    one session: a span id seen twice, in one file or across them, is one call."""
    calls, seen = [], set()
    for fi, path in _unique_files(paths):
        for n, rec in _read_records(path):
            if rec.get("type") != "llm_request":
                continue
            ts = iso_utc(rec.get("ts"))
            cid = _span_id(rec.get("spanId")) or "f%d:l%d" % (fi, n)
            if ts is None or cid in seen:
                continue
            seen.add(cid)
            attrs = _dict(rec.get("attrs"))
            calls.append(_call(cid, ts, normalize_model(attrs.get("model")),
                               _tokens(_loose_num(attrs.get("inputTokens"))),
                               _tokens(_loose_num(attrs.get("outputTokens"))),
                               _tokens(_loose_num(attrs.get("cachedTokens"))), 0,
                               _nano_credits(_loose_num(attrs.get("copilotUsageNanoAiu")))))
    calls.sort(key=lambda c: c["ts"])
    return calls


def _otel_value(value):
    """An OTLP attribute value ({"intValue": ..} and friends) as the plain value; anything else as it is."""
    if isinstance(value, dict):
        for key in ("intValue", "doubleValue", "stringValue", "boolValue"):
            if key in value:
                return value[key]
        return None
    return value


def _otel_attrs(raw):
    """A span's attributes as {name: value}, from a dict or from OTLP's [{"key", "value": {...}}] list."""
    if isinstance(raw, dict):
        return {k: _otel_value(v) for k, v in raw.items() if isinstance(k, str)}
    out = {}
    for item in _list(raw):
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            out[item["key"]] = _otel_value(item.get("value"))
    return out


def _otel_time(span):
    """The span's start as an iso_utc() string: startTimeUnixNano (a number, or OTLP's numeric string),
    else startTime / timestamp as [seconds, nanoseconds], epoch milliseconds or ISO. None if there is none."""
    nano = _loose_num(span.get("startTimeUnixNano"))
    if nano is not None and nano > 0:
        return iso_utc(int(nano) // 1000000)
    for key in ("startTime", "timestamp"):
        value = span.get(key)
        if isinstance(value, list) and len(value) == 2:
            sec, nanos = _num(value[0]), _num(value[1])
            value = None if sec is None or nanos is None else sec * 1000 + nanos / 1e6
        ts = iso_utc(value)
        if ts:
            return ts
    return None


def _otel_model(name, attrs):
    """The model a chat span ran: the response model, else the request model, else the name's second
    word ("chat gpt-4.1"); None for "auto" or when nothing says."""
    for key in ("gen_ai.response.model", "gen_ai.request.model"):
        model = normalize_model(attrs.get(key))
        if model:
            return model
    parts = name.split(None, 1) if isinstance(name, str) else []
    return normalize_model(parts[1]) if len(parts) == 2 else None


def _otel_spans(rec):
    """The spans in one line of an OTel file: the line itself, or the spans of an OTLP batch."""
    if "resourceSpans" not in rec:
        yield rec
        return
    for resource in _list(rec["resourceSpans"]):
        resource = _dict(resource)
        for scope in _list(resource.get("scopeSpans")) + _list(resource.get("instrumentationLibrarySpans")):
            for span in _list(_dict(scope).get("spans")):
                if isinstance(span, dict):
                    yield span


_OTEL_TOKEN_KEYS = ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
                    "gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_creation.input_tokens")


def read_otel_calls(paths):
    """The LLM calls in OpenTelemetry file exports, as {conversation id: [call, ...]} in time order.

    A `chat` span is kept when its name starts with "chat" or gen_ai.operation.name is "chat", and
    it has gen_ai.conversation.id (the VS Code session id), a start time and at least one token count (a span
    without any is not an LLM call, and must not make its session's tokens "exact" with nothing in them). Attributes may be a dict or
    OTLP's list of {"key", "value": {"intValue" | "doubleValue" | "stringValue"}}. input =
    max(0, gen_ai.usage.input_tokens - cache_read.input_tokens - cache_creation.input_tokens),
    credits = github.copilot.nano_aiu / 1e9, id = spanId. A span id seen twice in one conversation
    (the same export read again, a rotated file) is one call."""
    index, seen = {}, {}
    for fi, path in _unique_files(paths):
        for n, rec in _read_records(path):
            for si, span in enumerate(_otel_spans(rec)):
                attrs = _otel_attrs(span.get("attributes"))
                name = span.get("name")
                if not ((isinstance(name, str) and name.startswith("chat"))
                        or attrs.get("gen_ai.operation.name") == "chat"):
                    continue
                conv = _str(attrs.get("gen_ai.conversation.id"))
                ts = _otel_time(span)
                cid = _span_id(span.get("spanId")) or "f%d:l%d:s%d" % (fi, n, si)
                counts = [_tokens(_loose_num(attrs.get(k))) for k in _OTEL_TOKEN_KEYS]
                if not any(counts):
                    # no token counts: not a call. Kept, it would make the session "otel" and drop its approx. tokens
                    continue
                if conv is None or ts is None or cid in seen.setdefault(conv, set()):
                    continue
                seen[conv].add(cid)
                index.setdefault(conv, []).append(_call(
                    cid, ts, _otel_model(name, attrs), counts[0], counts[1], counts[2], counts[3],
                    _nano_credits(_loose_num(attrs.get("github.copilot.nano_aiu")))))
    for calls in index.values():
        calls.sort(key=lambda c: c["ts"])
    return index


def exact_calls_for(sid, debug, otel_index):
    """Where session `sid` gets its exact tokens from, one source and never both: ("otel", calls) when
    the OpenTelemetry index (read_otel_calls) has calls for it, else ("debug", calls) when its debug-log
    files (`debug`: {sid: [paths]}, as discover() lists them) hold any llm_request, else ("none", [])."""
    calls = _dict(otel_index).get(sid)
    if isinstance(calls, list) and calls:
        return "otel", list(calls)
    calls = read_debug_calls(_dict(debug).get(sid) or [])
    if calls:
        return "debug", calls
    return "none", []


def usage_events(sid, calls, project, cwd, branch):
    """One Claude-shaped assistant event per exact call, for parse_entry(): id and requestId
    f"{sid}:x:{call id}", usage in the Claude convention and the credits as _cost_override.

    Tokens without credits get _cost_override 0.0 and _no_credits True (never a price from the Claude
    table); a call with no tokens at all has usage {} and carries _cost_override only if its credits
    are known. A call without a usable time is skipped. `project` is accepted for symmetry with the
    other translators; events carry no project."""
    out = []
    for call in calls:
        call = _dict(call)
        ts = iso_utc(call.get("ts"))
        if ts is None:
            continue
        inp, outp = _tokens(call.get("input")), _tokens(call.get("output"))
        read, write = _tokens(call.get("cache_read")), _tokens(call.get("cache_write"))
        usage = {}
        if inp or outp or read or write:        # a call that recorded nothing is not a call with 0 tokens
            usage = {"input_tokens": inp, "output_tokens": outp,
                     "cache_read_input_tokens": read, "cache_creation_input_tokens": write}
        eid = "%s:x:%s" % (sid, call.get("id"))
        event = {"type": "assistant", "timestamp": ts, "sessionId": sid, "requestId": eid,
                 "cwd": cwd or "", "gitBranch": branch or "",
                 "message": {"id": eid, "model": _str(call.get("model")) or "unknown", "usage": usage,
                             "stop_reason": None, "content": []}}
        credits = _num(call.get("credits"))
        _set_credits(event, usage, float(credits) if credits is not None and credits >= 0 else None)
        out.append(event)
    return out


# ---------------------------------------------------------------------------
# Projects, the session store, and reading a log's lines
# ---------------------------------------------------------------------------

_DRIVE_RE = re.compile(r"^/?([A-Za-z]):")
_STORE_FIELDS = ("cwd", "repository", "branch", "summary")


def read_lines(path):
    """A JSONL log's lines for the translators: a byte order mark on line 1 is dropped (so the first
    record still parses) and bytes that are not UTF-8 are replaced. None when the file cannot be read."""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            return fh.readlines()
    except (OSError, ValueError):
        return None


def _uri_path(uri):
    """The local path in a VS Code folder URI (file:///c%3A/x -> c:/x, file:///home/x -> /home/x,
    file://server/share -> //server/share); another scheme gives its path part. None when there is none,
    or when the URI cannot be parsed."""
    uri = _str(uri)
    if uri is None:
        return None
    try:
        parts = urlsplit(uri.strip())
    except ValueError:                          # e.g. "Invalid IPv6 URL" for a stray "[": no folder, not a failure
        return None
    if len(parts.scheme) < 2:                   # no scheme, or a bare drive letter (c:/x)
        path = uri.strip()
    else:
        path = unquote(parts.path)
        if parts.scheme == "file" and parts.netloc:
            path = "//" + parts.netloc + path
    if _DRIVE_RE.match(path) and path.startswith("/"):
        path = path[1:]                          # /c:/x -> c:/x
    return path or None


def workspace_folder(ws_dir):
    """The project folder of a workspaceStorage/<hash> folder, from its workspace.json: the decoded
    `folder` URI, or for a `workspace` (a .code-workspace file) the folder that holds that file.
    None when there is no readable workspace.json or it names neither."""
    if not ws_dir:
        return None
    try:
        with open(os.path.join(ws_dir, "workspace.json"), "r", encoding="utf-8-sig", errors="replace") as fh:
            data = _dict(json.load(fh))
    except (OSError, ValueError, RecursionError):
        return None
    folder = _uri_path(data.get("folder"))
    if folder:
        return folder
    workspace = _uri_path(data.get("workspace"))
    if not workspace:
        return None
    return posixpath.dirname(workspace.replace("\\", "/")) or None


def project_label(path):
    """The dashboard's project name for a folder, the way the Claude side names it: every character that
    is not a letter or digit becomes "-" (as Claude Code names its log folders), then the Claude Scanner's
    prettify. The drive letter is upper-cased first, because VS Code writes c: where Claude Code's folder
    names say C:, and the same folder must get the same label in both tools. None -> "(no folder)"."""
    path = _str(path)
    if path is None:
        return "(no folder)"
    path = path.strip()
    path = path.rstrip("/\\") or path
    if _DRIVE_RE.match(path) and not path.startswith("/"):
        path = path[0].upper() + path[1:]
    label = re.sub(r"[^A-Za-z0-9]", "-", path)
    return label.lstrip("-").replace("--", "/").replace("-", "/") or "/"


def _open_store(path, tmpdir):
    """A read-only connection to a session-store database. A database in WAL mode keeps its recent writes
    in the -wal file, which an immutable open would ignore, so that kind is read from a temp copy in
    `tmpdir`. The caller looks for the -wal once and passes a tmpdir only when it found one (None: no
    -wal), so a -wal that appears between two looks cannot leave the copy without a folder."""
    if tmpdir is not None:
        dst = os.path.join(tmpdir, "%d.db" % len(os.listdir(tmpdir)))
        shutil.copyfile(path, dst)
        shutil.copyfile(path + "-wal", dst + "-wal")
        return sqlite3.connect(dst, timeout=2)
    uri = pathlib.Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=2)


def _store_rows(con):
    """(id, {cwd, repository, branch, summary}) for each row of the sessions table; a column the table
    lacks reads as None, and a table without an id column (or no table) gives nothing."""
    cols = {row[1] for row in con.execute('PRAGMA table_info("sessions")')}
    if "id" not in cols:
        return []
    fields = [c for c in _STORE_FIELDS if c in cols]
    sql = 'SELECT "id"%s FROM "sessions"' % "".join(', "%s"' % c for c in fields)
    out = []
    for row in con.execute(sql):
        if row[0] is None:
            continue
        values = dict(zip(fields, row[1:]))
        out.append((str(row[0]), {c: _str(values.get(c)) for c in _STORE_FIELDS}))
    return out


def read_session_store(db_paths, problems=None):
    """The Copilot Chat session-store databases' sessions table, as {session id: {"cwd", "repository",
    "branch", "summary"}} (None for what a row lacks). Only read: an immutable read-only open, or a temp
    copy when a -wal file exists. A missing table, a file that is not a database or cannot be opened gives
    nothing, and so does any other error reading one database: the others are still read. With several
    databases the first that has a value for a field wins. `problems`, when a list, gets the path of each
    database that could not be read (a copy taken while it was being written, say), so a caller can try
    again instead of keeping the gap."""
    out, tmpdir = {}, None
    try:
        for _i, path in _unique_files(db_paths):
            if not os.path.isfile(path):
                continue
            try:
                wal = os.path.exists(path + "-wal")          # looked at once: _open_store follows this answer
                if wal and tmpdir is None:
                    tmpdir = tempfile.mkdtemp(prefix="afi_store_")
                con = _open_store(path, tmpdir if wal else None)
                try:
                    rows = _store_rows(con)
                finally:
                    con.close()
            except Exception:              # sqlite3.Error, OSError, ...: one bad database never fails the scan
                if isinstance(problems, list):
                    problems.append(path)
                continue
            for sid, row in rows:
                have = out.setdefault(sid, dict.fromkeys(_STORE_FIELDS))
                for key, value in row.items():
                    if have[key] is None:
                        have[key] = value
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return out
