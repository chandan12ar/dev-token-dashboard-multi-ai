# Setup Guide

Get AI Fluency Insights running on your machine in under two minutes.

## Prerequisites

- **Python 3.8+** — check with `python --version`. Nothing else: the
  dashboard uses only the standard library, no `pip install` needed.
- **Claude Code** and/or **GitHub Copilot** installed and used at least once
  (the dashboard reads the session logs they write locally; with no logs there
  is nothing to show). You need only one of them; the top-bar tool switch
  appears when the dashboard finds both. Copilot means VS Code's Copilot Chat or
  the Copilot CLI.
- Any OS — Windows, macOS, or Linux. Auto-start instructions differ per OS
  (see below); everything else is identical.

## 1. Get the code

```
git clone https://github.com/chandan12ar/dev-token-dashboard-multi-ai.git
cd dev-token-dashboard-multi-ai
```

(or just download `ai_fluency_insights.py`, `copilot_source.py` and `chart.umd.min.js`
into one folder — those three files are the entire app.)

## 2. Run it

```
python ai_fluency_insights.py
```

That's it. Your browser opens http://localhost:8787 with your stats. The
page auto-refreshes every 15 seconds, and the parser re-reads only log
files that changed, so it stays fast even with months of history.

The dashboard is 100% local: it binds to `127.0.0.1` only, makes zero
external requests (Chart.js is bundled), and never calls any API — it
costs **zero tokens** to run.

### Where it finds your logs

Claude Code writes session logs to:

| OS | Default location |
|---|---|
| Windows | `C:\Users\<you>\.claude\projects\` |
| macOS / Linux | `~/.claude/projects/` |

The script auto-detects this (it also honors the `CLAUDE_CONFIG_DIR`
environment variable). If your logs live somewhere unusual:

```
python ai_fluency_insights.py --dir "D:\path\to\.claude\projects"
```

GitHub Copilot's logs are found automatically too:

| What | Default location |
|---|---|
| VS Code chat history, transcripts, debug logs (Windows) | `%APPDATA%\Code\User\workspaceStorage\` and `...\globalStorage\` |
| VS Code (macOS) | `~/Library/Application Support/Code/User/` |
| VS Code (Linux) | `~/.config/Code/User/` |
| Copilot CLI | `~/.copilot/session-state/` |

VS Code Insiders, VSCodium, named profiles and `~/.vscode-server` are included.
If VS Code keeps its user folder somewhere unusual, add it (repeat the flag for
more than one):

```
python ai_fluency_insights.py --copilot-root "D:\path\to\Code\User"
```

Copilot's files are only ever opened read-only.

### Options

```
python ai_fluency_insights.py --port 9000      # different port
python ai_fluency_insights.py --no-browser     # don't auto-open the browser
python ai_fluency_insights.py --dump           # print raw stats JSON and exit
python ai_fluency_insights.py --dump --source copilot   # print one tool's stats (claude, copilot or all)
python ai_fluency_insights.py --no-copilot     # ignore GitHub Copilot logs (the tool switch hides)
python ai_fluency_insights.py --copilot-root PATH       # extra VS Code user folder for Copilot logs
```

## 3. Optional: start automatically at logon

### Windows — option A: Task Scheduler (needs an Administrator terminal)

```
python ai_fluency_insights.py --install-startup     # install
python ai_fluency_insights.py --uninstall-startup   # remove
```

### Windows — option B: Startup folder (no admin needed)

Create a shortcut in your Startup folder pointing at `pythonw.exe` (the
console-less Python), with the script path as argument. In PowerShell:

```powershell
$pyw = (Get-Command pythonw.exe).Source
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$([Environment]::GetFolderPath('Startup'))\AI Fluency Insights.lnk")
$lnk.TargetPath = $pyw
$lnk.Arguments = '"C:\full\path\to\ai_fluency_insights.py" --no-browser'
$lnk.Save()
```

Delete the shortcut (`Win+R` → `shell:startup`) to remove auto-start.

### macOS

```
crontab -e
# add:
@reboot /usr/bin/python3 /full/path/to/ai_fluency_insights.py --no-browser
```

(or create a `launchd` user agent if you prefer.)

### Linux (systemd)

```
# ~/.config/systemd/user/ai-fluency-insights.service
[Unit]
Description=AI Fluency Insights

[Service]
ExecStart=/usr/bin/python3 /full/path/to/ai_fluency_insights.py --no-browser

