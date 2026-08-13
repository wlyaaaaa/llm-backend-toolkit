"use strict";

const POLL_INTERVAL_MS = 5000;
const ACTIVE_REFRESH_INTERVAL_MS = 5000;
const REFRESH_DEBOUNCE_MS = 180;
const HISTORY_PAGE_SIZE = 100;
const EVENT_PAGE_SIZE = 160;
const MAX_TIMELINE_EVENTS = 240;

const state = {
  runs: [],
  selectedJobId: null,
  selectedDetail: null,
  filter: "all",
  query: "",
  eventSource: null,
  pollingTimer: null,
  refreshTimer: null,
  toastTimer: null,
  detailRequestSerial: 0,
  listRequestInFlight: false,
  refreshQueued: false,
  appendQueued: false,
  historyTotal: 0,
  nextOffset: null,
  timelineJobId: null,
  timelineEvents: [],
  timelinePage: null,
  eventPageLoading: false,
  timelineBrowsingEarlier: false,
  draftJobId: null,
  draftText: "",
  conversationJobId: null,
  conversationLatestSequence: 0,
  conversationPendingCount: 0,
  activeTimer: null,
  lastActiveRefresh: 0,
  durationObservedAt: 0,
};

const elements = {
  connectionState: document.querySelector("#connection-state"),
  lastUpdated: document.querySelector("#last-updated"),
  refreshButton: document.querySelector("#refresh-button"),
  runCount: document.querySelector("#run-count"),
  runSearch: document.querySelector("#run-search"),
  runList: document.querySelector("#run-list"),
  runListEmpty: document.querySelector("#run-list-empty"),
  loadMoreButton: document.querySelector("#load-more-button"),
  filterButtons: [...document.querySelectorAll(".filter-button")],
  conversationEmptyState: document.querySelector("#conversation-empty-state"),
  conversationContent: document.querySelector("#conversation-content"),
  conversationPane: document.querySelector(".conversation-pane"),
  conversationStream: document.querySelector("#conversation-stream"),
  conversationLabel: document.querySelector("#conversation-label"),
  conversationMeta: document.querySelector("#conversation-meta"),
  conversationOutputNode: document.querySelector(".assistant-output-node"),
  conversationOutputTitle: document.querySelector("#conversation-output-title"),
  conversationOutputState: document.querySelector("#conversation-output-state"),
  conversationOutput: document.querySelector("#conversation-output"),
  conversationWorkRecords: document.querySelector("#conversation-work-records"),
  conversationNewEvents: document.querySelector("#conversation-new-events"),
  mobileBackButton: document.querySelector("#mobile-back-button"),
  inspectorToggle: document.querySelector("#inspector-toggle"),
  inspectorCloseButton: document.querySelector("#inspector-close-button"),
  inspectorEmptyState: document.querySelector("#inspector-empty-state"),
  inspectorContent: document.querySelector("#inspector-content"),
  timeline: document.querySelector("#timeline"),
  timelineStatus: document.querySelector("#timeline-status"),
  loadEarlierEvents: document.querySelector("#load-earlier-events"),
  returnLatestEvents: document.querySelector("#return-latest-events"),
  runStatus: document.querySelector("#run-status"),
  runId: document.querySelector("#run-id"),
  runTitle: document.querySelector("#run-title"),
  copyIdButton: document.querySelector("#copy-id-button"),
  receiptSummary: document.querySelector("#receipt-summary"),
  receiptChecks: document.querySelector("#receipt-checks"),
  receiptContent: document.querySelector("#receipt-content"),
  toast: document.querySelector("#toast"),
  metrics: {
    model: document.querySelector("#metric-model"),
    execution: document.querySelector("#metric-execution"),
    reasoning: document.querySelector("#metric-reasoning"),
    tokens: document.querySelector("#metric-tokens"),
    context: document.querySelector("#metric-context"),
    tps: document.querySelector("#metric-tps"),
    duration: document.querySelector("#metric-duration"),
    gpu: document.querySelector("#metric-gpu"),
    delivery: document.querySelector("#metric-delivery"),
  },
  contextDetail: document.querySelector("#metric-context-detail"),
  tokenDetail: document.querySelector("#metric-token-detail"),
};

const runItemCache = new Map();
const timelineItemCache = new Map();

const STATUS_MAP = {
  accepted: { label: "已接收", tone: "neutral" },
  queued: { label: "排队中", tone: "active" },
  running: { label: "执行中", tone: "active" },
  cancellation_requested: { label: "取消中", tone: "active" },
  completed: { label: "已完成", tone: "success" },
  succeeded: { label: "已完成", tone: "success" },
  ok: { label: "已完成", tone: "success" },
  partial: { label: "部分完成", tone: "warning" },
  blocked: { label: "已阻塞", tone: "danger" },
  failed: { label: "失败", tone: "danger" },
  error: { label: "异常", tone: "danger" },
  cancelled: { label: "已取消", tone: "danger" },
  stale: { label: "已过期", tone: "danger" },
};

const RESULT_STATUS_PRIORITY = new Set([
  "failed",
  "error",
  "blocked",
  "stale",
  "cancelled",
  "partial",
]);

function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== "");
}

function pick(object, ...paths) {
  for (const path of paths) {
    let current = object;
    for (const key of path.split(".")) {
      if (current === null || current === undefined || typeof current !== "object") {
        current = undefined;
        break;
      }
      current = current[key];
    }
    if (current !== undefined && current !== null && current !== "") {
      return current;
    }
  }
  return undefined;
}

function normalizedStatus(run) {
  const resultStatus = String(
    firstDefined(
      run.result_status,
      pick(run, "result.status"),
      pick(run, "result.delivery_receipt.status"),
      pick(run, "delivery.status"),
      "",
    ),
  ).toLowerCase();
  if (RESULT_STATUS_PRIORITY.has(resultStatus)) {
    return resultStatus;
  }
  return String(firstDefined(run.job_status, run.status, resultStatus, "unknown")).toLowerCase();
}

function statusInfo(run) {
  const status = normalizedStatus(run);
  if (STATUS_MAP[status]) {
    return STATUS_MAP[status];
  }
  return { label: status === "unknown" ? "状态未知" : status, tone: "neutral" };
}

function isActive(run) {
  return ["accepted", "queued", "running", "cancellation_requested"].includes(
    normalizedStatus(run),
  );
}

function isCompleted(run) {
  return ["completed", "succeeded", "ok"].includes(normalizedStatus(run));
}

function formatStructured(value, fallback = "—") {
  if (value === undefined || value === null || value === "") {
    return fallback;
  }
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (
      (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
      (trimmed.startsWith("[") && trimmed.endsWith("]"))
    ) {
      try {
        return JSON.stringify(JSON.parse(trimmed), null, 2);
      } catch {
        return value;
      }
    }
    return value;
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function formatNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat("zh-CN").format(number) : "—";
}

function formatCompactTokens(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) {
    return "—";
  }
  if (number < 1000) {
    return formatNumber(number);
  }
  const thousands = number / 1000;
  const precision = thousands < 10 && !Number.isInteger(thousands) ? 1 : 0;
  return `${thousands.toFixed(precision)}k`;
}

function formatDuration(value, unitHint = "") {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) {
    return "—";
  }
  let milliseconds = number;
  if (unitHint === "ns") {
    milliseconds = number / 1_000_000;
  } else if (unitHint === "seconds") {
    milliseconds = number * 1000;
  }
  if (milliseconds < 1000) {
    return `${Math.round(milliseconds)} ms`;
  }
  const seconds = milliseconds / 1000;
  if (seconds < 10) {
    return `${seconds.toFixed(1)} 秒`;
  }
  const roundedSeconds = Math.round(milliseconds / 1000);
  if (roundedSeconds < 60) {
    return `${roundedSeconds} 秒`;
  }
  const minutes = Math.floor(roundedSeconds / 60);
  const remaining = roundedSeconds % 60;
  return `${minutes} 分 ${remaining} 秒`;
}

function calculateDuration(detail) {
  const elapsedSeconds = pick(
    detail,
    "performance.elapsed_seconds",
    "progress.metrics.elapsed_seconds",
  );
  if (elapsedSeconds !== undefined) {
    return formatDuration(elapsedSeconds, "seconds");
  }
  const direct = pick(
    detail,
    "duration_ms",
    "result.duration_ms",
    "result.execution_receipt.duration_ms",
    "display.duration_ms",
  );
  if (direct !== undefined) {
    return formatDuration(direct);
  }
  const nanoseconds = pick(detail, "result.usage.total_duration_ns", "usage.total_duration_ns");
  if (nanoseconds !== undefined) {
    return formatDuration(nanoseconds, "ns");
  }
  const seconds = pick(detail, "duration_seconds", "result.duration_seconds");
  if (seconds !== undefined) {
    return formatDuration(seconds, "seconds");
  }
  const start = Date.parse(firstDefined(detail.started_utc, detail.created_utc));
  const end = Date.parse(firstDefined(detail.completed_utc, detail.updated_utc));
  return Number.isFinite(start) && Number.isFinite(end) && end >= start
    ? formatDuration(end - start)
    : "—";
}

