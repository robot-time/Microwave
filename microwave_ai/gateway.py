import argparse
import asyncio
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
import uvicorn

from . import __version__
from .network.latency import LatencyTracker
from .network.topology import TopologyManager
from .network.region import RegionEngine
from .inference.moe import MoECoordinator, ExpertInfo, AggregationStrategy
from .inference.router import ExpertRouter, classify_prompt
from .inference.speculative import SpeculativeDecoder


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
    latitude: float = 0.0
    longitude: float = 0.0
    vram_mb: int = 0
    ram_mb: int = 0
    compute_score: float = 0.0
    engine_type: str = "ollama"
    loaded_layers: Optional[Tuple[int, int]] = None
    draft_models: List[str] = field(default_factory=list)
    expert_domains: List[str] = field(default_factory=lambda: ["general"])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


app = FastAPI(title="Microwave AI Gateway")
nodes: Deque[NodeInfo] = deque()

HEALTH_INTERVAL_SECONDS = 3
TOPOLOGY_MEASURE_INTERVAL = 60
_health_task = None
_topology_task = None

latency_tracker = LatencyTracker(alpha=0.3, failure_penalty_ms=500.0)
topology_manager = TopologyManager(stale_seconds=120.0)
region_engine = RegionEngine(max_pipeline_distance_km=500.0)
moe_coordinator = MoECoordinator(default_k=2, max_k=5)
expert_router = ExpertRouter(
    latency_tracker=latency_tracker,
    region_engine=region_engine,
)
speculative_decoder = SpeculativeDecoder(draft_k=5, min_k=2, max_k=12)

# WebSocket reverse-connected nodes
_ws_connections: Dict[str, WebSocket] = {}
_ws_locks: Dict[str, asyncio.Lock] = {}
_task_queues: Dict[str, asyncio.Queue] = {}


async def _periodic_health_check() -> None:
    """Background loop: ping every registered node with EWMA tracking."""
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
                            rtt = (time.perf_counter() - start) * 1000.0
                            node.last_heartbeat = time.time()
                            node.last_latency_ms = rtt
                            latency_tracker.record(node.node_id, rtt)
                        except Exception:
                            node.last_latency_ms = -1.0
                            latency_tracker.record_failure(node.node_id)
                    else:
                        node.last_latency_ms = -1.0
                        latency_tracker.record_failure(node.node_id)
                else:
                    start = time.perf_counter()
                    try:
                        resp = await client.get(
                            f"{node.base_url}/health", timeout=3.0
                        )
                        if resp.status_code == 200:
                            rtt = (time.perf_counter() - start) * 1000.0
                            node.last_heartbeat = time.time()
                            node.last_latency_ms = rtt
                            latency_tracker.record(node.node_id, rtt)
                        else:
                            node.last_latency_ms = -1.0
                            latency_tracker.record_failure(node.node_id)
                    except Exception:
                        node.last_latency_ms = -1.0
                        latency_tracker.record_failure(node.node_id)


async def _periodic_topology_measure() -> None:
    """Background loop: measure inter-node latencies for pipeline optimization."""
    while True:
        await asyncio.sleep(TOPOLOGY_MEASURE_INTERVAL)
        node_ids = [n.node_id for n in nodes if n.is_ws]
        if len(node_ids) < 2:
            continue

        pairs = topology_manager.needs_measurement(node_ids)
        for src, dst in pairs[:10]:
            ws = _ws_connections.get(src)
            lock = _ws_locks.get(src)
            if not ws or not lock:
                continue
            try:
                async with lock:
                    await ws.send_json(
                        {
                            "type": "measure_peer",
                            "target_node_id": dst,
                        }
                    )
            except Exception:
                pass


@app.on_event("startup")
async def start_background_loops() -> None:
    global _health_task, _topology_task
    _health_task = asyncio.create_task(_periodic_health_check())
    _topology_task = asyncio.create_task(_periodic_topology_measure())


def _upsert_node(
    node_id: str,
    host: str,
    port: int,
    region: str,
    models: List[str],
    metadata: Dict[str, Any],
    is_ws: bool = False,
    latitude: float = 0.0,
    longitude: float = 0.0,
    vram_mb: int = 0,
    ram_mb: int = 0,
    compute_score: float = 0.0,
    engine_type: str = "ollama",
    draft_models: Optional[List[str]] = None,
    expert_domains: Optional[List[str]] = None,
) -> NodeInfo:
    global nodes
    domains = expert_domains or ["general"]
    nodes = deque(n for n in nodes if n.node_id != node_id)
    info = NodeInfo(
        node_id=node_id,
        host=host,
        port=port,
        region=region,
        models=models,
        metadata=metadata,
        last_heartbeat=time.time(),
        last_latency_ms=0.0,
        is_ws=is_ws,
        latitude=latitude,
        longitude=longitude,
        vram_mb=vram_mb,
        ram_mb=ram_mb,
        compute_score=compute_score,
        engine_type=engine_type,
        draft_models=draft_models or [],
        expert_domains=domains,
    )
    nodes.append(info)

    region_engine.register(node_id, latitude, longitude, region)

    moe_coordinator.register_expert(ExpertInfo(
        node_id=node_id,
        models=models,
        domains=domains,
        compute_score=compute_score,
        vram_mb=vram_mb,
    ))

    return info


