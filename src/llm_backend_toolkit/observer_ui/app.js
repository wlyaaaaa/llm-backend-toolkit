"use strict";

const POLL_INTERVAL_MS = 5000;
const PAGE_SIZE = 100;
const EVENT_PAGE_SIZE = 160;
const MAX_EVENTS_PER_TURN = 240;
const MAX_PINNED_EVENTS_PER_TURN = 8;

const state = {
  conversations: [],
  total: 0,
  nextOffset: null,
  selectedRootId: null,
  selectedDetail: null,
  eventPages: new Map(),
  eventSource: null,
  pollTimer: null,
  durationTimer: null,
  durationObservation: null,
  refreshTimer: null,
};

const elements = {
  list: document.querySelector("#conversation-list"),
  count: document.querySelector("#conversation-count"),
  listEmpty: document.querySelector("#list-empty"),
  loadMore: document.querySelector("#load-more"),
  connection: document.querySelector("#connection-state"),
  refresh: document.querySelector("#refresh-button"),
  lastUpdated: document.querySelector("#last-updated"),
  status: document.querySelector("#conversation-status"),
  turnCount: document.querySelector("#turn-count-label"),
  title: document.querySelector("#conversation-title"),
  subtitle: document.querySelector("#conversation-subtitle"),
  scroll: document.querySelector("#conversation-scroll"),
  empty: document.querySelector("#conversation-empty"),
  feed: document.querySelector("#conversation-feed"),
  turns: document.querySelector("#turns"),
  factsEmpty: document.querySelector("#facts-empty"),
  factsContent: document.querySelector("#facts-content"),
  factStatus: document.querySelector("#fact-status"),
  factTurns: document.querySelector("#fact-turns"),
  factModel: document.querySelector("#fact-model"),
  factExecution: document.querySelector("#fact-execution"),
  factReasoning: document.querySelector("#fact-reasoning"),
  factDuration: document.querySelector("#fact-duration"),
  factTokens: document.querySelector("#fact-tokens"),
  factTps: document.querySelector("#fact-tps"),
  factContext: document.querySelector("#fact-context"),
  factContextBar: document.querySelector("#fact-context-bar"),
  factHandoff: document.querySelector("#fact-handoff"),
  factUpdated: document.querySelector("#fact-updated"),
  factRootId: document.querySelector("#fact-root-id"),
  toast: document.querySelector("#toast"),
};

const STATUS = {
  accepted: ["已接受", "live"],
  queued: ["排队中", "live"],
  running: ["执行中", "live"],
  cancellation_requested: ["正在结束", "warning"],
  completed: ["已完成", "quiet"],
  failed: ["失败", "danger"],
  cancelled: ["已取消", "warning"],
  stale: ["已失联", "warning"],
};

function createElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function first(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== "");
}

function nested(object, path) {
  return path.split(".").reduce(
    (value, key) => (value && typeof value === "object" ? value[key] : undefined),
    object,
  );
}

function pick(object, ...paths) {
  return first(...paths.map((path) => nested(object, path)));
}

function statusName(value) {
  return String(value || "").trim().toLowerCase();
}

function conversationStatus(conversation) {
  return statusName(first(conversation?.job_status, conversation?.result_status, "unknown"));
}

function statusInfo(value) {
  return STATUS[statusName(value)] || [value ? String(value) : "未知", "quiet"];
}

function isActive(value) {
  return ["accepted", "queued", "running", "cancellation_requested"].includes(statusName(value));
}

function formatDate(value, mode = "time") {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  if (mode === "short") {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "不可用";
  if (value < 1) return `${Math.round(value * 1000)} ms`;
  if (value < 60) return `${value < 10 ? value.toFixed(1) : Math.round(value)} 秒`;
  const roundedSeconds = Math.round(value);
  const minutes = Math.floor(roundedSeconds / 60);
  const remainder = roundedSeconds % 60;
  return `${minutes} 分 ${remainder} 秒`;
}

function formatNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat("zh-CN").format(number) : "不可用";
}

