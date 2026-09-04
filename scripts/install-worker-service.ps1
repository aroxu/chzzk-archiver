<#
.SYNOPSIS
    Install the CHZZK Archive encoder worker as a Windows service via WinSW.

.DESCRIPTION
    Copies the worker binary and a WinSW wrapper into a machine-wide directory,
    writes the service configuration with the controller URL and token, then
    registers and starts the service. Run from an elevated PowerShell session.

.EXAMPLE
    .\install-worker-service.ps1 -Server https://archive.example -Token SECRET -WinSW C:\tools\WinSW-x64.exe
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Token,
    [Parameter(Mandatory = $true)][string]$WinSW,
    [ValidateSet("auto", "hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_vaapi", "libx265")]
    [string]$Encoder = "auto",
    [string]$WorkerExe = "dist\worker\archiver-worker.exe",
    [string]$InstallDirectory = "$env:ProgramData\CHZZKArchiveWorker",
    [string]$ServiceAccount,
    [securestring]$ServicePassword
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Installing a Windows service requires an elevated PowerShell session."
}

$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$workerPath = Join-Path $workspace $WorkerExe
if (-not (Test-Path -LiteralPath $workerPath)) {
    throw "Worker binary not found at '$workerPath'. Run scripts\build-worker.ps1 first."
}
$worker = (Resolve-Path -LiteralPath $workerPath).Path
$wrapper = (Resolve-Path -LiteralPath $WinSW).Path
$template = Join-Path $workspace "deploy\windows\archiver-worker.xml"

New-Item -ItemType Directory -Force -Path $InstallDirectory | Out-Null
$serviceExe = Join-Path $InstallDirectory "archiver-worker-service.exe"
$serviceXml = Join-Path $InstallDirectory "archiver-worker-service.xml"

# Reinstall cleanly so an upgrade never leaves a stale binary running.
if (Test-Path -LiteralPath $serviceExe) {
    & $serviceExe stop 2>&1 | Out-Null
    & $serviceExe uninstall 2>&1 | Out-Null
    Start-Sleep -Seconds 2
}

Copy-Item -Force -LiteralPath $worker -Destination (Join-Path $InstallDirectory "archiver-worker.exe")
Copy-Item -Force -LiteralPath $wrapper -Destination $serviceExe

# XML-escape the values so a token containing & or < cannot corrupt the config.
$config = (Get-Content -LiteralPath $template -Raw).
    Replace("__ARCHIVER_WORKER_SERVER__", [System.Security.SecurityElement]::Escape($Server)).
    Replace("__ARCHIVER_WORKER_TOKEN__", [System.Security.SecurityElement]::Escape($Token)).
    Replace("__ARCHIVER_ENCODING_VIDEO_ENCODER__", [System.Security.SecurityElement]::Escape($Encoder))

if ($ServiceAccount) {
    if (-not $ServicePassword) {
        throw "-ServicePassword is required when -ServiceAccount is supplied."
    }
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ServicePassword))
    $account = "  <serviceaccount>`n" +
        "    <username>$([System.Security.SecurityElement]::Escape($ServiceAccount))</username>`n" +
        "    <password>$([System.Security.SecurityElement]::Escape($plain))</password>`n" +
        "    <allowservicelogon>true</allowservicelogon>`n" +
        "  </serviceaccount>`n</service>"
    $config = $config.Replace("</service>", $account)
}

Set-Content -LiteralPath $serviceXml -Value $config -Encoding UTF8

# The config holds the shared secret: restrict it to administrators and SYSTEM.
$acl = Get-Acl -LiteralPath $serviceXml
$acl.SetAccessRuleProtection($true, $false)
foreach ($principal in @("BUILTIN\Administrators", "NT AUTHORITY\SYSTEM")) {
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
        $principal, "FullControl", "Allow")))
}
Set-Acl -LiteralPath $serviceXml -AclObject $acl

& $serviceExe install
& $serviceExe start

Write-Host "Installed: CHZZK Archive Encoder Worker"
Write-Host "  binary : $InstallDirectory\archiver-worker.exe"
Write-Host "  config : $serviceXml"
Write-Host "  logs   : $InstallDirectory"
Write-Host "  encoder: $Encoder"
Write-Host "Remove with: scripts\uninstall-worker.ps1 -Service"