function calculateTps(detail) {
  const explicit = pick(
    detail,
    "result.usage.tps",
    "usage.tps",
    "result.tps",
    "tps",
    "performance.tokens_per_second",
  );
  const completionTokens = Number(
    pick(
      detail,
      "result.usage.completion_tokens",
      "result.usage.output_tokens",
      "usage.completion_tokens",
      "usage.output_tokens",
    ),
  );
  const evalDurationNs = Number(
    pick(detail, "result.usage.eval_duration_ns", "usage.eval_duration_ns"),
  );
  if (
    Number.isFinite(completionTokens) &&
    Number.isFinite(evalDurationNs) &&
    evalDurationNs > 0
  ) {
    const exact = completionTokens / (evalDurationNs / 1_000_000_000);
    return `${exact.toFixed(exact < 10 ? 1 : 0)} 输出 token/秒（模型评估时段精确）`;
  }
  const source = String(
    firstDefined(
      pick(detail, "result.usage.tps_source"),
      pick(detail, "usage.tps_source"),
      pick(detail, "tps_source"),
      pick(detail, "performance.tokens_per_second_source"),
      "",
    ),
  ).toLowerCase();
  if (Number.isFinite(Number(explicit)) && source) {
    const value = Number(explicit).toFixed(Number(explicit) < 10 ? 1 : 0);
    if (["exact", "eval_duration"].includes(source)) {
      return `${value} 输出 token/秒（模型评估时段精确）`;
    }
    if (source === "wall_clock_estimate") {
      return `≈ ${value} 输出 token/秒（整段墙钟估算）`;
    }
    if (source === "public_content_estimate") {
      return `≈ ${value} 输出 token/秒（公开内容估算）`;
    }
    return `≈ ${value} 输出 token/秒（来源：${source}）`;
  }
  const durationNs = Number(pick(detail, "result.usage.total_duration_ns", "usage.total_duration_ns"));
  if (Number.isFinite(completionTokens) && Number.isFinite(durationNs) && durationNs > 0) {
    const estimate = completionTokens / (durationNs / 1_000_000_000);
    return `≈ ${estimate.toFixed(estimate < 10 ? 1 : 0)} 输出 token/秒（总耗时估算）`;
  }
  return "—";
}

function tokenSummary(detail) {
  const suppliedTotal = firstDefined(
    pick(detail, "result.usage.total_tokens"),
    pick(detail, "usage.total_tokens"),
  );
  const promptTokens = pick(
    detail,
    "result.usage.prompt_tokens",
    "result.usage.input_tokens",
    "usage.prompt_tokens",
    "usage.input_tokens",
  );
  const completionTokens = pick(
    detail,
    "result.usage.completion_tokens",
    "result.usage.output_tokens",
    "usage.completion_tokens",
    "usage.output_tokens",
  );
  const cachedTokens = pick(
    detail,
    "result.usage.cached_tokens",
    "result.usage.cached_input_tokens",
    "usage.cached_tokens",
    "usage.cached_input_tokens",
  );
  const reasoningTokens = pick(
    detail,
    "result.usage.reasoning_tokens",
    "result.usage.reasoning_output_tokens",
    "usage.reasoning_tokens",
    "usage.reasoning_output_tokens",
  );
  const numericPrompt = Number(promptTokens);
  const numericCompletion = Number(completionTokens);
  const numericReasoning = Number(reasoningTokens);
  const calculatedTotal =
    Number.isFinite(numericPrompt) && Number.isFinite(numericCompletion)
      ? numericPrompt + numericCompletion +
        (Number.isFinite(numericReasoning) && numericReasoning > 0
          ? numericReasoning
          : 0)
      : undefined;
  const totalTokens = suppliedTotal ?? calculatedTotal;
  if (totalTokens !== undefined) {
    const parts = [`总计 ${formatNumber(totalTokens)}`];
    if (promptTokens !== undefined) {
      parts.push(`输入 ${formatNumber(promptTokens)}`);
    }
    if (completionTokens !== undefined) {
      parts.push(`输出 ${formatNumber(completionTokens)}`);
    }
    if (reasoningTokens !== undefined && Number(reasoningTokens) > 0) {
      parts.push(`推理 ${formatNumber(reasoningTokens)}`);
    }
    if (cachedTokens !== undefined && Number(cachedTokens) > 0) {
      parts.push(`缓存 ${formatNumber(cachedTokens)}`);
    }
    return {
      label: `总计 ${formatNumber(totalTokens)}`,
      detail: parts.join(" · "),
    };
  }
  const estimatedOutputTokens = pick(
    detail,
    "progress.metrics.estimated_output_tokens",
    "metrics.estimated_output_tokens",
  );
  if (estimatedOutputTokens !== undefined && Number(estimatedOutputTokens) > 0) {
    if (!pick(detail, "progress.public_preview")) {
      return { label: "—", detail: "Token usage 不可用" };
    }
    const label = `≈ ${formatNumber(estimatedOutputTokens)} 输出`;
    return { label, detail: `${label} token（公开内容估算）` };
  }
  const tokenEvents = pick(
    detail,
    "progress.metrics.token_events",
    "metrics.token_events",
    "progress.token_events",
  );
  if (tokenEvents !== undefined) {
    return {
      label: "暂无 Token",
      detail: `尚无 Token usage；已观察 ${formatNumber(tokenEvents)} 个公开片段`,
    };
  }
  return { label: "—", detail: "Token usage 不可用" };
}

function contextSummary(detail) {
  const current = pick(detail, "context.current_tokens");
  const contextWindow = pick(detail, "context.context_window_tokens");
  const hasCurrent = Number.isFinite(Number(current)) && Number(current) >= 0;
  const hasWindow =
    Number.isFinite(Number(contextWindow)) && Number(contextWindow) > 0;
  const label = hasCurrent && hasWindow
    ? `已用 ${formatCompactTokens(current)} / 共 ${formatCompactTokens(contextWindow)}`
    : hasCurrent || hasWindow
      ? "Codex 运行时数据不完整"
      : "等待 Codex 运行时实测";
  const percentage =
    hasCurrent && hasWindow
      ? Math.min(100, (Number(current) / Number(contextWindow)) * 100)
      : undefined;
  const currentDetail = hasCurrent
    ? `${formatNumber(current)} token（Codex 运行时实测）`
    : "等待 Codex 运行时上报当前占用";
  const windowDetail = hasWindow
    ? `${formatNumber(contextWindow)} token（Codex 运行时实测）`
    : "等待 Codex 运行时上报总上下文上限";
  const percentageDetail =
    percentage === undefined ? "" : `\n占用：${Math.round(percentage)}%。`;
  return {
    label,
    detail:
      `已用：${currentDetail}\n共：${windowDetail}${percentageDetail}\n` +
      "说明：不把累计输入 Token 当成当前占用。",
  };
}

function formatDateTime(value, style = "time") {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  const options =
    style === "relative"
      ? { hour: "2-digit", minute: "2-digit" }
      : { hour: "2-digit", minute: "2-digit", second: "2-digit" };
  return new Intl.DateTimeFormat("zh-CN", options).format(date);
}

function runTitle(run) {
  return String(
    firstDefined(
      pick(run, "display.task_label"),
      run.task_label,
      "历史模型任务",
    ),
  );
}

function modelName(run) {
  return String(
    firstDefined(
      pick(run, "result.backend.model"),
      pick(run, "backend.model"),
      pick(run, "result.provider.actual"),
      run.model,
      run.backend,
      run.provider,
      "—",
    ),
  );
}

function executionMode(detail) {
  const value = String(
    firstDefined(
      pick(detail, "display.execution_mode"),
      pick(detail, "result.execution_receipt.resolved_runner"),
      pick(detail, "result.execution_receipt.runner"),
      pick(detail, "request.execution.mode"),
      "—",
    ),
  );
  const labels = {
    direct: "直接调用",
    agent: "智能体",
    "codex-cli": "Codex CLI",
    "qwen-code": "Qwen Code",
    data_factory: "数据工厂",
  };
  return labels[value] || value;
}

function reasoningLevel(detail) {
  const value = String(
    firstDefined(
      pick(detail, "display.reasoning_effort"),
      pick(detail, "result.execution_receipt.reasoning_effort"),
      pick(detail, "request.reasoning.effort"),
      pick(detail, "display.reasoning_mode"),
      pick(detail, "request.reasoning.mode"),
      "—",
    ),
  ).toLowerCase();
  const labels = {
    off: "关闭",
    on: "开启",
    low: "低",
    medium: "中",
    high: "高",
    max: "最高",
    xhigh: "超高",
    ultra: "极高",
  };
  return labels[value] || value;
}

