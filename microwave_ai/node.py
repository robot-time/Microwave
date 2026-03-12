import argparse
import asyncio
import json
import os
import platform
import ssl
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

from . import __version__

try:
    import psutil
except ImportError:
    psutil = None

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]


app = FastAPI(title="Microwave AI Node")

GATEWAY_URL = os.getenv("MICROWAVE_GATEWAY_URL")
NODE_ID = os.getenv("MICROWAVE_NODE_ID", platform.node())
REGION = os.getenv("MICROWAVE_REGION", "LAN")
MODEL = os.getenv("MICROWAVE_MODEL", "llama3")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
LATITUDE = float(os.getenv("MICROWAVE_LAT", "0.0"))
LONGITUDE = float(os.getenv("MICROWAVE_LON", "0.0"))
ENGINE_TYPE = os.getenv("MICROWAVE_ENGINE", "ollama")
DRAFT_MODELS = os.getenv("MICROWAVE_DRAFT_MODELS", "").split(",")
DRAFT_MODELS = [m.strip() for m in DRAFT_MODELS if m.strip()]
EXPERT_DOMAINS = os.getenv("MICROWAVE_EXPERT_DOMAINS", "general").split(",")
EXPERT_DOMAINS = [d.strip() for d in EXPERT_DOMAINS if d.strip()]

CYAN = "\033[1;36m"
GREEN = "\033[1;32m"
YELLOW = "\033[1;33m"
DIM = "\033[2m"
RESET = "\033[0m"

_inference_engine = None


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
    print(f"Microwave Network (node) v{__version__}")


def _detect_capabilities() -> Dict[str, Any]:
    """Detect hardware capabilities: RAM, VRAM, compute benchmark."""
    caps: Dict[str, Any] = {
        "ram_mb": 0,
        "vram_mb": 0,
        "compute_score": 0.0,
    }

    if psutil is not None:
        try:
            caps["ram_mb"] = int(psutil.virtual_memory().total / (1024 * 1024))
        except Exception:
            pass

    caps["vram_mb"] = _detect_vram()
    caps["compute_score"] = _run_compute_benchmark()
    return caps


def _detect_vram() -> int:
    """Attempt to detect GPU VRAM in MB."""
    try:
        import subprocess

        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            return sum(int(l.strip()) for l in lines if l.strip().isdigit())
    except Exception:
        pass

    try:
        import subprocess

        result = subprocess.run(
            ["system_profiler", "SPDisplaysDataType"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.split("\n"):
            if "VRAM" in line or "Memory" in line:
                parts = line.split(":")
                if len(parts) == 2:
                    val = parts[1].strip().split()[0]
                    if val.isdigit():
                        return int(val) * 1024
    except Exception:
        pass

    return 0


def _run_compute_benchmark() -> float:
    """Quick matrix-multiply benchmark returning approximate tokens/sec estimate."""
    if np is None:
        return 0.0
    try:
        size = 512
        a = np.random.randn(size, size).astype(np.float32)
        b = np.random.randn(size, size).astype(np.float32)
        start = time.perf_counter()
        for _ in range(10):
            np.dot(a, b)
        elapsed = time.perf_counter() - start
        ops = 10 * 2 * size**3
        gflops = ops / elapsed / 1e9
        return round(gflops * 2.0, 1)
    except Exception:
        return 0.0


async def _geolocate_self() -> Tuple[float, float]:
    """Try to auto-detect lat/lon via IP geolocation."""
    if LATITUDE != 0.0 or LONGITUDE != 0.0:
        return (LATITUDE, LONGITUDE)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "http://ip-api.com/json/", params={"fields": "lat,lon,status"}
            )
            data = resp.json()
            if data.get("status") == "success":
                return (float(data["lat"]), float(data["lon"]))
    except Exception:
        pass
    return (0.0, 0.0)


# ── HTTP mode endpoints ──

@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok"}


@app.post("/infer")
async def infer(request: Request) -> StreamingResponse:
    payload = await request.json()
    prompt: str = payload.get("prompt", "")
    model: str = payload.get("model") or MODEL

    async def stream_llm():
        async with httpx.AsyncClient(timeout=None) as client:
            ollama_req = {"model": model, "prompt": prompt, "stream": True}
            async with client.stream(
                "POST", f"{OLLAMA_URL}/api/generate", json=ollama_req
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    yield chunk

    return StreamingResponse(stream_llm(), media_type="application/octet-stream")


async def register_with_gateway(gateway_url: str, host: str, port: int) -> None:
    if not gateway_url:
        return

    lat, lon = await _geolocate_self()
    caps = _detect_capabilities()

    async with httpx.AsyncClient() as client:
        try:
            await client.post(
                f"{gateway_url.rstrip('/')}/nodes/register",
                json={
                    "node_id": NODE_ID,
                    "host": host,
                    "port": port,
                    "region": REGION,
                    "models": [MODEL],
                    "metadata": {"version": __version__},
                    "latitude": lat,
                    "longitude": lon,
                    "vram_mb": caps["vram_mb"],
                    "ram_mb": caps["ram_mb"],
                    "compute_score": caps["compute_score"],
                    "engine_type": ENGINE_TYPE,
                    "draft_models": DRAFT_MODELS,
                    "expert_domains": EXPERT_DOMAINS,
                },
                timeout=5.0,
            )
        except Exception:
            pass


# ── Reverse (WebSocket) mode ──

async def _process_task(task_id: str, prompt: str, model: str, ws) -> None:
    """Call Ollama and stream chunks back over the WebSocket."""
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            ollama_req = {"model": model, "prompt": prompt, "stream": True}
            async with client.stream(
                "POST", f"{OLLAMA_URL}/api/generate", json=ollama_req
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "chunk",
                                    "task_id": task_id,
                                    "data": chunk.decode("utf-8", errors="replace"),
                                }
                            )
                        )
    except Exception as e:
        await ws.send(
            json.dumps(
                {
                    "type": "chunk",
                    "task_id": task_id,
                    "data": json.dumps({"error": str(e)}),
                }
            )
        )
    finally:
        await ws.send(json.dumps({"type": "done", "task_id": task_id}))


