[CmdletBinding()]
param(
    [string] $ProjectPath = '',
    [string] $OutputRoot = '',
    [switch] $ResolveOnly,
    [switch] $ValidateRuntime,
    [switch] $PassThru
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
    $ProjectPath = Join-Path (
        Split-Path -Parent $PSScriptRoot
    ) 'src\LlmBackendObserverHost\LlmBackendObserverHost.csproj'
}
if (-not (Test-Path -LiteralPath $ProjectPath -PathType Leaf)) {
    throw "Observer host project not found: $ProjectPath"
}
$ProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$projectDirectory = Split-Path -Parent $ProjectPath
$repositoryRoot = Split-Path -Parent (Split-Path -Parent $projectDirectory)
$sourceFiles = @(
    $ProjectPath
    Get-ChildItem -LiteralPath $projectDirectory -Filter '*.cs' -File |
        Sort-Object Name |
        Select-Object -ExpandProperty FullName
    (Join-Path $repositoryRoot 'assets\observer-console.ico')
)
foreach ($sourceFile in $sourceFiles) {
    if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) {
        throw "Observer host source is incomplete: $sourceFile"
    }
}

$fingerprintLines = foreach ($sourceFile in $sourceFiles) {
    $resolvedSource = (Resolve-Path -LiteralPath $sourceFile).Path
    $relativeName = [IO.Path]::GetRelativePath($repositoryRoot, $resolvedSource)
    $hash = (Get-FileHash -LiteralPath $resolvedSource -Algorithm SHA256).Hash
    "$relativeName=$hash"
}
$fingerprintBytes = [Text.Encoding]::UTF8.GetBytes(
    ($fingerprintLines -join "`n")
)
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $sourceHash = [Convert]::ToHexString(
        $sha256.ComputeHash($fingerprintBytes)
    ).ToLowerInvariant()
} finally {
    $sha256.Dispose()
}

if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw 'LOCALAPPDATA is unavailable.'
    }
    $OutputRoot = Join-Path $env:LOCALAPPDATA 'LlmBackendToolkit\ObserverHost'
}
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
$outputDirectory = Join-Path $OutputRoot $sourceHash.Substring(0, 16)
$hostPath = Join-Path $outputDirectory 'LlmBackendObserverHost.exe'
$iconPath = (Resolve-Path -LiteralPath (
    Join-Path $repositoryRoot 'assets\observer-console.ico'
)).Path
$selfTestPath = Join-Path $outputDirectory 'webview-self-test.json'
$built = $false

if (-not $ResolveOnly -and -not (Test-Path -LiteralPath $hostPath -PathType Leaf)) {
    $mutex = [Threading.Mutex]::new(
        $false,
        "Local\LlmBackendObserverHostBuild-$($sourceHash.Substring(0, 16))"
    )
    $hasMutex = $false
    try {
        try {
            $hasMutex = $mutex.WaitOne([TimeSpan]::FromMinutes(2))
        } catch [Threading.AbandonedMutexException] {
            $hasMutex = $true
        }
        if (-not $hasMutex) {
            throw 'Timed out waiting for the observer host build lock.'
        }
        if (-not (Test-Path -LiteralPath $hostPath -PathType Leaf)) {
            [void] (New-Item -ItemType Directory -Path $outputDirectory -Force)
            $publishOutput = @(
                & dotnet publish $ProjectPath `
                    --configuration Release `
                    --framework net8.0-windows10.0.17763.0 `
                    --runtime win-x64 `
                    --self-contained false `
                    --nologo `
                    --output $outputDirectory 2>&1
            )
            if ($LASTEXITCODE -ne 0) {
                throw (
                    "Observer host build failed.`n" +
                    (($publishOutput | ForEach-Object { [string] $_ }) -join "`n")
                )
            }
            if (-not (Test-Path -LiteralPath $hostPath -PathType Leaf)) {
                throw 'Observer host build completed without the expected executable.'
            }
            $built = $true
        }
    } finally {
        if ($hasMutex) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}

$selfTestStatus = 'not_run'
$selfTestStage = $null
$processIdentityVerified = $false
$windowIdentityVerified = $false
$windowIconVerified = $false
$webViewReady = $false
if (-not $ResolveOnly -and $ValidateRuntime) {
    $needsSelfTest = $true
    if (Test-Path -LiteralPath $selfTestPath -PathType Leaf) {
        try {
            $retainedSelfTest = Get-Content -LiteralPath $selfTestPath -Raw |
                ConvertFrom-Json
            $needsSelfTest = [string] $retainedSelfTest.status -cne 'ok'
        } catch {
            $needsSelfTest = $true
        }
    }
    if ($needsSelfTest) {
        $startInfo = [Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $hostPath
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        foreach ($argument in @(
            '--title', '模型调用观察台后台自检',
            '--app-user-model-id', 'Wly.LlmBackendToolkit.Observer.SelfTest',
            '--icon', $iconPath,
            '--self-test-result', $selfTestPath
        )) {
            $startInfo.ArgumentList.Add($argument)
        }
        $selfTestProcess = [Diagnostics.Process]::Start($startInfo)
        if ($null -eq $selfTestProcess) {
            throw 'Observer host self-test did not start.'
        }
        try {
            if (-not $selfTestProcess.WaitForExit(30000)) {
                $selfTestProcess.Kill()
                throw 'Observer host self-test timed out.'
            }
            if ($selfTestProcess.ExitCode -ne 0) {
                $diagnostic = if (Test-Path -LiteralPath $selfTestPath -PathType Leaf) {
                    Get-Content -LiteralPath $selfTestPath -Raw
                } else {
                    'no diagnostic result'
                }
                throw "Observer host self-test failed: $diagnostic"
            }
        } finally {
            $selfTestProcess.Dispose()
        }
    }
    $selfTestResult = Get-Content -LiteralPath $selfTestPath -Raw |
        ConvertFrom-Json
    $selfTestStatus = [string] $selfTestResult.status
    if ($selfTestStatus -cne 'ok') {
        throw 'Observer host self-test did not retain a passing result.'
    }
    $selfTestStage = [string] $selfTestResult.stage
    $processIdentityVerified = [bool] $selfTestResult.evidence.process_identity_verified
    $windowIdentityVerified = [bool] $selfTestResult.evidence.window_identity_verified
    $windowIconVerified = [bool] $selfTestResult.evidence.window_icon_verified
    $webViewReady = [bool] $selfTestResult.evidence.webview_ready
    if (-not ($processIdentityVerified -and $windowIdentityVerified -and
        $windowIconVerified -and $webViewReady)) {
        throw 'Observer host self-test did not prove its identity and WebView runtime.'
    }
}

$result = [pscustomobject] @{
    host_path = $hostPath
    output_directory = $outputDirectory
    source_hash = $sourceHash
    built = $built
    available = (Test-Path -LiteralPath $hostPath -PathType Leaf)
    self_test_status = $selfTestStatus
    self_test_stage = $selfTestStage
    process_identity_verified = $processIdentityVerified
    window_identity_verified = $windowIdentityVerified
    window_icon_verified = $windowIconVerified
    webview_ready = $webViewReady
}
if ($PassThru) {
    Write-Output $result
} else {
    Write-Output $hostPath
}
