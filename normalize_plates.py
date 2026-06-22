#!/usr/bin/env python3
"""
normalize_plates.py — Normalize bird-plate / illustration scans for UI display.

For each image it:
  1. Detects the artwork's content region (robust to cream paper, foxing,
     scanner shadows, and stray margin specks).
  2. Auto-corrects rotated scans (landscape scans of portrait plates).
  3. Crops to that region with a little breathing room.
  4. Centers it on a canvas sized to match the content's natural aspect ratio,
     scaled to occupy most of the display area.

Designed for Audubon-style plates where a small illustration floats in a large
sea of aged paper, but it degrades gracefully on plain-white scans too.

Usage:
  python normalize_plates.py INPUT [INPUT ...] -o OUTDIR [options]

INPUT may be image files or directories (directories are scanned for images).

Key options:
  -o, --out DIR        Output directory (required).
  --bg MODE            Canvas fill: 'paper' (sampled, default), 'white',
                       'transparent', or a hex color like '#0e0e10'.
  --occupy F           Fraction of the canvas the content fills (default 0.92).
  --canvas WxH         Fixed output canvas, e.g. 1024x1024. Omit to keep the
                       content's own aspect ratio (uniform margin only).
  --pad F              Extra breathing room around detected content, as a
                       fraction of content size (default 0.04).
  --max-dim N          Cap the longest output edge in px (default 2400).
  --dark T             Luminance-drop threshold vs paper (default 25).
  --sat T              Saturation threshold vs paper (default 12).
  --min-blob F         Ignore content blobs smaller than this fraction of the
                       image area — kills foxing/dust (default 0.0005).
  --trim P             Discard the sparsest P% of content pixels per edge when
                       computing the box, in [0,5] (default 0.2).
  --no-rotate          Disable auto-rotation of landscape scans.
  --rotate-threshold F Minimum content width/height ratio to trigger rotation
                       (default 1.15 — only rotate clearly-landscape content).
  --format FMT         Output format: 'png' (default) or 'jpg'.
  --debug              Also write *_debug.jpg overlays showing the detected box.
"""
import argparse, os, sys
from PIL import Image, ImageDraw
import numpy as np
from scipy import ndimage

Image.MAX_IMAGE_PIXELS = None
EXTS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp', '.bmp'}
LUM = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def gather(inputs):
    files = []
    for p in inputs:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                for n in sorted(names):
                    if os.path.splitext(n)[1].lower() in EXTS:
                        files.append(os.path.join(root, n))
        elif os.path.isfile(p):
            files.append(p)
        else:
            print(f"  ! skipping (not found): {p}", file=sys.stderr)
    return files


def build_mask(a, args):
    """Return boolean content mask for an HxWx3 float32 array."""
    paper = np.median(a.reshape(-1, 3), axis=0)
    darker   = float(paper @ LUM) - (a @ LUM)
    colorful = (a.max(2) - a.min(2)) - float(paper.max() - paper.min())
    mask = (darker > args.dark) | (colorful > args.sat)

    mask = ndimage.binary_opening(mask, structure=np.ones((3, 3)), iterations=2)
    lbl, n = ndimage.label(mask)
    if n:
        sh, sw = mask.shape
        sizes = ndimage.sum(np.ones_like(lbl), lbl, range(1, n + 1))
        floor = args.min_blob * sh * sw
        keep  = np.where(sizes >= floor)[0] + 1
        mask  = np.isin(lbl, keep)
    return mask, paper


def mask_box(mask, trim_p):
    """Return (x0, y0, x1, y1) bounding box on a boolean mask."""
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    p = max(0.0, min(5.0, trim_p))
    x0 = int(np.percentile(xs, p));         x1 = int(np.ceil(np.percentile(xs, 100 - p)))
    y0 = int(np.percentile(ys, p));         y1 = int(np.ceil(np.percentile(ys, 100 - p)))
    return x0, y0, x1, y1


