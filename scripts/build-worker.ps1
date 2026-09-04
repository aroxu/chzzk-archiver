param(
    [string]$PythonVersion = "3.12",
    [string]$Venv = ".venv",
    [string]$Output = "dist\worker",
    [string]$BuildVenv = ".build-venv-windows",
    [string]$Uv
)

$ErrorActionPreference = "Stop"
$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPath = Join-Path $workspace $Venv
$projectPython = Join-Path $venvPath "Scripts\python.exe"
$outputPath = Join-Path $workspace $Output
$buildVenvPath = Join-Path $workspace $BuildVenv
$buildPython = Join-Path $buildVenvPath "Scripts\python.exe"
$workPath = Join-Path $workspace "build\worker-windows"
$specPath = Join-Path $workspace "build"
$workerExe = Join-Path $outputPath "archiver-worker.exe"
$toolsPath = Join-Path $workspace ".build-tools\uv"

function Assert-NativeSuccess([string]$Message) {
    if ($LASTEXITCODE -ne 0) {
        throw $Message
    }
}

function Resolve-UvExecutable {
    if ($Uv) {
        $requestedUv = if ([IO.Path]::IsPathRooted($Uv)) { $Uv } else { Join-Path $workspace $Uv }
        if (-not (Test-Path -LiteralPath $requestedUv -PathType Leaf)) {
            throw "uv executable not found at '$requestedUv'."
        }
        return (Resolve-Path -LiteralPath $requestedUv).Path
    }

    $installedUv = Get-Command uv -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($installedUv) {
        return $installedUv.Source
    }

    $cachedUv = Join-Path $toolsPath "uv.exe"
    if (Test-Path -LiteralPath $cachedUv -PathType Leaf) {
        return $cachedUv
    }

    $target = switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture) {
        "X64" { "x86_64-pc-windows-msvc" }
        "Arm64" { "aarch64-pc-windows-msvc" }
        default { throw "Unsupported Windows architecture: $($_)" }
    }
    $archiveName = "uv-$target.zip"
    $baseUrl = "https://github.com/astral-sh/uv/releases/latest/download/$archiveName"
    $archive = Join-Path $toolsPath $archiveName
    $checksum = "$archive.sha256"

    New-Item -ItemType Directory -Force -Path $toolsPath | Out-Null
    Write-Host "uv is not installed; downloading the standalone $target build..."
    Invoke-WebRequest -Uri $baseUrl -OutFile $archive
    Invoke-WebRequest -Uri "$baseUrl.sha256" -OutFile $checksum

    $expectedHash = ((Get-Content -LiteralPath $checksum -Raw).Trim() -split "\s+")[0]
    $actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
    if (-not $expectedHash -or $actualHash -ine $expectedHash) {
        Remove-Item -LiteralPath $archive, $checksum -Force -ErrorAction SilentlyContinue
        throw "uv download checksum verification failed."
    }

    Expand-Archive -LiteralPath $archive -DestinationPath $toolsPath -Force
    Remove-Item -LiteralPath $archive, $checksum -Force
    if (-not (Test-Path -LiteralPath $cachedUv -PathType Leaf)) {
        throw "Downloaded uv archive did not contain uv.exe."
    }
    return $cachedUv
}

$uvExe = Resolve-UvExecutable
Write-Host "Build runtime: uv-managed Python $PythonVersion"

$projectVenvReady = $false
if (Test-Path -LiteralPath $projectPython -PathType Leaf) {
    & $projectPython -c "import sys; raise SystemExit(0 if sys.version_info[:2] == tuple(map(int, '$PythonVersion'.split('.')[:2])) else 1)" 2>$null
    $projectVenvReady = $LASTEXITCODE -eq 0
}
if (-not $projectVenvReady) {
    Write-Host "Creating project virtual environment: $venvPath"
    & $uvExe venv --clear --python $PythonVersion --managed-python $venvPath
    Assert-NativeSuccess "Failed to create project virtual environment with Python $PythonVersion."
    if (-not (Test-Path -LiteralPath $projectPython -PathType Leaf)) {
        throw "uv did not create the expected Python executable: $projectPython"
    }
}

New-Item -ItemType Directory -Force -Path $outputPath, $workPath, $specPath | Out-Null

if (Test-Path -LiteralPath $buildVenvPath) {
    Remove-Item -LiteralPath $buildVenvPath -Recurse -Force
}
if (Test-Path -LiteralPath $workerExe) {
    # Never leave a stale executable that could be mistaken for this build.
    Remove-Item -LiteralPath $workerExe -Force
}

try {
    # Always use a uv-managed runtime. The installed system Python (or its absence)
    # cannot affect dependency resolution or the resulting executable.
    & $uvExe venv --python $PythonVersion --managed-python $buildVenvPath
    Assert-NativeSuccess "Failed to create isolated build environment with Python $PythonVersion."
    if (-not (Test-Path -LiteralPath $buildPython -PathType Leaf)) {
        throw "uv did not create the expected build Python: $buildPython"
    }

    & $uvExe pip install --python $buildPython $workspace pyinstaller
    Assert-NativeSuccess "Failed to install worker runtime dependencies."

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
    Assert-NativeSuccess "PyInstaller failed to build the worker."
    if (-not (Test-Path -LiteralPath $workerExe -PathType Leaf)) {
        throw "PyInstaller did not create: $workerExe"
    }
} finally {
    if (Test-Path -LiteralPath $buildVenvPath) {
        Remove-Item -LiteralPath $buildVenvPath -Recurse -Force
    }
}

Write-Host "Worker binary: $workerExe"
& $workerExe --doctor
Assert-NativeSuccess "The built worker failed its self-test: $workerExe"