function gpuLabel(detail) {
  const value = firstDefined(
    pick(detail, "gpu.status"),
    pick(detail, "display.gpu"),
    pick(detail, "result.execution_receipt.gpu"),
    pick(detail, "result.backend.gpu"),
  );
  if (value === undefined) {
    const local = String(firstDefined(detail.backend, "")).toLowerCase().includes("local");
    return local ? "本地 GPU" : "—";
  }
  if (typeof value === "boolean") {
    return value ? "使用中" : "未使用";
  }
  return typeof value === "object" ? formatStructured(value) : String(value);
}

function deliveryLabel(detail) {
  return statusInfo(detail).label;
}

function normalizedRuns(payload) {
  if (Array.isArray(payload)) {
    return payload;
  }
  for (const key of ["runs", "jobs", "items", "results"]) {
    if (Array.isArray(payload?.[key])) {
      return payload[key];
    }
  }
  return [];
}

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) {
    element.className = className;
  }
  if (text !== undefined) {
    element.textContent = text;
  }
  return element;
}

function filteredRuns() {
  const query = state.query.trim().toLocaleLowerCase("zh-CN");
  return state.runs.filter((run) => {
    const matchesFilter =
      state.filter === "all" ||
      (state.filter === "active" && isActive(run)) ||
      (state.filter === "completed" && isCompleted(run));
    const haystack =
      `${runTitle(run)} ${modelName(run)} ${conversationLabel(run)} ${run.job_id || ""}`.toLocaleLowerCase(
        "zh-CN",
      );
    return matchesFilter && (!query || haystack.includes(query));
  });
}

function conversationLabel(run) {
  const root = String(pick(run, "conversation.root_job_id") || "");
  const turn = Number(pick(run, "conversation.turn") || 1);
  if (!root) {
    return "独立对话";
  }
  return `对话 #${root.slice(0, 5)} · 第${turn}轮`;
}

function runJobId(run) {
  return String(firstDefined(run.job_id, run.id, ""));
}

function createRunItem(jobId) {
  const button = createElement("button", "run-item");
  button.type = "button";
  button.dataset.jobId = jobId;

  const top = createElement("span", "run-item-top");
  const status = createElement("span", "mini-status");
  const time = createElement("span", "run-item-time");
  top.append(status, time);

  const title = createElement("span", "run-item-title");
  const meta = createElement("span", "run-item-meta");
  const model = createElement("span", "run-item-model");
  const conversation = createElement("span", "run-item-conversation");
  const id = createElement("span", "run-item-id");
  meta.append(model, conversation, id);
  button.append(top, title, meta);
  button.addEventListener("click", () =>
    selectRun(button.dataset.jobId, { revealConversation: true }),
  );

  const entry = { button, status, time, title, model, conversation, id };
  runItemCache.set(jobId, entry);
  return entry;
}

function updateRunItem(entry, run, jobId) {
  const info = statusInfo(run);
  entry.button.dataset.jobId = jobId;
  entry.button.setAttribute("aria-current", String(jobId === state.selectedJobId));
  entry.status.textContent = info.label;
  entry.status.dataset.tone = info.tone;
  entry.time.textContent = formatDateTime(run.updated_utc, "relative");
  entry.title.textContent = runTitle(run);
  entry.model.textContent = modelName(run);
  entry.conversation.textContent = conversationLabel(run);
  entry.id.textContent = jobId ? `#${jobId.slice(0, 7)}` : "—";
}

function captureRunListViewport() {
  const containerRect = elements.runList.getBoundingClientRect();
  const anchor =
    elements.runList.scrollTop > 1
      ? [...elements.runList.children].find(
          (item) => item.getBoundingClientRect().bottom > containerRect.top,
        )
      : null;
  return {
    scrollTop: elements.runList.scrollTop,
    anchorJobId: anchor?.dataset.jobId || null,
    anchorTop: anchor ? anchor.getBoundingClientRect().top - containerRect.top : 0,
    focusedJobId:
      document.activeElement?.classList?.contains("run-item")
        ? document.activeElement.dataset.jobId
        : null,
  };
}

function restoreRunListViewport(viewport) {
  const anchor = viewport.anchorJobId
    ? runItemCache.get(viewport.anchorJobId)?.button
    : null;
  if (anchor?.isConnected) {
    const containerTop = elements.runList.getBoundingClientRect().top;
    const anchorTop = anchor.getBoundingClientRect().top - containerTop;
    elements.runList.scrollTop = viewport.scrollTop + anchorTop - viewport.anchorTop;
  } else {
    elements.runList.scrollTop = viewport.scrollTop;
  }

  const focused = viewport.focusedJobId
    ? runItemCache.get(viewport.focusedJobId)?.button
    : null;
  if (focused?.isConnected && document.activeElement !== focused) {
    focused.focus({ preventScroll: true });
  }
}

function renderRunList() {
  const runs = filteredRuns();
  const viewport = captureRunListViewport();
  elements.runCount.textContent =
    state.runs.length < state.historyTotal
      ? `${state.runs.length}/${state.historyTotal}`
      : String(state.historyTotal || state.runs.length);
  elements.runListEmpty.hidden = runs.length !== 0;
  elements.loadMoreButton.hidden = state.nextOffset === null;

  const knownJobIds = new Set(state.runs.map(runJobId));
  for (const [jobId, entry] of runItemCache) {
    if (!knownJobIds.has(jobId)) {
      entry.button.remove();
      runItemCache.delete(jobId);
    }
  }

  let cursor = elements.runList.firstElementChild;
  for (const run of runs) {
    const jobId = runJobId(run);
    const entry = runItemCache.get(jobId) || createRunItem(jobId);
    updateRunItem(entry, run, jobId);
    if (entry.button !== cursor) {
      elements.runList.insertBefore(entry.button, cursor);
    }
    cursor = entry.button.nextElementSibling;
  }
  while (cursor) {
    const next = cursor.nextElementSibling;
    cursor.remove();
    cursor = next;
  }

  restoreRunListViewport(viewport);
  elements.runList.setAttribute("aria-busy", "false");
}

function timelineEventKey(item, sourceIndex) {
  const explicitId = firstDefined(
    item.event_id,
    item.id,
    item.sequence_id,
    item.sequence,
    item.seq,
    pick(item, "payload.item_id"),
    pick(item, "payload.call_id"),
  );
  if (explicitId !== undefined) {
    return `event:${explicitId}`;
  }
  const kind = String(firstDefined(item.kind, item.type, "event"));
  return `event:${sourceIndex}:${kind}`;
}

function eventSequence(item) {
  const value = Number(firstDefined(item?.sequence, item?.seq, item?.sequence_id));
  return Number.isFinite(value) ? value : 0;
}

function mergeTimelineEvents(existing, incoming) {
  const merged = new Map();
  for (const item of [...existing, ...incoming]) {
    const key = String(
      firstDefined(item?.event_id, item?.id, `sequence:${eventSequence(item)}`),
    );
    merged.set(key, item);
  }
  return [...merged.values()].sort((left, right) => eventSequence(left) - eventSequence(right));
}

function collapseOutputDeltas(events) {
  const collapsed = [];
  let batch = null;
  const flush = () => {
    if (!batch) {
      return;
    }
    collapsed.push({
      schema: "llm-backend-toolkit.observer-ui-event.v1",
      event_id: `delta:${batch.first}:${batch.last}`,
      sequence: batch.last,
      occurred_utc: batch.time,
      kind: "agent.output.delta.batch",
      visibility: "public",
      summary_zh:
        `已接收 ${batch.count} 个公开片段；` +
        "完整内容已在“实时草稿”中逐段追加。",
      payload: { count: batch.count },
    });
    batch = null;
  };

  for (const item of events) {
    if (item?.kind === "agent.output.delta") {
      const sequence = eventSequence(item);
      if (!batch) {
        batch = {
          count: 0,
          first: sequence,
          last: sequence,
          time: item.occurred_utc,
        };
      }
      batch.count += 1;
      batch.last = sequence;
      batch.time = item.occurred_utc || batch.time;
      continue;
    }
    flush();
    collapsed.push(item);
  }
  flush();
  return collapsed;
}

