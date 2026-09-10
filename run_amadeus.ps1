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
$desktopArguments = @(".\desktop_live2d.py")
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

Write-Host "Stopping stale AMADEUS desktop/server processes..."
$amadeusPorts = 5173..5212
$listeningPids = @()
try {
  $listeningPids = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalAddress -in @("127.0.0.1", "0.0.0.0", "::") -and $amadeusPorts -contains $_.LocalPort } |
    Select-Object -ExpandProperty OwningProcess -Unique
} catch {
  $listeningPids = @()
}

$targets = @()
try {
  $targets = @(
    Get-CimInstance Win32_Process -ErrorAction Stop |
      Where-Object {
        $cmd = [string]$_.CommandLine
        $isPython = ([string]$_.ExecutablePath) -match '\\pythonw?\.exe$'
        $isAmadeusScript = `
          $cmd -match 'desktop_live2d\.py' -or `
          $cmd -match '(^|[\\\s"])server\.py($|[\s"])' -or `
          $cmd -match 'gpt_sovits_tts_server\.py' -or `
          $cmd -match 'screen_vision_server\.py'
        ($isPython -and $isAmadeusScript) -or ($listeningPids -contains $_.ProcessId -and $isPython -and $isAmadeusScript)
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
  # shutdown. Rebuild the isolated profile only after stale AMADEUS processes
  # have stopped; otherwise a second -TestMode launch fails with file-in-use.
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
  Write-Host "AMADEUS test mode: clean isolated data at $testDataDir"
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
