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
$script:ProcessTreeInspectionUnavailable = $false
$script:ProcessInspectionUnavailable = $false
$script:UnverifiedExplicitProcessIds = @()

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
    if (-not $script:ProcessTreeInspectionUnavailable) {
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
            # WMI process inspection can be denied for the helper account.  Keep
            # the fallback for the initial ownership snapshot, but do not repeat
            # the slow failed query during the 250 ms liveness polling loop.
            $script:ProcessTreeInspectionUnavailable = $true
            Write-UpdateLog "could not inspect process tree: $($_.Exception.Message)"
        }
    }
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
        $script:ProcessInspectionUnavailable = $true
        return @()
    }
}

function Get-ExplicitProcessSnapshot([string]$Root, [int[]]$Ids, [object[]]$KnownRecords = @()) {
    $records = @()
    $knownById = @{}
    foreach ($known in $KnownRecords) {
        $knownById[[int]$known.Id] = $known
    }
    foreach ($id in $Ids) {
        if ($id -le 0) { continue }

        # The full process snapshot is the authoritative handoff capture when
        # available.  Preserve its parent and creation metadata instead of
        # replacing it with a Get-Process record that loses both fields.
        $known = $knownById[[int]$id]
        if ($null -ne $known -and -not [string]::IsNullOrWhiteSpace([string]$known.Path)) {
            if (Test-Inside $Root ([string]$known.Path)) {
                $created = [string]$known.Created
                if ([string]::IsNullOrWhiteSpace($created)) {
                    try {
                        $knownProcess = Get-Process -Id $id -ErrorAction Stop
                        $created = $knownProcess.StartTime.ToUniversalTime().ToString("o", [Globalization.CultureInfo]::InvariantCulture)
                    } catch {
                        # A fallback snapshot may not expose creation metadata;
                        # the live check will still require the same executable.
                    }
                }
                $records += [pscustomobject]@{
                    Id = [int]$known.Id
                    ParentId = [int]$known.ParentId
                    Path = [string]$known.Path
                    Created = $created
                }
            }
            continue
        }

        try {
            $process = Get-Process -Id $id -ErrorAction Stop
            $path = [string]$process.Path
            if ([string]::IsNullOrWhiteSpace($path)) {
                # A live explicit PID whose executable cannot be inspected is
                # unsafe to classify as either owned or external.
                $script:UnverifiedExplicitProcessIds += [int]$process.Id
                continue
            }
            if (Test-Inside $Root $path) {
                $created = ""
                try {
                    $created = $process.StartTime.ToUniversalTime().ToString("o", [Globalization.CultureInfo]::InvariantCulture)
                } catch {
                    # The later liveness check will reject a reused PID when
                    # creation metadata cannot be obtained.
                }
                $records += [pscustomobject]@{
                    Id = [int]$process.Id
                    ParentId = 0
                    Path = $path
                    Created = $created
                }
            }
        } catch {
            # The process may have exited during the handoff.
        }
    }
    return @($records)
}

function Test-CapturedProcessCreation([string]$CapturedCreated, [object]$Process) {
    if ([string]::IsNullOrWhiteSpace($CapturedCreated)) { return $true }
    try {
        try {
            $capturedTime = [System.Management.ManagementDateTimeConverter]::ToDateTime($CapturedCreated)
        } catch {
            $capturedTime = [DateTime]::Parse(
                $CapturedCreated,
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::AssumeUniversal
            )
        }
        $currentTime = $Process.StartTime
        $delta = [Math]::Abs(($currentTime.ToUniversalTime() - $capturedTime.ToUniversalTime()).TotalSeconds)
        return $delta -lt 1
    } catch {
        # If process creation cannot be verified, do not act on a reused PID.
        return $false
    }
}

function Get-LiveCapturedProcessSnapshot([string]$Root, [object[]]$Captured) {
    $records = @()
    foreach ($capturedRecord in $Captured) {
        try {
            $process = Get-Process -Id ([int]$capturedRecord.Id) -ErrorAction Stop
            $path = [string]$process.Path
            $samePath = [StringComparer]::OrdinalIgnoreCase.Equals($path, [string]$capturedRecord.Path)
            $sameCreation = Test-CapturedProcessCreation ([string]$capturedRecord.Created) $process
            if ($samePath -and $sameCreation -and (Test-Inside $Root $path)) {
                $records += [pscustomobject]@{
                    Id = [int]$process.Id
                    ParentId = [int]$capturedRecord.ParentId
                    Path = $path
                    Created = [string]$capturedRecord.Created
                }
            }
        } catch {
            # A process may have exited between polls or deny process metadata.
        }
    }
    return @($records)
}

