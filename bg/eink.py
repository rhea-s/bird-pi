"""E-ink renderer.

Composes the same gallery model into a fixed-resolution image styled like a
sheet of engraved plates, converts it to the panel's palette (1-bit, grayscale,
or 7-colour), and pushes it to the display. The 'save' driver just writes a PNG
so you can see exactly what the panel will show before any hardware is wired.

Display drivers differ by panel, so push() supports the two common ecosystems
(Waveshare's waveshare_epd, Pimoroni's inky) plus 'save'. Tell me your exact
panel and I'll tailor the push; the composition above is panel-agnostic.
"""
from __future__ import annotations

import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from . import config as cfgmod
from .gallery import GalleryEntry

# 7-colour ACeP / Inky Impression palette (black, white, green, blue, red,
# yellow, orange) — the de-facto standard for colour e-ink.
_ACEP = [0,0,0, 255,255,255, 0,255,0, 0,0,255, 255,0,0, 255,255,0, 255,128,0]

_FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/Library/Fonts", "/System/Library/Fonts/Supplemental",
]


def _font(names: list[str], size: int) -> ImageFont.FreeTypeFont:
    for d in _FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except OSError:
                    pass
    for n in names:  # let PIL search its own paths
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _fit(draw, text, font_names, max_w, start, min_size=11):
    """Largest font (down to min_size) at which text fits max_w; truncates if
    even min_size is too wide."""
    size = start
    while size >= min_size:
        f = _font(font_names, size)
        if draw.textlength(text, font=f) <= max_w:
            return f, text
        size -= 1
    f = _font(font_names, min_size)
    t = text
    while t and draw.textlength(t + "…", font=f) > max_w:
        t = t[:-1]
    return f, (t + "…") if t != text else t


def _spaced(s: str, n: int = 1) -> str:
    return (" " * n).join(list(s))


def _center(draw, text, font, cx, y, fill):
    w = draw.textlength(text, font=font)
    draw.text((cx - w / 2, y), text, font=font, fill=fill)


