"""Client for BirdNET-Go's local JSON API.

We read /api/v2/analytics/species/summary, which returns one record per species
with a 'last heard' timestamp and an *all-time* detection count. For "how many
times today", we additionally read /api/v2/analytics/species/daily, which is the
per-day endpoint (date-filterable, defaults to today, and also carries an
hourly_counts breakdown on recent builds).

BirdNET-Go's exact JSON keys have drifted across releases, so nothing here
hard-codes a single shape: every field is resolved against a list of plausible
names, and `probe()` dumps the raw payload so you can see what your instance
actually returns.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date as date_cls, datetime
from typing import Any, Optional

import requests


@dataclass
class Species:
    common_name: str
    scientific_name: str
    last_heard: Optional[datetime]
    count: int = 0                      # all-time detections (species/summary)
    confidence: Optional[float] = None
    count_today: Optional[int] = None   # detections so far today (species/daily)
    hourly: Optional[list[int]] = None  # 24 ints, today's detections by local hour
    rarity: Optional[str] = None        # display label, e.g. "Rare"
    rarity_level: Optional[str] = None  # slug: very-common…very-rare (for styling)


# Candidate key names, in priority order, seen across BirdNET-Go versions.
_COMMON = ("common_name", "commonName", "comName", "common", "label")
_SCI = ("scientific_name", "scientificName", "sciName", "scientific", "latin")
_LAST = ("last_heard", "lastHeard", "latest_detection", "latestDetection",
         "last_seen", "lastSeen", "last_detection_at")
_COUNT = ("count", "total_detections", "totalDetections", "detections",
          "detection_count", "n")
_CONF = ("confidence", "max_confidence", "maxConfidence", "latest_confidence")
_HOURLY = ("hourly_counts", "hourlyCounts", "hourly", "counts_by_hour")
# When hourly arrives as a list of objects, the hour index lives under one of:
_HOUR_KEYS = ("hour", "h", "hour_of_day", "hourOfDay", "index", "idx")
# Rarity comes from the range-filter geomodel: either a ready-made label
# ("Very Rare") or the raw 0..1 occurrence probability we bucket ourselves.
_RARITY = ("rarity", "rarity_label", "rarityLabel", "occurrence_label", "status")
_SCORE = ("rarity_score", "rarityScore", "occurrence", "occurrence_score",
          "occurrenceScore", "range_score", "rangeScore", "probability")


def _first(d: dict, keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _as_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
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


def _daily_count(row: dict) -> int:
    """Today's detection count for one species/daily record. Prefers an explicit
    total; falls back to summing hourly_counts if that's all the build exposes."""
    c = _as_int(_first(row, _COUNT, 0))
    if c:
        return c
    hourly = _first(row, _HOURLY)
    if isinstance(hourly, (list, tuple)):
        return sum(_as_int(x) for x in hourly)
    return 0


def _normalize_hourly(value) -> Optional[list[int]]:
    """Coerce a daily record's hourly breakdown into a 24-int list indexed by
    local hour, or None if it's absent or unrecognized. Accepts a 24-length list
    of ints, a dict like {"0": n, "13": n}, or a list of
    {"hour": h, "count": n} objects — the shape has drifted across builds."""
    if value is None:
        return None
    if isinstance(value, dict):
        arr = [0] * 24
        for k, v in value.items():
            try:
                h = int(str(k).strip())
            except (TypeError, ValueError):
                continue
            if 0 <= h < 24:
                arr[h] = _as_int(v)
        return arr
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], dict):     # [{"hour": h, "count": n}, …]
            arr = [0] * 24
            for item in value:
                h = _as_int(_first(item, _HOUR_KEYS, -1), -1)
                if 0 <= h < 24:
                    arr[h] = _as_int(_first(item, _COUNT, 0))
            return arr
        vals = [_as_int(x) for x in value]           # plain [n, n, … ]
        if len(vals) >= 24:
            return vals[:24]
        if vals:
            return vals + [0] * (24 - len(vals))
    return None


# Occurrence-probability buckets (range-filter score 0..1 -> tier). Thresholds
# echo BirdNET-Go's own guidance: 0.5+ = most common, 0.1–0.3 strict, ~0.05
# fewer, 0.01 default/permissive.
_RARITY_BUCKETS = [           # (min_score, slug, display)
    (0.50, "very-common", "Very Common"),
    (0.30, "common",      "Common"),
    (0.10, "uncommon",    "Uncommon"),
    (0.03, "rare",        "Rare"),
    (0.00, "very-rare",   "Very Rare"),
]
# Match longest phrases first so "very rare" beats "rare", "uncommon" beats
# "common", etc.
_RARITY_KEYWORDS = [
    ("very common", "very-common", "Very Common"),
    ("very-common", "very-common", "Very Common"),
    ("abundant",    "very-common", "Very Common"),
    ("very rare",   "very-rare",   "Very Rare"),
    ("very-rare",   "very-rare",   "Very Rare"),
    ("uncommon",    "uncommon",    "Uncommon"),
    ("occasional",  "uncommon",    "Uncommon"),
    ("common",      "common",      "Common"),
    ("frequent",    "common",      "Common"),
    ("rare",        "rare",        "Rare"),
    ("scarce",      "rare",        "Rare"),
]


