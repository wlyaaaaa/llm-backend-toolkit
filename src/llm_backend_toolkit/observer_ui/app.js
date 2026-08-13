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
  return kind === "run.failed"
    || kind === "run.cancelled"
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
  if (raw.includes("web") || raw.includes("browser")) return "web";
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
  const safeSummary = String(first(event.summary_zh, event.payload?.summary_zh, "")).trim();
  const genericSummary = /^(智能体)?(正在|已)?(执行)?(命令|工具|网页|浏览器|mcp|电脑|计算机)(活动|调用|操作|执行)?[。.]?$/i;
  if (safeSummary && !genericSummary.test(safeSummary)) return safeSummary;
  const ordinal = eventOrdinal(event);
  const suffix = ordinal ? ` ${ordinal}` : "";
  if (type === "command") return `命令${suffix}`;
  if (type === "web") return `网页操作${suffix}`;
  if (type === "mcp") return `MCP 调用${suffix}`;
  if (type === "computer") return `电脑操作${suffix}`;
  if (type === "tool") return `工具活动${suffix}`;
  return "工作进度";
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
      status: eventStatus(event),
      durationMs: Number.isFinite(Number(event.payload?.duration_ms))
        ? Number(event.payload.duration_ms)
        : null,
      exitCode: Number.isInteger(Number(event.payload?.exit_code))
        ? Number(event.payload.exit_code)
        : null,
    };
    if (indexed.has(key)) {
      Object.assign(indexed.get(key), row);
    } else {
      indexed.set(key, row);
      rows.push(row);
    }
  }
  return { rows, workspace, compactions, outcomes };
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

