import argparse
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
import uvicorn


def print_banner() -> None:
    art = r"""
     ________________
    |.-----------.   |
    ||   _____   |ooo|
    ||  |     |  |ooo|
    ||  |     |  | = |
    ||  '-----'  | _ |
    ||___________|[_]|
    '----------------'
------------------------------------------------
"""
    print(art)
    print("Microwave Network (gateway)")


@dataclass
class NodeInfo:
    node_id: str
    host: str
    port: int
    region: str
    models: List[str]
    metadata: Dict[str, Any] = field(default_factory=dict)
    last_heartbeat: float = 0.0
    last_latency_ms: float = -1.0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


app = FastAPI(title="Microwave AI Gateway")
nodes: Deque[NodeInfo] = deque()
_rr_index = 0
HEALTH_INTERVAL_SECONDS = 10
_health_task = None


async def _periodic_health_check() -> None:
    """Background loop: ping every registered node every HEALTH_INTERVAL_SECONDS."""
    while True:
        await asyncio.sleep(HEALTH_INTERVAL_SECONDS)
        if not nodes:
            continue
        async with httpx.AsyncClient() as client:
            for node in list(nodes):
                start = time.perf_counter()
                try:
                    resp = await client.get(f"{node.base_url}/health", timeout=3.0)
                    if resp.status_code == 200:
                        node.last_heartbeat = time.time()
                        node.last_latency_ms = (time.perf_counter() - start) * 1000.0
                    else:
                        node.last_latency_ms = -1.0
                except Exception:
                    node.last_latency_ms = -1.0


@app.on_event("startup")
async def start_health_loop() -> None:
    global _health_task
    _health_task = asyncio.create_task(_periodic_health_check())


@app.post("/nodes/register")
async def register_node(payload: Dict[str, Any]) -> JSONResponse:
    node_id = payload.get("node_id")
    host = payload.get("host")
    port = payload.get("port")
    region = payload.get("region", "LAN")
    models = payload.get("models") or []
    metadata = payload.get("metadata") or {}

    if not node_id or not host or not port:
        raise HTTPException(status_code=400, detail="node_id, host, and port are required")

    # Replace any existing node with the same id
    global nodes
    nodes = deque(
        n for n in nodes if n.node_id != node_id
    )
    nodes.append(
        NodeInfo(
            node_id=node_id,
            host=host,
            port=int(port),
            region=region,
            models=models,
            metadata=metadata,
        )
    )
    return JSONResponse({"status": "ok"})