async def _process_moe_expert(task_id: str, prompt: str, model: str, ws) -> None:
    """Handle an MoE expert task: generate a full response and stream chunks back.

    Uses moe_expert_chunk / moe_expert_done message types so the gateway's
    MoE coordinator can aggregate responses from multiple experts in parallel.
    """
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            ollama_req = {"model": model, "prompt": prompt, "stream": True}
            async with client.stream(
                "POST", f"{OLLAMA_URL}/api/generate", json=ollama_req
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        await ws.send(
                            json.dumps({
                                "type": "moe_expert_chunk",
                                "task_id": task_id,
                                "data": chunk.decode("utf-8", errors="replace"),
                            })
                        )
    except Exception as e:
        await ws.send(
            json.dumps({
                "type": "moe_expert_chunk",
                "task_id": task_id,
                "data": json.dumps({"error": str(e)}),
            })
        )
    finally:
        await ws.send(
            json.dumps({"type": "moe_expert_done", "task_id": task_id})
        )


async def _process_draft_generate(
    task_id: str, prompt: str, model: str, num_tokens: int, ws
) -> None:
    """Generate draft tokens for speculative decoding."""
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            ollama_req = {
                "model": model,
                "prompt": prompt,
                "stream": True,
                "options": {"num_predict": num_tokens},
            }
            async with client.stream(
                "POST", f"{OLLAMA_URL}/api/generate", json=ollama_req
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        token = obj.get("response", "")
                        if token:
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "draft_result",
                                        "task_id": task_id,
                                        "token": token,
                                        "done": obj.get("done", False),
                                    }
                                )
                            )
                        if obj.get("done"):
                            break
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        await ws.send(
            json.dumps(
                {
                    "type": "draft_result",
                    "task_id": task_id,
                    "token": "",
                    "done": True,
                    "error": str(e),
                }
            )
        )
    finally:
        await ws.send(
            json.dumps({"type": "draft_result", "task_id": task_id, "done": True})
        )