@app.post("/nodes/register")
async def register_node(payload: Dict[str, Any]) -> JSONResponse:
    node_id = payload.get("node_id")
    host = payload.get("host")
    port = payload.get("port")
    region = payload.get("region", "LAN")
    models = payload.get("models") or []
    metadata = payload.get("metadata") or {}
    latitude = float(payload.get("latitude", 0.0))
    longitude = float(payload.get("longitude", 0.0))
    vram_mb = int(payload.get("vram_mb", 0))
    ram_mb = int(payload.get("ram_mb", 0))
    compute_score = float(payload.get("compute_score", 0.0))
    engine_type = payload.get("engine_type", "ollama")
    draft_models = payload.get("draft_models") or []
    expert_domains = payload.get("expert_domains") or ["general"]

    if not node_id or not host or not port:
        raise HTTPException(
            status_code=400, detail="node_id, host, and port are required"
        )

    _upsert_node(
        node_id,
        host,
        int(port),
        region,
        models,
        metadata,
        latitude=latitude,
        longitude=longitude,
        vram_mb=vram_mb,
        ram_mb=ram_mb,
        compute_score=compute_score,
        engine_type=engine_type,
        draft_models=draft_models,
        expert_domains=expert_domains,
    )
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
        latitude = float(reg.get("latitude", 0.0))
        longitude = float(reg.get("longitude", 0.0))
        vram_mb = int(reg.get("vram_mb", 0))
        ram_mb = int(reg.get("ram_mb", 0))
        compute_score = float(reg.get("compute_score", 0.0))
        engine_type = reg.get("engine_type", "ollama")
        draft_models = reg.get("draft_models", [])
        expert_domains = reg.get("expert_domains", ["general"])

        _upsert_node(
            node_id,
            "ws-connected",
            0,
            region,
            models,
            metadata,
            is_ws=True,
            latitude=latitude,
            longitude=longitude,
            vram_mb=vram_mb,
            ram_mb=ram_mb,
            compute_score=compute_score,
            engine_type=engine_type,
            draft_models=draft_models,
            expert_domains=expert_domains,
        )
        _ws_connections[node_id] = ws
        _ws_locks[node_id] = asyncio.Lock()
        print(
            f"[ws] Expert connected: {node_id} (region={region}, "
            f"domains={expert_domains}, models={models}, vram={vram_mb}MB)"
        )
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

            elif msg_type == "peer_measurement":
                target = msg.get("target_node_id", "")
                rtt = msg.get("rtt_ms", -1.0)
                if rtt >= 0:
                    topology_manager.update(node_id, target, rtt)

            elif msg_type == "draft_result":
                task_id = msg.get("task_id")
                q = _task_queues.get(task_id)
                if q:
                    await q.put(msg)

            elif msg_type == "verify_result":
                task_id = msg.get("task_id")
                q = _task_queues.get(task_id)
                if q:
                    await q.put(msg)

            elif msg_type == "pipeline_token":
                task_id = msg.get("task_id")
                q = _task_queues.get(task_id)
                if q:
                    await q.put(msg)

            elif msg_type == "pipeline_done":
                task_id = msg.get("task_id")
                q = _task_queues.get(task_id)
                if q:
                    await q.put(None)

            elif msg_type == "moe_expert_chunk":
                task_id = msg.get("task_id")
                q = _task_queues.get(task_id)
                if q:
                    await q.put(msg.get("data", ""))

            elif msg_type == "moe_expert_done":
                task_id = msg.get("task_id")
                q = _task_queues.get(task_id)
                if q:
                    await q.put(None)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if node_id:
            _ws_connections.pop(node_id, None)
            _ws_locks.pop(node_id, None)
            nodes = deque(n for n in nodes if n.node_id != node_id)
            latency_tracker.remove(node_id)
            topology_manager.remove_node(node_id)
            region_engine.remove(node_id)
            moe_coordinator.remove_expert(node_id)
            print(f"[ws] Expert disconnected: {node_id}")


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
            "ewma_ms": round(latency_tracker.ewma(n.node_id), 2),
            "jitter_ms": round(latency_tracker.jitter(n.node_id), 2),
            "score": round(latency_tracker.score(n.node_id), 2),
            "connection": "ws" if n.is_ws else "http",
            "latitude": n.latitude,
            "longitude": n.longitude,
            "vram_mb": n.vram_mb,
            "ram_mb": n.ram_mb,
            "compute_score": round(n.compute_score, 1),
            "engine_type": n.engine_type,
            "draft_models": n.draft_models,
            "expert_domains": n.expert_domains,
        }
        for n in nodes
    ]


@app.get("/experts")
async def list_experts() -> List[Dict[str, Any]]:
    """List all registered MoE experts with their scores."""
    result = []
    for expert in moe_coordinator.all_experts():
        result.append({
            "node_id": expert.node_id,
            "models": expert.models,
            "domains": expert.domains,
            "compute_score": round(expert.compute_score, 1),
            "vram_mb": expert.vram_mb,
            "latency_ms": round(latency_tracker.ewma(expert.node_id), 2),
        })
    return result


@app.post("/experts/route")
async def route_preview(payload: Dict[str, Any]) -> JSONResponse:
    """Preview which experts would be selected for a prompt (dry-run)."""
    prompt = payload.get("prompt", "")
    model = payload.get("model")
    region = payload.get("region")
    k = payload.get("k")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    experts = moe_coordinator.all_experts()
    online_ids = [n.node_id for n in nodes if n.last_latency_ms >= 0]
    if k is None:
        k = expert_router.adaptive_k(prompt, len(online_ids))
    selected = expert_router.select_experts(prompt, experts, online_ids, k=k, region=region, model=model)
    domains = classify_prompt(prompt)
    return JSONResponse({
        "prompt_domains": domains,
        "selected_experts": [
            {"node_id": nid, "weight": round(w, 3)} for nid, w in selected
        ],
        "k": k,
    })


