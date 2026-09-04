param(
    [string]$Python,
    [string]$Venv = ".venv",
    [string]$Output = "dist\worker",
    [string]$BuildVenv = ".build-venv-windows"
)

$ErrorActionPreference = "Stop"
$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$outputPath = Join-Path $workspace $Output
$buildVenvPath = Join-Path $workspace $BuildVenv
$buildPython = Join-Path $buildVenvPath "Scripts\python.exe"
$workPath = Join-Path $workspace "build\worker-windows"
$specPath = Join-Path $workspace "build"

if ($Python) {
    $requestedPython = if ([IO.Path]::IsPathRooted($Python)) {
        $Python
    } else {
        Join-Path $workspace $Python
    }
    if (-not (Test-Path -LiteralPath $requestedPython)) {
        throw "Python executable not found at '$requestedPython'."
    }
    $pythonPath = (Resolve-Path -LiteralPath $requestedPython).Path
} else {
    $venvPath = Join-Path $workspace $Venv
    $pythonPath = Join-Path $venvPath "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        if (Test-Path -LiteralPath $venvPath) {
            throw "'$venvPath' exists but is not a usable Windows virtual environment."
        }
        $bootstrap = $null
        $bootstrapArgs = @()
        foreach ($commandName in @("python3", "python", "py")) {
            $candidate = Get-Command $commandName -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if (-not $candidate) {
                continue
            }
            $candidateArgs = if ($commandName -eq "py") { @("-3.12") } else { @() }
            & $candidate.Source @candidateArgs -c `
                "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
            if ($LASTEXITCODE -eq 0) {
                $bootstrap = $candidate
                $bootstrapArgs = $candidateArgs
                break
            }
        }
        if (-not $bootstrap) {
            throw "Python 3.12 or newer is required. Install Python and run this script again."
        }
        Write-Host "Creating project virtual environment: $venvPath"
        & $bootstrap.Source @bootstrapArgs -m venv $venvPath
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonPath)) {
            throw "Failed to create '$venvPath'. Python 3.12 or newer is required."
        }
    }
}

$supported = & $pythonPath -c "import sys; print(int(sys.version_info >= (3, 12)))"
if ($LASTEXITCODE -ne 0 -or $supported.Trim() -ne "1") {
    throw "Python 3.12 or newer is required: $pythonPath"
}

New-Item -ItemType Directory -Force -Path $outputPath, $workPath, $specPath | Out-Null

if (Test-Path -LiteralPath $buildVenvPath) {
    Remove-Item -LiteralPath $buildVenvPath -Recurse -Force
}

try {
    # Build in an isolated environment and install the project itself. Merely
    # installing PyInstaller can produce an EXE that starts successfully on the
    # build machine but is missing httpx/pydantic-settings on a clean host.
    & $pythonPath -m venv $buildVenvPath
    & $buildPython -m pip install --disable-pip-version-check $workspace pyinstaller
    & $buildPython -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --name archiver-worker `
        --paths (Join-Path $workspace "backend") `
        --distpath $outputPath `
        --workpath $workPath `
        --specpath $specPath `
        (Join-Path $workspace "backend\worker_entry.py")
} finally {
    if (Test-Path -LiteralPath $buildVenvPath) {
        Remove-Item -LiteralPath $buildVenvPath -Recurse -Force
    }
}

Write-Host "Worker binary: $outputPath\archiver-worker.exe"
& (Join-Path $outputPath "archiver-worker.exe") --doctor
