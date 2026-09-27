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
$sourceRepository = "Akatosh6DW/Amadeus"
$sourceRef = "main"

function Get-GitHubCredential {
    # Read credentials already managed by the user's environment.  Keep the
    # value in process memory only; never write it to the source tree, command
    # line, or a generated settings file.
    foreach ($candidate in @($env:GH_TOKEN, $env:GITHUB_TOKEN)) {
        if ($candidate -and $candidate.Trim()) { return $candidate.Trim() }
    }
    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if ($gh) {
        $token = ((& $gh.Source auth token --hostname github.com 2>$null) -join "`n").Trim()
        if ($LASTEXITCODE -eq 0 -and $token) { return $token }
    }
    return ""
}

function Resolve-ExtractedSource {
    param([string]$Staging)
    $candidates = @(
        Get-ChildItem -LiteralPath $Staging -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "tools\bootstrap_deploy.ps1") }
    )
    if ($candidates.Count -ne 1) { throw "Deployment source archive did not contain exactly one complete source tree" }
    $resolved = [IO.Path]::GetFullPath($candidates[0].FullName)
    $resolvedRoot = [IO.Path]::GetFullPath($Staging).TrimEnd("\") + "\"
    if (-not $resolved.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Invalid archive root"
    }
    return $resolved
}

function Invoke-GitHubAuthenticatedDownload {
    param(
        [string]$Uri,
        [string]$OutFile,
        [string]$Token
    )
    # Do not let HttpWebRequest automatically carry Authorization across a
    # redirect.  GitHub's API commonly redirects archive downloads to a CDN;
    # the token is attached only to the two GitHub API hosts and is removed
    # from every other hop.
    $currentUri = [Uri]$Uri
    for ($attempt = 0; $attempt -lt 8; $attempt++) {
        $request = [Net.HttpWebRequest]::Create($currentUri)
        $request.Method = "GET"
        $request.UserAgent = "AMADEUS-source-installer"
        $request.AllowAutoRedirect = $false
        $currentHost = $currentUri.Host.ToLowerInvariant()
        if ($currentHost -eq "github.com" -or $currentHost -eq "api.github.com") {
            $request.Headers["Authorization"] = "Bearer $Token"
        }

        $response = $null
        try {
            try {
                $response = [Net.HttpWebResponse]$request.GetResponse()
            }
            catch {
                $exception = $_.Exception
                if ($exception -is [Net.WebException] -and $exception.Response) {
                    $response = [Net.HttpWebResponse]$exception.Response
                }
                else {
                    throw
                }
            }

            $statusCode = [int]$response.StatusCode
            if ($statusCode -ge 300 -and $statusCode -lt 400) {
                $location = $response.Headers["Location"]
                if (-not $location) { throw "GitHub source download returned a redirect without a location" }
                $currentUri = [Uri]::new($currentUri, $location)
                continue
            }
            if ($statusCode -lt 200 -or $statusCode -ge 300) {
                throw "GitHub source download returned HTTP $statusCode"
            }

            $stream = $response.GetResponseStream()
            $output = [IO.File]::Open($OutFile, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try {
                $stream.CopyTo($output)
            }
            finally {
                $output.Dispose()
                $stream.Dispose()
            }
            return
        }
        finally {
            if ($response) { $response.Close() }
        }
    }
    throw "GitHub source download followed too many redirects"
}

function Invoke-SourceArchiveDownload {
    param(
        [string]$ArchiveUri,
        [string]$AuthenticatedUri,
        [string]$OutFile
    )
    $token = Get-GitHubCredential
    try {
        if ($token) {
            # Use the API endpoint when a credential is available.  GitHub's
            # API handles the private repository authorization before it may
            # redirect to the archive CDN.  The helper manually follows that
            # redirect so Authorization is never sent to the CDN.
            Invoke-GitHubAuthenticatedDownload -Uri $AuthenticatedUri -OutFile $OutFile -Token $token
        }
        else {
            Invoke-WebRequest -Uri $ArchiveUri -OutFile $OutFile -UseBasicParsing | Out-Null
        }
        return (Test-Path -LiteralPath $OutFile -PathType Leaf) -and ((Get-Item -LiteralPath $OutFile).Length -gt 0)
    }
    catch {
        return $false
    }
    finally {
        $token = $null
        Remove-Variable token -ErrorAction SilentlyContinue
    }
}

function Get-SourceTree {
    param([string]$Staging)
    $archive = Join-Path $Staging "source.zip"
    $archiveUri = "https://github.com/$sourceRepository/archive/refs/heads/$sourceRef.zip"
    $authenticatedUri = "https://api.github.com/repos/$sourceRepository/zipball/$sourceRef"
    Write-Host "Downloading Amadeus deployment source..."
    if (Invoke-SourceArchiveDownload -ArchiveUri $archiveUri -AuthenticatedUri $authenticatedUri -OutFile $archive) {
        try {
            $archiveStaging = Join-Path $Staging "archive"
            New-Item -ItemType Directory -Path $archiveStaging -Force | Out-Null
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            [IO.Compression.ZipFile]::ExtractToDirectory($archive, $archiveStaging)
            return Resolve-ExtractedSource -Staging $archiveStaging
        }
        catch {
            # A private API error can still leave a nonempty response body. Let
            # the authenticated Git fallback produce the useful error path.
        }
    }

    # Existing GitHub authentication is also available through Git Credential
    # Manager.  Use it without putting credentials in the clone URL.  This is
    # the fallback for private repositories when the archive request is not
    # accepted by the configured credential provider.
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        $gitSource = Join-Path $Staging "Amadeus-git"
        Write-Host "Archive download was not authorized; trying authenticated Git..."
        $previousPrompt = $env:GIT_TERMINAL_PROMPT
        $env:GIT_TERMINAL_PROMPT = "0"
        try {
            & $git.Source clone --depth 1 --branch $sourceRef --single-branch "https://github.com/$sourceRepository.git" $gitSource *> $null
            $gitExit = $LASTEXITCODE
        }
        finally {
            if ($null -eq $previousPrompt) { Remove-Item Env:GIT_TERMINAL_PROMPT -ErrorAction SilentlyContinue }
            else { $env:GIT_TERMINAL_PROMPT = $previousPrompt }
        }
        if ($gitExit -eq 0) {
            return Resolve-ExtractedSource -Staging $Staging
        }
        if (Test-Path -LiteralPath $gitSource) {
            $resolvedGitSource = [IO.Path]::GetFullPath($gitSource)
            $resolvedStagingRoot = [IO.Path]::GetFullPath($Staging).TrimEnd("\") + "\"
            if (-not $resolvedGitSource.StartsWith($resolvedStagingRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to remove an unexpected Git staging path: $resolvedGitSource"
            }
            Remove-Item -LiteralPath $resolvedGitSource -Recurse -Force
        }
    }
    throw "Could not download the private Amadeus source. Sign in with 'gh auth login' or configure Git Credential Manager, then rerun the installer."
}

function Invoke-AmadeusDeployment {
    # irm | iex works even when downloaded .ps1 files cannot run under the
    # user's default policy. Bypass applies only to this child process.
    $arguments = @("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $bootstrap, "-Backend", $Backend)
    if ($NoLaunch) { $arguments += "-NoLaunch" }
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & powershell.exe @arguments
        $deploymentExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($deploymentExit -ne 0) { throw "Amadeus deployment failed with exit code $deploymentExit. Rerun the same command to retry." }
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
    $source = Get-SourceTree -Staging $staging
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
