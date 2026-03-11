import argparse
import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
import uvicorn

from . import __version__


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
    print(f"Microwave Network (gateway) v{__version__}")


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
    is_ws: bool = False

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


app = FastAPI(title="Microwave AI Gateway")
nodes: Deque[NodeInfo] = deque()
_rr_index = 0
HEALTH_INTERVAL_SECONDS = 10
_health_task = None

# WebSocket reverse-connected nodes
_ws_connections: Dict[str, WebSocket] = {}
_ws_locks: Dict[str, asyncio.Lock] = {}
_task_queues: Dict[str, asyncio.Queue] = {}


async def _periodic_health_check() -> None:
    """Background loop: ping every registered node every HEALTH_INTERVAL_SECONDS."""
    while True:
        await asyncio.sleep(HEALTH_INTERVAL_SECONDS)
        if not nodes:
            continue
        async with httpx.AsyncClient() as client:
            for node in list(nodes):
                if node.is_ws:
                    ws = _ws_connections.get(node.node_id)
                    lock = _ws_locks.get(node.node_id)
                    if ws and lock:
                        try:
                            start = time.perf_counter()
                            async with lock:
                                await ws.send_json({"type": "ping"})
                            node.last_heartbeat = time.time()
                            node.last_latency_ms = (time.perf_counter() - start) * 1000.0
                        except Exception:
                            node.last_latency_ms = -1.0
                    else:
                        node.last_latency_ms = -1.0
                else:
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


def _upsert_node(node_id: str, host: str, port: int, region: str,
                 models: List[str], metadata: Dict[str, Any],
                 is_ws: bool = False) -> NodeInfo:
    global nodes
    nodes = deque(n for n in nodes if n.node_id != node_id)
    info = NodeInfo(
        node_id=node_id, host=host, port=port, region=region,
        models=models, metadata=metadata,
        last_heartbeat=time.time(), last_latency_ms=0.0, is_ws=is_ws,
    )
    nodes.append(info)
    return info


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

    _upsert_node(node_id, host, int(port), region, models, metadata)
    return JSONResponse({"status": "ok"})