function toolActivityTitle(payload) {
  const toolNumber = Number(payload?.tool_calls);
  const numbered = Number.isFinite(toolNumber) && toolNumber > 0;
  if (payload?.item_type === "command_execution") {
    const prefix = numbered ? `第 ${toolNumber} 个命令` : "命令活动";
    const stateLabel = {
      in_progress: "正在执行",
      succeeded: "执行成功",
      failed: "执行失败",
      declined: "已拒绝",
    }[payload?.command_status] || "状态更新";
    return `${prefix} · ${stateLabel}`;
  }
  if (payload?.item_type === "file_change") {
    const prefix = numbered ? `第 ${toolNumber} 个工具活动` : "文件编辑";
    return `${prefix} · ${payload?.status === "completed" ? "完成编辑文件" : "正在编辑文件"}`;
  }
  const activityLabel = {
    web_search: "查询公开资料",
    mcp_tool_call: "调用 MCP 工具",
    computer_use: "操作计算机",
    dynamic_tool_call: "调用动态工具",
    tool_call: "调用工具",
  }[payload?.item_type] || "工具活动";
  const prefix = numbered ? `第 ${toolNumber} 个工具 · ` : "";
  const stateLabel = payload?.status === "completed" ? "已完成" : "进行中";
  return `${prefix}${activityLabel} · ${stateLabel}`;
}

function timelineEvents(detail) {
  const provided = firstDefined(
    pick(detail, "events"),
    pick(detail, "progress.events"),
    pick(detail, "timeline"),
  );
  if (!Array.isArray(provided)) {
    return [];
  }
  const kindLabels = {
    accepted: "调用已接收",
    queued: "等待执行资源",
    started: "开始执行",
    progress: "工作进展",
    tool: "工具活动",
    output: "产生公开输出",
    completed: "结果已交付",
    failed: "执行失败",
    cancelled: "执行已取消",
    "run.created": "创建可见运行",
    "run.started": "开始执行",
    "queue.entered": "等待 GPU 通道",
    "work.preparing": "整理输入与约束",
    "model.connecting": "连接模型与 Broker",
    "reasoning.activity": "推理活动",
    "output.started": "开始公开输出",
    "validation.started": "校验结果",
    "run.completed": "运行完成",
    "run.failed": "运行失败",
    "cache.hit": "复用已完成结果",
    "handoff.collected": "结果已取回",
    "handoff.disposition": "结果接管状态",
    "agent.observability": "AICLI 可观察性",
    "agent.thread.started": "建立智能体线程",
    "agent.turn.started": "智能体开始本轮",
    "agent.turn.completed": "智能体完成本轮",
    "agent.turn.failed": "智能体本轮失败",
    "agent.run.failed": "智能体运行失败",
    "agent.limit.hit": "智能体达到执行限制",
    "agent.reasoning.activity": "智能体分析活动",
    "agent.planning.activity": "智能体计划更新",
    "agent.tool.activity": "智能体工具活动",
    "agent.output.delta": "智能体公开进度",
    "agent.output.delta.batch": "智能体公开进度",
    "agent.output.completed": "智能体公开输出",
    "agent.context.usage.updated": "实时上下文更新",
    "agent.context.compaction.completed": "Codex 已自动压缩上下文",
    "context.compaction.completed": "已压缩调用输入",
    "workspace.change.observed": "检测到工作区变化",
    "media.ocr.started": "LocalOCR 开始",
    "media.ocr.completed": "LocalOCR 完成",
    "media.asr.started": "ChineseASR 开始",
    "media.asr.completed": "ChineseASR 完成",
  };
  const visible = collapseOutputDeltas(provided)
    .map((item, sourceIndex) => ({ item, sourceIndex }))
    .filter(({ item }) => item?.kind !== "agent.context.usage.updated")
    .map(({ item, sourceIndex }) => {
    const kind = String(firstDefined(item.kind, item.type, "event"));
    const metrics = item.metrics && typeof item.metrics === "object" ? item.metrics : undefined;
    const payload = item.payload && typeof item.payload === "object" ? item.payload : undefined;
    let title = String(firstDefined(item.title, item.label, kindLabels[kind], kind));
    if (kind === "agent.tool.activity") {
      title = toolActivityTitle(payload);
    }
    let detailText = formatStructured(
      firstDefined(item.summary_zh, item.public_summary, item.summary, metrics, ""),
    );
    const workspacePaths = [];
    let tone = String(firstDefined(item.tone, item.status, kind)).toLowerCase();
    if (kind === "context.compaction.completed") {
      const before = Number(payload?.estimated_tokens_before);
      const after = Number(payload?.estimated_tokens_after);
      const lines = [detailText];
      if (Number.isFinite(before) && Number.isFinite(after)) {
        lines.push(`预计 ${formatNumber(before)} → ${formatNumber(after)} Token。`);
      }
      if (Number(payload?.duplicates_removed) > 0) {
        lines.push(`移除 ${formatNumber(payload.duplicates_removed)} 个重复项。`);
      }
      if (payload?.lossy === true) {
        lines.push("为满足目标，已裁剪过长输入。");
        tone = "warning";
      } else {
        tone = "success";
      }
      detailText = lines.filter(Boolean).join("\n");
    }
    if (kind === "agent.context.compaction.completed") {
      const compactionCount = Number(payload?.compaction_count);
      const currentTokens = Number(payload?.current_tokens);
      const contextWindow = Number(payload?.context_window_tokens);
      const lines = [detailText];
      if (Number.isFinite(compactionCount) && compactionCount > 0) {
        lines.push(`第 ${compactionCount} 次自动压缩。`);
      }
      if (Number.isFinite(currentTokens) && Number.isFinite(contextWindow)) {
        lines.push(
          `压缩后约 ${formatNumber(currentTokens)} / ${formatNumber(contextWindow)} Token。`,
        );
      }
      detailText = lines.filter(Boolean).join("\n");
      tone = "success";
    }
    if (kind === "agent.output.completed") {
      detailText = "公开输出已完成；完整内容请查看“最终结果”。";
      tone = "success";
    }
    if (kind === "workspace.change.observed") {
      const lines = [detailText];
      const changeLabels = {
        added: "新增",
        deleted: "删除",
        modified: "修改",
        metadata: "仅元数据变化",
      };
      for (const change of Array.isArray(payload?.changes) ? payload.changes : []) {
        lines.push(
          `${change.relative_path} · ${changeLabels[change.change_kind] || "变化"} · ` +
            `+${change.lines_added ?? 0} -${change.lines_deleted ?? 0}`,
        );
        const absolutePath =
          typeof change.absolute_path === "string" ? change.absolute_path : "";
        if (absolutePath) {
          lines.push(`完整路径：${absolutePath}`);
          workspacePaths.push({
            relativePath: String(change.relative_path || "文件"),
            absolutePath,
          });
        }
        if (change.unified_diff) {
          lines.push(String(change.unified_diff));
        }
      }
      if (Number(payload?.details_omitted) > 0) {
        lines.push(
          `另有 ${payload.details_omitted} 个文件详情：未在本次公开名单内，或因安全、大小上限未展示。`,
        );
      }
      lines.push("来源：工作区前后快照；归因：运行时窗观察，未验证由单一进程造成。");
      detailText = lines.filter(Boolean).join("\n");
    }
    const toolOrdinal = Number(payload?.tool_calls);
    const workspaceChangeCount = kind === "workspace.change.observed"
      ? (Array.isArray(payload?.changes) ? payload.changes.length : 0) +
        Math.max(0, Number(payload?.details_omitted) || 0)
      : 0;
    return {
      key: timelineEventKey(item, sourceIndex),
      kind,
      title,
      detail: detailText,
      time: firstDefined(
        item.occurred_utc,
        item.timestamp_utc,
        item.timestamp,
        item.updated_utc,
        item.time,
      ),
      tone,
      workspacePaths,
      workType: kind === "workspace.change.observed"
        ? "workspace_change"
        : String(payload?.item_type || ""),
      workOrdinal: Number.isFinite(toolOrdinal) && toolOrdinal > 0
        ? toolOrdinal
        : null,
      workStatus: String(payload?.command_status || payload?.status || ""),
      workCount: workspaceChangeCount,
    };
    });
  const start = state.timelineBrowsingEarlier
    ? 0
    : Math.max(0, visible.length - MAX_TIMELINE_EVENTS);
  return visible.slice(start, start + MAX_TIMELINE_EVENTS);
}

function normalizedTone(value) {
  if (["running", "active", "queued", "progress"].includes(value)) {
    return "active";
  }
  if (["completed", "success", "succeeded", "ok", "passed"].includes(value)) {
    return "success";
  }
  if (["warning", "partial"].includes(value)) {
    return "warning";
  }
  if (["failed", "error", "danger", "cancelled", "stale"].includes(value)) {
    return "danger";
  }
  return "neutral";
}

function timelineIcon(tone) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute(
    "d",
    tone === "success"
      ? "m6 12 4 4 8-9"
      : tone === "danger"
        ? "M12 7v6m0 4h.01"
        : "M12 7v5l3 2",
  );
  svg.append(path);
  return svg;
}

