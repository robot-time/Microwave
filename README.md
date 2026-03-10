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


> A decentralized, volunteer-powered AI inference network – think **BitTorrent for AI**, starting with a tiny LAN prototype.

---

## 1. What Is Microwave AI?

Microwave AI is a long‑term project to build a **peer‑to‑peer AI inference network** where:

- **Anyone can run a node** on their own machine.
- **Nodes contribute compute** (GPU, CPU, RAM, bandwidth).
- **Users run AI models through the network**, instead of through a few big company APIs.
- **Requests are routed to nearby, reliable machines** to keep latency low.
- The network can **grow organically** from a couple of friends on a LAN to a global mesh of volunteer GPUs.

The endgame vision is a kind of **“BitTorrent for AI inference”**:

> a global, decentralized AI supercomputer built out of home GPUs, gaming PCs, and community servers – not just hyperscaler datacenters.

This repository starts **at the very bottom of that ladder**: a simple, concrete Phase 0 prototype you can run between **your server and your laptop on the same network**.

---

## 2. Why Centralized AI Is a Problem

Today, most powerful AI models live behind:

- OpenAI servers  
- Anthropic servers  
- Google / big‑tech datacenters  

Problems with this:

- **Centralized control**: a few companies decide who gets access and on what terms.
- **Opaque infrastructure**: no visibility into how models are run or scheduled.
- **Single‑provider bottlenecks**: outages, pricing changes, and policy shifts affect everyone.

Microwave AI asks:

> What if AI models ran on a **network of volunteer machines**, where anyone can contribute compute and anyone can consume it?

---

## 3. Dense Models vs Mixture‑of‑Experts vs Full‑Model‑Per‑Node

To understand the architecture, we need to briefly talk about how models are structured.

### 3.1 Dense models (naïve approach)

Most language models are **dense transformers**:

```text
Input
 ↓
Embedding
 ↓
Layer 1
 ↓
Layer 2
 ↓
...
 ↓
Layer N
 ↓
Output
```

Every token passes through **all** layers.

Big labs scale this by **splitting layers across GPUs** inside a datacenter (pipeline model parallelism):

- GPU 1 → layers 1–10  
- GPU 2 → layers 11–20  
- GPU 3 → layers 21–30  
- ...

Across a **local, high‑speed interconnect**, this works well.

If you naïvely copy that pattern to the **open internet**, you get:

```text
User → Node A → Node B → Node C → Node D → ...
```

If each hop adds **50–100 ms**, and you have **tens of layers**, latency explodes. This is why **“just shard the model across random internet nodes”** is usually a dead end.

### 3.2 Mixture‑of‑Experts (MoE) – the “expert network” idea

A more interesting direction is **Mixture‑of‑Experts (MoE)**.

Instead of **every** part of the model running for **every** token, you have:

```text
Router
 ├ Expert A (math)
 ├ Expert B (coding)
 ├ Expert C (reasoning)
 ├ Expert D (language)
 ├ Expert E (science)
 └ Expert F (creative writing)
```

For each token, the **router** activates only a **few experts** (e.g. coding + reasoning), not the entire dense stack.

Real models like **Mixtral** work this way:

- Total parameters ≈ 46B  
- Active per token ≈ 12B  

So they feel like a 12B model at runtime, even though the full parameter count is much larger.

On a distributed network, this suggests:

- Node 1 → math expert  
- Node 2 → coding expert  
- Node 3 → reasoning expert  
- Node 4 → language expert  
- Node 5 → science expert  

The router chooses **only relevant experts**, which:

- Reduces the number of network hops.
- Lets the network **grow by adding new experts** as people join.
- Handles churn: if one node disappears, the router can choose another expert.

This is a **very powerful idea** – and a natural fit for a volunteer network – but it is **not** where we start coding. It belongs to later **research phases** once the basic routing and node infrastructure exist.

### 3.3 Full‑model‑per‑node – the Phase 0–2 design

For the first, practical versions of Microwave AI, we adopt a simpler, brutalist principle:

> **Do NOT split models across machines.**

