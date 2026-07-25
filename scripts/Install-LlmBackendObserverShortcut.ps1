[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $ShortcutName = '模型调用观察台.lnk',
    [string] $LauncherPath = (Join-Path $PSScriptRoot 'Start-LlmBackendObserver.ps1'),
    [string] $ToolkitCommand = 'llm-backend-toolkit',
    [string] $PowerShellPath = '',
    [string] $IconPath = '',
    [string] $DesktopPath = '',
    [switch] $NoCreate,
    [switch] $PassThru
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

if (-not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
    throw "启动器不存在：$LauncherPath"
}
$LauncherPath = (Resolve-Path -LiteralPath $LauncherPath).Path
if ($ToolkitCommand -match '[\r\n"]') {
    throw 'ToolkitCommand 不能包含引号或换行。'
}
if ([IO.Path]::IsPathRooted($ToolkitCommand)) {
    if (-not (Test-Path -LiteralPath $ToolkitCommand -PathType Leaf)) {
        throw "ToolkitCommand 不存在：$ToolkitCommand"
    }
    $ToolkitCommand = (Resolve-Path -LiteralPath $ToolkitCommand).Path
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
if (-not (Test-Path -LiteralPath $PowerShellPath -PathType Leaf)) {
    throw "未找到 PowerShell 7：$PowerShellPath"
}
$PowerShellPath = (Resolve-Path -LiteralPath $PowerShellPath).Path

if ([string]::IsNullOrWhiteSpace($IconPath)) {
    $iconCandidates = @(
        $(if (${env:ProgramFiles(x86)}) {
            Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'
        }),
        $(if ($env:ProgramFiles) {
            Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'
        }),
        $(if ($env:LOCALAPPDATA) {
            Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\Application\msedge.exe'
        })
    ) | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_) -and
        (Test-Path -LiteralPath $_ -PathType Leaf)
    }
    $IconPath = @($iconCandidates)[0]
    if ([string]::IsNullOrWhiteSpace($IconPath)) {
        $IconPath = $PowerShellPath
    }
}
if (-not (Test-Path -LiteralPath $IconPath -PathType Leaf)) {
    throw "快捷方式图标不存在：$IconPath"
}
$IconPath = (Resolve-Path -LiteralPath $IconPath).Path

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
if (-not $NoCreate -and -not (Test-Path -LiteralPath $DesktopPath -PathType Container)) {
    throw "桌面目录不存在：$DesktopPath"
}

$shortcutPath = Join-Path $DesktopPath $ShortcutName
$arguments = (
    "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden " +
    "-File `"$LauncherPath`" -ToolkitCommand `"$ToolkitCommand`" -ShowErrors"
)
$result = [pscustomobject] @{
    status = $(if ($NoCreate) { 'planned' } else { 'created' })
    shortcut_path = $shortcutPath
    target_path = $PowerShellPath
    arguments = $arguments
    working_directory = (Split-Path -Parent $LauncherPath)
    icon_path = $IconPath
}

if (-not $NoCreate) {
    if ($PSCmdlet.ShouldProcess($shortcutPath, '创建模型调用观察台快捷方式')) {
        $shell = New-Object -ComObject 'WScript.Shell'
        $shortcut = $null
        try {
            $shortcut = $shell.CreateShortcut($shortcutPath)
            $shortcut.TargetPath = $PowerShellPath
            $shortcut.Arguments = $arguments
            $shortcut.WorkingDirectory = $result.working_directory
            $shortcut.Description = '打开模型调用观察台'
            $shortcut.IconLocation = "$IconPath,0"
            $shortcut.Save()
        } finally {
            if ($null -ne $shortcut) {
                [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
            }
            if ($null -ne $shell) {
                [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
            }
        }
    } else {
        $result.status = 'skipped'
    }
}

if ($PassThru) {
    Write-Output $result
} else {
    switch ($result.status) {
        'created' { Write-Host "已创建桌面快捷方式：$shortcutPath" }
        'planned' { Write-Host "快捷方式测试通过：$shortcutPath" }
        default { Write-Host "未创建快捷方式：$shortcutPath" }
    }
}