@app.get("/nodes")
async def list_nodes() -> List[Dict[str, Any]]:
    return [
        {
            "node_id": n.node_id,
            "host": n.host,
            "port": n.port,
            "region": n.region,
            "models": n.models,
            "metadata": n.metadata,
            "online": n.last_latency_ms >= 0,
            "last_latency_ms": n.last_latency_ms,
        }
        for n in nodes
    ]


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    # Simple UI that shows node status and a chat box
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Microwave AI – Dashboard</title>
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 0; background: #050816; color: #e5e7eb; }
    header { padding: 1rem 1.5rem; background: #020617; border-bottom: 1px solid #1f2937; display: flex; justify-content: space-between; align-items: center; }
    h1 { font-size: 1.1rem; margin: 0; }
    main { display: grid; grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr); gap: 1.5rem; padding: 1.5rem; }
    section { background: #020617; border-radius: 0.75rem; border: 1px solid #1f2937; padding: 1rem 1.25rem; }
    h2 { font-size: 0.95rem; margin: 0 0 0.75rem 0; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.08em; }
    table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
    th, td { padding: 0.35rem 0.5rem; text-align: left; }
    th { color: #9ca3af; border-bottom: 1px solid #1f2937; font-weight: 500; }
    tr:nth-child(even) { background: rgba(15,23,42,0.5); }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size: 0.78rem; }
    .badge { display: inline-flex; align-items: center; padding: 0.1rem 0.4rem; border-radius: 999px; font-size: 0.7rem; border: 1px solid #1f2937; background: #020617; color: #e5e7eb; }
    .badge.green { border-color: #15803d; color: #bbf7d0; }
    .chat-input { width: 100%; padding: 0.5rem 0.6rem; border-radius: 0.5rem; border: 1px solid #1f2937; background: #020617; color: #e5e7eb; font-size: 0.85rem; }
    .chat-input:focus { outline: none; border-color: #3b82f6; box-shadow: 0 0 0 1px #1d4ed8; }
    .btn { cursor: pointer; border: none; padding: 0.45rem 0.9rem; border-radius: 999px; font-size: 0.8rem; font-weight: 500; background: #2563eb; color: white; display: inline-flex; align-items: center; gap: 0.3rem; }
    .btn:disabled { opacity: 0.5; cursor: default; }
    .btn-secondary { background: transparent; border: 1px solid #1f2937; color: #9ca3af; }
    .row { display: flex; gap: 0.6rem; margin-top: 0.75rem; align-items: center; }
    .label { font-size: 0.75rem; color: #9ca3af; margin-bottom: 0.25rem; }
    textarea { width: 100%; min-height: 120px; padding: 0.6rem; border-radius: 0.5rem; border: 1px solid #1f2937; background: #020617; color: #e5e7eb; font-size: 0.8rem; resize: vertical; }
    textarea:focus { outline: none; border-color: #3b82f6; box-shadow: 0 0 0 1px #1d4ed8; }
    .small { font-size: 0.75rem; color: #6b7280; }
    .pill { padding: 0.1rem 0.4rem; border-radius: 999px; border: 1px solid #1f2937; font-size: 0.7rem; color: #9ca3af; }
    .status-dot { width: 8px; height: 8px; border-radius: 999px; margin-right: 0.3rem; display: inline-block; background: #22c55e; }
    .status-dot.offline { background: #ef4444; }
    .latency { font-size: 0.7rem; color: #6b7280; }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Microwave AI – Phase 0 Control Plane</h1>
      <div class="small">Gateway dashboard · LAN prototype</div>
    </div>
    <div class="small">
      <span class="pill" id="gatewayStatus"><span class="status-dot"></span>Gateway online</span>
    </div>
  </header>
  <main>
    <section>
      <h2>Nodes</h2>
      <div class="small" style="margin-bottom: 0.4rem;">Registered nodes exposed via <code>POST /nodes/register</code>.</div>
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Host</th>
            <th>Region</th>
            <th>Models</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody id="nodesTableBody">
          <tr><td colspan="4" class="small">Loading nodes…</td></tr>
        </tbody>
      </table>
      <div class="row" style="margin-top: 0.8rem;">
        <button class="btn-secondary btn" type="button" onclick="refreshNodes()">Refresh</button>
        <button class="btn-secondary btn" type="button" onclick="pingNodes()">Ping</button>
        <span class="small" id="nodesMeta"></span>
      </div>
    </section>
    <section>
      <h2>Chat</h2>
      <div class="label">Prompt</div>
      <textarea id="promptInput" placeholder="Ask Microwave AI anything…"></textarea>
      <div class="row">
        <div style="flex: 1;">
          <div class="label">Region</div>
          <input class="chat-input" id="regionInput" value="LAN" />
        </div>
        <div style="flex: 1;">
          <div class="label">Model</div>
          <input class="chat-input" id="modelInput" value="llama3.2" />
        </div>
      </div>
      <div class="row">
        <button class="btn" type="button" id="sendBtn" onclick="sendChat()">
          <span>Send</span>
        </button>
        <span class="small" id="chatStatus"></span>
      </div>
      <div class="label" style="margin-top: 0.9rem;">Conversation</div>
      <div id="chatWindow" style="border-radius:0.5rem;border:1px solid #1f2937;background:#020617;padding:0.6rem;max-height:260px;overflow-y:auto;">
        <div class="small" style="color:#6b7280;">Messages will appear here.</div>
      </div>
    </section>
  </main>
  <script>
    async function refreshNodes() {
      const body = document.getElementById('nodesTableBody');
      const meta = document.getElementById('nodesMeta');
      meta.textContent = 'Refreshing…';
      try {
        const res = await fetch('/nodes');
        const data = await res.json();
        if (!Array.isArray(data) || data.length === 0) {
          body.innerHTML = '<tr><td colspan="4" class="small">No nodes registered.</td></tr>';
          meta.textContent = '0 nodes';
          return;
        }
        body.innerHTML = '';
        for (const n of data) {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><code>${n.node_id}</code></td>
            <td><code>${n.host}:${n.port}</code></td>
            <td>${n.region}</td>
            <td>${(n.models || []).map(m => '<span class="badge">' + m + '</span>').join(' ')}</td>
            <td>
              <span class="badge ${n.online ? 'green' : ''}">
                <span class="status-dot ${n.online ? '' : 'offline'}"></span>
                ${n.online ? 'Online' : 'Unknown'}
              </span>
              ${typeof n.last_latency_ms === 'number' && n.last_latency_ms >= 0
                ? '<div class="latency">' + n.last_latency_ms.toFixed(1) + ' ms</div>'
                : ''}
            </td>
          `;
          body.appendChild(tr);
        }
        meta.textContent = data.length + ' node' + (data.length === 1 ? '' : 's');
      } catch (e) {
        body.innerHTML = '<tr><td colspan="4" class="small">Error loading nodes.</td></tr>';
        meta.textContent = 'Error';
      }
    }

    async function pingNodes() {
      const meta = document.getElementById('nodesMeta');
      meta.textContent = 'Pinging…';
      try {
        await fetch('/nodes/health', { method: 'POST' });
      } catch (e) {
        meta.textContent = 'Ping error';
        return;
      }
      await refreshNodes();
      meta.textContent += ' · health updated';
    }

    async function sendChat() {
      const promptEl = document.getElementById('promptInput');
      const regionEl = document.getElementById('regionInput');
      const modelEl = document.getElementById('modelInput');
      const chatWindow = document.getElementById('chatWindow');
      const statusEl = document.getElementById('chatStatus');
      const btn = document.getElementById('sendBtn');

      const prompt = promptEl.value.trim();
      if (!prompt) {
        statusEl.textContent = 'Enter a prompt first.';
        return;
      }
      // create user bubble
      const userBubble = document.createElement('div');
      userBubble.className = 'small';
      userBubble.style.marginBottom = '0.4rem';
      userBubble.innerHTML = '<div style="text-align:right;"><span class="badge" style="background:#1f2937;">You</span></div><div style="margin-top:0.15rem;text-align:right;">' + prompt.replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</div>';
      chatWindow.appendChild(userBubble);

      // create assistant bubble we will stream into
      const botBubble = document.createElement('div');
      botBubble.className = 'small';
      botBubble.style.marginBottom = '0.8rem';
      botBubble.innerHTML = '<div><span class="badge green">Microwave AI</span> <span id="routeInfo" class="latency"></span></div><div id="botText" style="margin-top:0.15rem;white-space:pre-wrap;"></div>';
      chatWindow.appendChild(botBubble);
      const botTextEl = botBubble.querySelector('#botText');
      const routeInfoEl = botBubble.querySelector('#routeInfo');

      chatWindow.scrollTop = chatWindow.scrollHeight;
      statusEl.textContent = 'Sending…';
      btn.disabled = true;

      try {
        const res = await fetch('/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            prompt,
            region: regionEl.value || null,
            model: modelEl.value || null,
          }),
        });
        if (!res.ok || !res.body) {
          statusEl.textContent = 'Error: ' + res.status;
          btn.disabled = false;
          return;
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();

        let buffer = '';
        let fullText = '';
        let done = false;
        while (!done) {
          const result = await reader.read();
          done = result.done;
          if (result.value) {
            const chunk = decoder.decode(result.value, { stream: !done });
            buffer += chunk;

            // Ollama streams JSON lines; split on newlines and parse each
            const lines = buffer.split('\\n');
            buffer = lines.pop(); // keep last partial line
            for (const line of lines) {
              if (!line.trim()) continue;
              try {
                const obj = JSON.parse(line);
                // Optional routing info header
                if (obj.route && routeInfoEl && !routeInfoEl.textContent) {
                  routeInfoEl.textContent = `· ${obj.route.node_id} (${obj.route.host}:${obj.route.port}, ${obj.route.model || 'model'})`;
                }
                if (typeof obj.response === 'string') {
                  fullText += obj.response;
                  botTextEl.textContent = fullText;
                  chatWindow.scrollTop = chatWindow.scrollHeight;
                }
              } catch (e) {
                // Fallback: treat as plain text if not JSON
                fullText += line;
                botTextEl.textContent = fullText;
                chatWindow.scrollTop = chatWindow.scrollHeight;
              }
            }
          }
        }
        statusEl.textContent = 'Done.';
      } catch (e) {
        statusEl.textContent = 'Error: ' + (e && e.message ? e.message : 'unknown');
      } finally {
        btn.disabled = false;
      }
    }

    // Allow Enter to send, Shift+Enter for newline
    document.getElementById('promptInput').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChat();
      }
    });

    // Initial load
    refreshNodes();
  </script>
</body>
</html>
    """



@app.get("/chat-ui", response_class=HTMLResponse)
async def chat_ui() -> str:
    return r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Microwave AI</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #000;
      color: #e5e7eb;
      height: 100vh;
      display: flex;
      overflow: hidden;
    }

    /* ── sidebar ── */
    #sidebar {
      width: 220px;
      min-width: 220px;
      background: #111;
      display: flex;
      flex-direction: column;
      padding: 0.75rem 0.5rem;
      border-right: 1px solid #1f1f1f;
      gap: 0.25rem;
    }
    #newChatBtn {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.5rem 0.6rem;
      border-radius: 0.5rem;
      cursor: pointer;
      font-size: 0.85rem;
      color: #e5e7eb;
      background: transparent;
      border: none;
      width: 100%;
      text-align: left;
    }
    #newChatBtn:hover { background: #1f1f1f; }
    #newChatBtn svg { flex-shrink: 0; }
    .history-label {
      font-size: 0.68rem;
      color: #6b7280;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      padding: 0.5rem 0.6rem 0.2rem;
    }
    .history-item {
      padding: 0.4rem 0.6rem;
      border-radius: 0.5rem;
      font-size: 0.82rem;
      color: #9ca3af;
      cursor: pointer;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .history-item:hover { background: #1f1f1f; color: #e5e7eb; }
    .history-item.active { background: #1f1f1f; color: #e5e7eb; }

    /* ── main chat area ── */
    #chatArea {
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }

    /* ── message list ── */
    #messages {
      flex: 1;
      overflow-y: auto;
      padding: 2rem 0;
      display: flex;
      flex-direction: column;
      gap: 1.5rem;
    }
    #messages::-webkit-scrollbar { width: 4px; }
    #messages::-webkit-scrollbar-track { background: transparent; }
    #messages::-webkit-scrollbar-thumb { background: #2a2a2a; border-radius: 4px; }

    .msg-row {
      display: flex;
      flex-direction: column;
      padding: 0 10%;
    }
    .msg-row.user { align-items: flex-end; }
    .msg-row.bot  { align-items: flex-start; }

    .bubble {
      max-width: 70%;
      font-size: 0.9rem;
      line-height: 1.55;
    }
    .msg-row.user .bubble {
      background: #2a2a2a;
      border-radius: 1.1rem 1.1rem 0.25rem 1.1rem;
      padding: 0.6rem 0.9rem;
      color: #e5e7eb;
    }
    .msg-row.bot .bubble {
      background: transparent;
      color: #e5e7eb;
      white-space: pre-wrap;
    }
    .msg-actions {
      margin-top: 0.3rem;
      display: flex;
      gap: 0.35rem;
    }
    .action-btn {
      background: none;
      border: none;
      cursor: pointer;
      color: #6b7280;
      padding: 0.2rem;
      border-radius: 0.3rem;
      display: flex;
      align-items: center;
    }
    .action-btn:hover { color: #e5e7eb; background: #1f1f1f; }
    .action-btn svg { width: 14px; height: 14px; }

    /* ── empty state ── */
    #emptyState {
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      color: #6b7280;
      font-size: 0.95rem;
      padding-bottom: 4rem;
    }
    #emptyState .big { font-size: 2rem; }

    /* ── input area ── */
    #inputArea {
      padding: 0.75rem 10% 1.25rem;
    }
    #inputShell {
      background: #1a1a1a;
      border-radius: 1rem;
      padding: 0.6rem 0.6rem 0.6rem 1rem;
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      border: 1px solid #2a2a2a;
    }
    #promptInput {
      background: transparent;
      border: none;
      outline: none;
      color: #e5e7eb;
      font-size: 0.9rem;
      resize: none;
      min-height: 24px;
      max-height: 160px;
      overflow-y: auto;
      line-height: 1.5;
    }
    #promptInput::placeholder { color: #4b5563; }
    .input-footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .input-left { display: flex; gap: 0.4rem; align-items: center; }
    .icon-btn {
      background: none;
      border: none;
      cursor: pointer;
      color: #6b7280;
      padding: 0.25rem;
      border-radius: 0.4rem;
      display: flex;
      align-items: center;
    }
    .icon-btn:hover { color: #e5e7eb; background: #2a2a2a; }
    .model-select {
      background: #2a2a2a;
      border: none;
      color: #9ca3af;
      font-size: 0.75rem;
      padding: 0.2rem 0.5rem;
      border-radius: 0.4rem;
      cursor: pointer;
      outline: none;
    }
    .model-select:focus { outline: none; }
    #sendBtn {
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background: #e5e7eb;
      border: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }
    #sendBtn:disabled { opacity: 0.3; cursor: default; }
    #sendBtn svg { color: #000; }
  </style>
</head>
<body>

  <!-- Sidebar -->
  <div id="sidebar">
    <button id="newChatBtn">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12 5v14M5 12h14"/>
      </svg>
      New Chat
    </button>
    <div class="history-label">Today</div>
    <div id="historyList"></div>
  </div>

  <!-- Main -->
  <div id="chatArea">
    <div id="messages">
      <div id="emptyState">
        <div class="big">⚡</div>
        <div>Ask Microwave AI anything</div>
        <div style="font-size:0.75rem;color:#374151;">Running on your local network</div>
      </div>
    </div>

    <div id="inputArea">
      <div id="inputShell">
        <textarea id="promptInput" rows="1" placeholder="Send a message"></textarea>
        <div class="input-footer">
          <div class="input-left">
            <button class="icon-btn" title="Attach" disabled>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/>
              </svg>
            </button>
            <select id="modelSelect" class="model-select">
              <option value="llama3.2">llama3.2</option>
              <option value="llama3">llama3</option>
              <option value="phi3">phi3</option>
              <option value="deepseek-coder:6.7b">deepseek-coder</option>
            </select>
          </div>
          <button id="sendBtn" disabled>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
              <line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>
            </svg>
          </button>
        </div>
      </div>
    </div>
  </div>

<script>
  const messagesEl  = document.getElementById('messages');
  const emptyState  = document.getElementById('emptyState');
  const promptEl    = document.getElementById('promptInput');
  const sendBtn     = document.getElementById('sendBtn');
  const modelSelect = document.getElementById('modelSelect');
  const historyList = document.getElementById('historyList');

  let sessions = [];       // [{title, messages:[]}]
  let activeIdx = -1;

  function newSession() {
    const s = { title: null, messages: [] };
    sessions.unshift(s);
    activeIdx = 0;
    renderHistory();
    renderMessages();
  }

  function renderHistory() {
    historyList.innerHTML = '';
    sessions.forEach((s, i) => {
      const d = document.createElement('div');
      d.className = 'history-item' + (i === activeIdx ? ' active' : '');
      d.textContent = s.title || 'New conversation';
      d.onclick = () => { activeIdx = i; renderHistory(); renderMessages(); };
      historyList.appendChild(d);
    });
  }

  function renderMessages() {
    const s = sessions[activeIdx];
    if (!s || s.messages.length === 0) {
      messagesEl.innerHTML = '';
      messagesEl.appendChild(emptyState);
      return;
    }
    messagesEl.innerHTML = '';
    s.messages.forEach(m => appendBubble(m.role, m.text));
  }

  function appendBubble(role, text, streaming) {
    const row = document.createElement('div');
    row.className = 'msg-row ' + role;

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = text;
    row.appendChild(bubble);

    if (!streaming) {
      const actions = document.createElement('div');
      actions.className = 'msg-actions';
      const copyBtn = document.createElement('button');
      copyBtn.className = 'action-btn';
      copyBtn.title = 'Copy';
      copyBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
      </svg>`;
      copyBtn.onclick = () => navigator.clipboard.writeText(text).catch(() => {});
      actions.appendChild(copyBtn);
      row.appendChild(actions);
    }

    messagesEl.appendChild(row);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return bubble;
  }

  function updateSendBtn() {
    sendBtn.disabled = promptEl.value.trim().length === 0;
  }

  promptEl.addEventListener('input', () => {
    promptEl.style.height = 'auto';
    promptEl.style.height = Math.min(promptEl.scrollHeight, 160) + 'px';
    updateSendBtn();
  });

  promptEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSend(); }
  });

  sendBtn.addEventListener('click', doSend);

  async function doSend() {
    const prompt = promptEl.value.trim();
    if (!prompt || sendBtn.disabled) return;

    if (activeIdx === -1 || sessions.length === 0) newSession();

    const s = sessions[activeIdx];
    s.messages.push({ role: 'user', text: prompt });
    if (!s.title) {
      s.title = prompt.slice(0, 32) + (prompt.length > 32 ? '…' : '');
      renderHistory();
    }

    // remove empty state, add user bubble
    if (messagesEl.contains(emptyState)) messagesEl.removeChild(emptyState);
    appendBubble('user', prompt, false);

    promptEl.value = '';
    promptEl.style.height = 'auto';
    sendBtn.disabled = true;

    // streaming bot bubble
    const botBubble = appendBubble('bot', '', true);
    let fullText = '';

    try {
      const res = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, region: 'LAN', model: modelSelect.value }),
      });

      if (!res.ok || !res.body) {
        botBubble.textContent = 'Error: ' + res.status;
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      let done = false;

      while (!done) {
        const { value, done: d } = await reader.read();
        done = d;
        if (value) {
          buf += decoder.decode(value, { stream: !done });
          const lines = buf.split('\n');
          buf = lines.pop();
          for (const line of lines) {
            if (!line.trim()) continue;
            try {
              const obj = JSON.parse(line);
              if (typeof obj.response === 'string') {
                fullText += obj.response;
                botBubble.textContent = fullText;
                messagesEl.scrollTop = messagesEl.scrollHeight;
              }
            } catch (_) {
              fullText += line;
              botBubble.textContent = fullText;
            }
          }
        }
      }

      // add copy button after streaming done
      const row = botBubble.parentElement;
      const actions = document.createElement('div');
      actions.className = 'msg-actions';
      const copyBtn = document.createElement('button');
      copyBtn.className = 'action-btn';
      copyBtn.title = 'Copy';
      copyBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
      </svg>`;
      copyBtn.onclick = () => navigator.clipboard.writeText(fullText).catch(() => {});
      actions.appendChild(copyBtn);
      row.appendChild(actions);

      s.messages.push({ role: 'bot', text: fullText });
    } catch (e) {
      botBubble.textContent = 'Error: ' + (e && e.message ? e.message : 'unknown');
    } finally {
      sendBtn.disabled = false;
      promptEl.focus();
    }
  }

  document.getElementById('newChatBtn').addEventListener('click', () => {
    newSession();
    promptEl.focus();
  });

  // Start with a fresh session
  newSession();
  promptEl.focus();
</script>
</body>
</html>
    """



def choose_node(region: Optional[str]) -> Optional[NodeInfo]:
    """Round-robin selection, preferring nodes in requested region."""
    global _rr_index
    if not nodes:
        return None

    if region:
        candidates = [n for n in nodes if n.region == region]
        if not candidates:
            candidates = list(nodes)
    else:
        candidates = list(nodes)

    if not candidates:
        return None

    idx = _rr_index % len(candidates)
    _rr_index += 1
    return candidates[idx]


@app.post("/chat")
async def chat(request: Request) -> StreamingResponse:
    payload = await request.json()
    prompt: str = payload.get("prompt", "")
    region: Optional[str] = payload.get("region")
    model: Optional[str] = payload.get("model")

    node = choose_node(region)
    if not node:
        raise HTTPException(status_code=503, detail="No nodes are currently registered")

    infer_payload: Dict[str, Any] = {"prompt": prompt}
    if model:
        infer_payload["model"] = model

    async def stream_from_node():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", f"{node.base_url}/infer", json=infer_payload
            ) as resp:
                if resp.status_code != 200:
                    text = await resp.aread()
                    yield text
                    return
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk

    return StreamingResponse(stream_from_node(), media_type="application/octet-stream")


@app.post("/nodes/health")
async def health_check_nodes() -> JSONResponse:
    async with httpx.AsyncClient() as client:
        for node in nodes:
            start = time.perf_counter()
            try:
                resp = await client.get(f"{node.base_url}/health", timeout=2.0)
                if resp.status_code == 200:
                    node.last_heartbeat = time.time()
                    node.last_latency_ms = (time.perf_counter() - start) * 1000.0
                else:
                    node.last_latency_ms = -1.0
            except Exception:
                node.last_latency_ms = -1.0

    return JSONResponse({"status": "ok", "count": len(nodes)})


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(description="Microwave AI Gateway")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