def _normalize_rarity(label, score):
    """Return (level_slug, display_label), or (None, None) if unknown.

    Prefers a textual label from BirdNET-Go (stripping any trailing "0%"), and
    otherwise buckets the raw 0..1 occurrence score."""
    if label not in (None, ""):
        text = re.sub(r"\s*\d+(?:\.\d+)?\s*%\s*$", "", str(label).strip().lower())
        for key, slug, disp in _RARITY_KEYWORDS:
            if key in text:
                return slug, disp
        if text:                       # unknown wording: show it, no styling tier
            return None, str(label).strip().title()
    if score not in (None, ""):
        try:
            s = float(score)
        except (TypeError, ValueError):
            return None, None
        for lo, slug, disp in _RARITY_BUCKETS:
            if s >= lo:
                return slug, disp
        return "very-rare", "Very Rare"
    return None, None


def _species_record(payload, scientific_name):
    """Pull the record for one species out of a /api/v2/species response, which
    may be a bare object, a wrapped object, or a list to match by name."""
    if isinstance(payload, dict):
        for key in ("species", "data", "result", "results", "items"):
            v = payload.get(key)
            if isinstance(v, dict):
                return v
            if isinstance(v, list):
                payload = v
                break
        else:
            return payload  # the dict itself is the record
    if isinstance(payload, list):
        target = scientific_name.strip().lower()
        for row in payload:
            if isinstance(row, dict) and \
                    str(_first(row, _SCI, "")).strip().lower() == target:
                return row
        return payload[0] if payload and isinstance(payload[0], dict) else None
    return None


def _resolve_rarity_fields(rec):
    """Find a rarity label and/or occurrence score in a species record, looking
    one level into a nested range/occurrence object if needed."""
    label = _first(rec, _RARITY)
    score = _first(rec, _SCORE)
    if label is None and score is None:
        for sub in ("range", "rangefilter", "range_filter", "occurrence",
                    "geomodel", "rarity"):
            v = rec.get(sub)
            if isinstance(v, dict):
                label = label or _first(v, _RARITY)
                score = score or _first(v, _SCORE)
            elif isinstance(v, (int, float)):
                score = score if score is not None else v
    return label, score


# Rarity is location+week based, so it's stable for hours: cache it across
# gallery builds (module-level so it's shared by the per-request clients).
_SPECIES_PATH = "/api/v2/species"
_RARITY_TTL = 6 * 3600        # cache a successful lookup this long
_RARITY_FAIL_TTL = 600        # retry a failed/empty lookup sooner
_rarity_cache: dict = {}      # sci_lower -> (expiry_ts, level, label)


