# win_check.ps1 - Windows-side "build" check for enter-router, run FROM WSL:
#   /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -ExecutionPolicy Bypass '<repo>\scripts\win_check.ps1'
# Keep this file ASCII-only: PS 5.1 reads BOM-less UTF-8 .ps1 as CP932 (measured bug).
param(
    [Parameter(Position = 0)][string]$Repo = "",
    [string]$Python = "python.exe"
)
# $PSScriptRoot is not available while the param block is evaluated, so the
# default repository path is resolved here (script = <repo>\scripts\x.ps1).
if (-not $Repo) { $Repo = Split-Path -Parent $PSScriptRoot }
$ErrorActionPreference = "Continue"
$py = $Python
Write-Host "repo=$Repo"
Write-Host "python=$(& $py --version 2>&1)"

# 1) the Python "build": syntax-compile every source and test file
$dirs = @("$Repo\src", "$Repo\tests")
$files = Get-ChildItem -Recurse -Filter *.py $dirs -ErrorAction SilentlyContinue
$fail = 0
foreach ($f in $files) {
    & $py -m py_compile $f.FullName 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "COMPILE_FAIL $($f.FullName)"; $fail = 1 }
    else { Write-Host "COMPILE_OK   $($f.FullName.Substring($Repo.Length + 1))" }
}
if ($fail -ne 0) { Write-Host "WIN_CHECK_FAIL compile"; exit 1 }

# 2) pywin32 reachability (must pass before any platform/ code runs)
& $py -c "import win32api, win32gui, win32con, win32clipboard; print('PYWIN32_OK')"
if ($LASTEXITCODE -ne 0) { Write-Host "WIN_CHECK_FAIL pywin32"; exit 1 }

# 3) core-on-Windows import check (no hook, no side effects)
$env:PYTHONPATH = "$Repo\src"
& $py -c "import enter_router.core.config as c; cfg = c.parse_config_text(open(r'$Repo\config\sample.config.json', encoding='utf-8').read()); print('CORE_IMPORT_OK schema', cfg.schema_version)"
if ($LASTEXITCODE -ne 0) { Write-Host "WIN_CHECK_FAIL core-import"; exit 1 }

# 4) every adapter imports on Windows WITHOUT installing a hook, and the
#    SendInput INPUT layout is really 40 bytes here (error 87 otherwise).
& $py -c "import enter_router.app.main, enter_router.app.dryrun, enter_router.app.sink_runner, enter_router.platform.hook, enter_router.platform.ime, enter_router.platform.inject as inj, enter_router.platform.notify_win, enter_router.platform.textcap_win, enter_router.platform.hermes_cli, enter_router.platform.sinks_win; print('ADAPTER_IMPORT_OK'); print('INPUT_SIZE', inj.input_size()); raise SystemExit(0 if inj.verify_layout() else 3)"
if ($LASTEXITCODE -ne 0) { Write-Host "WIN_CHECK_FAIL adapter-import"; exit 1 }

# 5) --once on Windows: config load + validation + resolution table, no hook.
& $py "$Repo\src\enter_router\main.py" --config "$Repo\config\sample.config.json" --once 2>&1 | Select-String -Pattern "hold_mode: deterministic","warnings: 0","PREVIEW profile=site-a" | ForEach-Object { Write-Host "ONCE $_" }
if ($LASTEXITCODE -ne 0) { Write-Host "WIN_CHECK_FAIL once"; exit 1 }

# 6) hardware self test: clipboard round trip, CallNextHookEx with a 64-bit
#    lparam, a real hook install/pass-through, and the SendInput-vs-hook-callback
#    timing (old shape vs shipped routing). Never swallows a key.
& $py "$Repo\scripts\win_selftest.py" 2>&1 | ForEach-Object { Write-Host "SELFTEST $_" }
if ($LASTEXITCODE -ne 0) { Write-Host "WIN_CHECK_FAIL selftest"; exit 1 }

# 7) live-launch check (manual, QA card): pythonw.exe main.py  -> resident, no console
# Start-Process pythonw -ArgumentList "$Repo\src\enter_router\main.py"
# Get-Process pythonw | Where-Object { $_.Path -like '*\\python.exe' } | Select-Object Id, StartTime

Write-Host "WIN_CHECK_OK"
exit 0
