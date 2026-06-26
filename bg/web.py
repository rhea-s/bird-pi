"""Web dashboard. Run via run_web.py.

Routes:
  /                 the field-guide gallery (the plate-book)
  /collage          the full-colour cutout collage (generated illustrations)
  /tree             the oak — recent birds perched on a generated tree
  /api/gallery.json plate-book data as JSON (the page polls this to refresh)
  /api/collage.json collage data as JSON, incl. each bird's cutout_url
  /api/tree.json    tree data as JSON, each bird tagged with size + position
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

from flask import (Flask, abort, jsonify, render_template, request,
                   send_from_directory)

from . import config as cfgmod
from . import gallery as gallerymod
from . import tree_layout
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
    # Re-read templates from disk on each request. Without this, running with
    # debug=False (as run_web.py does) caches the compiled template in memory,
    # so edits to tree.html/gallery.html won't show until the process restarts.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
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

    _bbox_cache: dict[str, tuple] = {}

    def _cutout_bbox(key: str):
        """Alpha bounding box of a generated cutout as (l, t, r, b) fractions of
        its canvas, plus the visible bird's aspect ratio (width/height in real
        pixels). The fractions let the tree size/anchor the visible bird rather
        than the padded canvas; the aspect lets it trim height for elongated
        birds so visual mass stays consistent. Cached by mtime; returns
        (None, None) if it can't be read."""
        path = os.path.join(gdir, key)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return (None, None)
        hit = _bbox_cache.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
        frac, aspect = None, None
        try:
            from PIL import Image
            with Image.open(path) as im:
                im = im.convert("RGBA")
                W, H = im.size
                bbox = im.getchannel("A").point(lambda a: 255 if a > 10 else 0).getbbox()
            if bbox:
                frac = (bbox[0] / W, bbox[1] / H, bbox[2] / W, bbox[3] / H)
                vw, vh = (bbox[2] - bbox[0]), (bbox[3] - bbox[1])
                aspect = (vw / vh) if vh else None
        except Exception:
            frac, aspect = None, None
        _bbox_cache[key] = (mtime, (frac, aspect))
        return (frac, aspect)

    def tree_entries():
        """The most-recent cutouts, each annotated with its alpha bbox + aspect,
        then sized + positioned on the oak by tree_layout."""
        ents = collage_entries()
        for e in ents:
            url = e.get("cutout_url") or ""
            frac, aspect = _cutout_bbox(os.path.basename(url)) if url else (None, None)
            e["bbox"] = frac
            e["aspect"] = aspect
        return tree_layout.build_layout(ents)

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

    @app.route("/tree")
    def tree_view():
        try:
            entries = tree_entries()
            error = None
        except Exception as exc:
            entries, error = [], str(exc)
        debug = request.args.get("debug") in ("1", "true", "yes")
        return render_template("tree.html", entries=entries,
                               refresh=cfg.web.refresh_seconds, error=error,
                               tree_url="/generated/tree.png", debug=debug)

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

    @app.route("/api/tree.json")
    def tree_json():
        try:
            return jsonify({"entries": tree_entries(), "error": None})
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