Instead:

- Node A → full 7B model  
- Node B → full 7B model  
- Node C → full 13B model  
- Node D → coding‑optimized model  

Each node is an **independent inference engine**. The network’s job is to:

- Discover nodes.
- Track metadata (location, latency, health, specialization).
- Route requests to the **best single node**.

This avoids the worst internet‑latency issues while still letting you:

- Aggregate thousands of independent inference endpoints.
- Route based on geography, load, and capabilities.
- Evolve toward a **model marketplace** (general chat vs coding vs translation, etc.).

MoE and cross‑node expert routing can be explored **later** on top of this foundation.

---

## 4. High‑Level System Components

At a conceptual level, Microwave AI has four main pieces:

1. **Node software (`ai-node`)** – what volunteers run.
2. **Discovery / coordination layer** – how nodes find and describe each other.
3. **Routing / gateway layer** – how user requests get mapped to nodes.
4. **Model runtime** – how nodes actually run models (LLM backends).

### 4.1 Node software (`ai-node`)

This is the program users run on their machines:

```bash
ai-node start --gateway http://gateway.local:8000 --region AU-SYD --model llama3
```

Responsibilities:

- **Download / manage model(s)** (Phase 0: assume already installed; later: P2P model distribution).
- **Register with the network** (send metadata to the gateway / discovery layer).
- **Expose an inference API** (`/infer`) for prompts.
- **Report health** (`/health`).

Example node metadata:

```json
{
  "node_id": "node-laptop-01",
  "ip": "192.168.1.42",
  "region": "AU-SYD",
  "models": ["llama3:8b", "deepseek-coder:6.7b"],
  "gpu_type": "RTX 4070",
  "cpu": "Ryzen 7",
  "ram_gb": 32,
  "latency_ms": 8,
  "uptime_seconds": 123456,
  "reputation": 0.98
}
```

In Phase 0, this will be simplified, but the **shape** of the data stays similar.

### 4.2 Discovery / coordination layer

The network needs to know:

- Which nodes exist.
- Where they are (region / approximate location).
- What they can run (models, hardware).
- How healthy they are (uptime, responsiveness).

Possible designs:

- **Bootstrap servers** (Phase 0–1): nodes register with one or more well‑known servers (like BitTorrent trackers).
- **Distributed Hash Table (DHT)** (Phase 2+): fully peer‑to‑peer node discovery (like Kademlia).

For now, the **gateway itself** will act as the coordination server.

### 4.3 Routing / gateway layer

Users do **not** talk directly to arbitrary nodes.

They send requests to a **gateway**:

```text
User
 ↓
Gateway
 ↓
Best node
 ↓
Gateway
 ↓
User
```

Gateway jobs:

- **Accept user prompts** via HTTP (`/chat`‑style endpoint).
- **Choose a node** based on:
  - Latency.
  - Reliability / reputation.
  - Load (how busy it is).
  - Region proximity (geographic clusters).
  - Model specialization (coding vs general chat, etc.).
- **Proxy the request** to the chosen node’s `/infer` endpoint.
- **Stream tokens** back to the user as they’re generated.

Example scoring function (later phases):

```text
score = α * latency_ms + β * load_factor - γ * reputation
```

Lower score = better node.

### 4.4 Model runtime

Nodes need a way to actually run models.

Options:

- **Ollama** – easy to install, simple HTTP API, supports many open models.
- **llama.cpp** – C/C++ runtime, good for edge; usually used via a wrapper.
- **vLLM** or similar – heavier, aimed at server settings.

For **Phase 0**, we’ll use:

- **Python** for node + gateway code.
- **Ollama** running locally on each node, speaking HTTP.

This keeps the first prototype very simple while still being “real” (a genuine LLM running on your machines).

---

## 5. Geographic Clusters and Latency

The **biggest enemy** of a usable distributed AI network is **latency**.

Every extra network hop slows down token generation, especially if hops cross continents.

To fight this, we lean hard on **geographic clustering**:

