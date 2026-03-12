"""Inter-node latency matrix and optimal pipeline construction."""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class PeerMeasurement:
    rtt_ms: float
    timestamp: float = field(default_factory=time.monotonic)


class TopologyManager:
    """Maintains an NxN latency matrix between nodes and builds optimal pipelines."""

    def __init__(self, stale_seconds: float = 120.0):
        self._matrix: Dict[Tuple[str, str], PeerMeasurement] = {}
        self._stale_seconds = stale_seconds

    def update(self, src: str, dst: str, rtt_ms: float) -> None:
        self._matrix[(src, dst)] = PeerMeasurement(rtt_ms=rtt_ms)

    def get_rtt(self, src: str, dst: str) -> float:
        """Get RTT between two nodes. Returns inf if unknown or stale."""
        m = self._matrix.get((src, dst))
        if m is None:
            return float("inf")
        if time.monotonic() - m.timestamp > self._stale_seconds:
            return float("inf")
        return m.rtt_ms

    def remove_node(self, node_id: str) -> None:
        keys_to_drop = [
            k for k in self._matrix if k[0] == node_id or k[1] == node_id
        ]
        for k in keys_to_drop:
            del self._matrix[k]

    def chain_latency(self, ordered_ids: List[str]) -> float:
        """Total hop-to-hop latency for an ordered pipeline."""
        total = 0.0
        for i in range(len(ordered_ids) - 1):
            rtt = self.get_rtt(ordered_ids[i], ordered_ids[i + 1])
            if rtt == float("inf"):
                return float("inf")
            total += rtt
        return total

    def best_pipeline(
        self,
        candidate_ids: List[str],
        num_stages: int,
        gateway_id: str = "__gateway__",
    ) -> Optional[List[str]]:
        """Find the ordering of num_stages nodes that minimizes total hop latency.

        Uses greedy nearest-neighbor for speed. For small N (<8), tries all
        permutations for optimality.
        """
        if len(candidate_ids) < num_stages:
            return None

        if num_stages <= 7:
            return self._exhaustive_search(candidate_ids, num_stages, gateway_id)
        return self._greedy_search(candidate_ids, num_stages, gateway_id)

    def _exhaustive_search(
        self,
        candidate_ids: List[str],
        num_stages: int,
        gateway_id: str,
    ) -> Optional[List[str]]:
        best_chain: Optional[List[str]] = None
        best_cost = float("inf")

        for combo in itertools.combinations(candidate_ids, num_stages):
            for perm in itertools.permutations(combo):
                ordered = list(perm)
                gw_to_first = self.get_rtt(gateway_id, ordered[0])
                last_to_gw = self.get_rtt(ordered[-1], gateway_id)
                hop_cost = self.chain_latency(ordered)
                total = gw_to_first + hop_cost + last_to_gw
                if total < best_cost:
                    best_cost = total
                    best_chain = ordered

        return best_chain

    def _greedy_search(
        self,
        candidate_ids: List[str],
        num_stages: int,
        gateway_id: str,
    ) -> Optional[List[str]]:
        remaining = set(candidate_ids)
        first = min(
            remaining,
            key=lambda n: self.get_rtt(gateway_id, n),
        )
        chain = [first]
        remaining.discard(first)

        while len(chain) < num_stages and remaining:
            last = chain[-1]
            nxt = min(remaining, key=lambda n: self.get_rtt(last, n))
            chain.append(nxt)
            remaining.discard(nxt)

        if len(chain) < num_stages:
            return None
        return chain

    def needs_measurement(self, node_ids: List[str]) -> List[Tuple[str, str]]:
        """Return pairs of nodes that need fresh RTT measurements."""
        pairs = []
        for a in node_ids:
            for b in node_ids:
                if a == b:
                    continue
                m = self._matrix.get((a, b))
                if m is None or time.monotonic() - m.timestamp > self._stale_seconds:
                    pairs.append((a, b))
        return pairs