[Install]
WantedBy=default.target
```

```
systemctl --user enable --now ai-fluency-insights
```

## 3b. Optional: accurate plan-usage numbers and toast notifications

By default the "Plan usage limits" card on the dashboard shows a rough
local estimate. To get Anthropic's real 5-hour/7-day numbers (and, on
Windows, toast pop-ups as you cross usage milestones), run:

```
python ai_fluency_insights.py --setup-notifications
```

It wires up a Claude Code `statusLine` hook for you and shows exactly
what it's about to change before writing anything. Preview without
changing anything with `--setup-notifications --dry-run`. If you already
have a custom `statusLine` script, it won't touch it — it'll tell you
what to add instead. See
[docs/DASHBOARD_GUIDE.md#plan-usage-limits](docs/DASHBOARD_GUIDE.md#plan-usage-limits)
for how this works and why the number can lag by a few minutes when idle.

Toast notifications are Windows-only for now; on macOS/Linux this sets
up the accurate numbers only.

## 3c. Optional: GitHub Copilot

Reading Copilot needs no setup: open the dashboard and use the switch in the
top bar. Out of the box Copilot's token counts come from chat history, so they
are **approximate** and marked "approx." (an agent turn may log only its last
model call). Credits per request are as Copilot logs them. The Copilot CLI is
always exact.

### Exact tokens: `--setup-copilot`

```
python ai_fluency_insights.py --setup-copilot
```

This turns on VS Code Copilot's official OpenTelemetry file export, so **new**
chats log exact token counts. It never changes anything until you say yes:

- it shows the exact settings it will add or change in your VS Code user
  `settings.json` and asks `[y/N]`;
- it backs the file up first, as `settings.json.bak-aifi-<date-time>` next to it;
- if your `settings.json` has comments or trailing commas (VS Code's JSONC), it
  **does not edit it**; it prints the three lines to paste yourself;
- if you already have your own `otel.outfile` or a different exporter, it keeps
  yours and tells you what it left alone;
- `--dry-run` shows what would change and writes nothing;
- `--undo` removes the three `github.copilot.chat.otel.*` settings it manages (`otel.enabled`, `otel.exporterType` set to `"file"`, and the outfile), but only the outfile if it is the path this tool sets; your own outfile is kept, and if your exporter is anything other than `"file"` nothing is changed. Every other setting stays as it is.

After it runs, reload VS Code windows (Command Palette, "Developer: Reload
Window"). Chats after that get exact tokens; older history stays approximate.
The Copilot **Data Health** tab then shows "Exact tokens". The OpenTelemetry
export records no prompt text by default.

```
python ai_fluency_insights.py --setup-copilot --dry-run    # preview only
python ai_fluency_insights.py --setup-copilot --undo       # remove it again
```

*Another way:* the VS Code setting
`github.copilot.chat.agentDebugLog.fileLogging.enabled` (Settings, "Agent Debug
Log > File Logging") also gives exact tokens and credits per call. It stores your
**full prompts** in local log files, which the OpenTelemetry export does not.

### When Copilot data looks wrong: `tools/copilot_log_check.py`

Copilot's log formats are not documented and change between releases. If the
Copilot view is empty or its numbers look off, run the checker. It is
**read-only** and **structure-only**: it looks at which fields exist in your
Copilot files and writes a short Markdown report. The report contains no
prompts and no code, but read it before you share it with anyone.

If you have the repo:

```
python tools/copilot_log_check.py --out copilot_log_report.md
```

Without cloning anything, on Windows (PowerShell). This saves the report to your
real Desktop, even when OneDrive has moved it:

```powershell
irm https://raw.githubusercontent.com/chandan12ar/dev-token-dashboard-multi-ai/main/tools/copilot_log_check.py -OutFile $env:TEMP\copilot_log_check.py; python $env:TEMP\copilot_log_check.py --out "$([Environment]::GetFolderPath('Desktop'))\copilot_log_report.md"
```

On macOS or Linux:

```
curl -fsSL https://raw.githubusercontent.com/chandan12ar/dev-token-dashboard-multi-ai/main/tools/copilot_log_check.py -o /tmp/copilot_log_check.py && python3 /tmp/copilot_log_check.py --out ~/Desktop/copilot_log_report.md
```

Add `--root PATH` (repeatable) if your VS Code user folder is not in the usual
place.

## 4. Optional: AI weekly summaries

The Weekly Report tab has an "AI summary" button that turns your week into
a polished standup paragraph by running `claude -p` locally. It only needs
the **Claude Code CLI on your PATH** (if you can type `claude` in a
terminal, it works). This is the single feature that consumes tokens, and
only when you press the button — everything else is free forever.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Log directory not found` | You haven't used Claude Code on this machine yet, or logs are elsewhere — pass `--dir` explicitly. |
| Port already in use | Another instance is running (check http://localhost:8787), or pass `--port 9000`. |
| Page loads but charts are empty | Hard-refresh (`Ctrl+F5`); if you deleted `chart.umd.min.js` the page falls back to the CDN and needs internet once. |
| Task Scheduler install fails | Run the terminal as Administrator, or use the Startup-folder method (option B). |
| A new tab says "Not enough data yet" | Flow needs 5 timed turns and Quality needs 3 sessions where Claude edited files, within the selected date range. Widen the range (30d or 90d). My Adoption needs 3 active days in the last 4 weeks. |
| A metric is missing or shows "not in your logs" | Open the **Data Health** tab. It lists which signals your Claude Code version writes (for example permission decisions or thinking time). Missing signals are shown as missing, never as zero. |
| The tool switch is missing | It only shows when more than one tool has data. Use Claude Code or Copilot on this machine first, check you did not pass `--no-copilot`, or point at VS Code's user folder with `--copilot-root`. "All AI" appears only when both tools have data. |
| Copilot view is empty, or a Copilot card says "Not available for Copilot" | Open the Copilot **Data Health** tab: it says which signals your logs have (older VS Code, for example, does not record stopped requests, and chat history has no exact tokens). If the tab itself looks wrong, run `tools/copilot_log_check.py` (see section 3c) and compare. |
| `--setup-copilot` says it can't edit `settings.json` | Your file has comments or trailing commas. It prints the lines to paste into VS Code's "Preferences: Open User Settings (JSON)". |
| Cost card looks wrong | It's an estimate at public API pricing; edit the `PRICING` table at the top of the script. On a subscription your real marginal cost is $0. |

## What next

- [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) — architecture, where Claude's
  and Copilot's data comes from, every panel explained, and what the Weekly
  Report, My Adoption and Engineering insight tabs do
- [docs/DASHBOARD_GUIDE.md](docs/DASHBOARD_GUIDE.md) — a plain-language tour of
  every tab, including how to read the scores
- [docs/TOKEN_COUNTS.md](docs/TOKEN_COUNTS.md) — why our token totals
  differ from the official Claude Code panel, and why ours are the
  accurate ones
