const state = {
  sessions: [],
  activeSession: null,
  busy: false,
  view: "chat",
  wikiHistory: [],
  wikiHistoryIndex: -1,
  wikiRequestId: 0,
};

const $ = (id) => document.getElementById(id);
const sessionList = $("session-list");
const messages = $("messages");
const input = $("message-input");
const sendButton = $("send-button");
const status = $("connection-status");
const wikiFiles = $("wiki-files");
const failureQueue = $("failure-queue");
const queueCount = $("queue-count");
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
  wikiHistoryBack.disabled = state.wikiHistoryIndex <= 0;
  wikiHistoryForward.disabled = state.wikiHistoryIndex >= state.wikiHistory.length - 1;
}

function rememberWikiScroll() {
  const current = state.wikiHistory[state.wikiHistoryIndex];
  if (current) current.scrollTop = wikiViewer.scrollTop;
}

function showWikiViewer() {
  state.view = "wiki";
  chatHeader.hidden = true;
  messages.hidden = true;
  $("message-form").hidden = true;
  wikiViewer.hidden = false;
}

function closeWikiPage() {
  rememberWikiScroll();
  state.view = "chat";
  wikiViewer.hidden = true;
  chatHeader.hidden = false;
  messages.hidden = false;
  $("message-form").hidden = false;
  wikiMetadata.replaceChildren();
  wikiBasic.replaceChildren();
  wikiSummary.textContent = "";
  const session = state.sessions.find((item) => item.id === state.activeSession);
  $("session-title").textContent = session?.title || state.activeSession || "选择一个会话";
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
    const response = await fetch(kind === "source" ? sourceUrl(path) : pageUrl(path));
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

function openWikiPage(path, kind = "page") {
  const continuingNavigation = state.view === "wiki" && state.wikiHistoryIndex >= 0;
  if (continuingNavigation) rememberWikiScroll();
  const entry = { path, kind, scrollTop: 0 };
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

function navigateWikiHistory(offset) {
  const nextIndex = state.wikiHistoryIndex + offset;
  if (nextIndex < 0 || nextIndex >= state.wikiHistory.length) return;
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

async function refreshFailureQueue() {
  const response = await fetch("/api/queue");
  if (!response.ok) throw new Error("无法读取错误队列");
  const items = await response.json();
  queueCount.textContent = items.length;
  queueCount.classList.toggle("has-errors", items.length > 0);
  failureQueue.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "queue-empty";
    empty.textContent = "没有待处理错误";
    failureQueue.appendChild(empty);
    return;
  }
  for (const item of items) {
    const node = document.createElement("div");
    node.className = "queue-item";
    const retryable = item.retry_policy === "auto_retry" || item.retry_policy === "retry_once";
    node.innerHTML = `<strong></strong><span class="queue-badge ${retryable ? "retry" : "manual"}">${retryable ? "可重试" : "待处理"}</span><small></small>`;
    node.querySelector("strong").textContent = item.file || item.source || item.type || "未知错误";
    node.querySelector("small").textContent = item.error || item.detail || item.stage || item.type || "";
    failureQueue.appendChild(node);
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
  closeWikiPage();
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
$("wiki-close").addEventListener("click", closeWikiPage);
document.addEventListener("keydown", (event) => {
  if (state.view !== "wiki") return;
  if (event.altKey && event.key === "ArrowLeft") {
    event.preventDefault();
    navigateWikiHistory(-1);
  } else if (event.altKey && event.key === "ArrowRight") {
    event.preventDefault();
    navigateWikiHistory(1);
  } else if (event.key === "Escape") {
    closeWikiPage();
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
    await refreshFailureQueue();
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
    await refreshFailureQueue();
    if (state.sessions.length) await selectSession(state.sessions[0].id);
    else await createSession();
  } catch (error) {
    setStatus(error.message || "连接失败");
  }
})();
