#!/usr/bin/env python3
"""Render an e-ink layout to PNGs without a panel — for iterating on the Mac.

    python preview_collage.py                  # collage (justified, non-overlap)
    python preview_collage.py --layout specimen # overlapping naturalist plate
    open collage_out.png      # full-colour design intent
    open collage_eink.png     # Spectra-6 panel preview (what the Inky shows)

Only needs bg/collage.py present — does NOT require the eink.py/config wiring.
"""
import argparse
import datetime as dt

from bg import config as cfgmod
from bg.gallery import build_gallery
from bg.collage import (render_collage, render_specimen, to_spectra6,
                        CollageOpts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layout", choices=["collage", "specimen"],
                    default="collage")
    args = ap.parse_args()

    cfg = cfgmod.load()
    try:
        entries = build_gallery(cfg)          # e-ink path: reads files directly
    except Exception as exc:
        raise SystemExit(f"Couldn't reach BirdNET-Go ({exc}).\n"
                         "Check birdnet.base_url in config.yaml, then retry.")
    if not entries:
        print("No species returned yet — is anything being heard?")

    date_str = dt.datetime.now().strftime("%A, %-d %B")
    render = render_specimen if args.layout == "specimen" else render_collage
    img = render(entries, CollageOpts(), date_str=date_str)
    img.save("collage_out.png")
    to_spectra6(img).save("collage_eink.png")
    print(f"Wrote collage_out.png and collage_eink.png "
          f"({args.layout}, {len(entries)} birds).")
    print("Any species missing a cutout in illustrations/generated/ is skipped.")


if __name__ == "__main__":
    main()
