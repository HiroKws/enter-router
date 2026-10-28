# live_smoke.ps1 - short RESIDENT launch of enter-router on real Windows.
# Starts the app, lets it install the LL hook, checks the log, then stops it.
# Run FROM WSL:
#   /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -ExecutionPolicy Bypass '<repo>\scripts\live_smoke.ps1'
# Keep this file ASCII-only (PS 5.1 reads BOM-less UTF-8 as CP932).
param(
    [Parameter(Position = 0)][string]$Repo = "",
    [string]$Python = "python.exe"
)
if (-not $Repo) { $Repo = Split-Path -Parent $PSScriptRoot }
$ErrorActionPreference = "Continue"
$py = $Python
$log = Join-Path $env:APPDATA "enter-router\logs\enter-router.log"

if (Test-Path $log) { Remove-Item $log -Force -ErrorAction SilentlyContinue }
Write-Host "SMOKE starting resident app (config=sample.config.json)"
$mainPy = Join-Path $Repo "src\enter_router\main.py"
$cfgPath = Join-Path $Repo "config\sample.config.json"
$argList = @("`"$mainPy`"", "--config", "`"$cfgPath`"")
$p = Start-Process -FilePath $py -PassThru -WindowStyle Hidden -ArgumentList $argList
Write-Host "SMOKE pid=$($p.Id)"
Start-Sleep -Seconds 3
$alive = -not $p.HasExited
Write-Host "SMOKE still_running=$alive"
if (Test-Path $log) {
    $lines = Get-Content $log
    Write-Host "SMOKE log_lines=$($lines.Count)"
    $lines | Select-String -Pattern "hook installed|started \(config|Traceback|ERROR|CRITICAL" | Select-Object -First 20 | ForEach-Object { Write-Host "SMOKE LOG $_" }
    $errors = ($lines | Select-String -Pattern "Traceback|CRITICAL").Count
    $hooks = ($lines | Select-String -Pattern "hook installed").Count
} else {
    Write-Host "SMOKE FAIL no log file at $log"
    $errors = 1; $hooks = 0
}
if ($alive) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500
$gone = $p.HasExited
Write-Host "SMOKE stopped=$gone"
if ($alive -and $hooks -ge 1 -and $errors -eq 0 -and $gone) {
    Write-Host "LIVE_SMOKE_OK"
    exit 0
}
Write-Host "LIVE_SMOKE_FAIL alive=$alive hooks=$hooks errors=$errors stopped=$gone"
exit 1