async def _process_verify_tokens(
    task_id: str,
    context: str,
    draft_tokens: List[str],
    model: str,
    ws,
) -> None:
    """Verify draft tokens by running the full model on the extended context.

    In a simplified verification: run the large model on the full context
    (context + all draft tokens) and compare. For now we accept all tokens
    if the model produces similar output, as full logprob-based verification
    requires deeper Ollama API integration.
    """
    full_prompt = context + "".join(draft_tokens)
    verified: List[str] = []
    correction: Optional[str] = None

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            ollama_req = {
                "model": model,
                "prompt": full_prompt,
                "stream": True,
                "options": {"num_predict": len(draft_tokens) + 1},
            }
            target_tokens: List[str] = []
            async with client.stream(
                "POST", f"{OLLAMA_URL}/api/generate", json=ollama_req
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        tok = obj.get("response", "")
                        if tok:
                            target_tokens.append(tok)
                        if obj.get("done"):
                            break
                    except json.JSONDecodeError:
                        continue

            for i, draft_tok in enumerate(draft_tokens):
                if i < len(target_tokens) and target_tokens[i] == draft_tok:
                    verified.append(draft_tok)
                else:
                    if i < len(target_tokens):
                        correction = target_tokens[i]
                    break
            else:
                if len(target_tokens) > len(draft_tokens):
                    correction = target_tokens[len(draft_tokens)]

    except Exception:
        verified = draft_tokens

    await ws.send(
        json.dumps(
            {
                "type": "verify_result",
                "task_id": task_id,
                "accepted": verified,
                "correction": correction,
            }
        )
    )


async def _process_pipeline_start(
    task_id: str, prompt: str, model: str, max_tokens: int, pipeline_config: Dict, ws
) -> None:
    """Handle pipeline inference when this node is stage 0.

    In the current implementation, each pipeline stage runs the full model
    via Ollama (simplified pipeline mode). True layer-split execution
    requires the LlamaCppEngine and is activated when engine_type != 'ollama'.
    """
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            ollama_req = {
                "model": model,
                "prompt": prompt,
                "stream": True,
                "options": {"num_predict": max_tokens},
            }
            async with client.stream(
                "POST", f"{OLLAMA_URL}/api/generate", json=ollama_req
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        token = obj.get("response", "")
                        if token:
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "pipeline_token",
                                        "task_id": task_id,
                                        "token": token,
                                    }
                                )
                            )
                        if obj.get("done"):
                            break
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        await ws.send(
            json.dumps(
                {
                    "type": "pipeline_token",
                    "task_id": task_id,
                    "token": f"\n[Pipeline error: {e}]",
                }
            )
        )
    finally:
        await ws.send(
            json.dumps({"type": "pipeline_done", "task_id": task_id})
        )


async def _measure_peer(target_node_id: str, gateway_ws_url: str, ws) -> None:
    """Measure RTT to a peer node via the gateway's relay (simplified).

    A full implementation would have nodes connect directly to each other.
    This simplified version measures round-trip through the gateway.
    """
    start = time.perf_counter()
    try:
        await ws.send(json.dumps({"type": "ping"}))
        rtt = (time.perf_counter() - start) * 1000.0
    except Exception:
        rtt = -1.0

    await ws.send(
        json.dumps(
            {
                "type": "peer_measurement",
                "target_node_id": target_node_id,
                "rtt_ms": rtt,
            }
        )
    )


