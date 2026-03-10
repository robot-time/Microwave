import argparse
import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
import uvicorn


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
    # Dedicated chat experience UI
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Microwave AI – Chat</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      color: #e5e7eb;
      background: radial-gradient(circle at top, #1e293b 0, #020617 45%, #000000 100%);
      display: flex;
      justify-content: center;
      align-items: stretch;
    }
    .shell {
      width: 100%;
      max-width: 960px;
      margin: 1.5rem;
      display: flex;
      flex-direction: column;
      border-radius: 1.25rem;
      border: 1px solid rgba(148,163,184,0.25);
      background: radial-gradient(circle at top left, rgba(59,130,246,0.3), transparent 40%), #020617;
      box-shadow: 0 25px 60px rgba(15,23,42,0.9);
      overflow: hidden;
    }
    header {
      padding: 1rem 1.5rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid rgba(15,23,42,0.9);
      background: linear-gradient(to right, rgba(15,23,42,0.95), rgba(15,23,42,0.7));
    }
    header h1 {
      margin: 0;
      font-size: 1rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #9ca3af;
    }
    header .brand {
      font-weight: 600;
      font-size: 1.05rem;
      color: #e5e7eb;
    }
    header .pill {
      border-radius: 999px;
      border: 1px solid #1f2937;
      padding: 0.15rem 0.5rem;
      font-size: 0.7rem;
      color: #9ca3af;
      display: inline-flex;
      align-items: center;
      gap: 0.25rem;
    }
    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: #22c55e;
    }
    main {
      flex: 1;
      display: flex;
      flex-direction: column;
      padding: 1.25rem 1.5rem 1rem;
      gap: 0.75rem;
    }
    .subtitle {
      font-size: 0.8rem;
      color: #9ca3af;
    }
    .conversation {
      flex: 1;
      border-radius: 0.9rem;
      border: 1px solid #1f2937;
      background: rgba(15,23,42,0.85);
      padding: 0.9rem;
      overflow-y: auto;
      font-size: 0.85rem;
    }
    .message {
      margin-bottom: 0.75rem;
      max-width: 82%;
      padding: 0.55rem 0.75rem;
      border-radius: 0.9rem;
      line-height: 1.4;
      white-space: pre-wrap;
    }
    .message.user {
      margin-left: auto;
      background: linear-gradient(to right, #1f2937, #111827);
      border-bottom-right-radius: 0.2rem;
    }
    .message.bot {
      margin-right: auto;
      background: linear-gradient(to right, #0f172a, #020617);
      border-bottom-left-radius: 0.2rem;
      border: 1px solid rgba(59,130,246,0.4);
    }
    .avatar {
      font-size: 0.7rem;
      font-weight: 500;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 0.2rem;
      color: #9ca3af;
    }
    .input-shell {
      margin-top: 0.25rem;
      border-radius: 999px;
      border: 1px solid rgba(148,163,184,0.4);
      background: rgba(15,23,42,0.95);
      display: flex;
      align-items: center;
      padding: 0.3rem 0.4rem 0.3rem 0.8rem;
      gap: 0.5rem;
    }
    #promptInput {
      flex: 1;
      border: none;
      outline: none;
      background: transparent;
      color: #e5e7eb;
      font-size: 0.9rem;
      padding: 0.45rem 0;
    }
    #promptInput::placeholder {
      color: #6b7280;
    }
    .send-btn {
      border-radius: 999px;
      border: none;
      background: linear-gradient(to right, #2563eb, #4f46e5);
      color: white;
      padding: 0.45rem 0.9rem;
      font-size: 0.8rem;
      font-weight: 500;
      display: inline-flex;
      align-items: center;
      gap: 0.25rem;
      cursor: pointer;
      box-shadow: 0 10px 25px rgba(37,99,235,0.45);
    }
    .send-btn:disabled {
      opacity: 0.5;
      cursor: default;
      box-shadow: none;
    }
    .send-icon {
      display: inline-block;
      border-radius: 999px;
      width: 16px;
      height: 16px;
      background: rgba(15,23,42,0.9);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 0.7rem;
    }
    .footer {
      margin-top: 0.35rem;
      font-size: 0.7rem;
      color: #6b7280;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .chip-row {
      display: flex;
      gap: 0.35rem;
      flex-wrap: wrap;
    }
    .chip {
      padding: 0.1rem 0.55rem;
      border-radius: 999px;
      border: 1px solid #1f2937;
      font-size: 0.7rem;
      color: #9ca3af;
      cursor: pointer;
    }
    @media (max-width: 640px) {
      .shell {
        margin: 0.75rem;
      }
      header {
        padding-inline: 1rem;
      }
      main {
        padding-inline: 1rem;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div>
        <div class="brand">Microwave AI</div>
        <h1>LAN chat · Phase 0</h1>
      </div>
      <div class="pill">
        <span class="status-dot"></span>
        Gateway online
      </div>
    </header>
    <main>
      <div>
        <div class="subtitle">Ask Microwave AI anything. Responses are served from your LAN node.</div>
      </div>
      <div id="conversation" class="conversation">
        <div class="message bot">
          <div class="avatar">Microwave AI</div>
          <div>Hey there – I’m running on your local network. What do you want to build?</div>
        </div>
      </div>
      <div class="input-shell">
        <input id="promptInput" placeholder="Ask Microwave AI to help you design or build something…" />
        <button id="sendBtn" class="send-btn" type="button">
          <span>Send</span>
          <span class="send-icon">↑</span>
        </button>
      </div>
      <div class="footer">
        <div class="chip-row">
          <div class="chip" onclick="usePreset('Design a simple landing page for my app.')">Landing page</div>
          <div class="chip" onclick="usePreset('Explain how this distributed AI network works in plain language.')">Explain Microwave AI</div>
          <div class="chip" onclick="usePreset('Help me design the next phase of Microwave AI.')">Next phase</div>
        </div>
        <div id="status" class="subtitle">Press Enter to send · Shift+Enter for newline</div>
      </div>
    </main>
  </div>
  <script>
    const conversationEl = document.getElementById('conversation');
    const promptEl = document.getElementById('promptInput');
    const sendBtn = document.getElementById('sendBtn');
    const statusEl = document.getElementById('status');

    function appendMessage(text, role) {
      const msg = document.createElement('div');
      msg.className = 'message ' + role;
      const avatar = document.createElement('div');
      avatar.className = 'avatar';
      avatar.textContent = role === 'user' ? 'You' : 'Microwave AI';
      const body = document.createElement('div');
      body.textContent = text;
      msg.appendChild(avatar);
      msg.appendChild(body);
      conversationEl.appendChild(msg);
      conversationEl.scrollTop = conversationEl.scrollHeight;
      return body;
    }

    async function sendChat() {
      const prompt = promptEl.value.trim();
      if (!prompt || sendBtn.disabled) return;

      const userBody = appendMessage(prompt, 'user');
      const botBody = appendMessage('', 'bot');

      promptEl.value = '';
      statusEl.textContent = 'Sending…';
      sendBtn.disabled = true;

      try {
        const res = await fetch('/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt, region: 'LAN', model: 'llama3.2' }),
        });
        if (!res.ok || !res.body) {
          statusEl.textContent = 'Error: ' + res.status;
          sendBtn.disabled = false;
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
            const lines = buffer.split('\\n');
            buffer = lines.pop();
            for (const line of lines) {
              if (!line.trim()) continue;
              try {
                const obj = JSON.parse(line);
                if (typeof obj.response === 'string') {
                  fullText += obj.response;
                  botBody.textContent = fullText;
                  conversationEl.scrollTop = conversationEl.scrollHeight;
                }
              } catch (e) {
                fullText += line;
                botBody.textContent = fullText;
                conversationEl.scrollTop = conversationEl.scrollHeight;
              }
            }
          }
        }
        statusEl.textContent = 'Done.';
      } catch (e) {
        statusEl.textContent = 'Error: ' + (e && e.message ? e.message : 'unknown');
      } finally {
        sendBtn.disabled = false;
      }
    }

    function usePreset(text) {
      promptEl.value = text;
      promptEl.focus();
    }

    sendBtn.addEventListener('click', sendChat);
    promptEl.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChat();
      }
    });
  </script>
