"""Test helpers that write synthetic Copilot files into a temp tree.

Every builder writes UTF-8 and returns the path (or folder) it wrote. The layout copies
what VS Code and the Copilot CLI really create; the values are made up. Nothing here is
imported by the dashboard itself.
"""
import copy
import json
import os
import sqlite3
from urllib.parse import quote

CHAT_EXT = "GitHub.copilot-chat"        # workspaceStorage/<hash>/<this>/transcripts, debug-logs
GLOBAL_EXT = "github.copilot-chat"      # globalStorage/<this>/session-store.db


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def _write_json(path, obj):
    return _write(path, json.dumps(obj, ensure_ascii=False))


def _write_jsonl(path, rows):
    return _write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def make_user_root(tmp, variant="Code"):
    """<tmp>/<variant>/User, like %APPDATA%/Code/User."""
    path = os.path.join(tmp, variant, "User")
    os.makedirs(path, exist_ok=True)
    return path


def add_workspace(user_root, ws_hash, folder):
    """workspaceStorage/<hash>/ with a workspace.json pointing at `folder`. Returns that folder."""
    ws_dir = os.path.join(user_root, "workspaceStorage", ws_hash)
    uri = "file:///" + quote(folder.replace("\\", "/").lstrip("/"), safe="/")
    _write_json(os.path.join(ws_dir, "workspace.json"), {"folder": uri})
    return ws_dir


def add_chat_jsonl(ws_dir, sid, lines):
    """chatSessions/<sid>.jsonl: one kind 0/1/2/3 mutation per line."""
    return _write_jsonl(os.path.join(ws_dir, "chatSessions", sid + ".jsonl"), lines)


def add_chat_json(ws_dir, sid, obj):
    """chatSessions/<sid>.json: the older whole-file format."""
    return _write_json(os.path.join(ws_dir, "chatSessions", sid + ".json"), obj)


def add_empty_window_chat(user_root, sid, obj):
    """globalStorage/emptyWindowChatSessions/<sid>.json (a list is written as .jsonl lines)."""
    folder = os.path.join(user_root, "globalStorage", "emptyWindowChatSessions")
    if isinstance(obj, list):
        return _write_jsonl(os.path.join(folder, sid + ".jsonl"), obj)
    return _write_json(os.path.join(folder, sid + ".json"), obj)


def add_transcript(ws_dir, sid, events):
    return _write_jsonl(os.path.join(ws_dir, CHAT_EXT, "transcripts", sid + ".jsonl"), events)


def add_debug_log(ws_dir, sid, events):
    return _write_jsonl(os.path.join(ws_dir, CHAT_EXT, "debug-logs", sid, "main.jsonl"), events)


def add_cli_session(copilot_home, sid, events):
    return _write_jsonl(os.path.join(copilot_home, "session-state", sid, "events.jsonl"), events)


