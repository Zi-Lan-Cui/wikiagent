const state = {
  sessions: [],
  activeSession: null,
  busy: false,
  view: "chat",
  wikiHistory: [],
  wikiHistoryIndex: -1,
  wikiReturnView: "chat",
  wikiRequestId: 0,
  issues: [],
  activeIssue: null,
  issueTasks: [],
  workbenchTab: "issues",
};

const $ = (id) => document.getElementById(id);
const sessionList = $("session-list");
const messages = $("messages");
const input = $("message-input");
const sendButton = $("send-button");
const status = $("connection-status");
const wikiFiles = $("wiki-files");
const queueCount = $("queue-count");
const issueCenter = $("issue-center");
const issueList = $("issue-list");
const issueDetail = $("issue-detail");
const issueTaskList = $("issue-task-list");
const issueTaskCount = $("issue-task-count");
const workbenchTasksPanel = $("workbench-tasks-panel");
const workbenchIssuesPanel = $("workbench-issues-panel");
const workbenchTaskTab = $("workbench-tab-tasks");
const workbenchIssueTab = $("workbench-tab-issues");
const workbenchTaskTabCount = $("workbench-task-tab-count");
const workbenchIssueTabCount = $("workbench-issue-tab-count");
const retryEligibleButton = $("retry-eligible");
const retryEligibleCount = $("retry-eligible-count");
const syncButton = $("sync-now");
const syncBadge = $("sync-badge");
const wikiViewer = $("wiki-viewer");
const wikiContent = $("wiki-content");
const wikiViewerPath = $("wiki-viewer-path");
const wikiMetadata = $("wiki-metadata");
const wikiPageTitle = $("wiki-page-title");
const chatHeader = document.querySelector(".chat-header");
const wikiBasic = $("wiki-basic");
const wikiSummary = $("wiki-summary");
const wikiHistoryBack = $("wiki-history-back");
const wikiHistoryForward = $("wiki-history-forward");

function setStatus(text) { status.textContent = text; }

function addMessage(role, text = "") {
  const node = document.createElement("div");
  node.className = `message ${role}`;
  node.textContent = text;
  messages.appendChild(node);
  messages.scrollTop = messages.scrollHeight;
  return node;
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

// Small dependency-free renderer for the Markdown produced by the agent.
// Raw HTML is escaped deliberately: model output must never become executable
// markup in the local web UI.
function resolveWikiPath(target, basePath = "") {
  const isRooted = target.startsWith("/") || target.startsWith("wiki/");
  const cleanTarget = target.replace(/^\/?wiki\//, "").replace(/^\//, "");
  if (!cleanTarget.startsWith(".") && isRooted) return cleanTarget;
  if (!cleanTarget.startsWith(".")) return cleanTarget;
  const baseParts = isRooted ? [] : basePath.split("/").slice(0, -1);
  const parts = [...baseParts, ...cleanTarget.split("/")];
  const normalized = [];
  for (const part of parts) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (!normalized.length) return cleanTarget;
      normalized.pop();
    } else {
      normalized.push(part);
    }
  }
  return normalized.join("/");
}