def render(entries: list[GalleryEntry], cfg: cfgmod.Config) -> Image.Image:
    e = cfg.eink
    # Dispatch to the collage/specimen renderer when configured; the grid
    # renderer below is the default and is otherwise left untouched.
    layout = getattr(e, "layout", "grid")
    if layout in ("collage", "specimen"):
        from . import collage as collagemod
        return collagemod.render(entries, cfg)   # RGB; portrait if cfg.eink is
    W, H = e.width, e.height
    INK = (40, 36, 27)
    SOFT = (90, 78, 60)
    MARK = (150, 138, 112)
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(canvas)

    serif = ["DejaVuSerif.ttf", "LiberationSerif-Regular.ttf", "Georgia.ttf"]
    serif_i = ["DejaVuSerif-Italic.ttf", "LiberationSerif-Italic.ttf"]

    # masthead
    pad = max(10, W // 50)
    title_f = _font(serif, max(18, H // 18))
    title = _spaced("RECENTLY HEARD")
    _center(d, title, title_f, W / 2, pad, INK)
    head_h = pad + title_f.size + 8
    d.line([(pad, head_h), (W - pad, head_h)], fill=MARK, width=1)

    grid_top = head_h + max(8, H // 40)
    cols, rows = e.columns, e.rows
    gx, gy = max(8, W // 80), max(8, H // 60)
    cell_w = (W - 2 * pad - (cols - 1) * gx) / cols
    cell_h = (H - grid_top - pad - (rows - 1) * gy) / rows

    for i, entry in enumerate(entries[: cols * rows]):
        r, c = divmod(i, cols)
        x0 = pad + c * (cell_w + gx)
        y0 = grid_top + r * (cell_h + gy)
        x1, y1 = x0 + cell_w, y0 + cell_h

        caption_h = cell_h * 0.30
        frame = [x0, y0, x1, y1 - caption_h]
        d.rectangle(frame, outline=MARK, width=1)

        # illustration, fitted inside the plate-mark
        inner = (frame[0] + 4, frame[1] + 4, frame[2] - 4, frame[3] - 4)
        bw_box = (int(inner[2] - inner[0]), int(inner[3] - inner[1]))
        if entry.plate_url and os.path.exists(entry.plate_url) and bw_box[0] > 0:
            try:
                art = Image.open(entry.plate_url).convert("RGB")
                art.thumbnail(bw_box, Image.LANCZOS)
                ax = int(inner[0] + (bw_box[0] - art.width) / 2)
                ay = int(inner[1] + (bw_box[1] - art.height) / 2)
                canvas.paste(art, (ax, ay))
            except Exception:
                pass
        else:
            uf = _font(serif_i, 13)
            _center(d, "plate not yet collected", uf,
                     (x0 + x1) / 2, (frame[1] + frame[3]) / 2 - 7, SOFT)

        if entry.plate_number:
            pnf = _font(serif, 12)
            t = f"No. {entry.plate_number}"
            d.text((frame[2] - 4 - d.textlength(t, font=pnf), frame[1] + 3),
                   t, font=pnf, fill=SOFT)

        # caption
        cx = (x0 + x1) / 2
        cy = frame[3] + 4
        name_f, name_t = _fit(d, _spaced(entry.common_name.upper()), serif,
                              cell_w - 6, max(13, int(cell_h * 0.12)))
        _center(d, name_t, name_f, cx, cy, INK)
        cy += name_f.size + 2
        lat_f, lat_t = _fit(d, entry.scientific_name, serif_i,
                            cell_w - 6, max(12, int(cell_h * 0.10)))
        _center(d, lat_t, lat_f, cx, cy, SOFT)
        cy += lat_f.size + 3
        heard_f = _font(serif, max(10, int(cell_h * 0.085)))
        _center(d, f"last heard · {entry.last_heard_text}", heard_f, cx, cy, SOFT)

    return _to_palette(canvas, e.palette)


def _to_palette(img: Image.Image, palette: str) -> Image.Image:
    if palette == "bw":
        return img.convert("L").convert("1")  # Floyd–Steinberg by default
    if palette == "gray16":
        return img.convert("L")
    if palette == "acep7":
        pal = Image.new("P", (1, 1))
        pal.putpalette(_ACEP + [0] * (768 - len(_ACEP)))
        return img.quantize(palette=pal, dither=Image.FLOYDSTEINBERG)
    return img


def push(img: Image.Image, cfg: cfgmod.Config) -> None:
    e = cfg.eink
    preview = e.output_png if os.path.isabs(e.output_png) \
        else os.path.join(cfg.root, e.output_png)
    img.save(preview)  # always leave a preview behind

    if e.driver == "save":
        print(f"Wrote preview → {preview}")
        return

    if e.driver == "waveshare":
        try:
            import importlib
            mod = importlib.import_module(e.waveshare_module)
            epd = mod.EPD()
            epd.init()
            epd.display(epd.getbuffer(img))
            epd.sleep()
            print("Pushed to Waveshare panel.")
        except Exception as exc:
            print(f"Waveshare push failed ({exc}). Preview saved at {preview}. "
                  f"Check eink.waveshare_module matches your panel.")
        return

    if e.driver == "inky":
        try:
            from inky.auto import auto
            inky = auto()
            out = img
            # The collage composes portrait (e.g. 480×800) but the panel is
            # landscape (800×480). Rotate only when our image is the panel's
            # dims transposed, so the grid layout (already landscape) is left
            # alone. Flip to 270 if the result hangs upside-down in the frame.
            if (out.width, out.height) == (inky.height, inky.width) \
                    and inky.width != inky.height:
                out = out.rotate(90, expand=True)
            # Hand the Inky library RGB and let it dither to the panel's inks;
            # bump saturation because e-ink reads flatter than a monitor.
            inky.set_image(out.convert("RGB"),
                           saturation=getattr(e, "saturation", 0.6))
            inky.show()
            print("Pushed to Inky panel.")
        except Exception as exc:
            print(f"Inky push failed ({exc}). Preview saved at {preview}.")
        return

    print(f"Unknown driver '{e.driver}'. Preview saved at {preview}.")
