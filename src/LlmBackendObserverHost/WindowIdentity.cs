using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace LlmBackendObserverHost;

internal static class WindowIdentity
{
    private const uint WmSetIcon = 0x0080;
    private const uint WmGetIcon = 0x007F;
    private const int IconSmall = 0;
    private const int IconBig = 1;
    private const uint GpsReadWrite = 0x00000002;
    private const int SwShowMaximized = 3;

    private static readonly Guid AppUserModelFormatId =
        new("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
    private static PropertyKey AppUserModelIdKey =>
        new(AppUserModelFormatId, 5);
    private static PropertyKey RelaunchIconResourceKey =>
        new(AppUserModelFormatId, 3);

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SetCurrentProcessExplicitAppUserModelID(string appId);

    [DllImport("shell32.dll")]
    private static extern int GetCurrentProcessExplicitAppUserModelID(out IntPtr appId);

    [DllImport("shell32.dll")]
    private static extern int SHGetPropertyStoreForWindow(
        IntPtr windowHandle,
        ref Guid interfaceId,
        [MarshalAs(UnmanagedType.Interface)] out IPropertyStore propertyStore
    );

    [DllImport("ole32.dll")]
    private static extern int PropVariantClear(ref PropVariant value);

    [DllImport("ole32.dll")]
    private static extern void CoTaskMemFree(IntPtr value);

    [DllImport("user32.dll")]
    private static extern IntPtr SendMessage(
        IntPtr windowHandle,
        uint message,
        IntPtr wordParameter,
        IntPtr longParameter
    );

    private delegate bool EnumWindowsCallback(IntPtr windowHandle, IntPtr parameter);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool EnumWindows(
        EnumWindowsCallback callback,
        IntPtr parameter
    );

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowText(
        IntPtr windowHandle,
        StringBuilder text,
        int maximumLength
    );

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(
        IntPtr windowHandle,
        out uint processId
    );

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsWindowVisible(IntPtr windowHandle);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ShowWindowAsync(IntPtr windowHandle, int command);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetForegroundWindow(IntPtr windowHandle);

    internal static bool TryActivateExistingManagedWindow(string title)
    {
        string managedRoot = Path.GetFullPath(
            Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "LlmBackendToolkit",
                "ObserverHost"
            )
        ).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        IntPtr match = IntPtr.Zero;
        EnumWindows((windowHandle, _) =>
        {
            if (!IsWindowVisible(windowHandle))
            {
                return true;
            }
            var windowTitle = new StringBuilder(256);
            if (GetWindowText(windowHandle, windowTitle, windowTitle.Capacity) <= 0 ||
                !String.Equals(windowTitle.ToString(), title, StringComparison.Ordinal))
            {
                return true;
            }
            GetWindowThreadProcessId(windowHandle, out uint processId);
            try
            {
                using Process process = Process.GetProcessById(checked((int)processId));
                string processPath = process.MainModule?.FileName ?? "";
                if (
                    processPath.StartsWith(managedRoot, StringComparison.OrdinalIgnoreCase) &&
                    String.Equals(
                        Path.GetFileName(processPath),
                        "LlmBackendObserverHost.exe",
                        StringComparison.OrdinalIgnoreCase
                    )
                )
                {
                    match = windowHandle;
                    return false;
                }
            }
            catch
            {
                // A candidate process may exit while top-level windows are enumerated.
            }
            return true;
        }, IntPtr.Zero);
        return match != IntPtr.Zero && TryActivateWindow(match);
    }

    internal static bool TryActivateWindow(IntPtr windowHandle)
    {
        _ = ShowWindowAsync(windowHandle, SwShowMaximized);
        return SetForegroundWindow(windowHandle);
    }

    internal static void ApplyProcessIdentity(string appUserModelId)
    {
        Marshal.ThrowExceptionForHR(
            SetCurrentProcessExplicitAppUserModelID(appUserModelId)
        );
        Marshal.ThrowExceptionForHR(
            GetCurrentProcessExplicitAppUserModelID(out IntPtr retainedPointer)
        );
        try
        {
            string retained = Marshal.PtrToStringUni(retainedPointer) ?? "";
            if (!String.Equals(retained, appUserModelId, StringComparison.Ordinal))
            {
                throw new InvalidOperationException(
                    "Windows did not retain the observer process identity."
                );
            }
        }
        finally
        {
            if (retainedPointer != IntPtr.Zero)
            {
                CoTaskMemFree(retainedPointer);
            }
        }
    }