function renderMarkdown(source, basePath = "", pageTitle = "") {
  // Wiki pages carry YAML frontmatter for indexing; it is metadata, not body
  // content, so keep it out of the reader view.
  source = source.replace(/^---\s*\n[\s\S]*?\n---\s*(?:\n|$)/, "");
  if (pageTitle) {
    const titlePattern = new RegExp(`^\\s*#\\s+${pageTitle.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&")}\\s*(?:\\n|$)`);
    source = source.replace(titlePattern, "");
  }
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const html = [];
  let inCode = false;
  let codeLanguage = "";
  let codeLines = [];
  let paragraph = [];
  let listItems = [];
  let orderedItems = [];

  const inline = (text) => escapeHtml(text)
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\[([^\]]+)\]\((?!https?:\/\/)([^)\s]+)\)/g,
      (_, label, path) => `<a href="#" class="wiki-link" data-wiki-path="${resolveWikiPath(path, basePath)}">${label}</a>`)
    .replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g,
      (_, path, label) => `<a href="#" class="wiki-link" data-wiki-path="${resolveWikiPath(path, basePath)}">${label || path}</a>`)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/_([^_]+)_/g, "<em>$1</em>");

  const flushParagraph = () => {
    if (paragraph.length) {
      html.push(`<p>${inline(paragraph.join("\n")).replace(/\n/g, "<br>")}</p>`);
      paragraph = [];
    }
  };
  const flushList = () => {
    if (listItems.length) {
      html.push(`<ul>${listItems.map((item) => `<li>${inline(item)}</li>`).join("")}</ul>`);
      listItems = [];
    }
  };
  const flushOrderedList = () => {
    if (orderedItems.length) {
      html.push(`<ol>${orderedItems.map((item) => `<li>${inline(item)}</li>`).join("")}</ol>`);
      orderedItems = [];
    }
  };
  const flushCode = () => {
    html.push(`<pre><code${codeLanguage ? ` class="language-${escapeHtml(codeLanguage)}"` : ""}>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
    codeLines = [];
    codeLanguage = "";
  };

  for (const line of lines) {
    const fence = line.match(/^\s*```\s*([\w+-]*)\s*$/);
    if (fence) {
      if (inCode) flushCode();
      else {
        flushParagraph();
        flushList();
        flushOrderedList();
        codeLanguage = fence[1];
      }
      inCode = !inCode;
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      continue;
    }
    const heading = line.match(/^\s*(#{1,6})\s+(.+?)\s*#*\s*$/);
    const list = line.match(/^\s*[-*+]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    const quote = line.match(/^\s*>\s?(.*)$/);
    if (heading) {
      flushParagraph();
      flushList();
      flushOrderedList();
      const level = heading[1].length;
      html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
    } else if (list) {
      flushParagraph();
      flushOrderedList();
      listItems.push(list[1]);
    } else if (ordered) {
      flushParagraph();
      flushList();
      orderedItems.push(ordered[1]);
    } else if (quote) {
      flushParagraph();
      flushList();
      flushOrderedList();
      html.push(`<blockquote>${inline(quote[1])}</blockquote>`);
    } else if (!line.trim()) {
      flushParagraph();
      flushList();
      flushOrderedList();
    } else {
      flushList();
      flushOrderedList();
      paragraph.push(line);
    }
  }
  if (inCode) flushCode();
  flushParagraph();
  flushList();
  flushOrderedList();
  return html.join("");
}

function pageUrl(path) {
  return `/api/wiki/pages/${path.split("/").map(encodeURIComponent).join("/")}`;
}

function sourceUrl(path) {
  const relative = path.replace(/^sources\//, "");
  return `/api/wiki/sources/${relative.split("/").map(encodeURIComponent).join("/")}`;
}

function updateWikiNavigation() {
  // At the first document, Back restores the view that opened the viewer.
  wikiHistoryBack.disabled = state.wikiHistoryIndex < 0;
  wikiHistoryForward.disabled = state.wikiHistoryIndex >= state.wikiHistory.length - 1;
  const backLabel = state.wikiHistoryIndex <= 0
    ? state.wikiReturnView === "issues" ? "返回工作台" : "返回会话"
    : "上一页";
  wikiHistoryBack.title = `${backLabel} (Alt+←)`;
  wikiHistoryBack.setAttribute("aria-label", backLabel);
  wikiHistoryForward.title = "下一页 (Alt+→)";
  wikiHistoryForward.setAttribute("aria-label", "下一页");
}

function rememberWikiScroll() {
  const current = state.wikiHistory[state.wikiHistoryIndex];
  if (current) current.scrollTop = wikiViewer.scrollTop;
}

function showWikiViewer() {
  state.view = "wiki";
  chatHeader.hidden = true;
  messages.hidden = true;
  issueCenter.hidden = true;
  $("message-form").hidden = true;
  wikiViewer.hidden = false;
}

function closeWikiPage(destination = state.wikiReturnView || "chat") {
  rememberWikiScroll();
  if (destination === "issues") restoreIssueView();
  else restoreChatView();
  state.wikiReturnView = "chat";
  wikiMetadata.replaceChildren();
  wikiBasic.replaceChildren();
  wikiSummary.textContent = "";
  const session = state.sessions.find((item) => item.id === state.activeSession);
  $("session-title").textContent = session?.title || state.activeSession || "选择一个会话";
}

function restoreChatView() {
  state.view = "chat";
  wikiViewer.hidden = true;
  issueCenter.hidden = true;
  chatHeader.hidden = false;
  messages.hidden = false;
  $("message-form").hidden = false;
}

function showIssueCenter() {
  rememberWikiScroll();
  restoreIssueView({ refresh: true });
}

function selectWorkbenchTab(tab) {
  state.workbenchTab = tab === "tasks" ? "tasks" : "issues";
  const showTasks = state.workbenchTab === "tasks";
  workbenchTasksPanel.hidden = !showTasks;
  workbenchIssuesPanel.hidden = showTasks;
  workbenchTaskTab.classList.toggle("active", showTasks);
  workbenchIssueTab.classList.toggle("active", !showTasks);
  workbenchTaskTab.setAttribute("aria-selected", String(showTasks));
  workbenchIssueTab.setAttribute("aria-selected", String(!showTasks));
}

function restoreIssueView({ refresh = false } = {}) {
  state.view = "issues";
  chatHeader.hidden = true;
  messages.hidden = true;
  wikiViewer.hidden = true;
  $("message-form").hidden = true;
  issueCenter.hidden = false;
  selectWorkbenchTab(state.workbenchTab);
  if (refresh) {
    Promise.all([refreshIssues(), refreshIssueTasks()]).catch((error) => setStatus(error.message));
  }
}

function closeIssueCenter() {
  restoreChatView();
}

async function loadWikiEntry(entry) {
  showWikiViewer();
  updateWikiNavigation();
  const requestId = ++state.wikiRequestId;
  const { path, kind } = entry;
  wikiViewerPath.textContent = path;
  wikiPageTitle.textContent = "正在加载…";
  wikiMetadata.replaceChildren();
  wikiBasic.replaceChildren();
  wikiSummary.textContent = "";
  wikiContent.innerHTML = "<p class=wiki-loading>正在加载页面……</p>";
  setStatus("正在打开 Wiki 页面……");
  try {
    const url = kind === "issue-resource"
      ? `/api/issues/${encodeURIComponent(entry.issueId)}/resource`
      : kind === "source"
        ? sourceUrl(path)
        : pageUrl(path);
    const response = await fetch(url);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "无法读取 Wiki 页面");
    if (requestId !== state.wikiRequestId) return;
    wikiViewerPath.textContent = payload.path;
    const title = payload.metadata?.title || (payload.content || "").match(/^#\s+(.+)$/m)?.[1] || payload.path;
    wikiPageTitle.textContent = title;
    renderWikiMetadata(payload.metadata || {}, payload);
    wikiContent.innerHTML = renderMarkdown(payload.content || "", payload.path || path, title);
    wikiViewer.scrollTop = entry.scrollTop || 0;
    setStatus("就绪");
  } catch (error) {
    if (requestId !== state.wikiRequestId) return;
    wikiContent.textContent = error.message || "无法读取 Wiki 页面";
    setStatus("读取失败");
  }
}

function openWikiPage(path, kind = "page", issueId = null) {
  const continuingNavigation = state.view === "wiki" && state.wikiHistoryIndex >= 0;
  if (continuingNavigation) rememberWikiScroll();
  else state.wikiReturnView = state.view === "issues" ? "issues" : "chat";
  const entry = { path, kind, issueId, scrollTop: 0 };
  if (continuingNavigation) {
    state.wikiHistory = state.wikiHistory.slice(0, state.wikiHistoryIndex + 1);
    state.wikiHistory.push(entry);
    state.wikiHistoryIndex += 1;
  } else {
    state.wikiHistory = [entry];
    state.wikiHistoryIndex = 0;
  }
  return loadWikiEntry(entry);
}

function openIssueResource(issue) {
  const path = issue.resource?.path || issue.resource?.label || issue.title;
  return openWikiPage(path, "issue-resource", issue.id);
}

function navigateWikiHistory(offset) {
  const nextIndex = state.wikiHistoryIndex + offset;
  if (nextIndex < 0) {
    closeWikiPage(state.wikiReturnView);
    return;
  }
  if (nextIndex >= state.wikiHistory.length) return;
  rememberWikiScroll();
  state.wikiHistoryIndex = nextIndex;
  loadWikiEntry(state.wikiHistory[nextIndex]);
}

function renderWikiMetadata(metadata, page) {
  wikiMetadata.replaceChildren();
  wikiBasic.replaceChildren();
  wikiSummary.textContent = metadata.summary || "";
  const labels = { type: "类型", summary: "摘要", updated: "更新", updated_at: "更新时间", created: "创建", author: "作者", status: "状态", tags: "标签", sources: "来源", related: "引用", goal: "目标", gaps: "缺口", size: "大小" };
  const addField = (key, value, className = "", target = wikiMetadata) => {
    if (value === undefined || value === "" || value === null) return;
    const item = document.createElement("div");
    item.className = `wiki-meta-field wiki-meta-${key} ${className}`;
    const label = document.createElement("span");
    label.className = "wiki-meta-label";
    label.textContent = `${labels[key] || key}：`;
    item.appendChild(label);
    const values = Array.isArray(value) ? value : [value];
    if (["sources", "related"].includes(key)) {
      const links = document.createElement("span");
      links.className = "wiki-meta-values";
      for (const [index, entry] of values.entries()) {
        const raw = String(entry);
        const match = raw.match(/^\[\[([^|\]]+)(?:\|([^\]]+))?\]\]$/);
        const target = match ? match[1] : raw;
        const displayName = match?.[2] || target.split("/").pop().replace(/\.md$/i, "");
        if (key === "related" && index > 0) {
          const separator = document.createElement("span");
          separator.className = "wiki-meta-separator";
          separator.textContent = "·";
          separator.setAttribute("aria-hidden", "true");
          links.appendChild(separator);
        }
        const link = document.createElement("button");
        link.type = "button";
        link.className = "wiki-meta-chip wiki-meta-link";
        link.textContent = displayName;
        link.title = target;
        link.addEventListener("click", () => openWikiPage(target, key === "sources" ? "source" : "page"));
        links.appendChild(link);
      }
      item.appendChild(links);
    } else if (Array.isArray(value)) {
      const valuesNode = document.createElement("span");
      valuesNode.className = "wiki-meta-values";
      for (const entry of values) {
        const chip = document.createElement("span");
        chip.className = `wiki-meta-chip ${String(entry).startsWith("大小 ") ? "wiki-meta-size-chip" : ""}`;
        chip.textContent = String(entry);
        valuesNode.appendChild(chip);
      }
      item.appendChild(valuesNode);
    } else {
      const text = document.createElement("span");
      text.className = "wiki-meta-value";
      text.textContent = String(value);
      item.appendChild(text);
    }
    target.appendChild(item);
  };
  const primary = ["type", "status", "created", "updated", "updated_at", "author"];
  primary.forEach((key) => addField(key, metadata[key], "", wikiBasic));
  addField("size", formatBytes(page.size || 0), "", wikiBasic);
  addField("tags", metadata.tags, "", wikiBasic);

  const makeGroup = (title) => {
    const group = document.createElement("section");
    group.className = "wiki-meta-group";
    group.setAttribute("aria-label", title);
    wikiMetadata.appendChild(group);
    return group;
  };
  const details = makeGroup("页面信息");
  addField("goal", metadata.goal, "", details);
  addField("gaps", metadata.gaps, "", details);
  addField("sources", metadata.sources, "", details);
  addField("related", metadata.related, "", details);
  if (!details.children.length) details.remove();
  if (!wikiMetadata.children.length) {
    wikiMetadata.textContent = "暂无页面元信息";
  }
}

function renderSessions() {
  sessionList.replaceChildren();
  for (const session of state.sessions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `session-item ${session.id === state.activeSession ? "active" : ""}`;
    button.innerHTML = `<strong></strong><small>${session.message_count} 条消息</small>`;
    button.querySelector("strong").textContent = session.title || session.id;
    button.addEventListener("click", () => selectSession(session.id));
    sessionList.appendChild(button);
  }
}

async function refreshSessions() {
  const response = await fetch("/api/sessions");
  if (!response.ok) throw new Error("无法读取会话列表");
  state.sessions = await response.json();
  renderSessions();
}

async function refreshWikiFiles() {
  const response = await fetch("/api/wiki/files");
  if (!response.ok) throw new Error("无法读取 Wiki 文件列表");
  const files = await response.json();
  wikiFiles.replaceChildren();
  if (!files.length) {
    const empty = document.createElement("div");
    empty.className = "wiki-file";
    empty.textContent = "目录为空";
    wikiFiles.appendChild(empty);
    return;
  }
  for (const file of files) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "wiki-file";
    item.title = file.path;
    item.innerHTML = `<span></span><small>${formatBytes(file.size)}</small>`;
    item.querySelector("span").textContent = file.path;
    item.addEventListener("click", () => openWikiPage(file.path));
    wikiFiles.appendChild(item);
  }
}