function optionalNumber(value) {
  if (value === undefined || value === null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function modelName(value) {
  return String(first(
    value?.model,
    pick(value, "display.model", "result.model", "result.provider.actual", "result.backend.model", "provider.actual", "backend.model"),
    "模型不可用",
  ));
}

function executionName(value) {
  return String(first(
    pick(value, "display.runner", "display.execution_mode", "result.execution_receipt.runner", "result.execution_receipt.mode"),
    "不可用",
  ));
}

function reasoningName(value) {
  const effort = pick(value, "display.reasoning_effort", "result.execution_receipt.reasoning_effort");
  if (effort !== undefined && effort !== null && effort !== "") return String(effort);
  const mode = statusName(pick(value, "display.reasoning_mode"));
  if (mode === "on") return "开启";
  if (mode === "off") return "关闭";
  return "不可用";
}

function taskLabel(value) {
  return String(first(
    pick(value, "display.task_label", "task_label"),
    "未命名对话",
  ));
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { elements.toast.hidden = true; }, 2600);
}

function setConnection(name, text) {
  elements.connection.dataset.state = name;
  elements.connection.textContent = text;
}

async function requestJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function renderList() {
  const fragment = document.createDocumentFragment();
  for (const conversation of state.conversations) {
    const rootId = String(conversation.root_job_id || "");
    const button = createElement("button", "conversation-item");
    button.type = "button";
    button.dataset.rootId = rootId;
    button.setAttribute("aria-current", String(rootId === state.selectedRootId));

    const top = createElement("span", "item-top");
    const title = createElement("span", "item-title", taskLabel(conversation));
    const dot = createElement("span", "item-status");
    dot.dataset.tone = statusInfo(conversationStatus(conversation))[1];
    top.append(title, dot);

    const summary = createElement(
      "span",
      "item-summary",
      String(conversation.summary_zh || "暂无公开进度摘要"),
    );
    const meta = createElement("span", "item-meta");
    meta.append(
      createElement("span", "item-model", modelName(conversation)),
      createElement("span", "item-turns", `${Number(conversation.turn_count || 1)} 轮 · ${formatDate(conversation.updated_utc, "short")}`),
    );
    button.append(top, summary, meta);
    button.addEventListener("click", () => selectConversation(rootId));
    fragment.append(button);
  }
  elements.list.replaceChildren(fragment);
  elements.list.setAttribute("aria-busy", "false");
  elements.count.textContent = String(state.total || state.conversations.length);
  elements.listEmpty.hidden = state.conversations.length > 0;
  elements.loadMore.hidden = state.nextOffset === null;
}

function mergeConversations(existing, incoming) {
  const merged = new Map(existing.map((item) => [String(item.root_job_id), item]));
  for (const item of incoming) merged.set(String(item.root_job_id), item);
  return [...merged.values()].sort((a, b) =>
    String(b.updated_utc || "").localeCompare(String(a.updated_utc || "")),
  );
}

async function loadConversations({ append = false, quiet = false } = {}) {
  if (!quiet) elements.refresh.classList.add("is-loading");
  try {
    const offset = append ? Number(state.nextOffset || state.conversations.length) : 0;
    const payload = await requestJson(`/api/conversations?limit=${PAGE_SIZE}&offset=${offset}`);
    const incoming = Array.isArray(payload.conversations) ? payload.conversations : [];
    state.conversations = append ? mergeConversations(state.conversations, incoming) : incoming;
    state.total = Number(payload.total || state.conversations.length);
    state.nextOffset = Number.isInteger(payload.next_offset) ? payload.next_offset : null;
    renderList();
    setConnection("live", "已同步");
    elements.lastUpdated.textContent = `${formatDate(payload.observed_utc || new Date())} 更新`;

    if (!state.selectedRootId && state.conversations.length) {
      await selectConversation(String(state.conversations[0].root_job_id), { preserveScroll: false });
    } else if (state.selectedRootId && !append) {
      await loadConversation(state.selectedRootId, { quiet: true, preserveScroll: true });
    }
  } catch (error) {
    setConnection("offline", "同步中断");
    if (!quiet) showToast(`暂时无法读取对话：${error.message}`);
  } finally {
    elements.refresh.classList.remove("is-loading");
  }
}

function eventSequence(event) {
  const value = Number(event?.sequence);
  return Number.isFinite(value) ? value : 0;
}

function mergeEvents(...groups) {
  const merged = new Map();
  for (const event of groups.flat()) {
    if (!event || typeof event !== "object") continue;
    const key = eventSequence(event) || `${event.kind}:${event.observed_utc}:${JSON.stringify(event.payload || {})}`;
    merged.set(String(key), event);
  }
  return [...merged.values()].sort((a, b) => eventSequence(a) - eventSequence(b));
}

function eventsForTurn(turn) {
  const jobId = String(turn.job_id || turn.id || "");
  const page = state.eventPages.get(jobId);
  if (page?.browsingEarlier) {
    const pinned = (Array.isArray(turn.events) ? turn.events : [])
      .filter((event) => isCompaction(event) || isOutcomeEvent(event))
      .slice(-MAX_PINNED_EVENTS_PER_TURN);
    return mergeEvents(page.events, pinned);
  }
  return mergeEvents(page?.events || [], Array.isArray(turn.events) ? turn.events : []);
}

function isCompaction(event) {
  const kind = String(event.kind || "").toLowerCase();
  return kind.includes("compact") || kind === "context.compacted" || kind === "context.compaction.completed";
}

function isOutputEvent(event) {
  const kind = String(event.kind || "").toLowerCase();
  return kind.includes("output.delta") || kind.includes("message.delta") || kind === "agent.output.updated";
}

function isOutcomeEvent(event) {
  const kind = String(event.kind || "").toLowerCase();
  return kind === "run.completed"
    || kind === "run.failed"
    || kind === "run.cancelled"
    || kind === "agent.run.completed"
    || kind === "agent.run.failed"
    || kind === "agent.turn.failed"
    || kind === "agent.limit.hit"
    || kind === "limit.hit"
    || kind === "handoff.collected";
}

function toolType(event) {
  const payload = event.payload || {};
  const raw = String(first(
    payload.item_type,
    payload.tool_type,
    payload.type,
    payload.tool,
    payload.operation_type,
    "",
  )).toLowerCase();
  if (raw.includes("command") || raw.includes("shell") || raw.includes("exec")) return "command";
  if (raw.includes("mcp")) return "mcp";
  if (raw.includes("computer")) return "computer";
  if (raw.includes("web_search") || raw.includes("search")) return "web_search";
  if (raw.includes("web") || raw.includes("browser")) return "web";
  if (raw.includes("file_change") || raw.includes("edit") || raw.includes("patch")) return "file_change";
  if (raw.includes("file")) return "file";
  return raw || (String(event.kind).includes("tool") ? "tool" : "");
}

function eventStatus(event) {
  const payload = event.payload || {};
  return statusName(first(payload.command_status, payload.status, payload.state, ""));
}

function eventOrdinal(event) {
  const payload = event.payload || {};
  const value = Number(first(
    payload.tool_calls,
    payload.tool_ordinal,
    payload.ordinal,
    payload.call_index,
  ));
  return Number.isFinite(value) && value > 0 ? value : null;
}

function workLabel(type, event) {
  const safeSummary = specificWorkSummary(event);
  if (safeSummary) return safeSummary;
  const ordinal = eventOrdinal(event);
  const suffix = ordinal ? ` ${ordinal}` : "";
  if (type === "command") return `命令${suffix}`;
  if (type === "web_search") return `网络搜索${suffix}`;
  if (type === "web") return `网页操作${suffix}`;
  if (type === "mcp") return `MCP 调用${suffix}`;
  if (type === "computer") return `电脑操作${suffix}`;
  if (type === "file_change") return `编辑文件${suffix}`;
  if (type === "file") return `文件操作${suffix}`;
  if (type === "tool") return `工具活动${suffix}`;
  return "工作进度";
}

function specificWorkSummary(event) {
  const safeSummary = String(first(event.summary_zh, event.payload?.summary_zh, "")).trim();
  const genericSummary = /^(智能体)?(正在|已)?(完成)?(执行|编辑|查询|调用|操作)?(命令|工具|网页|浏览器|网络搜索|公开资料|文件|mcp|电脑|计算机)(活动|调用|操作|执行|编辑|查询|成功|完成|失败)?(?:[（(][^）)]*[）)])?[。.]?$/i;
  return safeSummary && !genericSummary.test(safeSummary) ? safeSummary : "";
}

function workStatusLabel(value) {
  const labels = {
    started: "开始",
    running: "进行中",
    completed: "已完成",
    succeeded: "成功",
    success: "成功",
    in_progress: "进行中",
    failed: "失败",
    declined: "未执行",
    cancelled: "已取消",
  };
  return labels[value] || (value ? value : "已记录");
}

function workStatusDetail(item) {
  const parts = [workStatusLabel(item.status)];
  if (Number.isFinite(item.durationMs) && item.durationMs >= 0) {
    parts.push(formatDuration(item.durationMs / 1000));
  }
  if (Number.isInteger(item.exitCode)) parts.push(`退出码 ${item.exitCode}`);
  return parts.join(" · ");
}

function workTone(value) {
  if (["failed", "declined"].includes(value)) return "danger";
  if (["started", "running", "in_progress"].includes(value)) return "live";
  return "quiet";
}

function normalizedWork(events) {
  const rows = [];
  const indexed = new Map();
  const workspace = [];
  let workspaceCount = 0;
  const compactions = [];
  const outcomes = [];

  for (const event of events) {
    if (isOutputEvent(event)) continue;
    if (isCompaction(event)) {
      compactions.push(event);
      continue;
    }
    if (isOutcomeEvent(event)) {
      outcomes.push(event);
      continue;
    }
    if (event.kind === "workspace.change.observed") {
      const changes = Array.isArray(event.payload?.changes) ? event.payload.changes : [];
      workspace.push(...changes);
      const declaredCount = optionalNumber(event.payload?.changed_files);
      workspaceCount = Math.max(workspaceCount, declaredCount ?? changes.length);
      continue;
    }

    if (event.kind !== "agent.tool.activity") continue;
    const type = toolType(event);
    const ordinal = eventOrdinal(event);
    if (!type) continue;

    const key = type && ordinal ? `${type}:${ordinal}` : `event:${eventSequence(event) || rows.length}`;
    const row = {
      key,
      type,
      label: workLabel(type, event),
      specific: Boolean(specificWorkSummary(event)),
      status: eventStatus(event),
      durationMs: Number.isFinite(Number(event.payload?.duration_ms))
        ? Number(event.payload.duration_ms)
        : null,
      exitCode: Number.isInteger(Number(event.payload?.exit_code))
        ? Number(event.payload.exit_code)
        : null,
    };
    if (indexed.has(key)) {
      const existing = indexed.get(key);
      const specificLabel = existing.specific && !row.specific
        ? { label: existing.label, specific: true }
        : {};
      Object.assign(existing, row, specificLabel);
    } else {
      indexed.set(key, row);
      rows.push(row);
    }
  }
  return { rows, workspace, workspaceCount, compactions, outcomes };
}

function normalizedActivitySegment(segment) {
  const allowedTypes = new Set([
    "command", "file_change", "web_search", "web", "mcp", "computer", "file", "tool",
  ]);
  const activityCounts = [];
  for (const raw of Array.isArray(segment?.activities) ? segment.activities : []) {
    const type = String(raw?.type || "");
    const count = Number(raw?.count);
    if (!allowedTypes.has(type) || !Number.isInteger(count) || count <= 0) continue;
    const bounded = (name) => {
      const value = Number(raw?.[name]);
      return Number.isInteger(value) && value >= 0 ? Math.min(value, count) : 0;
    };
    activityCounts.push({
      type,
      count,
      completed: bounded("completed"),
      failed: bounded("failed"),
      active: bounded("active"),
      recorded: bounded("recorded"),
    });
  }
  const workspace = (Array.isArray(segment?.changes) ? segment.changes : [])
    .filter((change) => change && typeof change === "object")
    .slice(0, 8);
  const declaredCount = optionalNumber(segment?.workspace_changed_files);
  return {
    rows: [],
    workspace,
    workspaceCount: Math.max(0, declaredCount ?? workspace.length),
    activityCounts,
    compactions: [],
    outcomes: [],
  };
}

