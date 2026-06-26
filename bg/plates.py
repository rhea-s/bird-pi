"""Resolve and cache Audubon plates.

Renderers never touch the network: they look up a local file named after the
species' scientific name. Populating that cache is a separate, explicit step
(fetch_plates.py), so you stay in control of which image represents each bird.

The fetcher queries Wikimedia Commons live — it never invents URLs. It searches
the File namespace and downloads the top match. Anything it can't resolve, or
gets wrong, you can override by dropping your own JPG into the illustrations
folder named <genus_species>.jpg (e.g. cardinalis_cardinalis.jpg).

Normalization (cropping the artwork out of its sea of paper, and rotating
landscape scans upright) runs automatically right after each download, using
the same content-detection logic as the standalone normalize_plates.py CLI.
If numpy/scipy aren't available it falls back to a simple white-margin crop.

Only true raster images (jpeg/png/webp/gif) are accepted: Commons also returns
DjVu/PDF book scans for some queries (e.g. Audubon's "Ornithological Biography"
text volumes), and those report an image/* MIME but can't be opened as images.
Downloads are validated before they're written, so a stray document never gets
cached as a broken <species>.jpg.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import time
from typing import Optional

import requests
from PIL import Image

try:
    import numpy as np
    from scipy import ndimage
    _HAVE_CV = True
    _LUM = np.array([0.299, 0.587, 0.114], dtype=np.float32)
except ImportError:  # detection deps missing -> fall back to a simple crop
    _HAVE_CV = False

session = requests.Session()
session.headers["User-Agent"] = (
    "birdgallery/0.1 (personal project)"
)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_EXTS = (".jpg", ".jpeg", ".png", ".webp")
# MIME types we can actually open and serve in an <img>. NB: DjVu reports
# image/vnd.djvu and PDF reports application/pdf — both must be excluded even
# though DjVu's type starts with "image/".
_RASTER_MIMES = ("image/jpeg", "image/png", "image/webp", "image/gif")
# Pristine downloads are kept here (a subfolder of the serving dir) so plates
# can be re-normalized later with tweaked settings without re-hitting Commons.
# It's a subdir of plates_dir, but plate_path / the renderers only look at the
# top level, so raw files are never served or treated as cached plates.
_RAW_SUBDIR = "_raw"

# Avicommons: a curated CC photo per species, keyed by scientific name.
_AVICOMMONS_MANIFEST = "https://avicommons.org/latest.json"
_AVICOMMONS_IMG = "https://static.avicommons.org/{code}-{key}-{size}.jpg"
_avi_index = None  # lazy {sciName.lower(): record} cache


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


# --------------------------------------------------------------------------- #
# Content detection (shared logic with normalize_plates.py)
# --------------------------------------------------------------------------- #
def _content_mask(a, dark=25.0, sat=12.0, min_blob=0.0005):
    """Boolean content mask for an HxWx3 float32 array.

    Robust to cream paper, foxing, and scanner shadows: pixels count as
    content only if they're meaningfully darker OR more saturated than the
    sampled paper color, and tiny speckle blobs are discarded.
    """
    paper = np.median(a.reshape(-1, 3), axis=0)
    darker = float(paper @ _LUM) - (a @ _LUM)
    colorful = (a.max(2) - a.min(2)) - float(paper.max() - paper.min())
    mask = (darker > dark) | (colorful > sat)

    mask = ndimage.binary_opening(mask, structure=np.ones((3, 3)), iterations=2)
    lbl, n = ndimage.label(mask)
    if n:
        h, w = mask.shape
        sizes = ndimage.sum(np.ones_like(lbl), lbl, range(1, n + 1))
        keep = np.where(sizes >= min_blob * h * w)[0] + 1
        mask = np.isin(lbl, keep)
    return mask


def _mask_box(mask, trim=0.2):
    """(x0, y0, x1, y1) bounding box on a boolean mask, trimming the sparsest
    `trim`% of content pixels per edge."""
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    p = max(0.0, min(5.0, trim))
    x0 = int(np.percentile(xs, p)); x1 = int(np.ceil(np.percentile(xs, 100 - p)))
    y0 = int(np.percentile(ys, p)); y1 = int(np.ceil(np.percentile(ys, 100 - p)))
    return x0, y0, x1, y1


def _small_mask(img, dark, sat, min_blob):
    """Downscale `img` for detection and return (mask, scale)."""
    W, H = img.size
    scale = max(1, round(max(W, H) / 1600))
    small = img.resize((max(1, W // scale), max(1, H // scale)), Image.BILINEAR)
    a = np.asarray(small, dtype=np.float32)
    return _content_mask(a, dark, sat, min_blob), scale


def _simple_normalize(path, out_path, padding_ratio):
    """Fallback used when numpy/scipy aren't installed: crop white margins.

    Writes to `out_path` (which may equal `path` for in-place use). Returns True
    if it wrote a cropped image, False if it found nothing to crop (in which
    case the caller is responsible for placing a file at out_path).
    """
    img = Image.open(path).convert("RGB")
    gray = img.convert("L")
    mask = gray.point(lambda p: 0 if p > 245 else 255, mode="1")
    bbox = mask.getbbox()
    if not bbox:
        return False
    left, top, right, bottom = bbox
    pad_x = int((right - left) * padding_ratio)
    pad_y = int((bottom - top) * padding_ratio)
    left = max(0, left - pad_x)
    top = max(0, top - pad_y)
    right = min(img.width, right + pad_x)
    bottom = min(img.height, bottom + pad_y)
    img.crop((left, top, right, bottom)).save(out_path, quality=95)
    return True


def normalize_plate(path: str,
                    out_path: Optional[str] = None,
                    *,
                    padding_ratio: float = 0.06,
                    auto_rotate: bool = True,
                    dark: float = 25.0,
                    sat: float = 12.0,
                    min_blob: float = 0.0005,
                    trim: float = 0.2) -> bool:
    """Crop away the empty paper around an Audubon plate (and rotate a
    landscape scan upright) so the artwork fills the frame.

    Reads `path`; writes the result to `out_path` (defaults to `path`, i.e.
    in-place). Returns True if a normalized image was written, False if nothing
    was detected (in which case the caller should place a file at out_path —
    e.g. by copying the raw source — if it needs one).

    Shares its content-detection logic with normalize_plates.py. The cache
    stores a tight crop; canvas compositing / aspect-ratio padding is left to
    the display layer (CLI or e-ink renderer).
    """
    save_to = out_path or path

    if not _HAVE_CV:
        return _simple_normalize(path, save_to, padding_ratio)

    img = Image.open(path).convert("RGB")
    W, H = img.size
    mask, scale = _small_mask(img, dark, sat, min_blob)
    box = _mask_box(mask, trim)
    if box is None:
        return False  # nothing detected -> caller decides what lands at out_path

    # Auto-rotate landscape scans of portrait plates.
    if auto_rotate and W > H:
        bx0, by0, bx1, by1 = box
        cw, ch = bx1 - bx0, by1 - by0
        if cw > 0 and ch > 0 and cw / ch >= 1.15:
            best = None  # (score, rotated_img, scale, mask, box)
            for angle in (90, 270):
                cand = img.rotate(angle, expand=True)
                cmask, cscale = _small_mask(cand, dark, sat, min_blob)
                cbox = _mask_box(cmask, trim)
                if cbox is None:
                    continue
                a0, b0, a1, b1 = cbox
                if (b1 - b0) <= (a1 - a0):   # still landscape -> skip
                    continue
                # Right-side-up score: Audubon subjects carry more ink mass in
                # the top half of the content box than the bottom.
                half = (b1 - b0) // 2
                sl = cmask[b0:b1, a0:a1]
                score = float(sl[:half, :].sum() - sl[half:, :].sum())
                if best is None or score > best[0]:
                    best = (score, cand, cscale, cmask, cbox)
            if best is not None:
                _, img, scale, mask, box = best
                W, H = img.size

    x0, y0, x1, y1 = box
    x0 *= scale; y0 *= scale
    x1 = int(np.ceil(x1 * scale)); y1 = int(np.ceil(y1 * scale))
    pad_x = int((x1 - x0) * padding_ratio)
    pad_y = int((y1 - y0) * padding_ratio)
    x0 = max(0, x0 - pad_x); y0 = max(0, y0 - pad_y)
    x1 = min(W, x1 + pad_x); y1 = min(H, y1 + pad_y)
    img.crop((x0, y0, x1, y1)).save(save_to, quality=95)
    return True


# --------------------------------------------------------------------------- #
# Wikimedia Commons fetch
# --------------------------------------------------------------------------- #
def _search_commons(query: str, timeout: int = 15) -> list[dict]:
    """Return File-namespace results with direct raster image URLs, best first.

    Only jpeg/png/webp/gif are kept; DjVu/PDF/SVG/TIFF results (book scans,
    vector files) are dropped because they can't be opened or served as plates.
    Prefers the 1600px render (`thumburl`) over the full original (`url`).
    """
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": 6,        # File: namespace
        "gsrlimit": 8,
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
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
        mime = (info.get("mime") or "").lower()
        if mime not in _RASTER_MIMES:
            continue  # skip DjVu/PDF/SVG/TIFF (e.g. Ornithological Biography)
        # Prefer the scaled render; fall back to the original if absent.
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        title = page.get("title", "")
        # A relevance haystack: title + Commons object name + description. Many
        # historical plates are filed under a common name with the binomial only
        # in the description, so matching the title alone is too brittle.
        ext = info.get("extmetadata") or {}
        def _md(key):
            v = ext.get(key)
            return v.get("value", "") if isinstance(v, dict) else ""
        desc = re.sub(r"<[^>]+>", " ", f"{_md('ObjectName')} {_md('ImageDescription')}")
        haystack = f"{title} {desc}"[:600].lower()
        rows.append({"title": title, "url": url, "mime": mime,
                     "index": page.get("index", 999), "text": haystack})
    rows.sort(key=lambda r: r["index"])
    return rows


def _looks_like_image(data: bytes) -> bool:
    """True if `data` is something PIL can actually decode."""
    try:
        Image.open(io.BytesIO(data)).verify()
        return True
    except Exception:
        return False


def _is_blank_scan(data: bytes, *, margin: float = 0.06,
                   edge_thresh: float = 24.0,
                   min_edge_frac: float = 0.005,
                   min_ink_frac: float = 0.01) -> bool:
    """True if `data` is the blank *back* (verso) of a plate leaf rather than an
    illustration. A verso is smooth pale paper — only a faint low-contrast
    show-through from the picture on the other side — so it has almost no edge
    structure and almost no strong dark ink. A real plate clears one or both
    bars easily, whether it's a hand-coloured Audubon sheet or a black-and-white
    engraving. Requires BOTH signals to be near-zero before rejecting, so a
    valid plate need only show structure OR ink to be kept.

    An outer `margin` is dropped first to discard the near-black scan border
    that frames many Commons book scans (it's dark but carries no real content).
    No-op without numpy: blank scans were only ever cached on the CV path, and
    the noisier non-CV fallback isn't worth the risk of rejecting a real plate.
    """
    if not _HAVE_CV:
        return False
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return False  # let _looks_like_image own the undecodable case
    img.thumbnail((1000, 1000), Image.BILINEAR)
    a = np.asarray(img, dtype=np.float32)
    h, w, _ = a.shape
    my, mx = int(h * margin), int(w * margin)
    a = a[my:h - my, mx:w - mx]
    lum = a @ _LUM

    g = np.zeros_like(lum)
    g[:, :-1] += np.abs(np.diff(lum, axis=1))
    g[:-1, :] += np.abs(np.diff(lum, axis=0))
    edge_frac = float((g > edge_thresh).mean())

    paper = float(np.percentile(lum, 80))
    ink_frac = float((lum < paper - 55).mean())

    return edge_frac < min_edge_frac and ink_frac < min_ink_frac


def _download(url: str, *, timeout: int = 15, max_attempts: int = 5) -> Optional[bytes]:
    """Fetch bytes with 429 backoff. Returns None on failure / repeated 429s."""
    for attempt in range(max_attempts):
        resp = session.get(url, timeout=timeout)
        if resp.status_code == 429:
            wait = 2 ** attempt
            print(f"   ! rate limited, retrying in {wait}s")
            time.sleep(wait)
            continue
        try:
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"   ! download failed: {exc}")
            return None
        return resp.content
    print("   ! gave up after repeated 429s")
    return None


# Generic words that match too many plates to be evidence of a species match.
_RELEVANCE_STOP = {
    "the", "of", "and", "bird", "birds",
    "common", "northern", "southern", "eastern", "western",
    "american", "great", "greater", "lesser", "house",
    "european", "carolina", "crested", "spotted", "striped",
}


def _relevance_tokens(common_name: str, scientific_name: str) -> set:
    """Distinctive lowercased tokens a genuine plate title should contain: the
    genus, the species epithet, and the non-generic words of the common name.
    Generic modifiers (colours, directions, 'common', 'house', …) are dropped
    because they match unrelated plates."""
    toks = set()
    parts = scientific_name.lower().split()
    if parts:
        toks.add(parts[0])                 # genus, e.g. "passer"
        if len(parts) > 1:
            toks.add(parts[1])             # epithet, e.g. "domesticus"
    for w in re.findall(r"[a-z]+", common_name.lower()):
        if len(w) >= 4 and w not in _RELEVANCE_STOP:
            toks.add(w)
    return toks


def _hit_is_relevant(haystack: str, common_name: str, scientific_name: str) -> bool:
    """True if a result plausibly depicts the requested species.

    `haystack` is the lowercased title + Commons object name + description.
    Deliberately precision-favouring: for a species Audubon never painted
    (House Sparrow, say), no Audubon plate mentions 'passer', 'domesticus', or
    'sparrow', so every hit is rejected and the caller falls back to 'plate not
    yet collected' rather than caching the wrong bird. Checking the description
    (not just the filename) recovers plates filed under a common name with the
    binomial only in the metadata. A real plate Commons surfaces under none of
    these tokens may still be missed — drop your own file to override, or call
    with require_relevant=False to take a best guess.
    """
    toks = _relevance_tokens(common_name, scientific_name)
    if not toks:
        return True       # nothing to check against -> don't block
    return any(tok in haystack for tok in toks)


def _store_plate(plates_dir: str, scientific_name: str, url: str,
                 data: bytes) -> str:
    """Write `data` to _raw/, normalize into the serving dir, return dest."""
    ext = os.path.splitext(url.split("?")[0])[1].lower()
    if ext not in _EXTS:
        ext = ".jpg"
    slug = slugify(scientific_name)

    # Keep the pristine download in _raw/, normalize into the serving dir.
    raw_dir = os.path.join(plates_dir, _RAW_SUBDIR)
    os.makedirs(raw_dir, exist_ok=True)
    raw_dest = os.path.join(raw_dir, slug + ext)
    dest = os.path.join(plates_dir, slug + ext)
    with open(raw_dest, "wb") as fh:
        fh.write(data)

    try:
        wrote = normalize_plate(raw_dest, dest)
    except Exception as exc:
        print(f"   ! normalize failed: {exc}")
        wrote = False
    if not wrote:
        # Detection found nothing (or errored): serve the raw image as-is.
        shutil.copyfile(raw_dest, dest)
    return dest


def _avicommons_index(timeout: int = 15) -> dict:
    """Lazily load the Avicommons manifest into {sciName.lower(): record}."""
    global _avi_index
    if _avi_index is not None:
        return _avi_index
    idx: dict = {}
    try:
        resp = session.get(_AVICOMMONS_MANIFEST, timeout=timeout)
        resp.raise_for_status()
        for rec in resp.json():
            sci = (rec.get("sciName") or "").strip().lower()
            if sci:
                idx.setdefault(sci, rec)
    except (requests.RequestException, ValueError) as exc:
        print(f"   ! Avicommons manifest unavailable: {exc}")
    _avi_index = idx
    return idx


def fetch_photo(plates_dir: str, common_name: str, scientific_name: str,
                *, size: int = 900, timeout: int = 15,
                dry_run: bool = False) -> Optional[str]:
    """Cache a representative CC photo from Avicommons for a species. Returns
    the saved path (or, in dry_run, the would-be URL), or None if the species
    isn't in the manifest. Photos are stored as-is (no paper-crop normalization,
    which is meant for plates), and the photographer + license are recorded in
    <slug>.credit.json for attribution."""
    rec = _avicommons_index(timeout=timeout).get(scientific_name.strip().lower())
    if not rec:
        return None
    code, key = rec.get("code"), rec.get("key")
    if not code or not key:
        return None
    url = _AVICOMMONS_IMG.format(code=code, key=key, size=size)
    print(f"   -> avicommons:{code}  (© {rec.get('by', '?')}, {rec.get('license', '?')})")
    if dry_run:
        return url
    data = _download(url, timeout=timeout)
    if data is None or not _looks_like_image(data):
        return None

    slug = slugify(scientific_name)
    raw_dir = os.path.join(plates_dir, _RAW_SUBDIR)
    os.makedirs(raw_dir, exist_ok=True)
    raw_dest = os.path.join(raw_dir, slug + ".jpg")
    dest = os.path.join(plates_dir, slug + ".jpg")
    with open(raw_dest, "wb") as fh:
        fh.write(data)
    shutil.copyfile(raw_dest, dest)   # photo: no plate normalization

    try:
        import json
        with open(os.path.join(plates_dir, slug + ".credit.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"source": "avicommons", "by": rec.get("by"),
                       "license": rec.get("license"), "url": url}, fh)
    except Exception:
        pass
    return dest


def fetch_plate(plates_dir: str, common_name: str, scientific_name: str,
                sources, *, dry_run: bool = False, timeout: int = 15,
                require_relevant: bool = True,
                photo_fallback: bool = False,
                photo_only: bool = False) -> Optional[str]:
    """Resolve a plate from Commons and cache it. Returns the saved path,
    or None if nothing suitable was found.

    `sources` is an ordered list of query suffixes (e.g. Audubon, then Gould,
    then Yarrell); a single string is also accepted. Each source is tried in
    turn — scientific name first, then common name — and the first hit whose
    title or description plausibly depicts the species (see _hit_is_relevant)
    is taken, so an off-topic top hit is skipped rather than cached.

    Avicommons (CC photos) can be used two ways:
      * photo_fallback=True — try it only if every illustration source misses.
      * photo_only=True     — skip the illustration search entirely and fetch
                              the photo directly (an override, not a fallback).
    """
    if isinstance(sources, str):
        sources = [sources]
    os.makedirs(plates_dir, exist_ok=True)

    # Override: go straight to an Avicommons photo, no Commons search.
    if photo_only:
        return fetch_photo(plates_dir, common_name, scientific_name,
                           timeout=timeout, dry_run=dry_run)

    seen: set = set()
    for source in sources:
        # Scientific name first (Commons tags plates with the binomial), then
        # the common name. Identical queries (e.g. when only a scientific name
        # was supplied, so common == scientific) are run once.
        for name in (scientific_name, common_name):
            q = f"{name} {source}".strip()
            if not q or q in seen:
                continue
            seen.add(q)
            try:
                hits = _search_commons(q, timeout=timeout)
            except requests.RequestException as exc:
                print(f"   ! Commons search failed for '{q}': {exc}")
                continue
            if not hits:
                continue

            # Walk hits in rank order; take the first on-topic, downloadable one.
            considered = 0
            for hit in hits:
                if require_relevant and not _hit_is_relevant(
                        hit.get("text", hit["title"].lower()),
                        common_name, scientific_name):
                    continue
                considered += 1
                print(f"   -> {hit['title']}")
                if dry_run:
                    return hit["title"]
                data = _download(hit["url"], timeout=timeout)
                if data is None:
                    continue  # network failure / rate-limited out — next hit
                if not _looks_like_image(data):
                    print(f"   ! skipping non-image result: {hit['title']}")
                    continue
                return _store_plate(plates_dir, scientific_name, hit["url"], data)

            if require_relevant and considered == 0:
                print(f"   (no on-topic plate among {len(hits)} result(s) for '{q}')")

    if photo_fallback:
        return fetch_photo(plates_dir, common_name, scientific_name,
                           timeout=timeout, dry_run=dry_run)
    return None


def ensure_plate(plates_dir: str, common_name: str, scientific_name: str,
                 sources, *, timeout: int = 15,
                 photo_fallback: bool = False,
                 photo_only: bool = False) -> Optional[str]:
    """Return a cached plate path, fetching it on demand if it's missing.

    This is the entry point for 'fetch when a new bird is heard': it's a cheap
    path lookup when the plate already exists, and only hits the network on a
    cache miss. Returns None if the species couldn't be resolved.

    Note: on a miss this performs a live Commons search + download (with the
    429 backoff above), so it can block for several seconds. Call it off the
    request path — see the web integration notes.
    """
    existing = plate_path(plates_dir, scientific_name)
    if existing:
        return existing
    return fetch_plate(plates_dir, common_name, scientific_name, sources,
                       timeout=timeout, photo_fallback=photo_fallback,
                       photo_only=photo_only)
