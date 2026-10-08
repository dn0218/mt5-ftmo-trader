# cleanup_zombies.ps1 — kill all run_bot wrappers + bot pythons, then VERIFY.
# See INCIDENT_2026-09-24_MULTIINSTANCE.md. Run: powershell -ExecutionPolicy Bypass -File cleanup_zombies.ps1

Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*run_bot*'
} | ForEach-Object {
    Write-Host "Killing cmd PID $($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 2

Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -like '*main.py*'
} | ForEach-Object {
    Write-Host "Killing python PID $($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Write-Host "Cleanup complete. Verifying..."
Start-Sleep -Seconds 12   # long enough for a surviving wrapper to respawn
$remaining = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*main.py*' }
if ($remaining) {
    Write-Host "WARNING: $($remaining.Count) python processes still alive (a wrapper survived)" -ForegroundColor Red
} else {
    Write-Host "All clear. Start the bot with: powershell -ExecutionPolicy Bypass -File run_bot.ps1" -ForegroundColor Green
}