@app.get("/speculative/stats")
async def speculative_stats() -> Dict[str, Any]:
    return speculative_decoder.stats.to_dict()


def choose_node(
    region: Optional[str], model: Optional[str] = None
) -> Optional[NodeInfo]:
    """Latency-ranked node selection, with region and model filtering."""
    candidates = [n for n in nodes if n.last_latency_ms >= 0]

    if region:
        regional = [n for n in candidates if n.region == region]
        if regional:
            candidates = regional

    if model:
        with_model = [n for n in candidates if model in n.models]
        if with_model:
            candidates = with_model

    if not candidates:
        return None

    return min(candidates, key=lambda n: latency_tracker.score(n.node_id))


def choose_draft_node(region: Optional[str]) -> Optional[NodeInfo]:
    """Find the lowest-latency node that has a draft (small) model available."""
    candidates = [
        n
        for n in nodes
        if n.last_latency_ms >= 0 and len(n.draft_models) > 0
    ]
    if region:
        regional = [n for n in candidates if n.region == region]
        if regional:
            candidates = regional
    if not candidates:
        return None
    return min(candidates, key=lambda n: latency_tracker.score(n.node_id))


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _build_dashboard_html()


@app.get("/chat-ui", response_class=HTMLResponse)
async def chat_ui() -> str:
    return _build_chat_ui_html()


@app.post("/chat")
async def chat(request: Request) -> StreamingResponse:
    """MoE-first chat routing.

    Tier 1: MoE dispatch (2+ online experts) -- parallel expert query
    Tier 2: Single-node fallback (1 expert)  -- direct generation
    """
    payload = await request.json()
    prompt: str = payload.get("prompt", "")
    region: Optional[str] = payload.get("region")
    model: Optional[str] = payload.get("model")
    strategy_str: str = payload.get("strategy", "fastest")
    k_override: Optional[int] = payload.get("k")

    try:
        strategy = AggregationStrategy(strategy_str)
    except ValueError:
        strategy = AggregationStrategy.FASTEST

    experts = moe_coordinator.all_experts()
    online_ids = [n.node_id for n in nodes if n.last_latency_ms >= 0]

    k = k_override or expert_router.adaptive_k(prompt, len(online_ids))
    selected = expert_router.select_experts(
        prompt, experts, online_ids, k=k, region=region, model=model
    )

    if len(selected) >= 1:
        async def moe_stream():
            async for chunk in moe_coordinator.dispatch(
                prompt=prompt,
                model=model,
                selected_experts=selected,
                strategy=strategy,
                ws_connections=_ws_connections,
                ws_locks=_ws_locks,
                task_queues=_task_queues,
            ):
                yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk

        return StreamingResponse(
            moe_stream(), media_type="application/octet-stream"
        )

    node = choose_node(region, model)
    if not node:
        raise HTTPException(
            status_code=503, detail="No experts are currently registered"
        )

    if node.is_ws and node.node_id in _ws_connections:
        return await _chat_via_ws(node, prompt, model)
    else:
        return _chat_via_http(node, prompt, model)


def _chat_via_http(
    node: NodeInfo, prompt: str, model: Optional[str]
) -> StreamingResponse:
    infer_payload: Dict[str, Any] = {"prompt": prompt}
    if model:
        infer_payload["model"] = model

    async def stream_from_node():
        route_header = json.dumps(
            {
                "route": {
                    "node_id": node.node_id,
                    "host": node.host,
                    "port": node.port,
                    "model": model or "",
                    "mode": "single-node-http",
                    "latency_ms": round(
                        latency_tracker.ewma(node.node_id), 1
                    ),
                }
            }
        )
        yield (route_header + "\n").encode("utf-8")

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

    return StreamingResponse(
        stream_from_node(), media_type="application/octet-stream"
    )


async def _chat_via_ws(
    node: NodeInfo, prompt: str, model: Optional[str]
) -> StreamingResponse:
    ws = _ws_connections.get(node.node_id)
    lock = _ws_locks.get(node.node_id)
    if not ws or not lock:
        raise HTTPException(status_code=503, detail="Node disconnected")

    task_id = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    _task_queues[task_id] = queue

    try:
        async with lock:
            await ws.send_json(
                {
                    "type": "task",
                    "task_id": task_id,
                    "prompt": prompt,
                    "model": model or "",
                }
            )
    except Exception:
        _task_queues.pop(task_id, None)
        raise HTTPException(status_code=503, detail="Failed to reach node")

    async def stream_from_ws():
        route_header = json.dumps(
            {
                "route": {
                    "node_id": node.node_id,
                    "host": node.host,
                    "port": node.port,
                    "model": model or "",
                    "mode": "single-node-ws",
                    "latency_ms": round(
                        latency_tracker.ewma(node.node_id), 1
                    ),
                }
            }
        )
        yield (route_header + "\n").encode("utf-8")

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

    return StreamingResponse(
        stream_from_ws(), media_type="application/octet-stream"
    )


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
                        rtt = (time.perf_counter() - start) * 1000.0
                        node.last_heartbeat = time.time()
                        node.last_latency_ms = rtt
                        latency_tracker.record(node.node_id, rtt)
                    except Exception:
                        node.last_latency_ms = -1.0
                        latency_tracker.record_failure(node.node_id)
                else:
                    node.last_latency_ms = -1.0
            else:
                start = time.perf_counter()
                try:
                    resp = await client.get(
                        f"{node.base_url}/health", timeout=2.0
                    )
                    if resp.status_code == 200:
                        rtt = (time.perf_counter() - start) * 1000.0
                        node.last_heartbeat = time.time()
                        node.last_latency_ms = rtt
                        latency_tracker.record(node.node_id, rtt)
                    else:
                        node.last_latency_ms = -1.0
                        latency_tracker.record_failure(node.node_id)
                except Exception:
                    node.last_latency_ms = -1.0
                    latency_tracker.record_failure(node.node_id)

    return JSONResponse({"status": "ok", "count": len(nodes)})


