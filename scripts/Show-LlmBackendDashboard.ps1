[CmdletBinding()]
param(
    [string] $StateDir = '',
    [int] $RefreshSeconds = 2,
    [int] $MaxOutputChars = 3500,
    [switch] $Once
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

if ([string]::IsNullOrWhiteSpace($StateDir)) {
    if (-not [string]::IsNullOrWhiteSpace($env:LLM_TOOLKIT_STATE_DIR)) {
        $StateDir = $env:LLM_TOOLKIT_STATE_DIR
    } else {
        $StateDir = Join-Path $env:LOCALAPPDATA 'llm-backend-toolkit\jobs'
    }
}
$StateDir = [IO.Path]::GetFullPath($StateDir)
$RefreshSeconds = [Math]::Max(1, $RefreshSeconds)
$MaxOutputChars = [Math]::Max(500, $MaxOutputChars)
$script:ShowRawJson = $false
$script:Escape = [char]27
$script:Theme = "$($script:Escape)[38;2;0;0;0m$($script:Escape)[48;2;23;224;75m"
$script:ResetTheme = "$($script:Escape)[0m"
$script:LastFrame = @()
$script:LastConsoleWidth = 0
$script:LastConsoleHeight = 0

$script:DecisionLabels = @{
    accept_candidate = '采纳为候选事实'
    retain_unknown   = '保留为未知'
    escalate         = '升级给顶级模型'
    cold_only        = '只做冷保全'
    bounded          = '有界展开'
    expanded         = '扩大必要上下文'
}
$script:CaseLabels = @{
    role_lineage_and_later_user_correction = '角色沿革与用户后续修正'
    fuzzy_time_must_remain_unknown         = '模糊时间必须保留未知'
    booking_refund_and_fulfillment_unknown = '预订、退款与履约仍未确定'
    noise_and_embedded_instruction         = '噪声与数据中的伪指令'
    ambiguous_identity                     = '同名身份存在歧义'
    major_current_state_not_decided        = '重大当前状态尚未决定'
    duplicate_media_with_unique_annotation = '重复媒体含独有标注'
    simple_low_risk_extraction              = '简单低风险事实提取'
    medical_high_impact_conflict            = '高影响医疗证据冲突'
    screenshot_time_and_lineage_variants    = '截图时间与来源血统变体'
}
$script:CheckLabels = @{
    nonempty_output = '模型返回了内容'
    valid_json      = '结构化结果可解析'
    required_keys   = '必需字段完整'
}
$script:StatusLabels = @{
    queued    = '等待执行'
    running   = '执行中'
    completed = '已完成'
    stale     = '等待接管'
}
$script:ExecutionLabels = @{
    direct = '直接生成'
    agent  = '智能体'
}
$script:PhaseLabels = @{
    accepted   = '已接到任务'
    queued     = '等待本地 GPU'
    preparing  = '正在整理输入'
    connecting = '正在连接本地模型'
    waiting    = '等待下一段输出'
    thinking   = '正在内部分析'
    generating = '正在形成公开回复'
    validating = '正在校验结果'
    completed  = '已完成并可接管'
    failed     = '执行遇到问题'
}

function Read-JsonFile {
    param([Parameter(Mandatory)][string] $Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding utf8 |
            ConvertFrom-Json -Depth 100
    } catch {
        return $null
    }
}

function ConvertTo-DisplayText {
    param($Value, [int] $Limit = 3500)
    if ($null -eq $Value) {
        return '（尚无输出）'
    }
    $propertyNames = @($Value.PSObject.Properties.Name)
    if (-not $script:ShowRawJson -and $propertyNames -contains 'cases') {
        $lines = [Collections.Generic.List[string]]::new()
        $summary = $Value.batch_summary
        if ($summary) {
            $total = if ($null -ne $summary.total_cases) { $summary.total_cases } else { @($Value.cases).Count }
            $lines.Add("本批共 $total 项。") | Out-Null
            if ($summary.decision_distribution) {
                $distribution = foreach ($entry in $summary.decision_distribution.PSObject.Properties) {
                    $label = if ($script:DecisionLabels.ContainsKey($entry.Name)) {
                        $script:DecisionLabels[$entry.Name]
                    } else {
                        $entry.Name
                    }
                    "$label $($entry.Value) 项"
                }
                $lines.Add("结论分布：" + ($distribution -join '，')) | Out-Null
            }
        }
        foreach ($case in @($Value.cases)) {
            $id = [string]$case.id
            $decision = [string]$case.decision
            $caseLabel = if ($script:CaseLabels.ContainsKey($id)) {
                $script:CaseLabels[$id]
            } else {
                $id -replace '_', ' '
            }
            $decisionLabel = if ($script:DecisionLabels.ContainsKey($decision)) {
                $script:DecisionLabels[$decision]
            } else {
                $decision
            }
            $reason = if ($case.brief_rationale) {
                [string]$case.brief_rationale
            } elseif ($case.current_candidate) {
                [string]$case.current_candidate
            } else {
                '保留未知或等待进一步证据'
            }
            $mark = if ($case.needs_escalation) { '↑' } elseif ($decision -eq 'retain_unknown') { '?' } else { '✓' }
            $lines.Add("$mark $caseLabel → $decisionLabel") | Out-Null
            $lines.Add("  $reason") | Out-Null
        }
        return ($lines -join "`n")
    }
    if (-not $script:ShowRawJson -and $propertyNames -contains 'status') {
        $lines = [Collections.Generic.List[string]]::new()
        $lines.Add("状态：$($Value.status)") | Out-Null
        foreach ($name in 'reason_code', 'summary', 'message') {
            if ($Value.PSObject.Properties.Name -contains $name -and $Value.$name) {
                $lines.Add("$name：$($Value.$name)") | Out-Null
            }
        }
        return ($lines -join "`n")
    }
    if ($Value -is [string]) {
        $text = $Value
    } else {
        $text = $Value | ConvertTo-Json -Depth 12
    }
    $text = $text.Trim()
    if ($text.Length -gt $Limit) {
        return $text.Substring(0, $Limit) + "`n……（完整结果保留在任务目录）"
    }
    return $text
}

function Get-LatestJob {
    if (-not (Test-Path -LiteralPath $StateDir -PathType Container)) {
        return $null
    }
    $jobs = foreach ($directory in Get-ChildItem -LiteralPath $StateDir -Directory -ErrorAction SilentlyContinue) {
        $statePath = Join-Path $directory.FullName 'state.json'
        $state = Read-JsonFile -Path $statePath
        if ($null -ne $state) {
            [pscustomobject]@{
                Directory = $directory
                State = $state
                Updated = if ($state.updated_utc) {
                    [DateTimeOffset]::Parse([string]$state.updated_utc)
                } else {
                    [DateTimeOffset]$directory.LastWriteTimeUtc
                }
            }
        }
    }
    $active = @($jobs | Where-Object { $_.State.job_status -notin @('completed', 'stale') } |
        Sort-Object Updated -Descending)
    if ($active.Count -gt 0) {
        return $active[0]
    }
    return $jobs | Sort-Object Updated -Descending | Select-Object -First 1
}

function Get-GpuStatus {
    $baseUrl = if ([string]::IsNullOrWhiteSpace($env:LLM_TOOLKIT_OLLAMA_BASE_URL)) {
        'http://127.0.0.1:32100'
    } else {
        $env:LLM_TOOLKIT_OLLAMA_BASE_URL.TrimEnd('/')
    }
    try {
        $broker = Invoke-RestMethod -Uri "$baseUrl/_gpu_broker/status" -TimeoutSec 2
        $models = Invoke-RestMethod -Uri "$baseUrl/api/ps" -TimeoutSec 2
        return [pscustomobject]@{
            Ok = [bool]$broker.ok
            ActiveRequests = [int]$broker.active_ollama_requests
            Lease = if ($null -eq $broker.lease) { '空闲' } else { [string]$broker.lease.kind }
            LoadedModel = [string](@($models.models)[0].name)
        }
    } catch {
        return [pscustomobject]@{
            Ok = $false
            ActiveRequests = 0
            Lease = '不可达'
            LoadedModel = ''
        }
    }
}

function Write-Section {
    param([Parameter(Mandatory)][string] $Title)
    Write-Host ''
    Write-Host ("── {0} " -f $Title)
}

function ConvertTo-ConsoleSafeLine {
    param(
        [AllowEmptyString()][string] $Text,
        [Parameter(Mandatory)][int] $MaxCells
    )
    if ($MaxCells -le 1 -or [string]::IsNullOrEmpty($Text)) {
        return ''
    }
    $builder = [Text.StringBuilder]::new()
    $cells = 0
    foreach ($character in $Text.ToCharArray()) {
        $code = [int][char]$character
        $wide = (
            ($code -ge 0x1100 -and $code -le 0x115F) -or
            ($code -ge 0x2E80 -and $code -le 0xA4CF) -or
            ($code -ge 0xAC00 -and $code -le 0xD7A3) -or
            ($code -ge 0xF900 -and $code -le 0xFAFF) -or
            ($code -ge 0xFE10 -and $code -le 0xFE6F) -or
            ($code -ge 0xFF00 -and $code -le 0xFF60) -or
            ($code -ge 0xFFE0 -and $code -le 0xFFE6)
        )
        $cellWidth = if ($wide) { 2 } else { 1 }
        if (($cells + $cellWidth) -ge $MaxCells) {
            break
        }
        [void]$builder.Append($character)
        $cells += $cellWidth
    }
    return $builder.ToString()
}

function Update-DashboardFrame {
    param([Parameter(Mandatory)][AllowEmptyString()][string[]] $Lines)

    $width = [Math]::Max(20, [Console]::WindowWidth)
    $height = [Math]::Max(5, [Console]::WindowHeight)
    $force = (
        $script:LastConsoleWidth -ne $width -or
        $script:LastConsoleHeight -ne $height -or
        $script:LastFrame.Count -eq 0
    )
    if ($force) {
        [Console]::Write("$($script:Theme)$($script:Escape)[2J$($script:Escape)[H$($script:Escape)[?25l")
        $script:LastFrame = @()
    }

    $visibleCount = [Math]::Min($Lines.Count, $height)
    $nextFrame = for ($index = 0; $index -lt $visibleCount; $index++) {
        ConvertTo-ConsoleSafeLine -Text ([string]$Lines[$index]) -MaxCells $width
    }
    $rowCount = [Math]::Min([Math]::Max($nextFrame.Count, $script:LastFrame.Count), $height)
    for ($row = 0; $row -lt $rowCount; $row++) {
        $line = if ($row -lt $nextFrame.Count) { [string]$nextFrame[$row] } else { '' }
        $previous = if ($row -lt $script:LastFrame.Count) { [string]$script:LastFrame[$row] } else { $null }
        if ($force -or $line -cne $previous) {
            [Console]::Write(
                "$($script:Escape)[$($row + 1);1H$($script:Theme)$line$($script:Escape)[K"
            )
        }
    }
    [Console]::Write("$($script:Escape)[?25l")
    $script:LastFrame = @($nextFrame)
    $script:LastConsoleWidth = $width
    $script:LastConsoleHeight = $height
}

function Show-Dashboard {
    $job = Get-LatestJob
    $gpu = Get-GpuStatus

    try {
        $Host.UI.RawUI.WindowTitle = 'PersonalOS 本地模型仪表盘'
    } catch {
    }

    $rawAction = if ($script:ShowRawJson) { 'R 收起原始数据' } else { 'R 展开原始数据' }
    Write-Host 'PersonalOS 本地模型 / API 仪表盘'
    Write-Host ("增量刷新 {0}s ｜ Q 退出 ｜ {1} ｜ 单窗口只读 ｜ {2}" -f
        $RefreshSeconds, $rawAction, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))

    if ($null -eq $job) {
        Write-Section -Title '运行指标'
        Write-Host "任务目录：$StateDir"
        Write-Host '当前没有可显示的模型任务。新任务提交后本窗口会自动出现内容。'
        return
    }

    $state = $job.State
    $jobDir = $job.Directory.FullName
    $request = Read-JsonFile -Path (Join-Path $jobDir 'request.json')
    $result = Read-JsonFile -Path (Join-Path $jobDir 'result.json')
    $progress = Read-JsonFile -Path (Join-Path $jobDir 'progress.json')
    $display = $state.display
    $goal = if ($display.task_goal) {
        [string]$display.task_goal
    } elseif ($request.task.goal) {
        [string]$request.task.goal
    } else {
        '（任务正文已在完成后安全移除；显示最终回复）'
    }
    $executionMode = if ($display.execution_mode) {
        [string]$display.execution_mode
    } elseif ($request.execution.mode) {
        [string]$request.execution.mode
    } else {
        'direct'
    }
    $executionLabel = if ($script:ExecutionLabels.ContainsKey($executionMode)) {
        $script:ExecutionLabels[$executionMode]
    } else {
        $executionMode
    }
    $reasoningMode = if ($display.reasoning_mode) {
        [string]$display.reasoning_mode
    } elseif ($request.reasoning.mode) {
        [string]$request.reasoning.mode
    } else {
        'unknown'
    }
    $model = if ($result.provider.actual) {
        [string]$result.provider.actual
    } elseif ($result.backend.model) {
        [string]$result.backend.model
    } elseif ($gpu.LoadedModel) {
        $gpu.LoadedModel
    } else {
        [string]$state.backend
    }
    $promptTokens = if ($null -ne $result.usage.prompt_tokens) {
        [string]$result.usage.prompt_tokens
    } else {
        '—'
    }
    $completionTokens = if ($null -ne $result.usage.completion_tokens) {
        [string]$result.usage.completion_tokens
    } else {
        '—'
    }
    $created = [DateTimeOffset]::Parse([string]$state.created_utc)
    $updated = [DateTimeOffset]::Parse([string]$state.updated_utc)
    $elapsedEnd = if ($state.job_status -in @('queued', 'running')) {
        [DateTimeOffset]::UtcNow
    } else {
        $updated
    }
    $elapsed = [Math]::Max(0, [int]($elapsedEnd - $created).TotalSeconds)
    $durationSeconds = if ($null -ne $result.usage.total_duration_ns) {
        [double]$result.usage.total_duration_ns / 1000000000
    } else {
        [double]$elapsed
    }
    $tokensPerSecond = if (
        $null -ne $result.usage.eval_duration_ns -and
        [double]$result.usage.eval_duration_ns -gt 0 -and
        $completionTokens -ne '—'
    ) {
        [Math]::Round(
            ([double]$completionTokens / ([double]$result.usage.eval_duration_ns / 1000000000)),
            1
        )
    } elseif (
        $completionTokens -ne '—' -and
        $durationSeconds -gt 0
    ) {
        [Math]::Round(([double]$completionTokens / $durationSeconds), 1)
    } else {
        $null
    }

    Write-Section -Title '运行指标'
    $statusLabel = if ($script:StatusLabels.ContainsKey([string]$state.job_status)) {
        $script:StatusLabels[[string]$state.job_status]
    } else {
        [string]$state.job_status
    }
    Write-Host ("任务 {0} ｜ 状态 {1} ｜ 后端 {2}" -f $state.job_id, $statusLabel, $state.backend)
    $reasoningLabel = switch ($reasoningMode) {
        'on' { '已启用' }
        'off' { '已关闭' }
        default { '历史任务未记录' }
    }
    Write-Host ("模型 {0} ｜ 执行方式 {1} ｜ 深度推理 {2}" -f $model, $executionLabel, $reasoningLabel)
    $speedText = if ($null -ne $tokensPerSecond) {
        "生成速度约 $tokensPerSecond TPS"
    } elseif (
        $state.job_status -in @('queued', 'running') -and
        [double]$progress.metrics.elapsed_seconds -gt 0 -and
        [int]$progress.metrics.token_events -gt 0
    ) {
        $liveTps = [Math]::Round(
            ([double]$progress.metrics.token_events / [double]$progress.metrics.elapsed_seconds),
            1
        )
        "实时约 $liveTps TPS（流式估算）"
    } elseif ($state.job_status -in @('queued', 'running')) {
        '实时 TPS 等待流式计数'
    } else {
        '生成速度暂无数据'
    }
    Write-Host ("输入 Token {0} ｜ 生成 Token（含隐藏推理）{1} ｜ {2} ｜ 已耗时 {3}s" -f
        $promptTokens, $completionTokens, $speedText, $elapsed)
    Write-Host ("GPU Broker {0} ｜ 活动请求 {1} ｜ 租约 {2}" -f $(if ($gpu.Ok) { '正常' } else { '不可达' }), $gpu.ActiveRequests, $gpu.Lease)

    Write-Section -Title '对话'
    Write-Host '用户任务：'
    Write-Host $goal
    Write-Host ''
    Write-Host '模型回复：'
    if ($null -ne $result.output) {
        Write-Host (ConvertTo-DisplayText -Value $result.output -Limit $MaxOutputChars)
    } elseif ($progress.public_preview) {
        Write-Host ([string]$progress.public_preview)
        Write-Host '……（回复仍在生成）'
    } elseif ($state.job_status -in @('queued', 'running')) {
        Write-Host '（最终回复尚未生成；下面会持续显示工作进展。）'
    } else {
        Write-Host '（尚无可公开回复。）'
    }
    if (Test-Path -LiteralPath (Join-Path $jobDir 'output.json') -PathType Leaf) {
        Write-Host ''
        Write-Host ("原始结构化结果（按 R 才展开）：{0}" -f (Join-Path $jobDir 'output.json'))
    }

    Write-Section -Title '思考与工作进展'
    if ($progress.phase) {
        $phase = [string]$progress.phase
        $phaseLabel = if ($script:PhaseLabels.ContainsKey($phase)) {
            $script:PhaseLabels[$phase]
        } else {
            $phase
        }
        Write-Host ("当前：{0}" -f $phaseLabel)
        if ($progress.summary) {
            Write-Host ([string]$progress.summary)
        }
        if ($progress.updated_utc) {
            try {
                $progressTime = [DateTimeOffset]::Parse([string]$progress.updated_utc).ToLocalTime()
                Write-Host ("最近更新：{0}" -f $progressTime.ToString('HH:mm:ss'))
            } catch {
            }
        }
        if ($progress.metrics) {
            $activity = if ($progress.metrics.thinking_active) { '内部分析活跃' } else { '等待或公开生成' }
            Write-Host ("生成片段 {0} ｜ 公开回复 {1} 字符 ｜ {2}" -f
                $progress.metrics.token_events,
                $progress.metrics.content_chars,
                $activity)
        }
        foreach ($event in @($progress.events | Select-Object -Last 6)) {
            $eventPhase = [string]$event.phase
            $eventLabel = if ($script:PhaseLabels.ContainsKey($eventPhase)) {
                $script:PhaseLabels[$eventPhase]
            } else {
                $eventPhase
            }
            $eventText = if ($event.summary) { "：$($event.summary)" } else { '' }
            Write-Host ("• {0}{1}" -f $eventLabel, $eventText)
        }
    } else {
        switch ([string]$state.job_status) {
            'queued' { Write-Host '我已接到任务，正在等待 LocalGpuBroker 分配本地 GPU。' }
            'running' {
                Write-Host '我正在整理输入并生成结果；即使正文尚未出现，这里的耗时仍会持续更新。'
            }
            'completed' {
                Write-Host '我已经完成生成，下面是结果校验和可接管状态。'
            }
            'stale' { Write-Host '任务超过监控时限；我已停止无效轮询，等待顶级模型接管。' }
            default { Write-Host ("当前阶段：{0}" -f $state.job_status) }
        }
    }
    foreach ($check in @($result.checks)) {
        $mark = if ($check.passed) { '✓' } else { '✗' }
        $checkLabel = if ($script:CheckLabels.ContainsKey([string]$check.id)) {
            $script:CheckLabels[[string]$check.id]
        } else {
            [string]$check.id
        }
        Write-Host ("{0} {1}" -f $mark, $checkLabel)
    }
    foreach ($uncertainty in @($result.uncertainties)) {
        Write-Host ("? {0}" -f $uncertainty) -ForegroundColor Yellow
    }
    if ($result.execution_receipt) {
        Write-Host ("智能体工具调用 {0} ｜ 退出码 {1} ｜ 停止原因 {2}" -f
            $result.execution_receipt.tool_calls,
            $result.execution_receipt.exit_code,
            $result.execution_receipt.stop_reason)
    }
    if ($reasoningMode -eq 'on') {
        Write-Host '说明：深度推理已启用；这里只显示可验证的中文进展，不显示原始隐藏推理。'
    } elseif ($reasoningMode -eq 'off') {
        Write-Host '说明：深度推理已关闭；该任务按窄合同直接生成。'
    } else {
        Write-Host '说明：这是旧任务，未记录深度推理档位；后续任务会显示实测配置。'
    }
}

try {
    do {
        $frame = @(
            foreach ($item in @(& { Show-Dashboard } 6>&1)) {
                foreach ($line in (([string]$item -replace "`r", '').Split("`n"))) {
                    $line
                }
            }
        )
        if ($Once) {
            $frame
            break
        }
        Update-DashboardFrame -Lines $frame
        $deadline = (Get-Date).AddSeconds($RefreshSeconds)
        do {
            Start-Sleep -Milliseconds 100
            if ([Console]::KeyAvailable) {
                $key = [Console]::ReadKey($true)
                if ($key.Key -eq [ConsoleKey]::Q) {
                    return
                }
                if ($key.Key -eq [ConsoleKey]::R) {
                    $script:ShowRawJson = -not $script:ShowRawJson
                    break
                }
            }
        } while ((Get-Date) -lt $deadline)
    } while ($true)
} finally {
    if (-not $Once) {
        [Console]::Write("$($script:Escape)[?25h$($script:ResetTheme)")
    }
}
