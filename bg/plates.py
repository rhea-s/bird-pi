"""Resolve and cache Audubon plates.

Renderers never touch the network: they look up a local file named after the
species' scientific name. Populating that cache is a separate, explicit step
(fetch_plates.py), so you stay in control of which image represents each bird.

The fetcher queries Wikimedia Commons live — it never invents URLs. It searches
the File namespace and downloads the top match. Anything it can't resolve, or
gets wrong, you can override by dropping your own JPG into the illustrations
folder named <genus_species>.jpg (e.g. cardinalis_cardinalis.jpg).
"""
from __future__ import annotations

import os
import re
import time
from typing import Optional
from PIL import Image, ImageChops

import requests

session = requests.Session()
session.headers["User-Agent"] = (
    "birdgallery/0.1 (personal project)"
)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def slugify(scientific_name: str) -> str:
    s = scientific_name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def plate_path(plates_dir: str, scientific_name: str) -> Optional[str]:
    """Return the cached plate path for a species, or None if not collected."""
    if not scientific_name:
        return None
    slug = slugify(scientific_name)
    for ext in _EXTS:
        candidate = os.path.join(plates_dir, slug + ext)
        if os.path.exists(candidate):
            return candidate
    return None


def normalize_plate(path: str,
                    padding_ratio: float = 0.06,
                    background_threshold: int = 245) -> None:
    """
    Crop away empty paper around an Audubon plate and rescale so the
    artwork fills most of the frame.

    Operates in-place.
    """

    img = Image.open(path).convert("RGB")

    gray = img.convert("L")

    mask = gray.point(
        lambda p: 0 if p > 245 else 255,
        mode="1"
    )

    bbox = mask.getbbox()

    if not bbox:
        return

    left, top, right, bottom = bbox

    # Add a little breathing room.
    pad_x = int((right - left) * padding_ratio)
    pad_y = int((bottom - top) * padding_ratio)

    left = max(0, left - pad_x)
    top = max(0, top - pad_y)
    right = min(img.width, right + pad_x)
    bottom = min(img.height, bottom + pad_y)

    cropped = img.crop((left, top, right, bottom))

    cropped.save(path, quality=95)
    

def _search_commons(query: str, timeout: int = 15) -> list[dict]:
    """Return File-namespace results with direct image URLs, best first."""
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": 6,        # File: namespace
        "gsrlimit": 8,
        "prop": "imageinfo",
        "iiprop": "url|size|mime",
        "iiurlwidth": 1600,       # ask for a sensibly sized render
    }
    resp = session.get(COMMONS_API,
                   params=params,
                   timeout=timeout)
    resp.raise_for_status()
    pages = (resp.json().get("query") or {}).get("pages") or {}
    rows = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("url")
        mime = info.get("mime", "")
        if url and mime.startswith("image/"):
            rows.append({"title": page.get("title", ""), "url": url,
                         "mime": mime, "index": page.get("index", 999)})
    rows.sort(key=lambda r: r["index"])
    return rows


def fetch_plate(plates_dir: str, common_name: str, scientific_name: str,
                query_suffix: str, *, dry_run: bool = False,
                timeout: int = 15) -> Optional[str]:
    """Resolve a plate from Commons and cache it. Returns the saved path,
    or None if nothing suitable was found."""
    os.makedirs(plates_dir, exist_ok=True)
    # Scientific name first (Audubon's plates are reliably tagged with the
    # binomial on Commons), then fall back to the common name.
    queries = [
        f"{scientific_name} {query_suffix}",
        f"{common_name} {query_suffix}",
    ]
    for q in queries:
        if not q.strip():
            continue
        try:
            hits = _search_commons(q, timeout=timeout)
        except requests.RequestException as exc:
            print(f"   ! Commons search failed for '{q}': {exc}")
            continue
        if not hits:
            continue
        best = hits[0]
        print(f"   -> {best['title']}")
        if dry_run:
            return best["title"]
        ext = os.path.splitext(best["url"].split("?")[0])[1].lower()
        if ext not in _EXTS:
            ext = ".jpg"
        dest = os.path.join(plates_dir, slugify(scientific_name) + ext)
        try:
            for attempt in range(5):
                img = session.get(best["url"], timeout=timeout)

                if img.status_code == 429:
                    wait = 2 ** attempt
                    print(f"   ! rate limited, retrying in {wait}s")
                    time.sleep(wait)
                    continue

                img.raise_for_status()

                with open(dest, "wb") as fh:
                    fh.write(img.content)

                try:
                    normalize_plate(dest)
                except Exception as exc:
                    print(f"   ! normalize failed: {exc}")

                return dest

            raise requests.HTTPError("Too many 429 responses")
        except requests.RequestException as exc:
            print(f"   ! download failed: {exc}")
            return None
    return None
