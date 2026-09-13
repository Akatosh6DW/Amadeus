[CmdletBinding()]
param(
    [ValidateSet("auto", "cuda", "cpu")]
    [string]$Backend = "auto"
)

$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$python = Join-Path $root ".conda\amadeus-desktop\python.exe"
$verifier = Join-Path $PSScriptRoot "verify_deployment.py"
$ready = $false
if (Test-Path -LiteralPath $python -PathType Leaf) {
    Write-Host "Checking AMADEUS..."
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $python -X utf8 $verifier --project-root $root *> $null
        $ready = $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

if (-not $ready) {
    Write-Host "Preparing AMADEUS for use. Missing components will download automatically."
    Write-Host "First launch needs Internet access and several GB of downloads. Keep this window open."
    $bootstrap = Join-Path $PSScriptRoot "bootstrap_deploy.ps1"
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $bootstrap -Backend $Backend -NoLaunch
        $preparationExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($preparationExit -ne 0) {
        throw "Preparation failed. Correct the error above, then run start.cmd again."
    }
}

& (Join-Path $root "run_amadeus.ps1")
