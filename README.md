     ________________
    |.-----------.   |
    ||   _____   |ooo|
    ||  |     |  |ooo|
    ||  |     |  | = |
    ||  '-----'  | _ |
    ||___________|[_]|
    '----------------'

Microwave AI
============

> Decentralized AI inference – **BitTorrent for AI**.

Anyone can run a node. Nodes contribute compute. Requests get routed to nearby, reliable machines. The network grows from a couple of friends on a LAN to a global mesh of volunteer GPUs.

---

## Quickstart

**Requirements:** [Python 3.10+](https://python.org) and [Ollama](https://ollama.com).

```bash
git clone https://github.com/robot-time/Microwave.git
cd Microwave
bash setup.sh
```

That's it. The script clones the repo, installs dependencies, pulls a model, connects to the network, and drops you into an **interactive chat**.

After the first run, use the fast launcher:

```bash
bash run.sh
```

> **Windows (no admin):** Install Python from the Microsoft Store and [Git for Windows](https://git-scm.com/download/win), then run the commands above in **Git Bash**. Right-click to paste.

---

## How it works

```text
You  ──►  Gateway  ──►  Best Node (runs full model via Ollama)
                 ◄──  streams tokens back  ◄──
```

- Each node runs a **complete model** locally.
- Nodes connect to the gateway over a **reverse WebSocket** — no open ports, no firewall changes.
- The gateway picks the best node (region + health + round-robin).

| What | Where |
|------|-------|
| Dashboard | `http://GATEWAY:8000/` |
| Chat UI | `http://GATEWAY:8000/chat-ui` |
| Terminal chat | Starts automatically on the node |
| API | `POST /chat` (see below) |

```bash
curl -N http://GATEWAY:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is Microwave AI?", "region": "LAN", "model": "llama3.2"}'
```

---

## Roadmap

| Phase | Goal | Status |
|-------|------|--------|
| **0** | 2 machines on a LAN serving a model | done |
| **1** | 5–10 LAN nodes, health checks, load balancing | done |
| **2** | WAN support — reverse WebSocket from anywhere | done |
| **3** | Model marketplace — nodes advertise capabilities | planned |
| **4** | Reputation system, incentives | planned |
| **5** | Distributed Mixture-of-Experts research | future |

---

<details>
<summary><strong>Architecture deep dive</strong></summary>

### Why decentralize?

Most AI lives behind a few companies' APIs. Microwave asks: what if models ran on a **network of volunteer machines** instead?

### Full-model-per-node (current design)

Each node is an independent inference engine running a complete model. The gateway discovers nodes, tracks health, and routes requests to the best one. No model sharding = no inter-node latency penalty.

### System components

1. **Node** — registers with the gateway, exposes `/health` and `/infer` (HTTP mode) or communicates over WebSocket (reverse mode), talks to local Ollama.
2. **Gateway** — maintains a registry, routes `POST /chat` to the best node, streams tokens back.
3. **Ollama** — local model runtime on each node.

### Request flow (reverse/WS mode)

```text
Node connects OUT ──WebSocket──► Gateway
User ──POST /chat──────────────► Gateway ──task──► Node
User ◄──streaming tokens──────── Gateway ◄─chunks─ Node
```

### Request flow (HTTP mode)

1. Node → `POST /nodes/register` → Gateway
2. User → `POST /chat` → Gateway → `POST /infer` → Node
3. Node streams Ollama tokens back through the gateway.

### APIs

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/nodes/register` | POST | Register a node (HTTP mode) |
| `/nodes/ws` | WebSocket | Reverse-connect a node (WAN mode) |
| `/nodes` | GET | List registered nodes |
| `/nodes/health` | POST | Ping all nodes |
| `/chat` | POST | Send a prompt (streaming) |
| `/` | GET | Dashboard |
| `/chat-ui` | GET | Chat UI |

### Tech stack

- **Python** — FastAPI + httpx + uvicorn + websockets
- **Ollama** — local LLM runtime
- Protocol is HTTP/JSON + WebSocket; easy to reimplement in Go/Rust/Node later.

### Manual setup

```bash
# Gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
microwave-gateway --host 0.0.0.0 --port 8000

# Node (LAN)
microwave-node --gateway-url http://GATEWAY:8000 --region LAN --model llama3.2 --host THIS_IP --port 9000

# Node (WAN)
microwave-node --gateway-url https://GATEWAY_URL --region US-EAST --model llama3.2 --reverse
```

Use `--no-chat` for headless/daemon mode.

</details>
