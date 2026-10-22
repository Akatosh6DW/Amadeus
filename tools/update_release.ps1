param(
    [Parameter(Mandatory = $true)][string]$InstallDirectory,
    [Parameter(Mandatory = $true)][string]$StagedRelease,
    [Parameter(Mandatory = $true)][string]$StagingDirectory,
    [int]$ParentPid = 0,
    [string]$ExecutableName = "AMADEUS.exe",
    [string]$LogPath = "",
    [string]$StatusPath = "",
    [string]$ProcessIds = "",
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-UpdateLog([string]$Message) {
    $line = "{0} {1}" -f ([DateTime]::UtcNow.ToString("o")), $Message
    try {
        if ($LogPath) {
            $log = [IO.Path]::GetFullPath($LogPath)
            $parent = Split-Path -Parent $log
            if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
            Add-Content -LiteralPath $log -Value $line -Encoding UTF8
        }
    } catch {
        # Updating must not fail solely because a diagnostic log is locked.
    }
}

function FullPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw "update path is empty" }
    return [IO.Path]::GetFullPath($Path)
}

function Test-Inside([string]$Root, [string]$Candidate) {
    try {
        $rootFull = (FullPath $Root).TrimEnd('\') + '\'
        $candidateFull = FullPath $Candidate
        return $candidateFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Assert-NotDriveRoot([string]$Path, [string]$Label) {
    $full = FullPath $Path
    $root = [IO.Path]::GetPathRoot($full)
    if ([StringComparer]::OrdinalIgnoreCase.Equals($full.TrimEnd('\'), $root.TrimEnd('\'))) {
        throw "$Label may not be a drive root"
    }
}

function Assert-Inside([string]$Root, [string]$Candidate, [string]$Label) {
    if (-not (Test-Inside $Root $Candidate)) {
        throw "$Label is outside its staging directory"
    }
}

function Write-UpdateStatus([bool]$Ok, [string]$Phase, [string]$ErrorMessage) {
    if ([string]::IsNullOrWhiteSpace($StatusPath)) { return }
    try {
        $status = FullPath $StatusPath
        $statusParent = Split-Path -Parent $status
        if ($statusParent) { New-Item -ItemType Directory -Path $statusParent -Force | Out-Null }
        $payload = [ordered]@{
            ok = $Ok
            phase = $Phase
            error = [string]$ErrorMessage
            updatedAt = [DateTime]::UtcNow.ToString("o")
        }
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($status, ($payload | ConvertTo-Json -Compress), $utf8)
    } catch {
        Write-UpdateLog "could not persist update status: $($_.Exception.Message)"
    }
}

function Get-ProcessSnapshot {
    try {
        return @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop | ForEach-Object {
            [pscustomobject]@{
                Id = [int]$_.ProcessId
                ParentId = [int]$_.ParentProcessId
                Path = [string]$_.ExecutablePath
                Created = [string]$_.CreationDate
            }
        })
    } catch {
        Write-UpdateLog "could not inspect process tree: $($_.Exception.Message)"
        try {
            return @(Get-Process -ErrorAction Stop | ForEach-Object {
                $path = ""
                try { $path = [string]$_.Path } catch { }
                [pscustomobject]@{
                    Id = [int]$_.Id
                    ParentId = 0
                    Path = $path
                    Created = ""
                }
            })
        } catch {
            return @()
        }
    }
}

function Get-ExplicitProcessSnapshot([string]$Root, [int[]]$Ids) {
    $records = @()
    foreach ($id in $Ids) {
        if ($id -le 0) { continue }
        try {
            $process = Get-Process -Id $id -ErrorAction Stop
            $path = [string]$process.Path
            if (Test-Inside $Root $path) {
                $records += [pscustomobject]@{
                    Id = [int]$process.Id
                    ParentId = 0
                    Path = $path
                    Created = ""
                }
            }
        } catch {
            # The process may have exited during the handoff.
        }
    }
    return @($records)
}

function Get-OwnProcessSnapshot([string]$Root, [int]$AnchorPid, [int[]]$ExplicitIds) {
    if ($AnchorPid -le 0) { return @() }
    $records = @(Get-ProcessSnapshot)
    $records += @(Get-ExplicitProcessSnapshot $Root $ExplicitIds)
    $byId = @{}
    foreach ($record in $records) { $byId[[int]$record.Id] = $record }
    $anchor = $byId[[int]$AnchorPid]
    if ($null -eq $anchor -or -not (Test-Inside $Root $anchor.Path)) { return @() }

    $ownedIds = New-Object 'System.Collections.Generic.HashSet[int]'
    $cursor = [int]$AnchorPid
    while ($cursor -gt 0) {
        $record = $byId[$cursor]
        if ($null -eq $record -or -not (Test-Inside $Root $record.Path)) { break }
        $null = $ownedIds.Add([int]$record.Id)
        $cursor = [int]$record.ParentId
    }
    # Explicit process IDs are supplied by the running application for
    # components whose parent relationship may be unavailable (for example
    # when WMI process inspection is denied).  They are trusted only after
    # their executable path has been verified inside this installation.
    foreach ($record in $records) {
        if ($ExplicitIds -contains [int]$record.Id -and (Test-Inside $Root $record.Path)) {
            $null = $ownedIds.Add([int]$record.Id)
        }
    }
    do {
        $changed = $false
        foreach ($record in $records) {
            if ($ownedIds.Contains([int]$record.ParentId) -and (Test-Inside $Root $record.Path)) {
                if ($ownedIds.Add([int]$record.Id)) { $changed = $true }
            }
        }
    } while ($changed)
    return @($records | Where-Object { $ownedIds.Contains([int]$_.Id) })
}

function Get-LiveOwnProcessSnapshot([string]$Root, [object[]]$Captured) {
    $records = @(Get-ProcessSnapshot)
    if (-not $Captured -or -not $records) { return @() }
    $capturedById = @{}
    foreach ($record in $Captured) {
        $capturedById[[int]$record.Id] = $record
    }
    $ownedIds = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($record in $records) {
        $captured = $capturedById[[int]$record.Id]
        if ($null -eq $captured) { continue }
        $samePath = [StringComparer]::OrdinalIgnoreCase.Equals([string]$record.Path, [string]$captured.Path)
        $sameCreation = [string]::IsNullOrWhiteSpace([string]$captured.Created) -or
            [string]$captured.Created -eq [string]$record.Created
        if ($samePath -and $sameCreation -and (Test-Inside $Root $record.Path)) {
            $null = $ownedIds.Add([int]$record.Id)
        }
    }
    do {
        $changed = $false
        foreach ($record in $records) {
            if ($ownedIds.Contains([int]$record.ParentId) -and (Test-Inside $Root $record.Path)) {
                if ($ownedIds.Add([int]$record.Id)) { $changed = $true }
            }
        }
    } while ($changed)
    return @($records | Where-Object { $ownedIds.Contains([int]$_.Id) })
}

function Request-GracefulProcessClose([object[]]$Records) {
    foreach ($record in $Records) {
        try {
            $process = Get-Process -Id ([int]$record.Id) -ErrorAction Stop
            if ($process.MainWindowHandle -ne 0) { $null = $process.CloseMainWindow() }
        } catch {
            # A process may have exited between the snapshot and the close request.
        }
    }
}

function Stop-OwnProcesses([object[]]$Records) {
    foreach ($record in $Records) {
        try { Stop-Process -Id ([int]$record.Id) -Force -ErrorAction Stop } catch { }
    }
}

function Start-RolledBackInstallation([string]$Install, [string]$Name) {
    if ($NoLaunch) { return }
    $executable = Join-Path $Install $Name
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        Write-UpdateLog "rolled-back executable is missing: $executable"
        return
    }
    try {
        Start-Process -FilePath $executable -WorkingDirectory $Install | Out-Null
        Write-UpdateLog "restarted rolled-back installation"
    } catch {
        Write-UpdateLog "could not restart rolled-back installation: $($_.Exception.Message)"
    }
}

function Assert-NoReparsePoint([string]$Root) {
    $all = @(Get-Item -LiteralPath $Root -Force)
    $all += @(Get-ChildItem -LiteralPath $Root -Recurse -Force -ErrorAction Stop)
    foreach ($item in $all) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "reparse points are not allowed in update staging: $($item.FullName)"
        }
    }
}

function Move-WithRetry([string]$Source, [string]$Destination) {
    $last = $null
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            Move-Item -LiteralPath $Source -Destination $Destination -Force
            return
        } catch {
            $last = $_
            Start-Sleep -Milliseconds 500
        }
    }
    throw "could not move update directory: $Source -> $Destination ($last)"
}