function visibleWorkRows(rows) {
  const important = rows.filter((item) =>
    item.specific || ["started", "running", "in_progress", "failed", "declined", "cancelled"].includes(item.status));
  const unique = new Map();
  for (const item of important) unique.set(`${item.type}:${item.label}:${item.status}`, item);
  return [...unique.values()].slice(-12);
}

function workSummary(work) {
  const counts = new Map();
  for (const item of work.activityCounts || []) {
    counts.set(item.type, (counts.get(item.type) || 0) + item.count);
  }
  for (const item of work.rows) counts.set(item.type, (counts.get(item.type) || 0) + 1);
  const labels = {
    command: "命令",
    web_search: "网络搜索",
    web: "网页操作",
    mcp: "MCP 调用",
    computer: "电脑操作",
    file_change: "编辑文件",
    file: "文件操作",
    tool: "工具活动",
  };
  const parts = [...counts.entries()].map(([type, count]) =>
    `${labels[type] || "工作活动"} ${count} 次`);
  if (work.workspaceCount) parts.push(`工作区变化 ${work.workspaceCount} 项`);

  const failed = (work.activityCounts || []).reduce((total, item) => total + item.failed, 0)
    + work.rows.filter((item) => ["failed", "declined", "cancelled"].includes(item.status)).length;
  const active = (work.activityCounts || []).reduce((total, item) => total + item.active, 0)
    + work.rows.filter((item) => ["started", "running", "in_progress"].includes(item.status)).length;
  const recorded = (work.activityCounts || []).reduce((total, item) => total + item.recorded, 0);
  const activityTotal = [...counts.values()].reduce((total, count) => total + count, 0);
  if (failed) parts.push(`${failed} 项未成功`);
  else if (active) parts.push(`${active} 项进行中`);
  else if (activityTotal && recorded === activityTotal) parts.push("已记录");
  else if (activityTotal) parts.push("已完成");
  return parts.join(" · ") || "已记录工作区变化";
}

function compactionText(event) {
  const payload = event.payload || {};
  const kind = String(event.kind || "").toLowerCase();
  const base = String(first(
    event.summary_zh,
    payload.summary_zh,
    kind.startsWith("agent.") ? "Codex 已自动压缩上下文" : "已压缩调用输入",
  )).replace(/[。.;；]+$/u, "");
  const facts = [];
  const count = Number(payload.compaction_count);
  if (Number.isInteger(count) && count > 0) facts.push(`第 ${count} 次`);
  const before = Number(payload.estimated_tokens_before);
  const after = Number(payload.estimated_tokens_after);
  if (Number.isFinite(before) && Number.isFinite(after)) {
    facts.push(`${formatNumber(before)} → ${formatNumber(after)} Token`);
  } else {
    const current = Number(payload.current_tokens);
    const windowTokens = Number(payload.context_window_tokens);
    if (Number.isFinite(current) && Number.isFinite(windowTokens) && windowTokens > 0) {
      facts.push(`${formatNumber(current)} / ${formatNumber(windowTokens)} Token`);
    }
  }
  return facts.length ? `${base} · ${facts.join(" · ")}` : base;
}

function appendWorkspaceNote(container, changes, totalCount) {
  if (!changes.length && !totalCount) return;
  const note = createElement("div", "workspace-note");
  note.append(createElement(
    "span",
    "",
    `观察到工作区变化 ${totalCount || changes.length} 项；变化归因未验证。`,
  ));
  const visibleChanges = changes
    .filter((change) => change && change.relative_path)
    .slice(0, 8);
  if (visibleChanges.length) {
    const changeList = createElement("div", "workspace-paths");
    const kindLabels = { added: "新增", modified: "修改", deleted: "删除", renamed: "重命名" };
    for (const change of visibleChanges) {
      const row = createElement("div", "workspace-change-row");
      row.append(
        createElement(
          "span",
          "workspace-change-kind",
          kindLabels[String(first(change.change_kind, change.kind, "")).toLowerCase()] || "变化",
        ),
        createElement("code", "", String(change.relative_path)),
      );
      const added = optionalNumber(first(change.lines_added, change.added_lines, change.added));
      const removed = optionalNumber(first(change.lines_deleted, change.removed_lines, change.removed));
      if (added !== null || removed !== null) {
        const counts = createElement("span", "workspace-change-counts");
        if (added !== null) counts.append(createElement("span", "is-added", `+${formatNumber(added)}`));
        if (removed !== null) counts.append(createElement("span", "is-removed", `-${formatNumber(removed)}`));
        row.append(counts);
      }
      changeList.append(row);
    }
    note.append(changeList);
  }
  container.append(note);
}

function workThoughtEventSource(event) {
  const kind = String(event?.kind || "").toLowerCase();
  if (kind === "agent.commentary.delta" || kind === "agent.commentary.completed") {
    return "commentary";
  }
  if (kind === "agent.reasoning.summary.delta") return "reasoning";
  return "";
}

function workThoughtEventKey(event) {
  const source = workThoughtEventSource(event);
  const payload = event?.payload || {};
  if (source === "commentary" && Number.isInteger(payload.commentary_group)) {
    return `commentary:${payload.commentary_group}`;
  }
  if (
    source === "reasoning"
    && Number.isInteger(payload.summary_group)
    && Number.isInteger(payload.summary_index)
  ) return `reasoning:${payload.summary_group}:${payload.summary_index}`;
  return "";
}

