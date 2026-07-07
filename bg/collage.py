"""bg/collage.py — an alternate e-ink layout: a closely-arranged, non-gridded
collage of transparent bird cutouts with a compact field-guide caption per bird.

This is a *second* layout, selected by config; it does not touch the existing
plate-grid renderer. It consumes the same GalleryEntry objects build_gallery()
already produces. See render() for the eink.py entry point.

Design target: Inky Impression 7.3" Spectra 6, portrait 480x800, 6 inks.
The cutouts are read from illustrations/generated/<scientific_name>.png
(spaces -> underscores, lowercased), i.e. the same key as the plates.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- palette ----
# Warm paper + warm black keep the Cormorant character on the design preview.
PAPER      = (244, 239, 228)   # design background (warm rag paper)
PAPER_EINK = (250, 248, 242)   # near-white bg for the dither pass (clean paper)
INK        = (38, 34, 28)      # warm near-black
INK_SOFT   = (120, 112, 98)    # captions / secondary
HAIRLINE   = (176, 166, 148)   # rules
OXBLOOD    = (123, 45, 38)     # rare-species accent (matches the web gallery)

# Spectra 6 primaries (approximate, for the panel-reality preview/dither).
SPECTRA6 = [
    (24, 22, 22), (245, 243, 238), (170, 44, 40),
    (52, 116, 72), (44, 70, 140), (214, 178, 48),
]

# rarity_level -> (label, colour). Adjust keys to match your enrichment values.
RARITY_STYLE = {
    "rare":     ("rare",     OXBLOOD),
    "uncommon": ("uncommon", (138, 84, 44)),
    "common":   ("common",   INK_SOFT),
}


@dataclass
class CollageOpts:
    width: int = 480
    height: int = 800
    margin: int = 22
    gutter: int = 18          # horizontal space between cutouts
    row_gap: int = 12         # vertical space between rows
    count: int = 12           # birds shown (most-recent first)
    hero_h: int = 182         # target height of the featured (first) row
    body_h: int = 124         # target height of subsequent rows
    min_cut_h: int = 64       # never shrink a cutout below this
    generated_dir: str = "illustrations/generated"
    font_dir: str = "fonts"
    title: str = "Heard Today"
    show_header: bool = True       # masthead: title / counts / date / rule
    show_latin: bool = True        # scientific-name line under each bird
    show_meta: bool = True         # count / last-heard / rarity line under each bird
    sort: str = "recent"           # recent | rarest  — order birds appear left-to-right, top-to-bottom
    # specimen-plate layout
    specimen_hero: int = 124      # height of the central, most-recent bird
    specimen_fill: float = 1.0    # 0..1.1 — initial spread before relaxation
    specimen_decay: float = 0.30  # how much smaller outer birds get (0..1)
    specimen_overlap: float = 0.90  # lower = birds pushed further apart


# ---------------------------------------------------------------- fonts ------
class Fonts:
    """Loads Cormorant Garamond (variable) if present, else a serif fallback."""

    def __init__(self, font_dir: str):
        self._reg = self._try(os.path.join(font_dir, "CormorantGaramond.ttf"))
        self._ital = self._try(os.path.join(font_dir, "CormorantGaramond-Italic.ttf"))
        if self._reg is None:
            for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
                      "/Library/Fonts/Georgia.ttf", "/System/Library/Fonts/Supplemental/Georgia.ttf"):
                if os.path.exists(p):
                    self._reg = self._ital = p
                    break
        self._cache: dict[tuple, ImageFont.FreeTypeFont] = {}

    @staticmethod
    def _try(path):
        return path if os.path.exists(path) else None

    def get(self, size: int, *, italic=False, weight=500):
        key = (size, italic, weight)
        if key in self._cache:
            return self._cache[key]
        path = self._ital if (italic and self._ital) else self._reg
        if path is None:
            f = ImageFont.load_default()
        else:
            f = ImageFont.truetype(path, size)
            try:
                f.set_variation_by_axes([weight])   # variable-font weight
            except Exception:
                pass
        self._cache[key] = f
        return f


# ---------------------------------------------------------------- text utils -
def _text_w(draw, s, font, tracking=0.0):
    if not s:
        return 0
    w = draw.textlength(s, font=font)
    return w + tracking * (len(s) - 1)


def _draw_tracked(draw, x, y, s, font, fill, tracking=0.0, anchor_center=None):
    """Draw letter-spaced text. If anchor_center given, centre on that x."""
    total = _text_w(draw, s, font, tracking)
    cx = (anchor_center - total / 2) if anchor_center is not None else x
    for ch in s:
        draw.text((cx, y), ch, font=font, fill=fill)
        cx += draw.textlength(ch, font=font) + tracking


def _wrap_caps(draw, text, font, tracking, max_w, max_lines=2):
    """Greedy word-wrap for the letter-spaced common name (upper-cased)."""
    words = text.upper().split()
    lines, cur = [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if _text_w(draw, trial, font, tracking) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
            if len(lines) == max_lines - 1:
                break
    if cur:
        lines.append(cur)
    # if a tail remains, append it to the last line (will be slightly tight)
    used = sum(len(l.split()) for l in lines)
    if used < len(words):
        lines[-1] = lines[-1] + " " + " ".join(words[used:])
    return lines[:max_lines]


def _fit_italic(draw, fonts, text, max_w, start=13, floor=9):
    """Pick the largest italic size whose rendered width fits max_w."""
    size = start
    while size > floor:
        f = fonts.get(size, italic=True, weight=500)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 1
    return fonts.get(floor, italic=True, weight=500)


# ---------------------------------------------------------------- cutouts ----
def _key(scientific: str) -> str:
    return scientific.strip().lower().replace(" ", "_")


def _load_cutout(opts: CollageOpts, scientific: str):
    path = os.path.join(opts.generated_dir, _key(scientific) + ".png")
    if not os.path.exists(path):
        return None
    im = Image.open(path).convert("RGBA")
    bbox = im.getbbox()        # trim transparent padding for tight packing
    return im.crop(bbox) if bbox else im


# ---------------------------------------------------------------- packing ----
def _row_plan(opts):
    """A deliberately irregular rhythm of (target_height, max_items). Rows are
    capped at 3 so long common names never collide; variety comes from scale
    and count (2 vs 3), not from cramming. Tall cutouts keep their height
    because a justified row with fewer/narrower birds grows taller."""
    b = opts.body_h
    if opts.width > opts.height:
        # Landscape: a short, wide canvas wants fewer but wider rows. 3·4·5
        # sums to the default count of 12 so no bird is stranded alone on a
        # final row, and the back row stays the small/dense one — the same
        # rhythm as portrait, turned on its side.
        return [
            (opts.hero_h, 3),       # featured trio, large
            (int(b * 1.00), 4),
            (int(b * 0.86), 5),     # smaller, denser back row
        ]
    return [
        (opts.hero_h, 2),       # featured: the two most-recent, large
        (int(b * 0.95), 3),
        (int(b * 1.06), 2),     # a second, mid-scale feature
        (int(b * 0.80), 3),     # a smaller, denser trio
        (int(b * 0.92), 2),
        (int(b * 0.86), 3),
    ]


def _pack_rows(items, content_w, opts):
    """Assign items to rows following the rhythm plan, then justify each row to
    fill content_w within clamps. Variation in count *and* scale per row is what
    breaks the grid feel."""
    plan = _row_plan(opts)
    rows, i, pi, n = [], 0, 0, len(items)
    while i < n:
        target, maxn = plan[pi % len(plan)]
        pi += 1
        take = items[i:i + maxn]
        # never strand a single bird on the final row
        if (n - i - len(take)) == 1 and len(take) > 2:
            take = take[:-1]
        i += len(take)
        sum_asp = sum(it["asp"] for it in take)
        rows.append(_finalize(take, sum_asp, content_w, target, opts,
                              last=(i >= n)))
    return rows


def _finalize(items, sum_asp, content_w, target, opts, last=False):
    avail = content_w - opts.gutter * (len(items) - 1)
    row_h = avail / sum_asp
    # clamp so a sparse row doesn't balloon and a dense one doesn't vanish.
    # A looser ceiling lets a justified row keep the height it needs to fill
    # the width, which removes the side whitespace from over-clamped rows.
    row_h = max(opts.min_cut_h, min(row_h, target * 1.7))
    if last:
        row_h = min(row_h, target)        # don't stretch a lonely last row
    placed = [{**it, "h": row_h, "w": row_h * it["asp"]} for it in items]
    return {"h": row_h, "items": placed}


def _layout(items, opts, avail_h):
    """Pack, then grow OR shrink target heights until the stack fills avail_h
    (not just fits it) — so a short set doesn't leave the bottom empty."""
    content_w = opts.width - 2 * opts.margin
    hero, body = opts.hero_h, opts.body_h
    rows = _pack_rows(items, content_w, opts)
    total = 0
    for _ in range(28):
        o = CollageOpts(**{**opts.__dict__, "hero_h": hero, "body_h": body})
        rows = _pack_rows(items, content_w, o)
        total = sum(r["h"] for r in rows) + _caption_h(opts) * len(rows) \
                + opts.row_gap * (len(rows) - 1)
        if abs(total - avail_h) <= 6 or body <= opts.min_cut_h:
            break
        f = 0.94 if total > avail_h else 1.06
        hero *= f
        body *= f
    return rows, total


