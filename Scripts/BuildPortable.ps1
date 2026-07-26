[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$SkipVerification
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$BuildRoot = Join-Path $Root "build\portable"
$DistRoot = Join-Path $Root "dist"
$Bundle = Join-Path $BuildRoot "bundle\CLI-Downloader"

function Remove-GeneratedPath {
    param([string]$Path, [string]$ExpectedParent)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $Resolved = (Resolve-Path -LiteralPath $Path).Path
    $Parent = (Resolve-Path -LiteralPath $ExpectedParent).Path
    if ((Split-Path -Parent $Resolved) -ne $Parent) {
        throw "Refusing to remove unexpected generated path: $Resolved"
    }
    Remove-Item -LiteralPath $Resolved -Recurse -Force
}

if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = $VenvPython
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python environment not found: $Python"
}

Push-Location $Root
try {
    if (-not $SkipVerification) {
        & (Join-Path $PSScriptRoot "Verify.ps1") -Python $Python -NoVenvSetup
        if ($LASTEXITCODE -ne 0) { throw "Repository verification failed." }
    }

    & $Python -m PyInstaller --version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is unavailable. Install the package extra with: pip install -e '.[package]'"
    }

    New-Item -ItemType Directory -Force -Path $BuildRoot, $DistRoot | Out-Null
    Remove-GeneratedPath -Path (Join-Path $BuildRoot "bundle") -ExpectedParent $BuildRoot
    Remove-GeneratedPath -Path (Join-Path $BuildRoot "work") -ExpectedParent $BuildRoot
    New-Item -ItemType Directory -Force -Path (Join-Path $BuildRoot "bundle") | Out-Null

    & $Python -m PyInstaller --noconfirm --clean `
        --distpath (Join-Path $BuildRoot "bundle") `
        --workpath (Join-Path $BuildRoot "work") `
        (Join-Path $PSScriptRoot "clid.spec")
    if ($LASTEXITCODE -ne 0) { throw "Portable bundle build failed." }

    Copy-Item -LiteralPath (Join-Path $Root "assets\portable\clid.cmd") `
        -Destination (Join-Path $Bundle "clid.cmd")

    & $Python Scripts\portable_smoke.py $Bundle
    if ($LASTEXITCODE -ne 0) { throw "Portable smoke test failed." }

    $Version = (& $Python -c "import cli_downloader; print(cli_downloader.__version__)").Trim()
    $Archive = Join-Path $DistRoot "CLI-Downloader-$Version-windows-x64-portable.zip"
    if (Test-Path -LiteralPath $Archive) {
        Remove-Item -LiteralPath $Archive -Force
    }
    Compress-Archive -Path (Join-Path $Bundle "*") -DestinationPath $Archive -CompressionLevel Optimal
    Write-Host "Portable package: $Archive"
}
finally {
    Pop-Location
}
