import argparse
import asyncio
import json
import os
import platform
import sys
from typing import Any, Dict

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn


app = FastAPI(title="Microwave AI Node")

GATEWAY_URL = os.getenv("MICROWAVE_GATEWAY_URL")
NODE_ID = os.getenv("MICROWAVE_NODE_ID", platform.node())
REGION = os.getenv("MICROWAVE_REGION", "LAN")
MODEL = os.getenv("MICROWAVE_MODEL", "llama3")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

CYAN = "\033[1;36m"
GREEN = "\033[1;32m"
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
    print("Microwave Network (node)")


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
                    "metadata": {},
                },
                timeout=5.0,
            )
        except Exception:
            pass


# ── Interactive terminal chat ──

async def terminal_chat() -> None:
    """Interactive chat loop that talks to the local Ollama instance."""
    print()
    print(f"{GREEN}--- Terminal Chat ---{RESET}")
    print(f"{DIM}Model: {MODEL}  |  Type 'exit' to quit  |  Network tasks run in the background{RESET}")
    print()

    loop = asyncio.get_event_loop()

    while True:
        try:
            prompt = await loop.run_in_executor(
                None, lambda: input(f"{CYAN}You > {RESET}")
            )
        except (EOFError, KeyboardInterrupt):
            print()
            break

        stripped = prompt.strip()
        if not stripped:
            continue
        if stripped.lower() in ("exit", "quit"):
            break

        print(f"{GREEN}Microwave AI > {RESET}", end="", flush=True)
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{OLLAMA_URL}/api/generate",
                    json={"model": MODEL, "prompt": stripped, "stream": True},
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        if not chunk:
                            continue
                        try:
                            obj = json.loads(chunk)
                            token = obj.get("response", "")
                            if token:
                                print(token, end="", flush=True)
                        except json.JSONDecodeError:
                            pass
        except httpx.ConnectError:
            print(f"\n{DIM}(Ollama not reachable at {OLLAMA_URL} – is it running?){RESET}")
        except Exception as e:
            print(f"\n{DIM}(Error: {e}){RESET}")

        print()


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

    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=60) as ws:
                await ws.send(json.dumps({
                    "type": "register",
                    "node_id": NODE_ID,
                    "region": REGION,
                    "models": [MODEL],
                    "metadata": {},
                }))

                ack = json.loads(await ws.recv())
                if ack.get("type") == "registered":
                    print(f"Registered with gateway as '{ack.get('node_id')}' (reverse/WS)")

                async for raw in ws:
                    msg = json.loads(raw)

                    if msg.get("type") == "task":
                        task_id = msg["task_id"]
                        prompt = msg.get("prompt", "")
                        model = msg.get("model") or MODEL
                        print(f"\n{DIM}[network task {task_id[:8]}] {prompt[:40]}...{RESET}")
                        await _process_task(task_id, prompt, model, ws)

                    elif msg.get("type") == "ping":
                        await ws.send(json.dumps({"type": "pong"}))

        except Exception as e:
            print(f"\n{DIM}Connection lost: {e}. Reconnecting in 5s ...{RESET}")
            await asyncio.sleep(5)


async def reverse_mode_with_chat(gateway_url: str) -> None:
    """Run WS listener in background + interactive terminal chat."""
    asyncio.create_task(_ws_listener(gateway_url))
    await asyncio.sleep(1)
    await terminal_chat()


async def http_mode_with_chat(host: str, port: int) -> None:
    """Run uvicorn HTTP server in background + interactive terminal chat."""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    await asyncio.sleep(1)
    print(f"Node listening on http://{host}:{port}")
    await terminal_chat()
    server.should_exit = True


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
    parser.add_argument("--no-chat", action="store_true",
                        help="Disable interactive terminal chat")
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

        if args.no_chat:
            asyncio.run(_ws_listener(GATEWAY_URL))
        else:
            asyncio.run(reverse_mode_with_chat(GATEWAY_URL))
    else:
        if GATEWAY_URL:
            try:
                asyncio.run(register_with_gateway(GATEWAY_URL, args.host, args.port))
            except RuntimeError:
                pass

        if args.no_chat:
            uvicorn.run(app, host=args.host, port=args.port)
        else:
            asyncio.run(http_mode_with_chat(args.host, args.port))


if __name__ == "__main__":
    main()
