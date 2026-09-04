param(
    [string]$Python = ".venv\Scripts\python.exe",
    [string]$Output = "dist\worker",
    [string]$BuildVenv = ".build-venv-windows"
)

$ErrorActionPreference = "Stop"
$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = (Resolve-Path (Join-Path $workspace $Python)).Path
$outputPath = Join-Path $workspace $Output
$buildVenvPath = Join-Path $workspace $BuildVenv
$buildPython = Join-Path $buildVenvPath "Scripts\python.exe"
$workPath = Join-Path $workspace "build\worker-windows"
$specPath = Join-Path $workspace "build"

New-Item -ItemType Directory -Force -Path $outputPath, $workPath, $specPath | Out-Null

if (Test-Path -LiteralPath $buildVenvPath) {
    Remove-Item -LiteralPath $buildVenvPath -Recurse -Force
}

try {
    # Build in an isolated environment and install the project itself. Merely
    # installing PyInstaller can produce an EXE that starts successfully on the
    # build machine but is missing httpx/pydantic-settings on a clean host.
    & $pythonPath -m venv $buildVenvPath
    uv pip install --python $buildPython $workspace pyinstaller
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
