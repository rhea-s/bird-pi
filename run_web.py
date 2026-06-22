#!/usr/bin/env python3
"""Start the web dashboard.

    python run_web.py            # uses config.yaml
    python run_web.py --probe    # print BirdNET-Go's raw JSON and exit
"""
import argparse

from bg import config as cfgmod
from bg.birdnet import BirdNetClient
from bg.web import create_app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--probe", action="store_true",
                    help="dump the species/summary endpoint and exit")
    args = ap.parse_args()

    cfg = cfgmod.load(args.config)
    if args.probe:
        print(BirdNetClient(cfg.birdnet.base_url, cfg.birdnet.timeout).probe())
        return

    app = create_app(cfg)
    print(f"Serving on http://{cfg.web.host}:{cfg.web.port}  "
          f"(BirdNET-Go at {cfg.birdnet.base_url})")
    app.run(host=cfg.web.host, port=cfg.web.port, debug=False)


if __name__ == "__main__":
    main()