- Nodes group into regions like:
  - `AU-ADE` (Adelaide)
  - `AU-SYD` (Sydney)
  - `JP-TYO` (Tokyo)
  - `DE-BER` (Berlin)
  - `US-SFO` (San Francisco)
- Routing prefers:
  1. Nodes in the **same city / region**.
  2. Nodes in the **same country**.
  3. The **nearest neighboring region**.

Example:

```text
Adelaide user
 ↓
Gateway tags request as region AU-ADE
 ↓
Prefers node in AU-SYD (Sydney) over US-SFO (San Francisco)
```

For the **LAN prototype**, your “region” will effectively just be:

- `LAN` or `HOME-NETWORK`

…but the same concepts will apply when we step out to the wider internet.

---

## 6. Reputation, Cheating, and Incentives

Because **anyone** can join, the network needs ways to:

- **Prefer good nodes**.
- **Ignore bad actors**.

### 6.1 Reputation system

Nodes accumulate a **reputation score** based on:

- Uptime.
- Latency and stability.
- Successful requests.
- Response quality / correctness.

Bad behavior reduces reputation:

- Disconnecting often.
- Timing out.
- Returning garbage outputs.

In later phases, routing will factor this in so that:

- High‑reputation nodes get **more traffic**.
- Low‑reputation nodes are **de‑prioritized or ignored**.

### 6.2 Preventing cheating

Nodes might try to:

- Return fake results instantly.
- Modify or inject malicious content.

Partial mitigations:

- **Redundant inference**:
  - Occasionally send the same prompt to **two nodes**.
  - Compare outputs for consistency / plausibility.
- **Challenge prompts**:
  - Special prompts with known expected behavior.
  - Used to test nodes secretly.

This is a **hard problem** and will evolve over time.

### 6.3 Incentives

Long‑term, people need reasons to keep GPUs running:

- **Credit system**:
  - Nodes earn credits for serving requests.
  - Credits can be spent to run prompts.
- **Token system**:
  - Cryptocurrency‑like tokens for compute.
  - Users pay in tokens for inference.
- **Cooperative model**:
  - People run nodes to support open AI, like BitTorrent seeders or Tor relays.

Phase 0 will **not** implement economics – it focuses on **just making the network actually run**.

---

## 7. Phased Roadmap (Phase 0 → Phase 3)

Think of Microwave AI as an incremental climb:

### Phase 0 – LAN prototype (this repo)

Goal: **Prove the basic idea on your own network.**

- 1 gateway on your **server**.
- 1–2 nodes (server + laptop) on the **same LAN**.
- Nodes run a **complete local model** (via Ollama).
- Manual / simple registration (gateway keeps an in‑memory node list).
- Simple routing (first healthy node or round‑robin).
- Streaming tokens from node → gateway → user.

### Phase 1 – Multi‑region, central coordination

Goal: **Extend to the public internet with a central coordinator.**

- Multiple gateways and nodes with **region tags** (`AU-SYD`, `JP-TYO`, etc.).
- Nodes register with a **bootstrap server** (or small cluster of them).
- Basic reputation and **redundant inference** for some requests.
- Support for multiple model types (chat vs code).

### Phase 2 – Model marketplace and specialization

Goal: **Turn the network into a marketplace of capabilities.**

- Nodes advertise **capabilities** (chat, coding, translation, image).
- Routers choose nodes based on **task type + health + reputation**.
- Begin experimenting with:
  - Credit / token tracking.
  - Simple user accounts and quotas.
  - P2P **model distribution** (torrent‑like).

### Phase 3 – Research: distributed MoE and sharded models

Goal: **Explore the more exotic architectures (your MoE idea).**

- Implement a **router network** that selects experts across machines.
- Each node can host **one or more experts** instead of full models.
- Evaluate:
  - Latency vs dense / full‑model‑per‑node.
  - Reliability when nodes churn.
  - Quality consistency at scale.

This phase is where the **“decentralized Mixture‑of‑Experts”** dream really comes alive, but it relies heavily on having a robust Phase 0–2 infrastructure.

---

## 8. Phase 0: Concrete Architecture (LAN‑Only)