function timelineIconPath(tone) {
  return tone === "success"
    ? "m6 12 4 4 8-9"
    : tone === "danger"
      ? "M12 7v6m0 4h.01"
      : "M12 7v5l3 2";
}

function createTimelineItem(eventKey) {
  const item = createElement("li", "timeline-item");
  item.dataset.eventKey = eventKey;
  const marker = createElement("span", "timeline-marker");
  const icon = timelineIcon("neutral");
  marker.append(icon);
  const copy = createElement("div", "timeline-copy");
  const title = createElement("h3");
  const time = createElement("time");
  copy.append(title, time);
  item.append(marker, copy);

  const entry = {
    item,
    iconPath: icon.firstElementChild,
    copy,
    title,
    detail: null,
    pathButtons: [],
    time,
    signature: null,
  };
  timelineItemCache.set(eventKey, entry);
  return entry;
}

function updateTimelineItem(entry, event) {
  const tone = normalizedTone(event.tone);
  const signature = JSON.stringify([
    tone,
    event.kind,
    event.title,
    event.detail,
    event.time,
    event.workspacePaths,
  ]);
  if (signature === entry.signature) {
    return;
  }

  entry.signature = signature;
  entry.item.dataset.tone = tone;
  entry.iconPath?.setAttribute("d", timelineIconPath(tone));
  entry.title.textContent = event.title;

  if (event.detail) {
    if (!entry.detail) {
      entry.detail = createElement("p");
      entry.copy.insertBefore(entry.detail, entry.time);
    }
    entry.detail.className =
      event.kind === "workspace.change.observed" ? "workspace-change-detail" : "";
    entry.detail.textContent = event.detail;
  } else if (entry.detail) {
    entry.detail.remove();
    entry.detail = null;
  }

  for (const button of entry.pathButtons) {
    button.remove();
  }
  entry.pathButtons = [];
  for (const path of event.workspacePaths || []) {
    const button = createElement(
      "button",
      "copy-button",
      `复制完整路径：${path.relativePath}`,
    );
    button.type = "button";
    button.setAttribute("aria-label", `复制 ${path.relativePath} 的完整路径`);
    button.addEventListener("click", () =>
      copyText(path.absolutePath, "完整路径已复制"),
    );
    entry.copy.insertBefore(button, entry.time);
    entry.pathButtons.push(button);
  }
  entry.time.textContent = formatDateTime(event.time);
}

function clearTimelineItems() {
  for (const entry of timelineItemCache.values()) {
    entry.item.remove();
  }
  timelineItemCache.clear();
}

function shouldFollowTimelineEnd(runChanged) {
  if (runChanged || timelineItemCache.size === 0) {
    return true;
  }
  const distanceFromEnd =
    elements.timeline.scrollHeight -
    elements.timeline.clientHeight -
    elements.timeline.scrollTop;
  return distanceFromEnd <= 48;
}

function renderTimeline(detail) {
  const sourceDetail = { ...detail, events: state.timelineEvents };
  const events = timelineEvents(sourceDetail);
  const rawCount = state.timelineEvents.length;
  const earlierCount = Number(state.timelinePage?.earlier_count || 0);
  const latestSequence = Number(state.timelinePage?.latest_sequence || 0);
  const loadedLast = eventSequence(state.timelineEvents.at(-1));
  const laterCount = Math.max(0, latestSequence - loadedLast);
  elements.timelineStatus.textContent = events.length
    ? `${rawCount} 个事件 · ${events.length} 个节点` +
      (earlierCount > 0 ? ` · 更早 ${formatNumber(earlierCount)} 个` : "") +
      (laterCount > 0 ? ` · 更晚 ${formatNumber(laterCount)} 个` : "")
    : "暂无事件";
  elements.loadEarlierEvents.hidden = !state.timelinePage?.has_earlier;
  elements.loadEarlierEvents.disabled = state.eventPageLoading;
  elements.returnLatestEvents.hidden = laterCount === 0;
  const timelineJobId = String(firstDefined(detail.job_id, detail.id, state.selectedJobId, ""));
  const runChanged = timelineJobId !== state.timelineJobId;
  const followLatest = shouldFollowTimelineEnd(runChanged);
  state.timelineJobId = timelineJobId;

  if (!events.length) {
    clearTimelineItems();
    if (!elements.timeline.querySelector(".timeline-placeholder")) {
      const placeholder = createElement("li", "timeline-placeholder");
      placeholder.append(
        createElement("strong", "", "等待公开进展"),
        createElement("span", "", "服务端尚未提供可展示的时间线事件"),
      );
      elements.timeline.append(placeholder);
    }
    return;
  }

  elements.timeline.querySelector(".timeline-placeholder")?.remove();
  const currentKeys = new Set(events.map((event) => event.key));
  for (const [eventKey, entry] of timelineItemCache) {
    if (!currentKeys.has(eventKey)) {
      entry.item.remove();
      timelineItemCache.delete(eventKey);
    }
  }

  let cursor = elements.timeline.firstElementChild;
  for (const event of events) {
    const entry = timelineItemCache.get(event.key) || createTimelineItem(event.key);
    updateTimelineItem(entry, event);
    if (entry.item !== cursor) {
      elements.timeline.insertBefore(entry.item, cursor);
    }
    cursor = entry.item.nextElementSibling;
  }

  if (followLatest) {
    elements.timeline.scrollTop = elements.timeline.scrollHeight;
  }
}

function updateTimelineState(detail) {
  const jobId = String(firstDefined(detail.job_id, detail.id, state.selectedJobId, ""));
  const incoming = Array.isArray(detail.events) ? detail.events : [];
  if (jobId !== state.timelineJobId) {
    state.timelineEvents = incoming.slice(-MAX_TIMELINE_EVENTS);
    state.timelinePage = detail.event_page || null;
    state.timelineBrowsingEarlier = false;
    return;
  }
  if (!state.timelineBrowsingEarlier) {
    state.timelineEvents = mergeTimelineEvents(state.timelineEvents, incoming).slice(
      -MAX_TIMELINE_EVENTS,
    );
    state.timelinePage = detail.event_page || state.timelinePage;
  } else if (detail.event_page) {
    state.timelinePage = {
      ...state.timelinePage,
      latest_sequence: detail.event_page.latest_sequence,
    };
  }
}

function extractDraft(detail) {
  return pick(detail, "progress.public_preview");
}

function renderDraft(detail) {
  const jobId = String(firstDefined(detail.job_id, detail.id, state.selectedJobId, ""));
  const result = extractResult(detail);
  const hasFinalResult = result !== undefined && result !== null && result !== "";
  const active = isActive(detail);
  const truncated = pick(detail, "progress.public_preview_truncated") === true;
  const outputState = hasFinalResult
    ? "final"
    : active || extractDraft(detail)
      ? "draft"
      : "terminal-empty";
  const fallback = hasFinalResult
    ? "任务已完成，但没有返回结果"
    : active
      ? "尚未产生公开输出"
      : "任务已结束，但没有可展示结果";
  const nextText = formatStructured(
    hasFinalResult ? result : extractDraft(detail),
    fallback,
  );
  const runChanged = jobId !== state.draftJobId;
  const distanceFromEnd =
    elements.conversationOutput.scrollHeight -
    elements.conversationOutput.clientHeight -
    elements.conversationOutput.scrollTop;
  const followEnd = runChanged || distanceFromEnd <= 32;

  if (!runChanged && nextText.startsWith(state.draftText)) {
    const suffix = nextText.slice(state.draftText.length);
    if (suffix) {
      elements.conversationOutput.append(document.createTextNode(suffix));
    }
  } else if (runChanged || nextText !== state.draftText) {
    elements.conversationOutput.textContent = nextText;
  }

  state.draftJobId = jobId;
  state.draftText = nextText;
  if (followEnd) {
    elements.conversationOutput.scrollTop = elements.conversationOutput.scrollHeight;
  }

  elements.conversationOutputNode.dataset.outputState = outputState;
  elements.conversationOutputTitle.textContent = hasFinalResult ? "最终结果" : "实时草稿";
  elements.conversationOutputState.textContent = hasFinalResult
    ? "最终结果"
    : truncated
      ? "草稿预览已截断"
      : active
        ? "逐段更新中"
        : "等待公开输出";
  elements.conversationOutputState.title = truncated
    ? "草稿预览已达到安全上限；任务完成后请查看最终结果"
    : hasFinalResult
      ? "已在同一输出节点原位切换为最终结果"
      : "公开草稿正在按模型原文逐段追加";
}

function extractResult(detail) {
  return firstDefined(
    pick(detail, "result.output"),
    pick(detail, "result.result"),
    detail.output,
    isCompleted(detail) ? detail.result : undefined,
  );
}

