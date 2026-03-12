"""MoE Router: selects which experts to activate for a given prompt.

The router runs on the gateway (near-zero overhead) and produces a ranked
list of (expert_node_id, weight) pairs.  Selection combines:

    1. Domain relevance  - keyword/tag matching against expert domains
    2. Latency score     - EWMA ping (faster experts ranked higher)
    3. Compute capacity  - VRAM + benchmark score (can the expert handle it?)
    4. Region proximity  - geographic distance penalty

The combined score determines which top-K experts receive the prompt.
All K experts run IN PARALLEL -- latency = slowest single expert, not sum.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from ..network.latency import LatencyTracker
from ..network.region import RegionEngine
from .moe import ExpertInfo

# Keyword -> domain mapping for automatic prompt classification
_DOMAIN_KEYWORDS: Dict[str, List[str]] = {
    "code": [
        "code", "function", "class", "def ", "import ", "python", "javascript",
        "typescript", "rust", "golang", "api", "debug", "error", "traceback",
        "compile", "syntax", "variable", "loop", "algorithm", "git", "docker",
        "sql", "database", "html", "css", "react", "node", "npm", "pip",
        "async", "await", "http", "json", "xml", "regex", "bash", "shell",
        "linux", "terminal", "stdout", "stderr", "exception", "stack trace",
    ],
    "math": [
        "math", "calculate", "equation", "integral", "derivative", "matrix",
        "probability", "statistics", "algebra", "geometry", "calculus",
        "theorem", "proof", "formula", "sum", "product", "factorial",
        "logarithm", "exponential", "trigonometry", "vector", "eigenvalue",
    ],
    "creative": [
        "write a story", "poem", "creative", "fiction", "narrative",
        "character", "plot", "screenplay", "lyrics", "song", "haiku",
        "limerick", "metaphor", "imagery", "dialogue", "novel",
    ],
    "science": [
        "physics", "chemistry", "biology", "molecule", "atom", "cell",
        "evolution", "quantum", "relativity", "thermodynamics", "genetics",
        "neuroscience", "ecology", "astronomy", "planet", "star",
    ],
    "reasoning": [
        "explain", "why", "how does", "compare", "analyze", "evaluate",
        "pros and cons", "trade-off", "reasoning", "logic", "argument",
        "because", "therefore", "consequence", "implication",
    ],
}


def classify_prompt(prompt: str) -> List[str]:
    """Classify a prompt into domain tags by keyword matching.

    Returns a list of matched domains sorted by relevance (most matches first).
    Fast O(keywords) scan -- no ML model needed.
    """
    lower = prompt.lower()
    scores: Dict[str, int] = {}
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in lower)
        if count > 0:
            scores[domain] = count

    if not scores:
        return ["general"]

    ranked = sorted(scores, key=scores.get, reverse=True)  # type: ignore[arg-type]
    return ranked


class ExpertRouter:
    """Scores and selects top-K experts for a prompt.

    Scoring formula per expert:
        score = (w_domain * domain_relevance
               + w_speed * speed_score
               + w_capacity * capacity_score)
              * region_multiplier

    Higher score = better expert for this prompt.
    """

    def __init__(
        self,
        latency_tracker: LatencyTracker,
        region_engine: RegionEngine,
        w_domain: float = 0.35,
        w_speed: float = 0.45,
        w_capacity: float = 0.20,
        max_region_km: float = 2000.0,
    ):
        self._latency = latency_tracker
        self._region = region_engine
        self._w_domain = w_domain
        self._w_speed = w_speed
        self._w_capacity = w_capacity
        self._max_region_km = max_region_km

    def select_experts(
        self,
        prompt: str,
        experts: List[ExpertInfo],
        online_node_ids: List[str],
        k: int = 2,
        region: Optional[str] = None,
        model: Optional[str] = None,
    ) -> List[Tuple[str, float]]:
        """Return the top-K (node_id, weight) pairs for a prompt.

        Weights are normalized so they sum to 1.0.
        """
        query_domains = classify_prompt(prompt)

        candidates = [
            e for e in experts
            if e.node_id in online_node_ids
        ]

        if model:
            model_match = [e for e in candidates if model in e.models]
            if model_match:
                candidates = model_match

        if region:
            region_match = [
                e for e in candidates
                if self._region.get_location(e.node_id) is not None
                and (
                    self._region.get_location(e.node_id).region_label == region  # type: ignore[union-attr]
                    or self._region.distance_km(
                        e.node_id, candidates[0].node_id if candidates else ""
                    ) < self._max_region_km
                )
            ]
            if region_match:
                candidates = region_match

        if not candidates:
            return []

        scored: List[Tuple[str, float]] = []
        max_compute = max((e.compute_score for e in candidates), default=1.0) or 1.0

        for expert in candidates:
            domain_score = expert.domain_relevance(query_domains)

            latency_ms = self._latency.ewma(expert.node_id)
            if latency_ms <= 0:
                latency_ms = 500.0
            speed_score = 1.0 / (1.0 + latency_ms / 100.0)

            capacity_score = expert.compute_score / max_compute if max_compute > 0 else 0.5

            combined = (
                self._w_domain * domain_score
                + self._w_speed * speed_score
                + self._w_capacity * capacity_score
            )

            scored.append((expert.node_id, combined))

        scored.sort(key=lambda x: x[1], reverse=True)
        top_k = scored[:k]

        total_weight = sum(w for _, w in top_k)
        if total_weight > 0:
            top_k = [(nid, w / total_weight) for nid, w in top_k]

        return top_k

    def adaptive_k(self, prompt: str, num_available: int) -> int:
        """Choose how many experts to query based on prompt complexity.

        Simple/short prompts -> 1 expert (fastest).
        Complex/ambiguous prompts -> 2-3 experts (better quality).
        """
        domains = classify_prompt(prompt)
        word_count = len(prompt.split())

        if word_count < 10 and len(domains) <= 1:
            return 1

        if len(domains) >= 3 or word_count > 100:
            return min(3, num_available)

        return min(2, num_available)
