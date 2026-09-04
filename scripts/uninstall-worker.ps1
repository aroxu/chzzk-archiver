<#
.SYNOPSIS
    Remove the CHZZK Archive encoder worker from this machine.

.DESCRIPTION
    Stops and removes the Windows service, the Scheduled Task, or both, and
    clears the stored controller credentials. Recorded media is never touched;
    only the worker installation is removed.

.EXAMPLE
    .\uninstall-worker.ps1 -Service
    .\uninstall-worker.ps1 -Task
    .\uninstall-worker.ps1 -Service -Task -RemoveFiles
#>
[CmdletBinding()]
param(
    [switch]$Service,
    [switch]$Task,
    [switch]$RemoveFiles,
    [string]$InstallDirectory = "$env:ProgramData\CHZZKArchiveWorker",
    [string]$TaskName = "CHZZK Archive Encoder Worker"
)

$ErrorActionPreference = "Stop"

if (-not $Service -and -not $Task) {
    throw "Specify -Service, -Task, or both."
}

if ($Service) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Removing a Windows service requires an elevated PowerShell session."
    }
    $serviceExe = Join-Path $InstallDirectory "archiver-worker-service.exe"
    if (Test-Path -LiteralPath $serviceExe) {
        & $serviceExe stop 2>&1 | Out-Null
        & $serviceExe uninstall 2>&1 | Out-Null
        Write-Host "Removed Windows service: CHZZK Archive Encoder Worker"
    } else {
        Write-Host "No service wrapper found in '$InstallDirectory'."
    }
    [Environment]::SetEnvironmentVariable("ARCHIVER_WORKER_SERVER", $null, "Machine")
    [Environment]::SetEnvironmentVariable("ARCHIVER_WORKER_TOKEN", $null, "Machine")
}

if ($Task) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task: $TaskName"
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    [Environment]::SetEnvironmentVariable("ARCHIVER_WORKER_SERVER", $null, "User")
    [Environment]::SetEnvironmentVariable("ARCHIVER_WORKER_TOKEN", $null, "User")
}

if ($RemoveFiles) {
    # Guard against a caller pointing -InstallDirectory at something broad.
    $resolved = (Resolve-Path -LiteralPath $InstallDirectory -ErrorAction SilentlyContinue)
    if (-not $resolved) {
        Write-Host "Nothing to delete at '$InstallDirectory'."
    } elseif ($resolved.Path.TrimEnd('\') -notmatch 'CHZZKArchiveWorker$') {
        throw "Refusing to delete '$($resolved.Path)': not a worker install directory."
    } else {
        Remove-Item -LiteralPath $resolved.Path -Recurse -Force
        Write-Host "Deleted $($resolved.Path)"
    }
}

