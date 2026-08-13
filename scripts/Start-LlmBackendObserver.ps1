[CmdletBinding()]
param(
    [string] $ToolkitCommand = 'llm-backend-toolkit',
    [string] $WindowTitle = '模型调用观察台',
    [string] $EdgePath = '',
    [string] $IconPath = '',
    [string] $AppUserModelId = 'Wly.LlmBackendToolkit.Observer',
    [string] $ObserverJson = '',
    [switch] $NoLaunch,
    [switch] $ShowErrors,
    [switch] $PassThru
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

if ([string]::IsNullOrWhiteSpace($IconPath)) {
    $IconPath = Join-Path (
        Split-Path -Parent $PSScriptRoot
    ) 'assets\observer-console.ico'
}
if (-not (Test-Path -LiteralPath $IconPath -PathType Leaf)) {
    throw "观察台任务栏图标不存在：$IconPath"
}
$IconPath = (Resolve-Path -LiteralPath $IconPath).Path
if ($AppUserModelId.Length -gt 128 -or
    $AppUserModelId -notmatch '\A[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)+\z') {
    throw 'AppUserModelId 必须是不超过 128 字符的点分标识，且不能包含空格。'
}
if ($ToolkitCommand -match '[\r\n"]') {
    throw 'ToolkitCommand 不能包含引号或换行。'
}

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
    public sealed class ObserverWindowMatch
    {
        public int ProcessId { get; set; }
        public IntPtr WindowHandle { get; set; }
    }

    public static class ObserverWindowNativeMethods
    {
        [StructLayout(LayoutKind.Sequential, Pack = 4)]
        private struct PROPERTYKEY
        {
            public Guid formatId;
            public uint propertyId;

            public PROPERTYKEY(Guid formatId, uint propertyId)
            {
                this.formatId = formatId;
                this.propertyId = propertyId;
            }
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PROPVARIANT
        {
            public ushort valueType;
            public ushort reserved1;
            public ushort reserved2;
            public ushort reserved3;
            public IntPtr pointerValue;
            public int pointerValue2;

            public static PROPVARIANT FromString(string value)
            {
                return new PROPVARIANT
                {
                    valueType = 31,
                    pointerValue = Marshal.StringToCoTaskMemUni(value),
                    pointerValue2 = 0
                };
            }

            public string AsString()
            {
                return valueType == 31 && pointerValue != IntPtr.Zero
                    ? Marshal.PtrToStringUni(pointerValue)
                    : null;
            }

            public void Clear()
            {
                PROPVARIANT value = this;
                PropVariantClear(ref value);
                valueType = 0;
                reserved1 = 0;
                reserved2 = 0;
                reserved3 = 0;
                pointerValue = IntPtr.Zero;
                pointerValue2 = 0;
            }
        }

        [ComImport]
        [Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
        [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IPropertyStore
        {
            [PreserveSig] int GetCount(out uint propertyCount);
            [PreserveSig] int GetAt(uint propertyIndex, out PROPERTYKEY key);
            [PreserveSig] int GetValue(ref PROPERTYKEY key, out PROPVARIANT value);
            [PreserveSig] int SetValue(ref PROPERTYKEY key, ref PROPVARIANT value);
            [PreserveSig] int Commit();
        }

        private static readonly Guid AppUserModelFormatId =
            new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
        private static readonly PROPERTYKEY PKEY_AppUserModel_RelaunchIconResource =
            new PROPERTYKEY(AppUserModelFormatId, 3);
        private static readonly PROPERTYKEY PKEY_AppUserModel_ID =
            new PROPERTYKEY(AppUserModelFormatId, 5);

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

        [DllImport("shell32.dll")]
        private static extern int SHGetPropertyStoreForWindow(
            IntPtr windowHandle,
            ref Guid interfaceId,
            [MarshalAs(UnmanagedType.Interface)] out IPropertyStore propertyStore
        );

        [DllImport("ole32.dll")]
        private static extern int PropVariantClear(ref PROPVARIANT value);

        [DllImport("user32.dll")]
        private static extern bool PostMessage(
            IntPtr windowHandle,
            uint message,
            IntPtr wordParameter,
            IntPtr longParameter
        );

        public static ObserverWindowMatch[] FindWindowsByExactTitle(string expectedTitle)
        {
            var matches = new List<ObserverWindowMatch>();
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
                    matches.Add(new ObserverWindowMatch
                    {
                        ProcessId = (int)processId,
                        WindowHandle = windowHandle
                    });
                    return true;
                },
                IntPtr.Zero
            );
            return matches.ToArray();
        }

        public static int[] FindProcessIdsByExactTitle(string expectedTitle)
        {
            var processIds = new List<int>();
            foreach (var match in FindWindowsByExactTitle(expectedTitle))
            {
                processIds.Add(match.ProcessId);
            }
            return processIds.ToArray();
        }

        private static IPropertyStore GetWindowPropertyStore(IntPtr windowHandle)
        {
            Guid interfaceId = typeof(IPropertyStore).GUID;
            IPropertyStore propertyStore;
            int result = SHGetPropertyStoreForWindow(
                windowHandle,
                ref interfaceId,
                out propertyStore
            );
            if (result < 0)
            {
                throw new COMException("SHGetPropertyStoreForWindow failed.", result);
            }
            return propertyStore;
        }

        private static void SetString(
            IPropertyStore propertyStore,
            PROPERTYKEY key,
            string value
        )
        {
            PROPVARIANT propertyValue = PROPVARIANT.FromString(value);
            try
            {
                int result = propertyStore.SetValue(ref key, ref propertyValue);
                if (result < 0)
                {
                    throw new COMException(
                        "IPropertyStore.SetValue failed for property " + key.propertyId + ".",
                        result
                    );
                }
            }
            finally
            {
                propertyValue.Clear();
            }
        }

        private static string GetString(
            IPropertyStore propertyStore,
            PROPERTYKEY key
        )
        {
            PROPVARIANT propertyValue;
            int result = propertyStore.GetValue(ref key, out propertyValue);
            if (result < 0)
            {
                throw new COMException(
                    "IPropertyStore.GetValue failed for property " + key.propertyId + ".",
                    result
                );
            }
            try
            {
                return propertyValue.AsString();
            }
            finally
            {
                propertyValue.Clear();
            }
        }

        public static void SetWindowIdentity(
            IntPtr windowHandle,
            string appUserModelId,
            string iconResource
        )
        {
            IPropertyStore propertyStore = GetWindowPropertyStore(windowHandle);
            try
            {
                SetString(propertyStore, PKEY_AppUserModel_ID, appUserModelId);
                SetString(
                    propertyStore,
                    PKEY_AppUserModel_RelaunchIconResource,
                    iconResource
                );
                int commitResult = propertyStore.Commit();
                if (commitResult < 0)
                {
                    throw new COMException("IPropertyStore.Commit failed.", commitResult);
                }

                if (!String.Equals(
                        GetString(propertyStore, PKEY_AppUserModel_ID),
                        appUserModelId,
                        StringComparison.Ordinal) ||
                    !String.Equals(
                        GetString(propertyStore, PKEY_AppUserModel_RelaunchIconResource),
                        iconResource,
                        StringComparison.OrdinalIgnoreCase))
                {
                    throw new InvalidOperationException(
                        "Windows did not retain the observer taskbar identity."
                    );
                }
            }
            finally
            {
                if (Marshal.IsComObject(propertyStore))
                {
                    Marshal.FinalReleaseComObject(propertyStore);
                }
            }
        }

        public static void CloseWindow(IntPtr windowHandle)
        {
            PostMessage(windowHandle, 0x0010, IntPtr.Zero, IntPtr.Zero);
        }
    }
}
'@
    }

    $matches = [LlmBackendToolkit.ObserverWindowNativeMethods]::FindWindowsByExactTitle(
        $ExpectedTitle
    )
    foreach ($match in $matches) {
        try {
            $process = Get-Process -Id $match.ProcessId -ErrorAction Stop
            if ($process.ProcessName -eq 'msedge') {
                $process | Add-Member -MemberType NoteProperty `
                    -Name ObserverWindowHandle `
                    -Value ([IntPtr] $match.WindowHandle) -Force
                return $process
            }
        } catch {
            # 窗口进程可能在枚举期间退出；继续检查其他窗口。
        }
    }
    return $null
}