function receiptPayload(detail) {
  const result = detail.result || {};
  const receipt = {};
  for (const key of [
    "context_receipt",
    "delegation_receipt",
    "source_receipt",
    "execution_receipt",
    "delivery_receipt",
  ]) {
    if (result[key] !== undefined) {
      receipt[key] = result[key];
    }
  }
  if (result.checks !== undefined) {
    receipt.checks = result.checks;
  } else if (detail.checks !== undefined) {
    receipt.checks = detail.checks;
  }
  if (detail.error !== undefined) {
    receipt.error = detail.error;
  }
  return receipt;
}

function normalizedChecks(detail) {
  const checks = firstDefined(pick(detail, "result.checks"), detail.checks, []);
  if (!Array.isArray(checks)) {
    return [];
  }
  return checks.map((check, index) => {
    if (typeof check === "string") {
      return { id: `check-${index + 1}`, passed: true, summary: check };
    }
    return {
      id: String(firstDefined(check.label, check.id, check.name, `校验 ${index + 1}`)),
      passed: Boolean(firstDefined(check.passed, check.ok, check.status === "passed")),
      summary: String(
        firstDefined(check.summary, check.message, check.detail) ?? "",
      ),
    };
  });
}

function renderChecks(detail) {
  const checks = normalizedChecks(detail);
  elements.receiptChecks.replaceChildren();
  const passed = checks.filter((check) => check.passed).length;
  elements.receiptSummary.textContent = checks.length ? `${passed}/${checks.length} 通过` : "暂无校验";
  elements.receiptSummary.dataset.tone =
    checks.length === 0 ? "neutral" : passed === checks.length ? "success" : "danger";

  for (const check of checks) {
    const item = createElement("div", "check-item");
    item.dataset.passed = String(check.passed);
    const icon = createElement("span", "check-icon", check.passed ? "✓" : "!");
    icon.setAttribute("aria-hidden", "true");
    const copy = createElement("span", "check-copy");
    copy.append(createElement("strong", "", check.id));
    if (check.summary) {
      copy.append(createElement("span", "", check.summary));
    }
    item.append(icon, copy);
    elements.receiptChecks.append(item);
  }
}

function isOutputEvent(event) {
  return event.kind === "agent.output.delta" ||
    event.kind === "agent.output.delta.batch" ||
    event.kind === "agent.output.completed";
}

function isWorkRecordEvent(event) {
  return event.kind === "agent.tool.activity" ||
    event.kind === "workspace.change.observed";
}

function isContextCompactionEvent(event) {
  return event.kind === "context.compaction.completed" ||
    event.kind === "agent.context.compaction.completed";
}

function isAmbientWorkProgress(event) {
  return event.kind === "work.waiting" ||
    event.kind === "reasoning.activity" ||
    event.kind === "agent.reasoning.activity" ||
    event.kind === "agent.planning.activity";
}

function groupWorkRecords(events) {
  const groups = [];
  let workRecord = null;
  for (const event of events) {
    if (isOutputEvent(event) || event.kind === "agent.context.usage.updated") {
      continue;
    }
    if (isContextCompactionEvent(event)) {
      workRecord = null;
      groups.push({ type: "compaction", event });
      continue;
    }
    if (isWorkRecordEvent(event)) {
      if (!workRecord) {
        workRecord = { type: "work", events: [] };
        groups.push(workRecord);
      }
      workRecord.events.push(event);
      continue;
    }
    if (workRecord && isAmbientWorkProgress(event)) {
      continue;
    }
    workRecord = null;
    groups.push({ type: "activity", event });
  }
  return groups;
}

function coalesceWorkRecordEvents(events) {
  const items = [];
  const keyedIndexes = new Map();
  const terminalStatuses = new Set([
    "completed",
    "succeeded",
    "failed",
    "declined",
    "cancelled",
    "timed_out",
  ]);
  for (const event of events) {
    const hasOrdinal = Number.isFinite(event.workOrdinal) && event.workOrdinal > 0;
    if (!hasOrdinal || !event.workType) {
      items.push(event);
      continue;
    }
    const key = `${event.workType}:${event.workOrdinal}`;
    const existingIndex = keyedIndexes.get(key);
    if (existingIndex === undefined) {
      keyedIndexes.set(key, items.length);
      items.push(event);
      continue;
    }
    const existing = items[existingIndex];
    if (
      terminalStatuses.has(existing.workStatus) &&
      !terminalStatuses.has(event.workStatus)
    ) {
      continue;
    }
    items[existingIndex] = event;
  }
  return items;
}

function appendWorkRecordItem(record, event) {
  const item = createElement("li", "work-record-item");
  item.dataset.tone = normalizedTone(event.tone);
  item.append(createElement("span", "work-record-dot"));
  const copy = createElement("span", "work-record-copy");
  copy.append(createElement("strong", "", event.title));
  if (event.detail) {
    copy.append(createElement("span", "", event.detail));
  }
  item.append(copy, createElement("time", "", formatDateTime(event.time)));
  record.append(item);
}

function countToolActivities(events, type) {
  const matching = events.filter((event) => event.workType === type);
  if (!matching.length) {
    return 0;
  }
  const ordinals = new Set(
    matching
      .map((event) => event.workOrdinal)
      .filter((value) => Number.isFinite(value) && value > 0),
  );
  if (ordinals.size) {
    return ordinals.size;
  }
  const terminal = matching.filter((event) =>
    ["completed", "succeeded", "failed", "declined"].includes(event.workStatus),
  );
  return terminal.length || 1;
}

function summarizeWorkRecord(events) {
  const parts = [];
  const commandCount = countToolActivities(events, "command_execution");
  const observedFileCount = events
    .filter((event) => event.workType === "workspace_change")
    .reduce((sum, event) => sum + Math.max(0, Number(event.workCount) || 0), 0);
  const workspaceEventCount = events.filter(
    (event) => event.workType === "workspace_change",
  ).length;
  const fileEditCount = countToolActivities(events, "file_change");
  const webSearchCount = countToolActivities(events, "web_search");
  const mcpCount = countToolActivities(events, "mcp_tool_call");
  const computerUseCount = countToolActivities(events, "computer_use");
  if (commandCount) {
    parts.push(`运行 ${formatNumber(commandCount)} 个命令`);
  }
  if (fileEditCount) {
    parts.push(`编辑 ${formatNumber(fileEditCount)} 个文件`);
  }
  if (observedFileCount) {
    parts.push(`检测到 ${formatNumber(observedFileCount)} 个文件变化`);
  } else if (workspaceEventCount) {
    parts.push(`检测工作区变化 ${formatNumber(workspaceEventCount)} 次`);
  }
  if (webSearchCount) {
    parts.push(`查询公开资料 ${formatNumber(webSearchCount)} 次`);
  }
  if (mcpCount) {
    parts.push(`调用 MCP 工具 ${formatNumber(mcpCount)} 次`);
  }
  if (computerUseCount) {
    parts.push(`操作计算机 ${formatNumber(computerUseCount)} 次`);
  }
  return parts.length
    ? parts.join(" · ")
    : `${formatNumber(events.length)} 条安全工具活动`;
}

function renderWorkRecords(events) {
  const fragment = document.createDocumentFragment();
  for (const group of groupWorkRecords(events)) {
    if (group.type === "compaction") {
      const divider = createElement("div", "context-compaction-divider");
      divider.textContent = group.event.detail
        ? `${group.event.title} · ${group.event.detail}`
        : group.event.title;
      fragment.append(divider);
      continue;
    }
    if (group.type === "work") {
      const workItems = coalesceWorkRecordEvents(group.events);
      const record = createElement("section", "work-record");
      record.title = "仅展示安全状态、摘要和结果，不展示命令正文。";
      const header = createElement("div", "work-record-header");
      const heading = createElement("div");
      heading.append(createElement("span", "node-kind", "工作记录"));
      heading.append(
        createElement("h3", "", "工作记录"),
      );
      header.append(
        heading,
        createElement("span", "quiet-label", `${formatNumber(workItems.length)} 项工作`),
      );
      record.append(header);
      record.append(
        createElement(
          "p",
          "work-record-summary",
          summarizeWorkRecord(workItems),
        ),
      );
      const list = createElement("ol", "work-record-list");
      for (const event of workItems) {
        appendWorkRecordItem(list, event);
      }
      record.append(list);
      fragment.append(record);
      continue;
    }

    const event = group.event;
    const activity = createElement("article", "conversation-activity");
    activity.dataset.tone = normalizedTone(event.tone);
    const header = createElement("div", "conversation-activity-header");
    const heading = createElement("div");
    heading.append(createElement("span", "node-kind", "运行状态"));
    heading.append(createElement("h3", "", event.title));
    header.append(heading, createElement("time", "", formatDateTime(event.time)));
    activity.append(header);
    if (event.detail) {
      activity.append(createElement("p", "", event.detail));
    }
    fragment.append(activity);
  }
  elements.conversationWorkRecords.replaceChildren(fragment);
}

