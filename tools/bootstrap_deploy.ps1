param(
    [ValidateSet("auto", "cuda", "cpu")]
    [string]$Backend = "auto",
    [switch]$Force,
    [switch]$VerifyOnly,
    [switch]$NoLaunch,
    [string]$AssetArchiveDirectory = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$root = [System.IO.Path]::GetFullPath((Get-Location).Path)
$condaRoot = Join-Path $root ".tools\miniforge"
$desktopEnv = Join-Path $root ".conda\amadeus-desktop"
$voiceEnv = Join-Path $root ".conda\gpt-sovits"
$gptRoot = Join-Path $root ".sources\GPT-SoVITS"
$voiceRoot = Join-Path $root ".runtime-assets\voice"
$downloadHelper = Join-Path $root "tools\download_runtime_file.py"
$environment = Get-Content -LiteralPath (Join-Path $root "configs\deployment_environment.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if ($environment.formatVersion -ne 1) { throw "Unsupported deployment environment manifest" }
$gptRepository = $environment.gptSovits.repository
$gptCommit = $environment.gptSovits.commit

function Test-PathInsideRoot {
    param([string]$Path, [string]$AllowedRoot = $root)
    $candidate = [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
    $allowed = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd("\")
    return $candidate.StartsWith($allowed + "\", [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-PathInsideRoot {
    param([string]$Path, [string]$AllowedRoot = $root)
    if (-not (Test-PathInsideRoot -Path $Path -AllowedRoot $AllowedRoot)) {
        throw "Refusing to modify a path outside the expected root: $Path"
    }
}

function Invoke-Native {
    param([string]$FilePath, [string[]]$Arguments)
    # Resolve before relaxing native stderr handling so a missing executable
    # cannot reuse the previous command's successful exit code.
    $command = Get-Command -Name $FilePath -ErrorAction Stop
    $previousErrorAction = $ErrorActionPreference
    try {
        # Native progress/warnings are display output, never a caller's return
        # value. PowerShell 5 must use the exit code to decide native failure.
        $ErrorActionPreference = "Continue"
        & $command @Arguments 2>&1 | ForEach-Object { $_.ToString() } | Out-Host
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($exitCode -ne 0) {
        throw "$FilePath failed with exit code $exitCode"
    }
}

function Find-Conda {
    $candidates = @()
    if ($env:CONDA_EXE) { $candidates += $env:CONDA_EXE }
    $command = Get-Command conda -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    $candidates += @(
        (Join-Path $condaRoot "Scripts\conda.exe"),
        (Join-Path $env:USERPROFILE "miniforge3\Scripts\conda.exe"),
        (Join-Path $env:USERPROFILE "anaconda3\Scripts\conda.exe"),
        (Join-Path $env:LOCALAPPDATA "miniforge3\Scripts\conda.exe"),
        "D:\Anaconda\Scripts\conda.exe"
    )
    foreach ($candidate in $candidates | Where-Object { $_ } | Select-Object -Unique) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    return ""
}

function Install-ProjectMiniforge {
    Assert-PathInsideRoot -Path $condaRoot
    $downloadDir = Join-Path $root ".tmp\bootstrap"
    $installer = Join-Path $downloadDir "Miniforge3-Windows-x86_64.exe"
    New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
    Write-Host "Conda was not found. Downloading project-local Miniforge..."
    Invoke-WebRequest `
        -Uri $environment.miniforgeInstaller `
        -OutFile $installer `
        -UseBasicParsing
    $arguments = @(
        "/S",
        "/InstallationType=JustMe",
        "/AddToPath=0",
        "/RegisterPython=0",
        "/NoRegistry=1",
        "/D=$condaRoot"
    )
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw "Miniforge installer failed with exit code $($process.ExitCode)"
    }
    $installed = Join-Path $condaRoot "Scripts\conda.exe"
    if (-not (Test-Path -LiteralPath $installed -PathType Leaf)) {
        throw "Miniforge installation completed but conda.exe was not found: $installed"
    }
    return $installed
}

function Ensure-CondaEnvironment {
    param(
        [string]$Conda,
        [string]$Prefix,
        [string]$PythonVersion,
        [string[]]$CondaPackages = @()
    )
    Assert-PathInsideRoot -Path $Prefix
    $python = Join-Path $Prefix "python.exe"
    if ((Test-Path -LiteralPath $python -PathType Leaf) -and -not $Force) {
        return $python
    }
    $arguments = @("create", "--prefix", $Prefix, "python=$PythonVersion", "pip")
    $arguments += $CondaPackages
    $arguments += @("-y", "--override-channels", "-c", $environment.condaChannel)
    Invoke-Native -FilePath $Conda -Arguments $arguments
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Environment creation did not produce $python"
    }
    return $python
}

function Test-PythonImports {
    param([string]$Python, [string[]]$Modules)
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { return $false }
    $program = "import " + ($Modules -join ", ")
    # A failed import is the expected signal to install dependencies. Do not
    # let the script-wide Stop policy turn this capability probe into a fatal
    # PowerShell NativeCommandError before we can inspect the exit code.
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Python -c $program *> $null
        return $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

function Install-PipRequirements {
    param([string]$Python, [string]$Requirements)
    Invoke-Native -FilePath $Python -Arguments @("-m", "pip", "install", "--index-url", $environment.pipIndex, "--upgrade", "pip")
    Invoke-Native -FilePath $Python -Arguments @("-m", "pip", "install", "--index-url", $environment.pipIndex, "-r", $Requirements)
}

function Resolve-Backend {
    if ($Backend -ne "auto") { return $Backend }
    $nvidia = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $nvidia) { return "cpu" }
    $status = (& $nvidia.Source 2>$null) -join "`n"
    if ($LASTEXITCODE -eq 0 -and $status -match 'CUDA Version:\s*(\d+\.\d+)') {
        if ([version]$Matches[1] -ge [version]$environment.torch.cudaMinimum) { return "cuda" }
    }
    return "cpu"
}

function Ensure-GptSovitsSource {
    Assert-PathInsideRoot -Path $gptRoot
    if (-not (Test-Path -LiteralPath (Join-Path $gptRoot ".git"))) {
        $sourceParent = Split-Path $gptRoot -Parent
        New-Item -ItemType Directory -Path $sourceParent -Force | Out-Null
        $staging = Join-Path $sourceParent (".gpt-sovits-download-" + [guid]::NewGuid().ToString("N"))
        Assert-PathInsideRoot -Path $staging
        try {
            # Publish the checkout only after the pinned revision is ready.
            # An interrupted clone must not look like a complete installation
            # at the upstream default branch on the next launch.
            Invoke-Native -FilePath "git" -Arguments @("clone", "--no-checkout", "--filter=blob:none", $gptRepository, $staging)
            Invoke-Native -FilePath "git" -Arguments @("-C", $staging, "checkout", "--detach", $gptCommit)
            if (Test-Path -LiteralPath $gptRoot) { throw "GPT-SoVITS destination already exists: $gptRoot" }
            Move-Item -LiteralPath $staging -Destination $gptRoot
        } finally {
            Assert-PathInsideRoot -Path $staging
            if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
        }
    }

    # Playback uses upstream inference. Training-only changes belong to the
    # developer workflow and must not be a prerequisite for user deployment.
    $head = (& git -C $gptRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect GPT-SoVITS checkout" }
    if ($head -ne $gptCommit) {
        throw "GPT-SoVITS is at $head, expected pinned commit $gptCommit. Bootstrap will not overwrite an existing checkout."
    }
}

function Find-MsvcCompiler {
    $vswhere = Join-Path ([Environment]::GetFolderPath("ProgramFilesX86")) "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) { return "" }
    $paths = @(& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find 'VC\Tools\MSVC\**\bin\HostX64\x64\cl.exe')
    if ($LASTEXITCODE -ne 0) { return "" }
    foreach ($path in $paths) {
        if (Test-Path -LiteralPath $path -PathType Leaf) { return $path }
    }
    return ""
}

function Ensure-BuildTools {
    if (Find-MsvcCompiler) { return }
    Write-Host "Installing Microsoft's C++ build tools required by speech dependencies. Windows may request administrator approval."
    $downloadDir = Join-Path $root ".tmp\bootstrap"
    New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
    $installer = Join-Path $downloadDir "vs_buildtools.exe"
    Invoke-WebRequest -Uri $environment.buildToolsInstaller -OutFile $installer -UseBasicParsing
    $buildToolsRoot = Join-Path $root ".tools\vs-buildtools"
    Assert-PathInsideRoot -Path $buildToolsRoot
    $arguments = @("--quiet", "--wait", "--norestart", "--installPath", "`"$buildToolsRoot`"", "--add", "Microsoft.VisualStudio.Workload.VCTools", "--includeRecommended")
    $parameters = @{ FilePath=$installer; ArgumentList=$arguments; Wait=$true; PassThru=$true; WindowStyle="Hidden" }
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { $parameters.Verb="RunAs" }
    $process = Start-Process @parameters
    if ($process.ExitCode -eq 3010) {
        throw "Microsoft C++ tools require a Windows restart. Restart when convenient, then run start.cmd again. Amadeus System has not restarted Windows."
    }
    if ($process.ExitCode -ne 0 -or -not (Find-MsvcCompiler)) {
        throw "Microsoft C++ build tools were not installed successfully (exit $($process.ExitCode)). Run start.cmd again after resolving the installer error."
    }
}

function Ensure-VoiceEnvironment {
    param([string]$Conda, [string]$SelectedBackend)
    if (-not (Test-Path -LiteralPath $downloadHelper -PathType Leaf)) {
        throw "Runtime download helper is missing: $downloadHelper"
    }
    $python = Ensure-CondaEnvironment `
        -Conda $Conda `
        -Prefix $voiceEnv `
        -PythonVersion $environment.python.voice `
        -CondaPackages @("ffmpeg", "cmake")
    $voiceReady = $false
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $python -X utf8 (Join-Path $root "tools\verify_deployment.py") --project-root $root --voice-only *> $null
        $voiceReady = $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($voiceReady -and -not $Force) {
        return
    }
    Ensure-BuildTools
    # Use the upstream asset installer with a small, reviewable Windows patch.
    # TorchAudio 2.7 uses its own decoder; TorchCodec is not required by this stack.
    foreach ($patchName in @("gpt-sovits-install.patch", "gpt-sovits-model-repair.patch", "gpt-sovits-download.patch")) {
    $installerPatch = Join-Path $root ("patches\" + $patchName)
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & git -C $gptRoot apply --reverse --check $installerPatch 2>$null
        $alreadyPatched = $LASTEXITCODE -eq 0
    } finally { $ErrorActionPreference = $previousErrorAction }
    if (-not $alreadyPatched) {
        Invoke-Native -FilePath "git" -Arguments @("-C", $gptRoot, "apply", "--check", $installerPatch)
        Invoke-Native -FilePath "git" -Arguments @("-C", $gptRoot, "apply", $installerPatch)
    }
    }
    $device = $(if ($SelectedBackend -eq "cuda") { "CU128" } else { "CPU" })
    $torchIndex = $(if ($SelectedBackend -eq "cuda") { $environment.torch.cudaIndex } else { $environment.torch.cpuIndex })
    $condaDirectory = Split-Path $Conda -Parent
    $env:Path = "$condaDirectory;$voiceEnv;$(Join-Path $voiceEnv 'Scripts');$env:Path"
    $previousConstraint = $env:PIP_CONSTRAINT
    $previousIndex = $env:PIP_INDEX_URL
    $previousModelRepair = $env:AMADEUS_REPAIR_BASE_MODELS
    $previousDownloadHelper = $env:AMADEUS_DOWNLOAD_HELPER
    try {
    $env:PIP_CONSTRAINT = Join-Path $root $environment.voiceConstraints
    $env:PIP_INDEX_URL = $environment.pipIndex
    $env:AMADEUS_DOWNLOAD_HELPER = $downloadHelper
    & $python -X utf8 (Join-Path $root "tools\verify_deployment.py") --project-root $root --models-only > $null
    $env:AMADEUS_REPAIR_BASE_MODELS = $(if ($LASTEXITCODE -eq 0) { "0" } else { "1" })
    Invoke-Native -FilePath $python -Arguments @("-m", "pip", "install", "--index-url", $torchIndex, "torch==$($environment.torch.version)", "torchaudio==$($environment.torch.version)")
    Invoke-Native -FilePath $Conda -Arguments @(
        "run", "--no-capture-output", "--prefix", $voiceEnv,
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $gptRoot "install.ps1"),
        "-Device", $device,
        "-Source", $environment.gptSovits.modelSource
    )
    Install-PipRequirements -Python $python -Requirements (Join-Path $root "requirements-voice.txt")
    } finally {
        $env:PIP_CONSTRAINT = $previousConstraint
        $env:PIP_INDEX_URL = $previousIndex
        $env:AMADEUS_REPAIR_BASE_MODELS = $previousModelRepair
        $env:AMADEUS_DOWNLOAD_HELPER = $previousDownloadHelper
    }
}

function Ensure-Rhubarb {
    $rhubarbRoot = Join-Path (Join-Path $root ".tools\rhubarb") $environment.rhubarb.directory
    $rhubarbExe = Join-Path $rhubarbRoot "rhubarb.exe"
    if ((Test-Path -LiteralPath $rhubarbExe -PathType Leaf) -and -not $Force) { return }
    $downloadDir = Join-Path $root ".tmp\bootstrap"
    $archive = Join-Path $downloadDir "Rhubarb-Lip-Sync-1.14.0-Windows.zip"
    New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
    Invoke-WebRequest `
        -Uri $environment.rhubarb.url `
        -OutFile $archive `
        -UseBasicParsing
    $targetParent = Split-Path $rhubarbRoot -Parent
    New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $targetParent -Force
    if (-not (Test-Path -LiteralPath $rhubarbExe -PathType Leaf)) {
        throw "Rhubarb archive did not contain the expected executable: $rhubarbExe"
    }
}


function Ensure-Git {
    param([string]$Conda)
    if (Get-Command git -ErrorAction SilentlyContinue) { return }
    $gitEnv = Join-Path $root ".tools\git"
    Invoke-Native -FilePath $Conda -Arguments @("create", "--prefix", $gitEnv, "git", "-y", "--override-channels", "-c", $environment.condaChannel)
    $env:Path = "$(Join-Path $gitEnv 'Library\bin');$(Join-Path $gitEnv 'Library\usr\bin');$gitEnv;$env:Path"
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "Git installation did not provide git.exe" }
}

function Test-WebView2 {
    $client = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    foreach ($key in @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$client", "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$client")) {
        $version = (Get-ItemProperty -LiteralPath $key -Name pv -ErrorAction SilentlyContinue).pv
        if ($version -and $version -ne "0.0.0.0") { return $true }
    }
    return $false
}

function Ensure-WebView2 {
    if (Test-WebView2) { return }
    $installer = Join-Path $root ".tmp\bootstrap\MicrosoftEdgeWebview2Setup.exe"
    New-Item -ItemType Directory -Path (Split-Path $installer -Parent) -Force | Out-Null
    Invoke-WebRequest -Uri $environment.webView2Installer -OutFile $installer -UseBasicParsing
    $process = Start-Process -FilePath $installer -ArgumentList @("/silent", "/install") -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0 -or -not (Test-WebView2)) { throw "WebView2 installation was not completed" }
}

if ($VerifyOnly) {
    $python = Join-Path $desktopEnv "python.exe"
    if (-not (Test-Path -LiteralPath $python)) { throw "Run deployment first: desktop Python is missing" }
    Invoke-Native -FilePath $python -Arguments @("-X", "utf8", "tools\verify_deployment.py")
    exit 0
}

if (-not [Environment]::Is64BitOperatingSystem) { throw "Amadeus System requires 64-bit Windows" }
$selectedBackend = Resolve-Backend
Write-Host "Selected acceleration backend: $selectedBackend"
$conda = Find-Conda
if (-not $conda) { $conda = Install-ProjectMiniforge }
Ensure-Git -Conda $conda
Ensure-WebView2
Ensure-GptSovitsSource
$desktopPython = Ensure-CondaEnvironment -Conda $conda -Prefix $desktopEnv -PythonVersion $environment.python.desktop
if (-not (Test-PythonImports -Python $desktopPython -Modules @("webview", "PIL", "pypdf", "apscheduler", "dateutil", "ddgs", "certifi", "primp", "lxml", "cv2", "numpy", "onnxruntime", "tokenizers")) -or $Force) {
    Install-PipRequirements -Python $desktopPython -Requirements (Join-Path $root "requirements-deploy.txt")
}
Ensure-VoiceEnvironment -Conda $conda -SelectedBackend $selectedBackend
Ensure-Rhubarb
$assetArguments = @("-X", "utf8", "tools\install_deployment_assets.py")
if ($AssetArchiveDirectory) { $assetArguments += @("--archive-dir", $AssetArchiveDirectory) }
Invoke-Native -FilePath $desktopPython -Arguments $assetArguments
Invoke-Native -FilePath $desktopPython -Arguments @("-X", "utf8", "tools\verify_deployment.py")
Write-Host "Amadeus System deployment is ready. Enter your own API key in Settings on first launch."
if (-not $NoLaunch) { & (Join-Path $root "run_amadeus.ps1") }
