# run_bot.ps1 — PowerShell wrapper with a real single-instance guard.
# Replaces run_bot.bat whose :loop accumulated zombie wrappers whenever the
# python child was killed alone (incident 2026-09-24). PowerShell's try/finally
# responds to Ctrl-C correctly and the guard checks for a LIVE bot python
# (self-healing: no stale PID files).
#
# Usage:  powershell -ExecutionPolicy Bypass -File run_bot.ps1
# Stop:   Ctrl-C  (wrapper AND python die together)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Test-BotAlive {
    return [bool](Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*mt5-ftmo*main.py*' -or
                       ($_.CommandLine -like '*main.py*' -and $_.ExecutablePath -like "*$PSScriptRoot*") })
}

if (Test-BotAlive) {
    Write-Host "Another bot instance is already running. Exiting." -ForegroundColor Yellow
    exit 1
}

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "venv python not found: $py" -ForegroundColor Red; exit 1 }

Write-Host "Starting MT5 FTMO Trading Bot (single-instance guarded)..."
Write-Host "Press Ctrl+C to stop."
while ($true) {
    & $py (Join-Path $PSScriptRoot "main.py")
    $code = $LASTEXITCODE
    if ($code -eq 0) { break }   # clean exit (KeyboardInterrupt) — don't loop
    Write-Host ""
    Write-Host "Bot exited (code $code). Restarting in 10 seconds... (Ctrl+C to abort)"
    Start-Sleep -Seconds 10
}
