"""Geographic region engine with coordinate-based distance constraints."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two (lat, lon) points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))


@dataclass
class NodeLocation:
    node_id: str
    latitude: float
    longitude: float
    region_label: str = "LAN"


class RegionEngine:
    """Manages node coordinates and enforces distance constraints for pipelines."""

    def __init__(self, max_pipeline_distance_km: float = 500.0):
        self.max_distance_km = max_pipeline_distance_km
        self._locations: Dict[str, NodeLocation] = {}

    def register(
        self,
        node_id: str,
        latitude: float,
        longitude: float,
        region_label: str = "LAN",
    ) -> None:
        self._locations[node_id] = NodeLocation(
            node_id=node_id,
            latitude=latitude,
            longitude=longitude,
            region_label=region_label,
        )

    def remove(self, node_id: str) -> None:
        self._locations.pop(node_id, None)

    def distance_km(self, node_a: str, node_b: str) -> float:
        """Distance in km between two registered nodes. Returns inf if unknown."""
        loc_a = self._locations.get(node_a)
        loc_b = self._locations.get(node_b)
        if loc_a is None or loc_b is None:
            return float("inf")
        if loc_a.latitude == 0.0 and loc_a.longitude == 0.0:
            return float("inf")
        if loc_b.latitude == 0.0 and loc_b.longitude == 0.0:
            return float("inf")
        return haversine_km(
            loc_a.latitude, loc_a.longitude, loc_b.latitude, loc_b.longitude
        )

    def can_form_pipeline(self, node_ids: List[str]) -> bool:
        """Check whether all nodes in the list are within max distance of each other."""
        for i, a in enumerate(node_ids):
            for b in node_ids[i + 1 :]:
                if self.distance_km(a, b) > self.max_distance_km:
                    return False
        return True

    def filter_by_region(
        self, node_ids: List[str], region_label: str
    ) -> List[str]:
        """Return nodes matching a region label."""
        return [
            nid
            for nid in node_ids
            if nid in self._locations
            and self._locations[nid].region_label == region_label
        ]

    def nearby_nodes(
        self, reference_node: str, candidate_ids: List[str], max_km: Optional[float] = None
    ) -> List[str]:
        """Return candidates sorted by distance from reference, optionally filtered."""
        if max_km is None:
            max_km = self.max_distance_km
        ref = self._locations.get(reference_node)
        if ref is None:
            return candidate_ids

        scored = []
        for nid in candidate_ids:
            d = self.distance_km(reference_node, nid)
            if d <= max_km:
                scored.append((d, nid))
        scored.sort(key=lambda x: x[0])
        return [nid for _, nid in scored]

    def get_location(self, node_id: str) -> Optional[NodeLocation]:
        return self._locations.get(node_id)

    async def geolocate_ip(self, ip_address: str) -> Tuple[float, float]:
        """Best-effort IP geolocation via ip-api.com. Returns (0, 0) on failure."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"http://ip-api.com/json/{ip_address}",
                    params={"fields": "lat,lon,status"},
                )
                data = resp.json()
                if data.get("status") == "success":
                    return (float(data["lat"]), float(data["lon"]))
        except Exception:
            pass
        return (0.0, 0.0)
