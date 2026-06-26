#!/usr/bin/env python3
"""Populate the illustrations cache with public-domain plates.

    python fetch_plates.py            # fetch plates for currently-seen species
    python fetch_plates.py --dry-run  # show what it WOULD download, fetch nothing
    python fetch_plates.py --all      # also (re)fetch species already cached
    python fetch_plates.py --photos   # allow an Avicommons photo as a last resort
    python fetch_plates.py --species "Cardinalis cardinalis" "Cyanocitta cristata"

Plates are resolved from Wikimedia Commons across the tiered sources in
config.yaml (plates.fetch_sources) — Audubon first, then Gould, etc. — and the
first hit whose title plausibly depicts the species is taken. Species no source
covers stay a "plate not yet collected" leaf unless you enable --photos (or
plates.photo_fallback) or drop your own <genus_species>.jpg into the folder.
"""
import argparse
import time
import os, glob, shutil

from bg import config as cfgmod
from bg import plates as platemod
from bg.birdnet import BirdNetClient
from bg import plates as p

pdir = "illustrations"
junk = os.path.join(pdir, "_rejected")
for f in glob.glob(os.path.join(junk, "*.jpg")):
    if not p._is_blank_scan(open(f, "rb").read()):
        dest = os.path.join(pdir, os.path.basename(f))
        print("restoring:", os.path.basename(f))
        shutil.move(f, dest)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="re-fetch even if a plate is already cached")
    ap.add_argument("--photos", action="store_true",
                    help="allow an Avicommons CC photo when no illustration is found")
    ap.add_argument("--photos-only", action="store_true",
                    help="fetch Avicommons CC photos for everything, skipping the "
                         "illustration search (override, not fallback). Combine "
                         "with --all to replace existing plates.")
    ap.add_argument("--species", nargs="+",
                    help="explicit scientific names instead of querying BirdNET-Go")
    args = ap.parse_args()
    cfg = cfgmod.load(args.config)
    pdir = cfgmod.plates_dir(cfg)
    sources = cfgmod.plate_sources(cfg)
    photo_fallback = args.photos or cfg.plates.photo_fallback
    photo_only = args.photos_only

    if args.species:
        targets = [(s, s) for s in args.species]  # (common, scientific)
    else:
        client = BirdNetClient(cfg.birdnet.base_url, cfg.birdnet.timeout)
        sp = client.recent_species(limit=10_000)
        targets = [(s.common_name, s.scientific_name) for s in sp if s.scientific_name]

    if not targets:
        print("Nothing to fetch. Is BirdNET-Go reachable? Try run_web.py --probe")
        return

    got = skipped = missed = 0
    for common, sci in targets:
        if not args.all and platemod.plate_path(pdir, sci):
            skipped += 1
            continue
        print(f"· {common} ({sci})")
        result = platemod.fetch_plate(
            pdir, common, sci, sources,
            dry_run=args.dry_run,
            photo_fallback=photo_fallback,
            photo_only=photo_only,
        )
        if result:
            got += 1
        else:
            missed += 1
            print("   (no plate found — add one by hand if you have it)")
        time.sleep(0.5)

    print(f"\nDone. fetched={got} already-cached={skipped} unresolved={missed}")
    if args.dry_run:
        print("(dry run — nothing was written)")


if __name__ == "__main__":
    main()