def detect_box(im, args):
    """
    Return (x0, y0, x1, y1) content box in full-res coords, paper RGB,
    and rotation angle applied to `im` (0 or 90 or 270).
    """
    W, H = im.size
    scale = max(1, int(round(max(W, H) / 1600)))
    sw, sh = max(1, W // scale), max(1, H // scale)
    small = im.resize((sw, sh), Image.BILINEAR)
    a = np.asarray(small, dtype=np.float32)

    mask, paper = build_mask(a, args)
    box = mask_box(mask, args.trim)

    rotation = 0

    if box is not None and not args.no_rotate:
        bx0, by0, bx1, by1 = box
        cw, ch = bx1 - bx0, by1 - by0
        # Only attempt rotation when the scan file itself is landscape (W > H).
        # A square or portrait scan never needs rotation, even if the content
        # inside happens to be wide. The content-ratio check is a secondary
        # confirmation that the content really is landscape-oriented.
        scan_is_landscape = (W > H)
        if scan_is_landscape and cw > 0 and ch > 0 and cw / ch >= args.rotate_threshold:
            # Try both 90° rotations; pick the portrait one that is right-side-up.
            # PIL rotate(90) is CCW; PIL rotate(270) is CW.
            best_angle = None
            best_score = -1e9

            for angle in (90, 270):
                candidate = im.rotate(angle, expand=True)
                cW, cH = candidate.size
                cscale = max(1, int(round(max(cW, cH) / 1600)))
                csw, csh = max(1, cW // cscale), max(1, cH // cscale)
                ca = np.asarray(candidate.resize((csw, csh), Image.BILINEAR),
                                dtype=np.float32)
                cmask, cpaper = build_mask(ca, args)
                cbox = mask_box(cmask, args.trim)
                if cbox is None:
                    continue
                cbx0, cby0, cbx1, cby1 = cbox
                ccw, cch = cbx1 - cbx0, cby1 - cby0
                if cch <= ccw:          # still landscape → skip
                    continue

                # Right-side-up score: Audubon plates have the main subject
                # (birds, branches, densest ink) in the upper 2/3 and sparse
                # content (ground, tiny caption text) near the bottom.
                # → Correct orientation has more content *mass* (content pixels)
                # in the top half of the content box than the bottom half.
                # We use the content mask for this, not luminance, because
                # plate backgrounds vary and luminance direction is unreliable.
                content_mask_slice = cmask[cby0:cby1, cbx0:cbx1]
                half = cch // 2
                top_mass    = content_mask_slice[:half, :].sum()
                bottom_mass = content_mask_slice[half:, :].sum()
                # Positive score → more ink in top half → right-side-up.
                upright_score = float(top_mass - bottom_mass)

                if upright_score > best_score:
                    best_score = upright_score
                    best_angle = angle
                    _best = (candidate, cW, cH, cscale, csw, csh, cmask, cpaper, cbox)

            if best_angle is not None:
                candidate, cW, cH, cscale, csw, csh, cmask, cpaper, cbox = _best
                im = candidate
                W, H = cW, cH
                scale = cscale
                sw, sh = csw, csh
                mask, paper = cmask, cpaper
                box = cbox
                rotation = best_angle

    if box is None:
        return (0, 0, W, H), paper, rotation, im

    x0, y0, x1, y1 = box
    x0 *= scale; y0 *= scale; x1 = int(np.ceil(x1 * scale)); y1 = int(np.ceil(y1 * scale))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    return (x0, y0, x1, y1), paper, rotation, im


def parse_bg(mode, paper):
    if mode == 'paper':
        return tuple(int(v) for v in paper) + (255,)
    if mode == 'white':
        return (255, 255, 255, 255)
    if mode == 'transparent':
        return (0, 0, 0, 0)
    h = mode.lstrip('#')
    if len(h) == 6:
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4)) + (255,)
    raise ValueError(f"bad --bg value: {mode}")