LABEL_H = 44   # caption budget WITH the Latin line


def _caption_h(opts):
    """Vertical room reserved under each row for its caption block; tighter as
    the Latin and meta lines are hidden (common-name-only = smallest)."""
    h = 30                              # common name, up to 2 lines
    if opts.show_latin:
        h += 9
    if opts.show_meta:
        h += 5
    return h


# ---------------------------------------------------------------- caption ----
def _draw_caption(draw, fonts, cx, top, cell_w, entry, show_latin=True,
                  show_meta=True):
    """Common name (tracked caps, <=2 lines), optional Latin italic, optional
    meta line (count / last-heard / rarity)."""
    caps = fonts.get(15, weight=600)
    tracking = 1.4
    cap_w = cell_w + 10      # allow a little bleed past a narrow cutout
    lines = _wrap_caps(draw, entry["common"], caps, tracking, cap_w)
    y = top
    for ln in lines:
        _draw_tracked(draw, 0, y, ln, caps, INK, tracking, anchor_center=cx)
        y += 15

    if show_latin:
        lat = _fit_italic(draw, fonts, entry["latin"], cap_w)
        draw.text((cx, y + 1), entry["latin"], font=lat, fill=INK_SOFT, anchor="ma")
        y += lat.size + 3

    if not show_meta:
        return
    # meta: "4× · 2h ago · rare"  (rarity coloured by level)
    meta = fonts.get(11, weight=500)
    rstyle = RARITY_STYLE.get(entry.get("rarity_level", ""), None)
    left = f"{entry['count']}\u00d7 \u00b7 {entry['last']}"
    rare_txt = ("  \u00b7  " + rstyle[0]) if rstyle else ""
    lw = draw.textlength(left, font=meta)
    rw = draw.textlength(rare_txt, font=meta)
    start = cx - (lw + rw) / 2
    draw.text((start, y), left, font=meta, fill=INK_SOFT)
    if rstyle:
        draw.text((start + lw, y), rare_txt, font=meta, fill=rstyle[1])


