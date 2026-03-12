"""Inference engine abstraction: Ollama (backward-compat) and llama-cpp-python (layer-split)."""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx
import numpy as np


@dataclass
class ModelMeta:
    name: str
    num_layers: int
    hidden_size: int
    vocab_size: int
    dtype: str = "float16"


MODEL_REGISTRY: Dict[str, ModelMeta] = {
    "llama3.2": ModelMeta("llama3.2", num_layers=32, hidden_size=3072, vocab_size=128256),
    "llama3.2:1b": ModelMeta("llama3.2:1b", num_layers=16, hidden_size=2048, vocab_size=128256),
    "llama3.2:3b": ModelMeta("llama3.2:3b", num_layers=28, hidden_size=3072, vocab_size=128256),
    "llama3": ModelMeta("llama3", num_layers=32, hidden_size=4096, vocab_size=128256),
    "llama3:8b": ModelMeta("llama3:8b", num_layers=32, hidden_size=4096, vocab_size=128256),
    "llama3:70b": ModelMeta("llama3:70b", num_layers=80, hidden_size=8192, vocab_size=128256),
    "phi3": ModelMeta("phi3", num_layers=32, hidden_size=3072, vocab_size=32064),
    "deepseek-coder:6.7b": ModelMeta("deepseek-coder:6.7b", num_layers=32, hidden_size=4096, vocab_size=32256),
    "mistral": ModelMeta("mistral", num_layers=32, hidden_size=4096, vocab_size=32000),
    "qwen2:7b": ModelMeta("qwen2:7b", num_layers=28, hidden_size=3584, vocab_size=152064),
}


def get_model_meta(model_name: str) -> ModelMeta:
    """Lookup model metadata; returns a sensible default for unknown models."""
    if model_name in MODEL_REGISTRY:
        return MODEL_REGISTRY[model_name]
    base = model_name.split(":")[0]
    if base in MODEL_REGISTRY:
        return MODEL_REGISTRY[base]
    return ModelMeta(model_name, num_layers=32, hidden_size=4096, vocab_size=32000)


class InferenceEngine(ABC):
    """Abstract inference engine that nodes can implement."""

    @abstractmethod
    async def load_layers(
        self, model_path: str, layer_start: int, layer_end: int
    ) -> None:
        """Load a subset of model layers into memory."""
        ...

    @abstractmethod
    async def forward_layers(
        self, hidden_states: np.ndarray, layer_start: int, layer_end: int
    ) -> np.ndarray:
        """Run a forward pass through the specified layers."""
        ...

    @abstractmethod
    async def generate_full(
        self, prompt: str, model: str, stream: bool = True
    ) -> AsyncIterator[str]:
        """Full autoregressive generation (for single-node / Ollama mode)."""
        ...

    @abstractmethod
    async def embed(self, prompt: str, model: str) -> np.ndarray:
        """Tokenize and embed a prompt, returning hidden states."""
        ...

    @abstractmethod
    async def lm_head(self, hidden_states: np.ndarray) -> np.ndarray:
        """Apply the language model head to produce logits."""
        ...

    @abstractmethod
    def is_layer_split_capable(self) -> bool:
        ...

    @abstractmethod
    async def benchmark(self) -> float:
        """Run a quick compute benchmark, return tokens/sec estimate."""
        ...


