# Restart AI Fluency Insights on a fresh process so it reloads the latest
# code, then open it in your browser. Invoked by Restart-Insights.bat.
$ErrorActionPreference = 'SilentlyContinue'
$port   = 8788   # the original Claude-only dashboard keeps 8787
$script = Join-Path $PSScriptRoot 'ai_fluency_insights.py'

Write-Host ''
Write-Host '  AI Fluency Insights - restart' -ForegroundColor Cyan
Write-Host '  -----------------------------'

if (-not (Test-Path $script)) {
    Write-Host "  ERROR: ai_fluency_insights.py not found next to this script." -ForegroundColor Red
    Start-Sleep -Seconds 4
    exit 1
}

# 1) Stop this copy only - matched by this script's full path AND by the port.
#    The original Claude-only dashboard runs a file with the same name from another
#    folder (on port 8787), so a name match alone would stop it too.
Write-Host '  Stopping running instances...'
$killed = 0
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match '^pythonw?[\d.]*\.exe$' -and $_.CommandLine -and $_.CommandLine.ToLower().Contains($script.ToLower()) } |
    ForEach-Object { if (Stop-Process -Id $_.ProcessId -Force -PassThru) { $killed++ } }
foreach ($procId in ((Get-NetTCPConnection -LocalPort $port -State Listen).OwningProcess | Select-Object -Unique)) {
    if (Stop-Process -Id $procId -Force -PassThru) { $killed++ }
}
Write-Host "  Stopped $killed instance(s)."
Start-Sleep -Milliseconds 700

# 2) Launch one fresh, windowless instance (survives after this window closes).
Write-Host '  Starting fresh instance...'
$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) { $pyw = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $pyw) {
    Write-Host '  ERROR: Python not found on PATH.' -ForegroundColor Red
    Start-Sleep -Seconds 4
    exit 1
}
Start-Process -FilePath $pyw -ArgumentList ('"{0}" --no-browser --port {1}' -f $script, $port) -WindowStyle Hidden
Start-Sleep -Seconds 2

# 3) Confirm it is up, then open the browser.
try {
    $r = Invoke-WebRequest "http://localhost:$port/" -UseBasicParsing -TimeoutSec 6
    if ($r.StatusCode -eq 200) {
        Write-Host "  Dashboard is up at http://localhost:$port/" -ForegroundColor Green
    }
} catch {
    Write-Host "  Started, but couldn't confirm it yet - opening anyway." -ForegroundColor Yellow
}
Start-Process "http://localhost:$port/"
Start-Sleep -Seconds 1