# ---------------------------------------------------------------- header -----
def _draw_header(draw, fonts, opts, n_species, n_calls, date_str):
    m = opts.margin
    title = fonts.get(30, weight=600)
    _draw_tracked(draw, m, m - 4, opts.title.upper(), title, INK, 3.0)
    sub = fonts.get(12, weight=500)
    draw.text((opts.width - m, m + 8),
              f"{n_species} species  \u00b7  {n_calls} calls",
              font=sub, fill=INK_SOFT, anchor="ra")
    draw.text((opts.width - m, m + 24), date_str,
              font=fonts.get(11, italic=True), fill=INK_SOFT, anchor="ra")
    ry = m + 44
    draw.line([(m, ry), (opts.width - m, ry)], fill=HAIRLINE, width=1)
    return ry + opts.row_gap


def _collect(entries, opts):
    """entries -> list of plain dicts with loaded, trimmed cutouts (missing
    cutouts dropped). Shared by both the collage and specimen layouts."""
    # Rarity rank: lower number = displayed first.
    _RARITY_RANK = {"rare": 0, "uncommon": 1, "common": 2, "": 3}

    if opts.sort == "rarest":
        entries = sorted(
            entries,
            key=lambda e: _RARITY_RANK.get(
                (getattr(e, "rarity_level", None)
                 or (e.get("rarity_level", "") if isinstance(e, dict) else "")
                 or ""),
                3,
            ),
        )
    # "recent" keeps the arrival order that build_gallery() already provides.

    items = []
    for e in entries[:opts.count]:
        sci = getattr(e, "scientific_name", "") or (
            e.get("scientific_name", "") if isinstance(e, dict) else "")
        cut = _load_cutout(opts, sci)
        if cut is None:
            continue
        get = (lambda k: (getattr(e, k, None) if not isinstance(e, dict)
                          else e.get(k)))
        items.append({
            "img": cut, "asp": cut.width / cut.height,
            "common": get("common_name") or "",
            "latin": sci,
            "last": get("last_heard_text") or "",
            "count": get("count_today") or get("count") or 0,
            "rarity_level": get("rarity_level") or "",
        })
    return items