def process(path, args):
    im = Image.open(path).convert('RGB')
    orig_size = im.size
    (x0, y0, x1, y1), paper, rotation, im = detect_box(im, args)
    W, H = im.size

    # Breathing room.
    bw, bh = x1 - x0, y1 - y0
    px, py = int(bw * args.pad), int(bh * args.pad)
    cx0, cy0 = max(0, x0 - px), max(0, y0 - py)
    cx1, cy1 = min(W, x1 + px), min(H, y1 + py)
    crop = im.crop((cx0, cy0, cx1, cy1))
    cw, ch = crop.size

    bg = parse_bg(args.bg, paper)

    # Target canvas.
    if args.canvas:
        tw, th = args.canvas
    else:
        # Use the content's own aspect ratio so portrait plates stay portrait,
        # square plates get a square canvas (no dead-zone bars), and landscape
        # plates (when genuinely horizontal) stay landscape.
        tw = int(round(cw / args.occupy))
        th = int(round(ch / args.occupy))

    # Scale content to occupy the requested fraction of the canvas.
    avail_w, avail_h = tw * args.occupy, th * args.occupy
    s = min(avail_w / cw, avail_h / ch)
    nw, nh = max(1, int(round(cw * s))), max(1, int(round(ch * s)))

    # Cap output size.
    longest = max(tw, th)
    if longest > args.max_dim:
        r = args.max_dim / longest
        tw, th = int(round(tw * r)), int(round(th * r))
        nw, nh = max(1, int(round(nw * r))), max(1, int(round(nh * r)))

    crop = crop.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new('RGBA', (tw, th), bg)
    canvas.paste(crop, ((tw - nw) // 2, (th - nh) // 2))

    base = os.path.splitext(os.path.basename(path))[0]
    if args.format == 'jpg':
        out = os.path.join(args.out, base + '.jpg')
        canvas.convert('RGB').save(out, quality=92)
    else:
        out = os.path.join(args.out, base + '.png')
        canvas.save(out)

    if args.debug:
        dbg = im.copy()
        d = ImageDraw.Draw(dbg)
        d.rectangle([x0, y0, x1, y1], outline=(255, 0, 0),
                    width=max(2, max(W, H) // 400))
        d.rectangle([cx0, cy0, cx1, cy1], outline=(0, 160, 255),
                    width=max(2, max(W, H) // 500))
        if rotation:
            # Label the rotation so it's visible in debug output
            d.text((20, 20), f"rotated {rotation}°", fill=(255, 80, 0))
        dbg.thumbnail((1400, 1400))
        dbg.convert('RGB').save(
            os.path.join(args.out, base + '_debug.jpg'), quality=85)

    rot_note = f" [rotated {rotation}°]" if rotation else ""
    return out, orig_size, (tw, th), rot_note


def main():
    ap = argparse.ArgumentParser(
        description="Normalize illustration scans for UI display.")
    ap.add_argument('inputs', nargs='+')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--bg', default='paper')
    ap.add_argument('--occupy', type=float, default=0.92)
    ap.add_argument('--canvas', default=None)
    ap.add_argument('--pad', type=float, default=0.04)
    ap.add_argument('--max-dim', type=int, default=2400)
    ap.add_argument('--dark', type=float, default=25)
    ap.add_argument('--sat', type=float, default=12)
    ap.add_argument('--min-blob', type=float, default=0.0005)
    ap.add_argument('--trim', type=float, default=0.2)
    ap.add_argument('--no-rotate', action='store_true',
                    help="Disable auto-rotation of landscape scans.")
    ap.add_argument('--rotate-threshold', type=float, default=1.15,
                    help="content w/h ratio above which rotation is attempted (default 1.15).")
    ap.add_argument('--format', choices=['png', 'jpg'], default='png')
    ap.add_argument('--debug', action='store_true')
    args = ap.parse_args()

    if args.canvas:
        w, h = args.canvas.lower().split('x')
        args.canvas = (int(w), int(h))

    os.makedirs(args.out, exist_ok=True)
    files = gather(args.inputs)
    if not files:
        print("No images found.", file=sys.stderr); sys.exit(1)

    print(f"Normalizing {len(files)} image(s) -> {args.out}")
    for f in files:
        try:
            out, src, dst, note = process(f, args)
            print(f"  ok  {os.path.basename(f)}  "
                  f"{src[0]}x{src[1]} -> {dst[0]}x{dst[1]}  "
                  f"{os.path.basename(out)}{note}")
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  ERR {os.path.basename(f)}: {e}", file=sys.stderr)


if __name__ == '__main__':
    main()
