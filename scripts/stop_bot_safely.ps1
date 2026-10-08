# stop_bot_safely.ps1 — Truly stop the bot (task instance + wrapper loop + python).
#
# Requires ADMINISTRATOR. The MT5FTMO_AutoStart task runs at RunLevel=Highest:
# its python children are invisible to unelevated queries (blank CommandLine)
# and cannot be killed by an unelevated Stop-Process. An unelevated run would
# silently miss them (the exact failure mode of 2026-10-06), so we refuse.
#
# Usage:  Start PowerShell as Administrator, then:
#         powershell -ExecutionPolicy Bypass -File scripts\stop_bot_safely.ps1
# Idempotent: safe to run repeatedly.

$ErrorActionPreference = "Continue"
$TaskName = 'MT5FTMO_AutoStart'

# ── 0. Admin gate ────────────────────────────────────────────────────────
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin   = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "ERROR: administrator rights required." -ForegroundColor Red
    Write-Host "The bot's scheduled task runs elevated (RunLevel=Highest); its python" -ForegroundColor Red
    Write-Host "children are invisible and unkillable from a normal shell." -ForegroundColor Red
    Write-Host "Please reopen PowerShell as Administrator and rerun this script." -ForegroundColor Red
    exit 1
}
Write-Host "[0] Running as Administrator ($($identity.Name))" -ForegroundColor Cyan

# ── 1. End + disable the scheduled task ──────────────────────────────────
$taskExists = [bool](schtasks /Query /TN $TaskName 2>$null)
if ($taskExists) {
    # /End makes Task Scheduler kill its own instance tree — reaches the
    # elevated children even before our Stop-Process pass. Refusal when no
    # instance is running is fine.
    $endOut = schtasks /End /TN $TaskName 2>&1
    if ("$endOut" -match 'SUCCESS') { Write-Host "[1] Task instance tree ended." }
    else { Write-Host "[1] No running task instance to end ($($endOut -join ' '))." }
    $disOut = schtasks /Change /TN $TaskName /DISABLE 2>&1
    if ("$disOut" -match 'SUCCESS') { Write-Host "[2] Scheduled task '$TaskName' DISABLED (logon trigger off)." }
    else {
        Write-Host "[2] WARNING: could not disable task: $($disOut -join ' ')" -ForegroundColor Yellow
        Write-Host "    Bot is stopped, but it will relaunch at next logon/reboot." -ForegroundColor Yellow
    }
} else {
    Write-Host "[1] Scheduled task '$TaskName' not found - skipping."
}

# ── Matchers (narrow: Jupyter/pytest/editor pythons are never matched) ───
function Get-BotWrappers {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.Name -in @('powershell.exe', 'pwsh.exe', 'cmd.exe')) -and
        ($_.CommandLine -like '*run_bot.ps1*' -or $_.CommandLine -like '*run_bot.bat*')
    }
}
function Get-BotPythons {
    # Elevated children expose their CommandLine to an ADMIN query, so the
    # match now reaches every bot python on the box.
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -in @('python.exe', 'pythonw.exe') -and
        $_.CommandLine -like '*main.py*' -and
        ($_.CommandLine -like '*mt5-ftmo*' -or $_.CommandLine -like '*.\main.py*')
    }
}

# ── 2. Stop wrapper loops ────────────────────────────────────────────────
$wrappers = @(Get-BotWrappers)
if ($wrappers.Count -gt 0) {
    $wrappers | ForEach-Object {
        Write-Host "[3] Stopping wrapper PID $($_.ProcessId) ($($_.Name))"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else { Write-Host "[3] No wrapper loop processes found." }

Start-Sleep -Seconds 2   # let the wrapper's restart loop die

# ── 3. Stop bot pythons (elevated ones now visible to admin) ────────────
$pythons = @(Get-BotPythons)
if ($pythons.Count -gt 0) {
    $pythons | ForEach-Object {
        Write-Host "[4] Stopping bot python PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else { Write-Host "[4] No bot python processes found." }

Start-Sleep -Seconds 3

# ── 4. Verify: processes + task state + log quiescence ──────────────────
$left = @()
$left += Get-BotWrappers
$left += Get-BotPythons
$taskRunning = $taskExists -and ((schtasks /Query /TN $TaskName /V /FO LIST | Select-String 'Status:\s+Running') -ne $null)
if (-not $taskRunning) { Start-Sleep -Seconds 5 }
$log1 = (Get-Item storage\logs\bot.log -ErrorAction SilentlyContinue).Length
Start-Sleep -Seconds 8
$log2 = (Get-Item storage\logs\bot.log -ErrorAction SilentlyContinue).Length
$logActive = ($log2 -gt $log1)

Write-Host "[5] Verification:"
$clean = $true
if ($taskRunning) { $clean = $false; Write-Host "    FAIL: task instance still Running" -ForegroundColor Red }
if ($logActive)   { $clean = $false; Write-Host "    FAIL: bot.log still growing - an engine is alive" -ForegroundColor Red }
if ($left.Count -gt 0) {
    $clean = $false
    Write-Host "    FAIL: $($left.Count) processes still alive" -ForegroundColor Red
    $left | Select-Object ProcessId, Name,
        @{n = 'CommandLine'; e = { if ($_.CommandLine.Length -gt 120) { $_.CommandLine.Substring(0, 120) + '...' } else { $_.CommandLine } }} |
        Format-Table -AutoSize | Out-String | Write-Host -ForegroundColor Red
}
if ($clean) { Write-Host "    PASS: no wrappers, no pythons, task not running, log quiescent." -ForegroundColor Green }

if ($clean) {
    Write-Host "All stopped. Safe to edit .env or restart." -ForegroundColor Green
} else {
    Write-Host "WARNING: stop incomplete - see FAIL lines above." -ForegroundColor Red
}

# ── 5. Restart hint ──────────────────────────────────────────────────────
Write-Host "To restart: schtasks /Change /TN $TaskName /ENABLE  then  schtasks /Run /TN $TaskName"
