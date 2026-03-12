"""EWMA-based latency tracking and node scoring for optimal routing."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class _NodeStats:
    ewma_ms: float = 0.0
    jitter_ms: float = 0.0
    last_rtt_ms: float = -1.0
    last_update: float = 0.0
    sample_count: int = 0
    total_failures: int = 0
    consecutive_failures: int = 0


class LatencyTracker:
    """Tracks per-node latency with EWMA smoothing and jitter awareness.

    Score formula: ewma + 2*jitter + failure_penalty
    Lower score = better node.
    """

    def __init__(
        self,
        alpha: float = 0.3,
        failure_penalty_ms: float = 500.0,
        stale_threshold_s: float = 30.0,
    ):
        self._alpha = alpha
        self._failure_penalty = failure_penalty_ms
        self._stale_threshold = stale_threshold_s
        self._stats: Dict[str, _NodeStats] = {}

    def record(self, node_id: str, rtt_ms: float) -> None:
        """Record a successful RTT measurement."""
        stats = self._stats.get(node_id)
        if stats is None:
            stats = _NodeStats()
            self._stats[node_id] = stats

        if stats.sample_count == 0:
            stats.ewma_ms = rtt_ms
            stats.jitter_ms = 0.0
        else:
            prev = stats.ewma_ms
            stats.ewma_ms = self._alpha * rtt_ms + (1 - self._alpha) * prev
            stats.jitter_ms = (
                self._alpha * abs(rtt_ms - prev)
                + (1 - self._alpha) * stats.jitter_ms
            )

        stats.last_rtt_ms = rtt_ms
        stats.last_update = time.monotonic()
        stats.sample_count += 1
        stats.consecutive_failures = 0

    def record_failure(self, node_id: str) -> None:
        """Record a failed health check."""
        stats = self._stats.get(node_id)
        if stats is None:
            stats = _NodeStats()
            self._stats[node_id] = stats
        stats.total_failures += 1
        stats.consecutive_failures += 1
        stats.last_update = time.monotonic()

    def score(self, node_id: str) -> float:
        """Composite score: lower is better. Returns inf for unknown nodes."""
        stats = self._stats.get(node_id)
        if stats is None or stats.sample_count == 0:
            return float("inf")

        base = stats.ewma_ms + 2.0 * stats.jitter_ms

        if stats.consecutive_failures > 0:
            base += self._failure_penalty * stats.consecutive_failures

        age = time.monotonic() - stats.last_update
        if age > self._stale_threshold:
            base += (age - self._stale_threshold) * 10.0

        return base

    def ewma(self, node_id: str) -> float:
        stats = self._stats.get(node_id)
        if stats is None:
            return -1.0
        return stats.ewma_ms

    def jitter(self, node_id: str) -> float:
        stats = self._stats.get(node_id)
        if stats is None:
            return -1.0
        return stats.jitter_ms

    def is_healthy(self, node_id: str) -> bool:
        stats = self._stats.get(node_id)
        if stats is None:
            return False
        return stats.consecutive_failures < 3 and stats.sample_count > 0

    def remove(self, node_id: str) -> None:
        self._stats.pop(node_id, None)

    def all_scores(self) -> Dict[str, float]:
        return {nid: self.score(nid) for nid in self._stats}

    def ranked(self, node_ids: Optional[list] = None) -> list:
        """Return node_ids sorted by score (best first)."""
        if node_ids is None:
            node_ids = list(self._stats.keys())
        return sorted(node_ids, key=self.score)