@app.get("/health")
async def gateway_health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "nodes": len(nodes),
        "experts": len(moe_coordinator.all_experts()),
        "moe_stats": moe_coordinator.stats.to_dict(),
    }


# ──────────────────────────────────────────────────
#  Dashboard HTML
# ──────────────────────────────────────────────────

def _build_dashboard_html() -> str:
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
    main { display: grid; grid-template-columns: 1fr; gap: 1.5rem; padding: 1.5rem; }
    section { background: #020617; border-radius: 0.75rem; border: 1px solid #1f2937; padding: 1rem 1.25rem; }
    h2 { font-size: 0.95rem; margin: 0 0 0.75rem 0; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.08em; }
    table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
    th, td { padding: 0.35rem 0.5rem; text-align: left; }
    th { color: #9ca3af; border-bottom: 1px solid #1f2937; font-weight: 500; }
    tr:nth-child(even) { background: rgba(15,23,42,0.5); }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 0.78rem; }
    .badge { display: inline-flex; align-items: center; padding: 0.1rem 0.4rem; border-radius: 999px; font-size: 0.7rem; border: 1px solid #1f2937; background: #020617; color: #e5e7eb; }
    .badge.green { border-color: #15803d; color: #bbf7d0; }
    .badge.orange { border-color: #c2410c; color: #fed7aa; }
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
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }
    .stat-card { background: #0f1629; border: 1px solid #1f2937; border-radius: 0.5rem; padding: 0.75rem; }
    .stat-val { font-size: 1.4rem; font-weight: 600; color: #f97316; }
    .stat-label { font-size: 0.7rem; color: #6b7280; margin-top: 0.2rem; }
    .pipeline-stage { display: inline-flex; align-items: center; gap: 0.3rem; padding: 0.2rem 0.5rem; border: 1px solid #1f2937; border-radius: 0.4rem; font-size: 0.72rem; background: #0f1629; margin: 0.15rem; }
    .pipeline-arrow { color: #f97316; font-weight: bold; margin: 0 0.15rem; }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Microwave AI – Distributed Inference Dashboard</h1>
      <div class="small">Gateway v0.3.0 &middot; Mixture of Experts &middot; Parallel Dispatch</div>
    </div>
    <div class="small">
      <span class="pill" id="gatewayStatus"><span class="status-dot"></span>Gateway online</span>
    </div>
  </header>
  <main>
    <div class="grid-2">
      <div class="stat-card"><div class="stat-val" id="nodeCount">0</div><div class="stat-label">Online Experts</div></div>
      <div class="stat-card"><div class="stat-val" id="moeRequests">0</div><div class="stat-label">MoE Requests</div></div>
      <div class="stat-card"><div class="stat-val" id="avgExperts">--</div><div class="stat-label">Avg Experts/Request</div></div>
      <div class="stat-card"><div class="stat-val" id="avgLatency">--</div><div class="stat-label">Avg Response (ms)</div></div>
    </div>

    <section>
      <h2>Nodes (Latency-Ranked)</h2>
      <table>
        <thead>
          <tr>
            <th>ID</th><th>Region</th><th>Domains</th><th>Models</th>
            <th>VRAM</th><th>EWMA</th><th>Jitter</th><th>Score</th><th>Status</th>
          </tr>
        </thead>
        <tbody id="nodesTableBody">
          <tr><td colspan="9" class="small">Loading nodes...</td></tr>
        </tbody>
      </table>
      <div class="row">
        <button class="btn-secondary btn" onclick="refreshNodes()">Refresh</button>
        <button class="btn-secondary btn" onclick="pingNodes()">Ping</button>
        <span class="small" id="nodesMeta"></span>
      </div>
    </section>

    <section>
      <h2>Expert Registry</h2>
      <div id="expertsContainer" class="small">No experts registered.</div>
      <div class="row">
        <button class="btn-secondary btn" onclick="refreshExperts()">Refresh</button>
      </div>
    </section>

    <section>
      <h2>Chat</h2>
      <div class="label">Prompt</div>
      <textarea id="promptInput" placeholder="Ask Microwave AI anything..."></textarea>
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
        <button class="btn" id="sendBtn" onclick="sendChat()">Send</button>
        <span class="small" id="chatStatus"></span>
      </div>
      <div class="label" style="margin-top: 0.9rem;">Response</div>
      <div id="chatWindow" style="border-radius:0.5rem;border:1px solid #1f2937;background:#020617;padding:0.6rem;max-height:260px;overflow-y:auto;white-space:pre-wrap;">
        <div class="small" style="color:#6b7280;">Messages will appear here.</div>
      </div>
    </section>
  </main>
  <script>
    async function refreshNodes() {
      const body = document.getElementById('nodesTableBody');
      const meta = document.getElementById('nodesMeta');
      try {
        const res = await fetch('/nodes');
        const data = await res.json();
        document.getElementById('nodeCount').textContent = data.filter(n => n.online).length;
        if (!data.length) {
          body.innerHTML = '<tr><td colspan="9" class="small">No experts.</td></tr>';
          meta.textContent = '0 experts';
          return;
        }
        data.sort((a, b) => a.score - b.score);
        body.innerHTML = '';
        for (const n of data) {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><code>${n.node_id}</code></td>
            <td>${n.region} ${n.latitude ? '(' + n.latitude.toFixed(1) + ',' + n.longitude.toFixed(1) + ')' : ''}</td>
            <td>${(n.expert_domains || ['general']).map(d => '<span class="badge">' + d + '</span>').join(' ')}</td>
            <td>${(n.models || []).map(m => '<span class="badge">' + m + '</span>').join(' ')}</td>
            <td>${n.vram_mb ? n.vram_mb + ' MB' : '--'}</td>
            <td>${n.ewma_ms >= 0 ? n.ewma_ms.toFixed(1) + ' ms' : '--'}</td>
            <td>${n.jitter_ms >= 0 ? n.jitter_ms.toFixed(1) + ' ms' : '--'}</td>
            <td><strong>${n.score < 99999 ? n.score.toFixed(1) : 'inf'}</strong></td>
            <td>
              <span class="badge ${n.online ? 'green' : ''}">
                <span class="status-dot ${n.online ? '' : 'offline'}"></span>
                ${n.online ? 'Online' : 'Offline'}
              </span>
            </td>
          `;
          body.appendChild(tr);
        }
        meta.textContent = data.length + ' expert' + (data.length === 1 ? '' : 's');
      } catch (e) {
        body.innerHTML = '<tr><td colspan="9" class="small">Error loading.</td></tr>';
      }
    }

    async function refreshExperts() {
      const container = document.getElementById('expertsContainer');
      try {
        const res = await fetch('/experts');
        const data = await res.json();
        if (!data.length) {
          container.textContent = 'No experts registered.';
          return;
        }
        container.innerHTML = '';
        for (const e of data) {
          const div = document.createElement('div');
          div.style.marginBottom = '0.5rem';
          const domains = (e.domains || []).map(d =>
            '<span class="pipeline-stage">' + d + '</span>'
          ).join(' ');
          div.innerHTML = '<div><code>' + e.node_id + '</code> &middot; ' + (e.models||[]).join(', ') + ' &middot; <strong>' + e.latency_ms.toFixed(1) + ' ms</strong></div><div style="margin-top:0.2rem;">' + domains + '</div>';
          container.appendChild(div);
        }
      } catch (e) {
        container.textContent = 'Error loading experts.';
      }
    }

    async function refreshMoEStats() {
      try {
        const res = await fetch('/health');
        const s = await res.json();
        const m = s.moe_stats || {};
        document.getElementById('moeRequests').textContent = m.total_requests || 0;
        document.getElementById('avgExperts').textContent =
          m.total_requests > 0 ? m.avg_experts_per_request.toFixed(1) : '--';
        document.getElementById('avgLatency').textContent =
          m.total_requests > 0 ? m.avg_response_ms.toFixed(0) : '--';
      } catch (e) {}
    }

    async function pingNodes() {
      document.getElementById('nodesMeta').textContent = 'Pinging...';
      try { await fetch('/nodes/health', { method: 'POST' }); } catch (e) {}
      await refreshNodes();
    }

    async function sendChat() {
      const prompt = document.getElementById('promptInput').value.trim();
      if (!prompt) return;
      const chatWindow = document.getElementById('chatWindow');
      const status = document.getElementById('chatStatus');
      const btn = document.getElementById('sendBtn');
      chatWindow.textContent = '';
      status.textContent = 'Routing to experts...';
      btn.disabled = true;
      try {
        const res = await fetch('/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            prompt,
            region: document.getElementById('regionInput').value || null,
            model: document.getElementById('modelInput').value || null,
            strategy: 'fastest',
          }),
        });
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = '', full = '';
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\\n');
          buf = lines.pop();
          for (const line of lines) {
            if (!line.trim()) continue;
            try {
              const obj = JSON.parse(line);
              if (obj.response) { full += obj.response; chatWindow.textContent = full; }
              if (obj.route) {
                const experts = (obj.route.experts || []).map(e => e.node_id).join(', ');
                status.textContent = 'MoE (' + obj.route.strategy + ') via ' + experts;
              }
            } catch (e) { full += line; chatWindow.textContent = full; }
          }
        }
        status.textContent = 'Done.';
      } catch (e) { status.textContent = 'Error: ' + e.message; }
      finally { btn.disabled = false; }
    }

    document.getElementById('promptInput').addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
    });

    refreshNodes();
    refreshExperts();
    refreshMoEStats();
    setInterval(() => { refreshNodes(); refreshExperts(); refreshMoEStats(); }, 10000);
  </script>
</body>
</html>
    """


def _build_chat_ui_html() -> str:
    return r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Microwave AI</title>
  <style>
    :root {
      --bg: #ffffff;
      --panel: #f5f5f5;
      --border: #e0e0e0;
      --text: #111;
      --text-muted: #6b7280;
      --input-bg: #f5f5f5;
      --btn-bg: #111;
      --btn-text: #fff;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      height: 100vh;
      display: flex;
      overflow: hidden;
    }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }

    #sidebar {
      width: 220px; min-width: 220px;
      background: var(--panel);
      display: flex; flex-direction: column;
      padding: 1rem 0.6rem;
      border-right: 1px solid var(--border);
      gap: 0.4rem;
    }
    .brand { padding: 0 0.4rem 0.5rem; border-bottom: 1px solid var(--border); margin-bottom: 0.3rem; }
    .brand h1 { font-size: 0.9rem; font-weight: 600; }
    .brand p { font-size: 0.72rem; color: var(--text-muted); margin-top: 0.15rem; }

    #newChatBtn {
      display: flex; align-items: center; gap: 0.5rem;
      padding: 0.55rem 0.65rem; border-radius: 0.5rem; cursor: pointer;
      font-size: 0.84rem; color: var(--text); background: transparent;
      border: 1px solid var(--border); width: 100%; text-align: left;
    }
    #newChatBtn:hover { background: var(--bg); border-color: #999; }

    .history-label { font-size: 0.66rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.08em; padding: 0.5rem 0.6rem 0.2rem; }
    .history-item { padding: 0.45rem 0.6rem; border-radius: 0.5rem; font-size: 0.82rem; color: var(--text-muted); cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .history-item:hover { background: var(--bg); color: var(--text); }
    .history-item.active { background: var(--bg); color: var(--text); border: 1px solid var(--border); }
    #historyList { overflow-y: auto; padding-bottom: 0.3rem; }

    #chatArea { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
    #topBar {
      border-bottom: 1px solid var(--border);
      background: var(--bg);
      padding: 0.6rem 1rem; display: flex; align-items: center; justify-content: space-between; gap: 0.6rem;
    }
    .top-title { font-size: 0.84rem; color: var(--text-muted); }
    .pill-row { display: flex; align-items: center; gap: 0.35rem; flex-wrap: wrap; }
    .pill { border: 1px solid var(--border); background: var(--panel); color: var(--text-muted); border-radius: 999px; padding: 0.18rem 0.5rem; font-size: 0.7rem; }

    #messages { flex: 1; overflow-y: auto; padding: 1rem 0 1.5rem; display: flex; flex-direction: column; gap: 1rem; }
    .msg-row { display: flex; flex-direction: column; padding: 0 10%; }
    .msg-row.user { align-items: flex-end; }
    .msg-row.bot  { align-items: flex-start; }
    .meta { margin-bottom: 0.25rem; font-size: 0.68rem; color: var(--text-muted); display: flex; align-items: center; gap: 0.35rem; }
    .meta .who { color: var(--text); }
    .bubble { max-width: min(720px, 80%); font-size: 0.9rem; line-height: 1.55; }
    .msg-row.user .bubble { background: var(--text); color: var(--btn-text); border-radius: 0.85rem 0.85rem 0.2rem 0.85rem; padding: 0.55rem 0.85rem; }
    .msg-row.bot .bubble { background: var(--panel); border: 1px solid var(--border); border-radius: 0.85rem 0.85rem 0.85rem 0.2rem; padding: 0.55rem 0.85rem; color: var(--text); }
    .msg-row.bot .bubble p { margin: 0.4rem 0; }
    .msg-row.bot .bubble p:first-child { margin-top: 0; }
    .msg-row.bot .bubble p:last-child { margin-bottom: 0; }
    .msg-row.bot .bubble h1, .msg-row.bot .bubble h2, .msg-row.bot .bubble h3 { margin: 0.6rem 0 0.35rem; line-height: 1.25; }
    .msg-row.bot .bubble ul, .msg-row.bot .bubble ol { margin: 0.35rem 0 0.35rem 1.25rem; }
    .msg-row.bot .bubble li { margin: 0.2rem 0; }
    .msg-row.bot .bubble pre { margin: 0.5rem 0; padding: 0.6rem; border-radius: 0.45rem; background: #0a0d14; border: 1px solid var(--border); overflow-x: auto; }
    .msg-row.bot .bubble code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.85em; background: rgba(255,255,255,0.06); padding: 0.08rem 0.26rem; border-radius: 0.25rem; }
    .msg-row.bot .bubble pre code { background: transparent; padding: 0; border-radius: 0; }
    .msg-row.bot .bubble a { color: var(--brand); text-decoration: underline; }
    .msg-row.bot .bubble blockquote { margin: 0.5rem 0; padding-left: 0.7rem; border-left: 3px solid var(--border); color: var(--text-muted); }
    .bubble.loading { border-style: dashed; color: var(--text-muted); }
    .loading-dots { display: inline-flex; gap: 0.25rem; }
    .loading-dots span { width: 4px; height: 4px; border-radius: 50%; background: var(--text-muted); animation: blink 1s ease-in-out infinite; }
    .loading-dots span:nth-child(2) { animation-delay: 0.15s; }
    .loading-dots span:nth-child(3) { animation-delay: 0.3s; }
    @keyframes blink { 0%, 100% { opacity: 0.3; } 50% { opacity: 1; } }

    #emptyState {
      flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
      gap: 1rem; color: var(--text); font-size: 0.95rem; padding: 1rem 1rem 4rem; text-align: center;
    }
    .ascii-microwave {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 1rem; line-height: 1.15; letter-spacing: 0.02em;
      color: var(--text); white-space: pre; text-align: center; margin: 0.5rem 0;
    }
    .loading-copy { font-size: 0.8rem; color: var(--text-muted); margin-left: 0.5rem; }
    #emptyState .lead { font-size: 1.1rem; font-weight: 600; margin-top: 0.25rem; }
    #emptyState .sub { font-size: 0.78rem; color: var(--text-muted); }
    #suggestions { margin-top: 1.2rem; display: flex; flex-wrap: wrap; gap: 0.45rem; justify-content: center; max-width: 600px; }
    .suggestion { border: 1px solid var(--border); background: var(--panel); color: var(--text); border-radius: 999px; font-size: 0.78rem; padding: 0.35rem 0.7rem; cursor: pointer; }
    .suggestion:hover { background: var(--border); }

    #inputArea { padding: 0.6rem 10% 1rem; }
    #inputShell { background: var(--input-bg); border-radius: 0.75rem; padding: 0.6rem 0.6rem 0.55rem 0.9rem; display: flex; flex-direction: column; gap: 0.45rem; border: 1px solid var(--border); }
    #promptInput { background: transparent; border: none; outline: none; color: var(--text); font-size: 0.9rem; resize: none; min-height: 24px; max-height: 160px; overflow-y: auto; line-height: 1.5; }
    #promptInput::placeholder { color: var(--text-muted); }
    .input-footer { display: flex; align-items: center; justify-content: space-between; }
    .input-left { display: flex; gap: 0.35rem; align-items: center; }
    .model-select { background: var(--bg); border: 1px solid var(--border); color: var(--text); font-size: 0.74rem; padding: 0.25rem 0.5rem; border-radius: 0.4rem; cursor: pointer; outline: none; }
    #statusText { color: var(--text-muted); font-size: 0.71rem; min-height: 1.1em; }
    #sendBtn { min-width: 34px; height: 34px; padding: 0 0.6rem; border-radius: 50%; background: var(--btn-bg); color: var(--btn-text); border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
    #sendBtn:disabled { opacity: 0.4; cursor: default; }
    #sendBtn:hover:not(:disabled) { opacity: 0.9; }

    @media (max-width: 920px) {
      #sidebar { display: none; }
      .msg-row { padding: 0 5%; }
      #inputArea { padding: 0.6rem 5% 1rem; }
    }
  </style>
</head>
<body>
  <div id="sidebar">
    <div class="brand"><h1>Microwave AI</h1><p>Distributed inference</p></div>
    <button id="newChatBtn">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>
      New Chat
    </button>
    <div class="history-label">Today</div>
    <div id="historyList"></div>
  </div>

  <div id="chatArea">
    <div id="topBar">
      <span class="top-title">Microwave AI</span>
      <div class="pill-row">
        <span class="pill mono" id="routePill">POST /chat</span>
        <span class="pill mono">region LAN</span>
        <span class="pill mono" id="activeModelTag">model llama3.2</span>
      </div>
    </div>
    <div id="messages">
      <div id="emptyState">
        <pre class="ascii-microwave">     ________________
    |.-----------.   |
    ||   _____   |ooo|
    ||  |     |  |ooo|
    ||  |     |  | = |
    ||  '-----'  | _ |
    ||___________|[_]|
    '----------------'</pre>
        <div class="lead">Microwave</div>
        <div class="sub">Ask anything — routes to the best nodes</div>
        <div id="suggestions">
          <button class="suggestion">Explain how Microwave AI routing works</button>
          <button class="suggestion">Write a quick microwave mug cake recipe</button>
          <button class="suggestion">Summarize distributed inference in 5 bullets</button>
          <button class="suggestion">Generate Python code for a websocket client</button>
        </div>
      </div>
    </div>
    <div id="inputArea">
      <div id="inputShell">
        <textarea id="promptInput" rows="1" placeholder="Send a message"></textarea>
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
  const routePill = document.getElementById('routePill');
  const suggestionButtons = Array.from(document.querySelectorAll('.suggestion'));

  let sessions = [], activeIdx = -1, isSending = false;

  function newSession() { sessions.unshift({ title: null, messages: [] }); activeIdx = 0; renderHistory(); renderMessages(); }
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
    if (!s || !s.messages.length) { messagesEl.innerHTML = ''; messagesEl.appendChild(emptyState); return; }
    messagesEl.innerHTML = '';
    s.messages.forEach(m => appendBubble(m.role, m.text, { model: m.model, time: m.time }));
  }
  function nowStamp() { return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }

  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function renderMarkdown(md) {
    let html = escapeHtml(md);

    // Fenced code blocks
    html = html.replace(/```([a-zA-Z0-9_-]+)?\n([\s\S]*?)```/g, (_m, _lang, code) =>
      '<pre><code>' + code.trimEnd() + '</code></pre>'
    );

    // Inline code
    html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');

    // Headings
    html = html.replace(/^### (.*)$/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.*)$/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.*)$/gm, '<h1>$1</h1>');

    // Blockquotes
    html = html.replace(/^> (.*)$/gm, '<blockquote>$1</blockquote>');

    // Bold / italic
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');

    // Links
    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

    // Unordered lists
    html = html.replace(/(?:^|\n)((?:[-*] .*(?:\n|$))+)/g, (m, block) => {
      const items = block.trim().split('\n').map(line => line.replace(/^[-*] /, '').trim());
      return '\n<ul>' + items.map(i => '<li>' + i + '</li>').join('') + '</ul>\n';
    });

    // Ordered lists
    html = html.replace(/(?:^|\n)((?:\d+\. .*(?:\n|$))+)/g, (m, block) => {
      const items = block.trim().split('\n').map(line => line.replace(/^\d+\. /, '').trim());
      return '\n<ol>' + items.map(i => '<li>' + i + '</li>').join('') + '</ol>\n';
    });

    // Paragraphs for remaining lines
    html = html
      .split('\n\n')
      .map(chunk => {
        const t = chunk.trim();
        if (!t) return '';
        if (/^<(h1|h2|h3|ul|ol|pre|blockquote)/.test(t)) return t;
        return '<p>' + t.replace(/\n/g, '<br>') + '</p>';
      })
      .join('');

    return html;
  }

  function appendBubble(role, text, opts = {}) {
    const row = document.createElement('div'); row.className = 'msg-row ' + role;
    const meta = document.createElement('div'); meta.className = 'meta';
    meta.innerHTML = '<span class="who">' + (role === 'user' ? 'You' : 'Microwave AI') + (opts.model ? ' &middot; ' + opts.model : '') + '</span><span>' + (opts.time || nowStamp()) + '</span>';
    row.appendChild(meta);
    const bubble = document.createElement('div'); bubble.className = 'bubble';
    if (role === 'bot') {
      bubble.innerHTML = renderMarkdown(text);
    } else {
      bubble.textContent = text;
    }
    row.appendChild(bubble);
    messagesEl.appendChild(row);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return { row, bubble };
  }
  function createLoadingBubble() {
    const { row, bubble } = appendBubble('bot', '', { streaming: true, model: modelSelect.value });
    bubble.classList.add('loading');
    bubble.innerHTML = '<div class="loading-dots"><span></span><span></span><span></span></div> <span class="loading-copy">Heating...</span>';
    return { row, bubble };
  }
  function updateSendBtn() { sendBtn.disabled = isSending || !promptEl.value.trim().length; }
  promptEl.addEventListener('input', () => { promptEl.style.height = 'auto'; promptEl.style.height = Math.min(promptEl.scrollHeight, 160) + 'px'; updateSendBtn(); });
  promptEl.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSend(); } });
  sendBtn.addEventListener('click', doSend);
  modelSelect.addEventListener('change', () => { activeModelTag.textContent = 'model ' + modelSelect.value; });
  suggestionButtons.forEach(btn => btn.addEventListener('click', () => { promptEl.value = btn.textContent; promptEl.dispatchEvent(new Event('input')); promptEl.focus(); }));

  async function doSend() {
    const prompt = promptEl.value.trim();
    if (!prompt || isSending) return;
    if (activeIdx === -1 || !sessions.length) newSession();
    const s = sessions[activeIdx];
    const ts = nowStamp();
    s.messages.push({ role: 'user', text: prompt, model: null, time: ts });
    if (!s.title) { s.title = prompt.slice(0, 32) + (prompt.length > 32 ? '...' : ''); renderHistory(); }
    if (messagesEl.contains(emptyState)) messagesEl.removeChild(emptyState);
    appendBubble('user', prompt, { time: ts });
    promptEl.value = ''; promptEl.style.height = 'auto';
    isSending = true; sendBtn.classList.add('sending'); statusText.textContent = 'Heating...'; updateSendBtn();
    const loading = createLoadingBubble();
    let fullText = '', converted = false;
    try {
      const res = await fetch('/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt, region: 'LAN', model: modelSelect.value }) });
      if (!res.ok || !res.body) { loading.bubble.classList.remove('loading'); loading.bubble.textContent = 'Error: ' + res.status; return; }
      const reader = res.body.getReader(); const decoder = new TextDecoder(); let buf = '';
      while (true) {
        const { value, done } = await reader.read(); if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n'); buf = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const obj = JSON.parse(line);
            if (obj.route) {
              const experts = (obj.route.experts || []).map(e => e.node_id).join(', ');
              routePill.textContent = (obj.route.mode || 'direct') + ' (' + (obj.route.strategy || '?') + ') via ' + (experts || obj.route.node_id || '?');
            }
            if (typeof obj.response === 'string') {
              if (!converted) { converted = true; loading.bubble.classList.remove('loading'); loading.bubble.innerHTML = ''; }
              fullText += obj.response; loading.bubble.innerHTML = renderMarkdown(fullText); messagesEl.scrollTop = messagesEl.scrollHeight;
            }
          } catch (e) { if (!converted) { converted = true; loading.bubble.classList.remove('loading'); loading.bubble.innerHTML = ''; } fullText += line; loading.bubble.innerHTML = renderMarkdown(fullText); }
        }
      }
      if (!converted) { loading.bubble.classList.remove('loading'); loading.bubble.innerHTML = renderMarkdown(fullText || 'No response.'); }
      s.messages.push({ role: 'bot', text: fullText || 'No response.', model: modelSelect.value, time: nowStamp() });
      statusText.textContent = 'Served by Microwave network';
    } catch (e) { loading.bubble.classList.remove('loading'); loading.bubble.textContent = 'Error: ' + e.message; statusText.textContent = 'Error'; }
    finally { isSending = false; sendBtn.classList.remove('sending'); updateSendBtn(); promptEl.focus(); }
  }
  document.getElementById('newChatBtn').addEventListener('click', () => { newSession(); promptEl.focus(); });
  newSession(); activeModelTag.textContent = 'model ' + modelSelect.value; promptEl.focus();
</script>
</body>
</html>
    """


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(description="Microwave AI Gateway")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument(
        "--max-region-km",
        type=float,
        default=2000.0,
        help="Max distance (km) for expert region filtering",
    )
    parser.add_argument(
        "--default-k",
        type=int,
        default=2,
        help="Default number of MoE experts per request",
    )
    args = parser.parse_args()

    region_engine.max_distance_km = args.max_region_km
    moe_coordinator.default_k = args.default_k
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
