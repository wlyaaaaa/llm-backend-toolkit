[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $ShortcutName = '模型调用观察台.lnk',
    [string] $LauncherPath = (Join-Path $PSScriptRoot 'Start-LlmBackendObserver.ps1'),
    [string] $ToolkitCommand = 'llm-backend-toolkit',
    [string] $PowerShellPath = '',
    [string] $IconPath = '',
    [string] $DesktopPath = '',
    [string] $StartMenuPath = '',
    [switch] $NoCreate,
    [switch] $Remove,
    [switch] $PassThru,
    [Parameter(DontShow)]
    [scriptblock] $TestHook = $null
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

if ([IO.Path]::GetFileName($ShortcutName) -ne $ShortcutName) {
    throw 'ShortcutName 只能是文件名，不能包含目录。'
}
if ([IO.Path]::GetExtension($ShortcutName) -ine '.lnk') {
    $ShortcutName += '.lnk'
}

if ($NoCreate -and $Remove) {
    throw 'NoCreate 与 Remove 不能同时使用。'
}

if ($Remove) {
    try {
        $LauncherPath = [IO.Path]::GetFullPath($LauncherPath)
    } catch {
        throw "启动器路径无效：$LauncherPath"
    }
} else {
    if (-not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
        throw "启动器不存在：$LauncherPath"
    }
    $LauncherPath = (Resolve-Path -LiteralPath $LauncherPath).Path
}

if ($ToolkitCommand -match '[\r\n"]') {
    throw 'ToolkitCommand 不能包含引号或换行。'
}
if ([IO.Path]::IsPathRooted($ToolkitCommand)) {
    if (-not $Remove -and -not (Test-Path -LiteralPath $ToolkitCommand -PathType Leaf)) {
        throw "ToolkitCommand 不存在：$ToolkitCommand"
    }
    if (Test-Path -LiteralPath $ToolkitCommand -PathType Leaf) {
        $ToolkitCommand = (Resolve-Path -LiteralPath $ToolkitCommand).Path
    } else {
        $ToolkitCommand = [IO.Path]::GetFullPath($ToolkitCommand)
    }
} elseif ($ToolkitCommand -notmatch '\A[A-Za-z0-9_.-]+\z') {
    throw 'ToolkitCommand 必须是安全命令名或绝对可执行文件路径。'
}

if ([string]::IsNullOrWhiteSpace($PowerShellPath)) {
    $pwshCommand = Get-Command 'pwsh.exe' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $pwshCommand) {
        $PowerShellPath = $pwshCommand.Source
    } else {
        $PowerShellPath = Join-Path $PSHOME 'pwsh.exe'
    }
}
if (-not $Remove -and -not (Test-Path -LiteralPath $PowerShellPath -PathType Leaf)) {
    throw "未找到 PowerShell 7：$PowerShellPath"
}
if (Test-Path -LiteralPath $PowerShellPath -PathType Leaf) {
    $PowerShellPath = (Resolve-Path -LiteralPath $PowerShellPath).Path
} elseif ($Remove) {
    try {
        $PowerShellPath = [IO.Path]::GetFullPath($PowerShellPath)
    } catch {
        throw "PowerShell 路径无效：$PowerShellPath"
    }
}

if (-not $Remove -and [string]::IsNullOrWhiteSpace($IconPath)) {
    $IconPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'assets\observer-console.ico'
}
if (-not $Remove -and -not (Test-Path -LiteralPath $IconPath -PathType Leaf)) {
    throw "快捷方式图标不存在：$IconPath"
}
if (-not $Remove) {
    $IconPath = (Resolve-Path -LiteralPath $IconPath).Path
}

if ([string]::IsNullOrWhiteSpace($DesktopPath)) {
    $DesktopPath = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::DesktopDirectory,
        [Environment+SpecialFolderOption]::DoNotVerify
    )
}
if ([string]::IsNullOrWhiteSpace($DesktopPath)) {
    throw 'Windows Known Folder 未返回桌面路径。'
}
$DesktopPath = [IO.Path]::GetFullPath($DesktopPath)

if ([string]::IsNullOrWhiteSpace($StartMenuPath)) {
    $StartMenuPath = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::Programs,
        [Environment+SpecialFolderOption]::DoNotVerify
    )
}
if ([string]::IsNullOrWhiteSpace($StartMenuPath)) {
    throw 'Windows Known Folder 未返回当前用户开始菜单 Programs 路径。'
}
$StartMenuPath = [IO.Path]::GetFullPath($StartMenuPath)