const issueKindLabels = {
  ingestion_failure: "资料处理失败",
  run_failure: "运行失败",
  quality_issue: "质量问题",
  content_correction: "用户纠错",
};
const issueStatusLabels = {
  open: "待处理",
  blocked: "等待决策",
  resolved: "已解决",
  dismissed: "已忽略",
};
const issueAttentionLabels = {
  retryable: "可重试",
  source_unavailable: "来源不可用",
  decision_required: "等待决策",
};

function issueDisplayState(issue) {
  return {
    label: issueAttentionLabels[issue.attention] || issueStatusLabels[issue.status] || issue.status,
    className: issueAttentionLabels[issue.attention]
      ? `attention-${issue.attention}`
      : `status-${issue.status}`,
  };
}

function formatIssueTime(value, { compact = false } = {}) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: compact ? undefined : "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function issueMetaRow(label, value) {
  if (value === undefined || value === null || value === "") return null;
  const row = document.createElement("div");
  row.className = "issue-meta-row";
  const term = document.createElement("b");
  term.textContent = label;
  const content = document.createElement("span");
  content.textContent = String(value);
  row.append(term, content);
  return row;
}

function renderIssueDetail(issue) {
  issueDetail.replaceChildren();
  if (!issue) {
    const empty = document.createElement("div");
    empty.className = "issue-detail-empty";
    empty.textContent = "选择一个问题查看证据与可用操作。";
    issueDetail.appendChild(empty);
    return;
  }
  const header = document.createElement("header");
  header.className = "issue-detail-header";
  const heading = document.createElement("div");
  const kicker = document.createElement("span");
  kicker.className = `issue-kind severity-${issue.severity}`;
  kicker.textContent = issueKindLabels[issue.kind] || issue.kind;
  const title = document.createElement("h3");
  title.textContent = issue.title;
  const summary = document.createElement("p");
  summary.textContent = issue.summary;
  heading.append(kicker, title, summary);
  const badge = document.createElement("span");
  const displayState = issueDisplayState(issue);
  badge.className = `issue-status ${displayState.className}`;
  badge.textContent = displayState.label;
  header.append(heading, badge);
  issueDetail.appendChild(header);

  const meta = document.createElement("section");
  meta.className = "issue-detail-section issue-meta";
  const path = issue.resource?.path || issue.resource?.label;
  [
    issueMetaRow("资源", path),
    issueMetaRow("阶段", issue.diagnostics?.stage || issue.origin?.stage),
    issueMetaRow("模式", issue.origin?.mode),
    issueMetaRow("错误码", issue.diagnostics?.error_code),
    issueMetaRow("发生次数", issue.occurrences),
    issueMetaRow("重试次数", issue.retry?.attempts),
    issueMetaRow("首次发现", formatIssueTime(issue.created_at)),
    issueMetaRow("最近更新", formatIssueTime(issue.updated_at)),
  ].filter(Boolean).forEach((row) => meta.appendChild(row));
  if (meta.children.length) issueDetail.appendChild(meta);

  const pageFailures = Array.isArray(issue.diagnostics?.failures)
    ? issue.diagnostics.failures
    : [];
  if (pageFailures.length) {
    const failures = document.createElement("section");
    failures.className = "issue-detail-section";
    failures.innerHTML = "<h4>失败页面</h4>";
    const list = document.createElement("div");
    list.className = "issue-failure-list";
    for (const failure of pageFailures) {
      const row = document.createElement("article");
      row.className = "issue-failure-item";
      const path = document.createElement("strong");
      path.textContent = failure.path || "未命名页面";
      const reason = document.createElement("p");
      reason.textContent = failure.reason || "未记录具体原因";
      row.append(path, reason);
      list.appendChild(row);
    }
    failures.appendChild(list);
    issueDetail.appendChild(failures);
  }

  const detailText = issue.diagnostics?.detail || issue.retry?.last_error;
  if (detailText) {
    const diagnostics = document.createElement("section");
    diagnostics.className = "issue-detail-section";
    diagnostics.innerHTML = "<h4>诊断</h4>";
    const pre = document.createElement("pre");
    pre.textContent = detailText;
    diagnostics.appendChild(pre);
    issueDetail.appendChild(diagnostics);
  }
  if (issue.evidence?.length) {
    const evidence = document.createElement("section");
    evidence.className = "issue-detail-section";
    evidence.innerHTML = "<h4>证据</h4>";
    const list = document.createElement("ul");
    for (const item of issue.evidence) {
      const row = document.createElement("li");
      row.textContent = item.claim || item.path || item.key || JSON.stringify(item);
      list.appendChild(row);
    }
    evidence.appendChild(list);
    issueDetail.appendChild(evidence);
  }

  const actions = document.createElement("footer");
  actions.className = "issue-actions";
  for (const action of issue.available_actions || []) {
    if ((action.id === "open_resource" && !path) || action.id === "open_log") continue;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `issue-action ${action.style === "primary" ? "primary" : ""}`;
    button.textContent = action.label;
    button.disabled = Boolean(action.disabled_reason);
    button.title = action.disabled_reason || "";
    button.addEventListener("click", async () => {
      try {
        await executeIssueAction(issue, action);
      } catch (error) {
        setStatus(error.message || `${action.label}失败`);
        await Promise.all([refreshIssues(), refreshIssueTasks()]);
      }
    });
    actions.appendChild(button);
  }
  if (actions.children.length) issueDetail.appendChild(actions);
}