Phase 0 is designed to be as simple as possible while still being “real”.

### 8.1 Components

- **Gateway service (Python)**:
  - Exposes an HTTP API (e.g. `POST /chat`).
  - Maintains a list of registered nodes (in memory).
  - Implements a trivial routing policy.
  - Proxies prompts to nodes and **streams back tokens**.

- **Node service (`ai-node`, Python)**:
  - Runs on your server and/or laptop.
  - On startup, **registers** with the gateway.
  - Exposes:
    - `GET /health` – simple health check.
    - `POST /infer` – accepts a prompt and forwards it to **local Ollama**.
  - Streams tokens from Ollama back to the gateway.

- **Local LLM runtime (Ollama)**:
  - Installed on each node machine.
  - Provides a `/api/generate` or `/v1/chat` HTTP API.
  - Hosts a 7B‑ish model (e.g. `llama3`, `qwen`, `deepseek-coder`).

### 8.2 Request flow (end‑to‑end)

1. **Node starts**:
   - `ai-node start --gateway http://gateway:8000 --region LAN --model llama3`
   - Sends `POST /nodes/register` to the gateway with its metadata.
2. **User sends a prompt**:
   - `POST /chat` to the gateway with JSON body `{ "prompt": "Hello, who are you?" }`.
3. **Gateway selects a node**:
   - For Phase 0:
     - Either **first registered node**.
     - Or **simple round‑robin** across healthy nodes.
4. **Gateway forwards to node**:
   - Calls the node’s `POST /infer` with the prompt.
5. **Node calls Ollama**:
   - `POST http://localhost:11434/api/generate` (or `/v1/chat`) with the prompt and model name.
   - Streams tokens as they are produced.
6. **Gateway relays stream to user**:
   - Reads the streamed response from the node.
   - Streams tokens back to the original user connection.

Result: text appears **token by token** in the client, as if you were calling a normal hosted LLM – but all the compute happens on your **own machines**.

---

## 9. APIs (Phase 0 Draft)

These are **not yet implemented**, but this is the intended shape.

### 9.1 Gateway API

#### `POST /nodes/register`

Register a node with the gateway.

Request body:

```json
{
  "node_id": "node-laptop-01",
  "host": "192.168.1.42",
  "port": 9000,
  "region": "LAN",
  "models": ["llama3:8b"],
  "metadata": {
    "gpu": "RTX 4070",
    "ram_gb": 32
  }
}
```

Response:

```json
{
  "status": "ok"
}
```

#### `GET /nodes`

Debug endpoint that returns the currently known nodes.

#### `POST /chat`

User entrypoint for generating a response.

Request:

```json
{
  "prompt": "Explain what Microwave AI is in 2 sentences.",
  "region": "LAN",
  "model": "llama3:8b"
}
```

Response:

- **Streaming** text (e.g. Server‑Sent Events, chunked HTTP, or similar).

### 9.2 Node API

#### `GET /health`

Simple liveness check.

Response:

```json
{
  "status": "ok",
  "uptime_seconds": 1234
}
```

#### `POST /infer`

Called by the gateway to run inference.

Request:

```json
{
  "prompt": "Explain Microwave AI in one sentence.",
  "model": "llama3:8b"
}
```

Behavior:

- Node forwards this to **local Ollama** and streams back tokens.
- Gateway acts as a transparent proxy.

---

## 10. Tech Stack Choices (Phase 0)

For the initial LAN prototype:

- **Language**: Python
  - Fast to iterate.
  - Easy async networking (e.g. FastAPI + `httpx` / `aiohttp`).
  - Plays nicely with ML tooling later.
- **Gateway**:
  - Python HTTP server (e.g. FastAPI / Starlette / bare ASGI).
  - Minimal dependencies beyond the standard library + 1 async HTTP lib.
- **Node**:
  - Python HTTP server + simple CLI wrapper (`ai-node` entrypoint).
  - Talks to **local Ollama** over HTTP.
- **Model runtime**:
  - Ollama (`ollama` CLI + HTTP service).