async def _ws_listener(gateway_url: str) -> None:
    """Connect to gateway via WebSocket and wait for tasks. Auto-reconnects."""
    import websockets

    ws_url = gateway_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url.rstrip('/')}/nodes/ws"
    insecure_tls = os.getenv("MICROWAVE_INSECURE_TLS", "").lower() in (
        "1",
        "true",
        "yes",
    )
    warned_about_tls = False

    lat, lon = await _geolocate_self()
    caps = _detect_capabilities()

    while True:
        try:
            connect_kwargs: Dict[str, Any] = {
                "ping_interval": 20,
                "ping_timeout": 60,
                "max_size": 64 * 1024 * 1024,
            }
            if ws_url.startswith("wss://") and insecure_tls:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                connect_kwargs["ssl"] = ctx

            async with websockets.connect(ws_url, **connect_kwargs) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "register",
                            "node_id": NODE_ID,
                            "region": REGION,
                            "models": [MODEL],
                            "metadata": {"version": __version__},
                            "latitude": lat,
                            "longitude": lon,
                            "vram_mb": caps["vram_mb"],
                            "ram_mb": caps["ram_mb"],
                            "compute_score": caps["compute_score"],
                            "engine_type": ENGINE_TYPE,
                            "draft_models": DRAFT_MODELS,
                            "expert_domains": EXPERT_DOMAINS,
                        }
                    )
                )

                ack = json.loads(await ws.recv())
                if ack.get("type") == "registered":
                    print(
                        f"Registered with gateway as '{ack.get('node_id')}' "
                        f"(reverse/WS, expert)"
                    )
                    print(f"  Region:  {REGION}  Lat/Lon: {lat:.2f},{lon:.2f}")
                    print(f"  Domains: {EXPERT_DOMAINS}")
                    print(
                        f"  VRAM: {caps['vram_mb']}MB  RAM: {caps['ram_mb']}MB  "
                        f"Compute: {caps['compute_score']} est.tok/s"
                    )
                    if DRAFT_MODELS:
                        print(f"  Draft models: {DRAFT_MODELS}")
                    print(
                        f"{GREEN}Ready. Expert node online and listening for tasks.{RESET}"
                    )

                async for raw in ws:
                    msg = json.loads(raw)
                    msg_type = msg.get("type")

                    if msg_type == "task":
                        task_id = msg["task_id"]
                        prompt = msg.get("prompt", "")
                        model = msg.get("model") or MODEL
                        print(f"{DIM}[task {task_id[:8]}] {prompt[:60]}{RESET}")
                        asyncio.create_task(
                            _process_task(task_id, prompt, model, ws)
                        )

                    elif msg_type == "ping":
                        await ws.send(json.dumps({"type": "pong"}))

                    elif msg_type == "draft_generate":
                        task_id = msg["task_id"]
                        prompt = msg.get("prompt", "")
                        model = msg.get("model") or (DRAFT_MODELS[0] if DRAFT_MODELS else MODEL)
                        num_tokens = msg.get("num_tokens", 5)
                        print(
                            f"{DIM}[draft {task_id[:8]}] k={num_tokens} {prompt[-40:]}{RESET}"
                        )
                        asyncio.create_task(
                            _process_draft_generate(
                                task_id, prompt, model, num_tokens, ws
                            )
                        )

                    elif msg_type == "verify_tokens":
                        task_id = msg["task_id"]
                        context = msg.get("context", "")
                        draft_tokens = msg.get("draft_tokens", [])
                        model = msg.get("model") or MODEL
                        print(
                            f"{DIM}[verify {task_id[:8]}] {len(draft_tokens)} tokens{RESET}"
                        )
                        asyncio.create_task(
                            _process_verify_tokens(
                                task_id, context, draft_tokens, model, ws
                            )
                        )

                    elif msg_type == "pipeline_start":
                        task_id = msg["task_id"]
                        prompt = msg.get("prompt", "")
                        model = msg.get("model") or MODEL
                        max_tokens = msg.get("max_tokens", 512)
                        pipeline_config = msg.get("pipeline", {})
                        print(
                            f"{DIM}[pipeline {task_id[:8]}] stages={len(pipeline_config.get('stages', []))}{RESET}"
                        )
                        asyncio.create_task(
                            _process_pipeline_start(
                                task_id,
                                prompt,
                                model,
                                max_tokens,
                                pipeline_config,
                                ws,
                            )
                        )

                    elif msg_type == "load_layers":
                        model_name = msg.get("model", "")
                        layer_start = msg.get("layer_start", 0)
                        layer_end = msg.get("layer_end", 0)
                        print(
                            f"{CYAN}[layers] Loading {model_name} layers "
                            f"{layer_start}-{layer_end}{RESET}"
                        )

                    elif msg_type == "moe_expert_task":
                        task_id = msg["task_id"]
                        prompt = msg.get("prompt", "")
                        model = msg.get("model") or MODEL
                        print(
                            f"{CYAN}[moe {task_id[:8]}] {prompt[:60]}{RESET}"
                        )
                        asyncio.create_task(
                            _process_moe_expert(task_id, prompt, model, ws)
                        )

                    elif msg_type == "measure_peer":
                        target = msg.get("target_node_id", "")
                        asyncio.create_task(
                            _measure_peer(target, ws_url, ws)
                        )

        except ssl.SSLCertVerificationError as e:
            if ws_url.startswith("wss://") and not insecure_tls:
                if not warned_about_tls:
                    print(
                        f"\n{YELLOW}TLS verification failed ({e}). "
                        f"Retrying with insecure TLS.{RESET}"
                    )
                    warned_about_tls = True
                insecure_tls = True
                await asyncio.sleep(1)
                continue
            print(
                f"{DIM}Connection lost: {e}. Reconnecting in 5s ...{RESET}"
            )
            await asyncio.sleep(5)
        except Exception as e:
            print(
                f"{DIM}Connection lost: {e}. Reconnecting in 5s ...{RESET}"
            )
            await asyncio.sleep(5)


