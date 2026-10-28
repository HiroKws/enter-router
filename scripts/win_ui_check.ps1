# win_ui_check.ps1 - Windows-side GUI check for the tray + settings window, run FROM WSL:
#   /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -ExecutionPolicy Bypass \
#     '<repo>\scripts\win_ui_check.ps1'
# Keep this file ASCII-only: PS 5.1 reads BOM-less UTF-8 .ps1 as CP932 (measured bug).
param(
    [Parameter(Position = 0)][string]$Repo = "",
    [Parameter(Position = 1)][string]$OutDir = "",
    [string]$Python = "python.exe"
)
if (-not $Repo) { $Repo = Split-Path -Parent $PSScriptRoot }
if (-not $OutDir) { $OutDir = Join-Path $Repo "ui_check_out" }
$ErrorActionPreference = "Continue"
$py = $Python
$env:PYTHONPATH = "$Repo\src"
Write-Host "repo=$Repo"
Write-Host "out=$OutDir"
Write-Host "python=$(& $py --version 2>&1)"

& $py "$Repo\scripts\win_ui_check.py" $OutDir 2>&1 | ForEach-Object { Write-Host "UI $_" }
$code = $LASTEXITCODE
Write-Host "UI_CHECK_EXIT $code"
exit $code
