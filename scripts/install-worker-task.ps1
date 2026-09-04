<#
.SYNOPSIS
    Register the CHZZK Archive encoder worker as a Scheduled Task.

.DESCRIPTION
    Use this when installing a Windows service is not an option, or when the
    GPU encoder only works inside an interactive logon session. The task runs
    at logon, restarts on failure and never times out.

    The controller URL and token are stored as per-user environment variables
    so they stay out of the task's command line.

.EXAMPLE
    .\install-worker-task.ps1 -Server https://archive.example -Token SECRET
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Token,
    [ValidateSet("auto", "hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_vaapi", "libx265")]
    [string]$Encoder = "auto",
    [string]$WorkerExe = "dist\worker\archiver-worker.exe",
    [string]$TaskName = "CHZZK Archive Encoder Worker"
)

$ErrorActionPreference = "Stop"

$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$workerPath = Join-Path $workspace $WorkerExe
if (-not (Test-Path -LiteralPath $workerPath)) {
    throw "Worker binary not found at '$workerPath'. Run scripts\build-worker.ps1 first."
}
$exe = (Resolve-Path -LiteralPath $workerPath).Path

# Passing the token as an argument would expose it in the process list, so the
# worker reads both values from the environment instead.
[Environment]::SetEnvironmentVariable("ARCHIVER_WORKER_SERVER", $Server, "User")
[Environment]::SetEnvironmentVariable("ARCHIVER_WORKER_TOKEN", $Token, "User")
[Environment]::SetEnvironmentVariable("ARCHIVER_ENCODING_VIDEO_ENCODER", $Encoder, "User")

$action = New-ScheduledTaskAction -Execute $exe -WorkingDirectory (Split-Path -Parent $exe)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Scheduled task installed and started: $TaskName"
Write-Host "  binary : $exe"
Write-Host "Environment variables were set for user '$env:USERNAME' only."
Write-Host "Encoder: $Encoder"
Write-Host "Remove with: scripts\uninstall-worker.ps1 -Task"
