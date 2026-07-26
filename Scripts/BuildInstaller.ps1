[CmdletBinding()]
param(
    [string]$BundlePath = "",
    [string]$OutputDirectory = "",
    [string]$Version = "",
    [string]$IsccPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if ([string]::IsNullOrWhiteSpace($BundlePath)) {
    $BundlePath = Join-Path $Root "build\portable\bundle\CLI-Downloader"
}
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $Root "dist"
}
if ([string]::IsNullOrWhiteSpace($Version)) {
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "Python environment not found: $Python"
    }
    $Version = (& $Python -c "import cli_downloader; print(cli_downloader.__version__)").Trim()
}
if ([string]::IsNullOrWhiteSpace($Version) -or $Version -eq "0+unknown") {
    throw "A release version is required."
}

$BundlePath = (Resolve-Path -LiteralPath $BundlePath).Path
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$IconFile = (Resolve-Path (Join-Path $Root "assets\icon\CLI-Downloader.ico")).Path
$Script = (Resolve-Path (Join-Path $PSScriptRoot "CLI-Downloader.iss")).Path

foreach ($Required in @("CLI-Downloader.exe", "CLI-Downloader.PluginInstaller.exe", "clid.cmd")) {
    if (-not (Test-Path -LiteralPath (Join-Path $BundlePath $Required))) {
        throw "Portable bundle is missing ${Required}: $BundlePath"
    }
}

if ([string]::IsNullOrWhiteSpace($IsccPath)) {
    $Command = Get-Command iscc -ErrorAction SilentlyContinue
    if ($null -ne $Command) {
        $IsccPath = $Command.Source
    } else {
        $Candidates = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
        )
        $IsccPath = $Candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
}
if ([string]::IsNullOrWhiteSpace($IsccPath) -or -not (Test-Path -LiteralPath $IsccPath)) {
    throw "Inno Setup compiler not found. Install Inno Setup 6 or pass -IsccPath."
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
& $IsccPath "/DAppVersion=$Version" "/DSourceDir=$BundlePath" "/DOutputDir=$OutputDirectory" "/DIconFile=$IconFile" $Script
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE."
}

$Installer = Join-Path $OutputDirectory "CLI-Downloader-$Version-windows-x64-setup.exe"
if (-not (Test-Path -LiteralPath $Installer)) {
    throw "Installer output was not created: $Installer"
}
Write-Host "Installer package: $Installer"