function Remove-SafeDirectory([string]$Path, [string]$AllowedParent) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Assert-Inside $AllowedParent $Path "cleanup path"
    Remove-Item -LiteralPath $Path -Recurse -Force
}

$install = FullPath $InstallDirectory
$staged = FullPath $StagedRelease
$staging = FullPath $StagingDirectory
$explicitProcessIds = @(
    ($ProcessIds -split ",") | ForEach-Object {
        $text = ([string]$_).Trim()
        if ($text) {
            try { [int]$text } catch { }
        }
    }
)
if ([string]::IsNullOrWhiteSpace($ExecutableName) -or [IO.Path]::GetFileName($ExecutableName) -ne $ExecutableName) {
    throw "executable name must be a file name"
}
Assert-NotDriveRoot $install "install directory"
Assert-NotDriveRoot $staging "staging directory"
if (-not (Test-Path -LiteralPath $staging -PathType Container)) { throw "staging directory does not exist" }
if ((Get-Item -LiteralPath $staging -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "staging directory may not be a reparse point"
}
Assert-Inside $staging $staged "staged release"
if (-not (Test-Path -LiteralPath $install -PathType Container)) { throw "install directory does not exist" }
if (-not (Test-Path -LiteralPath $staged -PathType Container)) { throw "staged release does not exist" }
if ((Get-Item -LiteralPath $install -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "install directory may not be a reparse point"
}
if ([IO.Path]::GetFileName($staged) -ne "AMADEUS") { throw "staged release root must be AMADEUS" }
Assert-NoReparsePoint $staged
$newExecutable = Join-Path $staged $ExecutableName
Assert-Inside $staged $newExecutable "staged executable"
if (-not (Test-Path -LiteralPath $newExecutable -PathType Leaf)) { throw "staged AMADEUS.exe is missing" }

$parent = Split-Path -Parent $install
$backup = Join-Path $parent (".amadeus-update-backup-" + [Guid]::NewGuid().ToString("N"))
$movedOld = $false
$movedNew = $false
$ownedProcesses = @(Get-OwnProcessSnapshot $install $ParentPid $explicitProcessIds)
if ($ParentPid -gt 0 -and -not $ownedProcesses) {
    throw "could not identify the Amadeus process tree for this installation"
}

try {
    Write-UpdateLog "waiting for parent process $ParentPid"
    if ($ParentPid -gt 0) {
        for ($attempt = 0; $attempt -lt 120; $attempt++) {
            if (-not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { break }
            Start-Sleep -Milliseconds 500
        }
        if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
            Request-GracefulProcessClose $ownedProcesses
            Start-Sleep -Seconds 2
            $ownedProcesses = @(Get-LiveOwnProcessSnapshot $install $ownedProcesses)
            if ($ownedProcesses) { Stop-OwnProcesses $ownedProcesses }
        }
        $deadline = [DateTime]::UtcNow.AddSeconds(12)
        while ($true) {
            $liveProcesses = @(Get-LiveOwnProcessSnapshot $install $ownedProcesses)
            if (-not $liveProcesses) { break }
            if ([DateTime]::UtcNow -ge $deadline) {
                Stop-OwnProcesses $liveProcesses
                Start-Sleep -Milliseconds 500
                $liveProcesses = @(Get-LiveOwnProcessSnapshot $install $ownedProcesses)
                if ($liveProcesses) { throw "Amadeus installation processes did not exit" }
                break
            }
            Request-GracefulProcessClose $liveProcesses
            Start-Sleep -Milliseconds 250
        }
    }

    Move-WithRetry $install $backup
    $movedOld = $true
    Move-WithRetry $staged $install
    $movedNew = $true
    $installedExecutable = Join-Path $install $ExecutableName
    if (-not (Test-Path -LiteralPath $installedExecutable -PathType Leaf)) {
        throw "new installation failed validation"
    }
    Write-UpdateLog "installed new release at $install"
    if (-not $NoLaunch) {
        Start-Process -FilePath $installedExecutable -WorkingDirectory $install | Out-Null
        Write-UpdateLog "launched $installedExecutable"
    }
    if (Test-Path -LiteralPath $backup) {
        try { Remove-Item -LiteralPath $backup -Recurse -Force } catch { Write-UpdateLog "backup cleanup deferred: $($_.Exception.Message)" }
    }
    Write-UpdateStatus $true "completed" ""
    Write-UpdateLog "update completed"
} catch {
    $failure = $_.Exception.Message
    Write-UpdateLog "update failed: $failure"
    $rollbackSucceeded = $false
    try {
        # A failed directory move can leave a destination behind even when the
        # move helper did not return successfully.  Remove it before restoring
        # the only backup we own; never touch a sibling outside the parent.
        if ($movedOld -and (Test-Path -LiteralPath $install)) {
            Remove-SafeDirectory $install $parent
        }
        if (Test-Path -LiteralPath $backup) {
            Move-WithRetry $backup $install
        }
        $rollbackSucceeded = Test-Path -LiteralPath (Join-Path $install $ExecutableName) -PathType Leaf
    } catch {
        $rollbackError = $_.Exception.Message
        Write-UpdateLog "rollback failed: $rollbackError"
        $failure = "${failure}; rollback failed: $rollbackError"
    }
    if ($rollbackSucceeded) {
        $failure = "${failure}; original release restored."
        Write-UpdateLog "rolled back original installation"
    }
    Write-UpdateStatus $false "failed" $failure
    if ($rollbackSucceeded) { Start-RolledBackInstallation $install $ExecutableName }
    throw
} finally {
    if (Test-Path -LiteralPath $staging) {
        try { Remove-SafeDirectory $staging $parent } catch { Write-UpdateLog "staging cleanup deferred: $($_.Exception.Message)" }
    }
}
