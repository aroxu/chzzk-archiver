param(
    [string]$Python = ".venv\Scripts\python.exe",
    [string]$Output = "dist\worker"
)

$ErrorActionPreference = "Stop"
$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = (Resolve-Path (Join-Path $workspace $Python)).Path
$outputPath = Join-Path $workspace $Output

uv pip install --python $pythonPath pyinstaller
& $pythonPath -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name archiver-worker `
    --paths (Join-Path $workspace "backend") `
    --distpath $outputPath `
    (Join-Path $workspace "backend\worker_entry.py")

Write-Host "Worker binary: $outputPath\archiver-worker.exe"