function conversationTimelineEvents(detail) {
  return timelineEvents({ ...detail, events: state.timelineEvents });
}

function conversationScrollContainer() {
  return window.matchMedia("(max-width: 680px)").matches
    ? document.scrollingElement || elements.conversationPane
    : elements.conversationPane;
}

function conversationDistanceFromEnd() {
  const container = conversationScrollContainer();
  return container.scrollHeight - container.clientHeight - container.scrollTop;
}

function returnConversationToLatest({ behavior = "auto" } = {}) {
  const container = conversationScrollContainer();
  container.scrollTo({ top: container.scrollHeight, behavior });
}

function clearConversationNewEventsIfAtEnd() {
  if (conversationDistanceFromEnd() <= 48 && state.conversationPendingCount > 0) {
    state.conversationPendingCount = 0;
    elements.conversationNewEvents.hidden = true;
  }
}

function updateConversationNewEvents(detail, events, { runChanged, followEnd }) {
  const latestSequence = Number(
    state.timelinePage?.latest_sequence || eventSequence(state.timelineEvents.at(-1)),
  );
  if (runChanged) {
    state.conversationLatestSequence = Number.isFinite(latestSequence) ? latestSequence : 0;
    state.conversationPendingCount = 0;
    elements.conversationNewEvents.hidden = true;
    return;
  }
  const newlyArrived = Number.isFinite(latestSequence)
    ? Math.max(0, latestSequence - state.conversationLatestSequence)
    : 0;
  state.conversationLatestSequence = Math.max(
    state.conversationLatestSequence,
    Number.isFinite(latestSequence) ? latestSequence : 0,
  );
  if (followEnd) {
    state.conversationPendingCount = 0;
    elements.conversationNewEvents.hidden = true;
    return;
  }
  if (newlyArrived > 0) {
    state.conversationPendingCount += newlyArrived;
  }
  const laterCount = state.conversationPendingCount;
  elements.conversationNewEvents.hidden = laterCount === 0 || events.length === 0;
  if (laterCount > 0) {
    elements.conversationNewEvents.textContent = `新增 ${formatNumber(laterCount)} 条`;
  }
}

function renderConversation(detail) {
  state.selectedDetail = detail;
  state.durationObservedAt = Date.now();
  if (isActive(detail)) {
    state.lastActiveRefresh = Date.now();
  }
  const jobId = String(firstDefined(detail.job_id, detail.id, state.selectedJobId, ""));
  const runChanged = jobId !== state.conversationJobId;
  const followEnd = runChanged || conversationDistanceFromEnd() <= 48;
  state.conversationJobId = jobId;

  const info = statusInfo(detail);
  elements.conversationEmptyState.hidden = true;
  elements.conversationContent.hidden = false;
  elements.inspectorEmptyState.hidden = true;
  elements.inspectorContent.hidden = false;
  elements.runStatus.textContent = info.label;
  elements.runStatus.dataset.tone = info.tone;
  elements.runId.textContent = jobId;
  elements.runTitle.textContent = runTitle(detail);
  elements.conversationLabel.textContent = conversationLabel(detail);
  elements.conversationMeta.textContent = `${modelName(detail)} · ${executionMode(detail)} · ${formatDateTime(
    firstDefined(detail.updated_utc, detail.created_utc),
  )}`;

  const tokens = tokenSummary(detail);
  const context = contextSummary(detail);
  const tps = calculateTps(detail);

  elements.metrics.model.textContent = modelName(detail);
  elements.metrics.execution.textContent = executionMode(detail);
  elements.metrics.reasoning.textContent = reasoningLevel(detail);
  elements.metrics.tokens.textContent = tokens.label;
  elements.metrics.context.textContent = context.label;
  elements.metrics.tps.textContent = tps;
  elements.metrics.duration.textContent = calculateDuration(detail);
  elements.metrics.gpu.textContent = gpuLabel(detail);
  elements.metrics.delivery.textContent = deliveryLabel(detail);
  for (const metric of Object.values(elements.metrics)) {
    metric.title = metric.textContent;
  }
  elements.metrics.tokens.title = tokens.detail;
  elements.tokenDetail.textContent = tokens.detail;
  elements.metrics.context.title = context.detail;
  elements.contextDetail.textContent = context.detail;
  elements.metrics.tps.title = tps;

  updateTimelineState(detail);
  renderDraft(detail);
  const events = conversationTimelineEvents(detail);
  renderWorkRecords(events);
  renderTimeline(detail);
  renderChecks(detail);
  elements.receiptContent.textContent = formatStructured(receiptPayload(detail), "暂无回执");
  updateConversationNewEvents(detail, events, { runChanged, followEnd });
  if (followEnd) {
    returnConversationToLatest();
  }
}

function renderDetail(detail) {
  renderConversation(detail);
}

function renderNoSelection() {
  state.selectedDetail = null;
  elements.conversationEmptyState.hidden = false;
  elements.conversationContent.hidden = true;
  elements.inspectorEmptyState.hidden = false;
  elements.inspectorContent.hidden = true;
  clearTimelineItems();
  elements.timeline.querySelector(".timeline-placeholder")?.remove();
  state.timelineJobId = null;
  state.timelineEvents = [];
  state.timelinePage = null;
  state.timelineBrowsingEarlier = false;
  state.draftJobId = null;
  state.draftText = "";
  state.conversationJobId = null;
  state.conversationLatestSequence = 0;
  state.conversationPendingCount = 0;
  elements.conversationNewEvents.hidden = true;
  elements.conversationWorkRecords.replaceChildren();
  elements.timelineStatus.textContent = "未选择";
}

function setConnection(stateName, label) {
  elements.connectionState.dataset.state = stateName;
  elements.connectionState.lastElementChild.textContent = label;
}

function showToast(message) {
  clearTimeout(state.toastTimer);
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  state.toastTimer = setTimeout(() => {
    elements.toast.hidden = true;
  }, 2200);
}

