[CmdletBinding()]
param(
    [string] $ToolkitCommand = 'llm-backend-toolkit',
    [string] $WindowTitle = '模型调用观察台',
    [string] $EdgePath = '',
    [string] $ObserverHostPath = '',
    [string] $IconPath = '',
    [string] $AppUserModelId = 'Wly.LlmBackendToolkit.Observer',
    [string] $ObserverJson = '',
    [switch] $NoLaunch,
    [switch] $ShowErrors,
    [switch] $PassThru
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$OutputEncoding = [Text.Encoding]::UTF8

# EdgePath and ShowErrors remain accepted for a compatibility-only transition.
# The native host never launches Edge and never creates a detached error popup.
$null = $EdgePath
$null = $ShowErrors

if ([string]::IsNullOrWhiteSpace($IconPath)) {
    $IconPath = Join-Path (
        Split-Path -Parent $PSScriptRoot
    ) 'assets\observer-console.ico'
}
if (-not (Test-Path -LiteralPath $IconPath -PathType Leaf)) {
    throw "观察台任务栏图标不存在：$IconPath"
}
$IconPath = (Resolve-Path -LiteralPath $IconPath).Path
if ($WindowTitle.Length -gt 120 -or [string]::IsNullOrWhiteSpace($WindowTitle)) {
    throw 'WindowTitle 必须是 1 到 120 个字符。'
}
if ($AppUserModelId.Length -gt 128 -or
    $AppUserModelId -notmatch '\A[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)+\z') {
    throw 'AppUserModelId 必须是不超过 128 字符的点分标识，且不能包含空格。'
}
if ($ToolkitCommand -match '[\r\n"]') {
    throw 'ToolkitCommand 不能包含引号或换行。'
}

function Get-ObserverContract {
    param(
        [Parameter(Mandatory)][string] $Command,
        [string] $InjectedJson
    )

    if ([string]::IsNullOrWhiteSpace($InjectedJson)) {
        $commandOutput = @(& $Command observer --no-open)
        $commandSucceeded = $?
        $commandExitCode = $LASTEXITCODE
        if (-not $commandSucceeded -or
            ($null -ne $commandExitCode -and $commandExitCode -ne 0)) {
            throw "观察台命令失败，退出代码：$commandExitCode"
        }
        $contractLine = $commandOutput |
            Where-Object { -not [string]::IsNullOrWhiteSpace([string] $_) } |
            Select-Object -Last 1
    } else {
        $contractLine = $InjectedJson
    }

    try {
        $contract = $contractLine | ConvertFrom-Json -Depth 20
    } catch {
        throw '观察台命令没有返回有效 JSON。'
    }
    if ($contract -isnot [pscustomobject] -or
        [string]::IsNullOrWhiteSpace([string] $contract.status) -or
        [string]::IsNullOrWhiteSpace([string] $contract.url)) {
        throw '观察台命令返回的 JSON 缺少 status 或 url。'
    }
    if ([string] $contract.status -notin @('ok', 'already_running')) {
        throw "观察台未就绪，状态：$($contract.status)"
    }

    $uri = $null
    if (-not [Uri]::TryCreate([string] $contract.url, [UriKind]::Absolute, [ref] $uri) -or
        -not $uri.IsLoopback -or
        $uri.Scheme -notin @('http', 'https')) {
        throw '观察台 URL 必须是 loopback HTTP(S) 地址。'
    }
    return $contract
}

function Resolve-ObserverHost {
    param(
        [string] $RequestedPath,
        [switch] $ResolveOnly
    )

    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        $fullPath = [IO.Path]::GetFullPath($RequestedPath)
        if (-not $ResolveOnly -and
            -not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
            throw "观察台原生宿主不存在：$fullPath"
        }
        if (Test-Path -LiteralPath $fullPath -PathType Leaf) {
            return (Resolve-Path -LiteralPath $fullPath).Path
        }
        return $fullPath
    }

    $builderPath = Join-Path $PSScriptRoot 'Build-LlmBackendObserverHost.ps1'
    if (-not (Test-Path -LiteralPath $builderPath -PathType Leaf)) {
        throw "观察台原生宿主构建器不存在：$builderPath"
    }
    $buildResult = & $builderPath `
        -ResolveOnly:$ResolveOnly `
        -ValidateRuntime:(-not $ResolveOnly) `
        -PassThru
    if ($buildResult -isnot [pscustomobject] -or
        [string]::IsNullOrWhiteSpace([string] $buildResult.host_path)) {
        throw '观察台原生宿主构建器没有返回有效路径。'
    }
    if (-not $ResolveOnly -and -not [bool] $buildResult.available) {
        throw '观察台原生宿主构建后仍不可用。'
    }
    return [string] $buildResult.host_path
}

function Initialize-ObserverWindowNativeMethods {
    if ('LlmBackendToolkit.ObserverHostWindowNativeMethods' -as [type]) {
        return
    }
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

namespace LlmBackendToolkit
{
    public static class ObserverHostWindowNativeMethods
    {
        private delegate bool EnumWindowsCallback(IntPtr windowHandle, IntPtr parameter);

        [DllImport("user32.dll")]
        private static extern bool EnumWindows(EnumWindowsCallback callback, IntPtr parameter);

        [DllImport("user32.dll")]
        private static extern int GetWindowTextLength(IntPtr windowHandle);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetWindowText(
            IntPtr windowHandle,
            StringBuilder text,
            int maximumCount
        );

        [DllImport("user32.dll")]
        private static extern bool IsWindowVisible(IntPtr windowHandle);

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(
            IntPtr windowHandle,
            out uint processId
        );

        [DllImport("user32.dll")]
        private static extern bool ShowWindowAsync(IntPtr windowHandle, int command);

        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr windowHandle);

        public static int[] FindProcessIdsByExactTitle(string expectedTitle)
        {
            var processIds = new List<int>();
            EnumWindows(
                delegate(IntPtr windowHandle, IntPtr parameter)
                {
                    if (!IsWindowVisible(windowHandle))
                    {
                        return true;
                    }
                    int length = GetWindowTextLength(windowHandle);
                    if (length <= 0)
                    {
                        return true;
                    }
                    var title = new StringBuilder(length + 1);
                    GetWindowText(windowHandle, title, title.Capacity);
                    if (!String.Equals(title.ToString(), expectedTitle, StringComparison.Ordinal))
                    {
                        return true;
                    }
                    uint processId;
                    GetWindowThreadProcessId(windowHandle, out processId);
                    processIds.Add((int)processId);
                    return true;
                },
                IntPtr.Zero
            );
            return processIds.ToArray();
        }

        public static bool ActivateProcessWindow(int expectedProcessId)
        {
            IntPtr match = IntPtr.Zero;
            EnumWindows(
                delegate(IntPtr windowHandle, IntPtr parameter)
                {
                    if (!IsWindowVisible(windowHandle))
                    {
                        return true;
                    }
                    uint processId;
                    GetWindowThreadProcessId(windowHandle, out processId);
                    if (processId == (uint) expectedProcessId)
                    {
                        match = windowHandle;
                        return false;
                    }
                    return true;
                },
                IntPtr.Zero
            );
            if (match == IntPtr.Zero)
            {
                return false;
            }
            ShowWindowAsync(match, 3);
            return SetForegroundWindow(match);
        }
    }
}
'@
}

function Find-ObserverWindow {
    param(
        [Parameter(Mandatory)][string] $ExpectedTitle,
        [Parameter(Mandatory)][string] $ExpectedHostPath
    )

    Initialize-ObserverWindowNativeMethods
    $expectedFullPath = [IO.Path]::GetFullPath($ExpectedHostPath)
    $expectedFileName = [IO.Path]::GetFileName($expectedFullPath)
    $expectedGeneration = Split-Path -Parent $expectedFullPath
    $expectedHostRoot = Split-Path -Parent $expectedGeneration
    foreach (
        $processId in
        [LlmBackendToolkit.ObserverHostWindowNativeMethods]::FindProcessIdsByExactTitle(
            $ExpectedTitle
        )
    ) {
        try {
            $process = Get-Process -Id $processId -ErrorAction Stop
            if ($process.ProcessName -cne 'LlmBackendObserverHost') {
                continue
            }
            $actualPath = [string] $process.Path
            if ([string]::IsNullOrWhiteSpace($actualPath)) {
                continue
            }
            $actualFullPath = [IO.Path]::GetFullPath($actualPath)
            $actualGeneration = Split-Path -Parent $actualFullPath
            $actualHostRoot = Split-Path -Parent $actualGeneration
            $isCurrentHost = [string]::Equals(
                $actualFullPath,
                $expectedFullPath,
                [StringComparison]::OrdinalIgnoreCase
            )
            $isOwnedPriorGeneration = (
                [string]::Equals(
                    [IO.Path]::GetFileName($actualFullPath),
                    $expectedFileName,
                    [StringComparison]::OrdinalIgnoreCase
                ) -and
                [string]::Equals(
                    $actualHostRoot,
                    $expectedHostRoot,
                    [StringComparison]::OrdinalIgnoreCase
                )
            )
            if ($isCurrentHost -or $isOwnedPriorGeneration) {
                return [pscustomobject] @{
                    process = $process
                    host_path = $actualFullPath
                    current_generation = $isCurrentHost
                }
            }
        } catch {
            # 窗口进程可能在枚举期间退出；继续检查其他窗口。
        }
    }
    return $null
}

function Start-ObserverHost {
    param(
        [Parameter(Mandatory)][string] $HostPath,
        [Parameter(Mandatory)][string] $Url
    )

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $HostPath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    foreach ($argument in @(
        '--url', $Url,
        '--title', $WindowTitle,
        '--app-user-model-id', $AppUserModelId,
        '--icon', $IconPath
    )) {
        $startInfo.ArgumentList.Add($argument)
    }
    return [Diagnostics.Process]::Start($startInfo)
}

function Write-ObserverResult {
    param([pscustomobject] $Result)

    if ($PassThru) {
        Write-Output $Result
        return
    }
    switch ($Result.status) {
        'already_running' { Write-Host '模型调用观察台已打开。' }
        'planned' { Write-Host "模型调用观察台已通过测试检查：$($Result.url)" }
        default { Write-Host "模型调用观察台已启动：$($Result.url)" }
    }
}

$contract = Get-ObserverContract -Command $ToolkitCommand -InjectedJson $ObserverJson
$resolvedHost = Resolve-ObserverHost -RequestedPath $ObserverHostPath -ResolveOnly:$NoLaunch
$mutex = [Threading.Mutex]::new($false, 'Local\LlmBackendToolkitObserverGui')
$hasMutex = $false

try {
    try {
        $hasMutex = $mutex.WaitOne([TimeSpan]::FromSeconds(15))
    } catch [Threading.AbandonedMutexException] {
        $hasMutex = $true
    }
    if (-not $hasMutex) {
        throw '等待模型调用观察台启动锁超时。'
    }

    $existing = Find-ObserverWindow -ExpectedTitle $WindowTitle -ExpectedHostPath $resolvedHost
    if ($null -ne $existing) {
        $null = [LlmBackendToolkit.ObserverHostWindowNativeMethods]::ActivateProcessWindow(
            $existing.process.Id
        )
        Write-ObserverResult ([pscustomobject] @{
            status = 'already_running'
            url = [string] $contract.url
            observer_status = [string] $contract.status
            host_path = [string] $existing.host_path
            process_id = $existing.process.Id
            app_user_model_id = $AppUserModelId
            taskbar_icon = $IconPath
            taskbar_identity_applied = $true
            native_window = $true
            current_host_generation = [bool] $existing.current_generation
            launched = $false
        })
        return
    }

    if ($NoLaunch) {
        Write-ObserverResult ([pscustomobject] @{
            status = 'planned'
            url = [string] $contract.url
            observer_status = [string] $contract.status
            host_path = $resolvedHost
            app_user_model_id = $AppUserModelId
            taskbar_icon = $IconPath
            taskbar_identity_applied = $false
            native_window = $true
            launched = $false
        })
        return
    }

    $hostProcess = Start-ObserverHost -HostPath $resolvedHost -Url ([string] $contract.url)
    $window = $null
    $windowDeadline = [DateTime]::UtcNow.AddSeconds(20)
    while ([DateTime]::UtcNow -lt $windowDeadline) {
        $window = Find-ObserverWindow -ExpectedTitle $WindowTitle -ExpectedHostPath $resolvedHost
        if ($null -ne $window) {
            break
        }
        if ($hostProcess.HasExited) {
            throw "观察台原生宿主启动失败，退出代码：$($hostProcess.ExitCode)"
        }
        Start-Sleep -Milliseconds 100
    }
    if ($null -eq $window) {
        if (-not $hostProcess.HasExited) {
            $hostProcess.Kill()
        }
        throw '观察台原生窗口未在 20 秒内出现。'
    }

    Write-ObserverResult ([pscustomobject] @{
        status = 'launched'
        url = [string] $contract.url
        observer_status = [string] $contract.status
        host_path = $resolvedHost
        process_id = $window.process.Id
        app_user_model_id = $AppUserModelId
        taskbar_icon = $IconPath
        taskbar_identity_applied = $true
        native_window = $true
        current_host_generation = [bool] $window.current_generation
        launched = $true
    })
} finally {
    if ($hasMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