</body>
</html>
    """

def choose_node(region: Optional[str]) -> Optional[NodeInfo]:
    if not nodes:
        return None
    # Simple policy:
    # - if region is provided, prefer first node matching region
    # - else, round-robin over all nodes
    if region:
        for n in nodes:
            if n.region == region:
                return n
    # Fallback: rotate deque for naive round-robin
    nodes.rotate(-1)
    return nodes[0]


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
                    # surface basic error to client
                    text = await resp.aread()
                    yield text
                    return
                # First yield a small JSON header with routing info
                header = {
                    "route": {
                        "node_id": node.node_id,
                        "host": node.host,
                        "port": node.port,
                        "region": node.region,
                        "model": model or (node.models[0] if node.models else None),
                    }
                }
                yield (httpx.Request("GET", "/").content or b"")  # no-op to satisfy type checkers
                yield (('{"route": ' + str(header["route"]).replace("'", '"') + '}\\n').encode("utf-8"))
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    yield chunk

    return StreamingResponse(stream_from_node(), media_type="application/octet-stream")


@app.post("/nodes/health")
async def health_check_nodes() -> JSONResponse:
    """Ping all nodes' /health endpoints and record latency."""
    import time

    async with httpx.AsyncClient() as client:
        for node in nodes:
            start = time.perf_counter()
            try:
                resp = await client.get(f"{node.base_url}/health", timeout=2.0)
                if resp.status_code == 200:
                    node.last_heartbeat = time.time()
                    node.last_latency_ms = (time.perf_counter() - start) * 1000.0
            except Exception:
                node.last_latency_ms = -1.0

    return JSONResponse({"status": "ok", "count": len(nodes)})


def main() -> None:
    parser = argparse.ArgumentParser(description="Microwave AI Gateway")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