# ---------------------------------------------------------------- compose ----
def render_collage(entries, opts: CollageOpts | None = None,
                   date_str: str = "") -> Image.Image:
    opts = opts or CollageOpts()
    fonts = Fonts(opts.font_dir)
    canvas = Image.new("RGB", (opts.width, opts.height), PAPER)
    draw = ImageDraw.Draw(canvas)

    items = _collect(entries, opts)

    if opts.show_header:
        n_calls = sum(int(it["count"]) for it in items)
        top = _draw_header(draw, fonts, opts, len(items), n_calls, date_str)
    else:
        top = opts.margin

    rows, _ = _layout(items, opts, opts.height - top - opts.margin)

    y = top
    for r in rows:
        baseline = y + r["h"]                 # cutouts stand on this line
        x = opts.margin
        # centre the justified row’s leftover (clamped rows may under/overfill)
        used = sum(it["w"] for it in r["items"]) + opts.gutter * (len(r["items"]) - 1)
        x += max(0, (opts.width - 2 * opts.margin - used) / 2)
        for it in r["items"]:
            w, h = int(it["w"]), int(r["h"])
            sprite = it["img"].resize((max(1, w), max(1, h)), Image.LANCZOS)
            canvas.paste(sprite, (int(x), int(baseline - h)), sprite)
            _draw_caption(draw, fonts, x + w / 2, baseline + 6, w, it,
                          show_latin=opts.show_latin, show_meta=opts.show_meta)
            x += w + opts.gutter
        y = baseline + _caption_h(opts) + opts.row_gap

    if opts.show_header:                       # footer rule belongs with the masthead
        fy = opts.height - opts.margin + 2
        draw.line([(opts.margin, fy - 16), (opts.width - opts.margin, fy - 16)],
                  fill=HAIRLINE, width=1)
    return canvas