function selectIssue(issueId) {
  state.activeIssue = issueId;
  renderIssueList();
  renderIssueDetail(state.issues.find((item) => item.id === issueId));
}

function activeTaskIssueIds() {
  return new Set(
    state.issueTasks
      .filter((task) => ["queued", "running"].includes(task.status))
      .map((task) => task.issue_id),
  );
}

function visibleIssues() {
  const active = activeTaskIssueIds();
  return state.issues.filter((issue) => !active.has(issue.id));
}

function reconcileActiveIssue() {
  const visible = visibleIssues();
  if (!visible.some((item) => item.id === state.activeIssue)) {
    state.activeIssue = visible[0]?.id || null;
  }
}

function renderIssueList() {
  issueList.replaceChildren();
  const visible = visibleIssues();
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "issue-list-empty";
    empty.textContent = "当前筛选下没有问题。";
    issueList.appendChild(empty);
    return;
  }
  for (const issue of visible) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `issue-card ${issue.id === state.activeIssue ? "active" : ""}`;
    const top = document.createElement("span");
    top.className = "issue-card-topline";
    const kind = document.createElement("b");
    kind.textContent = issueKindLabels[issue.kind] || issue.kind;
    const badge = document.createElement("small");
    const displayState = issueDisplayState(issue);
    badge.className = displayState.className;
    badge.textContent = displayState.label;
    top.append(kind, badge);
    const title = document.createElement("strong");
    title.textContent = issue.title;
    const summary = document.createElement("span");
    summary.className = "issue-card-summary";
    summary.textContent = issue.summary;
    const occurred = document.createElement("time");
    occurred.className = "issue-card-time";
    occurred.dateTime = issue.created_at;
    occurred.textContent = `发现于 ${formatIssueTime(issue.created_at, { compact: true })}`;
    button.append(top, title, summary, occurred);
    button.addEventListener("click", () => selectIssue(issue.id));
    issueList.appendChild(button);
  }
}

