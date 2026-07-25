[CmdletBinding()]
param(
    [string] $ToolkitCommand = 'llm-backend-toolkit',
    [string] $WindowTitle = '模型调用观察台',
    [string] $EdgePath = '',
    [string] $ObserverJson = '',
    [switch] $NoLaunch,
    [switch] $ShowErrors,
    [switch] $PassThru
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

trap {
    if ($ShowErrors) {
        try {
            Add-Type -AssemblyName PresentationFramework -ErrorAction Stop
            [void] [System.Windows.MessageBox]::Show(
                "模型调用观察台启动失败。`n`n$($_.Exception.Message)",
                '模型调用观察台',
                [System.Windows.MessageBoxButton]::OK,
                [System.Windows.MessageBoxImage]::Error
            )
        } catch {
            # 隐藏启动环境无法加载弹窗组件时，仍以非零退出码失败。
        }
    }
    Write-Error -ErrorRecord $_
    exit 1
}

function Resolve-EdgeExecutable {
    param([string] $RequestedPath)

    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        if (-not (Test-Path -LiteralPath $RequestedPath -PathType Leaf)) {
            throw "指定的 Edge 不存在：$RequestedPath"
        }
        return (Resolve-Path -LiteralPath $RequestedPath).Path
    }

    $candidates = @(
        $(if (${env:ProgramFiles(x86)}) {
            Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'
        }),
        $(if ($env:ProgramFiles) {
            Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'
        }),
        $(if ($env:LOCALAPPDATA) {
            Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\Application\msedge.exe'
        })
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw '未找到 Microsoft Edge。已检查 Program Files (x86)、Program Files 和 LOCALAPPDATA 的标准安装路径。'
}

function Get-ObserverContract {
    param(
        [string] $Command,
        [string] $InjectedJson
    )

    if ([string]::IsNullOrWhiteSpace($InjectedJson)) {
        $previousExitCode = $global:LASTEXITCODE
        $global:LASTEXITCODE = 0
        $rawOutput = @(& $Command observer --no-open)
        $commandSucceeded = $?
        $commandExitCode = $global:LASTEXITCODE
        $global:LASTEXITCODE = $previousExitCode
        if (-not $commandSucceeded -or $commandExitCode -ne 0) {
            throw "观察台命令启动失败，退出码：$commandExitCode"
        }
        $commandOutput = $rawOutput | Out-String
    } else {
        $commandOutput = $InjectedJson
    }

    try {
        $contract = $commandOutput | ConvertFrom-Json -Depth 20
    } catch {
        throw "观察台命令未返回有效 JSON：$($_.Exception.Message)"
    }

    if ($contract -isnot [pscustomobject] -or
        [string]::IsNullOrWhiteSpace([string] $contract.status) -or
        [string]::IsNullOrWhiteSpace([string] $contract.url)) {
        throw '观察台 JSON 必须包含非空的 status 和 url。'
    }
    if ([string] $contract.status -notin @('ok', 'already_running')) {
        throw "观察台未就绪，状态：$($contract.status)"
    }

    $uri = $null
    if (-not [Uri]::TryCreate([string] $contract.url, [UriKind]::Absolute, [ref] $uri) -or
        $uri.Scheme -notin @('http', 'https') -or
        -not $uri.IsLoopback) {
        throw '观察台 JSON 中的 url 必须是 loopback HTTP(S) 地址。'
    }

    return $contract
}

function Find-ObserverWindow {
    param([string] $ExpectedTitle)

    if (-not ('LlmBackendToolkit.ObserverWindowNativeMethods' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

namespace LlmBackendToolkit
{
    public static class ObserverWindowNativeMethods
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
    }
}
'@
    }

    $processIds = [LlmBackendToolkit.ObserverWindowNativeMethods]::FindProcessIdsByExactTitle(
        $ExpectedTitle
    )
    foreach ($processId in $processIds) {
        try {
            $process = Get-Process -Id $processId -ErrorAction Stop
            if ($process.ProcessName -eq 'msedge') {
                return $process
            }
        } catch {
            # 窗口进程可能在枚举期间退出；继续检查其他窗口。
        }
    }
    return $null
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

    $existing = Find-ObserverWindow -ExpectedTitle $WindowTitle
    if ($null -ne $existing) {
        Write-ObserverResult ([pscustomobject] @{
            status = 'already_running'
            url = [string] $contract.url
            observer_status = [string] $contract.status
            process_id = $existing.Id
            launched = $false
        })
        return
    }

    $resolvedEdge = Resolve-EdgeExecutable -RequestedPath $EdgePath
    if ($NoLaunch) {
        Write-ObserverResult ([pscustomobject] @{
            status = 'planned'
            url = [string] $contract.url
            observer_status = [string] $contract.status
            edge_path = $resolvedEdge
            launched = $false
        })
        return
    }

    $edgeProcess = Start-Process -FilePath $resolvedEdge -ArgumentList @("--app=$($contract.url)") -PassThru
    $windowProcess = $null
    $windowDeadline = [DateTime]::UtcNow.AddSeconds(15)
    while ([DateTime]::UtcNow -lt $windowDeadline) {
        $windowProcess = Find-ObserverWindow -ExpectedTitle $WindowTitle
        if ($null -ne $windowProcess) {
            break
        }
        Start-Sleep -Milliseconds 100
    }
    if ($null -eq $windowProcess) {
        throw 'Edge 已启动，但观察台窗口未在 15 秒内出现。'
    }
    Write-ObserverResult ([pscustomobject] @{
        status = 'launched'
        url = [string] $contract.url
        observer_status = [string] $contract.status
        edge_path = $resolvedEdge
        process_id = $windowProcess.Id
        launched = $true
    })
} finally {
    if ($hasMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
