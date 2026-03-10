import argparse
import asyncio
import os
from typing import Any, Dict

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn


app = FastAPI(title="Microwave AI Node")

GATEWAY_URL = os.getenv("MICROWAVE_GATEWAY_URL")
NODE_ID = os.getenv("MICROWAVE_NODE_ID", os.uname().nodename)
REGION = os.getenv("MICROWAVE_REGION", "LAN")
MODEL = os.getenv("MICROWAVE_MODEL", "llama3")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")


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
            # Stream from Ollama's /api/generate endpoint
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
            # Registration failures shouldn't crash the node in Phase 0
            pass


def main() -> None:
    global GATEWAY_URL, REGION, MODEL

    parser = argparse.ArgumentParser(description="Microwave AI Node")
    parser.add_argument("--gateway-url", default=GATEWAY_URL, help="Gateway base URL")
    parser.add_argument("--region", default=REGION, help="Region identifier (e.g. LAN)")
    parser.add_argument("--model", default=MODEL, help="Default model name")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=9000, help="Bind port")
    args = parser.parse_args()

    # Update globals from CLI
    GATEWAY_URL = args.gateway_url or GATEWAY_URL
    REGION = args.region
    MODEL = args.model

    if GATEWAY_URL:
        # Perform a one-shot registration before starting the server
        try:
            asyncio.run(register_with_gateway(GATEWAY_URL, args.host, args.port))
        except RuntimeError:
            # If there's already a running loop (unlikely here), skip registration
            pass

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