async function requestJson(url) {
  const response = await fetch(url, {
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  return response.json();
}

function mergeRunPage(existing, incoming, { prepend = false } = {}) {
  if (prepend) {
    const incomingIds = new Set(incoming.map(runJobId));
    return [...incoming, ...existing.filter((run) => !incomingIds.has(runJobId(run)))];
  }

  const merged = [...existing];
  const indexes = new Map(merged.map((run, index) => [runJobId(run), index]));
  for (const run of incoming) {
    const jobId = runJobId(run);
    if (indexes.has(jobId)) {
      merged[indexes.get(jobId)] = run;
    } else {
      indexes.set(jobId, merged.length);
      merged.push(run);
    }
  }
  return merged;
}

async function loadRuns({ quiet = false, append = false } = {}) {
  if (state.listRequestInFlight) {
    if (append) {
      state.appendQueued = true;
    } else {
      state.refreshQueued = true;
    }
    return;
  }
  if (append && state.nextOffset === null) {
    return;
  }

  state.listRequestInFlight = true;
  if (!quiet) {
    elements.refreshButton.classList.add("is-loading");
  }
  if (append) {
    elements.loadMoreButton.disabled = true;
  }
  try {
    const offset = append ? state.nextOffset : 0;
    const limit = HISTORY_PAGE_SIZE;
    const payload = await requestJson(`/api/runs?limit=${limit}&offset=${offset}`);
    const incoming = normalizedRuns(payload);
    const hadRuns = state.runs.length > 0;
    const previousTotal = state.historyTotal;
    if (append) {
      state.runs = mergeRunPage(state.runs, incoming);
    } else if (hadRuns) {
      state.runs = mergeRunPage(state.runs, incoming, { prepend: true });
    } else {
      state.runs = incoming;
    }
    state.historyTotal = Number(firstDefined(payload.total, state.runs.length));
    if (Number.isFinite(state.historyTotal) && state.runs.length > state.historyTotal) {
      state.runs = state.runs.slice(0, state.historyTotal);
    }
    const responseNextOffset =
      payload.next_offset === null || payload.next_offset === undefined
        ? null
        : Number(payload.next_offset);
    if (append || !hadRuns) {
      state.nextOffset = responseNextOffset;
    } else if (state.runs.length >= state.historyTotal) {
      state.nextOffset = null;
    } else {
      const totalIncrease = Math.max(0, state.historyTotal - previousTotal);
      state.nextOffset =
        totalIncrease > incoming.length || state.nextOffset === null
          ? responseNextOffset
          : Math.min(state.historyTotal, state.nextOffset + totalIncrease);
    }
    renderRunList();

    if (append) {
      // Loading older history must not disturb the currently observed run.
    } else if (!state.selectedJobId && state.runs.length) {
      const firstId = String(firstDefined(state.runs[0].job_id, state.runs[0].id, ""));
      if (firstId) {
        await selectRun(firstId, { quiet: true });
      }
    } else if (state.selectedJobId) {
      await loadDetail(state.selectedJobId, { quiet: true });
    } else {
      renderNoSelection();
    }
    elements.lastUpdated.textContent = `更新于 ${formatDateTime(new Date().toISOString())}`;
    if (!state.eventSource) {
      setConnection("polling", "轮询同步");
    }
  } catch (error) {
    setConnection("offline", "连接中断");
    if (!quiet) {
      showToast(`刷新失败：${error.message}`);
    }
    startPolling();
  } finally {
    state.listRequestInFlight = false;
    elements.refreshButton.classList.remove("is-loading");
    elements.loadMoreButton.disabled = false;
    let scheduledAppend = false;
    if (state.appendQueued) {
      state.appendQueued = false;
      if (state.nextOffset !== null) {
        void loadRuns({ quiet: true, append: true });
        scheduledAppend = true;
      }
    }
    if (!scheduledAppend && state.refreshQueued) {
      state.refreshQueued = false;
      void loadRuns({ quiet: true });
    }
  }
}

async function loadDetail(jobId, { quiet = false } = {}) {
  const serial = ++state.detailRequestSerial;
  try {
    const detail = await requestJson(`/api/runs/${encodeURIComponent(jobId)}`);
    if (serial !== state.detailRequestSerial || state.selectedJobId !== jobId) {
      return;
    }
    renderDetail(detail);
  } catch (error) {
    if (!quiet) {
      showToast(`详情加载失败：${error.message}`);
    }
  }
}

async function loadEarlierEvents() {
  if (
    state.eventPageLoading ||
    !state.selectedJobId ||
    !state.timelinePage?.has_earlier
  ) {
    return;
  }
  const before = Number(state.timelinePage.next_before_sequence);
  if (!Number.isFinite(before) || before <= 1) {
    return;
  }
  state.eventPageLoading = true;
  elements.loadEarlierEvents.disabled = true;
  const previousHeight = elements.timeline.scrollHeight;
  const previousTop = elements.timeline.scrollTop;
  const anchor = [...elements.timeline.querySelectorAll(".timeline-item")].find(
    (item) => item.offsetTop + item.offsetHeight >= previousTop,
  );
  const anchorKey = anchor?.dataset.eventKey;
  const anchorOffset = anchor ? anchor.offsetTop - previousTop : 0;
  try {
    const page = await requestJson(
      `/api/runs/${encodeURIComponent(state.selectedJobId)}/events` +
        `?limit=${EVENT_PAGE_SIZE}&before_sequence=${before}`,
    );
    if (state.selectedJobId !== page.job_id) {
      return;
    }
    state.timelineEvents = mergeTimelineEvents(
      state.timelineEvents,
      Array.isArray(page.events) ? page.events : [],
    ).slice(0, MAX_TIMELINE_EVENTS);
    state.timelinePage = {
      ...(page.event_page || {}),
      latest_sequence: state.timelinePage?.latest_sequence,
    };
    state.timelineBrowsingEarlier = true;
    renderConversation(state.selectedDetail || { job_id: state.selectedJobId });
    const retainedAnchor = anchorKey ? timelineItemCache.get(anchorKey)?.item : null;
    elements.timeline.scrollTop = retainedAnchor
      ? retainedAnchor.offsetTop - anchorOffset
      : previousTop + Math.max(0, elements.timeline.scrollHeight - previousHeight);
  } catch (error) {
    showToast(`加载更早事件失败：${error.message}`);
  } finally {
    state.eventPageLoading = false;
    elements.loadEarlierEvents.disabled = false;
  }
}

async function returnToLatestEvents() {
  if (!state.selectedJobId) {
    return;
  }
  state.timelineBrowsingEarlier = false;
  state.timelineEvents = [];
  state.timelinePage = null;
  state.timelineJobId = null;
  await loadDetail(state.selectedJobId);
}

function tickActiveDetail() {
  const detail = state.selectedDetail;
  if (!detail || !isActive(detail)) {
    return;
  }
  const baseSeconds = Number(
    pick(
      detail,
      "performance.elapsed_seconds",
      "progress.metrics.elapsed_seconds",
    ),
  );
  const observedAt = Number(state.durationObservedAt);
  if (Number.isFinite(baseSeconds) && Number.isFinite(observedAt)) {
    const liveSeconds = baseSeconds + Math.max(0, Date.now() - observedAt) / 1000;
    elements.metrics.duration.textContent = formatDuration(liveSeconds, "seconds");
    elements.metrics.duration.title = elements.metrics.duration.textContent;
  }
  const now = Date.now();
  if (
    !document.hidden &&
    now - state.lastActiveRefresh >= ACTIVE_REFRESH_INTERVAL_MS
  ) {
    state.lastActiveRefresh = now;
    void loadRuns({ quiet: true });
  }
}

async function selectRun(
  jobId,
  { quiet = false, revealConversation = false } = {},
) {
  if (!jobId) {
    return;
  }
  state.selectedJobId = jobId;
  renderRunList();
  if (revealConversation && window.matchMedia("(max-width: 680px)").matches) {
    document.body.dataset.mobileView = "conversation";
  }
  await loadDetail(jobId, { quiet });
}

function setInspectorOpen(open) {
  const isOpen = Boolean(open);
  document.body.dataset.inspectorOpen = String(isOpen);
  elements.inspectorToggle.setAttribute("aria-expanded", String(isOpen));
}

function scheduleRefresh() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(() => loadRuns({ quiet: true }), REFRESH_DEBOUNCE_MS);
}

function startPolling() {
  if (state.pollingTimer) {
    return;
  }
  setConnection("polling", "轮询同步");
  state.pollingTimer = setInterval(() => loadRuns({ quiet: true }), POLL_INTERVAL_MS);
}

function stopPolling() {
  if (state.pollingTimer) {
    clearInterval(state.pollingTimer);
    state.pollingTimer = null;
  }
}

function connectStream() {
  if (!("EventSource" in window)) {
    startPolling();
    return;
  }
  if (state.eventSource) {
    state.eventSource.close();
  }

  const eventSource = new EventSource("/api/stream");
  state.eventSource = eventSource;
  eventSource.onopen = () => {
    if (state.eventSource !== eventSource) {
      return;
    }
    stopPolling();
    setConnection("live", "实时同步");
  };
  eventSource.onmessage = () => scheduleRefresh();
  eventSource.addEventListener("refresh", () => scheduleRefresh());
  eventSource.onerror = () => {
    if (state.eventSource === eventSource) {
      startPolling();
      setConnection("polling", "正在重连");
    }
  };
}

async function copyText(text, successMessage) {
  try {
    await navigator.clipboard.writeText(text);
    showToast(successMessage);
  } catch {
    showToast("复制失败，请手动选择文本");
  }
}

elements.refreshButton.addEventListener("click", () => loadRuns());
elements.loadMoreButton.addEventListener("click", () =>
  loadRuns({ quiet: true, append: true }),
);
elements.loadEarlierEvents.addEventListener("click", loadEarlierEvents);
elements.returnLatestEvents.addEventListener("click", returnToLatestEvents);
elements.runSearch.addEventListener("input", (event) => {
  state.query = event.target.value;
  renderRunList();
});

for (const button of elements.filterButtons) {
  button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    for (const item of elements.filterButtons) {
      const active = item === button;
      item.classList.toggle("is-active", active);
      item.setAttribute("aria-pressed", String(active));
    }
    renderRunList();
  });
}

elements.copyIdButton.addEventListener("click", () => {
  if (state.selectedJobId) {
    copyText(state.selectedJobId, "调用 ID 已复制");
  }
});

elements.mobileBackButton.addEventListener("click", () => {
  document.body.dataset.mobileView = "list";
  elements.runSearch.focus({ preventScroll: true });
});
elements.inspectorToggle.addEventListener("click", () =>
  setInspectorOpen(document.body.dataset.inspectorOpen !== "true"),
);
elements.inspectorCloseButton.addEventListener("click", () => setInspectorOpen(false));
elements.conversationNewEvents.addEventListener("click", async () => {
  if (state.timelineBrowsingEarlier) {
    await returnToLatestEvents();
  }
  returnConversationToLatest({ behavior: "smooth" });
  state.conversationPendingCount = 0;
  elements.conversationNewEvents.hidden = true;
});
elements.conversationPane.addEventListener("scroll", clearConversationNewEventsIfAtEnd);
window.addEventListener("scroll", clearConversationNewEventsIfAtEnd, { passive: true });

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    scheduleRefresh();
  }
});

window.addEventListener("beforeunload", () => {
  state.eventSource?.close();
  stopPolling();
  clearInterval(state.activeTimer);
});

state.activeTimer = setInterval(tickActiveDetail, 1000);
loadRuns();
connectStream();