@app.websocket("/nodes/ws")
async def node_websocket(ws: WebSocket) -> None:
    """Reverse-connection endpoint: nodes connect here instead of listening."""
    global nodes
    await ws.accept()
    node_id: Optional[str] = None
    try:
        reg = await ws.receive_json()
        if reg.get("type") != "register":
            await ws.close(code=1008)
            return

        node_id = reg.get("node_id", f"ws-{uuid.uuid4().hex[:8]}")
        region = reg.get("region", "LAN")
        models = reg.get("models", [])
        metadata = reg.get("metadata", {})

        _upsert_node(node_id, "ws-connected", 0, region, models, metadata, is_ws=True)
        _ws_connections[node_id] = ws
        _ws_locks[node_id] = asyncio.Lock()
        print(f"[ws] Node connected: {node_id} (region={region}, models={models})")
        await ws.send_json({"type": "registered", "node_id": node_id})

        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type")
            if msg_type == "chunk":
                task_id = msg.get("task_id")
                q = _task_queues.get(task_id)
                if q:
                    await q.put(msg.get("data", ""))
            elif msg_type == "done":
                task_id = msg.get("task_id")
                q = _task_queues.get(task_id)
                if q:
                    await q.put(None)
            elif msg_type == "pong":
                for n in nodes:
                    if n.node_id == node_id:
                        n.last_heartbeat = time.time()
                        break

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if node_id:
            _ws_connections.pop(node_id, None)
            _ws_locks.pop(node_id, None)
            nodes = deque(n for n in nodes if n.node_id != node_id)
            print(f"[ws] Node disconnected: {node_id}")


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
            "connection": "ws" if n.is_ws else "http",
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
        if (buffer.trim()) {
          try {
            const obj = JSON.parse(buffer);
            if (typeof obj.response === 'string') {
              fullText += obj.response;
              botTextEl.textContent = fullText;
            }
          } catch (e) {
            fullText += buffer;
            botTextEl.textContent = fullText;
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
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Microwave AI Chat</title>
  <style>
    :root {
      --bg-0: #0a0b0f;
      --bg-1: #131520;
      --bg-2: #181b28;
      --bg-3: #222738;
      --panel: #161a26;
      --text-0: #f8fafc;
      --text-1: #d1d5db;
      --text-2: #9ca3af;
      --text-3: #6b7280;
      --line: #2b3145;
      --accent: #f97316;
      --accent-soft: #fb923c;
      --accent-glow: rgba(249, 115, 22, 0.35);
      --good: #34d399;
      --bad: #fb7185;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background:
        radial-gradient(circle at 20% -20%, rgba(249, 115, 22, 0.25), transparent 40%),
        radial-gradient(circle at 90% 20%, rgba(56, 189, 248, 0.12), transparent 35%),
        var(--bg-0);
      color: var(--text-0);
      height: 100vh;
      display: flex;
      overflow: hidden;
    }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }

    #sidebar {
      width: 250px;
      min-width: 250px;
      background: linear-gradient(180deg, #10131d, #0d1018);
      display: flex;
      flex-direction: column;
      padding: 1rem 0.65rem 0.65rem;
      border-right: 1px solid var(--line);
      gap: 0.45rem;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 0.55rem;
      padding: 0 0.45rem 0.35rem;
    }
    .brand-icon {
      width: 30px;
      height: 30px;
      border-radius: 9px;
      border: 1px solid rgba(251, 146, 60, 0.6);
      background: linear-gradient(160deg, #2f364d, #171b2a);
      display: grid;
      place-items: center;
      box-shadow: 0 0 16px rgba(251, 146, 60, 0.22);
      font-size: 0.95rem;
    }
    .brand h1 { font-size: 0.9rem; line-height: 1.1; }
    .brand p { font-size: 0.72rem; color: var(--text-2); line-height: 1.2; }

    #newChatBtn {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.62rem 0.7rem;
      border-radius: 0.72rem;
      cursor: pointer;
      font-size: 0.84rem;
      color: var(--text-0);
      background: linear-gradient(135deg, rgba(249, 115, 22, 0.18), rgba(249, 115, 22, 0.08));
      border: 1px solid rgba(251, 146, 60, 0.3);
      width: 100%;
      text-align: left;
    }
    #newChatBtn:hover {
      border-color: rgba(251, 146, 60, 0.6);
      box-shadow: 0 0 0 3px rgba(249, 115, 22, 0.12);
    }
    #newChatBtn svg { flex-shrink: 0; }

    .history-label {
      font-size: 0.66rem;
      color: var(--text-3);
      text-transform: uppercase;
      letter-spacing: 0.09em;
      padding: 0.58rem 0.6rem 0.12rem;
    }
    .history-item {
      padding: 0.5rem 0.62rem;
      border-radius: 0.65rem;
      font-size: 0.82rem;
      color: var(--text-2);
      cursor: pointer;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      border: 1px solid transparent;
    }
    .history-item:hover {
      background: #171b27;
      color: var(--text-0);
      border-color: rgba(148, 163, 184, 0.2);
    }
    .history-item.active {
      background: #202638;
      color: var(--text-0);
      border-color: rgba(251, 146, 60, 0.4);
    }
    #historyList {
      overflow-y: auto;
      padding-bottom: 0.3rem;
    }

    #chatArea {
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    #topBar {
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(24, 27, 40, 0.94), rgba(24, 27, 40, 0.68));
      padding: 0.7rem 1.2rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.8rem;
    }
    .top-title {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.86rem;
      color: var(--text-1);
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      display: inline-block;
      box-shadow: 0 0 10px rgba(52, 211, 153, 0.45);
      background: var(--good);
    }
    .pill-row { display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap; }
    .pill {
      border: 1px solid var(--line);
      background: rgba(19, 21, 32, 0.9);
      color: var(--text-2);
      border-radius: 999px;
      padding: 0.2rem 0.52rem;
      font-size: 0.71rem;
    }
    #activeModelTag { color: #fed7aa; border-color: rgba(251, 146, 60, 0.35); }

    #messages {
      flex: 1;
      overflow-y: auto;
      padding: 1.1rem 0 1.6rem;
      display: flex;
      flex-direction: column;
      gap: 1.1rem;
    }
    #messages::-webkit-scrollbar { width: 6px; }
    #messages::-webkit-scrollbar-track { background: transparent; }
    #messages::-webkit-scrollbar-thumb { background: #2d3447; border-radius: 6px; }

    .msg-row {
      display: flex;
      flex-direction: column;
      padding: 0 9%;
    }
    .msg-row.user { align-items: flex-end; }
    .msg-row.bot  { align-items: flex-start; }

    .meta {
      margin-bottom: 0.28rem;
      font-size: 0.68rem;
      color: var(--text-3);
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }
    .meta .who { color: var(--text-2); }

    .bubble {
      max-width: min(780px, 78%);
      font-size: 0.92rem;
      line-height: 1.58;
    }
    .msg-row.user .bubble {
      background: linear-gradient(165deg, #343b53, #282d42);
      border: 1px solid #3a4462;
      border-radius: 1rem 1rem 0.22rem 1rem;
      padding: 0.62rem 0.9rem;
      color: #f8fafc;
    }
    .msg-row.bot .bubble {
      background: linear-gradient(180deg, rgba(24, 28, 42, 0.72), rgba(24, 28, 42, 0.36));
      border: 1px solid rgba(148, 163, 184, 0.2);
      border-radius: 0.9rem 0.9rem 0.9rem 0.24rem;
      padding: 0.58rem 0.82rem;
      color: #f1f5f9;
      white-space: pre-wrap;
    }

    .bubble.loading {
      border-color: rgba(251, 146, 60, 0.4);
      background: linear-gradient(180deg, rgba(36, 29, 27, 0.95), rgba(34, 29, 29, 0.52));
      color: #fdba74;
    }
    .heating-wrap {
      display: flex;
      align-items: center;
      gap: 0.7rem;
      min-width: 230px;
    }
    .microwave-loader {
      width: 26px;
      height: 26px;
      border-radius: 8px;
      border: 1px solid rgba(251, 146, 60, 0.7);
      display: grid;
      place-items: center;
      position: relative;
      background: rgba(18, 18, 24, 0.88);
      color: #fb923c;
    }
    .microwave-loader::before,
    .microwave-loader::after {
      content: "";
      position: absolute;
      inset: -3px;
      border-radius: 10px;
      border: 1px solid rgba(251, 146, 60, 0.45);
      opacity: 0;
      animation: ping 1.5s ease-out infinite;
    }
    .microwave-loader::after { animation-delay: 0.45s; }
    .heating-copy { display: flex; flex-direction: column; gap: 0.15rem; }
    .heating-title { font-size: 0.8rem; color: #fed7aa; }
    .heating-sub { font-size: 0.72rem; color: #cbd5e1; opacity: 0.86; }

    .msg-actions {
      margin-top: 0.3rem;
      display: flex;
      gap: 0.35rem;
    }
    .action-btn {
      background: none;
      border: 1px solid transparent;
      cursor: pointer;
      color: var(--text-3);
      padding: 0.24rem;
      border-radius: 0.44rem;
      display: flex;
      align-items: center;
    }
    .action-btn:hover {
      color: #f8fafc;
      background: #1d2232;
      border-color: #30374d;
    }
    .action-btn svg { width: 14px; height: 14px; }

    #emptyState {
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 0.62rem;
      color: var(--text-2);
      font-size: 0.95rem;
      padding: 0.8rem 1rem 4rem;
      text-align: center;
    }
    #emptyState .big {
      font-size: 2.05rem;
      filter: drop-shadow(0 0 20px rgba(249, 115, 22, 0.28));
    }
    #suggestions {
      margin-top: 0.8rem;
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      justify-content: center;
      max-width: 700px;
    }
    .suggestion {
      border: 1px solid #38405b;
      background: #1b2030;
      color: #cbd5e1;
      border-radius: 999px;
      font-size: 0.78rem;
      padding: 0.38rem 0.72rem;
      cursor: pointer;
    }
    .suggestion:hover {
      border-color: rgba(251, 146, 60, 0.6);
      color: #fff7ed;
      box-shadow: 0 0 0 3px rgba(249, 115, 22, 0.12);
    }

    #inputArea {
      padding: 0.65rem 9% 1.1rem;
    }
    #inputShell {
      background: linear-gradient(180deg, rgba(24, 28, 41, 0.95), rgba(20, 23, 34, 0.95));
      border-radius: 1.05rem;
      padding: 0.68rem 0.68rem 0.62rem 0.95rem;
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      border: 1px solid #353f5c;
      box-shadow: 0 15px 40px rgba(0, 0, 0, 0.25);
    }
    #promptInput {
      background: transparent;
      border: none;
      outline: none;
      color: #f8fafc;
      font-size: 0.92rem;
      resize: none;
      min-height: 24px;
      max-height: 160px;
      overflow-y: auto;
      line-height: 1.5;
    }
    #promptInput::placeholder { color: #76809c; }
    .input-footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .input-left { display: flex; gap: 0.4rem; align-items: center; }
    .model-select {
      background: #23293b;
      border: 1px solid #36405a;
      color: #d1d5db;
      font-size: 0.75rem;
      padding: 0.27rem 0.55rem;
      border-radius: 0.44rem;
      cursor: pointer;
      outline: none;
    }
    .model-select:hover { border-color: #4a5675; }
    .model-select:focus { border-color: #7c8ab1; }
    #statusText {
      color: var(--text-3);
      font-size: 0.72rem;
      min-height: 1.1em;
    }
    #sendBtn {
      min-width: 36px;
      height: 36px;
      padding: 0 0.66rem;
      border-radius: 50%;
      background: linear-gradient(170deg, #fb923c, #f97316);
      border: 1px solid rgba(254, 215, 170, 0.45);
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      box-shadow: 0 0 18px var(--accent-glow);
      color: #1f130f;
    }
    #sendBtn:disabled {
      opacity: 0.45;
      cursor: default;
      box-shadow: none;
      filter: grayscale(0.35);
    }
    #sendBtn svg { color: #1a0f0a; }
    #sendBtn.sending {
      animation: warmPulse 1.3s ease-in-out infinite;
    }

    .error-pill {
      color: #fecdd3;
      background: rgba(127, 29, 29, 0.35);
      border: 1px solid rgba(251, 113, 133, 0.45);
      border-radius: 999px;
      padding: 0.15rem 0.5rem;
      font-size: 0.71rem;
    }

    @keyframes ping {
      0% { transform: scale(0.95); opacity: 0.65; }
      70% { transform: scale(1.35); opacity: 0; }
      100% { transform: scale(1.35); opacity: 0; }
    }
    @keyframes warmPulse {
      0%, 100% { box-shadow: 0 0 12px rgba(249, 115, 22, 0.28); }
      50% { box-shadow: 0 0 24px rgba(249, 115, 22, 0.5); }
    }

    @media (max-width: 920px) {
      #sidebar { display: none; }
      .msg-row { padding: 0 5%; }
      #inputArea { padding: 0.65rem 5% 1rem; }
      .bubble { max-width: 92%; }
      #topBar { padding: 0.65rem 0.8rem; }
    }
  </style>
