param(
    [ValidateSet('start', 'status', 'stop', 'switch')]
    [string]$Action = 'status',
    [string]$RuntimeRoot = '',
    [string]$AppRoot = ''
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

if ([string]::IsNullOrWhiteSpace($AppRoot)) {
    $AppRoot = Split-Path -Parent $PSScriptRoot
}
$AppRoot = [IO.Path]::GetFullPath($AppRoot)
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    $RuntimeRoot = Join-Path $AppRoot 'data\qq-agent'
}
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$pathsFile = Join-Path $RuntimeRoot 'paths.json'
$processFile = Join-Path $RuntimeRoot 'napcat-processes.json'
$startErrorFile = Join-Path $RuntimeRoot 'napcat-start-error.json'
$napcatWork = Join-Path $RuntimeRoot 'napcat'
$legacyAssets = Join-Path $AppRoot '.runtime-assets\qq-agent'
$legacyClientRoot = [IO.Path]::GetFullPath((Join-Path $legacyAssets 'qqnt'))
$legacyShell = [IO.Path]::GetFullPath((Join-Path $legacyAssets 'napcat-shell'))

function Get-ObjectValue {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary] -and $Object.Contains($Name)) {
        return $Object[$Name]
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -ne $property) { return $property.Value }
    return $null
}

function Resolve-ConfiguredPath {
    param([string]$Value, [string]$BasePath)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    try {
        if ([IO.Path]::IsPathRooted($Value)) { return [IO.Path]::GetFullPath($Value) }
        return [IO.Path]::GetFullPath((Join-Path $BasePath $Value))
    } catch {
        return $null
    }
}

