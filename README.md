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

Anyone can run a node. Nodes contribute compute as **experts**. A router picks the best experts for each prompt and queries them **in parallel**. The network grows from a couple of friends on a LAN to a global mesh of volunteer GPUs.

---

## Quickstart

**Requirements:** [Python 3.10+](https://python.org), [Git](https://git-scm.com), and [curl](https://curl.se).

```bash
curl -fsSL https://raw.githubusercontent.com/robot-time/Microwave/main/install.sh | sh
```

That's it. One command. It clones the repo, creates a venv, installs Ollama + a model, and connects your machine to the network as an expert node.

With options:

```bash
curl -fsSL https://raw.githubusercontent.com/robot-time/Microwave/main/install.sh | MICROWAVE_EXPERT_DOMAINS=code,math sh
```

After the first run, re-launch from the install directory:

```bash
cd ~/Microwave && bash run.sh
```

> **Windows:** Install Python from the Microsoft Store and [Git for Windows](https://git-scm.com/download/win), then run the commands above in **Git Bash**.

### Expert domains

Each node can specialize. Set domains before running setup:

```bash
MICROWAVE_EXPERT_DOMAINS="code,math" bash setup.sh
```

Available domains: `general`, `code`, `math`, `creative`, `science`, `reasoning`. Defaults to `general` if unset.

---

## How it works

```text
User  ──►  Gateway (router)  ──►  Expert 1  ──►  ┐
                              ──►  Expert 2  ──►  ├── Aggregate ──► Stream back
                              ──►  Expert K  ──►  ┘
```

Microwave uses a **Mixture of Experts (MoE)** architecture:

1. **Prompt arrives** at the gateway.
2. **Router classifies** the prompt by domain (code, math, creative, etc.) and scores every online expert by relevance + latency + compute capacity.
3. **Top-K experts** receive the prompt **in parallel** — latency = slowest single expert, not the sum of all.
4. **Aggregation** returns the fastest response (or highest-confidence, or blended).

Each node runs a **complete model** locally via Ollama and connects over a **reverse WebSocket** — no open ports, no firewall changes.

| What | Where |
|------|-------|
| Dashboard | `http://GATEWAY:8000/` |
| Chat UI | `http://GATEWAY:8000/chat-ui` |
| Expert list | `http://GATEWAY:8000/experts` |
| Route preview | `POST /experts/route` |
| API | `POST /chat` (see below) |

```bash
curl -N http://GATEWAY:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write a python sort function", "model": "llama3.2", "strategy": "fastest"}'
```

The `strategy` field controls aggregation: `fastest` (default), `confidence`, or `blend`.

---

## Roadmap

| Phase | Goal | Status |
|-------|------|--------|
| **0** | 2 machines on a LAN serving a model | done |
| **1** | 5–10 LAN nodes, health checks, load balancing | done |
| **2** | WAN support — reverse WebSocket from anywhere | done |
| **3** | Mixture of Experts — parallel dispatch, domain routing | done |
| **4** | Latency-optimized networking — EWMA tracking, geo-aware | done |
| **5** | Model marketplace — nodes advertise capabilities | planned |
| **6** | Reputation system, incentives | planned |

---

<details>
<summary><strong>Architecture deep dive</strong></summary>

### Why decentralize?

Most AI lives behind a few companies' APIs. Microwave asks: what if models ran on a **network of volunteer machines** instead?

### Mixture of Experts (MoE)

Every node is an **expert**. The gateway's router selects which experts to activate for each prompt:

```text
device
  ↓
small local router (on gateway, near-zero latency)
  ↓
distributed experts (parallel)
```

**Scoring formula per expert:**
```
score = 0.35 × domain_relevance + 0.45 × speed_score + 0.20 × capacity_score
```

- **Domain relevance** — keyword classifier maps prompts to domains, matched against each expert's declared specialties.
- **Speed score** — derived from EWMA ping latency (faster = higher).
- **Capacity score** — normalized GPU/compute benchmark.

The router also adapts **K** (how many experts to query) based on prompt complexity: simple questions → 1 expert, complex multi-domain prompts → 2-3 experts.

### Why MoE over pipeline parallelism?

| | Pipeline (serial) | MoE (parallel) |
|---|---|---|
| Latency | `sum(all stages)` | `max(1 expert)` |
| Failure | 1 node down = broken | 1 down = use others |
| Communication | Tensor serialization | Standard prompt/response |
| Scaling | More nodes = more hops | More nodes = more choices |

### System components

1. **Expert Node** — registers with the gateway with its model, domains, and hardware capabilities. Handles `moe_expert_task` messages by running Ollama and streaming chunks back.
2. **Gateway** — maintains the expert registry, runs the MoE router, dispatches to top-K experts in parallel, aggregates responses, and streams tokens to the user.
3. **Router** (`inference/router.py`) — classifies prompts, scores experts, selects top-K.
4. **MoE Coordinator** (`inference/moe.py`) — parallel dispatch, response aggregation (fastest / confidence / blend).
5. **Ollama** — local model runtime on each node.

### Aggregation strategies

| Strategy | Behavior | Best for |
|----------|----------|----------|
| `fastest` | Lock onto whichever expert streams first | Lowest time-to-first-token |
| `confidence` | Collect all responses, pick highest confidence | Best quality |
| `blend` | Stream fastest, note disagreements | Balance of speed and quality |

### Network layer

- **EWMA latency tracking** — exponentially weighted moving average for robust ping estimates.
- **Geographic awareness** — Haversine distance between nodes, IP geolocation auto-detection.
- **Inter-node topology** — RTT matrix for optimal routing decisions.

### Request flow (MoE / reverse WS)

```text
Expert nodes connect OUT ──WebSocket──► Gateway
User ──POST /chat──────────────────────► Gateway
  Gateway router selects top-K experts
  Gateway ──moe_expert_task──► Expert 1 (parallel)
  Gateway ──moe_expert_task──► Expert 2 (parallel)
  Expert 1 ──moe_expert_chunk──► Gateway ──stream──► User
```

### APIs

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/nodes/register` | POST | Register a node (HTTP mode) |
| `/nodes/ws` | WebSocket | Reverse-connect a node (WAN mode) |
| `/nodes` | GET | List registered nodes |
| `/nodes/health` | POST | Ping all nodes |
| `/experts` | GET | List MoE experts with scores |
| `/experts/route` | POST | Preview routing for a prompt (dry-run) |
| `/chat` | POST | Send a prompt (streaming MoE) |
| `/health` | GET | Gateway health + MoE stats |
| `/` | GET | Dashboard |
| `/chat-ui` | GET | Chat UI |

### Tech stack

- **Python** — FastAPI + httpx + uvicorn + websockets + numpy + psutil
- **Ollama** — local LLM runtime
- Protocol is HTTP/JSON + WebSocket; easy to reimplement in Go/Rust/Node later.

### Manual setup

```bash
# Gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
microwave-gateway --host 0.0.0.0 --port 8000

# Expert node (LAN)
microwave-node --gateway-url http://GATEWAY:8000 --region LAN --model llama3.2 \
  --expert-domains general,code --host THIS_IP --port 9000

# Expert node (WAN)
microwave-node --gateway-url https://GATEWAY_URL --region US-EAST --model llama3.2 \
  --expert-domains math,science --reverse
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MICROWAVE_GATEWAY_URL` | — | Gateway address |
| `MICROWAVE_MODEL` | `llama3` | Model to run |
| `MICROWAVE_REGION` | `LAN` | Region label |
| `MICROWAVE_EXPERT_DOMAINS` | `general` | Comma-separated domains |
| `MICROWAVE_LAT` / `MICROWAVE_LON` | auto-detected | GPS coordinates |
| `MICROWAVE_ENGINE` | `ollama` | Inference engine |
| `MICROWAVE_DRAFT_MODELS` | — | Draft models for speculative decoding |

</details>