$shortcutPath = Join-Path $DesktopPath $ShortcutName
$startMenuShortcutPath = Join-Path $StartMenuPath $ShortcutName
$shortcutTargets = @(
    [pscustomobject] @{ location = 'desktop'; directory = $DesktopPath; path = $shortcutPath },
    [pscustomobject] @{ location = 'start_menu'; directory = $StartMenuPath; path = $startMenuShortcutPath }
)
$shortcutResults = [Collections.Generic.List[object]]::new()
$description = '打开模型调用观察台'
$arguments = (
    "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden " +
    "-File `"$LauncherPath`" -ToolkitCommand `"$ToolkitCommand`" -ShowErrors"
)
$workingDirectory = Split-Path -Parent $LauncherPath
if (-not $Remove) {
    $desiredIconLocation = "$IconPath,0"
}

function Test-ShortcutCoreContract {
    param(
        [Parameter(Mandatory)] $Shortcut
    )

    return (
        [string]::Equals(
            $Shortcut.TargetPath,
            $PowerShellPath,
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        $Shortcut.Arguments -ceq $arguments -and
        [string]::Equals(
            $Shortcut.WorkingDirectory,
            $workingDirectory,
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        $Shortcut.Description -ceq $description
    )
}

function Test-ShortcutCurrentIcon {
    param(
        [Parameter(Mandatory)] $Shortcut
    )

    return [string]::Equals(
        ($Shortcut.IconLocation -replace ',\s*0$', ',0'),
        $desiredIconLocation,
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Get-ShortcutFileToken {
    param(
        [Parameter(Mandatory)]
        [string] $Path
    )

    $item = $null
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    } catch {
        if (
            $_.CategoryInfo.Category -eq
            [Management.Automation.ErrorCategory]::ObjectNotFound
        ) {
            return [pscustomobject] @{
                exists = $false
                full_name = $null
                length = $null
                creation_time_utc_ticks = $null
                last_write_time_utc_ticks = $null
                attributes = $null
                sha256 = $null
            }
        }
        throw "拒绝处理无法检查的快捷方式路径：$Path"
    }

    if ($item.PSIsContainer) {
        throw "拒绝处理目录；快捷方式目标必须是文件：$Path"
    }
    if (
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne
        0
    ) {
        throw "拒绝处理重解析点快捷方式路径：$Path"
    }

    $firstMetadata = [pscustomobject] @{
        full_name = $item.FullName
        length = [long] $item.Length
        creation_time_utc_ticks = [long] $item.CreationTimeUtc.Ticks
        last_write_time_utc_ticks = [long] $item.LastWriteTimeUtc.Ticks
        attributes = [long] $item.Attributes
    }

    try {
        $sha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    } catch {
        throw "快捷方式检查期间发生冲突，拒绝继续：$Path"
    }
    if (
        $item.PSIsContainer -or
        (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne
            0) -or
        -not [string]::Equals(
            $firstMetadata.full_name,
            $item.FullName,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        $firstMetadata.length -ne [long] $item.Length -or
        $firstMetadata.creation_time_utc_ticks -ne
            [long] $item.CreationTimeUtc.Ticks -or
        $firstMetadata.last_write_time_utc_ticks -ne
            [long] $item.LastWriteTimeUtc.Ticks -or
        $firstMetadata.attributes -ne [long] $item.Attributes
    ) {
        throw "快捷方式检查期间发生冲突，拒绝继续：$Path"
    }

    return [pscustomobject] @{
        exists = $true
        full_name = $firstMetadata.full_name
        length = $firstMetadata.length
        creation_time_utc_ticks = $firstMetadata.creation_time_utc_ticks
        last_write_time_utc_ticks = $firstMetadata.last_write_time_utc_ticks
        attributes = $firstMetadata.attributes
        sha256 = $sha256
    }
}

function Test-ShortcutFileTokenEqual {
    param(
        [Parameter(Mandatory)] $Expected,
        [Parameter(Mandatory)] $Actual
    )

    if ([bool] $Expected.exists -ne [bool] $Actual.exists) {
        return $false
    }
    if (-not $Expected.exists) {
        return $true
    }
    return (
        [string]::Equals(
            $Expected.full_name,
            $Actual.full_name,
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        $Expected.length -eq $Actual.length -and
        $Expected.creation_time_utc_ticks -eq $Actual.creation_time_utc_ticks -and
        $Expected.last_write_time_utc_ticks -eq $Actual.last_write_time_utc_ticks -and
        $Expected.attributes -eq $Actual.attributes -and
        $Expected.sha256 -ceq $Actual.sha256
    )
}

function Get-ShortcutPlan {
    param(
        [Parameter(Mandatory)] $Shell,
        [Parameter(Mandatory)] $Target,
        [switch] $ForRemoval
    )

    $initialToken = Get-ShortcutFileToken -Path $Target.path
    if (-not $initialToken.exists) {
        return [pscustomobject] @{
            target = $Target
            existed = $false
            action = $(if ($ForRemoval) { 'absent' } else { 'create' })
            file_token = $initialToken
        }
    }

    $shortcut = $null
    try {
        try {
            $shortcut = $Shell.CreateShortcut($Target.path)
        } catch {
            throw "拒绝处理无法解析且不能证明属于本工具的快捷方式：$($Target.path)"
        }

        if (-not (Test-ShortcutCoreContract -Shortcut $shortcut)) {
            throw "拒绝覆盖或删除不能证明属于本工具的快捷方式：$($Target.path)"
        }

        $action = $(
            if ($ForRemoval) {
                'remove'
            } elseif (Test-ShortcutCurrentIcon -Shortcut $shortcut) {
                'unchanged'
            } else {
                'update'
            }
        )
        $verifiedToken = Get-ShortcutFileToken -Path $Target.path
        if (-not (Test-ShortcutFileTokenEqual -Expected $initialToken -Actual $verifiedToken)) {
            throw "快捷方式检查期间发生冲突，拒绝继续：$($Target.path)"
        }

        return [pscustomobject] @{
            target = $Target
            existed = $true
            action = $action
            file_token = $verifiedToken
        }
    } finally {
        if ($null -ne $shortcut) {
            [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
        }
    }
}

function Assert-ShortcutPlanUnchanged {
    param(
        [Parameter(Mandatory)] $Shell,
        [Parameter(Mandatory)] $Plan,
        [switch] $ForRemoval
    )

    try {
        $freshPlan = Get-ShortcutPlan `
            -Shell $Shell `
            -Target $Plan.target `
            -ForRemoval:$ForRemoval
    } catch {
        throw (
            "快捷方式状态冲突，拒绝覆盖或删除：$($Plan.target.path)。" +
            $_.Exception.Message
        )
    }
    if (
        $freshPlan.action -cne $Plan.action -or
        -not (
            Test-ShortcutFileTokenEqual `
                -Expected $Plan.file_token `
                -Actual $freshPlan.file_token
        )
    ) {
        throw "快捷方式状态冲突，拒绝覆盖或删除：$($Plan.target.path)"
    }
}

function Invoke-ShortcutTestHook {
    param(
        [Parameter(Mandatory)]
        [string] $Phase,
        [Parameter(Mandatory)] $Plan
    )

    if ($null -ne $TestHook) {
        $null = & $TestHook $Phase $Plan.target.path $Plan.action
    }
}

if ($NoCreate) {
    foreach ($target in $shortcutTargets) {
        $shortcutResults.Add([pscustomobject] @{
            location = $target.location
            path = $target.path
            status = 'planned'
        })
    }
} else {
    # Inspect every destination before the first write or deletion. This keeps one
    # foreign destination from causing a half-completed desktop/start-menu change.
    $shortcutPlans = [Collections.Generic.List[object]]::new()
    $shell = New-Object -ComObject 'WScript.Shell'
    try {
        foreach ($target in $shortcutTargets) {
            if (Test-Path -LiteralPath $target.directory -PathType Leaf) {
                throw "拒绝处理被文件占用的快捷方式目录：$($target.directory)"
            }
            $shortcutPlans.Add(
                (Get-ShortcutPlan -Shell $shell -Target $target -ForRemoval:$Remove)
            )
        }
    } finally {
        if ($null -ne $shell) {
            [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
        }
    }

    foreach ($plan in $shortcutPlans) {
        Invoke-ShortcutTestHook -Phase 'after_preflight' -Plan $plan
    }

    # Revalidate the complete two-target plan immediately before the first
    # mutation so a late conflict cannot partially update the other location.
    $shell = New-Object -ComObject 'WScript.Shell'
    try {
        foreach ($plan in $shortcutPlans) {
            if (Test-Path -LiteralPath $plan.target.directory -PathType Leaf) {
                throw "快捷方式目录状态冲突，拒绝继续：$($plan.target.directory)"
            }
            Assert-ShortcutPlanUnchanged `
                -Shell $shell `
                -Plan $plan `
                -ForRemoval:$Remove
        }
    } finally {
        if ($null -ne $shell) {
            [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
        }
    }

    if ($Remove) {
        $shell = New-Object -ComObject 'WScript.Shell'
        try {
            foreach ($plan in $shortcutPlans) {
                $targetStatus = $plan.action
                if ($plan.action -eq 'remove') {
                    if ($PSCmdlet.ShouldProcess($plan.target.path, '删除模型调用观察台快捷方式')) {
                        Invoke-ShortcutTestHook `
                            -Phase 'before_target_mutation' `
                            -Plan $plan
                        Assert-ShortcutPlanUnchanged `
                            -Shell $shell `
                            -Plan $plan `
                            -ForRemoval
                        Remove-Item -LiteralPath $plan.target.path -Force
                        $targetStatus = 'removed'
                    } else {
                        $targetStatus = 'skipped'
                    }
                } else {
                    Assert-ShortcutPlanUnchanged `
                        -Shell $shell `
                        -Plan $plan `
                        -ForRemoval
                }
                $shortcutResults.Add([pscustomobject] @{
                    location = $plan.target.location
                    path = $plan.target.path
                    status = $targetStatus
                })
            }
        } finally {
            if ($null -ne $shell) {
                [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
            }
        }
    } else {
        $shell = New-Object -ComObject 'WScript.Shell'
        try {
            foreach ($plan in $shortcutPlans) {
                if ($plan.action -eq 'unchanged') {
                    Assert-ShortcutPlanUnchanged `
                        -Shell $shell `
                        -Plan $plan
                    $shortcutResults.Add([pscustomobject] @{
                        location = $plan.target.location
                        path = $plan.target.path
                        status = 'unchanged'
                    })
                    continue
                }

                if (-not (Test-Path -LiteralPath $plan.target.directory -PathType Container)) {
                    if ($PSCmdlet.ShouldProcess($plan.target.directory, '创建快捷方式目录')) {
                        [void] (New-Item -ItemType Directory -Path $plan.target.directory -Force)
                    } else {
                        $shortcutResults.Add([pscustomobject] @{
                            location = $plan.target.location
                            path = $plan.target.path
                            status = 'skipped'
                        })
                        continue
                    }
                }

                $targetStatus = 'skipped'
                if ($PSCmdlet.ShouldProcess($plan.target.path, '创建或更新模型调用观察台快捷方式')) {
                    Invoke-ShortcutTestHook `
                        -Phase 'before_target_mutation' `
                        -Plan $plan
                    Assert-ShortcutPlanUnchanged `
                        -Shell $shell `
                        -Plan $plan
                    $shortcut = $null
                    try {
                        $shortcut = $shell.CreateShortcut($plan.target.path)
                        $shortcut.TargetPath = $PowerShellPath
                        $shortcut.Arguments = $arguments
                        $shortcut.WorkingDirectory = $workingDirectory
                        $shortcut.Description = $description
                        $shortcut.IconLocation = $desiredIconLocation
                        $shortcut.Save()
                        $targetStatus = $(if ($plan.existed) { 'updated' } else { 'created' })
                    } finally {
                        if ($null -ne $shortcut) {
                            [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
                        }
                    }
                }

                $shortcutResults.Add([pscustomobject] @{
                    location = $plan.target.location
                    path = $plan.target.path
                    status = $targetStatus
                })
            }
        } finally {
            if ($null -ne $shell) {
                [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
            }
        }
    }
}

$statuses = @(
    foreach ($shortcutResult in $shortcutResults) {
        $shortcutResult.status
    }
)
if ($NoCreate) {
    $status = 'planned'
} elseif ($Remove) {
    $status = $(if ('removed' -in $statuses) { 'removed' } elseif ('skipped' -in $statuses) { 'skipped' } else { 'absent' })
} elseif ('created' -in $statuses) {
    $status = 'created'
} elseif ('updated' -in $statuses) {
    $status = 'updated'
} elseif ('skipped' -in $statuses) {
    $status = 'skipped'
} else {
    $status = 'unchanged'
}

$result = [pscustomobject] @{
    status = $status
    action = $(if ($Remove) { 'remove' } else { 'install' })
    shortcut_path = $shortcutPath
    start_menu_shortcut_path = $startMenuShortcutPath
    shortcut_paths = @($shortcutPath, $startMenuShortcutPath)
    shortcuts = @($shortcutResults)
    target_path = $(if ($Remove) { $null } else { $PowerShellPath })
    arguments = $(if ($Remove) { $null } else { $arguments })
    working_directory = $(if ($Remove) { $null } else { $workingDirectory })
    icon_path = $(if ($Remove) { $null } else { $IconPath })
}

if ($PassThru) {
    Write-Output $result
} else {
    switch ($result.status) {
        'created' { Write-Host '已创建桌面和开始菜单快捷方式。' }
        'updated' { Write-Host '已更新桌面和开始菜单快捷方式。' }
        'unchanged' { Write-Host '桌面和开始菜单快捷方式已是最新。' }
        'planned' { Write-Host '快捷方式测试通过；未写入桌面或开始菜单。' }
        'removed' { Write-Host '已删除桌面和开始菜单中的模型调用观察台快捷方式。' }
        'absent' { Write-Host '桌面和开始菜单中没有模型调用观察台快捷方式。' }
        default { Write-Host '未更改快捷方式。' }
    }
}
