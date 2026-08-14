using System.Diagnostics;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace LlmBackendObserverHost;

internal sealed record HostOptions(
    string Title,
    string AppUserModelId,
    string IconPath,
    Uri? Url,
    string? ToolkitCommand,
    string? SelfTestResultPath
);

internal static partial class Program
{
    private const string MutexName = @"Local\LlmBackendToolkitObserverHost";

    [GeneratedRegex(@"\A[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)+\z")]
    private static partial Regex AppUserModelIdPattern();

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            HostOptions options = ParseOptions(args);
            Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
            ApplicationConfiguration.Initialize();
            WindowIdentity.ApplyProcessIdentity(options.AppUserModelId);
            if (options.SelfTestResultPath is not null)
            {
                using var selfTest = new WebViewSelfTestForm(options);
                Application.Run(selfTest);
                return selfTest.Succeeded ? 0 : 2;
            }

            using var mutex = new Mutex(true, MutexName, out bool createdNew);
            if (!createdNew)
            {
                WindowIdentity.TryActivateExistingManagedWindow(options.Title);
                return 0;
            }
            Uri url = options.Url ?? StartObserver(options.ToolkitCommand!);
            Application.Run(new ObserverForm(options, url));
            return 0;
        }
        catch (Exception exception)
        {
            // Shortcut launches stay silent: no console and no detached error popup.
            HostDiagnostics.WriteFailure(
                HostDiagnostics.DefaultFailurePath,
                "startup",
                exception
            );
            return 1;
        }
    }

    private static HostOptions ParseOptions(string[] args)
    {
        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        for (int index = 0; index < args.Length; index += 2)
        {
            if (index + 1 >= args.Length || !args[index].StartsWith("--", StringComparison.Ordinal))
            {
                throw new ArgumentException("Invalid observer host arguments.");
            }
            if (!values.TryAdd(args[index], args[index + 1]))
            {
                throw new ArgumentException("Duplicate observer host argument.");
            }
        }

        string title = Required(values, "--title");
        string appUserModelId = Required(values, "--app-user-model-id");
        string iconPath = Path.GetFullPath(Required(values, "--icon"));
        if (
            title.Length > 120
            || !AppUserModelIdPattern().IsMatch(appUserModelId)
            || !File.Exists(iconPath)
        )
        {
            throw new ArgumentException("Invalid observer host identity.");
        }

        values.TryGetValue("--url", out string? rawUrl);
        values.TryGetValue("--toolkit-command", out string? toolkitCommand);
        values.TryGetValue("--self-test-result", out string? selfTestResultPath);
        string[] allowedNames =
        [
            "--title",
            "--app-user-model-id",
            "--icon",
            "--url",
            "--toolkit-command",
            "--self-test-result",
        ];
        if (values.Keys.Any(key => !allowedNames.Contains(key, StringComparer.Ordinal)))
        {
            throw new ArgumentException("Unknown observer host argument.");
        }
        bool isSelfTest = !String.IsNullOrWhiteSpace(selfTestResultPath);
        if (isSelfTest)
        {
            if (!String.IsNullOrWhiteSpace(rawUrl) ||
                !String.IsNullOrWhiteSpace(toolkitCommand))
            {
                throw new ArgumentException("Self-test cannot launch an observer source.");
            }
            selfTestResultPath = Path.GetFullPath(selfTestResultPath!);
        }
        else if (String.IsNullOrWhiteSpace(rawUrl) == String.IsNullOrWhiteSpace(toolkitCommand))
        {
            throw new ArgumentException("Exactly one observer source is required.");
        }
        Uri? url = rawUrl is null ? null : RequireLoopbackUrl(rawUrl);
        if (toolkitCommand is not null && (
            toolkitCommand.Length > 1024
            || toolkitCommand.IndexOfAny(['\r', '\n', '"']) >= 0
        ))
        {
            throw new ArgumentException("Invalid toolkit command.");
        }
        return new HostOptions(
            title,
            appUserModelId,
            iconPath,
            url,
            toolkitCommand,
            selfTestResultPath
        );
    }

    private static string Required(
        IReadOnlyDictionary<string, string> values,
        string name
    )
    {
        if (!values.TryGetValue(name, out string? value) || String.IsNullOrWhiteSpace(value))
        {
            throw new ArgumentException($"Missing {name}.");
        }
        return value;
    }

    private static Uri RequireLoopbackUrl(string value)
    {
        if (
            !Uri.TryCreate(value, UriKind.Absolute, out Uri? uri)
            || !uri.IsLoopback
            || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps)
        )
        {
            throw new ArgumentException("Observer URL must be loopback HTTP(S).");
        }
        return uri;
    }

    private static Uri StartObserver(string toolkitCommand)
    {
        ProcessStartInfo startInfo = CreateObserverStartInfo(toolkitCommand);
        using Process process = Process.Start(startInfo)
            ?? throw new InvalidOperationException("Observer command did not start.");
        Task<string> outputTask = process.StandardOutput.ReadToEndAsync();
        Task<string> errorTask = process.StandardError.ReadToEndAsync();
        if (!process.WaitForExit(30_000))
        {
            try { process.Kill(entireProcessTree: true); } catch { }
            throw new InvalidOperationException("Observer command failed.");
        }
        string output = outputTask.GetAwaiter().GetResult();
        _ = errorTask.GetAwaiter().GetResult();
        if (process.ExitCode != 0)
        {
            throw new InvalidOperationException("Observer command failed.");
        }

        try
        {
            using JsonDocument document = JsonDocument.Parse(output.Trim());
            JsonElement root = document.RootElement;
            string status = root.GetProperty("status").GetString() ?? "";
            string url = root.GetProperty("url").GetString() ?? "";
            if (status is "ok" or "already_running")
            {
                return RequireLoopbackUrl(url);
            }
        }
        catch (JsonException)
        {
            // A valid observer contract is a single JSON document.
        }
        throw new InvalidOperationException("Observer command returned no valid contract.");
    }

    private static ProcessStartInfo CreateObserverStartInfo(string toolkitCommand)
    {
        string executable = toolkitCommand;
        bool launchModuleWithPythonw = false;
        if (String.Equals(
            Path.GetFileName(toolkitCommand),
            "llm-backend-toolkit.exe",
            StringComparison.OrdinalIgnoreCase
        ))
        {
            string? toolkitDirectory = Path.GetDirectoryName(toolkitCommand);
            if (!String.IsNullOrWhiteSpace(toolkitDirectory))
            {
                string pythonw = Path.Combine(toolkitDirectory, "pythonw.exe");
                if (File.Exists(pythonw))
                {
                    executable = pythonw;
                    launchModuleWithPythonw = true;
                }
            }
        }

        var startInfo = new ProcessStartInfo
        {
            FileName = executable,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        if (launchModuleWithPythonw)
        {
            startInfo.ArgumentList.Add("-m");
            startInfo.ArgumentList.Add("llm_backend_toolkit");
        }
        startInfo.ArgumentList.Add("observer");
        startInfo.ArgumentList.Add("--no-open");
        return startInfo;
    }
}

