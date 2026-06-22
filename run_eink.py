#!/usr/bin/env python3
"""Render the gallery to the e-ink panel.

    python run_eink.py            # render once and push
    python run_eink.py --loop 300 # re-render every 300s (good for a service)

Start with driver: save in config.yaml and open eink_out.png to preview the
layout before wiring the panel.
"""
import argparse
import time

from bg import config as cfgmod
from bg import eink as einkmod
from bg.gallery import build_gallery


def render_once(cfg):
    entries = build_gallery(cfg)              # e-ink reads plate files directly
    img = einkmod.render(entries, cfg)
    einkmod.push(img, cfg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--loop", type=int, default=0,
                    help="seconds between refreshes; 0 = render once")
    args = ap.parse_args()
    cfg = cfgmod.load(args.config)

    if args.loop <= 0:
        render_once(cfg)
        return
    print(f"Refreshing every {args.loop}s. Ctrl-C to stop.")
    while True:
        try:
            render_once(cfg)
        except Exception as exc:
            print(f"render failed: {exc}")
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