</head>
<body>
  <div id="sidebar">
    <div class="brand">
      <div class="brand-icon">▣</div>
      <div>
        <h1>Microwave AI</h1>
        <p>Heat up distributed inference</p>
      </div>
    </div>
    <button id="newChatBtn">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12 5v14M5 12h14"/>
      </svg>
      New Chat Session
    </button>
    <div class="history-label">Today</div>
    <div id="historyList"></div>
  </div>

  <div id="chatArea">
    <div id="topBar">
      <div class="top-title">
        <span class="dot"></span>
        <span>Microwave chat ready</span>
      </div>
      <div class="pill-row">
        <span class="pill mono">POST /chat</span>
        <span class="pill mono">region LAN</span>
        <span class="pill mono" id="activeModelTag">model llama3.2</span>
      </div>
    </div>

    <div id="messages">
      <div id="emptyState">
        <div class="big">📡</div>
        <div>What should we heat up?</div>
        <div style="font-size:0.78rem;color:#73809f;">Microwave routes your request to a live node and streams tokens back in real-time.</div>
        <div id="suggestions">
          <button class="suggestion">Explain how Microwave AI routing works</button>
          <button class="suggestion">Write a healthy microwave mug cake recipe</button>
          <button class="suggestion">Summarize this project in 5 bullets</button>
          <button class="suggestion">Generate Python code for a websocket client</button>
        </div>
      </div>
    </div>

    <div id="inputArea">
      <div id="inputShell">
        <textarea id="promptInput" rows="1" placeholder="Ask Microwave AI anything..."></textarea>
        <div class="input-footer">
          <div class="input-left">
            <select id="modelSelect" class="model-select">
              <option value="llama3.2">llama3.2</option>
              <option value="llama3">llama3</option>
              <option value="phi3">phi3</option>
              <option value="deepseek-coder:6.7b">deepseek-coder</option>
            </select>
            <span id="statusText"></span>
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
  const messagesEl = document.getElementById('messages');
  const emptyState = document.getElementById('emptyState');
  const promptEl = document.getElementById('promptInput');
  const sendBtn = document.getElementById('sendBtn');
  const modelSelect = document.getElementById('modelSelect');
  const historyList = document.getElementById('historyList');
  const statusText = document.getElementById('statusText');
  const activeModelTag = document.getElementById('activeModelTag');
  const suggestionButtons = Array.from(document.querySelectorAll('.suggestion'));

  let sessions = []; // [{title, messages:[{role,text,model,time}]}]
  let activeIdx = -1;
  let isSending = false;

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
      d.onclick = () => {
        activeIdx = i;
        renderHistory();
        renderMessages();
      };
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
    s.messages.forEach(m => appendBubble(m.role, m.text, { model: m.model, time: m.time }));
  }

  function nowStamp() {
    const d = new Date();
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function escapeHtml(text) {
    return (text || '').replace(/[&<>"']/g, ch => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    }[ch]));
  }

  function appendBubble(role, text, opts = {}) {
    const row = document.createElement('div');
    row.className = 'msg-row ' + role;

    const meta = document.createElement('div');
    meta.className = 'meta';
    const who = role === 'user' ? 'You' : 'Microwave AI';
    const model = opts.model ? ' · ' + opts.model : '';
    const time = opts.time || nowStamp();
    meta.innerHTML = `<span class="who">${who}${model}</span><span>${time}</span>`;
    row.appendChild(meta);

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = text;
    row.appendChild(bubble);

    if (!opts.streaming) {
      const actions = document.createElement('div');
      actions.className = 'msg-actions';
      const copyBtn = document.createElement('button');
      copyBtn.className = 'action-btn';
      copyBtn.title = 'Copy';
      copyBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
      </svg>`;
      copyBtn.onclick = () => navigator.clipboard.writeText(text || '').catch(() => {});
      actions.appendChild(copyBtn);
      row.appendChild(actions);
    }

    messagesEl.appendChild(row);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return { row, bubble };
  }

  function createLoadingBubble() {
    const { row, bubble } = appendBubble('bot', '', { streaming: true, model: modelSelect.value });
    bubble.classList.add('loading');
    bubble.innerHTML = `
      <div class="heating-wrap">
        <div class="microwave-loader">~</div>
        <div class="heating-copy">
          <div class="heating-title">Heating response...</div>
          <div class="heating-sub">Microwave is routing and streaming</div>
        </div>
      </div>
    `;
    return { row, bubble };
  }

  function updateSendBtn() {
    sendBtn.disabled = isSending || promptEl.value.trim().length === 0;
  }

  function setStatus(text, isError) {
    if (!text) {
      statusText.textContent = '';
      statusText.className = '';
      return;
    }
    statusText.textContent = text;
    statusText.className = isError ? 'error-pill' : '';
  }

  promptEl.addEventListener('input', () => {
    promptEl.style.height = 'auto';
    promptEl.style.height = Math.min(promptEl.scrollHeight, 160) + 'px';
    updateSendBtn();
  });

  promptEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      doSend();
    }
  });

  sendBtn.addEventListener('click', doSend);
  modelSelect.addEventListener('change', () => {
    activeModelTag.textContent = 'model ' + modelSelect.value;
  });

  suggestionButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      promptEl.value = btn.textContent;
      promptEl.dispatchEvent(new Event('input'));
      promptEl.focus();
    });
  });

  async function doSend() {
    const prompt = promptEl.value.trim();
    if (!prompt || sendBtn.disabled || isSending) return;

    if (activeIdx === -1 || sessions.length === 0) newSession();

    const s = sessions[activeIdx];
    const sentAt = nowStamp();
    s.messages.push({ role: 'user', text: prompt, model: null, time: sentAt });
    if (!s.title) {
      s.title = prompt.slice(0, 32) + (prompt.length > 32 ? '…' : '');
      renderHistory();
    }

    if (messagesEl.contains(emptyState)) messagesEl.removeChild(emptyState);
    appendBubble('user', prompt, { time: sentAt });

    promptEl.value = '';
    promptEl.style.height = 'auto';
    isSending = true;
    sendBtn.classList.add('sending');
    setStatus('Heating...');
    updateSendBtn();

    const loading = createLoadingBubble();
    const botBubble = loading.bubble;
    const botRow = loading.row;
    let fullText = '';
    let convertedFromLoading = false;

    try {
      const res = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, region: 'LAN', model: modelSelect.value }),
      });

      if (!res.ok || !res.body) {
        const detail = res.status === 503
          ? 'No active nodes. Start or connect a node first.'
          : 'Request failed with status ' + res.status;
        botBubble.classList.remove('loading');
        botBubble.textContent = detail;
        setStatus(detail, true);
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
                if (!convertedFromLoading) {
                  convertedFromLoading = true;
                  botBubble.classList.remove('loading');
                  botBubble.textContent = '';
                }
                fullText += obj.response;
                botBubble.textContent = fullText;
                messagesEl.scrollTop = messagesEl.scrollHeight;
              }
            } catch (_) {
              if (!convertedFromLoading) {
                convertedFromLoading = true;
                botBubble.classList.remove('loading');
                botBubble.textContent = '';
              }
              fullText += line;
              botBubble.textContent = fullText;
            }
          }
        }
      }

      if (!convertedFromLoading && !fullText) {
        botBubble.classList.remove('loading');
        botBubble.textContent = 'No response received from node.';
      }

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
      botRow.appendChild(actions);

      s.messages.push({
        role: 'bot',
        text: fullText || botBubble.textContent,
        model: modelSelect.value,
        time: nowStamp()
      });
      setStatus('Served by Microwave network');
    } catch (e) {
      botBubble.classList.remove('loading');
      botBubble.textContent = 'Error: ' + (e && e.message ? e.message : 'unknown');
      setStatus('Stream error. Please retry.', true);
    } finally {
      isSending = false;
      sendBtn.classList.remove('sending');
      updateSendBtn();
      promptEl.focus();
    }
  }

  document.getElementById('newChatBtn').addEventListener('click', () => {
    newSession();
    promptEl.focus();
  });

  // Start with a fresh session
  newSession();
  activeModelTag.textContent = 'model ' + modelSelect.value;
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

    if node.is_ws and node.node_id in _ws_connections:
        return await _chat_via_ws(node, prompt, model)
    else:
        return _chat_via_http(node, prompt, model)


def _chat_via_http(node: NodeInfo, prompt: str, model: Optional[str]) -> StreamingResponse:
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


async def _chat_via_ws(node: NodeInfo, prompt: str, model: Optional[str]) -> StreamingResponse:
    ws = _ws_connections.get(node.node_id)
    lock = _ws_locks.get(node.node_id)
    if not ws or not lock:
        raise HTTPException(status_code=503, detail="Node disconnected")

    task_id = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    _task_queues[task_id] = queue

    try:
        async with lock:
            await ws.send_json({
                "type": "task",
                "task_id": task_id,
                "prompt": prompt,
                "model": model or "",
            })
    except Exception:
        _task_queues.pop(task_id, None)
        raise HTTPException(status_code=503, detail="Failed to reach node")

    async def stream_from_ws():
        try:
            while True:
                chunk = await asyncio.wait_for(queue.get(), timeout=120.0)
                if chunk is None:
                    break
                if isinstance(chunk, str):
                    yield chunk.encode("utf-8")
                else:
                    yield chunk
        except asyncio.TimeoutError:
            yield b'{"error":"node timeout"}'
        finally:
            _task_queues.pop(task_id, None)

    return StreamingResponse(stream_from_ws(), media_type="application/octet-stream")


@app.post("/nodes/health")
async def health_check_nodes() -> JSONResponse:
    async with httpx.AsyncClient() as client:
        for node in list(nodes):
            if node.is_ws:
                ws = _ws_connections.get(node.node_id)
                lock = _ws_locks.get(node.node_id)
                if ws and lock:
                    try:
                        start = time.perf_counter()
                        async with lock:
                            await ws.send_json({"type": "ping"})
                        node.last_heartbeat = time.time()
                        node.last_latency_ms = (time.perf_counter() - start) * 1000.0
                    except Exception:
                        node.last_latency_ms = -1.0
                else:
                    node.last_latency_ms = -1.0
            else:
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

