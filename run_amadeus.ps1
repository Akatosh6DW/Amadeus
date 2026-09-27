[CmdletBinding()]
param(
  [Alias("Test")]
  [switch]$TestMode
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:HF_HOME = Join-Path $PSScriptRoot ".cache\huggingface"
$env:TORCH_HOME = Join-Path $PSScriptRoot ".cache\torch"
# Ordinary source use takes the real Persona Skill provider path.  An explicit
# operator override remains available for rollback and diagnostics.
if (-not $env:AMADEUS_PERSONA_CONTEXT_MODE) {
  $env:AMADEUS_PERSONA_CONTEXT_MODE = "on"
}
$nodeBin = Join-Path $PSScriptRoot ".conda\nodejs"
if (Test-Path (Join-Path $nodeBin "node.exe")) {
  $env:Path = "$nodeBin;$env:Path"
}

$previousRuntimeMode = $env:AMADEUS_RUNTIME_MODE
$previousDataDir = $env:AMADEUS_DATA_DIR
$desktopArguments = @(('"' + (Join-Path $PSScriptRoot "desktop_live2d.py") + '"'))
$testDataDir = $null
if ($TestMode) {
  $runtimeParent = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "data\runtime"))
  $testDataDir = [System.IO.Path]::GetFullPath((Join-Path $runtimeParent "interactive-test-mode"))
  if (-not $testDataDir.StartsWith($runtimeParent + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing unsafe test data path: $testDataDir"
  }
  $env:AMADEUS_RUNTIME_MODE = "test"
  $env:AMADEUS_DATA_DIR = $testDataDir
  $desktopArguments += "--test-mode"
}

function Test-ProcessBelongsToAmadeusInstallation {
  param($Process, [string]$ProjectRoot)
  $installationPrefix = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd("\") + "\"
  $executable = [string]$Process.ExecutablePath
  $commandLine = [string]$Process.CommandLine
  if ($executable -notmatch '\\pythonw?\.exe$') { return $false }
  if ($commandLine -notmatch '(desktop_live2d|gpt_sovits_tts_server|screen_vision_server|server)\.py') { return $false }
  # Separate installations must not stop one another. For a shared Python,
  # require the absolute script path; a relative path cannot prove ownership.
  if ($executable.StartsWith($installationPrefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
  foreach ($script in @("desktop_live2d.py", "server.py", "tools\gpt_sovits_tts_server.py", "tools\screen_vision_server.py")) {
    $scriptPath = Join-Path $installationPrefix $script
    if ($commandLine.IndexOf($scriptPath, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
  }
  return $false
}

Write-Host "Stopping stale Amadeus processes from this installation..."
$targets = @()
try {
  $targets = @(
    Get-CimInstance Win32_Process -ErrorAction Stop |
      Where-Object {
        Test-ProcessBelongsToAmadeusInstallation -Process $_ -ProjectRoot $PSScriptRoot
      }
  )
} catch {
  Write-Warning "Cannot inspect stale process command lines; continuing without CIM cleanup: $($_.Exception.Message)"
}

foreach ($proc in $targets) {
  try {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
    Write-Host "Stopped stale process $($proc.ProcessId): $($proc.CommandLine)"
  } catch {
    Write-Warning "Failed to stop stale process $($proc.ProcessId): $($_.Exception.Message)"
  }
}
Start-Sleep -Milliseconds 600

if ($TestMode) {
  # The previous test backend keeps its log handles open until process
  # shutdown. Rebuild the isolated profile only after stale Amadeus processes
  # have stopped; otherwise a second -TestMode launch fails with file-in-use.
  # Keep only connection preferences; chat, memory and task state still reset.
  # Keys remain in the ignored local data directory, never in source defaults.
  $testConnectionSettings = @{}
  $testSettingsPath = Join-Path $testDataDir "settings.json"
  if (Test-Path -LiteralPath $testSettingsPath) {
    $previousTestSettings = Get-Content -LiteralPath $testSettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($key in @("apiEndpoint", "apiModel", "apiKey", "apiNetworkMode", "apiProxyUrl")) {
      if ($null -ne $previousTestSettings.PSObject.Properties[$key]) {
        $testConnectionSettings[$key] = $previousTestSettings.$key
      }
    }
  }
  $removed = $false
  for ($attempt = 1; $attempt -le 5 -and -not $removed; $attempt++) {
    try {
      if (Test-Path -LiteralPath $testDataDir) {
        Remove-Item -LiteralPath $testDataDir -Recurse -Force
      }
      $removed = $true
    } catch {
      if ($attempt -ge 5) { throw }
      Start-Sleep -Milliseconds 250
    }
  }
  New-Item -ItemType Directory -Force -Path $testDataDir | Out-Null
  [System.IO.File]::WriteAllText(
    (Join-Path $testDataDir ".amadeus-test-data"),
    "interactive test mode`n",
    $utf8NoBom
  )
  if ($testConnectionSettings.Count -gt 0) {
    [System.IO.File]::WriteAllText(
      $testSettingsPath,
      ($testConnectionSettings | ConvertTo-Json -Depth 4),
      $utf8NoBom
    )
  }
  Write-Host "Amadeus test mode: clean isolated data at $testDataDir"
}

$desktopPythonw = Join-Path $PSScriptRoot ".conda\amadeus-desktop\pythonw.exe"
$pythonw = Get-Command pythonw -ErrorAction SilentlyContinue
try {
  if (Test-Path $desktopPythonw) {
    Start-Process -FilePath $desktopPythonw -ArgumentList $desktopArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
  } elseif ($pythonw) {
    Start-Process -FilePath $pythonw.Source -ArgumentList $desktopArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
  } else {
    Start-Process -FilePath "python" -ArgumentList $desktopArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
  }
} finally {
  if ($null -eq $previousRuntimeMode) { Remove-Item Env:AMADEUS_RUNTIME_MODE -ErrorAction SilentlyContinue }
  else { $env:AMADEUS_RUNTIME_MODE = $previousRuntimeMode }
  if ($null -eq $previousDataDir) { Remove-Item Env:AMADEUS_DATA_DIR -ErrorAction SilentlyContinue }
  else { $env:AMADEUS_DATA_DIR = $previousDataDir }
}