function workThoughtData(turn, events) {
  const progress = turn.progress || {};
  const fullThoughtNodes = turn?.conversation_process?.thought_nodes;
  if (Array.isArray(fullThoughtNodes)) {
    let truncated = false;
    const items = [];
    for (const raw of fullThoughtNodes) {
      const source = raw?.source;
      const key = String(raw?.key || "");
      const text = raw?.text;
      const sequence = Number(raw?.first_sequence);
      if (
        !["commentary", "reasoning"].includes(source)
        || !key
        || typeof text !== "string"
        || !text
        || !Number.isInteger(sequence)
        || sequence <= 0
      ) continue;
      items.push({
        source,
        key,
        text,
        sequence,
        stableOrder: items.length,
      });
      if (raw.truncated === true) truncated = true;
    }
    items.sort((a, b) => a.sequence - b.sequence || a.stableOrder - b.stableOrder);
    return { items, truncated };
  }
  const orderedEvents = mergeEvents(events, Array.isArray(turn.events) ? turn.events : []);
  const eventAnchors = new Map();
  for (const event of orderedEvents) {
    const key = workThoughtEventKey(event);
    if (key && !eventAnchors.has(key)) eventAnchors.set(key, eventSequence(event));
  }
  const hasCommentaryCanonical = Array.isArray(progress.public_commentary_segments)
    || progress.public_commentary_truncated === true;
  const hasReasoningCanonical = Array.isArray(progress.public_reasoning_summaries)
    || progress.public_reasoning_summaries_truncated === true;
  const canonical = [];
  let truncated = progress.public_commentary_truncated === true
    || progress.public_reasoning_summaries_truncated === true;
  const sourceChars = { commentary: 0, reasoning: 0 };
  const sourceCounts = { commentary: 0, reasoning: 0 };
  const seenCanonical = new Set();

  function appendCanonical(source, key, text, firstSequence) {
    if (seenCanonical.has(key) || typeof text !== "string" || !text) return;
    if (sourceCounts[source] >= 12) {
      truncated = true;
      return;
    }
    const available = Math.min(4_000, 20_000 - sourceChars[source]);
    if (available <= 0) {
      truncated = true;
      return;
    }
    const bounded = text.slice(0, available);
    const explicitSequence = Number(firstSequence);
    canonical.push({
      source,
      key,
      text: bounded,
      sequence: Number.isFinite(explicitSequence) && explicitSequence > 0
        ? explicitSequence
        : eventAnchors.has(key) ? eventAnchors.get(key) : Number.POSITIVE_INFINITY,
      stableOrder: canonical.length,
    });
    seenCanonical.add(key);
    sourceCounts[source] += 1;
    sourceChars[source] += bounded.length;
    if (bounded.length < text.length) truncated = true;
  }

  if (Array.isArray(progress.public_commentary_segments)) {
    for (const item of progress.public_commentary_segments) {
      const group = item?.commentary_group;
      if (!Number.isInteger(group) || group <= 0 || group > 1_000_000) continue;
      appendCanonical("commentary", `commentary:${group}`, item.text, item.first_sequence);
    }
  }
  if (Array.isArray(progress.public_reasoning_summaries)) {
    for (const item of progress.public_reasoning_summaries) {
      const group = item?.summary_group;
      const index = item?.summary_index;
      if (
        !Number.isInteger(group) || group <= 0 || group > 1_000_000
        || !Number.isInteger(index) || index < 0 || index > 1_000_000
      ) continue;
      appendCanonical("reasoning", `reasoning:${group}:${index}`, item.text, item.first_sequence);
    }
  }

  // When a canonical field or its truncation marker is present, it is
  // authoritative even when empty. This prevents a revoked accumulated segment
  // from being reconstructed from older paged deltas.
  const eventItems = [];
  const byKey = new Map();
  for (const event of orderedEvents) {
    const source = workThoughtEventSource(event);
    if (!source) continue;
    if (source === "commentary" && hasCommentaryCanonical) continue;
    if (source === "reasoning" && hasReasoningCanonical) continue;
    const payload = event.payload || {};
    if (payload.truncated === true) truncated = true;
    const group = source === "commentary" ? payload.commentary_group : payload.summary_group;
    const index = source === "reasoning" ? payload.summary_index : null;
    if (!Number.isInteger(group) || group <= 0 || group > 1_000_000) continue;
    if (source === "reasoning" && (!Number.isInteger(index) || index < 0 || index > 1_000_000)) continue;
    const key = source === "commentary"
      ? `commentary:${group}`
      : `reasoning:${group}:${index}`;
    const completed = String(event.kind || "").toLowerCase() === "agent.commentary.completed";
    const content = completed ? payload.content_replace : payload.delta;
    if (typeof content !== "string" || !content) continue;
    let item = byKey.get(key);
    if (!item) {
      if (sourceCounts[source] >= 12) {
        truncated = true;
        continue;
      }
      item = {
        source,
        key,
        text: "",
        frozen: false,
        sequence: eventSequence(event),
        stableOrder: eventItems.length,
      };
      byKey.set(key, item);
      eventItems.push(item);
      sourceCounts[source] += 1;
    }
    if (item.frozen) continue;
    const retainedChars = sourceChars[source] - item.text.length;
    const available = completed
      ? Math.min(4_000, 20_000 - retainedChars)
      : Math.min(4_000 - item.text.length, 20_000 - sourceChars[source]);
    if (available <= 0) {
      truncated = true;
      continue;
    }
    const piece = content.slice(0, available);
    if (completed) {
      sourceChars[source] = retainedChars + piece.length;
      item.text = piece;
      item.frozen = true;
    } else {
      item.text += piece;
      sourceChars[source] += piece.length;
    }
    if (piece.length < content.length) truncated = true;
  }
  const visibleEvents = eventItems
    .filter((item) => item.text)
    .map(({ frozen: _frozen, ...item }) => item);
  const anyCanonical = hasCommentaryCanonical || hasReasoningCanonical;
  const items = anyCanonical
    ? ["commentary", "reasoning"].flatMap((source) => [
      ...canonical.filter((item) => item.source === source),
      ...visibleEvents.filter((item) => item.source === source),
    ])
    : visibleEvents;
  items.sort((a, b) => a.sequence - b.sequence || a.stableOrder - b.stableOrder);
  return { items, truncated };
}

function renderWorkThought(item, terminal, truncated = false) {
  const details = createElement("details", "work-thought");
  details.open = true;
  details.dataset.live = String(!terminal);
  const heading = createElement("summary", "work-thought-heading");
  heading.append(
    createElement("strong", "", "工作思路"),
    createElement(
      "span",
      "work-thought-source",
      item.source === "commentary" ? "公开过程消息" : "公开推理摘要",
    ),
  );
  details.append(heading);
  const body = createElement("div", "work-thought-body");
  const text = createElement("div", "work-thought-text markdown-body");
  appendMarkdown(text, item.text);
  text.dataset.markdownSource = item.text;
  text.dataset.workThoughtKey = item.key;
  text.setAttribute("aria-live", terminal ? "off" : "polite");
  text.setAttribute("aria-atomic", "false");
  body.append(text);
  if (truncated) {
    body.append(createElement("div", "work-thought-limit", "工作思路显示已截断。"));
  }
  details.append(body);
  return details;
}

function chronologicalProcessData(turn, events) {
  const thoughtView = workThoughtData(turn, events);
  const canonicalProcess = turn?.conversation_process;
  if (
    canonicalProcess?.schema === "llm-backend-toolkit.observer-conversation-process.v1"
    && Array.isArray(canonicalProcess.activity_segments)
    && Array.isArray(canonicalProcess.events)
  ) {
    const ordered = [];
    thoughtView.items.forEach((item, stableOrder) => ordered.push({
      type: "thought",
      item,
      sequence: item.sequence,
      priority: 0,
      stableOrder,
    }));
    canonicalProcess.activity_segments.forEach((segment, stableOrder) => {
      const firstSequence = Number(segment?.first_sequence);
      const lastSequence = Number(segment?.last_sequence);
      if (!Number.isInteger(firstSequence) || firstSequence <= 0) return;
      const work = normalizedActivitySegment(segment);
      if (!(work.activityCounts.length || work.workspace.length || work.workspaceCount)) return;
      ordered.push({
        type: "activity",
        work,
        key: `full:${firstSequence}:${Number.isInteger(lastSequence) ? lastSequence : firstSequence}`,
        fullHistory: true,
        sequence: firstSequence,
        priority: 1,
        stableOrder,
      });
    });
    canonicalProcess.events.forEach((event, stableOrder) => {
      if (!isCompaction(event) && !isOutcomeEvent(event)) return;
      ordered.push({
        type: "event",
        event,
        sequence: eventSequence(event),
        priority: 2,
        stableOrder,
      });
    });
    ordered.sort((a, b) =>
      a.sequence - b.sequence || a.priority - b.priority || a.stableOrder - b.stableOrder);
    return {
      blocks: ordered.map(({ sequence: _sequence, priority: _priority, stableOrder: _stable, ...block }) => block),
      truncated: thoughtView.truncated,
      fullHistory: true,
      processTruncated: canonicalProcess.truncated === true,
    };
  }
  const thoughtByKey = new Map(thoughtView.items.map((item) => [item.key, item]));
  const seenThoughts = new Set();
  const blocks = [];
  let activityEvents = [];
  const flushActivity = () => {
    if (!activityEvents.length) return;
    const work = normalizedWork(activityEvents);
    if (work.rows.length || work.workspace.length) blocks.push({
      type: "activity",
      work,
      key: `${eventSequence(activityEvents[0])}:${eventSequence(activityEvents.at(-1))}`,
    });
    activityEvents = [];
  };
  let thoughtIndex = 0;
  const emitThoughtsThrough = (sequence) => {
    while (
      thoughtIndex < thoughtView.items.length
      && thoughtView.items[thoughtIndex].sequence <= sequence
    ) {
      const item = thoughtView.items[thoughtIndex];
      thoughtIndex += 1;
      if (seenThoughts.has(item.key)) continue;
      flushActivity();
      blocks.push({ type: "thought", item });
      seenThoughts.add(item.key);
    }
  };
  for (const event of mergeEvents(events, Array.isArray(turn.events) ? turn.events : [])) {
    emitThoughtsThrough(eventSequence(event) - 0.5);
    const thoughtKey = workThoughtEventKey(event);
    if (thoughtKey && thoughtByKey.has(thoughtKey)) {
      if (!seenThoughts.has(thoughtKey)) {
        flushActivity();
        blocks.push({ type: "thought", item: thoughtByKey.get(thoughtKey) });
        seenThoughts.add(thoughtKey);
      }
      continue;
    }
    if (event.kind === "agent.tool.activity" || event.kind === "workspace.change.observed") {
      activityEvents.push(event);
      continue;
    }
    if (isCompaction(event) || isOutcomeEvent(event)) {
      flushActivity();
      blocks.push({ type: "event", event });
    }
  }
  flushActivity();
  emitThoughtsThrough(Number.POSITIVE_INFINITY);
  return { blocks, truncated: thoughtView.truncated };
}

