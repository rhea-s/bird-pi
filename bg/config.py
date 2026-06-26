"""Load config.yaml over a set of defaults. No surprises: anything you omit
falls back to the values here."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


@dataclass
class BirdnetCfg:
    base_url: str = "http://localhost:8080"
    timezone_is_local: bool = True
    timeout: int = 8


@dataclass
class GalleryCfg:
    limit: int = 12
    min_confidence: float = 0.0


@dataclass
class PlatesCfg:
    dir: str = "illustrations"
    # Back-compat single suffix. Prefer fetch_sources below; this is only used
    # as a fallback when fetch_sources is empty.
    fetch_query_suffix: str = "John James Audubon Birds of America"
    # Illustration sources, tried in order until an on-topic plate is found.
    # Audubon covers North American natives; the Old World engravers below fill
    # introduced / Eurasian species (House Sparrow, Starling, …) in the same
    # plate aesthetic. Gould's coverage on Commons is patchy per-species, so
    # Yarrell/Naumann/Morris are included as further fallbacks.
    fetch_sources: list = field(default_factory=lambda: [
        "John James Audubon Birds of America",
        "John Gould Birds of Great Britain",
        "William Yarrell A History of British Birds",
        "Naumann Naturgeschichte der Vögel Mitteleuropas",
        "Francis Orpen Morris A History of British Birds",
    ])
    # Last-resort CC photo from Avicommons when no illustration is found. Off by
    # default: photos clash with the plates and dither poorly on e-ink, and many
    # are cc-by-nc (attribution required, written to <slug>.credit.json).
    photo_fallback: bool = False
    # Background auto-fetch (see platefetcher.py / web.py). Set auto_fetch:false
    # to keep fetching a manual fetch_plates.py step.
    auto_fetch: bool = True
    auto_fetch_interval: int = 120  # seconds between polls for missing plates


@dataclass
class WebCfg:
    host: str = "0.0.0.0"
    port: int = 8000
    refresh_seconds: int = 60


@dataclass
class EinkCfg:
    width: int = 800
    height: int = 480
    palette: str = "bw"
    columns: int = 3
    rows: int = 2
    driver: str = "save"
    output_png: str = "eink_out.png"
    waveshare_module: str = "waveshare_epd.epd7in5_V2"


@dataclass
class Config:
    birdnet: BirdnetCfg = field(default_factory=BirdnetCfg)
    gallery: GalleryCfg = field(default_factory=GalleryCfg)
    plates: PlatesCfg = field(default_factory=PlatesCfg)
    web: WebCfg = field(default_factory=WebCfg)
    eink: EinkCfg = field(default_factory=EinkCfg)
    # Absolute path of the project root, filled in at load time.
    root: str = "."


def _apply(dc: Any, data: dict) -> None:
    """Overlay a plain dict onto a (possibly nested) dataclass instance."""
    if not data:
        return
    valid = {f.name: f for f in fields(dc)}
    for key, value in data.items():
        if key not in valid:
            continue
        current = getattr(dc, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value)
        else:
            setattr(dc, key, value)


def load(path: str | None = None) -> Config:
    cfg = Config()
    if path is None:
        # Look next to the project root (one level above this package).
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
    cfg.root = os.path.dirname(os.path.abspath(path))
    if os.path.exists(path) and yaml is not None:
        with open(path, "r", encoding="utf-8") as fh:
            _apply(cfg, yaml.safe_load(fh) or {})
    return cfg


def plates_dir(cfg: Config) -> str:
    d = cfg.plates.dir
    return d if os.path.isabs(d) else os.path.join(cfg.root, d)


def plate_sources(cfg: Config) -> list:
    """Effective ordered list of illustration source suffixes. Prefers
    plates.fetch_sources; falls back to the single fetch_query_suffix so older
    configs keep working."""
    srcs = [s for s in (cfg.plates.fetch_sources or []) if s and str(s).strip()]
    if srcs:
        return srcs
    suffix = (cfg.plates.fetch_query_suffix or "").strip()
    return [suffix] if suffix else []
