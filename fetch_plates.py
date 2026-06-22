#!/usr/bin/env python3
"""Populate the illustrations cache with Audubon plates from Wikimedia Commons.

    python fetch_plates.py            # fetch plates for currently-seen species
    python fetch_plates.py --dry-run  # show what it WOULD download, fetch nothing
    python fetch_plates.py --all      # also (re)fetch species already cached
    python fetch_plates.py --species "Cardinalis cardinalis" "Cyanocitta cristata"

Plates are saved as <genus_species>.jpg. To override any pick, just drop your
own file with that name into the illustrations folder — the renderers prefer
whatever file is there and never overwrite it unless you pass --all.

Note: Audubon painted ~489 North American species. Birds outside that set (and
the occasional taxonomic rename) won't resolve; those show a "plate not yet
collected" leaf until you add an image by hand.
"""
import argparse
import time

from bg import config as cfgmod
from bg import plates as platemod
from bg.birdnet import BirdNetClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="re-fetch even if a plate is already cached")
    ap.add_argument("--species", nargs="+",
                    help="explicit scientific names instead of querying BirdNET-Go")
    args = ap.parse_args()
    cfg = cfgmod.load(args.config)
    pdir = cfgmod.plates_dir(cfg)

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
            pdir, common, sci,
            cfg.plates.fetch_query_suffix,
            dry_run=args.dry_run
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
