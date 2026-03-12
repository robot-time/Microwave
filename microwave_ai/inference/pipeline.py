"""Pipeline parallelism coordinator: layer assignment, execution, and activation forwarding.

The PipelineCoordinator lives on the gateway and orchestrates distributed
inference across multiple nodes, each holding a contiguous slice of model
layers.  Activation tensors flow node-to-node via binary WebSocket frames;
the final stage produces logits and streams tokens back.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import numpy as np

from ..network.latency import LatencyTracker
from ..network.topology import TopologyManager
from ..network.region import RegionEngine
from ..protocol.messages import (
    MsgType,
    encode_message,
    decode_message,
    ConnectionPool,
)
from .tensor_transfer import serialize_activation, deserialize_activation
from .engine import get_model_meta, ModelMeta


@dataclass
class LayerAssignment:
    """Describes which node owns which layers for a pipeline stage."""

    node_id: str
    layer_start: int
    layer_end: int
    model: str
    is_first: bool = False
    is_last: bool = False


@dataclass
class Pipeline:
    """A fully-formed pipeline ready for inference."""

    pipeline_id: str
    model: str
    stages: List[LayerAssignment]
    created_at: float = field(default_factory=time.monotonic)
    request_count: int = 0

    @property
    def num_stages(self) -> int:
        return len(self.stages)

    @property
    def node_ids(self) -> List[str]:
        return [s.node_id for s in self.stages]


@dataclass
class NodeCapability:
    """Reported hardware capabilities of a node."""

    node_id: str
    vram_mb: int = 0
    ram_mb: int = 0
    compute_score: float = 0.0
    engine_type: str = "ollama"
    loaded_layers: Optional[Tuple[int, int]] = None
    loaded_model: Optional[str] = None


class PipelineCoordinator:
    """Builds and manages layer-split pipelines across the node network."""

    def __init__(
        self,
        latency_tracker: LatencyTracker,
        topology: TopologyManager,
        region_engine: RegionEngine,
        min_vram_per_layer_mb: int = 100,
    ):
        self._latency = latency_tracker
        self._topology = topology
        self._region = region_engine
        self._min_vram_per_layer = min_vram_per_layer_mb
        self._capabilities: Dict[str, NodeCapability] = {}
        self._pipelines: Dict[str, Pipeline] = {}
        self._model_pipelines: Dict[str, str] = {}
        self._conn_pool = ConnectionPool()

    def register_capability(self, cap: NodeCapability) -> None:
        self._capabilities[cap.node_id] = cap

    def remove_node(self, node_id: str) -> None:
        self._capabilities.pop(node_id, None)
        stale = [
            pid
            for pid, p in self._pipelines.items()
            if node_id in p.node_ids
        ]
        for pid in stale:
            model = self._pipelines[pid].model
            del self._pipelines[pid]
            if self._model_pipelines.get(model) == pid:
                del self._model_pipelines[model]

    def get_pipeline(self, model: str) -> Optional[Pipeline]:
        pid = self._model_pipelines.get(model)
        if pid and pid in self._pipelines:
            return self._pipelines[pid]
        return None

    def build_pipeline(
        self,
        model: str,
        candidate_node_ids: List[str],
        max_stages: int = 8,
    ) -> Optional[Pipeline]:
        """Build an optimal pipeline for a model given available nodes.

        Layer assignment is proportional to each node's VRAM, and stage
        ordering minimizes total inter-node latency.
        """
        meta = get_model_meta(model)

        pipeline_capable = [
            nid
            for nid in candidate_node_ids
            if nid in self._capabilities
            and self._capabilities[nid].engine_type != "ollama"
            and self._capabilities[nid].vram_mb > 0
        ]
        if len(pipeline_capable) < 2:
            return None

        if not self._region.can_form_pipeline(pipeline_capable):
            close_nodes = self._region.nearby_nodes(
                pipeline_capable[0], pipeline_capable
            )
            pipeline_capable = close_nodes[:max_stages]
            if len(pipeline_capable) < 2:
                return None

        num_stages = min(len(pipeline_capable), max_stages)
        ordered = self._topology.best_pipeline(
            pipeline_capable, num_stages
        )
        if ordered is None:
            ordered = self._latency.ranked(pipeline_capable)[:num_stages]

        stages = self._assign_layers(meta, ordered)
        if not stages:
            return None

        pid = uuid.uuid4().hex[:12]
        pipeline = Pipeline(pipeline_id=pid, model=model, stages=stages)
        self._pipelines[pid] = pipeline
        self._model_pipelines[model] = pid
        return pipeline

    def _assign_layers(
        self, meta: ModelMeta, ordered_nodes: List[str]
    ) -> List[LayerAssignment]:
        """Split layers proportional to each node's VRAM."""
        total_layers = meta.num_layers
        n = len(ordered_nodes)

        vram_values = []
        for nid in ordered_nodes:
            cap = self._capabilities.get(nid)
            vram_values.append(cap.vram_mb if cap else 1000)

        total_vram = sum(vram_values)
        if total_vram == 0:
            return []

        stages: List[LayerAssignment] = []
        current_layer = 0
        for i, nid in enumerate(ordered_nodes):
            if i == n - 1:
                layer_end = total_layers
            else:
                proportion = vram_values[i] / total_vram
                num_layers = max(1, round(proportion * total_layers))
                layer_end = min(current_layer + num_layers, total_layers)

            stages.append(
                LayerAssignment(
                    node_id=nid,
                    layer_start=current_layer,
                    layer_end=layer_end,
                    model=meta.name,
                    is_first=(i == 0),
                    is_last=(i == n - 1),
                )
            )
            current_layer = layer_end
            if current_layer >= total_layers:
                break

        if stages:
            stages[-1].is_last = True
            stages[-1].layer_end = total_layers

        return stages

    async def execute(
        self,
        prompt: str,
        pipeline: Pipeline,
        ws_connections: Dict[str, Any],
        ws_locks: Dict[str, asyncio.Lock],
        task_queues: Dict[str, asyncio.Queue],
        max_tokens: int = 512,
    ) -> AsyncIterator[str]:
        """Execute a full generation through the pipeline, yielding tokens.

        Flow:
        1. Send the prompt to stage 0 (embed + first layers)
        2. Each stage forwards activations to the next via binary WS
        3. Final stage produces logits, samples a token, sends it back
        4. Repeat until EOS or max_tokens
        """
        task_id = uuid.uuid4().hex
        result_queue: asyncio.Queue = asyncio.Queue()
        task_queues[task_id] = result_queue

        first_stage = pipeline.stages[0]
        ws = ws_connections.get(first_stage.node_id)
        lock = ws_locks.get(first_stage.node_id)

        if not ws or not lock:
            yield json.dumps({"error": "Pipeline stage 0 node disconnected"})
            return

        pipeline_config = {
            "stages": [
                {
                    "node_id": s.node_id,
                    "layer_start": s.layer_start,
                    "layer_end": s.layer_end,
                    "is_first": s.is_first,
                    "is_last": s.is_last,
                }
                for s in pipeline.stages
            ],
            "model": pipeline.model,
        }

        try:
            async with lock:
                await ws.send_json(
                    {
                        "type": "pipeline_start",
                        "task_id": task_id,
                        "prompt": prompt,
                        "model": pipeline.model,
                        "max_tokens": max_tokens,
                        "pipeline": pipeline_config,
                    }
                )
        except Exception as e:
            task_queues.pop(task_id, None)
            yield json.dumps({"error": f"Failed to start pipeline: {e}"})
            return

        pipeline.request_count += 1
        tokens_generated = 0
        try:
            while tokens_generated < max_tokens:
                try:
                    msg = await asyncio.wait_for(
                        result_queue.get(), timeout=120.0
                    )
                except asyncio.TimeoutError:
                    yield json.dumps({"error": "pipeline timeout"})
                    break

                if msg is None:
                    break

                if isinstance(msg, dict):
                    token = msg.get("token", "")
                    if token:
                        tokens_generated += 1
                        yield json.dumps({"response": token}) + "\n"
                    if msg.get("done"):
                        break
                elif isinstance(msg, str):
                    yield msg
        finally:
            task_queues.pop(task_id, None)

    async def notify_load_layers(
        self,
        pipeline: Pipeline,
        ws_connections: Dict[str, Any],
        ws_locks: Dict[str, asyncio.Lock],
    ) -> bool:
        """Tell each node in the pipeline to load its assigned layers."""
        success = True
        for stage in pipeline.stages:
            ws = ws_connections.get(stage.node_id)
            lock = ws_locks.get(stage.node_id)
            if not ws or not lock:
                success = False
                continue
            try:
                async with lock:
                    await ws.send_json(
                        {
                            "type": "load_layers",
                            "model": stage.model,
                            "layer_start": stage.layer_start,
                            "layer_end": stage.layer_end,
                            "pipeline_stages": [
                                {
                                    "node_id": s.node_id,
                                    "layer_start": s.layer_start,
                                    "layer_end": s.layer_end,
                                }
                                for s in pipeline.stages
                            ],
                        }
                    )
            except Exception:
                success = False
        return success

    def get_all_pipelines(self) -> List[Dict[str, Any]]:
        """Return pipeline info for the dashboard."""
        result = []
        for pid, p in self._pipelines.items():
            result.append(
                {
                    "pipeline_id": pid,
                    "model": p.model,
                    "num_stages": p.num_stages,
                    "created": p.created_at,
                    "requests": p.request_count,
                    "stages": [
                        {
                            "node_id": s.node_id,
                            "layers": f"{s.layer_start}-{s.layer_end}",
                            "is_first": s.is_first,
                            "is_last": s.is_last,
                        }
                        for s in p.stages
                    ],
                }
            )
        return result
