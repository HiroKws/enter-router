# qa_cleanup.ps1 - remove ONLY enter-router QA leftovers, never the user's own documents.
#
#   powershell.exe /NoProfile /ExecutionPolicy Bypass /File \\wsl.localhost\...\scripts\qa_cleanup.ps1
#   ... append  -Kill  to actually stop them (default prints the list only)
#
# A previous QA run left ~20 Notepad windows behind. This script exists so that
# cannot happen again: it is safe to run at the start of any GUI-touching task
# and it refuses to touch a process whose identity it cannot prove is ours.
#
# Identity rule (both must be true for a kill):
#   notepad : the window title OR the command line contains a QA marker
#             (qa_, enter-router, enter_router, ime_check)
#   python  : the command line contains enter_router / enter-router
# Anything else is listed as SKIP and left alone.
#
# Keep this file ASCII-only: PS 5.1 reads BOM-less UTF-8 .ps1 as CP932.
param(
    [switch]$Kill,
    [string]$Ledger = ""
)
$ErrorActionPreference = "Continue"

$titleMarkers = 'qa_|enter-router|enter_router|ime_check'
$cmdMarkers   = 'qa_|enter-router|enter_router|ime_check'

$targets = @()

# --- 0) pids from a QA ledger (qa/ime_check_pids.json, written by the worker
#        that opened the window) ---------------------------------------------
if ($Ledger -ne "" -and (Test-Path $Ledger)) {
    $entries = Get-Content -Raw -Encoding UTF8 $Ledger | ConvertFrom-Json
    foreach ($entry in @($entries)) {
        $wanted = [int]$entry.pid
        if ($wanted -le 0) { continue }
        $p = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $wanted) -ErrorAction SilentlyContinue
        if ($null -eq $p) {
            Write-Host ("LEDGER pid=" + $wanted + " already gone")
            continue
        }
        if ($p.Name -notin @("notepad.exe", "python.exe", "pythonw.exe", "Notepad.exe")) {
            Write-Host ("SKIP ledger pid=" + $wanted + " name=" + $p.Name + " (not a QA-capable process)")
            continue
        }
        $targets += [pscustomobject]@{ Kind = $p.Name; Id = [int]$p.ProcessId; Detail = ("from ledger " + $Ledger + ", cmd='" + [string]$p.CommandLine + "'") }
    }
}

# --- 1) Notepad windows -----------------------------------------------------
$titles = @{}
foreach ($p in @(Get-Process notepad -ErrorAction SilentlyContinue)) {
    $titles[[int]$p.Id] = [string]$p.MainWindowTitle
}
$np = @(Get-CimInstance Win32_Process -Filter "Name='notepad.exe'" -ErrorAction SilentlyContinue)
foreach ($n in $np) {
    $id = [int]$n.ProcessId
    $title = ""
    if ($titles.ContainsKey($id)) { $title = $titles[$id] }
    $cmd = [string]$n.CommandLine
    if (($title -match $titleMarkers) -or ($cmd -match $cmdMarkers)) {
        $targets += [pscustomobject]@{ Kind = "notepad"; Id = $id; Detail = ("title='" + $title + "' cmd='" + $cmd + "'") }
    } else {
        Write-Host ("SKIP notepad pid=" + $id + " title='" + $title + "' (not provably ours)")
    }
}

# --- 2) resident enter-router python processes ------------------------------
$py = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq "python.exe" -or $_.Name -eq "pythonw.exe" })
foreach ($p in $py) {
    $cmd = [string]$p.CommandLine
    if ($cmd -match 'enter_router|enter-router') {
        $targets += [pscustomobject]@{ Kind = "python"; Id = [int]$p.ProcessId; Detail = ("cmd='" + $cmd + "'") }
    }
}

# --- 3) report / kill -------------------------------------------------------
if ($targets.Count -eq 0) {
    Write-Host "QA_CLEANUP no leftovers"
} else {
    foreach ($t in $targets) {
        Write-Host ("TARGET " + $t.Kind + " pid=" + $t.Id + " " + $t.Detail)
    }
}

$killed = 0
if ($Kill -and $targets.Count -gt 0) {
    foreach ($t in $targets) {
        try {
            Stop-Process -Id $t.Id -Force -ErrorAction Stop
            $killed++
            Write-Host ("KILLED " + $t.Kind + " pid=" + $t.Id)
        } catch {
            Write-Host ("KILL_FAILED pid=" + $t.Id + " " + $_.Exception.Message)
        }
    }
    Start-Sleep -Milliseconds 500
}

# --- 4) verify --------------------------------------------------------------
$npLeft = 0
$titles2 = @{}
foreach ($p in @(Get-Process notepad -ErrorAction SilentlyContinue)) { $titles2[[int]$p.Id] = [string]$p.MainWindowTitle }
foreach ($n in @(Get-CimInstance Win32_Process -Filter "Name='notepad.exe'" -ErrorAction SilentlyContinue)) {
    $id = [int]$n.ProcessId
    $title = ""
    if ($titles2.ContainsKey($id)) { $title = $titles2[$id] }
    if (($title -match $titleMarkers) -or ([string]$n.CommandLine -match $cmdMarkers)) { $npLeft++ }
}
$pyLeft = 0
foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                 Where-Object { $_.Name -eq "python.exe" -or $_.Name -eq "pythonw.exe" })) {
    if ([string]$p.CommandLine -match 'enter_router|enter-router') { $pyLeft++ }
}
$totalNotepad = @(Get-Process notepad -ErrorAction SilentlyContinue).Count

Write-Host ("QA_CLEANUP_OK notepad_target=" + $npLeft + " enter_router=" + $pyLeft + " killed=" + $killed + " notepad_total=" + $totalNotepad)
if ($npLeft -ne 0 -or $pyLeft -ne 0) { exit 1 }
exit 0
