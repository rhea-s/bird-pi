"""The single source of truth both renderers consume.

build_gallery() turns BirdNET-Go detections into a list of GalleryEntry objects:
species name, Latin name, a humanized 'last heard', the cached plate path (or
None), and an optional verified Audubon plate number. The web and e-ink
renderers each take this same list — layout logic isn't duplicated between them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from . import config as cfgmod
from . import plates as platemod
from . import reltime
from .birdnet import BirdNetClient, Species


@dataclass
class GalleryEntry:
    common_name: str
    scientific_name: str
    last_heard_text: str          # "12 min ago"
    last_heard_iso: str           # machine-readable, for sorting/JS
    plate_url: Optional[str]      # path or web route; None if not collected
    plate_number: Optional[str]   # Roman numeral, only if verified
    count: int


# Verified Audubon plate numbers, scientific_name -> integer.
# Left to you to fill in (see plate_meta.yaml) so the Roman numeral shown on a
# plate is always the real one, never a guess. Unknown -> simply omitted.
def _load_plate_numbers(root: str) -> dict[str, int]:
    path = os.path.join(root, "plate_meta.yaml")
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        nums = data.get("plate_numbers") or {}
        return {k.strip().lower(): int(v) for k, v in nums.items()}
    except Exception:
        return {}


_ROMAN = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
          (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
          (5, "V"), (4, "IV"), (1, "I")]


def to_roman(n: int) -> str:
    out = []
    for value, sym in _ROMAN:
        while n >= value:
            out.append(sym)
            n -= value
    return "".join(out)


def build_gallery(cfg: cfgmod.Config, *, plate_url_for=None) -> list[GalleryEntry]:
    """plate_url_for: optional fn(local_path) -> str to rewrite a filesystem
    path into a web route. If None, the raw path is used (e-ink reads files
    directly)."""
    client = BirdNetClient(cfg.birdnet.base_url, timeout=cfg.birdnet.timeout)
    species = client.recent_species(limit=cfg.gallery.limit,
                                    min_confidence=cfg.gallery.min_confidence)
    pdir = cfgmod.plates_dir(cfg)
    plate_numbers = _load_plate_numbers(cfg.root)

    entries: list[GalleryEntry] = []
    for sp in species:
        local = platemod.plate_path(pdir, sp.scientific_name)
        url = None
        if local is not None:
            url = plate_url_for(local) if plate_url_for else local
        num = plate_numbers.get(sp.scientific_name.strip().lower())
        entries.append(GalleryEntry(
            common_name=sp.common_name,
            scientific_name=sp.scientific_name,
            last_heard_text=reltime.humanize(sp.last_heard),
            last_heard_iso=sp.last_heard.isoformat() if sp.last_heard else "",
            plate_url=url,
            plate_number=to_roman(num) if num else None,
            count=sp.count,
        ))
    return entries
