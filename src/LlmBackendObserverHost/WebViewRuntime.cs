using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace LlmBackendObserverHost;

internal static class WebViewRuntime
{
    internal static async Task InitializeAsync(
        WebView2 webView,
        Uri? allowedRoot,
        string profilePurpose = "runtime"
    )
    {
        string userDataRoot = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "LlmBackendToolkit",
            "ObserverHost",
            "WebView2",
            profilePurpose
        );
        Directory.CreateDirectory(userDataRoot);
        // WebView2 gives these process environment variables higher precedence
        // than the explicit CreateAsync arguments. This desktop has a machine
        // integration value used by Windows Search/Widgets, so clear only the
        // current host process overrides before creating our isolated profile.
        Environment.SetEnvironmentVariable(
            "WEBVIEW2_USER_DATA_FOLDER",
            null,
            EnvironmentVariableTarget.Process
        );
        Environment.SetEnvironmentVariable(
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
            null,
            EnvironmentVariableTarget.Process
        );
        Environment.SetEnvironmentVariable(
            "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER",
            null,
            EnvironmentVariableTarget.Process
        );
        Environment.SetEnvironmentVariable(
            "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE",
            null,
            EnvironmentVariableTarget.Process
        );
        try
        {
            // Never inherit WEBVIEW2_USER_DATA_FOLDER. Windows Search/Widgets may
            // already use that shared profile with different browser options,
            // which makes WebView2 fail with ERROR_INVALID_STATE (0x8007139F).
            // An owned profile also keeps this read-only host isolated from other
            // desktop WebView2 applications.
            CoreWebView2Environment environment =
                await CoreWebView2Environment.CreateAsync(
                    browserExecutableFolder: null,
                    userDataFolder: userDataRoot
                );
            await webView.EnsureCoreWebView2Async(environment);
        }
        catch (Exception exception)
        {
            throw new InvalidOperationException(
                "ensure_core_webview",
                exception
            );
        }
        CoreWebView2 core = webView.CoreWebView2;
        try
        {
            core.Settings.AreBrowserAcceleratorKeysEnabled = false;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.IsZoomControlEnabled = false;
            core.Settings.AreHostObjectsAllowed = false;
            core.NewWindowRequested += (_, eventArgs) => eventArgs.Handled = true;
            core.DownloadStarting += (_, eventArgs) => eventArgs.Cancel = true;
        }
        catch (Exception exception)
        {
            throw new InvalidOperationException(
                "configure_core_webview",
                exception
            );
        }

        if (allowedRoot is not null)
        {
            core.NavigationStarting += (_, eventArgs) =>
            {
                if (!IsAllowedNavigation(allowedRoot, eventArgs.Uri))
                {
                    eventArgs.Cancel = true;
                }
            };
            var navigationCompleted = new TaskCompletionSource<
                CoreWebView2NavigationCompletedEventArgs
            >(TaskCreationOptions.RunContinuationsAsynchronously);
            void OnNavigationCompleted(
                object? sender,
                CoreWebView2NavigationCompletedEventArgs eventArgs
            ) => navigationCompleted.TrySetResult(eventArgs);
            core.NavigationCompleted += OnNavigationCompleted;
            try
            {
                core.Navigate(allowedRoot.AbsoluteUri);
                CoreWebView2NavigationCompletedEventArgs result =
                    await navigationCompleted.Task.WaitAsync(
                        TimeSpan.FromSeconds(20)
                    );
                if (!result.IsSuccess)
                {
                    throw new InvalidOperationException(
                        $"navigation_failed:{result.WebErrorStatus}"
                    );
                }
            }
            catch (Exception exception)
            {
                throw new InvalidOperationException(
                    "navigate_loopback",
                    exception
                );
            }
            finally
            {
                core.NavigationCompleted -= OnNavigationCompleted;
            }
        }
    }

    private static bool IsAllowedNavigation(Uri allowedRoot, string value)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out Uri? candidate))
        {
            return false;
        }
        return candidate.IsLoopback
            && candidate.Scheme == allowedRoot.Scheme
            && candidate.Host == allowedRoot.Host
            && candidate.Port == allowedRoot.Port;
    }
}