function Set-ObserverWindowIdentity {
    param(
        [Parameter(Mandatory)] $WindowProcess,
        [Parameter(Mandatory)][string] $Identity,
        [Parameter(Mandatory)][string] $TaskbarIcon
    )

    $windowHandle = [IntPtr] $WindowProcess.ObserverWindowHandle
    if ($windowHandle -eq [IntPtr]::Zero) {
        throw '观察台窗口缺少可验证的 Windows 句柄。'
    }
    [LlmBackendToolkit.ObserverWindowNativeMethods]::SetWindowIdentity(
        $windowHandle,
        $Identity,
        "$TaskbarIcon,0"
    )
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
        Set-ObserverWindowIdentity `
            -WindowProcess $existing `
            -Identity $AppUserModelId `
            -TaskbarIcon $IconPath
        Write-ObserverResult ([pscustomobject] @{
            status = 'already_running'
            url = [string] $contract.url
            observer_status = [string] $contract.status
            process_id = $existing.Id
            app_user_model_id = $AppUserModelId
            taskbar_icon = $IconPath
            taskbar_identity_applied = $true
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
            app_user_model_id = $AppUserModelId
            taskbar_icon = $IconPath
            taskbar_identity_applied = $false
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
    try {
        Set-ObserverWindowIdentity `
            -WindowProcess $windowProcess `
            -Identity $AppUserModelId `
            -TaskbarIcon $IconPath
    } catch {
        [LlmBackendToolkit.ObserverWindowNativeMethods]::CloseWindow(
            [IntPtr] $windowProcess.ObserverWindowHandle
        )
        throw
    }
    Write-ObserverResult ([pscustomobject] @{
        status = 'launched'
        url = [string] $contract.url
        observer_status = [string] $contract.status
        edge_path = $resolvedEdge
        process_id = $windowProcess.Id
        app_user_model_id = $AppUserModelId
        taskbar_icon = $IconPath
        taskbar_identity_applied = $true
        launched = $true
    })
} finally {
    if ($hasMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