internal sealed class ObserverForm : Form
{
    private readonly HostOptions _options;
    private readonly Uri _url;
    private readonly WebView2 _webView;
    private readonly Icon _windowIcon;

    internal ObserverForm(HostOptions options, Uri url)
    {
        _options = options;
        _url = url;
        string iconPath = options.IconPath;
        _windowIcon = new Icon(iconPath);
        // The launcher treats the exact title as the readiness signal. Keep
        // it absent until the loopback page has completed navigation.
        Text = "";
        Icon = _windowIcon;
        StartPosition = FormStartPosition.CenterScreen;
        MinimumSize = new Size(1120, 720);
        Size = new Size(1440, 900);
        WindowState = FormWindowState.Maximized;
        BackColor = Color.White;
        // Do not expose a blank or failed shell while WebView2 is starting.
        // The real window and its taskbar button become visible only after the
        // read-only loopback page is ready.
        Opacity = 0D;
        ShowInTaskbar = false;

        _webView = new WebView2
        {
            Dock = DockStyle.Fill,
            BackColor = Color.White,
        };
        Controls.Add(_webView);
        Shown += OnShown;
    }

    protected override void OnHandleCreated(EventArgs eventArgs)
    {
        base.OnHandleCreated(eventArgs);
        WindowIdentity.ApplyWindowIdentity(
            Handle,
            _options.AppUserModelId,
            _options.IconPath,
            _windowIcon
        );
    }

    private async void OnShown(object? sender, EventArgs args)
    {
        Shown -= OnShown;
        try
        {
            await WebViewRuntime.InitializeAsync(_webView, _url);
            Text = _options.Title;
            ShowInTaskbar = true;
            Opacity = 1D;
            BringToFront();
            Activate();
            WindowIdentity.TryActivateWindow(Handle);
        }
        catch (Exception exception)
        {
            HostDiagnostics.WriteFailure(
                HostDiagnostics.DefaultFailurePath,
                "webview_runtime",
                exception
            );
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