# ---------------------------------------------------------------- eink dither
def to_spectra6(img: Image.Image, bg=PAPER_EINK) -> Image.Image:
    """Floyd–Steinberg dither to the 6 Spectra inks for a panel-true preview /
    for pushing to the panel if your driver expects an already-quantised image."""
    flat = Image.new("RGB", img.size, bg)
    flat.paste(img, (0, 0))
    pal = Image.new("P", (1, 1))
    table = []
    for c in SPECTRA6:
        table += list(c)
    table += [0, 0, 0] * (256 - len(SPECTRA6))
    pal.putpalette(table)
    return flat.quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG)


# ---------------------------------------------------------------- eink hook --
def render(entries, cfg) -> Image.Image:
    """Adapter for bg/eink.py. Dispatches on cfg.eink.layout ("collage" or
    "specimen"); reads optional knobs off cfg.eink with safe defaults."""
    e = getattr(cfg, "eink", None)
    pw, ph = getattr(e, "width", 800), getattr(e, "height", 480)
    opts = CollageOpts(
        width=max(pw, ph), height=min(pw, ph),     # collage is landscape
        count=getattr(e, "collage_count", 12),
        generated_dir=getattr(e, "generated_dir", "illustrations/generated"),
        font_dir=getattr(cfg, "font_dir", "fonts"),
        show_header=getattr(e, "collage_header", True),
        show_latin=getattr(e, "collage_latin", True),
        show_meta=getattr(e, "collage_meta", True),
        sort=getattr(e, "collage_sort", "recent"),
    )
    if getattr(e, "layout", "collage") == "specimen":
        return render_specimen(entries, opts)
    return render_collage(entries, opts)


# ============================================================ specimen plate =
# An overlapping naturalist's arrangement: cutouts nestle and overlap in a
# phyllotactic cluster, each tagged with a small engraved numeral; the full
# identifications live in a numbered letterpress key beneath. This keeps the
# captions from fighting the overlap — the way real specimen plates do it.
import math as _math

# small per-bird rotation so they read as pinned specimens, not stickers
_JITTER = [-7, 5, -4, 8, -6, 3, -8, 6, -3, 7, -5, 4]


def _numeral(draw, cx, top, n, fonts):
    """Engraved numeral, horizontally centred on cx, with a paper halo so it
    stays legible over any bird."""
    f = fonts.get(14, weight=600)
    s = str(n)
    x = cx - draw.textlength(s, font=f) / 2
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx or dy:
                draw.text((x + dx, top + dy), s, font=f, fill=PAPER)
    draw.text((x, top), s, font=f, fill=INK)


def _specimen_cluster(canvas, draw, fonts, items, opts, zone):
    zx0, zy0, zx1, zy1 = zone
    cx, cy = (zx0 + zx1) / 2, (zy0 + zy1) / 2
    golden = _math.radians(137.507)
    n = len(items)
    pad = 36
    half_w = max(1.0, (zx1 - zx0) / 2 - pad) * opts.specimen_fill
    half_h = max(1.0, (zy1 - zy0) / 2 - pad) * opts.specimen_fill

    placed = []
    for i, it in enumerate(items):
        ru = _math.sqrt((i + 0.5) / n)          # Vogel: uniform initial spread
        ang = i * golden
        h = opts.specimen_hero * (1 - opts.specimen_decay * (i / max(1, n - 1)))
        h = max(opts.min_cut_h, h)
        w = h * it["asp"]
        placed.append({**it, "x": cx + ru * _math.cos(ang) * half_w,
                       "y": cy + ru * _math.sin(ang) * half_h,
                       "h": h, "w": w, "rad": 0.40 * max(w, h), "num": i + 1})

    # Relaxation: overlapping birds shove each other apart until they settle
    # into the open space, so nothing buries its neighbour and gaps fill in.
    lo_x, hi_x = zx0 + pad, zx1 - pad
    lo_y, hi_y = zy0 + pad, zy1 - pad
    for _ in range(160):
        for a in range(n):
            for b in range(a + 1, n):
                pa, pb = placed[a], placed[b]
                dx, dy = pb["x"] - pa["x"], pb["y"] - pa["y"]
                d = _math.hypot(dx, dy) or 0.01
                target = (pa["rad"] + pb["rad"]) * opts.specimen_overlap
                if d < target:
                    push = (target - d) / 2
                    ux, uy = dx / d, dy / d
                    pa["x"] -= ux * push; pa["y"] -= uy * push
                    pb["x"] += ux * push; pb["y"] += uy * push
        for p in placed:                         # stay inside the zone
            p["x"] = min(hi_x, max(lo_x, p["x"]))
            p["y"] = min(hi_y, max(lo_y, p["y"]))

    # paste outermost/oldest first (behind), most-recent on top
    for p in sorted(placed, key=lambda d: d["num"], reverse=True):
        sprite = p["img"].resize((max(1, int(p["w"])), max(1, int(p["h"]))),
                                 Image.LANCZOS)
        sprite = sprite.rotate(_JITTER[(p["num"] - 1) % len(_JITTER)],
                               expand=True, resample=Image.BICUBIC)
        px = int(p["x"] - sprite.width / 2)
        py = int(p["y"] - sprite.height / 2)
        canvas.paste(sprite, (px, py), sprite)
        ob = sprite.getbbox() or (0, 0, sprite.width, sprite.height)
        p["nx"] = px + (ob[0] + ob[2]) / 2
        p["ny"] = py + ob[1] + 0.14 * (ob[3] - ob[1])

    for p in placed:                      # numerals last, on top of everything
        _numeral(draw, p["nx"], p["ny"], p["num"], fonts)