function Get-OwnProcessSnapshot([string]$Root, [int]$AnchorPid, [int[]]$ExplicitIds) {
    if ($AnchorPid -le 0) { return @() }
    $script:UnverifiedExplicitProcessIds = @()
    $records = @(Get-ProcessSnapshot)
    $records += @(Get-ExplicitProcessSnapshot $Root $ExplicitIds $records)
    $byId = @{}
    foreach ($record in $records) { $byId[[int]$record.Id] = $record }
    $anchor = $byId[[int]$AnchorPid]

    $ownedIds = New-Object 'System.Collections.Generic.HashSet[int]'
    if ($null -ne $anchor -and (Test-Inside $Root $anchor.Path)) {
        $cursor = [int]$AnchorPid
        while ($cursor -gt 0) {
            $record = $byId[$cursor]
            if ($null -eq $record -or -not (Test-Inside $Root $record.Path)) { break }
            $null = $ownedIds.Add([int]$record.Id)
            $cursor = [int]$record.ParentId
        }
    }
    # Explicit process IDs are supplied by the running application for
    # components whose parent relationship may be unavailable (for example
    # when the backend exits before this helper takes its ownership snapshot).
    # They are trusted only after their executable path and, when available,
    # their creation time have been captured inside this installation.
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

    if ($script:UnverifiedExplicitProcessIds.Count -gt 0) {
        $ids = ($script:UnverifiedExplicitProcessIds | Sort-Object -Unique) -join ","
        throw "could not verify explicit Amadeus process path for PID(s): $ids"
    }

    # A process under the installation that is not reachable from the backend
    # anchor or a validated explicit handoff PID cannot safely be terminated.
    # Refuse the replacement rather than treating an unrelated process as ours.
    $unowned = @($byId.Values | Where-Object {
        (Test-Inside $Root $_.Path) -and -not $ownedIds.Contains([int]$_.Id)
    } | Sort-Object -Property Id -Unique)
    if ($unowned) {
        $ids = ($unowned | ForEach-Object { [int]$_.Id }) -join ","
        throw "could not verify live Amadeus process ownership for PID(s): $ids"
    }

    return @($records | Where-Object { $ownedIds.Contains([int]$_.Id) })
}

function Get-LiveOwnProcessSnapshot([string]$Root, [object[]]$Captured) {
    if (-not $Captured) { return @() }
    if ($script:ProcessTreeInspectionUnavailable) {
        return @(Get-LiveCapturedProcessSnapshot $Root $Captured)
    }
    $records = @(Get-ProcessSnapshot)
    if ($script:ProcessTreeInspectionUnavailable) {
        return @(Get-LiveCapturedProcessSnapshot $Root $Captured)
    }
    if (-not $records) { return @() }
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

$install = $null
$staged = $null
$staging = $null
$parent = $null
$backup = $null
$newExecutable = $null
$stagingCleanupAllowed = $false
$movedOld = $false
$movedNew = $false
$ownedProcesses = @()

try {
    $install = FullPath $InstallDirectory
    $staged = FullPath $StagedRelease
    $staging = FullPath $StagingDirectory
    # Keep the parent available for rollback and cleanup, but do not permit
    # staging deletion until all ownership and scope checks below succeed.
    $parent = Split-Path -Parent $install
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
    Assert-Inside $parent $staging "staging directory"
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
    # At this point both directories and their relationship have been
    # validated, so a later content/ownership failure may clean this staging
    # directory.  Keep the flag false for every earlier scope failure.
    $stagingCleanupAllowed = $true
    if (-not (Test-Path -LiteralPath $newExecutable -PathType Leaf)) { throw "staged AMADEUS.exe is missing" }

    $backup = Join-Path $parent (".amadeus-update-backup-" + [Guid]::NewGuid().ToString("N"))
    $ownedProcesses = @(Get-OwnProcessSnapshot $install $ParentPid $explicitProcessIds)
    if ($ParentPid -gt 0 -and -not $ownedProcesses) {
        if ($script:ProcessInspectionUnavailable) {
            throw "could not verify whether Amadeus installation processes remain"
        }
        if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
            throw "could not identify the Amadeus process tree for this installation"
        }
        Write-UpdateLog "parent process $ParentPid already exited and no installation process remains"
    }

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
        if ($movedOld -and $install -and (Test-Path -LiteralPath $install)) {
            Remove-SafeDirectory $install $parent
        }
        if ($backup -and (Test-Path -LiteralPath $backup)) {
            Move-WithRetry $backup $install
        }
        if ($install) {
            $rollbackSucceeded = Test-Path -LiteralPath (Join-Path $install $ExecutableName) -PathType Leaf
        }
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
    if ($stagingCleanupAllowed -and $staging -and $parent -and (Test-Path -LiteralPath $staging)) {
        try { Remove-SafeDirectory $staging $parent } catch { Write-UpdateLog "staging cleanup deferred: $($_.Exception.Message)" }
    }
}
