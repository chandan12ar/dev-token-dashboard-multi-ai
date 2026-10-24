#!/usr/bin/env python3
"""
Copilot log check — a read-only scan of the files GitHub Copilot writes on this machine.

It finds Copilot's local logs (VS Code chat sessions, transcripts, debug logs, Copilot CLI
sessions, OpenTelemetry exports) and writes a report of their *structure*: which fields
exist, their types, how often they appear, number ranges and date ranges. It never copies
prompt text, code, file paths or project names — every text value is replaced by its
length. The only text values shown are short product names on a fixed allow-list
(model ids, tool names, event types, versions).

It changes nothing: files are opened read-only and SQLite databases with mode=ro.

Usage:
    python copilot_log_check.py                    # writes copilot_log_report.md here
    python copilot_log_check.py --out report.md
    python copilot_log_check.py --root "D:/portable/Code/User"   # extra VS Code User folder

Read the report before sharing it.

Requires: Python 3.8+ (standard library only).
"""

import argparse
import json
import os
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1"

VARIANTS = ["Code", "Code - Insiders", "Code - Exploration", "VSCodium", "Cursor"]
REMOTE_ROOTS = [".vscode-server", ".vscode-server-insiders", ".vscode-remote"]
EXT_HOMES = [".vscode", ".vscode-insiders", ".vscode-exploration", ".vscode-oss", ".cursor",
             ".vscode-server", ".vscode-server-insiders"]
# The Copilot Chat extension's own storage folder has had several spellings.
COPILOT_EXT_FOLDERS = ["GitHub.copilot-chat", "github.copilot-chat", "GitHub.copilot", "github.copilot"]

MAX_FILES = 30        # most recent files sampled per data source
MAX_LINES = 50000     # lines read per JSONL file
MAX_DEPTH = 12
MAX_FIELDS = 2500     # distinct field paths kept per record type
MAX_ROWS = 300        # field rows printed per record type
MAX_VALUES = 25       # distinct allow-listed values kept per field

STORES = [
    # key, label, what it is
    ("chat_json", "VS Code chat sessions (.json)",
     "workspaceStorage/<hash>/chatSessions/*.json — older whole-file format"),
    ("chat_jsonl", "VS Code chat sessions (.jsonl)",
     "workspaceStorage/<hash>/chatSessions/*.jsonl — incremental kind 0/1/2 format"),
    ("empty_window", "VS Code empty-window chats",
     "globalStorage/emptyWindowChatSessions/* — chats with no folder open"),
    ("transcripts", "Copilot Chat transcripts",
     "workspaceStorage/<hash>/GitHub.copilot-chat/transcripts/*.jsonl — newer event format"),
    ("debug_logs", "Copilot Chat debug logs",
     "workspaceStorage/<hash>/GitHub.copilot-chat/debug-logs/<id>/*.jsonl — per-LLM-call tokens"),
    ("otel", "OpenTelemetry export files",
     "github.copilot.chat.otel outfile, COPILOT_OTEL_FILE_EXPORTER_PATH, ~/.copilot/otel/*.jsonl"),
    ("cli_events", "Copilot CLI sessions",
     "~/.copilot/session-state/<id>/events.jsonl"),
    ("jetbrains", "Copilot for JetBrains sessions",
     "~/.copilot/jb/<id>/partition-*.jsonl"),
]
STORE_LABEL = {k: label for k, label, _ in STORES}

# Keys whose short values are product names (event types, models, tools, versions).
SAFE_VALUE_KEYS = {
    "type", "kind", "modelId", "model", "identifier", "details", "mode", "family", "vendor",
    "producer", "copilotVersion", "vscodeVersion", "version", "status", "debugName",
    "toolName", "toolId", "finishReason", "finish_reason", "location", "agentMode",
    "host_type", "agent_name", "edit_type", "unit", "scope", "level", "severity",
    "category", "tokenType", "initiator", "interactionType", "apiEndpoint", "aiCreditsStatus",
    "reasoningEffort", "routingMethod", "chosenModel", "currentModel", "modelDisplayName",
    "selectedModel", "resolvedModel", "originApi", "phase", "initialLocation",
    "permissionLevel", "frontierSource", "transport", "autoTier",
    "gen_ai.request.model", "gen_ai.response.model", "gen_ai.agent.name",
    "gen_ai.provider.name", "gen_ai.tool.name", "gen_ai.tool.type", "gen_ai.operation.name",
    "github.copilot.agent.type", "service.name", "service.version",
}
# Subtrees that only hold prompt/tool content: the field is listed, its insides are not.
CONTENT_KEYS = {"toolCallResults", "renderedUserMessage", "renderedGlobalContext", "inputMessages",
                "requestOptions", "configurationSchema", "outputBuffer"}
