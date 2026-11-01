param(
    [ValidateSet('prepare', 'start', 'status', 'stop', 'panel')]
    [string]$Action = 'status',
    [switch]$HelpersOnly,
    [string]$RuntimeRoot = '',
    [string]$AppRoot = ''
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONNOUSERSITE = '1'

if ([string]::IsNullOrWhiteSpace($AppRoot)) {
    $AppRoot = Split-Path -Parent $PSScriptRoot
}
$AppRoot = [IO.Path]::GetFullPath($AppRoot)
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    $RuntimeRoot = Join-Path $AppRoot 'data\qq-agent'
}
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$pathsFile = Join-Path $RuntimeRoot 'paths.json'
$gatewayFile = Join-Path $RuntimeRoot 'gateway.json'
$processFile = Join-Path $RuntimeRoot 'processes.json'

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
        if ([IO.Path]::IsPathRooted($Value)) {
            return [IO.Path]::GetFullPath($Value)
        }
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

function Get-ManagedProcess {
    param($Entry, [string]$ExpectedPath = '')
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
    if ($ExpectedPath -and !(Test-SamePath $actualPath $ExpectedPath)) { return $null }
    if ($actualTicks -ne [long]$ticksValue) { return $null }
    return $process
}

function New-ProcessEntry {
    param($Process, [string]$ExpectedPath)
    $idValue = 0
    try { $idValue = [int]$Process.Id } catch { $idValue = 0 }
    $actual = $null
    if ($idValue -gt 0) {
        $actual = Get-Process -Id $idValue -ErrorAction SilentlyContinue
    }
    $pathValue = Get-ProcessPathSafe $actual
    if ([string]::IsNullOrWhiteSpace($pathValue)) { $pathValue = $ExpectedPath }
    $ticksValue = Get-StartTicksSafe $actual
    return [ordered]@{ id = $idValue; startTicks = $ticksValue; path = [string]$pathValue }
}

function Stop-RecordedProcess {
    param($Entry)
    $process = Get-ManagedProcess $Entry
    if ($null -eq $process) { return $false }
    try {
        if (!$process.HasExited) { $process.Kill() }
        $null = $process.WaitForExit(5000)
    } catch {
        throw "无法停止已记录的 QQ helper PID $($process.Id)：$($_.Exception.Message)"
    }
    return $true
}

function Save-HelperEntries {
    param([hashtable]$Entries)
    $record = [ordered]@{}
    foreach ($name in @('gateway', 'astrbot')) {
        if ($Entries.ContainsKey($name)) { $record[$name] = $Entries[$name] }
        else { $record[$name] = $null }
    }
    Write-JsonFile $processFile $record
}

function Load-HelperEntries {
    $record = Read-JsonFile $processFile
    $entries = @{}
    if ($null -ne $record) {
        foreach ($property in $record.PSObject.Properties) { $entries[$property.Name] = $property.Value }
    }
    return $entries
}

function Start-Helper {
    param([string]$Name, [string]$FilePath, [object[]]$Arguments, [hashtable]$Entries)
    $existing = Get-ManagedProcess $Entries[$Name] $FilePath
    if ($null -ne $existing) { return $Entries[$Name] }
    if (!(Test-Path -LiteralPath $FilePath -PathType Leaf)) {
        throw "找不到 QQ helper 可执行文件：$FilePath"
    }
    $stdout = Join-Path $RuntimeRoot "$Name.stdout.log"
    $stderr = Join-Path $RuntimeRoot "$Name.stderr.log"
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $AppRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $Entries[$Name] = New-ProcessEntry $process $FilePath
    return $Entries[$Name]
}