class BirdNetClient:
    def __init__(self, base_url: str, timeout: int = 8):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None,
             timeout: int | None = None) -> Any:
        url = f"{self.base}{path}"
        resp = requests.get(url, params=params,
                            timeout=timeout or self.timeout,
                            headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _as_list(payload: Any) -> list:
        """summary/daily may be a bare array or wrapped in an object."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("species", "species_list", "data", "results", "items"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        return []

    def daily_detail(self, day: date_cls | str | None = None) -> dict[str, dict]:
        """Return {name_key: {"count": int, "hourly": [24 ints] | None}} for a
        single day (default today), keyed by both scientific and common name
        (lowercased) so callers can match on either. Returns {} if the endpoint
        is unavailable (older builds) or the request fails, so callers degrade
        gracefully.

        'count' is today's total; 'hourly' is the per-hour breakdown indexed by
        BirdNET-Go's local hour, or None if the build doesn't expose it.

        Note: 'today' is this machine's local date, and the hourly indices are
        the server's local hours. If birdgallery runs in a different timezone
        than the Pi, pass an explicit `day` near midnight and expect the
        morning/afternoon split to be shifted by the offset.
        """
        if day is None:
            day = datetime.now().date()
        if hasattr(day, "isoformat"):
            day = day.isoformat()
        try:
            payload = self._get("/api/v2/analytics/species/daily",
                                {"date": day})
        except requests.RequestException:
            return {}
        out: dict[str, dict] = {}
        for row in self._as_list(payload):
            if not isinstance(row, dict):
                continue
            rec = {"count": _daily_count(row),
                   "hourly": _normalize_hourly(_first(row, _HOURLY))}
            sci = str(_first(row, _SCI, "")).strip().lower()
            com = str(_first(row, _COMMON, "")).strip().lower()
            if sci:
                out[sci] = rec
            if com:
                out.setdefault(com, rec)
        return out

    def daily_counts(self, day: date_cls | str | None = None) -> dict[str, int]:
        """{name_key: today's count}. Thin wrapper over daily_detail() for
        callers that don't need the hourly breakdown."""
        return {k: v["count"] for k, v in self.daily_detail(day).items()}

    def species_rarity(self, scientific_name: str) -> tuple:
        """Return (level_slug, display_label) for a species via /api/v2/species,
        cached. Returns (None, None) if the endpoint is unavailable or carries
        no rarity. Rarity isn't on the analytics summary, so this is a separate
        per-species lookup — heavily cached because it changes only by week."""
        key = (scientific_name or "").strip().lower()
        if not key:
            return (None, None)
        now = time.time()
        hit = _rarity_cache.get(key)
        if hit and hit[0] > now:
            return hit[1], hit[2]

        level = label = None
        ok = False
        try:
            # Pass the name under several plausible param keys; the server uses
            # whichever it recognizes and ignores the rest.
            payload = self._get(_SPECIES_PATH, {
                "scientific_name": scientific_name,
                "scientificName": scientific_name,
                "name": scientific_name,
                "species": scientific_name,
                "q": scientific_name,
            }, timeout=min(self.timeout, 5))
            rec = _species_record(payload, scientific_name)
            if rec is not None:
                level, label = _normalize_rarity(*_resolve_rarity_fields(rec))
                ok = True
        except requests.RequestException:
            ok = False

        _rarity_cache[key] = (now + (_RARITY_TTL if ok else _RARITY_FAIL_TTL),
                              level, label)
        return level, label

    def recent_species(self, limit: int = 12,
                       min_confidence: float = 0.0,
                       include_today: bool = False,
                       include_rarity: bool = False) -> list[Species]:
        payload = self._get("/api/v2/analytics/species/summary")
        out: list[Species] = []
        for row in self._as_list(payload):
            if not isinstance(row, dict):
                continue
            conf_raw = _first(row, _CONF)
            conf = float(conf_raw) if conf_raw is not None else None
            if conf is not None and conf < min_confidence:
                continue
            level, label = _normalize_rarity(_first(row, _RARITY),
                                             _first(row, _SCORE))
            out.append(Species(
                common_name=str(_first(row, _COMMON, "Unknown")),
                scientific_name=str(_first(row, _SCI, "")),
                last_heard=_parse_dt(_first(row, _LAST)),
                count=_as_int(_first(row, _COUNT, 0)),
                confidence=conf,
                rarity=label,
                rarity_level=level,
            ))
        # Most recently heard first; records with no timestamp sink to the end.
        out.sort(key=lambda s: (s.last_heard is not None, s.last_heard),
                 reverse=True)
        result = out[:limit]

        # Enrich the displayed slice with today's per-species count. Done after
        # slicing so we only pay one extra request, and never in the plate
        # worker's path (which leaves include_today False).
        if include_today and result:
            today = self.daily_detail()
            if today:
                for s in result:
                    rec = today.get(s.scientific_name.strip().lower())
                    if rec is None and s.common_name:
                        rec = today.get(s.common_name.strip().lower())
                    if rec is not None:
                        s.count_today = rec["count"]
                        s.hourly = rec["hourly"]

        # Enrich with rarity (separate per-species endpoint; cached). Only fetch
        # for species the summary didn't already carry a rarity for.
        if include_rarity:
            for s in result:
                if s.rarity is None:
                    s.rarity_level, s.rarity = self.species_rarity(
                        s.scientific_name)
        return result

    def probe(self) -> str:
        """Return a readable dump of the summary endpoint for debugging keys."""
        payload = self._get("/api/v2/analytics/species/summary")
        rows = self._as_list(payload)
        head = rows[0] if rows else payload
        return (f"Endpoint: {self.base}/api/v2/analytics/species/summary\n"
                f"Records: {len(rows)}\n"
                f"First record:\n{json.dumps(head, indent=2, default=str)}")

    def probe_daily(self, day: date_cls | str | None = None) -> str:
        """Dump the raw species/daily response so you can confirm whether your
        build exposes an hourly breakdown (and under what key/shape)."""
        if day is None:
            day = datetime.now().date()
        if hasattr(day, "isoformat"):
            day = day.isoformat()
        try:
            payload = self._get("/api/v2/analytics/species/daily", {"date": day})
        except requests.RequestException as exc:
            return f"GET {self.base}/api/v2/analytics/species/daily failed: {exc}"
        rows = self._as_list(payload)
        head = rows[0] if rows else payload
        return (f"Endpoint: {self.base}/api/v2/analytics/species/daily?date={day}\n"
                f"Records: {len(rows)}\n"
                f"First record:\n{json.dumps(head, indent=2, default=str)}")

    def probe_species(self, scientific_name: str = "Cardinalis cardinalis") -> str:
        """Dump the raw /api/v2/species response for one species so you can see
        where the rarity score lives (and under what key) on your instance."""
        try:
            payload = self._get(_SPECIES_PATH, {
                "scientific_name": scientific_name,
                "name": scientific_name,
                "species": scientific_name,
                "q": scientific_name,
            }, timeout=min(self.timeout, 5))
        except requests.RequestException as exc:
            return f"GET {self.base}{_SPECIES_PATH} failed: {exc}"
        return (f"Endpoint: {self.base}{_SPECIES_PATH}  (species={scientific_name})\n"
                f"{json.dumps(payload, indent=2, default=str)[:2000]}")
