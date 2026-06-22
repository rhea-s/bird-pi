"""Client for BirdNET-Go's local JSON API.

We read /api/v2/analytics/species/summary, which returns one record per species
with a 'last heard' timestamp. BirdNET-Go's exact JSON keys have drifted across
releases, so nothing here hard-codes a single shape: every field is resolved
against a list of plausible names, and `probe()` dumps the raw payload so you
can see what your instance actually returns.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import requests


@dataclass
class Species:
    common_name: str
    scientific_name: str
    last_heard: Optional[datetime]
    count: int = 0
    confidence: Optional[float] = None


# Candidate key names, in priority order, seen across BirdNET-Go versions.
_COMMON = ("common_name", "commonName", "comName", "common", "label")
_SCI = ("scientific_name", "scientificName", "sciName", "scientific", "latin")
_LAST = ("last_heard", "lastHeard", "latest_detection", "latestDetection",
         "last_seen", "lastSeen", "last_detection_at")
_COUNT = ("count", "total_detections", "totalDetections", "detections",
          "detection_count", "n")
_CONF = ("confidence", "max_confidence", "maxConfidence", "latest_confidence")


def _first(d: dict, keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):  # epoch seconds
        try:
            return datetime.fromtimestamp(value)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    # Normalise a trailing Z to an explicit UTC offset for fromisoformat.
    iso = s.replace("Z", "+00:00")
    for attempt in (iso, s):
        try:
            return datetime.fromisoformat(attempt)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


class BirdNetClient:
    def __init__(self, base_url: str, timeout: int = 8):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base}{path}"
        resp = requests.get(url, params=params, timeout=self.timeout,
                            headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _as_list(payload: Any) -> list:
        """summary may be a bare array or wrapped in an object."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("species", "species_list", "data", "results", "items"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        return []

    def recent_species(self, limit: int = 12,
                       min_confidence: float = 0.0) -> list[Species]:
        payload = self._get("/api/v2/analytics/species/summary")
        out: list[Species] = []
        for row in self._as_list(payload):
            if not isinstance(row, dict):
                continue
            conf_raw = _first(row, _CONF)
            conf = float(conf_raw) if conf_raw is not None else None
            if conf is not None and conf < min_confidence:
                continue
            out.append(Species(
                common_name=str(_first(row, _COMMON, "Unknown")),
                scientific_name=str(_first(row, _SCI, "")),
                last_heard=_parse_dt(_first(row, _LAST)),
                count=int(_first(row, _COUNT, 0) or 0),
                confidence=conf,
            ))
        # Most recently heard first; records with no timestamp sink to the end.
        out.sort(key=lambda s: (s.last_heard is not None, s.last_heard),
                 reverse=True)
        return out[:limit]

    def probe(self) -> str:
        """Return a readable dump of the summary endpoint for debugging keys."""
        payload = self._get("/api/v2/analytics/species/summary")
        rows = self._as_list(payload)
        head = rows[0] if rows else payload
        return (f"Endpoint: {self.base}/api/v2/analytics/species/summary\n"
                f"Records: {len(rows)}\n"
                f"First record:\n{json.dumps(head, indent=2, default=str)}")
