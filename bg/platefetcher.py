"""Background worker that keeps the plate cache populated.

Polls BirdNET-Go for recently-heard species and fetches a Commons plate for any
that don't have one yet, so a newly-heard bird shows its illustration within a
poll interval — no manual `fetch_plates.py` run required.

All network work (the Commons search, the download with its 429 backoff, and
normalization) happens on this worker thread, never on a Flask request, so the
dashboard never blocks. The web and e-ink renderers are unchanged: they still
just read whatever files are on disk.

Unresolved species (birds Audubon never painted, or taxonomic renames Commons
can't match) are remembered and not retried until `retry_after` elapses, so a
permanently-missing plate doesn't get searched every single poll.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from . import config as cfgmod
from . import plates as platemod
from .birdnet import BirdNetClient


class PlateFetcher:
    def __init__(self, cfg: cfgmod.Config, *,
                 interval: int = 120,
                 retry_after: int = 6 * 3600,
                 startup_delay: int = 2,
                 limit: Optional[int] = None,
                 client: Optional[BirdNetClient] = None):
        self.cfg = cfg
        self.pdir = cfgmod.plates_dir(cfg)
        self.sources = cfgmod.plate_sources(cfg)
        self.photo_fallback = getattr(cfg.plates, "photo_fallback", False)
        self.interval = interval
        self.retry_after = retry_after
        self.startup_delay = startup_delay
        # Fetch plates for at least what the gallery displays.
        self.limit = limit if limit is not None else cfg.gallery.limit
        self.client = client or BirdNetClient(cfg.birdnet.base_url,
                                              cfg.birdnet.timeout)
        self._misses: dict[str, float] = {}   # sci_name -> last unresolved attempt
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="plate-fetcher",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: Optional[float] = None) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    # -- loop --------------------------------------------------------------- #
    def _run(self) -> None:
        # Let app startup / the first request settle before hitting the network.
        if self._stop.wait(self.startup_delay):
            return
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # one bad poll shouldn't kill the worker
                print(f"[plate-fetcher] poll failed: {exc}")
            self._stop.wait(self.interval)

    def _tick(self) -> int:
        """One pass: fetch any missing plate among the recent species. Returns
        the number of plates fetched this pass."""
        species = self.client.recent_species(
            limit=self.limit,
            min_confidence=self.cfg.gallery.min_confidence,
        )
        now = time.time()
        fetched = 0
        for sp in species:
            if self._stop.is_set():
                break
            sci = (sp.scientific_name or "").strip()
            if not sci:
                continue
            if platemod.plate_path(self.pdir, sci):
                continue  # already cached (or hand-dropped) — nothing to do
            last = self._misses.get(sci)
            if last is not None and (now - last) < self.retry_after:
                continue  # recently unresolved — don't hammer Commons
            print(f"[plate-fetcher] fetching plate for {sp.common_name} ({sci})")
            try:
                path = platemod.fetch_plate(self.pdir, sp.common_name, sci,
                                            self.sources,
                                            photo_fallback=self.photo_fallback)
            except Exception as exc:
                print(f"[plate-fetcher] fetch error for {sci}: {exc}")
                path = None
            if path:
                fetched += 1
                self._misses.pop(sci, None)
            else:
                self._misses[sci] = now
        return fetched
