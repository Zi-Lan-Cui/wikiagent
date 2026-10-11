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
  centerView: "issues",
  previewProposal: null,  // 待处置的整理提议；入队成功后据此记处置
  maintainProposals: [],  // 账本中待处置的提议，服务端保证至多一条
};

const $ = (id) => document.getElementById(id);
const sessionList = $("session-list");
const messages = $("messages");
const input = $("message-input");
const sendButton = $("send-button");
const wikiFiles = $("wiki-files");
const wikiSpace = $("wiki-space");
const wikiStageEmpty = $("wiki-stage-empty");
const queueCount = $("queue-count");
const issueCenter = $("issue-center");
const issueList = $("issue-list");
const issueDetail = $("issue-detail");
const issueTaskList = $("issue-task-list");
const issueTaskCount = $("issue-task-count");
const organizeView = $("organize-view");
const attentionView = $("attention-view");
const organizeRunning = $("organize-running");
const retryEligibleButton = $("retry-eligible");
const retryEligibleCount = $("retry-eligible-count");
const syncButton = $("sync-now");
const syncBadge = $("sync-badge");
const maintainPreviewButton = $("maintain-preview");
const linkScanButton = $("link-scan");
const maintainDialog = $("maintain-dialog");
const maintainSummary = $("maintain-summary");
const maintainUnits = $("maintain-units");
const maintainSubmitButton = $("maintain-submit");
const syncState = $("sync-state");
const queueRecentToggle = $("queue-recent-toggle");
const queueRecentPanel = $("queue-recent");
const syncDiffDialog = $("sync-diff-dialog");
const syncDiffList = $("sync-diff-list");
const centerStatus = $("center-status");
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

// 贴底跟随：流式输出时自动滚到底，但用户上滑翻阅就停止打断，
// 出现"回到底部"按钮；滚回底部或发新消息恢复跟随
const chatPanel = document.querySelector(".chat-panel");
let stickToBottom = true;
function isNearBottom() {
  return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 60;
}
function scrollToBottom(force = false) {
  if (!force && !stickToBottom) return;
  stickToBottom = true;
  messages.scrollTop = messages.scrollHeight;
  updateJumpVisibility();
}
const jumpToBottom = document.createElement("button");
jumpToBottom.type = "button";
jumpToBottom.className = "jump-to-bottom";
jumpToBottom.textContent = "回到底部";
jumpToBottom.hidden = true;
jumpToBottom.addEventListener("click", () => scrollToBottom(true));
chatPanel.appendChild(jumpToBottom);
// 按钮只在"真的滚得动且不贴底"时出现：短会话与空状态没有底部可回
function updateJumpVisibility() {
  const scrollable = messages.scrollHeight - messages.clientHeight > 60;
  jumpToBottom.hidden = stickToBottom || !scrollable;
}
messages.addEventListener("scroll", () => {
  stickToBottom = isNearBottom();
  updateJumpVisibility();
});

function setStatus(text) {
  // 状态只落在页内状态条（右上角全局指示已移除：聊天页的反馈在气泡与
  // 错误消息里，维护/待处理页在 center-status）
  centerStatus.textContent = text;
  centerStatus.classList.remove("error");
}

function setStatusError(text) {
  setStatus(text);
  centerStatus.classList.add("error");
}

// 动作按钮忙态：禁用 + 旋转伪元素 + 卡片高亮，恢复在 finally
function setBusy(button, busy) {
  button.disabled = busy;
  button.classList.toggle("busy", busy);
  const card = button.closest(".maintain-card");
  if (card) card.classList.toggle("pending", busy);
}