function appendWorkspaceNote(container, changes) {
  if (!changes.length) return;
  const note = createElement("div", "workspace-note");
  note.append(createElement(
    "span",
    "",
    `观察到工作区变化 ${changes.length} 项；变化归因未验证。`,
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
        createElement("span", "workspace-change-kind", kindLabels[String(change.kind || "").toLowerCase()] || "变化"),
        createElement("code", "", String(change.relative_path)),
      );
      const added = optionalNumber(first(change.added_lines, change.added));
      const removed = optionalNumber(first(change.removed_lines, change.removed));
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

function outputData(turn) {
  const raw = turn.result?.output;
  let finalText = "";
  let finalTruncated = false;
  if (typeof raw === "string") finalText = raw;
  else if (raw && typeof raw === "object") {
    finalText = String(first(raw.preview, raw.output, ""));
    finalTruncated = raw.truncated === true;
  }
  const draft = String(turn.progress?.public_preview || "");
  const terminal = ["completed", "failed", "cancelled", "stale", "blocked"].includes(statusName(turn.job_status));
  if (finalText) return { text: finalText, final: true, truncated: finalTruncated };
  if (draft) return {
    text: draft,
    final: terminal,
    truncated: turn.progress?.public_preview_truncated === true,
  };
  return {
    text: terminal ? "本轮未提供公开答复。" : "等待公开答复…",
    final: terminal,
    truncated: false,
    empty: true,
  };
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

  const publicTask = String(first(pick(turn, "display.task_label"), "")).trim();
  if (publicTask) {
    const task = createElement("section", "task-message");
    task.append(
      createElement("span", "message-label", "你"),
      createElement("p", "", publicTask),
    );
    article.append(task);
  } else {
    article.append(createElement("p", "task-withheld", "本轮任务正文未公开"));
  }

  const events = eventsForTurn(turn);
  const work = normalizedWork(events);
  if (work.rows.length || work.workspace.length || turn.event_page?.has_earlier || state.eventPages.get(jobId)?.hasEarlier) {
    const log = createElement("details", "work-log");
    log.open = work.rows.some((item) =>
      ["started", "running", "in_progress"].includes(item.status));
    const logHeading = createElement("summary", "work-log-heading");
    const summaryParts = [];
    if (work.rows.length) summaryParts.push(`${work.rows.length} 项活动`);
    if (work.workspace.length) summaryParts.push(`${work.workspace.length} 个文件`);
    logHeading.append(
      createElement("strong", "", log.open ? "正在工作" : "工作记录"),
      createElement("span", "", summaryParts.join(" · ") || "观察到工作区变化"),
    );
    log.append(logHeading);
    if (work.rows.length) {
      const rows = createElement("div", "work-rows");
      for (const item of work.rows) {
        const row = createElement("div", "work-row");
        row.dataset.tone = workTone(item.status);
        row.append(
          createElement("span", "work-row-main", item.label),
          createElement("span", "work-row-status", workStatusDetail(item)),
        );
        rows.append(row);
      }
      log.append(rows);
    }
    appendWorkspaceNote(log, work.workspace);

    const page = state.eventPages.get(jobId);
    const hasEarlier = page ? page.hasEarlier : turn.event_page?.has_earlier;
    if (hasEarlier) {
      const earlier = createElement("button", "older-events", "加载本轮更早的工作记录");
      earlier.type = "button";
      earlier.addEventListener("click", () => loadEarlierEvents(turn, earlier));
      log.append(earlier);
    }
    if (page?.browsingEarlier) {
      const latest = createElement("button", "older-events", "返回本轮最新记录");
      latest.type = "button";
      latest.addEventListener("click", () => {
        state.eventPages.delete(jobId);
        renderConversation(state.selectedDetail, { preserveScroll: true });
      });
      log.append(latest);
    }
    article.append(log);
  }

  for (const event of work.compactions) {
    article.append(createElement(
      "div",
      "compaction-divider",
      compactionText(event),
    ));
  }

  for (const event of work.outcomes) {
    const kind = String(event.kind || "").toLowerCase();
    const tone = kind.includes("failed") || kind.includes("limit") ? "danger" : "quiet";
    const fallback = kind === "handoff.collected" ? "Codex 已取回本轮结果" : "本轮运行未成功完成";
    const outcome = createElement(
      "div",
      "outcome-note",
      String(first(event.summary_zh, event.payload?.summary_zh, fallback)),
    );
    outcome.dataset.tone = tone;
    article.append(outcome);
  }

  const output = outputData(turn);
  const assistant = createElement("section", "assistant-message");
  assistant.dataset.outputState = output.final ? "final" : "draft";
  assistant.append(createElement("span", "assistant-avatar", "›_"));
  const assistantHeader = createElement("div", "assistant-message-header");
  const outputStatus = createElement(
    "span",
    "assistant-state",
    output.final ? "最终答复" : "实时草稿",
  );
  outputStatus.dataset.live = String(!output.final);
  assistantHeader.append(createElement("strong", "", "回复"), outputStatus);
  const outputBody = createElement("div", `assistant-output${output.empty ? " is-empty" : ""}`, output.text);
  outputBody.dataset.truncated = String(output.truncated);
  assistant.append(assistantHeader, outputBody);
  article.append(assistant);

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
  const currentContext = optionalNumber(context.current_tokens);
  const contextWindow = optionalNumber(context.context_window_tokens);
  let contextText = "不可用";
  if (currentContext !== null && contextWindow !== null && contextWindow > 0) {
    contextText = `${formatNumber(currentContext)} / ${formatNumber(contextWindow)}（${Math.round(currentContext / contextWindow * 100)}%）`;
  } else if (currentContext !== null) {
    contextText = formatNumber(currentContext);
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
  elements.title.textContent = taskLabel(first(turns[0], listItem));
  elements.subtitle.textContent = `${modelName(latest)} · ${executionName(latest)} · 最近更新 ${formatDate(first(latest.updated_utc, listItem.updated_utc), "short")}`;

  const priorOutputs = new Map();
  const priorWorkOpen = new Map();
  for (const article of elements.turns.querySelectorAll(".turn[data-job-id]")) {
    const jobId = String(article.dataset.jobId || "");
    const output = article.querySelector(".assistant-output");
    if (jobId && output) priorOutputs.set(jobId, output);
    const workLog = article.querySelector(".work-log");
    if (jobId && workLog) priorWorkOpen.set(jobId, workLog.open);
  }

  const fragment = document.createDocumentFragment();
  turns.forEach((turn, index) => {
    const article = renderTurn(turn, index);
    const jobId = String(article.dataset.jobId || "");
    const nextOutput = article.querySelector(".assistant-output");
    const priorOutput = priorOutputs.get(jobId);
    if (nextOutput && priorOutput) {
      const nextText = nextOutput.textContent || "";
      const priorText = priorOutput.textContent || "";
      priorOutput.className = nextOutput.className;
      priorOutput.dataset.truncated = nextOutput.dataset.truncated;
      if (nextText.startsWith(priorText)) {
        priorOutput.append(document.createTextNode(nextText.slice(priorText.length)));
      } else if (nextText !== priorText) {
        priorOutput.textContent = nextText;
      }
      nextOutput.replaceWith(priorOutput);
    }
    const workLog = article.querySelector(".work-log");
    if (workLog && priorWorkOpen.has(jobId)) workLog.open = priorWorkOpen.get(jobId);
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
      browsingEarlier: true,
    });
    renderConversation(state.selectedDetail, { preserveScroll: true });
  } catch (error) {
    button.disabled = false;
    button.textContent = "加载本轮更早的工作记录";
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