internal static class HostDiagnostics
{
    internal static string DefaultFailurePath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "LlmBackendToolkit",
        "ObserverHost",
        "last-error.json"
    );

    internal static void WriteSuccess(
        string path,
        string stage,
        object? evidence = null
    )
    {
        Write(path, "ok", stage, null, evidence);
    }

    internal static void WriteFailure(string path, string stage, Exception exception)
    {
        Write(path, "failed", stage, exception, null);
    }

    private static void Write(
        string path,
        string status,
        string stage,
        Exception? exception,
        object? evidence
    )
    {
        try
        {
            string fullPath = Path.GetFullPath(path);
            string? directory = Path.GetDirectoryName(fullPath);
            if (String.IsNullOrWhiteSpace(directory))
            {
                return;
            }
            Directory.CreateDirectory(directory);
            Exception? rootException = exception?.GetBaseException();
            string message = exception?.Message ?? "";
            if (rootException is not null && rootException != exception)
            {
                message += $": {rootException.Message}";
            }
            if (message.Length > 500)
            {
                message = message[..500];
            }
            var payload = new
            {
                status,
                stage,
                exception_type = rootException?.GetType().FullName,
                hresult = rootException?.HResult,
                message,
                evidence,
                recorded_utc = DateTimeOffset.UtcNow,
            };
            string temporaryPath = fullPath + ".tmp";
            File.WriteAllText(
                temporaryPath,
                JsonSerializer.Serialize(payload),
                new System.Text.UTF8Encoding(false)
            );
            File.Move(temporaryPath, fullPath, overwrite: true);
        }
        catch
        {
            // Diagnostics must never create a second startup failure.
        }
    }
}

internal sealed class WebViewSelfTestForm : Form
{
    private readonly HostOptions _options;
    private readonly WebView2 _webView;
    private readonly Icon _windowIcon;

    internal bool Succeeded { get; private set; }

    internal WebViewSelfTestForm(HostOptions options)
    {
        _options = options;
        _windowIcon = new Icon(options.IconPath);
        Text = options.Title;
        Icon = _windowIcon;
        ShowInTaskbar = false;
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.Manual;
        Location = new Point(-32000, -32000);
        Size = new Size(800, 600);
        _webView = new WebView2 { Dock = DockStyle.Fill };
        Controls.Add(_webView);
        Shown += OnShown;
    }

    protected override bool ShowWithoutActivation => true;

    protected override CreateParams CreateParams
    {
        get
        {
            const int WsExToolWindow = 0x00000080;
            CreateParams parameters = base.CreateParams;
            parameters.ExStyle |= WsExToolWindow;
            return parameters;
        }
    }

    private async void OnShown(object? sender, EventArgs eventArgs)
    {
        Shown -= OnShown;
        try
        {
            WindowIdentity.ApplyWindowIdentity(
                Handle,
                _options.AppUserModelId,
                _options.IconPath,
                _windowIcon
            );
            await WebViewRuntime.InitializeAsync(
                _webView,
                allowedRoot: null,
                profilePurpose: "self-test"
            );
            Succeeded = true;
            HostDiagnostics.WriteSuccess(
                _options.SelfTestResultPath!,
                "host_runtime",
                new
                {
                    webview_ready = true,
                    process_identity_verified = true,
                    window_identity_verified = true,
                    window_icon_verified = true,
                    app_user_model_id = _options.AppUserModelId,
                }
            );
        }
        catch (Exception exception)
        {
            HostDiagnostics.WriteFailure(
                _options.SelfTestResultPath!,
                "host_runtime",
                exception
            );
        }
        finally
        {
            Close();
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _webView.Dispose();
            _windowIcon.Dispose();
        }
        base.Dispose(disposing);
    }
}