function Test-SamePath {
    param([string]$Left, [string]$Right)
    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) { return $false }
    try {
        return [string]::Equals(
            [IO.Path]::GetFullPath($Left).TrimEnd('\', '/'),
            [IO.Path]::GetFullPath($Right).TrimEnd('\', '/'),
            [StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Test-PathInside {
    param([string]$Path, [string]$Root)
    if ([string]::IsNullOrWhiteSpace($Path) -or [string]::IsNullOrWhiteSpace($Root)) { return $false }
    try {
        $fullPath = [IO.Path]::GetFullPath($Path)
        $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        return $fullPath.StartsWith($fullRoot, [StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Read-JsonFile {
    param([string]$Path)
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Write-JsonFile {
    param([string]$Path, $Value)
    $parent = Split-Path -Parent $Path
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 20), $utf8)
}

function Get-ProcessPathSafe {
    param($Process)
    try { return [string]$Process.Path } catch { return $null }
}

function Get-StartTicksSafe {
    param($Process)
    try { return [long]$Process.StartTime.ToUniversalTime().Ticks } catch { return [long]0 }
}

function Get-ProcessByNameSafe {
    param([string]$Name)
    try { return @(Get-Process -Name $Name -ErrorAction SilentlyContinue) } catch { return @() }
}

function Get-ManagedProcess {
    param($Entry)
    if ($null -eq $Entry) { return $null }
    $idValue = Get-ObjectValue $Entry 'id'
    $ticksValue = Get-ObjectValue $Entry 'startTicks'
    $recordPath = [string](Get-ObjectValue $Entry 'path')
    if ($null -eq $idValue -or [int]$idValue -le 0 -or [string]::IsNullOrWhiteSpace($recordPath)) { return $null }
    if ($null -eq $ticksValue -or [long]$ticksValue -le 0) { return $null }
    $process = Get-Process -Id ([int]$idValue) -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    $actualPath = Get-ProcessPathSafe $process
    $actualTicks = Get-StartTicksSafe $process
    if ([string]::IsNullOrWhiteSpace($actualPath) -or $actualTicks -le 0) { return $null }
    if (!(Test-SamePath $actualPath $recordPath)) { return $null }
    if ($actualTicks -ne [long]$ticksValue) { return $null }
    return $process
}

function New-ProcessEntry {
    param($Process, [string]$ExpectedPath)
    $idValue = 0
    try { $idValue = [int]$Process.Id } catch { $idValue = 0 }
    $actual = $null
    if ($idValue -gt 0) { $actual = Get-Process -Id $idValue -ErrorAction SilentlyContinue }
    $pathValue = Get-ProcessPathSafe $actual
    if ([string]::IsNullOrWhiteSpace($pathValue)) { $pathValue = $ExpectedPath }
    $ticksValue = Get-StartTicksSafe $actual
    return [ordered]@{ id = $idValue; startTicks = $ticksValue; path = [string]$pathValue }
}

function Stop-OneProcess {
    param($Process)
    if ($null -eq $Process) { return }
    try {
        if (!$Process.HasExited) { $Process.Kill() }
        $null = $Process.WaitForExit(5000)
    } catch {
        throw "无法停止已记录的 NapCat/QQ PID $($Process.Id)：$($_.Exception.Message)"
    }
}

function Load-ProcessRecords {
    $record = Read-JsonFile $processFile
    if ($null -eq $record) { return [ordered]@{ launcher = $null; qq = $null } }
    return $record
}

function Save-ProcessRecords {
    param($Record)
    $normalized = [ordered]@{
        launcher = Get-ObjectValue $Record 'launcher'
        qq = Get-ObjectValue $Record 'qq'
    }
    Write-JsonFile $processFile $normalized
}

function Get-LegacyPrivateProcesses {
    $result = @()
    foreach ($name in @('QQ', 'NapCatWinBootMain')) {
        foreach ($process in (Get-ProcessByNameSafe $name)) {
            $path = Get-ProcessPathSafe $process
            if ([string]::IsNullOrWhiteSpace($path)) { continue }
            $isPrivateQq = Test-PathInside $path $legacyClientRoot
            $isPrivateLauncher = Test-SamePath $path (Join-Path $legacyShell 'NapCatWinBootMain.exe')
            if ($isPrivateQq -or $isPrivateLauncher) { $result += $process }
        }
    }
    return $result
}

function Get-ManagedLiveProcesses {
    param($Record)
    $result = @()
    foreach ($name in @('launcher', 'qq')) {
        $process = Get-ManagedProcess (Get-ObjectValue $Record $name)
        if ($null -ne $process) { $result += $process }
    }
    foreach ($process in (Get-LegacyPrivateProcesses)) {
        if (-not ($result | Where-Object { $_.Id -eq $process.Id })) { $result += $process }
    }
    return $result
}

function Get-ManagedProcessIds {
    param($Record)
    $ids = @{}
    foreach ($process in (Get-ManagedLiveProcesses $Record)) { $ids[[int]$process.Id] = $true }
    return $ids
}

function Get-UnmanagedQqProcesses {
    param($Record)
    $managedIds = Get-ManagedProcessIds $Record
    $result = @()
    foreach ($process in (Get-ProcessByNameSafe 'QQ')) {
        if (!$managedIds.ContainsKey([int]$process.Id)) { $result += $process }
    }
    return $result
}

function Clear-StartError {
    if (Test-Path -LiteralPath $startErrorFile -PathType Leaf) {
        Remove-Item -LiteralPath $startErrorFile -Force
    }
}

function Write-QqAlreadyRunningError {
    $message = '请先退出正在运行的 QQ，再连接代管；Amadeus 不会自动关闭你的 QQ'
    Write-JsonFile $startErrorFile ([ordered]@{ code = 'QQ_ALREADY_RUNNING'; message = $message })
    return $message
}

function Resolve-NapCatPaths {
    param($Paths)
    $configuredClient = Resolve-ConfiguredPath ([string](Get-ObjectValue $Paths 'qq_client')) $AppRoot
    $configuredShell = Resolve-ConfiguredPath ([string](Get-ObjectValue $Paths 'napcat_shell')) $AppRoot
    $fallbackClient = [IO.Path]::GetFullPath((Join-Path $legacyClientRoot 'Files\QQ.exe'))
    $fallbackShell = $legacyShell
    $client = $null
    $shell = $null
    if ($configuredClient -and (Test-Path -LiteralPath $configuredClient -PathType Leaf)) { $client = $configuredClient }
    elseif (Test-Path -LiteralPath $fallbackClient -PathType Leaf) { $client = $fallbackClient }
    if ($configuredShell -and (Test-Path -LiteralPath $configuredShell -PathType Container)) { $shell = $configuredShell }
    elseif (Test-Path -LiteralPath $fallbackShell -PathType Container) { $shell = $fallbackShell }
    return [ordered]@{ qqClient = $client; shell = $shell }
}

function Assert-NapCatFiles {
    param($Resolved)
    if ([string]::IsNullOrWhiteSpace($Resolved.qqClient)) {
        throw '未找到 QQ.exe。请先自行安装 QQ，并在 data\qq-agent\paths.json 设置 paths.qq_client；本工具不会下载或安装 QQ。'
    }
    if ([string]::IsNullOrWhiteSpace($Resolved.shell)) {
        throw '未找到 NapCat Shell。请自行安装 NapCat Shell，并在 data\qq-agent\paths.json 设置 paths.napcat_shell；本工具不会下载组件。'
    }
    foreach ($name in @('NapCatWinBootMain.exe', 'NapCatWinBootHook.dll', 'loadNapCat.js', 'napcat.mjs', 'qqnt.json')) {
        $file = Join-Path $Resolved.shell $name
        if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "NapCat Shell 缺少 $name：$file" }
    }
}

function Set-AutoLoginAccountEmpty {
    $configDir = Join-Path $napcatWork 'config'
    $webConfig = Join-Path $configDir 'webui.json'
    if (!(Test-Path -LiteralPath $webConfig -PathType Leaf)) { return }
    $settings = Read-JsonFile $webConfig
    if ($null -eq $settings) { return }
    if ($settings -is [System.Collections.IDictionary]) { $settings['autoLoginAccount'] = '' }
    else { $settings | Add-Member -NotePropertyName autoLoginAccount -NotePropertyValue '' -Force }
    Write-JsonFile $webConfig $settings
}

function Clear-QuickLoginEnvironment {
    foreach ($name in @('NAPCAT_QUICK_ACCOUNT', 'NAPCAT_QUICK_PASSWORD', 'NAPCAT_QUICK_PASSWORD_MD5')) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}

function New-WebUiConfig {
    $configDir = Join-Path $napcatWork 'config'
    New-Item -ItemType Directory -Force -Path $configDir | Out-Null
    $webConfig = Join-Path $configDir 'webui.json'
    if (Test-Path -LiteralPath $webConfig -PathType Leaf) { return $webConfig }
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $token = [Convert]::ToBase64String($bytes)
    $settings = [ordered]@{ host = '127.0.0.1'; port = 6099; token = $token; autoLoginAccount = ''; disableWebUI = $false }
    Write-JsonFile $webConfig $settings
    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
    ('NapCat：http://127.0.0.1:6099' + [Environment]::NewLine + 'WebUI Token：' + $token) | Set-Content -LiteralPath (Join-Path $RuntimeRoot 'napcat-login.txt') -Encoding UTF8
    return $webConfig
}

function Find-NewQqProcess {
    param([string]$ExpectedPath, [hashtable]$BeforeIds)
    for ($index = 0; $index -lt 20; $index++) {
        foreach ($process in (Get-ProcessByNameSafe 'QQ')) {
            if ($BeforeIds.ContainsKey([int]$process.Id)) { continue }
            $path = Get-ProcessPathSafe $process
            if (Test-SamePath $path $ExpectedPath) { return $process }
        }
        Start-Sleep -Milliseconds 250
    }
    return $null
}

function Start-NapCat {
    param($Record, $Resolved)
    $unmanaged = @(Get-UnmanagedQqProcesses $Record)
    if ($unmanaged.Count -gt 0) {
        $message = Write-QqAlreadyRunningError
        throw $message
    }
    Assert-NapCatFiles $Resolved
    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
    $null = New-WebUiConfig
    $launcher = Join-Path $Resolved.shell 'NapCatWinBootMain.exe'
    $hook = Join-Path $Resolved.shell 'NapCatWinBootHook.dll'
    $env:NAPCAT_WORKDIR = $napcatWork
    $env:NAPCAT_PATCH_PACKAGE = Join-Path $Resolved.shell 'qqnt.json'
    $env:NAPCAT_LOAD_PATH = Join-Path $Resolved.shell 'loadNapCat.js'
    $env:NAPCAT_INJECT_PATH = $hook
    $env:NAPCAT_LAUNCHER_PATH = $launcher
    $env:NAPCAT_MAIN_PATH = Join-Path $Resolved.shell 'napcat.mjs'
    $beforeIds = @{}
    foreach ($process in (Get-ProcessByNameSafe 'QQ')) { $beforeIds[[int]$process.Id] = $true }
    $launcherProcess = Start-Process -FilePath $launcher -ArgumentList @((('"' + $Resolved.qqClient + '"')), (('"' + $hook + '"'))) -WorkingDirectory $Resolved.shell -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $RuntimeRoot 'napcat.stdout.log') -RedirectStandardError (Join-Path $RuntimeRoot 'napcat.stderr.log')
    $Record.launcher = New-ProcessEntry $launcherProcess $launcher
    $qqProcess = Find-NewQqProcess $Resolved.qqClient $beforeIds
    if ($null -ne $qqProcess) { $Record.qq = New-ProcessEntry $qqProcess $Resolved.qqClient }
    else { $Record.qq = [ordered]@{ id = 0; startTicks = 0; path = $Resolved.qqClient } }
    Save-ProcessRecords $Record
    Write-Output "已启动 NapCat 启动器；launcher PID：$($Record.launcher.id)，QQ PID：$($Record.qq.id)。请在 WebUI 自行扫码登录。"
}

if ($Action -in @('start', 'switch')) {
    Clear-StartError
}

$record = Load-ProcessRecords
if ($Action -eq 'status') {
    $startError = Read-JsonFile $startErrorFile
    if ($null -ne $startError -and (Get-ObjectValue $startError 'code') -eq 'QQ_ALREADY_RUNNING') {
        Write-Output (Get-ObjectValue $startError 'message')
    }
    $resolvedStatus = Resolve-NapCatPaths (Read-JsonFile $pathsFile)
    $live = @(Get-ManagedLiveProcesses $record)
    Write-Output "NapCat/QQ 托管进程数：$($live.Count)"
    Write-Output "QQ 路径：$($resolvedStatus.qqClient)"
    Write-Output "NapCat Shell：$($resolvedStatus.shell)"
    Write-Output "运行目录：$RuntimeRoot"
    exit
}

if ($Action -eq 'stop' -or $Action -eq 'switch') {
    $stoppedIds = @{}
    foreach ($name in @('launcher', 'qq')) {
        $process = Get-ManagedProcess (Get-ObjectValue $record $name)
        if ($null -ne $process -and !$stoppedIds.ContainsKey([int]$process.Id)) {
            Stop-OneProcess $process
            $stoppedIds[[int]$process.Id] = $true
        }
    }
    foreach ($process in (Get-LegacyPrivateProcesses)) {
        if (!$stoppedIds.ContainsKey([int]$process.Id)) {
            Stop-OneProcess $process
            $stoppedIds[[int]$process.Id] = $true
        }
    }
    $record.launcher = $null
    $record.qq = $null
    Save-ProcessRecords $record
    Write-Output '已停止已记录或旧版项目私有路径下的 NapCat/QQ；普通 QQ 未被关闭。'
    if ($Action -eq 'stop') { exit }
    Set-AutoLoginAccountEmpty
    Clear-QuickLoginEnvironment
    $Action = 'start'
}

if ($Action -eq 'start') {
    $live = @(Get-ManagedLiveProcesses $record)
    if ($live.Count -gt 0) {
        Write-Output "NapCat/QQ 已在托管中，未重复启动（进程数：$($live.Count)）。"
        exit
    }
    $paths = Read-JsonFile $pathsFile
    $resolved = Resolve-NapCatPaths $paths
    Start-NapCat $record $resolved
}