function renderActivityBlock(block, windowIsLimited = false) {
  const work = block.work;
  const log = createElement("details", "work-log");
  log.dataset.activityKey = block.key;
  log.open = work.rows.some((item) =>
    ["started", "running", "in_progress", "failed", "declined"].includes(item.status))
    || (work.activityCounts || []).some((item) => item.active > 0 || item.failed > 0);
  const heading = createElement("summary", "work-log-heading");
  heading.append(
    createElement("strong", "", log.open ? "正在工作" : "工作记录"),
    createElement("span", "", windowIsLimited ? `当前窗口：${workSummary(work)}` : workSummary(work)),
  );
  log.append(heading);
  const visibleRows = visibleWorkRows(work.rows);
  if (visibleRows.length) {
    const rows = createElement("div", "work-rows");
    for (const item of visibleRows) {
      const row = createElement("div", "work-row");
      row.dataset.tone = workTone(item.status);
      row.append(
        createElement("span", "work-row-main", item.label),
        createElement("span", "work-row-status", workStatusDetail(item)),
      );
      rows.append(row);
    }
    log.append(rows);
  } else if (work.rows.length || (work.activityCounts || []).length) {
    log.append(createElement(
      "p",
      "work-aggregate-note",
      "完成活动已聚合；上游未公开可安全展示的命令、路径或查询正文。",
    ));
  }
  appendWorkspaceNote(log, work.workspace, work.workspaceCount);
  return log;
}

function renderProcessEvent(event) {
  if (isCompaction(event)) return createElement("div", "compaction-divider", compactionText(event));
  const kind = String(event.kind || "").toLowerCase();
  const tone = kind.includes("failed") || kind.includes("limit") ? "danger" : "quiet";
  const fallback = kind.includes("completed")
    ? "本轮运行已完成"
    : kind === "handoff.collected" ? "Codex 已取回本轮结果" : "本轮运行未成功完成";
  const outcome = createElement(
    "div",
    "outcome-note",
    String(first(event.summary_zh, event.payload?.summary_zh, fallback)),
  );
  outcome.dataset.tone = tone;
  return outcome;
}

function safeLinkHref(raw) {
  try {
    const url = new URL(String(raw || ""));
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
  } catch (_error) {
    return "";
  }
}

function appendInlineMarkdown(container, text) {
  const source = String(text || "");
  const pattern = /(`[^`\n]+`|\[([^\]\n]+)\]\(([^)\s]+)\)|\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*|_([^_\n]+)_)/g;
  let cursor = 0;
  let match;
  while ((match = pattern.exec(source)) !== null) {
    if (match.index > cursor) container.append(document.createTextNode(source.slice(cursor, match.index)));
    const token = match[0];
    if (token.startsWith("`")) {
      container.append(createElement("code", "", token.slice(1, -1)));
    } else if (token.startsWith("[")) {
      const href = safeLinkHref(match[3]);
      if (!href) {
        container.append(document.createTextNode(token));
      } else {
        const link = createElement("a", "", match[2]);
        link.setAttribute("href", href);
        link.setAttribute("rel", "noreferrer noopener");
        link.setAttribute("target", "_blank");
        container.append(link);
      }
    } else if (token.startsWith("_") && (
      /[\p{L}\p{N}]/u.test(source[match.index - 1] || "")
      || /[\p{L}\p{N}]/u.test(source[pattern.lastIndex] || "")
    )) {
      // CommonMark does not treat underscores inside an identifier/word as
      // emphasis. Keep model names, markers, and snake_case values intact.
      container.append(document.createTextNode(token));
    } else if (token.startsWith("**") || token.startsWith("__")) {
      container.append(createElement("strong", "", first(match[4], match[5])));
    } else {
      container.append(createElement("em", "", first(match[6], match[7])));
    }
    cursor = pattern.lastIndex;
  }
  if (cursor < source.length) container.append(document.createTextNode(source.slice(cursor)));
}

function isTableDivider(line) {
  const cells = splitTableRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
}

function splitTableRow(line) {
  const trimmed = String(line || "").trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.includes("|") ? trimmed.split("|").map((cell) => cell.trim()) : [];
}