def add_session_store(user_root, rows):
    """globalStorage/github.copilot-chat/session-store.db, in WAL mode like the real one."""
    path = os.path.join(user_root, "globalStorage", GLOBAL_EXT, "session-store.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cols = ("id", "cwd", "repository", "branch", "summary", "created_at", "updated_at")
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS sessions (%s)" % ", ".join(cols))
        db.executemany("INSERT INTO sessions VALUES (%s)" % ", ".join("?" * len(cols)),
                       [tuple(r.get(c) for c in cols) for r in rows])
        db.commit()
    finally:
        db.close()
    return path


def write_settings(user_root, obj_or_text):
    """<User>/settings.json from a dict, or from raw text (to test JSONC comments)."""
    if isinstance(obj_or_text, str):
        return _write(os.path.join(user_root, "settings.json"), obj_or_text)
    return _write(os.path.join(user_root, "settings.json"), json.dumps(obj_or_text, indent=2))


def add_otel(path, spans):
    """An OpenTelemetry file-exporter output: one span (or batch) per line."""
    return _write_jsonl(path, spans)


def add_raw(path, text):
    """A file written exactly as given (a BOM, a half-written last line, ...). Returns the path."""
    return _write(path, text)


def llm_request(span_id, ts_ms, input_tokens=0, output_tokens=0, cached=0, model="gpt-4.1", nano_aiu=None):
    """One debug-log `llm_request` line. inputTokens includes the cached tokens, like the real log."""
    attrs = {"inputTokens": input_tokens, "outputTokens": output_tokens, "cachedTokens": cached, "model": model}
    if nano_aiu is not None:
        attrs["copilotUsageNanoAiu"] = nano_aiu
    return {"type": "llm_request", "spanId": span_id, "ts": ts_ms, "attrs": attrs}


def otel_span(conversation, span_id, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0,
              nano_aiu=None, model="gpt-4.1", start_ns=1759320000000000000, as_list=False, name=None):
    """One OpenTelemetry `chat` span. input_tokens includes the cache tokens, like gen_ai.usage.input_tokens.

    The attributes are a plain dict, or with as_list=True the OTLP list of {key, value: {intValue |
    stringValue}} pairs."""
    attrs = {"gen_ai.operation.name": "chat", "gen_ai.conversation.id": conversation,
             "gen_ai.request.model": model, "gen_ai.usage.input_tokens": input_tokens,
             "gen_ai.usage.output_tokens": output_tokens,
             "gen_ai.usage.cache_read.input_tokens": cache_read,
             "gen_ai.usage.cache_creation.input_tokens": cache_write}
    if nano_aiu is not None:
        attrs["github.copilot.nano_aiu"] = nano_aiu
    if as_list:
        attrs = [{"key": k, "value": {"stringValue": v} if isinstance(v, str) else {"intValue": v}}
                 for k, v in attrs.items()]
    return {"name": name or "chat " + model, "spanId": span_id, "startTimeUnixNano": start_ns,
            "attributes": attrs}


def chat_request(rid, ts_ms, text, model="copilot/gpt-5.6-luna", rounds=(), prompt=0, completion=0,
                 credits=None, details=None, elapsed=0, state=None, completed=None, resolved=None,
                 timings=None, token_details=None):
    """One entry of a VS Code chat session's requests[], in the shape the real files use.

    `rounds` are toolCallRounds dicts (timestamp, modelId, toolCalls[{id, name, arguments}]),
    copied as given. Keys the real file leaves out until a request finishes are left out here
    when their value is 0 or None, so a test can add them (metadata.promptTokens, result.timings,
    metadata.toolCallResults, ...) to a returned request to exercise a fallback.

    The fields newer VS Code versions add are left out unless given: `state` and `completed` make
    modelState {value, completedAt} (0 Pending, 1 Complete, 2 Cancelled, 3 Failed, 4 NeedsInput; a
    `completed` of None leaves completedAt out), `resolved` is result.metadata.resolvedModel,
    `timings` is result.timings and `token_details` is promptTokenDetails. realistic_chat_request()
    sets all of them the way the office logs have them."""
    req = {"requestId": rid, "timestamp": ts_ms, "modelId": model, "message": {"text": text},
           "result": {"metadata": {"toolCallRounds": copy.deepcopy(list(rounds))}}}
    if prompt:
        req["promptTokens"] = prompt
    if completion:
        req["completionTokens"] = completion
    if credits is not None:
        req["copilotCredits"] = credits
    if details is not None:
        req["result"]["details"] = details
    if elapsed:
        req["elapsedMs"] = elapsed
    if state is not None:
        req["modelState"] = {"value": state}
        if completed is not None:
            req["modelState"]["completedAt"] = completed
    if resolved is not None:
        req["result"]["metadata"]["resolvedModel"] = resolved
    if timings is not None:
        req["result"]["timings"] = copy.deepcopy(timings)
    if token_details is not None:
        req["promptTokenDetails"] = copy.deepcopy(token_details)
    return req


def realistic_chat_request(rid, ts_ms, text, state=1, elapsed=4000, model="copilot/auto",
                           resolved="claude-haiku-4-5-20251001", details="Claude Haiku 4.5", rounds=(),
                           prompt=1200, completion=300, credits=None):
    """A chat request as the office logs have it: modelId copilot/auto, modelState {value, completedAt}
    (finished `elapsed` ms after it started), promptTokenDetails, result.details without credits, a dated
    result.metadata.resolvedModel and result.timings {firstProgress, totalElapsed}."""
    return chat_request(
        rid, ts_ms, text, model=model, rounds=rounds, prompt=prompt, completion=completion, credits=credits,
        details=details, elapsed=elapsed, state=state, completed=ts_ms + elapsed, resolved=resolved,
        timings={"firstProgress": 800, "totalElapsed": elapsed},
        token_details=[{"category": "System", "label": "System Instructions", "percentageOfPrompt": 20},
                       {"category": "User Context", "label": "Tool Definitions", "percentageOfPrompt": 80}])


def add_debug_session(ws_dir, sid, calls, extra_files=True):
    """debug-logs/<sid>/ the way VS Code writes it with file logging on: main.jsonl holds a session_start,
    the turn, tool and message lines that carry no token counts, and the `calls` (llm_request lines, built
    with llm_request()). With extra_files, two more one-line files sit beside it that are not event logs:
    a model list (a JSON array) and a prompt file (an object with only `content`). Returns the folder."""
    folder = os.path.join(ws_dir, CHAT_EXT, "debug-logs", sid)
    first = calls[0]["ts"] if calls else 0
    last = calls[-1]["ts"] if calls else 0
    lines = [{"type": "session_start", "ts": first - 500, "v": 1,
              "attrs": {"copilotVersion": "0.67.0", "vscodeVersion": "1.139.0"}},
             {"type": "user_message", "ts": first - 400, "attrs": {"content": "prompt text"}},
             {"type": "turn_start", "ts": first - 300, "attrs": {"turn": 0}},
             {"type": "discovery", "ts": first - 250, "attrs": {"category": "instructions"}},
             {"type": "generic", "ts": first - 200, "attrs": {"name": "note"}}]
    lines += calls
    lines += [{"type": "tool_call", "ts": last + 100, "attrs": {"name": "read_file", "duration": 12}},
              {"type": "agent_response", "ts": last + 200, "attrs": {"finishReason": "stop"}},
              {"type": "turn_end", "ts": last + 300, "attrs": {"turn": 0}}]
    _write_jsonl(os.path.join(folder, "main.jsonl"), lines)
    if extra_files:
        _write(os.path.join(folder, "models.jsonl"),
               json.dumps([{"id": "gpt-5.5", "name": "GPT-5.5"}, {"id": "claude-haiku-4.5"}]) + "\n")
        _write(os.path.join(folder, "prompt_0.jsonl"), json.dumps({"content": "system prompt text"}) + "\n")
    return folder


def cli_usage_record(sec, call_id, model, inp, out, cache_read=0, cache_write=0, nano=None):
    """A Copilot CLI session.usage_record with the fields the real log has. `inp` includes the cache tokens."""
    usage = {"apiCallId": call_id, "model": model, "inputTokens": inp, "outputTokens": out,
             "cacheReadTokens": cache_read, "cacheWriteTokens": cache_write, "reasoningTokens": 0,
             "initiator": "user", "finishReason": "stop"}
    if nano is not None:
        fresh = max(0, inp - cache_read - cache_write)
        usage["copilotUsage"] = {"totalNanoAiu": nano, "tokenDetails": [
            {"tokenType": "input", "tokenCount": fresh, "batchSize": 1, "costPerBatch": 1},
            {"tokenType": "output", "tokenCount": out, "batchSize": 1, "costPerBatch": 1},
            {"tokenType": "cache_read", "tokenCount": cache_read, "batchSize": 1, "costPerBatch": 1},
            {"tokenType": "cache_write", "tokenCount": cache_write, "batchSize": 1, "costPerBatch": 1}]}
    return {"type": "session.usage_record", "id": "rec-" + call_id, "timestamp": "2026-10-24T10:00:%02dZ" % sec,
            "data": {"usage": usage, "accounting": {"sequence": sec, "sourceSessionId": "k1", "usageId": call_id}}}


ROUTINE_AGENT_TYPES = (
    "permission.requested", "permission.completed", "skill.invoked", "skill.context_delivered_ref",
    "subagent.selected", "model.turn_started", "model.model_call_started", "model.model_call_success",
    "model.message", "model.response", "model.turn_ended", "model.messages_snapshot",
    "model.captured_assignment_context", "session.info", "session.model_change", "session.binary_asset")


def cli_events(model="gpt-5.5-2026-04-23", routine=True):
    """A Copilot CLI session as the office logs have it: session.start with context.cwd only, one prompt, one
    usage record (inputTokens 5000 including 3000 cached, 400 out, 2 credits), a usage_checkpoint that holds
    the same totals twice (accountingSnapshot.modelMetrics and ...agentMetrics.main.modelMetrics), and, with
    `routine`, one record of each type in ROUTINE_AGENT_TYPES, which the reader must not report as unknown."""
    def rec(rtype, sec, data=None):
        return {"type": rtype, "id": "e-%s-%d" % (rtype, sec), "timestamp": "2026-10-24T10:00:%02dZ" % sec,
                "data": data or {}}
    metrics = {"usage": {"inputTokens": 5000, "outputTokens": 400, "cacheReadTokens": 3000, "cacheWriteTokens": 0},
               "totalNanoAiu": 2e9}
    events = [rec("session.start", 0, {"sessionId": "k1", "copilotVersion": "1.0.100-1",
                                       "context": {"cwd": "/work/cli"}}),
              rec("user.message", 1, {"content": "run the tests"})]
    if routine:
        events += [rec(t, 2) for t in ROUTINE_AGENT_TYPES]
    events += [cli_usage_record(3, "api1", model, 5000, 400, cache_read=3000, nano=2e9),
               rec("assistant.turn_end", 4, {"turnId": "0"}),
               rec("session.usage_checkpoint", 5, {"accountingSnapshot": {
                   "modelMetrics": {model: metrics},
                   "agentMetrics": {"main": {"modelMetrics": {model: metrics}}}}})]
    return events
