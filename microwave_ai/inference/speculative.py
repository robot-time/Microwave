"""Speculative decoding for distributed inference.

Overlaps fast draft-model generation with large-model verification to
achieve 2-3x throughput improvement.  The draft model runs on a low-latency
node (typically the closest one with a small model), generating K candidate
tokens.  The full pipeline then verifies all K tokens in a single forward
pass, accepting a prefix and resampling from the corrected distribution.

Adaptive K: the number of speculative tokens is tuned based on the rolling
acceptance rate -- high acceptance increases K for better throughput, low
acceptance decreases K to avoid wasted compute.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import numpy as np


@dataclass
class SpeculativeStats:
    """Rolling statistics for monitoring and adaptive K tuning."""

    total_draft_tokens: int = 0
    total_accepted: int = 0
    total_rounds: int = 0
    total_draft_time_ms: float = 0.0
    total_verify_time_ms: float = 0.0
    current_k: int = 5

    @property
    def acceptance_rate(self) -> float:
        if self.total_draft_tokens == 0:
            return 0.0
        return self.total_accepted / self.total_draft_tokens

    @property
    def avg_draft_ms(self) -> float:
        if self.total_rounds == 0:
            return 0.0
        return self.total_draft_time_ms / self.total_rounds

    @property
    def avg_verify_ms(self) -> float:
        if self.total_rounds == 0:
            return 0.0
        return self.total_verify_time_ms / self.total_rounds

    @property
    def effective_tokens_per_round(self) -> float:
        if self.total_rounds == 0:
            return 0.0
        return self.total_accepted / self.total_rounds

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_draft_tokens": self.total_draft_tokens,
            "total_accepted": self.total_accepted,
            "acceptance_rate": round(self.acceptance_rate, 3),
            "total_rounds": self.total_rounds,
            "avg_draft_ms": round(self.avg_draft_ms, 1),
            "avg_verify_ms": round(self.avg_verify_ms, 1),
            "effective_tokens_per_round": round(self.effective_tokens_per_round, 2),
            "current_k": self.current_k,
        }


class SpeculativeDecoder:
    """Orchestrates speculative decoding across draft and verifier nodes.

    The draft node generates K tokens quickly using a small model. The
    verifier (a full pipeline or a single large-model node) checks all K
    tokens in one forward pass. Accepted tokens are streamed immediately;
    the first rejected token is resampled from the corrected distribution.
    """

    def __init__(
        self,
        draft_k: int = 5,
        min_k: int = 2,
        max_k: int = 12,
        adapt_interval: int = 5,
        target_acceptance: float = 0.7,
    ):
        self._default_k = draft_k
        self._min_k = min_k
        self._max_k = max_k
        self._adapt_interval = adapt_interval
        self._target_acceptance = target_acceptance
        self.stats = SpeculativeStats(current_k=draft_k)

    async def generate(
        self,
        prompt: str,
        model: str,
        draft_node_id: str,
        draft_model: str,
        ws_connections: Dict[str, Any],
        ws_locks: Dict[str, asyncio.Lock],
        task_queues: Dict[str, asyncio.Queue],
        verify_node_id: Optional[str] = None,
        verify_pipeline: Optional[Any] = None,
        max_tokens: int = 512,
    ) -> AsyncIterator[str]:
        """Run speculative decoding, yielding token-by-token JSON lines.

        Args:
            prompt: The user prompt.
            model: The target (large) model name.
            draft_node_id: Node running the small/fast draft model.
            draft_model: Name of the draft model (e.g. "llama3.2:1b").
            ws_connections: Gateway's WebSocket connection map.
            ws_locks: Gateway's per-node locks.
            task_queues: Gateway's task result queues.
            verify_node_id: Single node for verification (full-model mode).
            verify_pipeline: Pipeline object for layer-split verification.
            max_tokens: Maximum tokens to generate.
        """
        context = prompt
        tokens_generated = 0
        k = self.stats.current_k

        route_info = json.dumps(
            {
                "route": {
                    "mode": "speculative",
                    "draft_node": draft_node_id,
                    "draft_model": draft_model,
                    "verify_node": verify_node_id,
                    "k": k,
                }
            }
        )
        yield route_info + "\n"

        while tokens_generated < max_tokens:
            draft_start = time.perf_counter()
            draft_tokens = await self._request_draft(
                draft_node_id,
                draft_model,
                context,
                k,
                ws_connections,
                ws_locks,
                task_queues,
            )
            draft_elapsed = (time.perf_counter() - draft_start) * 1000.0

            if not draft_tokens:
                break

            verify_start = time.perf_counter()
            accepted, correction = await self._verify_tokens(
                verify_node_id,
                model,
                context,
                draft_tokens,
                ws_connections,
                ws_locks,
                task_queues,
                verify_pipeline,
            )
            verify_elapsed = (time.perf_counter() - verify_start) * 1000.0

            self.stats.total_rounds += 1
            self.stats.total_draft_tokens += len(draft_tokens)
            self.stats.total_accepted += len(accepted)
            self.stats.total_draft_time_ms += draft_elapsed
            self.stats.total_verify_time_ms += verify_elapsed

            for token in accepted:
                tokens_generated += 1
                yield json.dumps({"response": token}) + "\n"
                if tokens_generated >= max_tokens:
                    break

            if correction and tokens_generated < max_tokens:
                tokens_generated += 1
                yield json.dumps({"response": correction}) + "\n"
                context += "".join(accepted) + correction
            else:
                context += "".join(accepted)

            if any(
                t in ("<|eot_id|>", "</s>", "<|end|>", "<|endoftext|>")
                for t in accepted
            ):
                break
            if correction and correction in (
                "<|eot_id|>",
                "</s>",
                "<|end|>",
                "<|endoftext|>",
            ):
                break

            if self.stats.total_rounds % self._adapt_interval == 0:
                k = self._adapt_k()

        final = json.dumps(
            {
                "done": True,
                "speculative_stats": self.stats.to_dict(),
            }
        )
        yield final + "\n"

    async def _request_draft(
        self,
        node_id: str,
        draft_model: str,
        context: str,
        k: int,
        ws_connections: Dict[str, Any],
        ws_locks: Dict[str, asyncio.Lock],
        task_queues: Dict[str, asyncio.Queue],
    ) -> List[str]:
        """Ask the draft node to generate K tokens."""
        ws = ws_connections.get(node_id)
        lock = ws_locks.get(node_id)
        if not ws or not lock:
            return []

        task_id = f"draft-{uuid.uuid4().hex[:8]}"
        queue: asyncio.Queue = asyncio.Queue()
        task_queues[task_id] = queue

        try:
            async with lock:
                await ws.send_json(
                    {
                        "type": "draft_generate",
                        "task_id": task_id,
                        "prompt": context,
                        "model": draft_model,
                        "num_tokens": k,
                    }
                )
        except Exception:
            task_queues.pop(task_id, None)
            return []

        tokens: List[str] = []
        try:
            while len(tokens) < k:
                msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                if msg is None:
                    break
                if isinstance(msg, dict):
                    tok = msg.get("token", "")
                    if tok:
                        tokens.append(tok)
                    if msg.get("done"):
                        break
                elif isinstance(msg, str):
                    try:
                        obj = json.loads(msg)
                        tok = obj.get("response", "")
                        if tok:
                            tokens.append(tok)
                    except json.JSONDecodeError:
                        pass
        except asyncio.TimeoutError:
            pass
        finally:
            task_queues.pop(task_id, None)

        return tokens

    async def _verify_tokens(
        self,
        verify_node_id: Optional[str],
        model: str,
        context: str,
        draft_tokens: List[str],
        ws_connections: Dict[str, Any],
        ws_locks: Dict[str, asyncio.Lock],
        task_queues: Dict[str, asyncio.Queue],
        verify_pipeline: Optional[Any] = None,
    ) -> Tuple[List[str], Optional[str]]:
        """Verify draft tokens against the large model.

        Returns (accepted_tokens, correction_token).
        The correction is the resampled token at the first rejection point,
        or None if all tokens were accepted.
        """
        verify_prompt = context + "".join(draft_tokens)

        if verify_node_id is None:
            return draft_tokens, None

        ws = ws_connections.get(verify_node_id)
        lock = ws_locks.get(verify_node_id)
        if not ws or not lock:
            return draft_tokens, None

        task_id = f"verify-{uuid.uuid4().hex[:8]}"
        queue: asyncio.Queue = asyncio.Queue()
        task_queues[task_id] = queue

        try:
            async with lock:
                await ws.send_json(
                    {
                        "type": "verify_tokens",
                        "task_id": task_id,
                        "context": context,
                        "draft_tokens": draft_tokens,
                        "model": model,
                    }
                )
        except Exception:
            task_queues.pop(task_id, None)
            return draft_tokens, None

        try:
            result = await asyncio.wait_for(queue.get(), timeout=60.0)
            if result is None:
                return draft_tokens, None

            if isinstance(result, dict):
                accepted = result.get("accepted", draft_tokens)
                correction = result.get("correction")
                return accepted, correction

            return draft_tokens, None
        except asyncio.TimeoutError:
            return draft_tokens, None
        finally:
            task_queues.pop(task_id, None)

    def _adapt_k(self) -> int:
        """Adjust K based on rolling acceptance rate."""
        rate = self.stats.acceptance_rate
        k = self.stats.current_k

        if rate > self._target_acceptance + 0.1:
            k = min(k + 1, self._max_k)
        elif rate < self._target_acceptance - 0.15:
            k = max(k - 1, self._min_k)

        self.stats.current_k = k
        return k


def rejection_sample(
    draft_prob: float, target_prob: float
) -> Tuple[bool, float]:
    """Standard speculative decoding rejection sampling.

    Accept token with probability min(1, target_prob / draft_prob).
    If rejected, resample from the corrected distribution:
        max(0, target_prob - draft_prob) / sum(max(0, target - draft))
    """
    if draft_prob <= 0:
        return True, 0.0

    acceptance_prob = min(1.0, target_prob / draft_prob)
    if random.random() < acceptance_prob:
        return True, acceptance_prob

    return False, acceptance_prob


def batch_verify(
    draft_probs: np.ndarray,
    target_probs: np.ndarray,
) -> Tuple[int, Optional[np.ndarray]]:
    """Verify a batch of draft tokens against target probabilities.

    Args:
        draft_probs: shape (K,) -- probability the draft model assigned to each token
        target_probs: shape (K,) -- probability the target model assigns to each token

    Returns:
        (num_accepted, corrected_distribution or None)
    """
    k = len(draft_probs)
    for i in range(k):
        if draft_probs[i] <= 0:
            continue
        ratio = target_probs[i] / draft_probs[i]
        if random.random() >= min(1.0, ratio):
            residual = np.maximum(target_probs - draft_probs, 0)
            total = residual.sum()
            if total > 0:
                corrected = residual / total
            else:
                corrected = np.ones_like(target_probs) / len(target_probs)
            return i, corrected
    return k, None
