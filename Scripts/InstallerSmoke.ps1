[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$InstallerPath = (Resolve-Path -LiteralPath $InstallerPath).Path
$Root = Join-Path ([IO.Path]::GetTempPath()) ("cli-downloader-installer-smoke-" + [Guid]::NewGuid().ToString("N"))
$InstallDirectory = Join-Path $Root "app"
$DataRoot = Join-Path $Root "data"
$Marker = Join-Path $DataRoot "state\preserve-me.txt"
New-Item -ItemType Directory -Force -Path $Root, (Split-Path -Parent $Marker) | Out-Null

$OldDataRoot = $env:CLI_DOWNLOADER_DATA_ROOT
$env:CLI_DOWNLOADER_DATA_ROOT = $DataRoot
try {
    $InstallArguments = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/DIR=`"$InstallDirectory`"",
        "/TASKS="
    )
    $InstallResult = Start-Process -FilePath $InstallerPath -ArgumentList $InstallArguments -Wait -PassThru
    if ($InstallResult.ExitCode -ne 0) {
        throw "Installer exited with code $($InstallResult.ExitCode)."
    }

    $Executable = Join-Path $InstallDirectory "CLI-Downloader.exe"
    $Uninstaller = Join-Path $InstallDirectory "unins000.exe"
    if (-not (Test-Path -LiteralPath $Executable) -or -not (Test-Path -LiteralPath $Uninstaller)) {
        throw "Installer did not create the application and uninstaller."
    }
    & $Executable --help *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Installed executable failed --help."
    }

    Set-Content -LiteralPath $Marker -Value "user data" -Encoding UTF8
    $UninstallResult = Start-Process -FilePath $Uninstaller -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART"
    ) -Wait -PassThru
    if ($UninstallResult.ExitCode -ne 0) {
        throw "Uninstaller exited with code $($UninstallResult.ExitCode)."
    }
    if (-not (Test-Path -LiteralPath $Marker)) {
        throw "Uninstall removed user data."
    }
    if (Test-Path -LiteralPath $InstallDirectory) {
        throw "Uninstall left the application directory behind."
    }
    Write-Host "Installer smoke test: PASS"
}
finally {
    if ($null -eq $OldDataRoot) {
        Remove-Item Env:CLI_DOWNLOADER_DATA_ROOT -ErrorAction SilentlyContinue
    } else {
        $env:CLI_DOWNLOADER_DATA_ROOT = $OldDataRoot
    }
    if (Test-Path -LiteralPath $Root) {
        Remove-Item -LiteralPath $Root -Recurse -Force -ErrorAction SilentlyContinue
    }
}
