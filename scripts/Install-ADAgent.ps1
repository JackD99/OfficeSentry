<#
.SYNOPSIS
    Installs the Office Sentry AD Agent as a Windows Scheduled Task.

.DESCRIPTION
    Creates a scheduled task that runs AD-Agent.ps1 every 60 seconds.
    The task runs under the SYSTEM account with highest privileges.

.NOTES
    Must be run as Administrator.
#>

#requires -Version 5.1
#requires -RunAsAdministrator

[CmdletBinding()]
param(
    [string]$InstallDir = $PSScriptRoot,
    [int]$IntervalSeconds = 60,
    [string]$TaskName = "OfficeSentry-AD-Agent"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$agentScript = Join-Path $InstallDir "AD-Agent.ps1"
if (-not (Test-Path $agentScript)) {
    Write-Error "AD-Agent.ps1 not found in $InstallDir"
    exit 1
}

# Check for existing task
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "$(Get-Date) INFO: Removing existing scheduled task '$TaskName'"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Build the scheduled task
$pwshPath = if (Get-Command pwsh -ErrorAction SilentlyContinue) {
    (Get-Command pwsh).Source
} else {
    (Get-Command powershell).Source
}

$action = New-ScheduledTaskAction `
    -Execute $pwshPath `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$agentScript`" -ConfigDir `"$InstallDir`" -RunOnce" `
    -WorkingDirectory $InstallDir

# Run every N seconds via repetition
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Seconds $IntervalSeconds) `
    -RepetitionDuration (New-TimeSpan -Days 9999)

$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Office Sentry AD Agent — polls portal for AD commands"

Write-Host "$(Get-Date) INFO: Scheduled task '$TaskName' created successfully"
Write-Host "$(Get-Date) INFO: Agent will poll every $IntervalSeconds seconds"
Write-Host "$(Get-Date) INFO: Run 'schtasks /run /tn $TaskName' to start immediately"

# Start the task now
Start-ScheduledTask -TaskName $TaskName
Write-Host "$(Get-Date) INFO: Task started"
