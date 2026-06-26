"""Web dashboard. Run via run_web.py.

Routes:
  /                 the field-guide gallery (the plate-book)
  /collage          the full-colour cutout collage (generated illustrations)
  /api/gallery.json plate-book data as JSON (the page polls this to refresh)
  /api/collage.json collage data as JSON, incl. each bird's cutout_url
  /plate/<name>     serves a cached plate image
  /generated/<name> serves a generated transparent cutout
  /healthz          liveness check

A background PlateFetcher (see platefetcher.py) is started alongside the app so
that plates for newly-heard birds are downloaded automatically. It runs off the
request path, so requests never block on the network. Disable it with
`plates.auto_fetch: false` in config.yaml, or pass start_fetcher=False.
"""
from __future__ import annotations

import os
from dataclasses import asdict

from flask import (Flask, abort, jsonify, render_template,
                   send_from_directory)

from . import config as cfgmod
from . import gallery as gallerymod
from .platefetcher import PlateFetcher


def _plate_route(local_path: str) -> str:
    return "/plate/" + os.path.basename(local_path)


def _cutout_key(scientific: str) -> str:
    """Match the on-disk filename: lowercase scientific name, spaces -> '_'."""
    return scientific.strip().lower().replace(" ", "_") + ".png"


def create_app(cfg: cfgmod.Config | None = None, *,
               start_fetcher: bool | None = None) -> Flask:
    cfg = cfg or cfgmod.load()
    app = Flask(__name__)
    pdir = cfgmod.plates_dir(cfg)
    # generated cutouts live alongside the plates, in illustrations/generated/
    gdir = getattr(getattr(cfg, "plates", None), "generated_dir", None) \
        or os.path.join(pdir, "generated")

    def collect():
        return gallerymod.build_gallery(cfg, plate_url_for=_plate_route)

    def gallery_dicts():
        """All gallery entries as dicts, each tagged with cutout_url (the
        generated illustration) or None if no cutout exists yet."""
        out = []
        for e in collect():
            d = asdict(e)
            key = _cutout_key(e.scientific_name)
            d["cutout_url"] = ("/generated/" + key
                               if os.path.exists(os.path.join(gdir, key))
                               else None)
            out.append(d)
        return out

    def collage_entries():
        """Only entries that actually have a generated cutout — the collage is
        about the illustrations."""
        return [d for d in gallery_dicts() if d["cutout_url"]]

    @app.route("/")
    def index():
        try:
            entries = gallery_dicts()
            error = None
        except Exception as exc:  # surface connection issues in the UI
            entries, error = [], str(exc)
        return render_template("gallery.html", entries=entries,
                               refresh=cfg.web.refresh_seconds, error=error)

    @app.route("/collage")
    def collage_view():
        try:
            entries = collage_entries()
            error = None
        except Exception as exc:
            entries, error = [], str(exc)
        return render_template("collage.html", entries=entries,
                               refresh=cfg.web.refresh_seconds, error=error)

    @app.route("/api/gallery.json")
    def gallery_json():
        try:
            return jsonify({"entries": gallery_dicts(), "error": None})
        except Exception as exc:
            return jsonify({"entries": [], "error": str(exc)}), 200

    @app.route("/api/collage.json")
    def collage_json():
        try:
            return jsonify({"entries": collage_entries(), "error": None})
        except Exception as exc:
            return jsonify({"entries": [], "error": str(exc)}), 200

    @app.route("/plate/<path:name>")
    def plate(name):
        # Only serve from the plates directory; basename guards traversal.
        safe = os.path.basename(name)
        if not os.path.exists(os.path.join(pdir, safe)):
            abort(404)
        return send_from_directory(pdir, safe, max_age=3600)

    @app.route("/generated/<path:name>")
    def generated(name):
        # Only serve from the generated directory; basename guards traversal.
        safe = os.path.basename(name)
        if not os.path.exists(os.path.join(gdir, safe)):
            abort(404)
        return send_from_directory(gdir, safe, max_age=3600)

    @app.route("/healthz")
    def healthz():
        return "ok", 200

    # Auto-fetch plates for newly-heard birds in the background.
    # getattr fallbacks keep this working even with an older config.py.
    enable = (getattr(cfg.plates, "auto_fetch", True)
              if start_fetcher is None else start_fetcher)
    if enable:
        fetcher = PlateFetcher(
            cfg, interval=getattr(cfg.plates, "auto_fetch_interval", 120))
        fetcher.start()
        app.extensions["plate_fetcher"] = fetcher  # so it can be stopped/inspected

    return app
