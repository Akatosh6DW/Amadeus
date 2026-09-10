[CmdletBinding()]
param(
    [string]$InstallDirectory = (Join-Path $env:USERPROFILE "Apps\Amadeus"),
    [ValidateSet("auto", "cuda", "cpu")]
    [string]$Backend = "auto",
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$target = [IO.Path]::GetFullPath($InstallDirectory)
$bootstrap = Join-Path $target "tools\bootstrap_deploy.ps1"
function Invoke-AmadeusDeployment {
    # irm | iex works even when downloaded .ps1 files cannot run under the
    # user's default policy. Bypass applies only to this child process.
    $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $bootstrap, "-Backend", $Backend)
    if ($NoLaunch) { $arguments += "-NoLaunch" }
    & powershell.exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "AMADEUS deployment failed with exit code $LASTEXITCODE. Rerun the same command to retry." }
}
if (Test-Path -LiteralPath $bootstrap -PathType Leaf) {
    # Resume an interrupted installation; existing user data stays in place.
    Invoke-AmadeusDeployment
    return
}
if ((Test-Path -LiteralPath $target) -and (Get-ChildItem -LiteralPath $target -Force | Select-Object -First 1)) {
    throw "Installation directory is not empty. Choose a new InstallDirectory: $target"
}
$parent = Split-Path $target -Parent
New-Item -ItemType Directory -Path $parent -Force | Out-Null
$staging = Join-Path $parent (".amadeus-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $staging | Out-Null
try {
    $archive = Join-Path $staging "source.zip"
    Write-Host "Downloading AMADEUS deployment source..."
    Invoke-WebRequest -Uri "https://github.com/Akatosh6DW/Amadeus/archive/refs/heads/main.zip" -OutFile $archive -UseBasicParsing
    # Avoid loading the script-based Archive module under Restricted policy.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $staging)
    $source = [IO.Path]::GetFullPath((Join-Path $staging "Amadeus-main"))
    if (-not $source.StartsWith($staging + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw "Invalid archive root" }
    if (-not (Test-Path -LiteralPath (Join-Path $source "tools\bootstrap_deploy.ps1"))) { throw "Deployment source is incomplete" }
    # Publish the complete extracted tree at once; a failed copy cannot leave a
    # bootstrap script beside an incomplete source tree that looks resumable.
    if (Test-Path -LiteralPath $target) {
        if (Get-ChildItem -LiteralPath $target -Force | Select-Object -First 1) { throw "Installation directory became nonempty" }
        Remove-Item -LiteralPath $target
    }
    Move-Item -LiteralPath $source -Destination $target
} finally {
    $resolvedStaging = [IO.Path]::GetFullPath($staging)
    if ((Split-Path $resolvedStaging -Parent) -eq $parent -and (Split-Path $resolvedStaging -Leaf).StartsWith(".amadeus-install-")) {
        Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
    }
}
Invoke-AmadeusDeployment
