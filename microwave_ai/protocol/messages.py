"""Binary message protocol for high-performance tensor transfer.

Control messages (register, ping, task assignment) stay as JSON for
debuggability. Performance-critical paths (activation tensors, pipeline
forwards) use msgpack headers + LZ4-compressed binary payloads.

Wire format for binary messages:
    [1 byte msg_type][4 bytes header_len][4 bytes body_len][header (msgpack)][body (lz4)]
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import time
from enum import IntEnum
from typing import Any, Dict, Optional, Tuple

try:
    import msgpack
except ImportError:
    msgpack = None  # type: ignore[assignment]

try:
    import lz4.frame as lz4_frame
except ImportError:
    lz4_frame = None  # type: ignore[assignment]


class MsgType(IntEnum):
    PIPELINE_FORWARD = 0x01
    PIPELINE_RESULT = 0x02
    DRAFT_REQUEST = 0x03
    DRAFT_RESPONSE = 0x04
    PEER_PING = 0x05
    PEER_PONG = 0x06
    LAYER_LOAD = 0x07
    LAYER_READY = 0x08
    KV_CACHE_SYNC = 0x09

_HEADER_FMT = "<BII"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)  # 9 bytes


def encode_message(
    msg_type: int,
    payload: Dict[str, Any],
    binary_data: bytes = b"",
    compress: bool = True,
) -> bytes:
    """Encode a binary protocol message.

    Returns raw bytes suitable for sending over a WebSocket binary frame.
    """
    if msgpack is None:
        raise RuntimeError("msgpack is required: pip install msgpack")

    header = msgpack.packb(payload, use_bin_type=True)

    if compress and lz4_frame is not None and len(binary_data) > 256:
        body = lz4_frame.compress(binary_data, compression_level=0)
        payload_header = msgpack.packb(
            {**payload, "_lz4": True}, use_bin_type=True
        )
        header = payload_header
    else:
        body = binary_data

    return struct.pack(_HEADER_FMT, msg_type, len(header), len(body)) + header + body


def decode_message(data: bytes) -> Tuple[int, Dict[str, Any], bytes]:
    """Decode a binary protocol message. Returns (msg_type, header_dict, body_bytes)."""
    if msgpack is None:
        raise RuntimeError("msgpack is required: pip install msgpack")

    if len(data) < _HEADER_SIZE:
        raise ValueError(f"Message too short: {len(data)} < {_HEADER_SIZE}")

    msg_type, h_len, b_len = struct.unpack(_HEADER_FMT, data[:_HEADER_SIZE])
    header = msgpack.unpackb(data[_HEADER_SIZE : _HEADER_SIZE + h_len], raw=False)
    body = data[_HEADER_SIZE + h_len : _HEADER_SIZE + h_len + b_len]

    if header.pop("_lz4", False) and lz4_frame is not None:
        body = lz4_frame.decompress(body)

    return msg_type, header, body


def encode_control(payload: Dict[str, Any]) -> str:
    """Encode a JSON control message (register, ping, task, etc.)."""
    return json.dumps(payload, separators=(",", ":"))


def decode_control(raw: str) -> Dict[str, Any]:
    """Decode a JSON control message."""
    return json.loads(raw)


def apply_tcp_nodelay(sock: socket.socket) -> None:
    """Disable Nagle's algorithm for minimum latency."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (OSError, AttributeError):
        pass


class ConnectionPool:
    """Manages persistent WebSocket connections between pipeline peer nodes.

    Pre-warms connections when a pipeline is formed so there's zero
    connection-setup overhead per inference request.
    """

    def __init__(self):
        self._connections: Dict[str, Any] = {}  # node_id -> websocket
        self._locks: Dict[str, asyncio.Lock] = {}
        self._last_used: Dict[str, float] = {}

    async def get_or_connect(
        self, node_id: str, ws_url: str, connect_timeout: float = 5.0
    ) -> Any:
        """Get an existing connection or create a new one."""
        if node_id in self._connections:
            self._last_used[node_id] = time.monotonic()
            return self._connections[node_id]

        import websockets

        ws = await asyncio.wait_for(
            websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=60,
                max_size=64 * 1024 * 1024,  # 64 MB for large activations
            ),
            timeout=connect_timeout,
        )
        self._connections[node_id] = ws
        self._locks[node_id] = asyncio.Lock()
        self._last_used[node_id] = time.monotonic()
        return ws

    def get_lock(self, node_id: str) -> asyncio.Lock:
        if node_id not in self._locks:
            self._locks[node_id] = asyncio.Lock()
        return self._locks[node_id]

    async def send_binary(self, node_id: str, data: bytes) -> None:
        """Send binary data over a pooled connection with lock."""
        ws = self._connections.get(node_id)
        if ws is None:
            raise ConnectionError(f"No connection to node {node_id}")
        lock = self.get_lock(node_id)
        async with lock:
            await ws.send(data)

    async def close(self, node_id: str) -> None:
        ws = self._connections.pop(node_id, None)
        self._locks.pop(node_id, None)
        self._last_used.pop(node_id, None)
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def close_all(self) -> None:
        for node_id in list(self._connections):
            await self.close(node_id)

    async def prune_stale(self, max_idle_seconds: float = 300.0) -> None:
        """Close connections idle for too long."""
        now = time.monotonic()
        stale = [
            nid
            for nid, ts in self._last_used.items()
            if now - ts > max_idle_seconds
        ]
        for nid in stale:
            await self.close(nid)