def main() -> None:
    global GATEWAY_URL, REGION, MODEL, NODE_ID, LATITUDE, LONGITUDE
    global ENGINE_TYPE, DRAFT_MODELS, EXPERT_DOMAINS

    print_banner()
    parser = argparse.ArgumentParser(description="Microwave AI Node")
    parser.add_argument(
        "--gateway-url", default=GATEWAY_URL, help="Gateway base URL"
    )
    parser.add_argument(
        "--region", default=REGION, help="Region identifier (e.g. LAN)"
    )
    parser.add_argument("--model", default=MODEL, help="Default model name")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Bind host (HTTP mode)"
    )
    parser.add_argument(
        "--port", type=int, default=9000, help="Bind port (HTTP mode)"
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Reverse mode: connect OUT to gateway via WebSocket",
    )
    parser.add_argument("--node-id", default=None, help="Custom node ID")
    parser.add_argument(
        "--latitude",
        type=float,
        default=LATITUDE,
        help="Node latitude (auto-detected if 0)",
    )
    parser.add_argument(
        "--longitude",
        type=float,
        default=LONGITUDE,
        help="Node longitude (auto-detected if 0)",
    )
    parser.add_argument(
        "--engine",
        default=ENGINE_TYPE,
        choices=["ollama", "llamacpp"],
        help="Inference engine type",
    )
    parser.add_argument(
        "--draft-models",
        default=",".join(DRAFT_MODELS),
        help="Comma-separated list of draft model names for speculative decoding",
    )
    parser.add_argument(
        "--expert-domains",
        default=",".join(EXPERT_DOMAINS),
        help="Comma-separated expert domains (e.g. code,math,general)",
    )
    args = parser.parse_args()

    GATEWAY_URL = args.gateway_url or GATEWAY_URL
    REGION = args.region
    MODEL = args.model
    LATITUDE = args.latitude
    LONGITUDE = args.longitude
    ENGINE_TYPE = args.engine
    DRAFT_MODELS = [m.strip() for m in args.draft_models.split(",") if m.strip()]
    EXPERT_DOMAINS = [d.strip() for d in args.expert_domains.split(",") if d.strip()]
    if args.node_id:
        NODE_ID = args.node_id

    caps = _detect_capabilities()
    print(f"  Engine:  {ENGINE_TYPE}")
    print(f"  Model:   {MODEL}")
    print(f"  Region:  {REGION}")
    print(f"  Domains: {EXPERT_DOMAINS}")
    print(f"  VRAM:    {caps['vram_mb']} MB")
    print(f"  RAM:     {caps['ram_mb']} MB")
    print(f"  Compute: {caps['compute_score']} est.tok/s")
    if DRAFT_MODELS:
        print(f"  Drafts:  {DRAFT_MODELS}")

    if args.reverse:
        if not GATEWAY_URL:
            print("ERROR: --gateway-url is required for reverse mode")
            sys.exit(1)
        print(f"  Mode:    reverse (WebSocket)")
        print(f"  Gateway: {GATEWAY_URL}")
        asyncio.run(_ws_listener(GATEWAY_URL))
    else:
        if GATEWAY_URL:
            try:
                asyncio.run(
                    register_with_gateway(GATEWAY_URL, args.host, args.port)
                )
            except RuntimeError:
                pass
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
