"""Activation tensor serialization with LZ4 compression and optional INT8 quantization.

Designed for minimum-latency transfer of hidden-state tensors between
pipeline stages over WebSocket binary frames.

Wire format:
    [4 bytes header_len (LE uint32)][header (msgpack)][payload (raw or lz4)]

Header fields:
    shape: list[int]    -- tensor dimensions
    dtype: str          -- numpy dtype string ("float16", "float32", "int8")
    quantized: bool     -- whether payload is INT8-quantized
    scale: float        -- quantization scale factor (only if quantized)
    offset: float       -- quantization zero-point (only if quantized)
    compressed: bool    -- whether payload is LZ4-compressed
"""

from __future__ import annotations

import struct
from typing import Tuple

import numpy as np

try:
    import msgpack
except ImportError:
    msgpack = None  # type: ignore[assignment]

try:
    import lz4.frame as lz4_frame
except ImportError:
    lz4_frame = None  # type: ignore[assignment]

_LEN_FMT = "<I"
_LEN_SIZE = struct.calcsize(_LEN_FMT)

# Compression threshold: skip LZ4 for tiny tensors where overhead > savings
_COMPRESS_THRESHOLD = 512  # bytes


def serialize_activation(
    tensor: np.ndarray,
    compress: bool = True,
    quantize: bool = False,
) -> bytes:
    """Serialize a numpy tensor for network transfer.

    Args:
        tensor: Hidden-state activation (typically float16 or float32).
        compress: Apply LZ4 fast compression.
        quantize: Downcast to INT8 for 2x bandwidth reduction (~0.1% quality loss).

    Returns:
        Packed bytes ready for WebSocket binary frame.
    """
    if msgpack is None:
        raise RuntimeError("msgpack required: pip install msgpack")

    header = {
        "shape": list(tensor.shape),
        "dtype": str(tensor.dtype),
        "quantized": False,
        "compressed": False,
    }

    payload = tensor.tobytes()

    if quantize and tensor.dtype in (np.float16, np.float32):
        payload, scale, offset = _quantize_int8(tensor)
        header["quantized"] = True
        header["scale"] = float(scale)
        header["offset"] = float(offset)
        header["dtype"] = "int8"

    if compress and lz4_frame is not None and len(payload) > _COMPRESS_THRESHOLD:
        payload = lz4_frame.compress(payload, compression_level=0)
        header["compressed"] = True

    header_bytes = msgpack.packb(header, use_bin_type=True)
    return struct.pack(_LEN_FMT, len(header_bytes)) + header_bytes + payload


def deserialize_activation(data: bytes) -> np.ndarray:
    """Deserialize bytes back into a numpy tensor."""
    if msgpack is None:
        raise RuntimeError("msgpack required: pip install msgpack")

    header_len = struct.unpack(_LEN_FMT, data[:_LEN_SIZE])[0]
    header = msgpack.unpackb(data[_LEN_SIZE : _LEN_SIZE + header_len], raw=False)
    payload = data[_LEN_SIZE + header_len :]

    if header.get("compressed") and lz4_frame is not None:
        payload = lz4_frame.decompress(payload)

    if header.get("quantized"):
        tensor = _dequantize_int8(
            payload,
            tuple(header["shape"]),
            header["scale"],
            header["offset"],
        )
    else:
        tensor = np.frombuffer(payload, dtype=np.dtype(header["dtype"]))
        tensor = tensor.reshape(header["shape"])

    return tensor


def estimate_transfer_bytes(
    shape: Tuple[int, ...],
    dtype: str = "float16",
    quantize: bool = False,
    compress_ratio: float = 0.65,
) -> int:
    """Estimate wire size for planning. Assumes ~65% LZ4 compression ratio."""
    element_size = np.dtype(dtype).itemsize
    if quantize:
        element_size = 1
    raw = int(np.prod(shape)) * element_size
    return int(raw * compress_ratio)


def _quantize_int8(tensor: np.ndarray) -> Tuple[bytes, float, float]:
    """Symmetric min-max INT8 quantization."""
    flat = tensor.astype(np.float32).flatten()
    v_min = float(flat.min())
    v_max = float(flat.max())
    scale = (v_max - v_min) / 255.0 if v_max != v_min else 1.0
    offset = v_min
    quantized = np.clip(
        np.round((flat - offset) / scale), 0, 255
    ).astype(np.uint8)
    return quantized.tobytes(), scale, offset


def _dequantize_int8(
    data: bytes, shape: Tuple[int, ...], scale: float, offset: float
) -> np.ndarray:
    """Reverse INT8 quantization back to float16."""
    quantized = np.frombuffer(data, dtype=np.uint8)
    return (quantized.astype(np.float32) * scale + offset).astype(np.float16).reshape(shape)
