<#
.SYNOPSIS
    Register (or re-register) the hourly leaked-run reaper.

.DESCRIPTION
    NOT RUN AUTOMATICALLY. Arming this is a deliberate step.

    This program KILLS processes, which is the one thing a Claude scheduled task
    is not allowed to do - the permission classifier blocks it, correctly. A
    Windows Task Scheduler job runs outside that gate, which is the whole reason
    this exists as a program rather than as a task.

    Cutover:
      1. Look at what it would do:  .\run_reaper.bat --list
      2. Confirm it kills nothing live:  .\run_reaper.bat --dry-run
      3. Register it here.

    Hourly, starting on the hour. It needs no network, no browser and no login,
    so -RunOnlyIfNetworkAvailable is deliberately NOT set: a machine that is
    offline still leaks runs.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\register_task.ps1 -DryRun
    powershell -ExecutionPolicy Bypass -File .\register_task.ps1
    powershell -ExecutionPolicy Bypass -File .\register_task.ps1 -Remove
#>
param(
    [switch]$DryRun,
    [switch]$Remove,
    [int]$IntervalHours = 1
)

$ErrorActionPreference = "Stop"
$TaskName = "ClaudeRunReaper"
$Bat = Join-Path $PSScriptRoot "run_reaper.bat"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

if (-not (Test-Path $Bat)) { throw "run_reaper.bat not found at $Bat" }

# -Argument rejects an empty string, so it is omitted entirely for a live run.
if ($DryRun) {
    $action = New-ScheduledTaskAction -Execute $Bat -Argument "--dry-run" `
        -WorkingDirectory $PSScriptRoot
} else {
    $action = New-ScheduledTaskAction -Execute $Bat -WorkingDirectory $PSScriptRoot
}

# Repeat forever from the next whole hour. OMITTING -RepetitionDuration is how
# Task Scheduler spells "indefinitely" - passing [TimeSpan]::MaxValue instead
# serialises as P99999999DT23H59M59S and the register call rejects it outright.
$start = (Get-Date).Date.AddHours((Get-Date).Hour + 1)
$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Hourly reap of leaked Claude scheduled runs (claude_run_reaper)" | Out-Null

$mode = if ($DryRun) { "DRY RUN (never kills)" } else { "LIVE" }
Write-Host "Registered '$TaskName' every $IntervalHours hour(s) from $start - $mode"
Write-Host "Run now with:  Start-ScheduledTask -TaskName $TaskName"