function appendMarkdown(container, markdown) {
  const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
  let index = 0;
  while (index < lines.length) {
    if (!lines[index].trim()) {
      index += 1;
      continue;
    }
    const fence = lines[index].match(/^\s*```([^`]*)$/);
    if (fence) {
      const codeLines = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const pre = createElement("pre", "markdown-code-block");
      const code = createElement("code", "", codeLines.join("\n"));
      const language = fence[1].trim().split(/\s+/)[0];
      if (language) code.dataset.language = language;
      pre.append(code);
      container.append(pre);
      continue;
    }
    const heading = lines[index].match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const node = createElement(`h${heading[1].length}`, "");
      appendInlineMarkdown(node, heading[2]);
      container.append(node);
      index += 1;
      continue;
    }
    if (index + 1 < lines.length && splitTableRow(lines[index]).length && isTableDivider(lines[index + 1])) {
      const table = createElement("table", "markdown-table");
      const head = createElement("thead", "");
      const headRow = createElement("tr", "");
      for (const cell of splitTableRow(lines[index])) {
        const th = createElement("th", "");
        appendInlineMarkdown(th, cell);
        headRow.append(th);
      }
      head.append(headRow);
      table.append(head);
      index += 2;
      const body = createElement("tbody", "");
      while (index < lines.length && splitTableRow(lines[index]).length) {
        const row = createElement("tr", "");
        for (const cell of splitTableRow(lines[index])) {
          const td = createElement("td", "");
          appendInlineMarkdown(td, cell);
          row.append(td);
        }
        body.append(row);
        index += 1;
      }
      table.append(body);
      container.append(table);
      continue;
    }
    if (/^\s*>\s?/.test(lines[index])) {
      const quoteLines = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quoteLines.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      const quote = createElement("blockquote", "");
      appendMarkdown(quote, quoteLines.join("\n"));
      container.append(quote);
      continue;
    }
    const listMatch = lines[index].match(/^\s*(?:([-+*])|(\d+)\.)\s+(.+)$/);
    if (listMatch) {
      const ordered = Boolean(listMatch[2]);
      const list = createElement(ordered ? "ol" : "ul", "");
      while (index < lines.length) {
        const itemMatch = lines[index].match(/^\s*(?:([-+*])|(\d+)\.)\s+(.+)$/);
        if (!itemMatch || Boolean(itemMatch[2]) !== ordered) break;
        const item = createElement("li", "");
        appendInlineMarkdown(item, itemMatch[3]);
        list.append(item);
        index += 1;
      }
      container.append(list);
      continue;
    }
    const paragraphLines = [];
    while (index < lines.length && lines[index].trim()) {
      if (paragraphLines.length && (
        /^\s*```/.test(lines[index])
        || /^(#{1,6})\s+/.test(lines[index])
        || /^\s*>\s?/.test(lines[index])
        || /^\s*(?:[-+*]|\d+\.)\s+/.test(lines[index])
        || (index + 1 < lines.length && splitTableRow(lines[index]).length && isTableDivider(lines[index + 1]))
      )) break;
      paragraphLines.push(lines[index]);
      index += 1;
    }
    const paragraph = createElement("p", "");
    appendInlineMarkdown(paragraph, paragraphLines.join("\n"));
    container.append(paragraph);
  }
}

function updateMarkdownElement(node, nextSource) {
  const next = String(nextSource || "");
  const prior = String(node.dataset.markdownSource || "");
  if (next === prior) return;
  const suffix = next.startsWith(prior) ? next.slice(prior.length) : "";
  const plainAppend = suffix
    && !/[`*_\[\]#>|\n]/.test(prior + suffix)
    && node.lastElementChild?.tagName === "P";
  if (plainAppend) {
    node.lastElementChild.append(document.createTextNode(suffix));
  } else {
    node.replaceChildren();
    appendMarkdown(node, next);
  }
  node.dataset.markdownSource = next;
}

function safeOutputText(raw) {
  if (typeof raw === "string") return { text: raw, truncated: false };
  if (raw === undefined || raw === null) return { text: "", truncated: false };
  if (raw && typeof raw === "object" && raw.type === "preview" && typeof raw.preview === "string") {
    return { text: raw.preview, truncated: raw.truncated === true };
  }
  try {
    const text = JSON.stringify(raw, null, 2) || "";
    return { text: text.slice(0, 20_000), truncated: text.length > 20_000 };
  } catch (_error) {
    return { text: "", truncated: false };
  }
}

function outputData(turn) {
  const finalOutput = safeOutputText(turn.result?.output);
  const draft = String(turn.progress?.public_preview || "");
  const status = statusName(turn.job_status);
  const resultStatus = statusName(first(turn.result_status, turn.result?.status));
  const completed = status === "completed"
    && !["failed", "error", "blocked", "cancelled", "stale"].includes(resultStatus);
  const terminal = ["completed", "failed", "cancelled", "stale", "blocked"].includes(status);
  if (!terminal) return draft
    ? {
        text: draft,
        final: false,
        live: true,
        state: "draft",
        truncated: turn.progress?.public_preview_truncated === true,
      }
    : { text: "等待公开草稿…", final: false, live: true, state: "draft", truncated: false, empty: true };
  if (finalOutput.text) return {
    text: finalOutput.text,
    final: completed,
    live: false,
    state: completed ? "final" : "partial",
    truncated: finalOutput.truncated,
  };
  if (draft) return {
    text: draft,
    final: completed,
    live: false,
    state: completed ? "final" : "partial",
    truncated: turn.progress?.public_preview_truncated === true,
  };
  return {
    text: "本轮未提供公开答复。",
    final: completed,
    live: false,
    state: completed ? "final" : "partial",
    truncated: false,
    empty: true,
  };
}

function receiptData(turn) {
  const result = turn.result || {};
  const execution = turn.result?.execution_receipt;
  const checks = Array.isArray(turn.result?.checks) ? turn.result.checks : [];
  const full = {};
  for (const key of [
    "context_receipt",
    "delegation_receipt",
    "source_receipt",
    "execution_receipt",
    "delivery_receipt",
    "cache_identity",
    "media_routes",
  ]) {
    if (result[key] !== undefined) full[key] = result[key];
  }
  if (checks.length) full.checks = checks;
  const receipt = {
    execution: execution && typeof execution === "object" ? execution : {},
    checks,
    full,
  };
  const fields = [];
  const labels = {
    runner: "执行器",
    mode: "执行模式",
    model: "计划或回执模型",
    reasoning_effort: "推理强度",
    stop_reason: "停止原因",
    exit_code: "退出码",
    duration_ms: "执行耗时",
    steps: "步骤",
    tool_calls: "工具调用",
    machine_event_count: "机器事件",
    fallback_used: "使用回退",
    route_live_verified: "路由实时验证",
    route_evidence_state: "路由证据",
  };
  for (const key of Object.keys(labels)) {
    const value = receipt.execution[key];
    if (value === undefined || value === null || value === "") continue;
    let display = value;
    if (key === "duration_ms") display = formatDuration(Number(value) / 1000);
    else if (typeof value === "boolean") display = value ? "是" : "否";
    fields.push({ label: labels[key], value: String(display) });
  }
  const webSearch = receipt.execution.web_search;
  if (webSearch && typeof webSearch === "object") {
    if (webSearch.enabled === false) {
      fields.push({ label: "网络搜索", value: "已关闭" });
    } else if (
      webSearch.enabled === true
      && webSearch.provider === "bing-rss-v1"
      && Number.isInteger(webSearch.searches)
      && webSearch.searches >= 0
      && webSearch.event_evidence === "runtime-lifecycle"
    ) {
      fields.push({
        label: "网络搜索",
        value: `${webSearch.provider} · ${webSearch.searches} 次 · 运行期事件`,
      });
    }
  }
  const cleanup = receipt.execution.limit_usage?.cleanup_confirmed;
  if (typeof cleanup === "boolean") fields.push({ label: "清理确认", value: cleanup ? "是" : "否" });
  const failed = receipt.checks.some((check) => check?.passed === false)
    || Number(receipt.execution.exit_code) !== 0 && receipt.execution.exit_code !== undefined;
  return { receipt, fields, failed };
}

function renderReceipt(turn) {
  const view = receiptData(turn);
  const hasFullReceipt = Object.keys(view.receipt.full).length > 0;
  if (!view.fields.length && !view.receipt.checks.length && !hasFullReceipt) return null;
  const card = createElement("details", "receipt-card");
  card.open = view.failed;
  card.dataset.tone = view.failed ? "danger" : "quiet";
  const passed = view.receipt.checks.filter((check) => check?.passed === true).length;
  const summaryText = view.receipt.checks.length
    ? `${passed}/${view.receipt.checks.length} 项检查通过`
    : view.fields.length ? "可验证执行信息" : "可验证回执";
  const summary = createElement("summary", "receipt-heading");
  summary.append(
    createElement("strong", "", "运行与验收回执"),
    createElement("span", "", view.failed ? `${summaryText} · 有失败` : summaryText),
  );
  card.append(summary);

  if (view.fields.length) {
    const facts = createElement("dl", "receipt-facts");
    for (const field of view.fields) {
      const row = createElement("div", "");
      row.append(createElement("dt", "", field.label), createElement("dd", "", field.value));
      facts.append(row);
    }
    card.append(facts);
  }
  if (view.receipt.checks.length) {
    const list = createElement("ul", "receipt-checks");
    for (const check of view.receipt.checks) {
      const label = String(first(check?.summary, check?.id, "未命名检查"));
      const item = createElement("li", "", label);
      item.dataset.passed = String(check?.passed === true);
      list.append(item);
    }
    card.append(list);
  }
  if (hasFullReceipt) {
    const technical = createElement("details", "receipt-technical");
    technical.append(createElement("summary", "", "完整安全回执"));
    const fullText = safeOutputText(view.receipt.full);
    const body = createElement("pre", "", fullText.text);
    body.dataset.truncated = String(fullText.truncated);
    technical.append(body);
    card.append(technical);
  }
  return card;
}

function renderAssistant(output) {
  const assistant = createElement("section", "assistant-message");
  const outputState = output.state || (output.final ? "final" : "draft");
  const isPartial = outputState === "partial";
  assistant.dataset.outputState = outputState;
  assistant.append(createElement("span", "assistant-avatar", "›_"));
  const assistantHeader = createElement("div", "assistant-message-header");
  const outputStatus = createElement(
    "span",
    "assistant-state",
    output.final ? "最终答复" : isPartial ? "失败前草稿" : "实时草稿",
  );
  outputStatus.dataset.live = String(output.live === true);
  assistantHeader.append(
    createElement("strong", "", output.final || isPartial ? "回复" : "正在生成"),
    outputStatus,
  );
  const outputBody = createElement("div", `assistant-output markdown-body${output.empty ? " is-empty" : ""}`);
  appendMarkdown(outputBody, output.text);
  outputBody.dataset.markdownSource = output.text;
  outputBody.dataset.truncated = String(output.truncated);
  outputBody.setAttribute("aria-live", output.live === true ? "polite" : "off");
  outputBody.setAttribute("aria-atomic", "false");
  assistant.append(assistantHeader, outputBody);
  return assistant;
}

function renderTurn(turn, index) {
  const article = createElement("article", "turn");
  const jobId = String(turn.job_id || turn.id || "");
  article.dataset.jobId = jobId;

  const heading = createElement("div", "turn-heading");
  heading.append(
    createElement("span", "turn-index", `第 ${Number(pick(turn, "conversation.turn") || index + 1)} 轮`),
    createElement("time", "turn-time", formatDate(turn.created_utc, "short")),
  );
  article.append(heading);

  const events = eventsForTurn(turn);
  const output = outputData(turn);
  const assistant = renderAssistant(output);
  const terminal = ["completed", "failed", "cancelled", "stale", "blocked"]
    .includes(statusName(turn.job_status));
  const page = state.eventPages.get(jobId);
  const hasEarlier = page ? page.hasEarlier : turn.event_page?.has_earlier;
  const windowIsLimited = Boolean(hasEarlier || page?.browsingEarlier);
  const process = chronologicalProcessData(turn, events);
  const lastThoughtIndex = process.blocks.reduce(
    (last, block, blockIndex) => block.type === "thought" ? blockIndex : last,
    -1,
  );
  process.blocks.forEach((block, blockIndex) => {
    if (block.type === "thought") {
      article.append(renderWorkThought(
        block.item,
        terminal,
        process.truncated && blockIndex === lastThoughtIndex,
      ));
    } else if (block.type === "activity") {
      article.append(renderActivityBlock(block, block.fullHistory ? false : windowIsLimited));
    } else {
      article.append(renderProcessEvent(block.event));
    }
  });

  // The canonical process already contains the complete public semantic
  // history. Raw-event paging is only a fallback for older records.
  if (!process.fullHistory && (hasEarlier || page?.browsingEarlier)) {
    const navigation = createElement("div", "process-pagination");
    if (hasEarlier) {
      const earlierCount = optionalNumber(first(page?.earlierCount, turn.event_page?.earlier_count));
      const earlier = createElement(
        "button",
        "older-events",
        earlierCount === null
          ? "查看本轮更早的原始技术事件"
          : `查看本轮更早的原始技术事件（${formatNumber(earlierCount)} 条）`,
      );
      earlier.type = "button";
      earlier.addEventListener("click", () => loadEarlierEvents(turn, earlier));
      navigation.append(earlier);
    }
    if (page?.browsingEarlier) {
      const latest = createElement("button", "older-events", "返回最新原始技术事件");
      latest.type = "button";
      latest.addEventListener("click", () => {
        state.eventPages.delete(jobId);
        renderConversation(state.selectedDetail, { preserveScroll: true });
      });
      navigation.append(latest);
    }
    article.append(navigation);
  }

  article.append(assistant);

  const receipt = renderReceipt(turn);
  if (receipt) article.append(receipt);

  const errorCategory = turn.result?.error?.category;
  if (errorCategory) article.append(createElement("div", "turn-error", `错误类别：${errorCategory}`));
  return article;
}

function latestTurn(detail) {
  const turns = Array.isArray(detail?.turns) ? detail.turns : [];
  return turns.at(-1) || {};
}

function renderFacts(detail) {
  const conversation = detail.conversation || {};
  const latest = latestTurn(detail);
  const latestSummary = state.conversations.find(
    (item) => String(item.root_job_id) === state.selectedRootId,
  ) || {};
  const status = first(latest.job_status, latestSummary.job_status, latest.result?.status);
  const statusDisplay = statusInfo(status)[0];
  const performance = latest.performance || latestSummary.performance || {};
  const usage = latest.result?.usage || {};
  const prompt = first(usage.prompt_tokens, usage.input_tokens);
  const completion = first(usage.completion_tokens, usage.output_tokens);
  const reasoning = first(usage.reasoning_tokens, usage.reasoning_output_tokens);
  const cached = first(usage.cached_tokens, usage.cached_input_tokens);
  const total = usage.total_tokens;
  const tokenParts = [];
  if (total !== undefined) tokenParts.push(`总计 ${formatNumber(total)}`);
  if (prompt !== undefined) tokenParts.push(`输入 ${formatNumber(prompt)}`);
  if (completion !== undefined) tokenParts.push(`输出 ${formatNumber(completion)}`);
  if (reasoning !== undefined && Number(reasoning) > 0) tokenParts.push(`推理 ${formatNumber(reasoning)}`);
  if (cached !== undefined && Number(cached) > 0) tokenParts.push(`缓存 ${formatNumber(cached)}（已含于输入）`);
  const tokenText = tokenParts.length ? tokenParts.join(" · ") : "不可用";
  const tpsValue = optionalNumber(performance.tokens_per_second);
  const tpsSourceLabels = {
    eval_duration: "模型评估时段",
    wall_clock_estimate: "整段墙钟估算",
    public_content_estimate: "公开内容估算",
  };
  const tpsText = tpsValue === null
    ? "不可用"
    : `${tpsValue.toFixed(1)} Token/s${tpsSourceLabels[performance.tokens_per_second_source] ? `（${tpsSourceLabels[performance.tokens_per_second_source]}）` : ""}`;
  const handoff = latest.handoff || latestSummary.handoff || {};
  const context = latest.context || {};
  const executionMode = statusName(pick(latest, "display.execution_mode"));
  const directExecution = executionMode === "direct";
  const currentContext = optionalNumber(context.current_tokens);
  const contextWindow = optionalNumber(context.context_window_tokens);
  let contextText;
  if (currentContext !== null && contextWindow !== null && contextWindow > 0) {
    contextText = `${formatNumber(currentContext)} / ${formatNumber(contextWindow)}（${Math.round(currentContext / contextWindow * 100)}%）`;
  } else if (currentContext !== null) {
    contextText = formatNumber(currentContext);
  } else if (directExecution) {
    contextText = "直接调用，无 Codex 上下文";
  } else if (isActive(status)) {
    contextText = "等待 Codex 运行时实测";
  } else {
    contextText = "Codex 运行时未上报上下文";
  }
  const handoffLabels = {
    collected: "已取回",
    not_collected: "未取回",
    pending: "等待取回",
    unavailable: "不可用",
  };
  const duration = Number(performance.elapsed_seconds);
  const active = isActive(status);

  elements.factsEmpty.hidden = true;
  elements.factsContent.hidden = false;
  elements.factStatus.textContent = statusDisplay;
  elements.factTurns.textContent = `${Number(conversation.turn_count || detail.turns?.length || 0)} 轮`;
  elements.factModel.textContent = modelName(latest.model ? latest : latestSummary);
  elements.factExecution.textContent = executionName(latest);
  elements.factReasoning.textContent = reasoningName(latest);
  elements.factDuration.textContent = formatDuration(duration);
  elements.factTokens.textContent = tokenText;
  elements.factTps.textContent = tpsText;
  elements.factContext.textContent = contextText;
  const contextPercent = currentContext !== null && contextWindow !== null && contextWindow > 0
    ? Math.max(0, Math.min(100, currentContext / contextWindow * 100))
    : null;
  elements.factContextBar.parentElement.hidden = contextPercent === null;
  elements.factContextBar.style.width = contextPercent === null ? "0%" : `${contextPercent}%`;
  const handoffStatus = statusName(first(handoff.status, "unavailable"));
  elements.factHandoff.textContent = handoffLabels[handoffStatus] || String(first(handoff.status, "不可用"));
  elements.factUpdated.textContent = formatDate(first(latest.updated_utc, latestSummary.updated_utc), "short");
  elements.factRootId.textContent = String(detail.root_job_id || conversation.root_job_id || "—");
  elements.factRootId.title = elements.factRootId.textContent;
  state.durationObservation = Number.isFinite(duration) && duration >= 0
    ? { base: duration, observedAt: Date.now(), active }
    : null;
}

function tickDuration() {
  const observation = state.durationObservation;
  if (!observation?.active) return;
  const elapsed = observation.base + Math.max(0, Date.now() - observation.observedAt) / 1000;
  elements.factDuration.textContent = formatDuration(elapsed);
}

function renderConversation(detail, { preserveScroll = true } = {}) {
  const previousTop = elements.scroll.scrollTop;
  const wasNearEnd = elements.scroll.scrollHeight - elements.scroll.clientHeight - previousTop < 56;
  const turns = Array.isArray(detail.turns) ? detail.turns : [];
  const conversation = detail.conversation || {};
  const listItem = state.conversations.find(
    (item) => String(item.root_job_id) === state.selectedRootId,
  ) || {};
  const latest = turns.at(-1) || listItem;
  const [statusText, tone] = statusInfo(first(latest.job_status, listItem.job_status, latest.result?.status));

  elements.empty.hidden = true;
  elements.feed.hidden = false;
  elements.status.textContent = statusText;
  elements.status.dataset.tone = tone;
  elements.turnCount.textContent = `${Number(conversation.turn_count || turns.length)} 轮对话`;
  elements.title.textContent = taskLabel(first(latest, listItem));
  elements.subtitle.textContent = `${modelName(latest)} · ${executionName(latest)} · 最近更新 ${formatDate(first(latest.updated_utc, listItem.updated_utc), "short")}`;

  const priorOutputs = new Map();
  const priorWorkOpen = new Map();
  const priorThoughtOpen = new Map();
  const priorThoughtTexts = new Map();
  for (const article of elements.turns.querySelectorAll(".turn[data-job-id]")) {
    const jobId = String(article.dataset.jobId || "");
    const output = article.querySelector(".assistant-output");
    if (jobId && output) priorOutputs.set(jobId, output);
    for (const workLog of article.querySelectorAll(".work-log[data-activity-key]")) {
      if (jobId) priorWorkOpen.set(`${jobId}:${workLog.dataset.activityKey}`, workLog.open);
    }
    for (const thought of article.querySelectorAll(".work-thought")) {
      const thoughtText = thought.querySelector(".work-thought-text[data-work-thought-key]");
      if (!jobId || !thoughtText) continue;
      const mapKey = `${jobId}:${thoughtText.dataset.workThoughtKey}`;
      priorThoughtOpen.set(mapKey, thought.open);
      priorThoughtTexts.set(mapKey, thoughtText);
    }
  }

  const fragment = document.createDocumentFragment();
  turns.forEach((turn, index) => {
    const article = renderTurn(turn, index);
    const jobId = String(article.dataset.jobId || "");
    const nextOutput = article.querySelector(".assistant-output");
    const priorOutput = priorOutputs.get(jobId);
    if (nextOutput && priorOutput) {
      const nextText = nextOutput.dataset.markdownSource || "";
      priorOutput.className = nextOutput.className;
      priorOutput.dataset.truncated = nextOutput.dataset.truncated;
      updateMarkdownElement(priorOutput, nextText);
      nextOutput.replaceWith(priorOutput);
    }
    for (const workLog of article.querySelectorAll(".work-log[data-activity-key]")) {
      const key = `${jobId}:${workLog.dataset.activityKey}`;
      if (priorWorkOpen.has(key)) workLog.open = priorWorkOpen.get(key);
    }
    for (const nextThought of article.querySelectorAll(".work-thought")) {
      const nextTextNode = nextThought.querySelector(".work-thought-text[data-work-thought-key]");
      if (!nextTextNode) continue;
      const key = `${jobId}:${nextTextNode.dataset.workThoughtKey}`;
      const priorTextNode = priorThoughtTexts.get(key);
      if (!priorTextNode) continue;
      nextThought.open = priorThoughtOpen.get(key);
      priorTextNode.className = nextTextNode.className;
      priorTextNode.setAttribute("aria-live", nextTextNode.getAttribute("aria-live") || "off");
      priorTextNode.setAttribute("aria-atomic", "false");
      updateMarkdownElement(priorTextNode, nextTextNode.dataset.markdownSource || "");
      nextTextNode.replaceWith(priorTextNode);
    }
    fragment.append(article);
  });
  elements.turns.replaceChildren(fragment);
  renderFacts(detail);

  window.requestAnimationFrame(() => {
    if (!preserveScroll || wasNearEnd) elements.scroll.scrollTop = elements.scroll.scrollHeight;
    else elements.scroll.scrollTop = previousTop;
  });
}

async function loadConversation(rootId, { quiet = false, preserveScroll = true } = {}) {
  try {
    const detail = await requestJson(`/api/conversations/${encodeURIComponent(rootId)}`);
    if (state.selectedRootId !== rootId) return;
    state.selectedDetail = detail;
    renderConversation(detail, { preserveScroll });
    setConnection("live", "已同步");
  } catch (error) {
    setConnection("offline", "同步中断");
    if (!quiet) showToast(`暂时无法读取对话：${error.message}`);
  }
}

async function selectConversation(rootId, { preserveScroll = false } = {}) {
  state.selectedRootId = rootId;
  state.selectedDetail = null;
  state.eventPages.clear();
  renderList();
  await loadConversation(rootId, { preserveScroll });
}

async function loadEarlierEvents(turn, button) {
  const jobId = String(turn.job_id || turn.id || "");
  if (!jobId) return;
  const current = state.eventPages.get(jobId);
  const page = current || {
    events: Array.isArray(turn.events) ? turn.events : [],
    hasEarlier: Boolean(turn.event_page?.has_earlier),
    nextBefore: turn.event_page?.next_before_sequence,
    earlierCount: optionalNumber(turn.event_page?.earlier_count),
  };
  if (!page.hasEarlier) return;
  button.disabled = true;
  button.textContent = "正在加载…";
  try {
    const before = Number(page.nextBefore);
    const query = Number.isFinite(before) ? `&before_sequence=${before}` : "";
    const payload = await requestJson(
      `/api/runs/${encodeURIComponent(jobId)}/events?limit=${EVENT_PAGE_SIZE}${query}`,
    );
    const metadata = payload.event_page || {};
    state.eventPages.set(jobId, {
      events: mergeEvents(payload.events || [], page.events)
        .slice(0, MAX_EVENTS_PER_TURN - MAX_PINNED_EVENTS_PER_TURN),
      hasEarlier: Boolean(metadata.has_earlier),
      nextBefore: metadata.next_before_sequence,
      earlierCount: optionalNumber(metadata.earlier_count),
      browsingEarlier: true,
    });
    renderConversation(state.selectedDetail, { preserveScroll: true });
  } catch (error) {
    button.disabled = false;
    button.textContent = "查看本轮更早的原始技术事件";
    showToast(`无法加载更早记录：${error.message}`);
  }
}

function scheduleRefresh() {
  window.clearTimeout(state.refreshTimer);
  state.refreshTimer = window.setTimeout(() => loadConversations({ quiet: true }), 180);
}

function connectStream() {
  if (!("EventSource" in window)) return;
  state.eventSource?.close();
  const source = new EventSource("/api/stream");
  state.eventSource = source;
  source.addEventListener("open", () => setConnection("live", "实时同步"));
  source.addEventListener("update", scheduleRefresh);
  source.onmessage = scheduleRefresh;
  source.onerror = () => setConnection("offline", "轮询同步");
}

function startPolling() {
  window.clearInterval(state.pollTimer);
  state.pollTimer = window.setInterval(() => loadConversations({ quiet: true }), POLL_INTERVAL_MS);
  window.clearInterval(state.durationTimer);
  state.durationTimer = window.setInterval(tickDuration, 1000);
}

elements.refresh.addEventListener("click", () => loadConversations());
elements.loadMore.addEventListener("click", () => loadConversations({ append: true }));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") scheduleRefresh();
});
window.addEventListener("beforeunload", () => {
  state.eventSource?.close();
  window.clearInterval(state.pollTimer);
  window.clearInterval(state.durationTimer);
});

loadConversations();
connectStream();
startPolling();
