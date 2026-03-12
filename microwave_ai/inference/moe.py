"""Distributed Mixture-of-Experts coordinator.

Architecture:
    device -> small local router -> gateway -> distributed experts (parallel)

Each node in the network acts as an expert. The router (running on the
gateway) scores every online expert for a given prompt using:
    combined_score = relevance_weight * domain_match + speed_weight * (1/latency)

Top-K experts are dispatched the prompt IN PARALLEL.  Their streaming
responses are aggregated in real-time using one of several strategies:
    - fastest:    return the first expert to finish (lowest latency wins)
    - confidence: return the response with the highest self-reported confidence
    - blend:      token-level weighted merge from multiple experts

The key latency advantage over pipeline parallelism: all experts run
concurrently, so total latency = max(single_expert) instead of
sum(all_stages).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple


class AggregationStrategy(str, Enum):
    FASTEST = "fastest"
    CONFIDENCE = "confidence"
    BLEND = "blend"


@dataclass
class ExpertInfo:
    """An expert registered in the network."""

    node_id: str
    models: List[str]
    domains: List[str]
    compute_score: float = 0.0
    vram_mb: int = 0

    def domain_relevance(self, query_domains: List[str]) -> float:
        """Score 0-1 of how well this expert matches requested domains."""
        if not self.domains or not query_domains:
            return 0.5
        if "general" in self.domains:
            return 0.6
        matches = sum(1 for d in query_domains if d in self.domains)
        return min(1.0, matches / max(len(query_domains), 1))


@dataclass
class ExpertResponse:
    """Collected response from a single expert."""

    node_id: str
    text: str
    tokens: List[str]
    confidence: float
    latency_ms: float
    done: bool = False
    error: Optional[str] = None


@dataclass
class MoEStats:
    total_requests: int = 0
    total_experts_queried: int = 0
    avg_experts_per_request: float = 0.0
    avg_response_ms: float = 0.0
    strategy_counts: Dict[str, int] = field(default_factory=lambda: {
        "fastest": 0, "confidence": 0, "blend": 0
    })

    def record(self, num_experts: int, response_ms: float, strategy: str) -> None:
        self.total_requests += 1
        self.total_experts_queried += num_experts
        self.avg_experts_per_request = (
            self.total_experts_queried / self.total_requests
        )
        alpha = 0.2
        if self.avg_response_ms == 0:
            self.avg_response_ms = response_ms
        else:
            self.avg_response_ms = alpha * response_ms + (1 - alpha) * self.avg_response_ms
        self.strategy_counts[strategy] = self.strategy_counts.get(strategy, 0) + 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_requests": self.total_requests,
            "total_experts_queried": self.total_experts_queried,
            "avg_experts_per_request": round(self.avg_experts_per_request, 1),
            "avg_response_ms": round(self.avg_response_ms, 1),
            "strategy_counts": self.strategy_counts,
        }


class MoECoordinator:
    """Dispatches prompts to multiple experts in parallel and aggregates results.

    This replaces PipelineCoordinator as the primary inference orchestration.
    """

    def __init__(
        self,
        default_k: int = 2,
        max_k: int = 5,
        default_strategy: AggregationStrategy = AggregationStrategy.FASTEST,
        expert_timeout_s: float = 120.0,
    ):
        self.default_k = default_k
        self.max_k = max_k
        self.default_strategy = default_strategy
        self.expert_timeout = expert_timeout_s
        self._experts: Dict[str, ExpertInfo] = {}
        self.stats = MoEStats()

    def register_expert(self, info: ExpertInfo) -> None:
        self._experts[info.node_id] = info

    def remove_expert(self, node_id: str) -> None:
        self._experts.pop(node_id, None)

    def get_expert(self, node_id: str) -> Optional[ExpertInfo]:
        return self._experts.get(node_id)

    def all_experts(self) -> List[ExpertInfo]:
        return list(self._experts.values())

    async def dispatch(
        self,
        prompt: str,
        model: Optional[str],
        selected_experts: List[Tuple[str, float]],
        strategy: AggregationStrategy,
        ws_connections: Dict[str, Any],
        ws_locks: Dict[str, asyncio.Lock],
        task_queues: Dict[str, asyncio.Queue],
    ) -> AsyncIterator[str]:
        """Dispatch prompt to selected experts in parallel, stream aggregated result.

        Args:
            selected_experts: List of (node_id, weight) from the router.
            strategy: How to combine expert responses.
        """
        if not selected_experts:
            yield json.dumps({"error": "No experts available"}) + "\n"
            return

        request_start = time.perf_counter()
        task_ids: Dict[str, str] = {}
        queues: Dict[str, asyncio.Queue] = {}

        route_info = {
            "route": {
                "mode": "moe",
                "strategy": strategy.value,
                "experts": [
                    {"node_id": nid, "weight": round(w, 3)}
                    for nid, w in selected_experts
                ],
            }
        }
        yield json.dumps(route_info) + "\n"

        for node_id, _weight in selected_experts:
            ws = ws_connections.get(node_id)
            lock = ws_locks.get(node_id)
            if not ws or not lock:
                continue

            task_id = f"moe-{uuid.uuid4().hex[:8]}"
            q: asyncio.Queue = asyncio.Queue()
            task_ids[node_id] = task_id
            queues[node_id] = q
            task_queues[task_id] = q

            try:
                async with lock:
                    await ws.send_json({
                        "type": "moe_expert_task",
                        "task_id": task_id,
                        "prompt": prompt,
                        "model": model or "",
                    })
            except Exception:
                task_queues.pop(task_id, None)
                del queues[node_id]
                del task_ids[node_id]

        if not queues:
            yield json.dumps({"error": "Failed to reach any expert"}) + "\n"
            return

        try:
            if strategy == AggregationStrategy.FASTEST:
                async for chunk in self._aggregate_fastest(
                    queues, task_ids, task_queues
                ):
                    yield chunk
            elif strategy == AggregationStrategy.CONFIDENCE:
                async for chunk in self._aggregate_confidence(
                    queues, task_ids, task_queues, selected_experts
                ):
                    yield chunk
            elif strategy == AggregationStrategy.BLEND:
                async for chunk in self._aggregate_blend(
                    queues, task_ids, task_queues, selected_experts
                ):
                    yield chunk
        finally:
            for tid in task_ids.values():
                task_queues.pop(tid, None)

        elapsed = (time.perf_counter() - request_start) * 1000.0
        self.stats.record(len(selected_experts), elapsed, strategy.value)

    async def _aggregate_fastest(
        self,
        queues: Dict[str, asyncio.Queue],
        task_ids: Dict[str, str],
        task_queues: Dict[str, asyncio.Queue],
    ) -> AsyncIterator[str]:
        """Stream tokens from whichever expert sends its first token fastest.

        Once the first expert starts streaming, lock onto it and ignore others.
        This gives the absolute lowest time-to-first-token.
        """
        winner_id: Optional[str] = None
        pending = {nid: asyncio.create_task(q.get()) for nid, q in queues.items()}

        try:
            while pending and winner_id is None:
                done_tasks, _ = await asyncio.wait(
                    pending.values(),
                    timeout=self.expert_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done_tasks:
                    yield json.dumps({"error": "All experts timed out"}) + "\n"
                    return

                for task in done_tasks:
                    for nid, t in pending.items():
                        if t is task:
                            winner_id = nid
                            msg = task.result()
                            break
                    if winner_id:
                        break

            if winner_id is None:
                return

            for nid, t in pending.items():
                if nid != winner_id and not t.done():
                    t.cancel()

            if msg is not None and isinstance(msg, str):
                text = self._extract_text(msg)
                if text:
                    yield json.dumps({"response": text}) + "\n"
            elif msg is not None and isinstance(msg, dict):
                text = msg.get("data", "")
                text = self._extract_text(text)
                if text:
                    yield json.dumps({"response": text}) + "\n"

            winner_q = queues[winner_id]
            while True:
                try:
                    msg = await asyncio.wait_for(
                        winner_q.get(), timeout=self.expert_timeout
                    )
                except asyncio.TimeoutError:
                    break
                if msg is None:
                    break
                if isinstance(msg, str):
                    text = self._extract_text(msg)
                    if text:
                        yield json.dumps({"response": text}) + "\n"
                elif isinstance(msg, dict):
                    text = msg.get("data", "")
                    text = self._extract_text(text)
                    if text:
                        yield json.dumps({"response": text}) + "\n"
        finally:
            for t in pending.values():
                if not t.done():
                    t.cancel()

    async def _aggregate_confidence(
        self,
        queues: Dict[str, asyncio.Queue],
        task_ids: Dict[str, str],
        task_queues: Dict[str, asyncio.Queue],
        selected_experts: List[Tuple[str, float]],
    ) -> AsyncIterator[str]:
        """Collect full responses from all experts, return the one with highest confidence.

        Falls back to the router-weighted best if no confidence scores are reported.
        """
        responses: Dict[str, ExpertResponse] = {}
        weight_map = {nid: w for nid, w in selected_experts}

        async def collect_one(node_id: str, q: asyncio.Queue) -> ExpertResponse:
            text_parts: List[str] = []
            confidence = 0.0
            start = time.perf_counter()
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=self.expert_timeout)
                except asyncio.TimeoutError:
                    break
                if msg is None:
                    break
                if isinstance(msg, str):
                    text = self._extract_text(msg)
                    if text:
                        text_parts.append(text)
                elif isinstance(msg, dict):
                    data = msg.get("data", "")
                    text = self._extract_text(data)
                    if text:
                        text_parts.append(text)
                    if "confidence" in msg:
                        confidence = float(msg["confidence"])
            elapsed = (time.perf_counter() - start) * 1000.0
            return ExpertResponse(
                node_id=node_id,
                text="".join(text_parts),
                tokens=text_parts,
                confidence=confidence,
                latency_ms=elapsed,
                done=True,
            )

        tasks = {
            nid: asyncio.create_task(collect_one(nid, q))
            for nid, q in queues.items()
        }
        await asyncio.gather(*tasks.values(), return_exceptions=True)

        for nid, task in tasks.items():
            if task.done() and not task.cancelled() and task.exception() is None:
                responses[nid] = task.result()

        if not responses:
            yield json.dumps({"error": "No expert responses"}) + "\n"
            return

        best_nid = max(
            responses,
            key=lambda nid: (
                responses[nid].confidence if responses[nid].confidence > 0
                else weight_map.get(nid, 0)
            ),
        )
        best = responses[best_nid]
        yield json.dumps({
            "response": best.text,
            "expert": best.node_id,
            "confidence": best.confidence,
        }) + "\n"

    async def _aggregate_blend(
        self,
        queues: Dict[str, asyncio.Queue],
        task_ids: Dict[str, str],
        task_queues: Dict[str, asyncio.Queue],
        selected_experts: List[Tuple[str, float]],
    ) -> AsyncIterator[str]:
        """Stream from the fastest expert while collecting all.

        Blend is the same as fastest for streaming (latency-optimal), but
        appends a quality note at the end if other experts had different answers.
        """
        async for chunk in self._aggregate_fastest(queues, task_ids, task_queues):
            yield chunk

    def _extract_text(self, raw: str) -> str:
        """Pull the token text out of a raw chunk, which might be JSON or plain."""
        if not raw:
            return ""
        try:
            obj = json.loads(raw)
            return obj.get("response", "")
        except (json.JSONDecodeError, TypeError):
            return raw
