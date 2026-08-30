const state = { sessions: [], activeSession: null, busy: false };

const $ = (id) => document.getElementById(id);
const sessionList = $("session-list");
const messages = $("messages");
const input = $("message-input");
const sendButton = $("send-button");
const status = $("connection-status");
const wikiFiles = $("wiki-files");

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
function renderMarkdown(source) {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const html = [];
  let inCode = false;
  let codeLanguage = "";
  let codeLines = [];
  let paragraph = [];
  let listItems = [];

  const inline = (text) => escapeHtml(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/_([^_]+)_/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

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
    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length;
      html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
    } else if (list) {
      flushParagraph();
      listItems.push(list[1]);
    } else if (!line.trim()) {
      flushParagraph();
      flushList();
    } else {
      flushList();
      paragraph.push(line);
    }
  }
  if (inCode) flushCode();
  flushParagraph();
  flushList();
  return html.join("");
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
    const item = document.createElement("div");
    item.className = "wiki-file";
    item.title = file.path;
    item.innerHTML = `<span></span><small>${formatBytes(file.size)}</small>`;
    item.querySelector("span").textContent = file.path;
    wikiFiles.appendChild(item);
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
    if (state.sessions.length) await selectSession(state.sessions[0].id);
    else await createSession();
  } catch (error) {
    setStatus(error.message || "连接失败");
  }
})();
