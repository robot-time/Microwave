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

> A decentralized, volunteer-powered AI inference network – think **BitTorrent for AI**.

Anyone can run a node. Nodes contribute compute. Users talk to AI models through the network instead of through big company APIs. Requests get routed to nearby, reliable machines to keep latency low. The network grows organically – from a couple of friends on a LAN to a global mesh of volunteer GPUs.

---

## Quickstart

**Requirements:** [Python 3.10+](https://python.org) and [Ollama](https://ollama.com) installed.

```bash
git clone https://github.com/robot-time/Microwave.git
cd Microwave
bash setup.sh
```

The script auto-detects your IP, asks if you want to run a **Gateway**, a **Node**, or **Both**, picks a model, and starts everything.
#### Admin-less Windows setup

If you don't have administrator rights on Windows, you can still get running using the Microsoft Store Python and Git Bash:

1. **Install Python from the Microsoft Store**
   - Open the Microsoft Store and search for **"Python"** (for example, **Python 3.x** from the Python Software Foundation).
   - Click **Get** / **Install** and wait for it to finish.
   - When Windows prompts you to **"Open app execution aliases"** or **"Open Settings"**, open the **App execution aliases** settings page and **turn OFF** all toggles for `python.exe` and `python3.exe` so your shell uses the real Python installation.

2. **Install Git for Windows with Git Bash**
   - Download Git for Windows from `https://git-scm.com/download/win`.
   - Run the installer (install to your user directory if you don't have admin).
   - Make sure **Git Bash** is installed/enabled.

3. **Clone the repo using Git Bash**
   - Open **Git Bash** and run:

   ```bash
   git clone https://github.com/robot-time/Microwave.git
   cd Microwave
   ```

4. **Run the quickstart script in Git Bash**
   - From inside the repo, run:

   ```bash
   bash setup.sh
   ```

   This will create a virtual environment, install Microwave, and start the gateway/node based on your answers.

5. **If the script fails with a virtualenv or package error**
   - In **Git Bash**, from the project root, run:

   ```bash
   python -m venv .venv
   source .venv/Scripts/activate  # note: on Windows, use Scripts not bin
   pip install --upgrade pip
   pip install -e .
   ```
Then start the gateway manually:

   ```bash
   microwave-gateway --host 0.0.0.0 --port 8000
   ```
### Adding more machines

Run `bash setup.sh` on another computer, choose **Node**, and enter the gateway's IP when prompted. It self-registers automatically.

### Use it

| What | URL |
|------|-----|
| Dashboard (nodes, health, ping) | `http://GATEWAY_IP:8000/` |
| Chat UI | `http://GATEWAY_IP:8000/chat-ui` |
| Raw API | see below |

```bash
curl -N http://GATEWAY_IP:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is Microwave AI?", "region": "LAN", "model": "llama3.2"}'
```

### Windows note

On Windows, run `setup.sh` inside **Git Bash**. If the gateway can't reach your node, allow the port through Windows Firewall (Admin PowerShell):

```powershell
netsh advfirewall firewall add rule name="Microwave Node" dir=in action=allow protocol=TCP localport=9000
```

---

## How it works (short version)

```text
You  ──►  Gateway  ──►  Best Node (runs full model locally via Ollama)
                 ◄──  streams tokens back  ◄──
```

Each node runs a **complete model**. The gateway picks the best node based on region + health + round-robin load balancing. No model sharding across machines = no inter-node latency penalty.

---

## Roadmap

| Phase | Goal | Status |
|-------|------|--------|
| **0** | 2+ machines on a LAN serving a model | **done** |
| **1** | 5–10 LAN nodes, health checks, load balancing | in progress |
| **2** | WAN test across the internet, regional routing | planned |
| **3** | Model marketplace – nodes advertise capabilities | planned |
| **4** | Reputation system, incentives | planned |
| **5** | Research: distributed Mixture-of-Experts | future |

---

<details>
<summary><strong>Read more – full architecture deep dive</strong></summary>

## Why Centralized AI Is a Problem

Today, most powerful AI models live behind OpenAI, Anthropic, and Google datacenters.

- **Centralized control**: a few companies decide who gets access and on what terms.
- **Opaque infrastructure**: no visibility into how models are run or scheduled.
- **Single‑provider bottlenecks**: outages, pricing changes, and policy shifts affect everyone.

Microwave AI asks:

> What if AI models ran on a **network of volunteer machines**, where anyone can contribute compute and anyone can consume it?

---

## Dense Models vs Mixture‑of‑Experts vs Full‑Model‑Per‑Node

### Dense models (naïve approach)

Most language models are **dense transformers** – every token passes through **all** layers. Big labs scale this by splitting layers across GPUs inside a datacenter (pipeline model parallelism).

If you naïvely copy that pattern to the **open internet**:

```text
User → Node A → Node B → Node C → Node D → ...
```

Each hop adds **50–100 ms**. With tens of layers, latency explodes. This is why "just shard the model across random internet nodes" is usually a dead end.

### Mixture‑of‑Experts (MoE) – the "expert network" idea

Instead of every part of the model running for every token:

```text
Router
 ├ Expert A (math)
 ├ Expert B (coding)
 ├ Expert C (reasoning)
 ├ Expert D (language)
 ├ Expert E (science)
 └ Expert F (creative writing)
```

For each token, the router activates only a **few experts**. Real models like **Mixtral** work this way (46B total params, ~12B active per token).

On a distributed network, each node could host one expert. The router chooses only relevant experts, reducing hops and handling churn naturally. This is a **very powerful idea** – and a natural fit for a volunteer network – but it belongs to later research phases.

### Full‑model‑per‑node – the current design

For Phase 0–2:

> **Do NOT split models across machines.**

- Node A → full 7B model
- Node B → full 7B model
- Node C → full 13B model
- Node D → coding‑optimized model

Each node is an independent inference engine. The network discovers nodes, tracks health, and routes requests to the best single node. This avoids internet-latency problems while still letting you aggregate thousands of endpoints and route by geography, load, and capabilities.

---

## System Components

1. **Node software** – what volunteers run. Registers with the gateway, exposes `/health` and `/infer`, talks to local Ollama.
2. **Discovery / coordination** – the gateway maintains a registry of nodes, their regions, models, and health.
3. **Routing / gateway** – accepts user prompts via `POST /chat`, picks the best node (round-robin within region), proxies the request, streams tokens back.
4. **Model runtime** – Ollama running locally on each node.

### Node metadata example

```json
{
  "node_id": "node-laptop-01",
  "ip": "192.168.1.42",
  "region": "AU-SYD",
  "models": ["llama3:8b", "deepseek-coder:6.7b"],
  "gpu_type": "RTX 4070",
  "ram_gb": 32,
  "latency_ms": 8,
  "reputation": 0.98
}
```

### Request flow

1. Node starts → sends `POST /nodes/register` to the gateway.
2. User sends `POST /chat` to the gateway.
3. Gateway picks the best node (round-robin within region).
4. Gateway forwards `POST /infer` to the node.
5. Node calls Ollama's `/api/generate` and streams tokens.
6. Gateway relays the stream back to the user.

---

## Geographic Clusters and Latency

Latency is the biggest enemy. To fight it, nodes group into regions (`AU-SYD`, `JP-TYO`, `US-SFO`, etc.) and routing prefers same-region > same-country > nearest-region.

```text
Adelaide user → Gateway tags AU-ADE → prefers AU-SYD node over US-SFO
```

For the LAN prototype, region is just `LAN`.

---

## Reputation, Cheating, and Incentives

### Reputation

Nodes accumulate a score based on uptime, latency, successful requests, and response quality. Bad behavior (disconnects, timeouts, garbage output) reduces reputation. High-reputation nodes get more traffic.

### Preventing cheating

- **Redundant inference**: occasionally send the same prompt to two nodes, compare outputs.
- **Challenge prompts**: known-answer prompts to secretly test nodes.

### Incentives (future)

- **Credit system**: earn credits by serving requests, spend them to run prompts.
- **Token system**: cryptocurrency-like tokens for compute.
- **Cooperative model**: volunteer-driven, like BitTorrent seeders or Tor relays.

---

## APIs (Phase 0)

### Gateway

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/nodes/register` | POST | Register a node |
| `/nodes` | GET | List registered nodes |
| `/nodes/health` | POST | Ping all nodes and update latency |
| `/chat` | POST | Send a prompt (streaming response) |
| `/` | GET | Dashboard UI |
| `/chat-ui` | GET | Chat UI |

**`POST /chat` request:**

```json
{
  "prompt": "Explain Microwave AI in 2 sentences.",
  "region": "LAN",
  "model": "llama3:8b"
}
```

### Node

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Liveness check |
| `/infer` | POST | Run inference (called by gateway) |

---

## Tech Stack

- **Python** – FastAPI + httpx + uvicorn
- **Ollama** – local LLM runtime on each node
- Protocol is plain HTTP/JSON, easy to re-implement in Go, Rust, or Node.js later

---

## Manual Setup (advanced)

If you prefer to skip the interactive script:

### Prerequisites

- Python 3.10+
- [Ollama](https://ollama.com) with a model pulled (`ollama pull llama3.2`)

### Gateway

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
microwave-gateway --host 0.0.0.0 --port 8000
```

### Node

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
microwave-node \
  --gateway-url http://GATEWAY_IP:8000 \
  --region LAN \
  --model llama3.2 \
  --host THIS_MACHINE_IP \
  --port 9000
```

</details>