function addMessage(role, text = "") {
  const node = document.createElement("div");
  node.className = `message ${role}`;
  node.textContent = text;
  messages.appendChild(node);
  scrollToBottom(true);
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

/* 轻量代码高亮：字符串/注释/数字/关键词/装饰器五类 token，不引外部库；
   着色只用现有纸-墨-砖红梯度，未知语言退化为 generic 规则 */
const hlPython = [
  ["com", /#.*/y],
  ["str", /[rbfRBF]{0,3}"""[\s\S]*?"""/y],
  ["str", /[rbfRBF]{0,3}'''[\s\S]*?'''/y],
  ["str", /"(?:\\.|[^"\\\n])*"/y],
  ["str", /'(?:\\.|[^'\\\n])*'/y],
  ["dec", /@[A-Za-z_][\w.]*/y],
  ["kw", /\b(?:def|class|return|if|elif|else|for|while|in|not|and|or|is|None|True|False|import|from|as|with|try|except|finally|raise|lambda|yield|global|nonlocal|assert|pass|break|continue|async|await)\b/y],
  ["num", /\b\d[\d_]*(?:\.\d+)?\b/y],
];
const hlJs = [
  ["com", /\/\/.*/y],
  ["com", /\/\*[\s\S]*?\*\//y],
  ["str", /"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`/y],
  ["kw", /\b(?:const|let|var|function|return|if|else|for|while|in|of|new|class|extends|import|export|default|async|await|try|catch|finally|throw|typeof|instanceof|null|undefined|true|false|this)\b/y],
  ["num", /\b\d[\d_]*(?:\.\d+)?\b/y],
];
const hlShell = [
  ["com", /#.*/y],
  ["str", /"(?:\\.|[^"\\\n])*"|'[^']*'/y],
  ["var", /\$\{[^}]*\}|\$[A-Za-z_]\w*/y],
  ["kw", /\b(?:if|then|else|elif|fi|for|in|do|done|while|case|esac|function|export|local|return)\b/y],
];
const hlJson = [
  ["key", /"(?:\\.|[^"\\\n])*"(?=\s*:)/y],
  ["str", /"(?:\\.|[^"\\\n])*"/y],
  ["kw", /\b(?:true|false|null)\b/y],
  ["num", /-?\d+(?:\.\d+)?/y],
];
const hlGeneric = [
  ["com", /\/\/.*/y],
  ["str", /"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'/y],
  ["num", /\b\d[\d_]*(?:\.\d+)?\b/y],
];
const hlLanguages = {
  python: hlPython, py: hlPython,
  javascript: hlJs, js: hlJs, typescript: hlJs, ts: hlJs, jsx: hlJs, tsx: hlJs,
  bash: hlShell, sh: hlShell, shell: hlShell, zsh: hlShell, console: hlShell,
  json: hlJson,
};
/* 围栏不带语言标注时按内容特征猜一个；猜不出退 generic。
   顺序即优先级：python 信号最强（def/self），shell 的命令词避开散文误伤 */
function detectCodeLang(raw) {
  if (/\bdef\s+\w+\s*\(|\bself\b|>>>|^from\s+\w/m.test(raw)) return "python";
  if (/=>|\b(?:const|let)\s+\w+\s*=|console\.\w/.test(raw)) return "javascript";
  if (/^\s*[{[]/m.test(raw) && /"\s*[\w.-]+"\s*:/.test(raw)) return "json";
  if (/\b(?:sudo|apt|docker|git|pip|uv|curl|wget|mkdir)\b/.test(raw)) return "bash";
  return "";
}
function highlightCode(raw, lang) {
  const rules = (lang && hlLanguages[lang.toLowerCase()]) || hlGeneric;
  let out = "";
  let i = 0;
  outer: while (i < raw.length) {
    for (const [cls, re] of rules) {
      re.lastIndex = i;
      const match = re.exec(raw);
      if (match && match[0]) {
        out += `<span class="hl-${cls}">${escapeHtml(match[0])}</span>`;
        i += match[0].length;
        continue outer;
      }
    }
    out += escapeHtml(raw[i]);
    i += 1;
  }
  return out;
}

// 代码块右上角的悬停复制钮；clipboard 在非安全上下文失败时只改文案不抛错
function mountCodeUI(root) {
  for (const pre of root.querySelectorAll("pre")) {
    const shell = document.createElement("div");
    shell.className = "code-shell";
    pre.replaceWith(shell);
    shell.appendChild(pre);
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "code-copy";
    copy.textContent = "复制";
    copy.addEventListener("click", async () => {
      let ok = true;
      try {
        await navigator.clipboard.writeText(pre.innerText);
      } catch {
        // async clipboard 在无头/失焦环境会拒绝，改用隐藏 textarea + execCommand
        ok = false;
        const ta = document.createElement("textarea");
        ta.value = pre.innerText;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { ok = document.execCommand("copy"); } catch { /* 保持 false */ }
        ta.remove();
      }
      copy.textContent = ok ? "已复制" : "复制失败";
      window.setTimeout(() => { copy.textContent = "复制"; }, 1400);
    });
    shell.appendChild(copy);
  }
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
    const codeText = codeLines.join("\n");
    const lang = codeLanguage || detectCodeLang(codeText);
    html.push(`<pre><code${lang ? ` class="language-${escapeHtml(lang)}"` : ""}>${highlightCode(codeText, lang)}</code></pre>`);
    codeLines = [];
    codeLanguage = "";
  };

  const splitRow = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  const renderTable = (header, bodyRows) => {
    const head = splitRow(header).map((c) => `<th>${inline(c)}</th>`).join("");
    const body = bodyRows
      .map((row) => `<tr>${splitRow(row).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`)
      .join("");
    return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];

    // GFM 表格：| 行 + 分隔行起头，连续 | 行入表体
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length
        && /^[\s|:-]+$/.test(lines[i + 1]) && lines[i + 1].includes("-") && lines[i + 1].includes("|")) {
      flushParagraph(); flushList(); flushOrderedList();
      const body = [];
      let j = i + 2;
      while (j < lines.length && /^\s*\|.*\|\s*$/.test(lines[j])) {
        body.push(lines[j]);
        j += 1;
      }
      html.push(renderTable(line, body));
      i = j - 1;
      continue;
    }

    // 缩进代码块（模型高频用法）：4 空格/Tab 起始、与上文空行或块边界相接
    if (/^(?: {4}|\t)\S/.test(line) && !paragraph.length && !listItems.length && !orderedItems.length) {
      const collected = [];
      let j = i;
      while (j < lines.length && (/^(?: {4}|\t)/.test(lines[j])
          || (!lines[j].trim() && j + 1 < lines.length && /^(?: {4}|\t)\S/.test(lines[j + 1])))) {
        if (lines[j].trim()) collected.push(lines[j].replace(/^(?: {4}|\t)/, ""));
        else collected.push("");
        j += 1;
      }
      const indented = collected.join("\n");
      html.push(`<pre><code>${highlightCode(indented, detectCodeLang(indented))}</code></pre>`);
      i = j - 1;
      continue;
    }

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
    // 分节符：独占一行的 --- / *** / ___ 渲染成 hairline 横线（代码块内的已在上面按字面量收集）
    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flushParagraph(); flushList(); flushOrderedList();
      html.push("<hr>");
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
    ? (state.wikiReturnView === "issues" ? "返回待处理" : state.wikiReturnView === "organize" ? "返回维护" : "返回会话")
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

function setActiveDestination(view) {
  for (const [key, id] of [["wiki", "wiki-open"], ["organize", "organize-open"], ["issues", "attention-open"]]) {
    $(id).classList.toggle("active", view === key);
  }
}

// initWikiRail 注入的抽屉开关：进库弹出选页，选完自动收拢让位阅读
let wikiDrawerReveal = () => {};
let wikiDrawerHide = () => {};

function showWikiViewer() {
  state.view = "wiki";
  jumpToBottom.hidden = true;
  chatHeader.hidden = true;
  messages.hidden = true;
  issueCenter.hidden = true;
  $("message-form").hidden = true;
  wikiSpace.hidden = false;
  wikiStageEmpty.hidden = true;
  wikiViewer.hidden = false;
  wikiDrawerHide();
  setActiveDestination("wiki");
}

// 从侧栏进入 Wiki 库：先弹列表，读完的页面不自动复开
function showWikiSpace() {
  state.view = "wiki";
  jumpToBottom.hidden = true;
  chatHeader.hidden = true;
  messages.hidden = true;
  draftStage.hidden = true;
  issueCenter.hidden = true;
  $("message-form").hidden = true;
  wikiSpace.hidden = false;
  wikiViewer.hidden = true;
  wikiStageEmpty.hidden = false;
  wikiDrawerReveal();
  setActiveDestination("wiki");
  persistNav();
}

function closeWikiPage(destination = state.wikiReturnView || "chat") {
  rememberWikiScroll();
  if (destination === "issues" || destination === "organize") restoreCenterView(destination);
  else restoreChatView();
  state.wikiReturnView = "chat";
  wikiMetadata.replaceChildren();
  wikiBasic.replaceChildren();
  wikiSummary.textContent = "";
  const session = state.sessions.find((item) => item.id === state.activeSession);
  $("session-title").textContent = session?.title || state.activeSession || "选择一个会话";
  persistNav();
}

function restoreChatView() {
  state.view = "chat";
  updateJumpVisibility();
  wikiViewer.hidden = true;
  wikiSpace.hidden = true;
  issueCenter.hidden = true;
  $("message-form").hidden = false;
  setActiveDestination(null);
  syncDraftView();
}

const CENTER_COPY = {
  organize: {
    eyebrow: "知识库维护",
    title: "维护",
    sub: "同步笔记、整理结构、补全链接；提交的任务在下方排队执行。",
  },
  issues: {
    eyebrow: "知识库运行",
    title: "待处理",
    sub: "资料处理失败、你提出的纠错、质量提醒，需要逐条决定怎么处理。",
  },
};

function showCenterView(view) {
  state.view = view;
  jumpToBottom.hidden = true;
  chatHeader.hidden = true;
  messages.hidden = true;
  draftStage.hidden = true;
  wikiViewer.hidden = true;
  wikiSpace.hidden = true;
  $("message-form").hidden = true;
  setActiveDestination(view);
  issueCenter.hidden = false;
  organizeView.hidden = view !== "organize";
  attentionView.hidden = view !== "issues";
  const copy = CENTER_COPY[view];
  $("center-eyebrow").textContent = copy.eyebrow;
  $("center-title").textContent = copy.title;
  $("center-sub").textContent = copy.sub;
  persistNav();
}

function restoreCenterView(view, { refresh = false } = {}) {
  const target = view === "organize" ? "organize" : "issues";
  state.centerView = target;
  showCenterView(target);
  if (refresh) {
    Promise.all([refreshIssues(), refreshIssueTasks()]).catch((error) => setStatusError(error.message));
  }
}

function showOrganize() {
  rememberWikiScroll();
  restoreCenterView("organize", { refresh: true });
}

function showAttention() {
  rememberWikiScroll();
  restoreCenterView("issues", { refresh: true });
}

// 动作提交后把用户带到能看见任务进度的地方
function showTaskProgress() {
  if (state.view !== "organize") showOrganize();
}

function closeIssueCenter() {
  restoreChatView();
}

async function loadWikiEntry(entry) {
  showWikiViewer();
  updateWikiNavigation();
  persistNav();
  const requestId = ++state.wikiRequestId;
  const { path, kind } = entry;
  wikiViewerPath.textContent = path;
  wikiPageTitle.textContent = "正在加载";
  wikiMetadata.replaceChildren();
  wikiBasic.replaceChildren();
  wikiSummary.textContent = "";
  wikiContent.innerHTML = "<p class=wiki-loading>正在加载页面</p>";
  setStatus("正在打开页面");
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
    mountCodeUI(wikiContent);
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
  else state.wikiReturnView = state.view === "issues" || state.view === "organize" ? state.view : "chat";
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
    // 空串、空集合都不渲染，避免只剩一个孤零零的标签
    if (value === undefined || value === "" || value === null) return;
    if (Array.isArray(value) && !value.length) return;
    const item = document.createElement("div");
    item.className = `wiki-meta-field wiki-meta-${key} ${className}`;
    const label = document.createElement("span");
    label.className = "wiki-meta-label";
    label.textContent = labels[key] || key;
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
        if (index > 0) {
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

const draftStage = $("draft-stage");
const onboardingNode = $("onboarding");
const messageForm = $("message-form");

/* 草稿态下整个对话区换成大 logo 落地页，composer 从底部移挂进页面。
   只挪 DOM 节点不重建：事件绑定与输入内容跟着节点走 */
function syncDraftView() {
  const draft = state.activeSession === null && state.view === "chat";
  draftStage.hidden = !draft;
  messages.hidden = draft;
  // 草稿页顶栏整条撤掉：大 logo 就是页头，不需要再挂"新对话"三个字
  chatHeader.hidden = draft;
  if (draft) {
    if (messageForm.parentElement !== draftStage) draftStage.appendChild(messageForm);
  } else if (messageForm.parentElement !== chatPanel) {
    chatPanel.appendChild(messageForm);
  }
}

function openNewChat() {
  closeWikiPage("chat");
  restoreChatView();
  state.activeSession = null;
  messages.replaceChildren();
  stickToBottom = true;
  jumpToBottom.hidden = true;
  syncDraftView();
  onboardingNode.hidden = state.sessions.length > 0;
  renderSessions();
  input.disabled = false;
  sendButton.disabled = false;
  // 移动端自动聚焦会顶起键盘挡住草稿页；只在有指针设备时聚焦
  if (window.matchMedia("(pointer: fine)").matches) input.focus();
  persistNav();
}

function renderSessions() {
  sessionList.replaceChildren();
  for (const session of state.sessions) {
    // 旧流程点击即建会话，留下一批 0 消息存根；延迟创建后只保留正在使用的
    if (session.message_count === 0 && session.id !== state.activeSession) continue;
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

// 首轮结束后后台 LLM 定名要晚几秒；标题仍是占位形态（未命名/截断带…）
// 时补查三次，拿到定名或次数用尽即停
function refreshTitleSoon(id) {
  let tries = 0;
  const timer = window.setInterval(async () => {
    tries += 1;
    try {
      await refreshSessions();
    } catch {
      /* 下一轮再试 */
    }
    const session = state.sessions.find((item) => item.id === id);
    const done = !session || tries >= 3 || (session.title !== "未命名" && !session.title.endsWith("…"));
    if (done) {
      window.clearInterval(timer);
      if (session && id === state.activeSession) $("session-title").textContent = session.title;
    }
  }, 3500);
}

async function refreshWikiFiles() {
  const response = await fetch("/api/wiki/files");
  if (!response.ok) throw new Error("无法读取 Wiki 文件列表");
  const files = await response.json();
  $("wiki-count").textContent = String(files.length);
  wikiFiles.replaceChildren();
  if (!files.length) {
    const empty = document.createElement("div");
    empty.className = "wiki-file";
    empty.textContent = "还没有页面，首次同步后生成";
    wikiFiles.appendChild(empty);
    return;
  }
  // 按顶层目录分组：行内不再重复前缀，一屏的信息密度才降得下来；
  // 目录行可点击折叠，折叠状态跨刷新记忆（纯外观，文件行为不变）
  const groups = new Map();
  for (const file of files) {
    const idx = file.path.lastIndexOf("/");
    const dir = idx < 0 ? "根目录" : file.path.slice(0, idx);
    if (!groups.has(dir)) groups.set(dir, []);
    groups.get(dir).push(file);
  }
  let folded = new Set();
  try { folded = new Set(JSON.parse(localStorage.getItem("wikiFoldedDirs") || "[]")); } catch { folded = new Set(); }
  const saveFolded = () => { try { localStorage.setItem("wikiFoldedDirs", JSON.stringify([...folded])); } catch { /* 存不下就不记忆 */ } };
  for (const [dir, items] of groups) {
    const section = document.createElement("section");
    section.className = `wiki-group${folded.has(dir) ? " collapsed" : ""}`;
    const label = document.createElement("button");
    label.type = "button";
    label.className = "wiki-group-label";
    label.setAttribute("aria-expanded", String(!folded.has(dir)));
    label.innerHTML = `<span class="group-name"></span><b class="group-count">${items.length}</b>`;
    label.querySelector(".group-name").textContent = dir;
    label.addEventListener("click", () => {
      const collapsed = section.classList.toggle("collapsed");
      label.setAttribute("aria-expanded", String(!collapsed));
      if (collapsed) folded.add(dir); else folded.delete(dir);
      saveFolded();
    });
    section.appendChild(label);
    for (const file of items) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "wiki-file";
      item.title = `${file.path} · ${formatBytes(file.size)}`;
      item.innerHTML = `<span></span><small>${formatBytes(file.size)}</small>`;
      const name = file.path.slice(file.path.lastIndexOf("/") + 1).replace(/\.md$/, "");
      item.querySelector("span").textContent = name;
      item.addEventListener("click", () => openWikiPage(file.path));
      section.appendChild(item);
    }
    wikiFiles.appendChild(section);
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
    empty.textContent = "选择一个问题，查看原因与可执行的操作。";
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
    evidence.innerHTML = "<h4>相关记录</h4>";
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
    empty.textContent = "没有符合条件的问题，换个筛选试试。";
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

/* 内部词表 → 用户话术：kind 前缀、阶段名、阶段完成度。
   后端 progress() 用的阶段名在这里统一翻译，数据模型不迁就文案 */
const taskKindTitleLabels = { 编译: "同步", 重组: "结构调整", 补链: "补全链接", 删除: "清理" };
// 只翻译内部黑话（成文/落盘/出链/消解等），普通书面词原样保留；措辞用书面语域
const taskStageLabels = {
  等待执行: "排队等待",
  编译成页: "编译为页面",
  页面核对: "核对受影响页面",
  逐页成文: "逐页改写内容",
  写入落盘: "写入文件",
  质量扫描: "质量检查",
  核对出链: "核对页内链接",
  复扫问题: "重新检查问题",
  同步质量问题: "更新问题记录",
  初步建议: "生成修改建议",
  二次复核: "复核建议",
  冲突消解: "处理冲突",
  // 编译流水线细粒度阶段（progress_scope 转发，稳定英文码）
  load: "读取文件",
  convert: "转换格式",
  extract: "提炼摘要",
  search: "检索相关页面",
  analyze: "分析页面关系",
  plan: "规划页面改动",
  execute: "生成页面内容",
};
// running 任务的完成度按阶段估算；未知阶段取 40，不做假精确
const taskStageProgress = {
  读取资料: 15, 编译成页: 55, 页面核对: 30, 逐页成文: 60, 写入落盘: 85,
  质量扫描: 90, 提交版本: 96, 核对出链: 50, 复扫问题: 50, 同步质量问题: 90,
  初步建议: 35, 二次复核: 70, 冲突消解: 90,
  load: 12, convert: 20, extract: 32, search: 45, analyze: 58, plan: 68, execute: 82,
};
// 把带 slug 的内部标题翻成人话；认不出的 kind 回退原标题
function taskTitleForUser(task) {
  const fallback = String(task.title || task.resource || "未命名任务");
  const pageName = (raw) => String(raw || "").split("/").pop().replace(/\.md$/, "");
  if (task.kind === "restructure" && fallback.includes(" → ")) {
    const [from, to] = fallback.replace(/^重组\s*/, "").split(" → ");
    const src = (from || "").split("+").filter(Boolean).map(pageName);
    if ((to || "").trim() === "（删除）") return `删除页面 ${src.join("、")}`;
    if (src.length > 1) return `合并 ${src.join("、")} 为「${pageName(to)}」`;
    return `重写页面「${pageName(to)}」`;
  }
  if (task.kind === "link") return `补全链接「${pageName(task.resource || fallback.replace(/^补链\s*/, ""))}」`;
  if (task.kind === "compile") return `同步「${pageName(task.resource)}」`;
  if (task.kind === "delete") return `清理「${pageName(task.resource)}」`;
  return fallback.replace(/^(编译|重组|补链|删除)\s/, (_m, k) => `${taskKindTitleLabels[k]} `);
}
function taskStageForUser(task) {
  const raw = task.current_stage || "";
  const label = taskStageLabels[raw] || raw;
  if (task.status === "queued") return "排队中";
  if (task.status === "running") return label ? `正在${label}` : "正在执行";
  return label || taskStatusLabels[task.status] || "";
}
function taskProgressPercent(task) {
  if (task.status !== "running") return 6;
  return taskStageProgress[task.current_stage || ""] ?? 40;
}

function timeAgo(iso) {
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return "";
  const mins = Math.max(0, Math.floor((Date.now() - ts) / 60000));
  if (mins < 60) return `${mins} 分钟前`;
  if (mins < 1440) return `${Math.floor(mins / 60)} 小时前`;
  if (mins < 10080) return `${Math.floor(mins / 1440)} 天前`;
  return new Date(ts).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

/* 队尾"最近完成"：终态任务不再跑完即消失，回答"系统最近替我做了什么"；
   折叠在队列底部，复用任务行语言，只保留标题/结果/相对时间 */
let queueRecentOpen = false;

function renderRecentDone() {
  const done = state.issueTasks
    .filter((task) => ["completed", "failed", "cancelled"].includes(task.status))
    .sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))
    .slice(0, 5);
  queueRecentToggle.hidden = done.length === 0;
  queueRecentToggle.textContent = `最近完成 ${done.length}`;
  queueRecentToggle.setAttribute("aria-expanded", String(queueRecentOpen));
  queueRecentPanel.hidden = !queueRecentOpen || done.length === 0;
  if (!queueRecentOpen) {
    queueRecentPanel.replaceChildren();
    return;
  }
  const rows = done.map((task) => {
    const row = document.createElement("div");
    row.className = `issue-task done task-${task.status}`;
    const marker = document.createElement("span");
    marker.className = "issue-task-marker";
    const main = document.createElement("div");
    main.className = "issue-task-main";
    const title = document.createElement("strong");
    title.textContent = taskTitleForUser(task);
    const when = document.createElement("span");
    when.textContent = timeAgo(task.updated_at);
    main.append(title, when);
    const chip = document.createElement("span");
    chip.className = `issue-task-status task-${task.status}`;
    chip.textContent = taskStatusLabels[task.status] || task.status;
    row.append(marker, main, document.createElement("span"), chip);
    return row;
  });
  queueRecentPanel.replaceChildren(...rows);
}

queueRecentToggle.addEventListener("click", () => {
  queueRecentOpen = !queueRecentOpen;
  renderRecentDone();
});

function upsertIssueTask(task) {
  const index = state.issueTasks.findIndex((item) => item.id === task.id);
  if (index >= 0) state.issueTasks[index] = task;
  else state.issueTasks.unshift(task);
}

// 在途任务出现新进展时清掉状态条旧提示——进度已由任务行本身承载
let lastTasksSignature = "";

function renderIssueTasks() {
  issueTaskList.replaceChildren();
  // 显示顺序=执行顺序：running 最前，其余按提交先后（created_at 升序）
  const rank = { running: 0, queued: 1 };
  const active = state.issueTasks
    .filter((task) => ["queued", "running"].includes(task.status))
    .sort(
      (a, b) =>
        (rank[a.status] ?? 2) - (rank[b.status] ?? 2) ||
        String(a.created_at).localeCompare(String(b.created_at)),
    );
  const signature = active.map((task) => `${task.id}:${task.status}:${task.current_stage}`).join("|");
  if (lastTasksSignature && signature !== lastTasksSignature) setStatus("");
  lastTasksSignature = signature;
  issueTaskCount.textContent = `${active.length} 个活动任务`;
  state.runningTasks = active.length;
  renderRecentDone();
  updateOrganizeBadge();
  issueTaskCount.classList.toggle("active", active.length > 0);
  // 整理结构分析的任务态：在途禁用三个动作（基线动了提议就没意义），
  // 出终态后最近一次结果留在列表底部供回看
  const previews = state.issueTasks.filter((task) => task.kind === "restructure_preview");
  const previewActive = previews.some((task) => ["queued", "running"].includes(task.status));
  // 待处置提议 ≤1 由账本的部分唯一索引强制；分析失败不产提议行，
  // 无待处置提议时用最近一条失败任务行交代原因
  const pendingProposal = state.maintainProposals[0] || null;
  const previewFailed = previews.filter((task) => task.status === "failed").at(-1) || null;
  const previewRow = pendingProposal
    ? proposalRow(pendingProposal)
    : previewFailed
      ? previewFailedRow(previewFailed)
      : null;
  maintainPreviewButton.textContent = previewActive ? "分析进行中" : "发起结构分析";
  setBusy(maintainPreviewButton, previewActive);
  for (const partner of [syncButton, linkScanButton]) {
    if (previewActive) {
      partner.disabled = true;
      partner.title = "结构分析进行中";
    } else if (!partner.classList.contains("busy")) {
      partner.disabled = false;
      partner.title = "";
    }
  }
  if (!active.length && !previewRow) {
    const empty = document.createElement("p");
    empty.className = "issue-task-empty";
    empty.textContent = "没有进行中的任务；同步、整理、补链提交后会出现在这里。";
    issueTaskList.appendChild(empty);
    return;
  }
  // 重组批尾的波及面补链（一页一任务）折叠成一行：N 条建议不该被
  // 几十张补链卡淹没；展开信息在行内文案里（剩余/共/当前）
  const tailGroups = new Map();
  const visible = [];
  for (const task of active) {
    if (task.kind === "link" && task.action === "tail" && task.batch) {
      let group = tailGroups.get(task.batch);
      if (!group) {
        group = { batch: task.batch, inFlight: [] };
        tailGroups.set(task.batch, group);
        visible.push({ tailGroup: group });
      }
      group.inFlight.push(task);
      continue;
    }
    visible.push(task);
  }
  for (const group of tailGroups.values()) {
    group.total = state.issueTasks.filter(
      (item) => item.kind === "link" && item.action === "tail" && item.batch === group.batch,
    ).length;
  }
  for (const item of visible) {
    if (item.tailGroup) {
      issueTaskList.appendChild(tailGroupRow(item.tailGroup));
      continue;
    }
    const task = item;
    const row = document.createElement("article");
    row.className = `issue-task task-${task.status}`;
    const marker = document.createElement("span");
    marker.className = "issue-task-marker";
    marker.setAttribute("aria-hidden", "true");
    const main = document.createElement("div");
    main.className = "issue-task-main";
    const title = document.createElement("strong");
    // 标题、阶段、进度都走用户话术映射（见 taskTitleForUser/taskStageForUser）；
    // 内部 resource/原标题留在悬停里
    title.textContent = taskTitleForUser(task);
    row.title = task.resource || "";
    const stage = document.createElement("span");
    stage.textContent = taskStageForUser(task);
    main.append(title, stage);
    if (["queued", "running"].includes(task.status)) {
      const progress = document.createElement("div");
      progress.className = "issue-task-progress";
      const fill = document.createElement("span");
      fill.style.width = `${taskProgressPercent(task)}%`;
      progress.appendChild(fill);
      main.appendChild(progress);
    }
    const context = document.createElement("div");
    context.className = "issue-task-context";
    const actionLabel = taskActionLabels[task.action];
    if (actionLabel) {
      const action = document.createElement("span");
      action.textContent = actionLabel;
      context.appendChild(action);
    }
    if (task.source_stage) {
      const original = document.createElement("span");
      original.textContent = `原失败阶段：${taskStageLabels[task.source_stage] || task.source_stage}`;
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
  if (previewRow) issueTaskList.appendChild(previewRow);
}

async function refreshIssueTasks() {
  const response = await fetch("/api/jobs?limit=100");
  if (!response.ok) throw new Error("无法读取运行队列");
  state.issueTasks = await response.json();
  const proposalsResponse = await fetch("/api/maintenance/proposals");
  state.maintainProposals = proposalsResponse.ok
    ? (await proposalsResponse.json()).proposals || []
    : [];
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
      ? "上一批同步还在执行，等它跑完"
      : `待同步变更 ${pending} 个，点击后重新编译成 Wiki`;
    syncState.hidden = false;
    syncState.textContent = pending > 0 ? "查看变更" : "已同步";
    syncState.classList.toggle("pending", pending > 0);
    syncState.disabled = pending === 0;
    syncState.title = pending > 0 ? "点击查看变更文件清单" : "";
    state.syncFiles = pending > 0 ? sync.files : null;
    state.syncPending = pending;
    updateOrganizeBadge();
    renderSyncDiff();
  }
  state.issues = await response.json();
  const summary = await summaryResponse.json();
  const activeCount = summary.active || 0;
  const retryableCount = summary.retryable || 0;
  retryEligibleCount.textContent = String(retryableCount);
  retryEligibleButton.disabled = retryableCount === 0;
  queueCount.textContent = activeCount;
  queueCount.classList.toggle("has-errors", activeCount > 0);
  reconcileActiveIssue();
  renderIssueList();
  renderIssueDetail(state.issues.find((item) => item.id === state.activeIssue));
}

async function syncNow() {
  setBusy(syncButton, true);
  setStatus("正在同步");
  try {
    const response = await fetch("/api/sync", { method: "POST" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "无法发起同步");
    for (const task of payload.tasks || []) upsertIssueTask(task);
    renderIssueTasks();
    showTaskProgress();
    setStatus(payload.count ? `已提交 ${payload.count} 个同步任务，进度见下方任务队列` : "没有待同步变更");
  } finally {
    setBusy(syncButton, false);
    await Promise.all([refreshIssues(), refreshIssueTasks()]).catch(() => {});
  }
}

async function retryEligibleIssues() {
  const count = Number(retryEligibleCount.textContent || 0);
  if (!count) return;
  if (!window.confirm(`将 ${count} 个可重试失败项加入队列？`)) return;
  retryEligibleButton.disabled = true;
  setStatus("正在提交重试");
  const response = await fetch("/api/issues/actions/retry-eligible", { method: "POST" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "无法创建批量重试任务");
  for (const task of payload.tasks || []) upsertIssueTask(task);
  renderIssueTasks();
  showTaskProgress();
  setStatus(payload.count ? `已加入 ${payload.count} 个重试任务` : "当前没有可重试项");
  await Promise.all([refreshIssues(), refreshIssueTasks()]);
}

async function openMaintenancePreview() {
  // 提交入队即返回；按钮忙态与结果由任务队列驱动（renderIssueTasks）
  setBusy(maintainPreviewButton, true);
  try {
    const response = await fetch("/api/maintenance/preview", { method: "POST" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "无法发起结构分析");
    upsertIssueTask(payload.task);
    setStatus("结构分析已提交");
  } catch (error) {
    setStatusError(error.message || "结构分析发起失败");
  } finally {
    await refreshIssueTasks().catch(() => {});
    setBusy(maintainPreviewButton, false);
  }
}

// 否决一条提议：账本处置后该行消失；采纳走提交口（同事务记 submitted）
async function dismissProposal(proposal) {
  const response = await fetch(`/api/maintenance/proposals/${proposal.id}/dismiss`, {
    method: "POST",
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "操作失败");
  await refreshIssueTasks().catch(() => {});
}

// 待处置提议行（维护账本 pending 条目）：给入口（查看提议/否决）。
// 与普通任务行同构：marker + main + 状态 + 操作（动作收行尾，徽章族里它是唯一无框的提示）
function proposalRow(proposal) {
  const payload = proposal.payload || {};
  const row = document.createElement("article");
  // 徽章族里 completed/failed 都是执行态；提议等人拍板不是执行完毕，
  // 用 queued 的墨色活态，文案与侧栏「待处理」区隔
  row.className = "issue-task task-queued";
  const marker = document.createElement("span");
  marker.className = "issue-task-marker";
  marker.setAttribute("aria-hidden", "true");
  const main = document.createElement("div");
  main.className = "issue-task-main";
  const title = document.createElement("strong");
  title.textContent = "整理提议";
  const detail = document.createElement("span");
  detail.textContent = payload.healthy
    ? "结构健康"
    : `${(payload.effective || []).length} 条整理提议`;
  main.append(title, detail);
  const actions = document.createElement("div");
  actions.className = "issue-task-actions";
  if (!payload.healthy && (payload.effective || []).length) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost-button";
    button.textContent = "查看提议";
    button.addEventListener("click", () => {
      state.previewProposal = proposal;  // 提交时按它把提议记为已采纳
      state.maintainUnits = payload.effective || [];
      renderMaintainDialog(payload);
      maintainDialog.showModal();
    });
    actions.appendChild(button);
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "ghost-button danger";
    dismiss.textContent = "不采纳";
    dismiss.addEventListener("click", async () => {
      try {
        await dismissProposal(proposal);
        setStatus("提议已按不采纳处理");
      } catch (error) {
        setStatusError(error.message || "操作失败");
      }
    });
    actions.appendChild(dismiss);
  }
  const badge = document.createElement("span");
  // 提示不是按钮：无框无底的中列小字，靠行首墨点表达"在等人"
  badge.className = "issue-task-note";
  badge.textContent = "待确认";
  row.prepend(marker, main);
  row.append(badge, actions);
  return row;
}

// 分析失败行：无待处置提议时交代最近一次分析的失败原因
function previewFailedRow(task) {
  const row = document.createElement("article");
  row.className = "issue-task task-failed";
  const marker = document.createElement("span");
  marker.className = "issue-task-marker";
  marker.setAttribute("aria-hidden", "true");
  const main = document.createElement("div");
  main.className = "issue-task-main";
  const title = document.createElement("strong");
  title.textContent = "结构分析";
  const detail = document.createElement("span");
  detail.textContent = `失败：${task.error || "分析未完成"}`;
  main.append(title, detail);
  row.title = task.error || "";
  const badge = document.createElement("span");
  badge.className = "issue-task-status task-failed";
  badge.textContent = taskStatusLabels[task.status] || task.status;
  row.append(marker, main, badge);
  return row;
}

function renderMaintainDialog(payload) {
  maintainSummary.textContent = `共产生 ${payload.proposed} 条建议，复核后剩 ${payload.confirmed} 条，其中可执行 ${(payload.effective || []).length} 条`;
  maintainUnits.innerHTML = "";
  (payload.effective || []).forEach((unit, index) => {
    const row = document.createElement("label");
    row.className = "maintain-unit";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = true;
    box.dataset.index = String(index);
    box.addEventListener("change", updateMaintainSubmit);
    const title = document.createElement("span");
    const out = (unit.out || []).map((p) => p.slug).join(" + ") || "（删除）";
    title.textContent = `${(unit.in_pages || []).join(" + ")} → ${out}`;
    const reason = document.createElement("small");
    reason.textContent = unit.reason || "";
    row.append(box, title, reason);
    maintainUnits.appendChild(row);
  });
  updateMaintainSubmit();
}

function updateMaintainSubmit() {
  const checked = maintainUnits.querySelectorAll("input:checked").length;
  maintainSubmitButton.disabled = checked === 0;
  maintainSubmitButton.textContent = `执行（${checked}）`;
}

async function submitMaintenance() {
  const units = [...maintainUnits.querySelectorAll("input:checked")].map(
    (box) => state.maintainUnits[Number(box.dataset.index)],
  );
  setBusy(maintainSubmitButton, true);
  const response = await fetch("/api/maintenance", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // 提交口在同一事务里把该提议记为 submitted，防止同一批建议二次入队
    body: JSON.stringify({ units, proposal_id: state.previewProposal?.id || "" }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    setBusy(maintainSubmitButton, false);
    throw new Error(payload.detail || "提议提交失败");
  }
  maintainDialog.close();
  state.previewProposal = null;
  for (const task of payload.tasks || []) upsertIssueTask(task);
  renderIssueTasks();
  showTaskProgress();
  setStatus(`已入队 ${payload.count} 个任务，整批可撤销`);
  await Promise.all([refreshIssues(), refreshIssueTasks()]);
}

async function linkScan() {
  if (!window.confirm("对全部页面发起出链体检？按页提交，可逐页撤销。")) return;
  setBusy(linkScanButton, true);
  try {
    const response = await fetch("/api/link", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "补链发起失败");
    for (const task of payload.tasks || []) upsertIssueTask(task);
    renderIssueTasks();
    showTaskProgress();
    setStatus(payload.count ? `关联扫已入队 ${payload.count} 页` : "没有可扫描的页面");
    await Promise.all([refreshIssues(), refreshIssueTasks()]);
  } finally {
    setBusy(linkScanButton, false);
  }
}

async function executeIssueAction(issue, action) {
  if (action.id === "open_resource") {
    openIssueResource(issue);
    return;
  }
  if (action.requires_confirmation && !window.confirm(`确定要“${action.label}”吗？`)) return;
  setStatus(`正在${action.label}`);
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
    showTaskProgress();
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
    setStatus(`${label}进行中`);
  }
}

function formatBytes(size) {
  // 数字与单位用不间断空格，换行不拆散（5.5 / KB 分行是最常见的排版毛刺）
  const nbsp = " ";
  if (size < 1024) return `${size}${nbsp}B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)}${nbsp}KB`;
  return `${(size / 1024 / 1024).toFixed(1)}${nbsp}MB`;
}

// 第一条消息发出前会话不落库；发出时才创建并挂在列表最前
async function materializeSession() {
  const response = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "未命名" }),
  });
  if (!response.ok) throw new Error("无法创建会话");
  const session = await response.json();
  state.sessions.unshift(session);
  state.activeSession = session.id;
  $("session-title").textContent = session.title;
  syncDraftView();
  renderSessions();
  persistNav();
}

async function selectSession(id) {
  closeWikiPage("chat");
  state.activeSession = id;
  const session = state.sessions.find((item) => item.id === id);
  $("session-title").textContent = session?.title || id;
  messages.replaceChildren();
  syncDraftView();
  persistNav();
  // 切会话即换上下文：跟随状态重置，上一会话的翻阅位置不该带过来
  stickToBottom = true;
  jumpToBottom.hidden = true;
  try {
    const response = await fetch(`/api/sessions/${id}/messages`);
    if (!response.ok) throw new Error("无法读取会话历史");
    const history = await response.json();
    if (!history.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "这个会话还没有消息，在下方输入第一个问题。";
      messages.appendChild(empty);
    }
    for (const message of history) {
      if (message.role === "assistant") {
        const { row, bubble } = assistantShell();
        bubble.innerHTML = renderMarkdown(message.content || "");
        bubble.classList.add("markdown");
        mountCodeUI(bubble);
        if (message.thinking && message.thinking.length) {
          bubble.prepend(thinkingBlock(message.thinking));
        }
        messages.appendChild(row);
      } else {
        addMessage(message.role, message.content || "");
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
$("wiki-open").addEventListener("click", showWikiSpace);

// Wiki 覆盖式抽屉：默认展开（首次进入自动弹出列表），把手随抽屉贴边可随时收起；
// 拖拽调宽（双击复位、方向键微调），宽度与收起态记在 localStorage
(function initWikiRail() {
  const drawer = $("wiki-drawer");
  const toggle = $("wiki-rail-toggle");
  const handle = $("wiki-rail-resize");
  const KEY_W = "wikiRailWidth";
  const DEFAULT_W = 300;
  const MIN_W = 200;
  const MAX_W = 480;
  let lastWidth = DEFAULT_W;
  const setWidth = (px) => drawer.style.setProperty("--rail-w", `${px}px`);
  const saved = Number(localStorage.getItem(KEY_W));
  if (saved >= MIN_W && saved <= MAX_W) {
    lastWidth = saved;
    setWidth(saved);
  }
  // 收起只在本次访问内生效：进库总是重新弹出，不跨刷新记忆
  function applyCollapsed(collapsed) {
    drawer.classList.toggle("collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-label", collapsed ? "展开列表" : "收起列表");
  }
  wikiDrawerReveal = () => applyCollapsed(false);
  wikiDrawerHide = () => applyCollapsed(true);
  applyCollapsed(false);
  toggle.addEventListener("click", () => applyCollapsed(!drawer.classList.contains("collapsed")));
  const resizeTo = (px) => {
    lastWidth = Math.min(MAX_W, Math.max(MIN_W, px));
    setWidth(lastWidth);
    localStorage.setItem(KEY_W, String(lastWidth));
  };
  let dragFrom = null;
  handle.addEventListener("pointerdown", (event) => {
    if (drawer.classList.contains("collapsed")) return;
    dragFrom = { x: event.clientX, w: lastWidth };
    handle.classList.add("dragging");
    handle.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  handle.addEventListener("pointermove", (event) => {
    if (dragFrom) resizeTo(dragFrom.w + event.clientX - dragFrom.x);
  });
  const endDrag = () => {
    dragFrom = null;
    handle.classList.remove("dragging");
  };
  handle.addEventListener("pointerup", endDrag);
  handle.addEventListener("pointercancel", endDrag);
  handle.addEventListener("dblclick", () => resizeTo(DEFAULT_W));
  handle.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft") {
      resizeTo(lastWidth - 16);
      event.preventDefault();
    } else if (event.key === "ArrowRight") {
      resizeTo(lastWidth + 16);
      event.preventDefault();
    }
  });
})();
$("organize-open").addEventListener("click", showOrganize);
$("attention-open").addEventListener("click", showAttention);
$("onboarding-sync")?.addEventListener("click", showOrganize);
$("onboarding-chat")?.addEventListener("click", () => openNewChat());
$("issue-center-close").addEventListener("click", closeIssueCenter);
$("issue-status-filter").addEventListener("change", () => refreshIssues().catch((error) => setStatus(error.message)));
$("issue-kind-filter").addEventListener("change", () => refreshIssues().catch((error) => setStatus(error.message)));
retryEligibleButton.addEventListener("click", () => {
  retryEligibleIssues().catch(async (error) => {
    setStatusError(error.message || "批量重试失败");
    await Promise.all([refreshIssues(), refreshIssueTasks()]);
  });
});
maintainPreviewButton.addEventListener("click", () => {
  openMaintenancePreview().catch((error) => setStatusError(error.message));
});
linkScanButton.addEventListener("click", () => {
  linkScan().catch((error) => setStatusError(error.message));
});
$("maintain-cancel").addEventListener("click", () => maintainDialog.close());
maintainSubmitButton.addEventListener("click", () => {
  submitMaintenance().catch((error) => setStatusError(error.message));
});
/* 侧栏"维护"计数 = 活动任务 + 待同步变更：都进队列或同步前，这里是唯一入口提示 */
function updateOrganizeBadge() {
  const sum = (state.runningTasks || 0) + (state.syncPending || 0);
  organizeRunning.textContent = String(sum);
  organizeRunning.classList.toggle("has-errors", sum > 0);
}

/* 待同步明细对话框：复用维护提议的弹窗容器，列表限高滚动，数量不构成压力 */
function renderSyncDiff() {
  const files = state.syncFiles;
  const groups = files
    ? [
        ["新增", files.added || []],
        ["修改", files.modified || []],
        ["删除", files.removed || []],
      ].filter(([, names]) => names.length)
    : [];
  const total = groups.reduce((n, [, names]) => n + names.length, 0);
  const blocks = groups.map(([label, names]) => {
    const section = document.createElement("section");
    const head = document.createElement("p");
    head.className = "sync-diff-group";
    head.textContent = `${label} ${names.length}`;
    section.append(head, ...names.map((name) => {
      const row = document.createElement("div");
      row.className = "sync-diff-item";
      const i = name.lastIndexOf("/");
      if (i > 0) {
        const dir = document.createElement("span");
        dir.className = "dir";
        dir.textContent = `${name.slice(0, i + 1)}`;
        row.append(dir, document.createTextNode(name.slice(i + 1)));
      } else {
        row.textContent = name;
      }
      return row;
    }));
    return section;
  });
  syncDiffList.replaceChildren(...blocks);
}

syncState.addEventListener("click", () => syncDiffDialog.showModal());
$("sync-diff-close").addEventListener("click", () => syncDiffDialog.close());

syncButton.addEventListener("click", () => {
  syncNow().catch(async (error) => {
    setStatusError(error.message || "同步失败");
    await refreshIssues();
  });
});
document.addEventListener("keydown", (event) => {
  if ((state.view === "issues" || state.view === "organize") && event.key === "Escape") {
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

const AGENT_AVATAR = '<span class="msg-avatar" aria-hidden="true"></span>';

// 流式渲染时给未闭合的代码围栏虚拟补闭合，避免半截 ``` 把后文吞成代码
function renderLiveMarkdown(source) {
  let src = source;
  if (((src.match(/```/g) || []).length % 2) === 1) src += "\n```";
  return renderMarkdown(src);
}

// 工具调用转为可读描述：动作 + 操作对象
function toolAction(name, args) {
  const a = args || {};
  const hint = ["pattern", "query", "keyword", "path", "dir", "file"]
    .map((k) => a[k]).find((v) => typeof v === "string" && v)
    || Object.values(a).find((v) => typeof v === "string" && v) || "";
  const short = hint.length > 36 ? `${hint.slice(0, 36)}…` : hint;
  switch (name) {
    case "Grep": return `查找内容「${short}」`;
    case "ReadFile": return `读取 ${short}`;
    case "ListDir": return `浏览目录 ${short || "wiki"}`;
    default: return `调用 ${name}${short ? `：${short}` : ""}`;
  }
}

function formatMs(ms) {
  return ms < 1000 ? `${Math.max(1, Math.round(ms))}ms` : `${(ms / 1000).toFixed(1)}s`;
}

// 历史重放：持久化的 thinking 分段还原成与实时同款的折叠块
// （think 文字段与 tool 动作行按记录顺序穿插）
function thinkingBlock(segments) {
  const details = document.createElement("details");
  details.className = "agent-think";
  const summary = document.createElement("summary");
  summary.innerHTML = '<span class="think-chev" aria-hidden="true"></span><span class="think-label">思考过程</span>';
  const flow = document.createElement("div");
  flow.className = "think-flow";
  for (const seg of segments) {
    if (seg.kind === "tool") {
      const row = document.createElement("div");
      row.className = seg.error ? "think-step error" : "think-step done";
      row.textContent = `${toolAction(seg.name, seg.arguments)} · ${formatMs(seg.ms)}${seg.error ? " · 失败" : ""}`;
      flow.appendChild(row);
    } else if (seg.text) {
      const body = document.createElement("div");
      body.className = "think-seg";
      body.textContent = seg.text;
      flow.appendChild(body);
    }
  }
  details.append(summary, flow);
  return details;
}

// 行壳：头像在气泡外（左侧），气泡本体仍是 .message.assistant
function assistantShell() {
  const row = document.createElement("div");
  row.className = "message-row assistant-row";
  const avatarHost = document.createElement("span");
  avatarHost.innerHTML = AGENT_AVATAR;
  row.appendChild(avatarHost.firstElementChild);
  const bubble = document.createElement("div");
  bubble.className = "message assistant";
  row.appendChild(bubble);
  return { row, bubble };
}

function createAssistantMessage() {
  const { row, bubble } = assistantShell();
  messages.appendChild(row);
  scrollToBottom();
  const node = row;
  const content = document.createElement("div");
  content.className = "agent-content";
  bubble.appendChild(content);

  const view = {
    node,
    content,
    body: document.createElement("div"),
    think: null,
    steps: new Map(),
    thinkText: "",
  };
  view.body.className = "agent-body markdown";
  view.content.appendChild(view.body);

  view.ensureThink = () => {
    if (view.think) return view.think;
    const details = document.createElement("details");
    details.className = "agent-think";
    details.open = true;
    const summary = document.createElement("summary");
    summary.innerHTML = '<span class="think-chev" aria-hidden="true"></span><span class="think-label">正在思考</span><span class="think-dots"><i></i><i></i><i></i></span>';
    // 单一时序列表：思考段与工具行按实际发生顺序穿插（想→查→再想）
    const flow = document.createElement("div");
    flow.className = "think-flow";
    details.append(summary, flow);
    view.content.prepend(details);
    view.think = { details, summary, flow, currentSeg: null };
    return view.think;
  };
  view.settleThink = () => {
    if (!view.think) return;
    // 发送即挂的占位盒：最终既无思考也无检索，摘掉，不留空折叠框
    if (!view.thinkText && view.steps.size === 0) {
      view.think.details.remove();
      view.think = null;
      return;
    }
    const label = view.think.summary.querySelector(".think-label");
    const n = view.steps.size;
    if (view.thinkText) {
      label.textContent = n ? `思考过程 · 检索 ${n} 步` : "思考过程";
    } else {
      label.textContent = `检索了 ${n} 步`;
    }
    view.think.summary.classList.remove("running");
    view.think.details.open = false;
  };
  return view;
}

async function sendMessage(text) {
  if (state.busy) return;
  if (!state.activeSession) {
    try {
      await materializeSession();
    } catch (error) {
      setStatusError(error.message || "无法创建会话");
      return;
    }
  }
  state.busy = true;
  input.disabled = true;
  sendButton.disabled = true;
  messages.querySelector(".empty-state")?.remove();
  addMessage("user", text);
  const view = createAssistantMessage();
  // 首包可能等数秒：发送即挂"正在思考"呼吸态，主气泡不再是死白块
  view.ensureThink();
  view.think.summary.classList.add("running");
  let answerText = "";
  let renderTimer = null;
  const scheduleRender = () => {
    if (renderTimer) return;
    renderTimer = window.setTimeout(() => {
      renderTimer = null;
      view.body.innerHTML = renderLiveMarkdown(answerText);
      mountCodeUI(view.body);
    }, 180);
  };
  setStatus("正在回答");
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
      if (event.type === "reasoning_started") {
        const t = view.ensureThink();
        t.summary.classList.add("running");
        t.currentSeg = document.createElement("div");
        t.currentSeg.className = "think-seg";
        t.flow.appendChild(t.currentSeg);
      } else if (event.type === "reasoning_delta" && data.delta) {
        const t = view.ensureThink();
        if (!t.currentSeg) {
          t.currentSeg = document.createElement("div");
          t.currentSeg.className = "think-seg";
          t.flow.appendChild(t.currentSeg);
        }
        view.thinkText += data.delta;
        t.currentSeg.textContent += data.delta;
      } else if (event.type === "tool_started") {
        const t = view.ensureThink();
        t.currentSeg = null; // 工具之后的思考属于下一段
        // 工具回合前流出的正文是过程旁白——留在主气泡会和最终回答
        // 拼成畸形 markdown；收进折叠块当作思考段，主区留给最终回答
        if (answerText.trim()) {
          const aside = document.createElement("div");
          aside.className = "think-seg";
          aside.textContent = answerText.trim();
          t.flow.appendChild(aside);
          answerText = "";
          view.body.innerHTML = "";
        }
        const row = document.createElement("div");
        row.className = "think-step run";
        row.dataset.base = toolAction(data.tool_name || "工具", data.arguments);
        row.dataset.startedAt = String(Date.now());
        row.textContent = `${row.dataset.base} …`;
        t.flow.appendChild(row);
        if (!view.thinkText) t.summary.querySelector(".think-label").textContent = "正在检索";
        view.steps.set(String(data.tool_call_id || `${data.tool_name}:${t.flow.childElementCount}`), row);
      } else if (event.type === "tool_finished" || event.type === "tool_error") {
        const row = view.steps.get(String(data.tool_call_id || ""));
        if (row) {
          const ms = Date.now() - Number(row.dataset.startedAt || Date.now());
          row.textContent = `${row.dataset.base} · ${formatMs(ms)}${event.type === "tool_error" ? " · 失败" : ""}`;
          row.classList.remove("run");
          row.classList.add(event.type === "tool_error" ? "error" : "done");
        }
      } else if (data.delta && event.type !== "reasoning_delta") {
        answerText += data.delta;
        view.settleThink();
        scheduleRender();
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
      scrollToBottom();
    }
    if (buffer.trim()) handleEventBlock(buffer);
    if (renderTimer) { window.clearTimeout(renderTimer); renderTimer = null; }
    view.body.innerHTML = renderMarkdown(answerText);
    mountCodeUI(view.body);
    view.settleThink();
    scrollToBottom();
    setStatus("就绪");
    await refreshSessions();
    refreshTitleSoon(state.activeSession);
    await refreshWikiFiles();
    await refreshIssues();
  } catch (error) {
    view.node.remove();
    addMessage("error", error.message || "请求失败");
    setStatus("请求失败");
  } finally {
    state.busy = false;
    input.disabled = false;
    sendButton.disabled = false;
    input.focus();
  }
}

$("new-session").addEventListener("click", () => openNewChat());
/* 拼音/输入法组合态：候选未上屏时回车会顺带触发 submit，丢候选像"输入法挂了"。
   compositionend 之前一律放行给输入法，不发消息 */
let imeComposing = false;
input.addEventListener("compositionstart", () => { imeComposing = true; });
input.addEventListener("compositionend", () => { imeComposing = false; });
// 输入框随内容长高，到 176px 内滚；发送后回落到基础高
function autosizeComposer() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 176)}px`;
}
input.addEventListener("input", autosizeComposer);
$("message-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (imeComposing || event.isComposing) return;
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  autosizeComposer();
  sendMessage(text);
});

/* 刷新回到刷新前的位置：导航快照存 sessionStorage——同标签刷新恢复，
   新开标签没有快照、从新对话草稿开始；记录失效（会话被删、页面读不到）
   时落回草稿。base 记录底层视图，wiki 记录覆盖层与其返回上下文。 */
const NAV_KEY = "wa-nav";
function persistNav() {
  try {
    const entry = state.wikiHistory[state.wikiHistoryIndex];
    sessionStorage.setItem(NAV_KEY, JSON.stringify({
      base: state.view,
      session: state.activeSession,
      wiki: state.view === "wiki"
        ? { path: entry?.path || null, kind: entry?.kind || "page", issueId: entry?.issueId ?? null, from: state.wikiReturnView }
        : null,
    }));
  } catch { /* 存储不可用只丢刷新恢复，不影响本会话 */ }
}

async function restoreNav() {
  let nav = null;
  try { nav = JSON.parse(sessionStorage.getItem(NAV_KEY) || "null"); } catch { nav = null; }
  if (nav?.base === "chat" && state.sessions.some((item) => item.id === nav.session)) await selectSession(nav.session);
  else if (nav?.base === "organize" || nav?.base === "issues") showCenterView(nav.base);
  else openNewChat();
  if (nav?.wiki) {
    showWikiSpace();
    if (nav.wiki.path) await openWikiPage(nav.wiki.path, nav.wiki.kind, nav.wiki.issueId);
    // openWikiPage 按当前 view 重置了返回上下文，恢复场景以快照为准
    state.wikiReturnView = nav.wiki.from;
    updateWikiNavigation();
  }
  persistNav();
}

/* 消息区滚动条占布局宽度，同列的提问框行没有滚动条，右缘需补回这一档差值；
   容器变宽或消息增减时实测写入 --scrollbar-gutter */
{
  const syncScrollbarGutter = () =>
    document.documentElement.style.setProperty(
      "--scrollbar-gutter",
      `${messages.offsetWidth - messages.clientWidth}px`
    );
  new ResizeObserver(syncScrollbarGutter).observe(messages);
  new MutationObserver(() => requestAnimationFrame(syncScrollbarGutter)).observe(messages, { childList: true });
}

(async function bootstrap() {
  try {
    await refreshSessions();
    await refreshWikiFiles();
    await Promise.all([refreshIssues(), refreshIssueTasks()]);
    await restoreNav();
  } catch (error) {
    setStatus(error.message || "连接失败");
  }
})();

// 批尾补链折叠行——与普通任务行同构（marker + main + 徽章），
// 组内一页一任务的事实不变，只是展示收敛为一行
function tailGroupRow(group) {
  const running = group.inFlight.find((task) => task.status === "running");
  const rowStatus = running ? "running" : "queued";
  const row = document.createElement("article");
  row.className = `issue-task task-${rowStatus}`;
  const marker = document.createElement("span");
  marker.className = "issue-task-marker";
  marker.setAttribute("aria-hidden", "true");
  const main = document.createElement("div");
  main.className = "issue-task-main";
  const title = document.createElement("strong");
  title.textContent = "自动补全链接";
  const stage = document.createElement("span");
  const done = group.total - group.inFlight.length;
  const pageName = (raw) => String(raw || "").split("/").pop().replace(/\.md$/, "");
  stage.textContent = `${done}/${group.total} 页已完成 · ${running ? `正在补全链接「${pageName(running.resource)}」` : "排队中"}`;
  main.append(title, stage);
  const progress = document.createElement("div");
  progress.className = "issue-task-progress";
  const fill = document.createElement("span");
  const ratio = group.total ? (group.total - group.inFlight.length) / group.total : 0;
  // 真实完成度，一页都没完成时也留 6% 起点头，避免误以为卡死
  fill.style.width = `${Math.max(6, Math.min(100, ratio * 100))}%`;
  progress.appendChild(fill);
  main.appendChild(progress);
  // 空 context 占第三列，徽章才落在 auto 第四列，不再被 1fr 列拉成整条
  const context = document.createElement("div");
  context.className = "issue-task-context";
  const badge = document.createElement("span");
  badge.className = `issue-task-status task-${rowStatus}`;
  badge.textContent = running ? "进行中" : "等待中";
  row.append(marker, main, context, badge);
  return row;
}

// 有排队/进行中的任务才轮询——队列空着就不该持续打 /api/jobs
// （进入页面本身有一次刷新，终态结果行渲染不依赖轮询）
window.setInterval(() => {
  if (state.view !== "issues" && state.view !== "organize") return;
  if (!state.issueTasks.some((task) => ["queued", "running"].includes(task.status))) return;
  if (state.view === "issues") {
    Promise.all([refreshIssues(), refreshIssueTasks()]).catch(() => {});
  } else {
    refreshIssueTasks().catch(() => {});
  }
}, 1500);
