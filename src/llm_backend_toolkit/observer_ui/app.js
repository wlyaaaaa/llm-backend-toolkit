"use strict";

const POLL_INTERVAL_MS = 5000;
const REFRESH_DEBOUNCE_MS = 180;
const HISTORY_PAGE_SIZE = 100;

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
  activeTab: "draft",
  historyTotal: 0,
  nextOffset: null,
  timelineSignature: null,
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
  emptyState: document.querySelector("#empty-state"),
  detailContent: document.querySelector("#detail-content"),
  timeline: document.querySelector("#timeline"),
  timelineStatus: document.querySelector("#timeline-status"),
  runStatus: document.querySelector("#run-status"),
  runId: document.querySelector("#run-id"),
  runTitle: document.querySelector("#run-title"),
  copyIdButton: document.querySelector("#copy-id-button"),
  draftLiveIndicator: document.querySelector("#draft-live-indicator"),
  draftContent: document.querySelector("#draft-content"),
  resultContent: document.querySelector("#result-content"),
  receiptSummary: document.querySelector("#receipt-summary"),
  receiptChecks: document.querySelector("#receipt-checks"),
  receiptContent: document.querySelector("#receipt-content"),
  detailTabs: [...document.querySelectorAll(".detail-tab")],
  tabPanels: [...document.querySelectorAll('[role="tabpanel"]')],
  toast: document.querySelector("#toast"),
  metrics: {
    model: document.querySelector("#metric-model"),
    execution: document.querySelector("#metric-execution"),
    reasoning: document.querySelector("#metric-reasoning"),
    tokens: document.querySelector("#metric-tokens"),
    tps: document.querySelector("#metric-tps"),
    duration: document.querySelector("#metric-duration"),
    gpu: document.querySelector("#metric-gpu"),
    delivery: document.querySelector("#metric-delivery"),
  },
};

const runItemCache = new Map();