const taskStatusLabels = {
  queued: "等待中",
  running: "进行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
const taskActionLabels = { retry: "重试来源", issue_retry: "重试来源", rescan: "重新扫描" };

function upsertIssueTask(task) {
  const index = state.issueTasks.findIndex((item) => item.id === task.id);
  if (index >= 0) state.issueTasks[index] = task;
  else state.issueTasks.unshift(task);
}

function renderIssueTasks() {
  issueTaskList.replaceChildren();
  const active = state.issueTasks.filter((task) => ["queued", "running"].includes(task.status));
  issueTaskCount.textContent = `${active.length} 个活动任务`;
  workbenchTaskTabCount.textContent = String(active.length);
  issueTaskCount.classList.toggle("active", active.length > 0);
  if (!active.length) {
    const empty = document.createElement("p");
    empty.className = "issue-task-empty";
    empty.textContent = "当前没有后台任务。";
    issueTaskList.appendChild(empty);
    return;
  }
  for (const task of active) {
    const row = document.createElement("article");
    row.className = `issue-task task-${task.status}`;
    const marker = document.createElement("span");
    marker.className = "issue-task-marker";
    marker.setAttribute("aria-hidden", "true");
    const main = document.createElement("div");
    main.className = "issue-task-main";
    const title = document.createElement("strong");
    title.textContent = task.resource || task.title || "未命名资源";
    const stage = document.createElement("span");
    const stagePosition = task.stage_index > 0
      ? ` ${task.stage_index}/${task.stage_total}`
      : "";
    stage.textContent = ["queued", "running"].includes(task.status)
      ? `当前阶段${stagePosition} · ${task.current_stage || "等待更新"}`
      : task.current_stage || "等待更新";
    main.append(title, stage);
    if (["queued", "running"].includes(task.status)) {
      const progress = document.createElement("div");
      progress.className = "issue-task-progress";
      const fill = document.createElement("span");
      const ratio = task.stage_total > 0 ? task.stage_index / task.stage_total : 0;
      fill.style.width = `${Math.max(3, Math.min(100, ratio * 100))}%`;
      progress.appendChild(fill);
      main.appendChild(progress);
    }
    const context = document.createElement("div");
    context.className = "issue-task-context";
    const action = document.createElement("span");
    action.textContent = taskActionLabels[task.action] || task.action;
    context.appendChild(action);
    if (task.source_stage) {
      const original = document.createElement("span");
      original.textContent = `原失败阶段：${task.source_stage}`;
      context.appendChild(original);
    }
    const badge = document.createElement("span");
    badge.className = `issue-task-status task-${task.status}`;
    badge.textContent = taskStatusLabels[task.status] || task.status;
    row.append(marker, main, context, badge);
    if (task.error) {
      row.title = task.error;
      const error = document.createElement("p");
      error.className = "issue-task-error";
      error.textContent = task.error;
      main.appendChild(error);
    }
    issueTaskList.appendChild(row);
  }
}

async function refreshIssueTasks() {
  const response = await fetch("/api/jobs?limit=100");
  if (!response.ok) throw new Error("无法读取运行队列");
  state.issueTasks = await response.json();
  reconcileActiveIssue();
  renderIssueTasks();
  renderIssueList();
  renderIssueDetail(state.issues.find((item) => item.id === state.activeIssue));
}

async function refreshIssues() {
  const statusFilter = $("issue-status-filter")?.value || "open,blocked";
  const kindFilter = $("issue-kind-filter")?.value || "";
  const params = new URLSearchParams({ status: statusFilter });
  if (kindFilter) params.set("kind", kindFilter);
  const [response, summaryResponse, syncResponse] = await Promise.all([
    fetch(`/api/issues?${params}`),
    fetch("/api/issues/summary"),
    fetch("/api/sync/status").catch(() => null),
  ]);
  if (!response.ok || !summaryResponse.ok) throw new Error("无法读取问题列表");
  if (syncResponse && syncResponse.ok) {
    const sync = await syncResponse.json();
    const pending = (sync.dirty || 0) + (sync.removed || 0);
    syncBadge.textContent = String(pending);
    syncBadge.classList.toggle("needs-sync", pending > 0);
    syncButton.disabled = (sync.in_flight || 0) > 0;
    syncButton.title = syncButton.disabled
      ? "上一次快照还在执行，等队列排空"
      : `待同步变更 ${pending} 个：拍快照并入队编译`;
  }
  state.issues = await response.json();
  const summary = await summaryResponse.json();
  const activeCount = summary.active || 0;
  const retryableCount = summary.retryable || 0;
  retryEligibleCount.textContent = String(retryableCount);
  retryEligibleButton.disabled = retryableCount === 0;
  workbenchIssueTabCount.textContent = String(activeCount);
  queueCount.textContent = activeCount;
  queueCount.classList.toggle("has-errors", activeCount > 0);
  reconcileActiveIssue();
  renderIssueList();
  renderIssueDetail(state.issues.find((item) => item.id === state.activeIssue));
}

async function syncNow() {
  syncButton.disabled = true;
  setStatus("正在拍快照并入队编译……");
  const response = await fetch("/api/sync", { method: "POST" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "无法发起同步");
  for (const task of payload.tasks || []) upsertIssueTask(task);
  renderIssueTasks();
  setStatus(payload.count ? `快照入队 ${payload.count} 个任务` : "没有待同步变更");
  await Promise.all([refreshIssues(), refreshIssueTasks()]);
}

async function retryEligibleIssues() {
  const count = Number(retryEligibleCount.textContent || 0);
  if (!count) return;
  if (!window.confirm(`将 ${count} 个来源有效的失败项加入串行重试队列，是否继续？`)) return;
  retryEligibleButton.disabled = true;
  setStatus("正在创建批量重试任务……");
  const response = await fetch("/api/issues/actions/retry-eligible", { method: "POST" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "无法创建批量重试任务");
  for (const task of payload.tasks || []) upsertIssueTask(task);
  renderIssueTasks();
  selectWorkbenchTab("tasks");
  setStatus(payload.count ? `已加入 ${payload.count} 个重试任务` : "当前没有可重试项");
  await Promise.all([refreshIssues(), refreshIssueTasks()]);
}

async function executeIssueAction(issue, action) {
  if (action.id === "open_resource") {
    openIssueResource(issue);
    return;
  }
  if (action.requires_confirmation && !window.confirm(`确定要“${action.label}”吗？`)) return;
  setStatus(`正在${action.label}……`);
  const response = await fetch(`/api/issues/${issue.id}/actions/${action.id}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ payload: {} }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `${action.label}失败`);
  }
  if (response.status === 202) {
    const task = await response.json();
    upsertIssueTask(task);
    renderIssueTasks();
    selectWorkbenchTab("tasks");
    await waitForIssueTask(task.id, action.label);
  }
  setStatus("就绪");
  await Promise.all([refreshIssues(), refreshIssueTasks()]);
}

async function waitForIssueTask(taskId, label) {
  while (true) {
    await new Promise((resolve) => window.setTimeout(resolve, 600));
    const response = await fetch(`/api/issue-tasks/${taskId}`);
    if (!response.ok) throw new Error(`无法读取${label}任务状态`);
    const task = await response.json();
    upsertIssueTask(task);
    renderIssueTasks();
    if (task.status === "completed") return task;
    if (["failed", "cancelled"].includes(task.status)) {
      throw new Error(task.error || `${label}失败`);
    }
    setStatus(`${label}进行中……`);
  }
}

function formatBytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

async function createSession() {
  const response = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "未命名" }),
  });
  if (!response.ok) throw new Error("无法创建会话");
  const session = await response.json();
  state.sessions.unshift(session);
  await selectSession(session.id);
  renderSessions();
}

async function selectSession(id) {
  closeWikiPage("chat");
  state.activeSession = id;
  const session = state.sessions.find((item) => item.id === id);
  $("session-title").textContent = session?.title || id;
  messages.replaceChildren();
  try {
    const response = await fetch(`/api/sessions/${id}/messages`);
    if (!response.ok) throw new Error("无法读取会话历史");
    const history = await response.json();
    if (!history.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "会话已准备好，可以开始提问。";
      messages.appendChild(empty);
    }
    for (const message of history) {
      const node = addMessage(message.role, message.content || "");
      if (message.role === "assistant") {
        node.innerHTML = renderMarkdown(message.content || "");
        node.classList.add("markdown");
      }
    }
  } catch (error) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = error.message || "无法读取会话历史";
    messages.appendChild(empty);
  }
  input.disabled = false;
  sendButton.disabled = false;
  renderSessions();
}

messages.addEventListener("click", (event) => {
  const link = event.target.closest(".wiki-link");
  if (link) {
    event.preventDefault();
    openWikiPage(link.dataset.wikiPath);
  }
});
wikiContent.addEventListener("click", (event) => {
  const link = event.target.closest(".wiki-link");
  if (link) {
    event.preventDefault();
    openWikiPage(link.dataset.wikiPath);
  }
});
wikiHistoryBack.addEventListener("click", () => navigateWikiHistory(-1));
wikiHistoryForward.addEventListener("click", () => navigateWikiHistory(1));
$("wiki-close").addEventListener("click", () => closeWikiPage("chat"));
$("issue-center-open").addEventListener("click", showIssueCenter);
$("issue-center-close").addEventListener("click", closeIssueCenter);
workbenchTaskTab.addEventListener("click", () => {
  selectWorkbenchTab("tasks");
  refreshIssueTasks().catch((error) => setStatus(error.message));
});
workbenchIssueTab.addEventListener("click", () => {
  selectWorkbenchTab("issues");
  refreshIssues().catch((error) => setStatus(error.message));
});
$("issue-status-filter").addEventListener("change", () => refreshIssues().catch((error) => setStatus(error.message)));
$("issue-kind-filter").addEventListener("change", () => refreshIssues().catch((error) => setStatus(error.message)));
retryEligibleButton.addEventListener("click", () => {
  retryEligibleIssues().catch(async (error) => {
    setStatus(error.message || "批量重试失败");
    await Promise.all([refreshIssues(), refreshIssueTasks()]);
  });
});
syncButton.addEventListener("click", () => {
  syncNow().catch(async (error) => {
    setStatus(error.message || "同步失败");
    await refreshIssues();
  });
});
document.addEventListener("keydown", (event) => {
  if (state.view === "issues" && event.key === "Escape") {
    closeIssueCenter();
    return;
  }
  if (state.view !== "wiki") return;
  if (event.altKey && event.key === "ArrowLeft") {
    event.preventDefault();
    navigateWikiHistory(-1);
  } else if (event.altKey && event.key === "ArrowRight") {
    event.preventDefault();
    navigateWikiHistory(1);
  } else if (event.key === "Escape") {
    closeWikiPage("chat");
  }
});

async function sendMessage(text) {
  if (!state.activeSession || state.busy) return;
  state.busy = true;
  input.disabled = true;
  sendButton.disabled = true;
  messages.querySelector(".empty-state")?.remove();
  addMessage("user", text);
  const answer = addMessage("assistant");
  let answerText = "";
  setStatus("正在思考……");
  try {
    const response = await fetch(`/api/sessions/${state.activeSession}/messages/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!response.ok || !response.body) throw new Error("请求失败");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const handleEventBlock = (block) => {
      const dataLine = block.split(/\r?\n/).find((line) => line.startsWith("data:"));
      if (!dataLine) return;
      const event = JSON.parse(dataLine.slice(5).trim());
      // AgentEvent keeps lifecycle payloads under `data`; older adapters
      // may put fields at the top level, so accept both shapes.
      const data = event.data || event;
      if (data.delta && event.type !== "reasoning_delta") {
        answerText += data.delta;
        // Keep the in-progress view stable; Markdown is rendered once the
        // complete response arrives so unfinished fences do not flicker.
        answer.textContent = answerText;
      }
      if (event.type === "run_error" || event.type === "error" || data.error) {
        throw new Error(data.error || "运行失败");
      }
    };
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() || "";
      for (const block of blocks) handleEventBlock(block);
      messages.scrollTop = messages.scrollHeight;
    }
    if (buffer.trim()) handleEventBlock(buffer);
    answer.innerHTML = renderMarkdown(answerText);
    answer.classList.add("markdown");
    setStatus("就绪");
    await refreshSessions();
    await refreshWikiFiles();
    await refreshIssues();
  } catch (error) {
    answer.remove();
    addMessage("error", error.message || "请求失败");
    setStatus("请求失败");
  } finally {
    state.busy = false;
    input.disabled = false;
    sendButton.disabled = false;
    input.focus();
  }
}

$("new-session").addEventListener("click", () => createSession().catch((error) => setStatus(error.message)));
$("message-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  sendMessage(text);
});

(async function bootstrap() {
  try {
    await refreshSessions();
    await refreshWikiFiles();
    await Promise.all([refreshIssues(), refreshIssueTasks()]);
    if (state.sessions.length) await selectSession(state.sessions[0].id);
    else await createSession();
  } catch (error) {
    setStatus(error.message || "连接失败");
  }
})();

window.setInterval(() => {
  if (state.view === "issues") {
    Promise.all([refreshIssues(), refreshIssueTasks()]).catch(() => {});
  }
}, 1500);
