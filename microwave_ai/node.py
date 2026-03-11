import argparse
import asyncio
import json
import os
import platform
import ssl
import sys
from typing import Any, Dict

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

from . import __version__


app = FastAPI(title="Microwave AI Node")

GATEWAY_URL = os.getenv("MICROWAVE_GATEWAY_URL")
NODE_ID = os.getenv("MICROWAVE_NODE_ID", platform.node())
REGION = os.getenv("MICROWAVE_REGION", "LAN")
MODEL = os.getenv("MICROWAVE_MODEL", "llama3")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

CYAN = "\033[1;36m"
GREEN = "\033[1;32m"
YELLOW = "\033[1;33m"
DIM = "\033[2m"
RESET = "\033[0m"


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


# ── HTTP mode endpoints (used when node listens on a port) ──

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
            ollama_req = {
                "model": model,
                "prompt": prompt,
                "stream": True,
            }
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
                        await ws.send(json.dumps({
                            "type": "chunk",
                            "task_id": task_id,
                            "data": chunk.decode("utf-8", errors="replace"),
                        }))
    except Exception as e:
        await ws.send(json.dumps({
            "type": "chunk",
            "task_id": task_id,
            "data": json.dumps({"error": str(e)}),
        }))
    finally:
        await ws.send(json.dumps({"type": "done", "task_id": task_id}))


async def _ws_listener(gateway_url: str) -> None:
    """Connect to gateway via WebSocket and wait for tasks. Auto-reconnects."""
    import websockets

    ws_url = gateway_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url.rstrip('/')}/nodes/ws"
    insecure_tls = os.getenv("MICROWAVE_INSECURE_TLS", "").lower() in ("1", "true", "yes")
    warned_about_tls = False

    while True:
        try:
            connect_kwargs = {"ping_interval": 20, "ping_timeout": 60}
            if ws_url.startswith("wss://") and insecure_tls:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                connect_kwargs["ssl"] = ctx

            async with websockets.connect(ws_url, **connect_kwargs) as ws:
                await ws.send(json.dumps({
                    "type": "register",
                    "node_id": NODE_ID,
                    "region": REGION,
                    "models": [MODEL],
                    "metadata": {"version": __version__},
                }))

                ack = json.loads(await ws.recv())
                if ack.get("type") == "registered":
                    print(f"Registered with gateway as '{ack.get('node_id')}' (reverse/WS)")
                    print(f"{GREEN}Ready. Use the Chat UI on the gateway to talk to this node.{RESET}")

                async for raw in ws:
                    msg = json.loads(raw)

                    if msg.get("type") == "task":
                        task_id = msg["task_id"]
                        prompt = msg.get("prompt", "")
                        model = msg.get("model") or MODEL
                        print(f"{DIM}[task {task_id[:8]}] {prompt[:60]}{RESET}")
                        await _process_task(task_id, prompt, model, ws)

                    elif msg.get("type") == "ping":
                        await ws.send(json.dumps({"type": "pong"}))

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
            print(f"{DIM}Connection lost: {e}. Reconnecting in 5s ...{RESET}")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"{DIM}Connection lost: {e}. Reconnecting in 5s ...{RESET}")
            await asyncio.sleep(5)


def main() -> None:
    global GATEWAY_URL, REGION, MODEL, NODE_ID

    print_banner()
    parser = argparse.ArgumentParser(description="Microwave AI Node")
    parser.add_argument("--gateway-url", default=GATEWAY_URL, help="Gateway base URL")
    parser.add_argument("--region", default=REGION, help="Region identifier (e.g. LAN)")
    parser.add_argument("--model", default=MODEL, help="Default model name")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (HTTP mode)")
    parser.add_argument("--port", type=int, default=9000, help="Bind port (HTTP mode)")
    parser.add_argument("--reverse", action="store_true",
                        help="Reverse mode: connect OUT to gateway via WebSocket "
                             "(no listening port needed, works behind NAT/firewall)")
    parser.add_argument("--node-id", default=None, help="Custom node ID")
    args = parser.parse_args()

    GATEWAY_URL = args.gateway_url or GATEWAY_URL
    REGION = args.region
    MODEL = args.model
    if args.node_id:
        NODE_ID = args.node_id

    if args.reverse:
        if not GATEWAY_URL:
            print("ERROR: --gateway-url is required for reverse mode")
            sys.exit(1)
        print(f"Mode:    reverse (WebSocket)")
        print(f"Gateway: {GATEWAY_URL}")
        print(f"Model:   {MODEL}")
        print(f"Region:  {REGION}")
        asyncio.run(_ws_listener(GATEWAY_URL))
    else:
        if GATEWAY_URL:
            try:
                asyncio.run(register_with_gateway(GATEWAY_URL, args.host, args.port))
            except RuntimeError:
                pass
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