const STATUS_MAP = {
  accepted: { label: "已接收", tone: "neutral" },
  queued: { label: "排队中", tone: "active" },
  running: { label: "执行中", tone: "active" },
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
  return ["accepted", "queued", "running"].includes(normalizedStatus(run));
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
  if (seconds < 60) {
    return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} 秒`;
  }
  const minutes = Math.floor(seconds / 60);
  const remaining = Math.round(seconds % 60);
  return `${minutes} 分 ${remaining} 秒`;
}

function calculateDuration(detail) {
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
    return `${exact.toFixed(exact < 10 ? 1 : 0)}（精确）`;
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
    return ["exact", "eval_duration"].includes(source)
      ? `${value}（精确）`
      : `≈ ${value}（估算）`;
  }
  const durationNs = Number(pick(detail, "result.usage.total_duration_ns", "usage.total_duration_ns"));
  if (Number.isFinite(completionTokens) && Number.isFinite(durationNs) && durationNs > 0) {
    const estimate = completionTokens / (durationNs / 1_000_000_000);
    return `≈ ${estimate.toFixed(estimate < 10 ? 1 : 0)}（估算）`;
  }
  return "—";
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
  button.addEventListener("click", () => selectRun(button.dataset.jobId));

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
    "agent.observability": "AICLI 可观察性",
    "agent.reasoning.activity": "智能体分析活动",
    "agent.tool.activity": "智能体工具活动",
    "agent.output.completed": "智能体公开输出",
    "media.ocr.started": "LocalOCR 开始",
    "media.ocr.completed": "LocalOCR 完成",
    "media.asr.started": "ChineseASR 开始",
    "media.asr.completed": "ChineseASR 完成",
  };
  return provided.map((item) => {
    const kind = String(firstDefined(item.kind, item.type, "event"));
    const metrics = item.metrics && typeof item.metrics === "object" ? item.metrics : undefined;
    const payload = item.payload && typeof item.payload === "object" ? item.payload : undefined;
    let title = String(firstDefined(item.title, item.label, kindLabels[kind], kind));
    if (kind === "agent.tool.activity" && payload?.item_type === "file_change") {
      title = payload.status === "completed" ? "完成编辑文件" : "正在编辑文件";
    }
    return {
      title,
      detail: formatStructured(
        firstDefined(item.summary_zh, item.public_summary, item.summary, metrics, ""),
      ),
      time: firstDefined(
        item.occurred_utc,
        item.timestamp_utc,
        item.timestamp,
        item.updated_utc,
        item.time,
      ),
      tone: String(firstDefined(item.tone, item.status, kind)).toLowerCase(),
    };
  });
}

function normalizedTone(value) {
  if (["running", "active", "queued", "progress"].includes(value)) {
    return "active";
  }
  if (["completed", "success", "succeeded", "ok", "passed"].includes(value)) {
    return "success";
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

function renderTimeline(detail) {
  const events = timelineEvents(detail);
  elements.timelineStatus.textContent = events.length ? `${events.length} 个节点` : "暂无事件";
  const signature = JSON.stringify(events);
  if (signature === state.timelineSignature) {
    return;
  }
  state.timelineSignature = signature;
  elements.timeline.replaceChildren();
  if (!events.length) {
    const placeholder = createElement("li", "timeline-placeholder");
    placeholder.append(
      createElement("strong", "", "等待公开进展"),
      createElement("span", "", "服务端尚未提供可展示的时间线事件"),
    );
    elements.timeline.append(placeholder);
    return;
  }
  for (const event of events) {
    const tone = normalizedTone(event.tone);
    const item = createElement("li", "timeline-item");
    item.dataset.tone = tone;
    const marker = createElement("span", "timeline-marker");
    marker.append(timelineIcon(tone));
    const copy = createElement("div", "timeline-copy");
    copy.append(createElement("h3", "", event.title));
    if (event.detail) {
      copy.append(createElement("p", "", event.detail));
    }
    copy.append(createElement("time", "", formatDateTime(event.time)));
    item.append(marker, copy);
    elements.timeline.append(item);
  }
}

function extractDraft(detail) {
  return pick(detail, "progress.public_preview");
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

function selectDetailTab(tabName, { focus = false } = {}) {
  const selected = elements.detailTabs.find((tab) => tab.dataset.tab === tabName);
  if (!selected) {
    return;
  }
  state.activeTab = tabName;
  for (const tab of elements.detailTabs) {
    const active = tab === selected;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  }
  for (const panel of elements.tabPanels) {
    panel.hidden = panel.id !== selected.getAttribute("aria-controls");
  }
  if (focus) {
    selected.focus();
  }
}

function renderDetail(detail) {
  state.selectedDetail = detail;
  const info = statusInfo(detail);
  elements.emptyState.hidden = true;
  elements.detailContent.hidden = false;
  elements.runStatus.textContent = info.label;
  elements.runStatus.dataset.tone = info.tone;
  elements.runId.textContent = String(firstDefined(detail.job_id, detail.id, state.selectedJobId, ""));
  elements.runTitle.textContent = runTitle(detail);

  const totalTokens = firstDefined(
    pick(detail, "result.usage.total_tokens"),
    pick(detail, "usage.total_tokens"),
  );
  const promptTokens = pick(detail, "result.usage.prompt_tokens", "usage.prompt_tokens");
  const completionTokens = pick(
    detail,
    "result.usage.completion_tokens",
    "result.usage.output_tokens",
    "usage.completion_tokens",
    "usage.output_tokens",
  );
  const tokenEvents = pick(
    detail,
    "progress.metrics.token_events",
    "metrics.token_events",
    "progress.token_events",
  );
  const estimatedOutputTokens = pick(
    detail,
    "progress.metrics.estimated_output_tokens",
    "metrics.estimated_output_tokens",
  );
  const tokenLabel =
    totalTokens !== undefined
      ? formatNumber(totalTokens)
      : promptTokens !== undefined || completionTokens !== undefined
        ? `${formatNumber(promptTokens || 0)} + ${formatNumber(completionTokens || 0)}`
        : estimatedOutputTokens !== undefined && Number(estimatedOutputTokens) > 0
          ? `≈ ${formatNumber(estimatedOutputTokens)}`
        : tokenEvents !== undefined
          ? `暂无（${formatNumber(tokenEvents)} 片段）`
          : "—";

  elements.metrics.model.textContent = modelName(detail);
  elements.metrics.execution.textContent = executionMode(detail);
  elements.metrics.reasoning.textContent = reasoningLevel(detail);
  elements.metrics.tokens.textContent = tokenLabel;
  elements.metrics.tps.textContent = calculateTps(detail);
  elements.metrics.duration.textContent = calculateDuration(detail);
  elements.metrics.gpu.textContent = gpuLabel(detail);
  elements.metrics.delivery.textContent = deliveryLabel(detail);
  for (const metric of Object.values(elements.metrics)) {
    metric.title = metric.textContent;
  }

  const draft = extractDraft(detail);
  const result = extractResult(detail);
  elements.draftContent.textContent = formatStructured(draft, "尚未产生草稿");
  elements.resultContent.textContent = formatStructured(
    result,
    isCompleted(detail) ? "任务已完成，但没有返回结果" : "等待任务完成",
  );
  elements.draftLiveIndicator.hidden = !isActive(detail);

  renderTimeline(detail);
  renderChecks(detail);
  elements.receiptContent.textContent = formatStructured(receiptPayload(detail), "暂无回执");
}

function renderNoSelection() {
  state.selectedDetail = null;
  elements.emptyState.hidden = false;
  elements.detailContent.hidden = true;
  elements.timeline.replaceChildren();
  state.timelineSignature = null;
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

async function selectRun(jobId, { quiet = false } = {}) {
  if (!jobId) {
    return;
  }
  state.selectedJobId = jobId;
  renderRunList();
  await loadDetail(jobId, { quiet });
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

for (const button of document.querySelectorAll("[data-copy-target]")) {
  button.addEventListener("click", () => {
    const target = document.querySelector(`#${button.dataset.copyTarget}`);
    if (target) {
      copyText(target.textContent, "内容已复制");
    }
  });
}

for (const tab of elements.detailTabs) {
  tab.addEventListener("click", () => selectDetailTab(tab.dataset.tab));
  tab.addEventListener("keydown", (event) => {
    const currentIndex = elements.detailTabs.indexOf(tab);
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight") {
      nextIndex = (currentIndex + 1) % elements.detailTabs.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (currentIndex - 1 + elements.detailTabs.length) % elements.detailTabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = elements.detailTabs.length - 1;
    } else {
      return;
    }
    event.preventDefault();
    selectDetailTab(elements.detailTabs[nextIndex].dataset.tab, { focus: true });
  });
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    scheduleRefresh();
  }
});

window.addEventListener("beforeunload", () => {
  state.eventSource?.close();
  stopPolling();
});

loadRuns();
connectStream();