    internal static void ApplyWindowIdentity(
        IntPtr windowHandle,
        string appUserModelId,
        string iconPath,
        Icon icon
    )
    {
        Guid interfaceId = typeof(IPropertyStore).GUID;
        Marshal.ThrowExceptionForHR(
            SHGetPropertyStoreForWindow(
                windowHandle,
                ref interfaceId,
                out IPropertyStore propertyStore
            )
        );
        try
        {
            SetString(propertyStore, AppUserModelIdKey, appUserModelId);
            SetString(
                propertyStore,
                RelaunchIconResourceKey,
                $"{iconPath},0"
            );
            Marshal.ThrowExceptionForHR(propertyStore.Commit());
            string retainedId = GetString(propertyStore, AppUserModelIdKey);
            string retainedIcon = GetString(
                propertyStore,
                RelaunchIconResourceKey
            );
            if (!String.Equals(retainedId, appUserModelId, StringComparison.Ordinal) ||
                !String.Equals(
                    retainedIcon,
                    $"{iconPath},0",
                    StringComparison.OrdinalIgnoreCase
                ))
            {
                throw new InvalidOperationException(
                    "Windows did not retain the observer window identity."
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

        SendMessage(windowHandle, WmSetIcon, new IntPtr(IconSmall), icon.Handle);
        SendMessage(windowHandle, WmSetIcon, new IntPtr(IconBig), icon.Handle);
        IntPtr retainedSmall = SendMessage(
            windowHandle,
            WmGetIcon,
            new IntPtr(IconSmall),
            IntPtr.Zero
        );
        IntPtr retainedBig = SendMessage(
            windowHandle,
            WmGetIcon,
            new IntPtr(IconBig),
            IntPtr.Zero
        );
        if (retainedSmall == IntPtr.Zero || retainedBig == IntPtr.Zero)
        {
            throw new InvalidOperationException(
                "Windows did not retain the observer window icon."
            );
        }
    }

    private static void SetString(
        IPropertyStore propertyStore,
        PropertyKey key,
        string value
    )
    {
        PropVariant propertyValue = PropVariant.FromString(value);
        try
        {
            Marshal.ThrowExceptionForHR(
                propertyStore.SetValue(ref key, ref propertyValue)
            );
        }
        finally
        {
            propertyValue.Clear();
        }
    }

    private static string GetString(
        IPropertyStore propertyStore,
        PropertyKey key
    )
    {
        Marshal.ThrowExceptionForHR(
            propertyStore.GetValue(ref key, out PropVariant propertyValue)
        );
        try
        {
            return propertyValue.AsString();
        }
        finally
        {
            propertyValue.Clear();
        }
    }

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    private struct PropertyKey
    {
        internal Guid FormatId;
        internal uint PropertyId;

        internal PropertyKey(Guid formatId, uint propertyId)
        {
            FormatId = formatId;
            PropertyId = propertyId;
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PropVariant
    {
        internal ushort ValueType;
        private ushort _reserved1;
        private ushort _reserved2;
        private ushort _reserved3;
        internal IntPtr PointerValue;
        private int _pointerValue2;

        internal static PropVariant FromString(string value)
        {
            return new PropVariant
            {
                ValueType = 31,
                PointerValue = Marshal.StringToCoTaskMemUni(value),
            };
        }

        internal readonly string AsString()
        {
            return ValueType == 31 && PointerValue != IntPtr.Zero
                ? Marshal.PtrToStringUni(PointerValue) ?? ""
                : "";
        }

        internal void Clear()
        {
            PropVariant value = this;
            PropVariantClear(ref value);
            ValueType = 0;
            PointerValue = IntPtr.Zero;
        }
    }

    [ComImport]
    [Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPropertyStore
    {
        [PreserveSig]
        int GetCount(out uint propertyCount);

        [PreserveSig]
        int GetAt(uint propertyIndex, out PropertyKey key);

        [PreserveSig]
        int GetValue(ref PropertyKey key, out PropVariant value);

        [PreserveSig]
        int SetValue(ref PropertyKey key, ref PropVariant value);

        [PreserveSig]
        int Commit();
    }
}