function Get-ConfiguredPaths {
    param($Paths)
    $pythonValue = Resolve-ConfiguredPath ([string](Get-ObjectValue $Paths 'python')) $AppRoot
    if ([string]::IsNullOrWhiteSpace($pythonValue)) {
        $pythonValue = Join-Path $AppRoot '.conda\qq-agent\python.exe'
        if (!(Test-Path -LiteralPath $pythonValue -PathType Leaf)) {
            $desktopPython = Join-Path $AppRoot '.conda\amadeus-desktop\python.exe'
            if (Test-Path -LiteralPath $desktopPython -PathType Leaf) { $pythonValue = $desktopPython }
        }
    }
    return [ordered]@{
        python = $pythonValue
        host = Resolve-ConfiguredPath ([string](Get-ObjectValue $Paths 'host_executable')) $AppRoot
        source = Resolve-ConfiguredPath ([string](Get-ObjectValue $Paths 'source')) $AppRoot
        dashboard = Resolve-ConfiguredPath ([string](Get-ObjectValue $Paths 'dashboard')) $AppRoot
    }
}

if ($Action -eq 'prepare') {
    $preparePaths = Read-JsonFile $pathsFile
    $prepareConfig = Get-ConfiguredPaths $preparePaths
    if (!(Test-Path -LiteralPath $prepareConfig.python -PathType Leaf)) {
        throw "未找到可用 Python：$($prepareConfig.python)。请先安装运行环境；QQ 不会由本工具下载。"
    }
    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
    $oldRuntime = $env:AMADEUS_QQ_RUNTIME_ROOT
    $oldApp = $env:AMADEUS_QQ_APP_ROOT
    $env:AMADEUS_QQ_RUNTIME_ROOT = $RuntimeRoot
    $env:AMADEUS_QQ_APP_ROOT = $AppRoot
    Push-Location $AppRoot
    try {
        & $prepareConfig.python -X utf8 -m integrations.qq.prepare
        if ($LASTEXITCODE) { throw 'QQ 配置初始化失败' }
    } finally {
        Pop-Location
        if ($null -eq $oldRuntime) { Remove-Item Env:AMADEUS_QQ_RUNTIME_ROOT -ErrorAction SilentlyContinue } else { $env:AMADEUS_QQ_RUNTIME_ROOT = $oldRuntime }
        if ($null -eq $oldApp) { Remove-Item Env:AMADEUS_QQ_APP_ROOT -ErrorAction SilentlyContinue } else { $env:AMADEUS_QQ_APP_ROOT = $oldApp }
    }
    exit
}

if ($Action -eq 'panel') {
    $panelFile = Join-Path $RuntimeRoot 'panel-link.txt'
    if (!(Test-Path -LiteralPath $panelFile -PathType Leaf)) { throw "找不到 QQ 面板链接：$panelFile" }
    $panelUrl = (Get-Content -LiteralPath $panelFile -Raw -Encoding UTF8).Trim()
    if ([string]::IsNullOrWhiteSpace($panelUrl)) { throw 'QQ 面板链接为空' }
    Start-Process -FilePath $panelUrl
    exit
}

$entries = Load-HelperEntries
if ($Action -eq 'stop') {
    foreach ($name in @('astrbot', 'gateway')) {
        if ($HelpersOnly -and $name -eq 'gateway') { continue }
        if (Stop-RecordedProcess $entries[$name]) { $entries[$name] = $null }
    }
    Save-HelperEntries $entries
    $napcatScript = Join-Path $PSScriptRoot 'qq_napcat.ps1'
    if (Test-Path -LiteralPath $napcatScript -PathType Leaf) {
        & $napcatScript -Action stop -RuntimeRoot $RuntimeRoot -AppRoot $AppRoot
    }
    Write-Output '已停止本脚本记录且仍能核对 PID、启动时间和路径的 QQ helper；普通 QQ 不会被关闭。'
    exit
}