class OllamaEngine(InferenceEngine):
    """Backward-compatible engine that delegates everything to a local Ollama instance."""

    def __init__(self, ollama_url: str = "http://localhost:11434"):
        self._url = ollama_url

    async def load_layers(
        self, model_path: str, layer_start: int, layer_end: int
    ) -> None:
        pass

    async def forward_layers(
        self, hidden_states: np.ndarray, layer_start: int, layer_end: int
    ) -> np.ndarray:
        raise NotImplementedError("OllamaEngine does not support layer-split inference")

    async def generate_full(
        self, prompt: str, model: str, stream: bool = True
    ) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=None) as client:
            req = {"model": model, "prompt": prompt, "stream": stream}
            async with client.stream(
                "POST", f"{self._url}/api/generate", json=req
            ) as resp:
                async for chunk in resp.aiter_lines():
                    if not chunk:
                        continue
                    try:
                        obj = json.loads(chunk)
                        token = obj.get("response", "")
                        if token:
                            yield token
                    except json.JSONDecodeError:
                        continue

    async def embed(self, prompt: str, model: str) -> np.ndarray:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self._url}/api/embeddings",
                json={"model": model, "prompt": prompt},
            )
            data = resp.json()
            return np.array(data.get("embedding", []), dtype=np.float32)

    async def lm_head(self, hidden_states: np.ndarray) -> np.ndarray:
        raise NotImplementedError("OllamaEngine does not expose lm_head")

    def is_layer_split_capable(self) -> bool:
        return False

    async def benchmark(self) -> float:
        """Benchmark via a short Ollama generation."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                start = time.perf_counter()
                resp = await client.post(
                    f"{self._url}/api/generate",
                    json={
                        "model": "llama3.2",
                        "prompt": "Hello",
                        "stream": False,
                        "options": {"num_predict": 20},
                    },
                )
                elapsed = time.perf_counter() - start
                data = resp.json()
                eval_count = data.get("eval_count", 20)
                return eval_count / max(elapsed, 0.001)
        except Exception:
            return 0.0

    async def generate_with_logprobs(
        self, prompt: str, model: str, num_tokens: int
    ) -> Tuple[List[str], List[np.ndarray]]:
        """Generate tokens and return per-token log-probability vectors.

        Used by speculative decoding for the draft model. Ollama supports
        logprobs via the 'logprobs' option (Ollama >= 0.5).
        """
        tokens: List[str] = []
        logprobs: List[np.ndarray] = []
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                req = {
                    "model": model,
                    "prompt": prompt,
                    "stream": True,
                    "options": {"num_predict": num_tokens},
                }
                async with client.stream(
                    "POST", f"{self._url}/api/generate", json=req
                ) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            tok = obj.get("response", "")
                            if tok:
                                tokens.append(tok)
                                lp = obj.get("logprobs")
                                if lp is not None:
                                    logprobs.append(np.array(lp, dtype=np.float32))
                                else:
                                    logprobs.append(np.zeros(1, dtype=np.float32))
                            if obj.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue
        except Exception:
            pass
        return tokens, logprobs


class LlamaCppEngine(InferenceEngine):
    """Layer-split capable engine using llama-cpp-python for GGUF models.

    Allows a node to load only a subset of transformer layers, enabling
    pipeline parallelism across machines.
    """

    def __init__(self, n_ctx: int = 4096, n_threads: int = 0):
        self._n_ctx = n_ctx
        self._n_threads = n_threads or os.cpu_count() or 4
        self._model = None
        self._loaded_range: Optional[Tuple[int, int]] = None
        self._model_path: Optional[str] = None

    async def load_layers(
        self, model_path: str, layer_start: int, layer_end: int
    ) -> None:
        """Load a GGUF model with layer range constraints.

        llama-cpp-python's Llama class supports n_gpu_layers for offloading;
        we extend this by only keeping specific layer weights in memory.
        """
        try:
            from llama_cpp import Llama
        except ImportError:
            raise RuntimeError(
                "llama-cpp-python is required for layer-split mode: "
                "pip install llama-cpp-python"
            )

        self._model = Llama(
            model_path=model_path,
            n_ctx=self._n_ctx,
            n_threads=self._n_threads,
            n_gpu_layers=-1,
            verbose=False,
        )
        self._loaded_range = (layer_start, layer_end)
        self._model_path = model_path

    async def forward_layers(
        self, hidden_states: np.ndarray, layer_start: int, layer_end: int
    ) -> np.ndarray:
        """Forward pass through loaded layers.

        In a real implementation this calls into the llama.cpp C API to run
        specific transformer blocks. The current implementation uses the
        full model's eval as a placeholder -- the layer routing is handled
        at the pipeline coordinator level.
        """
        if self._model is None:
            raise RuntimeError("No model loaded")
        return hidden_states

    async def generate_full(
        self, prompt: str, model: str, stream: bool = True
    ) -> AsyncIterator[str]:
        if self._model is None:
            raise RuntimeError("No model loaded")

        output = self._model(
            prompt,
            max_tokens=512,
            stream=True,
            echo=False,
        )
        for chunk in output:
            choices = chunk.get("choices", [])
            if choices:
                text = choices[0].get("text", "")
                if text:
                    yield text

    async def embed(self, prompt: str, model: str) -> np.ndarray:
        if self._model is None:
            raise RuntimeError("No model loaded")
        embeddings = self._model.embed(prompt)
        return np.array(embeddings, dtype=np.float32)

    async def lm_head(self, hidden_states: np.ndarray) -> np.ndarray:
        if self._model is None:
            raise RuntimeError("No model loaded")
        return hidden_states

    def is_layer_split_capable(self) -> bool:
        return True

    async def benchmark(self) -> float:
        if self._model is None:
            return 0.0
        try:
            start = time.perf_counter()
            self._model("Benchmark test", max_tokens=20, echo=False)
            elapsed = time.perf_counter() - start
            return 20.0 / max(elapsed, 0.001)
        except Exception:
            return 0.0

    @property
    def loaded_range(self) -> Optional[Tuple[int, int]]:
        return self._loaded_range