SAFE_VALUE_RE = re.compile(r"^[\w .\-+:•()\[\],/]{1,80}$")
MODEL_HINT_RE = re.compile(r"claude|gpt|gemini|grok|sonnet|opus|haiku|codex|raptor|llama|mistral|\bo\d", re.I)
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
ID_PREFIX_RE = re.compile(r"^(toolu|call|msg|req|request|response|tooluse|chatcmpl)[_-]", re.I)
HEXISH_RE = re.compile(r"^[0-9a-fA-F-]{8,}$")
TIME_KEY_RE = re.compile(r"time|date|ts$|At$|_at$|start|end|created|updated", re.I)
SETTING_RE = re.compile(r'"((?:github\.copilot|chat)[^"]*)"\s*:\s*(true|false|-?\d+(?:\.\d+)?|"[^"]*")')
EXT_RE = re.compile(r"^(github\.copilot(?:-chat)?)-(\d[\w.\-]*)$", re.I)

HOME = os.path.expanduser("~")


USER_NAMES = {n for n in (os.environ.get("USERNAME"), os.environ.get("USER"),
                          os.path.basename(os.path.normpath(HOME))) if n and len(n) > 1}


def tidy_path(p):
    """Show a path with the home folder as ~ and no user name — even when a company
    redirects AppData to a network share like \\\\server\\users\\<name>."""
    p = os.path.normpath(p)
    home = os.path.normpath(HOME)
    if os.path.normcase(p).startswith(os.path.normcase(home)):
        p = "~" + p[len(home):]
    for name in USER_NAMES:
        p = re.sub(re.escape(name), "<user>", p, flags=re.I)
    return p.replace("\\", "/")


def listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def fmt_day(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


def epoch_to_day(v):
    """A number that looks like a Unix time (ms or s) as YYYY-MM-DD, else None."""
    if 1.4e12 <= v <= 2.6e12:
        v = v / 1000.0
    elif not 1.4e9 <= v <= 2.6e9:
        return None
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


def safe_value(v):
    v = v.strip()
    if not SAFE_VALUE_RE.match(v) or v.count("/") > 1 or v.startswith(("/", "file:", "vscode")):
        return None
    return v


def key_label(k):
    """Field names that are really ids, paths or numbers collapse to a placeholder."""
    if k.isdigit():
        return "<n>"
    if MODEL_HINT_RE.search(k) and len(k) <= 60 and safe_value(k):
        return k
    if (len(k) > 40 or re.search(r"[\\/:@ ]", k) or HEXISH_RE.match(k) or ID_PREFIX_RE.match(k)
            or sum(c.isdigit() for c in k) >= 5):
        return "<key>"
    return k


def value_allowed(path, key):
    if key in SAFE_VALUE_KEYS:
        return True
    parent = path.rsplit(".", 1)[0].lower() if "." in path else ""
    if key == "name":
        return path == "name" or "tool" in parent
    if key == "id":
        return parent.endswith("agent")
    if key == "key":                       # OTel attribute lists: [{key, value}, ...]
        return "attributes" in parent
    return False


class Field:
    __slots__ = ("n", "types", "nmin", "nmax", "slen", "dmin", "dmax", "vals", "more", "t", "f")

    def __init__(self):
        self.n = 0
        self.types = Counter()
        self.nmin = self.nmax = None
        self.slen = 0
        self.dmin = self.dmax = None
        self.vals = Counter()
        self.more = False
        self.t = self.f = 0

    def day(self, d):
        if d:
            self.dmin = d if self.dmin is None or d < self.dmin else self.dmin
            self.dmax = d if self.dmax is None or d > self.dmax else self.dmax


class Tree:
    """Field paths, types and ranges for one kind of record."""

    def __init__(self):
        self.records = 0
        self.fields = {}
        self.dropped = 0
        self.kpaths = Counter()   # incremental-format update paths ("requests.<n>.result")

    def add(self, obj):
        self.records += 1
        if (isinstance(obj, dict) and "v" in obj and isinstance(obj.get("kind"), int)
                and isinstance(obj.get("k", []), list)):
            self._add_incremental(obj)
        else:
            self._walk(obj, "", "", 0)

    def _add_incremental(self, obj):
        """VS Code's incremental log: {kind, k: path, v: value}. File the value under its
        path (requests[].result...) so the fields line up with the whole-file format."""
        base, last = "", ""
        for seg in obj.get("k") or []:
            if isinstance(seg, int) or str(seg).isdigit():
                base += "[]"
            else:
                last = str(seg)
                base = f"{base}.{key_label(last)}" if base else key_label(last)
        if base:
            self.kpaths[base] += 1
        for k, v in obj.items():
            if k != "v":
                self._walk(v, key_label(str(k)), str(k), 1)
        self._walk(obj["v"], base, last, 1 if base else 0)

    def _field(self, path):
        f = self.fields.get(path)
        if f is None:
            if len(self.fields) >= MAX_FIELDS:
                self.dropped += 1
                return None
            f = self.fields[path] = Field()
        return f

    def _walk(self, v, path, key, depth):
        if path:
            f = self._field(path)
            if f is None:
                return
            f.n += 1
            if isinstance(v, bool):
                f.types["bool"] += 1
                if v:
                    f.t += 1
                else:
                    f.f += 1
            elif isinstance(v, (int, float)):
                f.types["number"] += 1
                f.nmin = v if f.nmin is None or v < f.nmin else f.nmin
                f.nmax = v if f.nmax is None or v > f.nmax else f.nmax
                if TIME_KEY_RE.search(key):
                    f.day(epoch_to_day(v))
            elif isinstance(v, str):
                f.types["text"] += 1
                f.slen = max(f.slen, len(v))
                if ISO_RE.match(v):
                    f.day(v[:10])
                elif value_allowed(path, key):
                    sv = safe_value(v)
                    if sv is not None:
                        if sv in f.vals or len(f.vals) < MAX_VALUES:
                            f.vals[sv] += 1
                        else:
                            f.more = True
            elif v is None:
                f.types["null"] += 1
            elif isinstance(v, list):
                f.types["list"] += 1
            elif isinstance(v, dict):
                f.types["object"] += 1
        if depth >= MAX_DEPTH or key in CONTENT_KEYS:
            return
        if isinstance(v, dict):
            for k, child in v.items():
                k = str(k)
                label = key_label(k)
                self._walk(child, f"{path}.{label}" if path else label, k, depth + 1)
        elif isinstance(v, list):
            for child in v:
                self._walk(child, f"{path}[]" if path else "[]", key, depth + 1)


def record_kind(obj):
    """JSONL lines are told apart by a string 'type' or a numeric 'kind'."""
    if isinstance(obj, dict):
        t = obj.get("type")
        if isinstance(t, str):
            return "type=" + (safe_value(t) or "<other>")
        k = obj.get("kind")
        if isinstance(k, int) and not isinstance(k, bool):
            return f"kind={k}"
        if "resourceSpans" in obj or "resourceMetrics" in obj or "resourceLogs" in obj:
            return "otel-batch"
        if "traceId" in obj or "spanId" in obj:
            return "span"
    return "(no type)"


class Report:
    def __init__(self):
        self.roots = []                 # (variant, path)
        self.files = {k: {} for k, _, _ in STORES}   # store -> {normpath: (path, workspace)}
        self.workspaces = 0
        self.workspace_json = 0
        self.workspace_json_keys = Counter()
        self.state_dbs = []
        self.store_dbs = []
        self.settings = []              # (root label, key, shown value)
        self.otel_paths = set()
        self.extensions = Counter()     # "github.copilot-chat 0.68.0"
        self.ext_logs = Counter()       # files / bytes
        self.ext_log_newest = 0
        self.copilot_home = []          # top-level names under ~/.copilot
        self.ext_storage_names = Counter()
        self.errors = Counter()
        self.trees = {}                 # store -> {record kind -> Tree}
        self.sampled = {}               # store -> (sampled files, lines read, bad lines)
        self.db_reports = []
        self.state_keys = Counter()
        self.state_key_size = Counter()

    def add_file(self, store, path, workspace=""):
        self.files[store].setdefault(os.path.normcase(os.path.realpath(path)), (path, workspace))


# ---------------------------------------------------------------------------
# Discovery — find every place Copilot keeps data
# ---------------------------------------------------------------------------

def user_roots(extra):
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(HOME, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    roots = [(v, os.path.join(base, v, "User")) for v in VARIANTS]
    roots += [(r, os.path.join(HOME, r, "data", "User")) for r in REMOTE_ROOTS]
    roots += [("custom", p) for p in extra]
    out, seen = [], set()
    for label, path in roots:
        key = os.path.normcase(os.path.realpath(path))
        if os.path.isdir(path) and key not in seen:
            seen.add(key)
            out.append((label, path))
            # A named profile keeps its own globalStorage (and sometimes its own chats).
            for prof in listdir(os.path.join(path, "profiles")):
                pp = os.path.join(path, "profiles", prof)
                if os.path.isdir(pp):
                    out.append((label + " (profile)", pp))
    return out


def ext_dirs(parent):
    """The Copilot extension folders under parent, once each (Windows and macOS ignore case)."""
    out, seen = [], set()
    for ext in COPILOT_EXT_FOLDERS:
        d = os.path.join(parent, ext)
        key = os.path.normcase(os.path.realpath(d))
        if os.path.isdir(d) and key not in seen:
            seen.add(key)
            out.append((ext, d))
    return out


def scan_jsonl_dir(rep, store, folder, workspace=""):
    for fn in listdir(folder):
        if fn.endswith(".jsonl") or fn.endswith(".json"):
            rep.add_file(store, os.path.join(folder, fn), workspace)


def scan_user_root(rep, label, root):
    ws_root = os.path.join(root, "workspaceStorage")
    for h in listdir(ws_root):
        d = os.path.join(ws_root, h)
        if not os.path.isdir(d):
            continue
        rep.workspaces += 1
        wj = os.path.join(d, "workspace.json")
        if os.path.isfile(wj):
            rep.workspace_json += 1
            try:
                with open(wj, "r", encoding="utf-8", errors="replace") as fh:
                    for k in json.load(fh):
                        rep.workspace_json_keys[key_label(str(k))] += 1
            except (OSError, ValueError, TypeError):
                rep.errors["workspace.json unreadable"] += 1
        cs = os.path.join(d, "chatSessions")
        for fn in listdir(cs):
            if fn.endswith(".jsonl"):
                rep.add_file("chat_jsonl", os.path.join(cs, fn), h)
            elif fn.endswith(".json"):
                rep.add_file("chat_json", os.path.join(cs, fn), h)
        for ext, ed in ext_dirs(d):
            scan_jsonl_dir(rep, "transcripts", os.path.join(ed, "transcripts"), h)
            dl = os.path.join(ed, "debug-logs")
            for sid in listdir(dl):
                sd = os.path.join(dl, sid)
                if os.path.isdir(sd):
                    scan_jsonl_dir(rep, "debug_logs", sd, h)
                elif sid.endswith(".jsonl"):
                    rep.add_file("debug_logs", sd, h)
            for fn in listdir(ed):
                rep.ext_storage_names["workspaceStorage/<hash>/" + ext + "/" + key_label(fn)] += 1
        sdb = os.path.join(d, "state.vscdb")
        if os.path.isfile(sdb):
            rep.state_dbs.append(sdb)

    gs = os.path.join(root, "globalStorage")
    scan_jsonl_dir(rep, "empty_window", os.path.join(gs, "emptyWindowChatSessions"))
    for ext, ed in ext_dirs(gs):
        for fn in listdir(ed):
            rep.ext_storage_names["globalStorage/" + ext + "/" + key_label(fn)] += 1
            if fn.endswith(".db"):
                rep.store_dbs.append(os.path.join(ed, fn))
    sdb = os.path.join(gs, "state.vscdb")
    if os.path.isfile(sdb):
        rep.state_dbs.append(sdb)

    settings = os.path.join(root, "settings.json")
    if os.path.isfile(settings):
        try:
            with open(settings, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for key, raw in SETTING_RE.findall(text):
                shown = raw if not raw.startswith('"') else "<text set>"
                rep.settings.append((label, key, shown))
                if key == "github.copilot.chat.otel.outfile" and raw.startswith('"') and len(raw) > 2:
                    rep.otel_paths.add(os.path.expanduser(json.loads(raw)))
        except (OSError, ValueError):
            rep.errors["settings.json unreadable"] += 1

    # VS Code's own log folder sits next to User: <variant>/logs/<session>/window*/exthost/<ext>/
    logs = os.path.join(os.path.dirname(root), "logs")
    for dirpath, _dirs, files in os.walk(logs):
        if "copilot" not in dirpath.lower():
            continue
        for fn in files:
            try:
                st = os.stat(os.path.join(dirpath, fn))
            except OSError:
                continue
            rep.ext_logs["files"] += 1
            rep.ext_logs["bytes"] += st.st_size
            rep.ext_log_newest = max(rep.ext_log_newest, st.st_mtime)


def scan_copilot_home(rep):
    ch = os.path.join(HOME, ".copilot")
    rep.copilot_home = [key_label(n) for n in listdir(ch)]
    ss = os.path.join(ch, "session-state")
    for name in listdir(ss):
        p = os.path.join(ss, name)
        if os.path.isdir(p):
            ev = os.path.join(p, "events.jsonl")
            if os.path.isfile(ev):
                rep.add_file("cli_events", ev)
        elif name.endswith(".jsonl"):
            rep.add_file("cli_events", p)
    scan_jsonl_dir(rep, "otel", os.path.join(ch, "otel"))
    for name in listdir(os.path.join(ch, "jb")):
        d = os.path.join(ch, "jb", name)
        for fn in listdir(d):
            if fn.startswith("partition-") and fn.endswith(".jsonl"):
                rep.add_file("jetbrains", os.path.join(d, fn))
    for fn in listdir(ch):
        if fn.endswith(".db"):
            rep.store_dbs.append(os.path.join(ch, fn))
    env = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH")
    if env:
        rep.otel_paths.add(os.path.expanduser(env))
    for p in rep.otel_paths:
        if os.path.isfile(p):
            rep.add_file("otel", p)


def scan_extensions(rep):
    for home in EXT_HOMES:
        for name in listdir(os.path.join(HOME, home, "extensions")):
            m = EXT_RE.match(name)
            if m:
                rep.extensions[f"{m.group(1).lower()} {m.group(2)}  ({home})"] += 1


# ---------------------------------------------------------------------------
# Analysis — read a sample of each data source into field trees
# ---------------------------------------------------------------------------

def analyse_store(rep, store):
    entries = list(rep.files[store].values())
    def mtime(e):
        try:
            return os.path.getmtime(e[0])
        except OSError:
            return 0
    entries.sort(key=mtime, reverse=True)
    trees = rep.trees.setdefault(store, {})
    lines_read = bad = 0
    for path, _ws in entries[:MAX_FILES]:
        try:
            if path.endswith(".json"):
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    obj = json.load(fh)
                trees.setdefault("(whole file)", Tree()).add(obj)
                lines_read += 1
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i >= MAX_LINES:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    lines_read += 1
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        bad += 1
                        continue
                    trees.setdefault(record_kind(obj), Tree()).add(obj)
        except (OSError, ValueError, RecursionError):
            rep.errors[f"{STORE_LABEL[store]}: file unreadable"] += 1
    rep.sampled[store] = (min(len(entries), MAX_FILES), lines_read, bad)


def open_ro(path, tmpdir):
    """Open a database without touching it. A database in WAL mode keeps recent writes in
    its -wal file, which a read-only open can't replay, so that kind is read from a copy."""
    if os.path.exists(path + "-wal"):
        dst = os.path.join(tmpdir, f"{len(os.listdir(tmpdir))}.db")
        shutil.copyfile(path, dst)
        shutil.copyfile(path + "-wal", dst + "-wal")
        return sqlite3.connect(dst, timeout=2)
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=2)


def analyse_store_db(rep, path, tmpdir):
    out = {"path": tidy_path(path), "tables": [], "error": None}
    try:
        con = open_ro(path, tmpdir)
        try:
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            for t in names:
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
                rows = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                ranges = []
                for c in cols:
                    if TIME_KEY_RE.search(c) or c in ("timestamp", "ts"):
                        lo, hi = con.execute(f'SELECT MIN("{c}"), MAX("{c}") FROM "{t}"').fetchone()
                        ranges.append((c, describe_range(lo), describe_range(hi)))
                out["tables"].append((t, cols, rows, ranges))
        finally:
            con.close()
    except (sqlite3.Error, OSError) as e:
        out["error"] = type(e).__name__
    rep.db_reports.append(out)


def describe_range(v):
    if isinstance(v, (int, float)):
        return epoch_to_day(v) or str(v)
    if isinstance(v, str) and ISO_RE.match(v):
        return v[:10]
    return "-" if v is None else "<text>"


def analyse_state_db(rep, path, tmpdir):
    try:
        con = open_ro(path, tmpdir)
        try:
            rows = con.execute(
                "SELECT key, length(value) FROM ItemTable WHERE key LIKE '%chat%' "
                "OR key LIKE '%copilot%' OR key LIKE '%interactive%'").fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        rep.errors["state.vscdb unreadable"] += 1
        return
    for key, size in rows:
        label = ".".join(key_label(p) for p in str(key).split("."))
        rep.state_keys[label] += 1
        rep.state_key_size[label] = max(rep.state_key_size[label], size or 0)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def has_path(rep, store, *needles):
    for tree in rep.trees.get(store, {}).values():
        for p in tree.fields:
            if any(p.endswith(n) for n in needles):
                return True
    return False


def has_value(rep, store, *needles):
    for tree in rep.trees.get(store, {}).values():
        for f in tree.fields.values():
            if any(n in f.vals for n in needles):
                return True
    return False


def verdict_rows(rep):
    n = {k: len(v) for k, v in rep.files.items()}
    sessions = n["chat_json"] + n["chat_jsonl"] + n["empty_window"] + n["transcripts"]
    chat = ("chat_json", "chat_jsonl", "empty_window")
    req_tokens = any(has_path(rep, s, "promptTokens", "outputTokens", "completionTokens") for s in chat)
    req_credits = any(has_path(rep, s, "copilotCredits") for s in chat)
    debug_llm = "type=llm_request" in rep.trees.get("debug_logs", {})
    debug_aiu = has_path(rep, "debug_logs", "copilotUsageNanoAiu")
    otel_tokens = (has_path(rep, "otel", "input_tokens")
                   or has_value(rep, "otel", "gen_ai.usage.input_tokens"))
    cli = rep.trees.get("cli_events", {})
    cli_exact = "type=session.usage_record" in cli or "type=session.shutdown" in cli
    yes = lambda b: "yes" if b else "no"
    return [
        ("Chat sessions (prompts, timestamps, models, projects)", yes(sessions),
         f"{sessions} session files — sessions, prompts, activity heatmap, per-project"),
        ("Per-request tokens inside session files", yes(req_tokens),
         "approximate tokens (agent turns record only their last LLM call)"),
        ("Per-request Copilot credits inside session files", yes(req_credits),
         "what each prompt cost in AI credits"),
        ("Debug logs with llm_request events", yes(debug_llm),
         "exact tokens per LLM call + time to first token (needs the 'Agent Debug Log › File Logging' setting)"),
        ("Exact Copilot billing in debug logs (copilotUsageNanoAiu)", yes(debug_aiu),
         "real AI-credit cost per LLM call instead of an estimate"),
        ("OpenTelemetry file export with token counts", yes(otel_tokens),
         "exact tokens via the documented github.copilot.chat.otel.* settings"),
        ("Copilot CLI sessions", yes(n["cli_events"]),
         f"{n['cli_events']} CLI sessions" + (", with exact per-call tokens and credits" if cli_exact else "")),
    ]


def fmt_field(f):
    parts = []
    if f.vals:
        vals = ", ".join(f"`{v}` ×{c}" for v, c in f.vals.most_common(12))
        parts.append(vals + (", …" if f.more or len(f.vals) > 12 else ""))
    if f.dmin:
        parts.append(f"dates {f.dmin} → {f.dmax}")
    if f.nmin is not None and not f.dmin:
        parts.append(f"{f.nmin:g} … {f.nmax:g}")
    if f.types.get("text") and not f.vals and not f.dmin:
        parts.append(f"text, up to {f.slen:,} chars")
    if f.t or f.f:
        parts.append(f"true {f.t} / false {f.f}")
    return "; ".join(parts) or "—"


def write_report(rep, out_path):
    L = []
    w = L.append
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    w("# Copilot log check report")
    w("")
    w(f"Generated {now} · script v{VERSION} · {platform.system()} {platform.release()} · "
      f"Python {platform.python_version()}")
    w("")
    w("> This report lists the **structure** of Copilot's local files: field names, types, counts,")
    w("> number ranges and date ranges. Text values are replaced by their length; the only text shown")
    w("> is short product names (models, tools, event types, versions). Paths use `~` for your home")
    w("> folder. **Read it before sharing.**")
    w("")

    w("## 1. What the dashboard could read on this machine")
    w("")
    w("| Data | Found | What it gives |")
    w("|---|---|---|")
    for a, b, c in verdict_rows(rep):
        w(f"| {a} | **{b}** | {c} |")
    w("")

    w("## 2. Where Copilot data was found")
    w("")
    w("**VS Code installs scanned:** " + (", ".join(f"{v} (`{tidy_path(p)}`)" for v, p in rep.roots) or "none found"))
    w("")
    w(f"**Workspaces:** {rep.workspaces} folders in workspaceStorage, {rep.workspace_json} with a "
      f"workspace.json (keys: {', '.join(f'{k} ×{c}' for k, c in rep.workspace_json_keys.most_common()) or '—'})")
    w("")
    w("| Data source | Files | Size | Oldest → newest (file dates) | Workspaces | Where |")
    w("|---|---|---|---|---|---|")
    for key, label, where in STORES:
        files = list(rep.files[key].values())
        if not files:
            w(f"| {label} | 0 | — | — | — | {where} |")
            continue
        sizes, times = 0, []
        for p, _ in files:
            try:
                st = os.stat(p)
            except OSError:
                continue
            sizes += st.st_size
            times.append(st.st_mtime)
        span = f"{fmt_day(min(times))} → {fmt_day(max(times))}" if times else "—"
        wss = len({ws for _, ws in files if ws}) or "—"
        w(f"| {label} | {len(files)} | {fmt_bytes(sizes)} | {span} | {wss} | {where} |")
    w("")
    w("**Copilot extensions in the user extensions folders:** " + (", ".join(sorted(rep.extensions)) or
      "none (newer VS Code ships Copilot Chat built in — its version shows up in section 3)"))
    w("")
    if rep.ext_logs:
        w(f"**Copilot output-channel logs** (VS Code `logs/` folder): {rep.ext_logs['files']} files, "
          f"{fmt_bytes(rep.ext_logs['bytes'])}, newest {fmt_day(rep.ext_log_newest)}")
        w("")
    w("**Copilot settings in settings.json:**")
    w("")
    if rep.settings:
        for label, key, shown in rep.settings:
            w(f"- `{key}` = {shown}  ({label})")
    else:
        w("- none set (all defaults)")
    w("")
    envs = sorted(k for k in os.environ if k.startswith("COPILOT_OTEL") or k.startswith("OTEL_EXPORTER"))
    w("**Copilot/OTel environment variables:** " + (", ".join(f"`{k}`" for k in envs) or "none"))
    w("")
    w("**`~/.copilot` contents (names only):** " + (", ".join(f"`{n}`" for n in rep.copilot_home) or "folder not found"))
    w("")
    if rep.ext_storage_names:
        w("**Copilot Chat extension storage (names, how many workspaces have each):**")
        w("")
        for name, c in sorted(rep.ext_storage_names.items()):
            w(f"- `{name}` ×{c}")
        w("")
    if rep.db_reports:
        w("**SQLite stores (schema and row counts only):**")
        w("")
        for db in rep.db_reports:
            w(f"- `{db['path']}`" + (f" — could not open ({db['error']})" if db["error"] else ""))
            for t, cols, rows, ranges in db["tables"]:
                rng = "; ".join(f"{c}: {lo} → {hi}" for c, lo, hi in ranges)
                w(f"  - table `{t}` — {rows} rows — columns: {', '.join(cols)}" + (f" — {rng}" if rng else ""))
        w("")
    if rep.state_keys:
        w(f"**Chat-related keys in VS Code state databases** ({len(rep.state_dbs)} state.vscdb files, "
          "key names and largest value size only):")
        w("")
        for k, c in sorted(rep.state_keys.items()):
            w(f"- `{k}` — in {c} databases, up to {fmt_bytes(rep.state_key_size[k])}")
        w("")
    if rep.errors:
        w("**Problems while reading:** " + ", ".join(f"{k} ×{c}" for k, c in rep.errors.items()))
        w("")

    w("## 3. Field structure per data source")
    w("")
    w(f"Each source is sampled from its {MAX_FILES} most recent files. \"Seen\" counts how many times")
    w("the field appeared. `[]` marks a list, `<key>` an id-like or path-like key, `<n>` a numeric key.")
    w("")
    for key, label, _ in STORES:
        trees = rep.trees.get(key)
        if not trees:
            continue
        files, lines, bad = rep.sampled.get(key, (0, 0, 0))
        w(f"### {label}")
        w("")
        w(f"Sampled {files} of {len(rep.files[key])} files, {lines:,} records"
          + (f", {bad} unreadable lines" if bad else "") + ".")
        w("")
        w("| Record type | Count |")
        w("|---|---|")
        for kind, tree in sorted(trees.items(), key=lambda kv: -kv[1].records):
            w(f"| `{kind}` | {tree.records:,} |")
        w("")
        for kind, tree in sorted(trees.items(), key=lambda kv: -kv[1].records):
            w(f"#### `{kind}` — {tree.records:,} records")
            w("")
            if tree.kpaths:
                w("Update paths (incremental format): " + ", ".join(
                    f"`{p}` ×{c}" for p, c in tree.kpaths.most_common(30)))
                w("")
            w("| Field | Types | Seen | Values / range |")
            w("|---|---|---|---|")
            rows = sorted(tree.fields.items())
            for path, f in rows[:MAX_ROWS]:
                types = ", ".join(t for t, _ in f.types.most_common())
                w(f"| `{path}` | {types} | {f.n:,} | {fmt_field(f)} |")
            if len(rows) > MAX_ROWS or tree.dropped:
                w(f"| … | | | {max(len(rows) - MAX_ROWS, 0)} more fields not shown"
                  + (f", {tree.dropped} beyond the {MAX_FIELDS}-field limit" if tree.dropped else "") + " |")
            w("")

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser(description="Read-only scan of GitHub Copilot's local log files.")
    ap.add_argument("--out", default="copilot_log_report.md", help="report file to write")
    ap.add_argument("--root", action="append", default=[],
                    help="extra VS Code User folder to scan (repeatable)")
    args = ap.parse_args()

    rep = Report()
    rep.roots = user_roots(args.root)
    print(f"Scanning {len(rep.roots)} VS Code folder(s) and ~/.copilot ...")
    for label, root in rep.roots:
        scan_user_root(rep, label, root)
    scan_copilot_home(rep)
    scan_extensions(rep)
    for key, label, _ in STORES:
        if rep.files[key]:
            print(f"  {label}: {len(rep.files[key])} files")
            analyse_store(rep, key)
    with tempfile.TemporaryDirectory() as tmpdir:
        for p in rep.store_dbs:
            analyse_store_db(rep, p, tmpdir)
        for p in rep.state_dbs:
            analyse_state_db(rep, p, tmpdir)

    write_report(rep, args.out)
    print()
    for a, b, _ in verdict_rows(rep):
        print(f"  {b:>3}  {a}")
    print(f"\nReport written to {os.path.abspath(args.out)}")
    print("It holds field names, counts and date ranges only. Please read it before sharing.")


if __name__ == "__main__":
    main()
