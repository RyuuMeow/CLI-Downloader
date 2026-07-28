[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$NoVenvSetup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$GeneratedDirectories = @(".tmp-wheel", "build")
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Results = [System.Collections.Generic.List[object]]::new()

function Invoke-Checked {
    param(
        [string]$Name,
        [scriptblock]$Command
    )

    $Timer = [Diagnostics.Stopwatch]::StartNew()
    $Output = @()
    try {
        $local:ErrorActionPreference = "Continue"
        $Output = & $Command 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "$Name failed with exit code $LASTEXITCODE."
        }

        $Timer.Stop()
        $Results.Add([pscustomobject]@{
            Name = $Name
            Seconds = $Timer.Elapsed.TotalSeconds
        })
        Write-Host ("[OK]   {0} ({1:n1}s)" -f $Name, $Timer.Elapsed.TotalSeconds)
    }
    catch {
        $Timer.Stop()
        Write-Host ("[FAIL] {0} ({1:n1}s)" -f $Name, $Timer.Elapsed.TotalSeconds)
        if ($Output.Count -gt 0) {
            Write-Host ""
            Write-Host "Last output:"
            $Output | Select-Object -Last 80 | ForEach-Object { Write-Host $_ }
        }
        throw
    }
}

function Remove-GeneratedDirectory {
    param([string]$Name)

    $Target = Join-Path $Root $Name
    if (-not (Test-Path -LiteralPath $Target)) {
        return
    }

    $Item = Get-Item -LiteralPath $Target -Force
    if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing to clean reparse point: $Target"
    }

    $Resolved = (Resolve-Path -LiteralPath $Target).Path
    if ((Split-Path -Parent $Resolved) -ne $Root -or
        (Split-Path -Leaf $Resolved) -notin $GeneratedDirectories) {
        throw "Refusing to clean unexpected path: $Resolved"
    }

    Remove-Item -LiteralPath $Resolved -Recurse -Force
}

function Test-DevToolsReady {
    if (-not (Test-Path -LiteralPath $Python)) {
        return $false
    }

    & $Python -m ruff --version *> $null
    return $LASTEXITCODE -eq 0
}

Push-Location $Root
try {
    foreach ($Directory in $GeneratedDirectories) {
        Remove-GeneratedDirectory $Directory
    }

    if ([string]::IsNullOrWhiteSpace($Python)) {
        $Python = $VenvPython
    }

    if (-not $NoVenvSetup) {
        if (-not (Test-Path -LiteralPath $Python)) {
            Invoke-Checked "Create venv" { & python -m venv (Join-Path $Root ".venv") }
        }
        if (-not (Test-DevToolsReady)) {
            Invoke-Checked "Install dev dependencies" { & $Python -m pip install -e ".[dev]" }
        }
    }

    Write-Host "Verification"
    Write-Host "  Root:   $Root"
    Write-Host "  Python: $Python"
    Write-Host ""

    Invoke-Checked "Ruff format" { & $Python -m ruff format . }
    Invoke-Checked "Ruff lint" { & $Python -m ruff check . }
    Invoke-Checked "Core verification gate" { & $Python Scripts\verify_plugin_suites.py }
    Invoke-Checked "Python compilation" { & $Python -m compileall -q cli_downloader tests Plugins Scripts }
    Invoke-Checked "Working-tree diff" { & git diff --check }
    Invoke-Checked "Staged diff" { & git diff --cached --check }
    Invoke-Checked "Wheel build" {
        & $Python -m pip wheel . --no-cache-dir --no-deps --no-build-isolation --wheel-dir .tmp-wheel
    }

    Write-Host ""
    Write-Host "Summary"
    foreach ($Result in $Results) {
        Write-Host ("  {0,-26} {1,6:n1}s" -f $Result.Name, $Result.Seconds)
    }
    Write-Host ""
    Write-Host "Verification passed."
}
finally {
    foreach ($Directory in $GeneratedDirectories) {
        Remove-GeneratedDirectory $Directory
    }
    Pop-Location
}