This keeps the code small and hackable while keeping the **network protocol** clean enough to later re‑implement in Go, Rust, or Node.js if needed.

---

## 11. Quickstart – One Interactive Setup Script

Run **one script** on any machine to set up a gateway, a node, or both.
The script auto-detects your LAN IP, walks you through a short menu, and starts everything.

```bash
git clone https://github.com/robot-time/Microwave.git
cd Microwave
bash setup.sh
```

The script will:

1. Print the **Microwave Network** ASCII banner.
2. Auto-detect your machine's LAN IP.
3. Ask: **Gateway, Node, or Both?**
4. Ask which model to serve (node) – with a numbered menu.
5. Check Ollama is installed and pull the model if needed.
6. Create a `.venv`, install dependencies, and start everything.

Example session:

```text
     ________________
    |.-----------.   |
    ||   _____   |ooo|
    ||  |     |  |ooo|
    ||  |     |  | = |
    ||  '-----'  | _ |
    ||___________|[_]|
    '----------------'
------------------------------------------------
Microwave Network – decentralised AI inference

  Detected LAN IP: 192.168.20.30

  What do you want to run on this machine?

  1) Gateway  (central coordinator – routes requests to nodes)
  2) Node     (volunteer compute – runs the AI model locally)
  3) Both     (gateway + node on the same machine)

  Enter 1, 2, or 3: 1

  Gateway port [8000]:
  Looks good? Start setup (y/n) [y]: y

  Starting gateway on port 8000 ...

  Control plane: http://192.168.20.30:8000/
  Chat UI:       http://192.168.20.30:8000/chat-ui
```

### Adding more nodes

Run `bash setup.sh` on any other machine, choose **Node (2)**, and enter the gateway's IP when prompted.
Each node auto-detects its own LAN IP and self-registers with the gateway.

### Talk to the network

- **Control plane** (node list, health, ping): `http://GATEWAY_IP:8000/`
- **Chat UI**: `http://GATEWAY_IP:8000/chat-ui`
- **Raw API**:

```bash
curl -N http://GATEWAY_IP:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is Microwave AI?", "region": "LAN", "model": "llama3.2"}'
```

---

## 12. Getting Started (Reference – lower level)

The same setup as above can be done manually if you want more control.

### 12.1 Prerequisites

- Python 3.10+ installed on both the **gateway server** and **node machines**.
- [Ollama](https://ollama.com) installed on each node machine.
- A model pulled on each node, e.g.:

```bash
ollama pull llama3
```

### 12.2 Run the gateway (on your server)

```bash
python -m microwave_ai.gateway --host 0.0.0.0 --port 8000
```

### 12.3 Run a node (on your laptop)

```bash
python -m microwave_ai.node \
  --gateway http://SERVER_LAN_IP:8000 \
  --region LAN \
  --model llama3
```

The node will:

- Call `POST /nodes/register` on the gateway.
- Start listening on e.g. `http://LAPTOP_LAN_IP:9000`.

### 12.4 Send a test prompt

From any machine that can reach the gateway:

```bash
curl -N http://SERVER_LAN_IP:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is Microwave AI?", "region": "LAN", "model": "llama3"}'
```

You should see a streamed response produced by the model running on your **laptop**.

---

## 13. Current Status and Next Steps

At the moment, this repository is primarily:

- A **spec and design document** for Microwave AI.
- A blueprint for the **Phase 0 LAN prototype**:
  - Python gateway.
  - Python node.
  - Ollama as the local runtime on each node.

Upcoming implementation steps:

1. Scaffold the Python project structure (`gateway` and `node` entrypoints).
2. Implement the **node HTTP API** (`/infer`, `/health`) that talks to local Ollama.
3. Implement the **gateway HTTP API** (`/nodes/register`, `/nodes`, `/chat`) with basic routing.
4. Run a **live test** on your LAN with your server + laptop.

From there, we can iterate toward:

- A simple **public testnet**.
- A more robust **reputation system**.
- And eventually, the more ambitious **MoE‑style distributed expert network**.