def _draw_key(draw, fonts, opts, items, top):
    """Two-column numbered key: '1. COMMON NAME / latin · 4× · 2h · rare'."""
    m, col_gap = opts.margin, 22
    col_w = (opts.width - 2 * m - col_gap) / 2
    draw.line([(m, top), (opts.width - m, top)], fill=HAIRLINE, width=1)
    y0 = top + 9
    caps = fonts.get(12, weight=600)
    ital = fonts.get(10, italic=True)
    meta = fonts.get(10, weight=500)
    row_h, half = 28, (len(items) + 1) // 2
    for idx, it in enumerate(items):
        col, row = (0, idx) if idx < half else (1, idx - half)
        x = m + col * (col_w + col_gap)
        y = y0 + row * row_h
        num = f"{idx + 1}."
        draw.text((x, y), num, font=caps, fill=INK)
        nx = x + draw.textlength(num + " ", font=caps)
        _draw_tracked(draw, nx, y, it["common"].upper(), caps, INK, 0.6)
        # line 2: latin · count · last · rarity
        lx = x + 11
        draw.text((lx, y + 13), it["latin"], font=ital, fill=INK_SOFT)
        lx += draw.textlength(it["latin"], font=ital)
        tail = f"  \u00b7 {it['count']}\u00d7 \u00b7 {it['last']}"
        draw.text((lx, y + 13), tail, font=meta, fill=INK_SOFT)
        rstyle = RARITY_STYLE.get(it.get("rarity_level", ""), None)
        if rstyle:
            lx += draw.textlength(tail, font=meta)
            draw.text((lx, y + 13), "  \u00b7 " + rstyle[0], font=meta, fill=rstyle[1])


def render_specimen(entries, opts: CollageOpts | None = None,
                    date_str: str = "") -> Image.Image:
    opts = opts or CollageOpts()
    fonts = Fonts(opts.font_dir)
    canvas = Image.new("RGB", (opts.width, opts.height), PAPER)
    draw = ImageDraw.Draw(canvas)

    items = _collect(entries, opts)
    n_calls = sum(int(it["count"]) for it in items)
    top = _draw_header(draw, fonts, opts, len(items), n_calls, date_str)

    key_h = 9 + ((len(items) + 1) // 2) * 28 + 8
    key_top = opts.height - opts.margin - key_h
    _specimen_cluster(canvas, draw, fonts, items, opts,
                      (opts.margin, top, opts.width - opts.margin, key_top - 10))
    _draw_key(draw, fonts, opts, items, key_top)
    return canvas