if (!(Test-Path -LiteralPath $pathsFile -PathType Leaf)) {
    if ($Action -eq 'status') {
        Write-Output "QQ helper 尚未配置：$pathsFile"
        Write-Output "可选启用 QQ 时请自行安装 QQ/NapCat，并写入 paths.json；本工具不会下载 QQ。"
        exit
    }
    throw "找不到 QQ 配置：$pathsFile。请先配置 paths.json；本工具不会下载 QQ。"
}
if (!(Test-Path -LiteralPath $gatewayFile -PathType Leaf)) { throw "找不到 QQ 网关配置：$gatewayFile" }

$paths = Read-JsonFile $pathsFile
$configured = Get-ConfiguredPaths $paths
$gatewayConfig = Read-JsonFile $gatewayFile
$replyAgent = [string](Get-ObjectValue $gatewayConfig 'reply_agent')
if ([string]::IsNullOrWhiteSpace($replyAgent)) { $replyAgent = 'amadeus' }
$replyAgent = $replyAgent.ToLowerInvariant()
if ($replyAgent -notin @('amadeus', 'astrbot')) { throw "不支持的 QQ reply_agent：$replyAgent；可用值为 amadeus 或已有 astrbot" }

if ($Action -eq 'start') {
    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
    if ($replyAgent -eq 'astrbot') {
        if ([string]::IsNullOrWhiteSpace($configured.source)) { throw 'reply_agent=astrbot 必须在 paths.json 提供已有 source 路径' }
        $astrbotMain = Join-Path $configured.source 'main.py'
        if (!(Test-Path -LiteralPath $astrbotMain -PathType Leaf)) { throw "找不到已有 AstrBot source：$astrbotMain；本工具不会安装 AstrBot" }
        if (!(Test-Path -LiteralPath $configured.python -PathType Leaf)) { throw "找不到 AstrBot 使用的 Python：$($configured.python)" }
        $env:ASTRBOT_ROOT = Join-Path $RuntimeRoot 'astrbot'
        $astrbotArgs = @('-X', 'utf8', ('"' + $astrbotMain + '"'))
        if (-not [string]::IsNullOrWhiteSpace($configured.dashboard)) {
            $astrbotArgs += @('--webui-dir', ('"' + $configured.dashboard + '"'))
        }
        Start-Helper 'astrbot' $configured.python $astrbotArgs $entries | Out-Null
        Save-HelperEntries $entries
    } else {
        $entries['astrbot'] = $null
    }

    $hostExecutable = $configured.host
    if (-not [string]::IsNullOrWhiteSpace($hostExecutable)) {
        if (!(Test-Path -LiteralPath $hostExecutable -PathType Leaf)) { throw "找不到 paths.host_executable：$hostExecutable" }
        $gatewayArgs = @('--qq-helper', 'gateway', '--config', ('"' + $gatewayFile + '"'))
        Start-Helper 'gateway' $hostExecutable $gatewayArgs $entries | Out-Null
    } else {
        if (!(Test-Path -LiteralPath $configured.python -PathType Leaf)) { throw "找不到 QQ 网关 Python：$($configured.python)" }
        $gatewayArgs = @('-X', 'utf8', '-m', 'integrations.qq.gateway', '--config', ('"' + $gatewayFile + '"'))
        Start-Helper 'gateway' $configured.python $gatewayArgs $entries | Out-Null
    }
    Save-HelperEntries $entries
}

foreach ($name in @('astrbot', 'gateway')) {
    $expected = $null
    if ($name -eq 'gateway' -and $null -ne $configured) {
        if (-not [string]::IsNullOrWhiteSpace($configured.host)) { $expected = $configured.host } else { $expected = $configured.python }
    }
    $process = Get-ManagedProcess $entries[$name] $expected
    if ($null -ne $process) { $state = '进程运行中' } else { $state = '未运行' }
    Write-Output "$name : $state"
}
Write-Output "QQ helper 运行目录：$RuntimeRoot；应用目录：$AppRoot"
Write-Output 'AstrBot 仅在 reply_agent=astrbot 且 paths.source 已存在时启动；默认 reply_agent 为 amadeus。'
