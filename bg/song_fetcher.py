"""Fetches one good song recording per species from xeno-canto and caches it
for /tree's autoplay, the audio sibling of the plate fetcher.

For each bird we want a single short, clean *song* (not a call, not a
two-minute soundscape) that the tree can play while music notes rise off the
bird. xeno-canto has exactly that — a huge, CC-licensed library searchable by
scientific name — so this module: queries v3 for the species, scores the hits
(song over call, A-quality over D, a tidy 6-25s clip over a rambler, fewer
background species), downloads the winner once, keeps the untouched original in
songs/_raw/, and records who recorded it so the page can credit them (xeno-canto
is Creative Commons; attribution isn't optional).

Mirrors the plate fetcher on purpose:
  songs/_raw/<xcid>.<ext>   the file exactly as xeno-canto served it, never touched
  songs/<sci>.<ext>         the working copy the page actually plays
  songs/index.json          sci -> {song_url, credit, licence, xc_url, ...}
  PlateFetcher -> SongFetcher   same background-worker shape, same backoff manners

Everything here is data you can nudge:
  IDEAL_LEN_S    the clip length the scorer aims for, in seconds
  Q_RANK         how much each quality grade (A-E) is worth
  AUDIO_EXT      content-types we accept, mapped to the extension we save under
  BASE_DELAY     polite gap between xeno-canto requests (it adapts up on a 429)

You need a key. Since 2025-10-10 xeno-canto's API requires one (free, from your
XC account page once your email is verified). Put it in the environment:

    export XENO_CANTO_KEY=xxxxxxxxxxxxxxxx
    python song_fetcher.py "Turdus migratorius" "Cardinalis cardinalis"

or call SongFetcher().fetch_many([...]) from a background thread next to the
plate worker. Nothing here blocks the page: a species with no cached song just
isn't sung, and the tree carries on.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

import requests

# --- where things live -----------------------------------------------------
SONGS_DIR = Path(os.environ.get("BIRDGALLERY_SONGS", "songs"))
RAW_DIR = SONGS_DIR / "_raw"
INDEX_PATH = SONGS_DIR / "index.json"
MISSES_PATH = SONGS_DIR / "misses.json"   # species XC had nothing for, with a timestamp

API_URL = "https://xeno-canto.org/api/3/recordings"
API_KEY = os.environ.get("XENO_CANTO_KEY", "").strip()

# How the page reaches a cached file. The Flask side serves SONGS_DIR at this
# prefix (see the route snippet at the bottom of this file).
SERVE_PREFIX = "/songs"

# --- what a "good" recording is --------------------------------------------
IDEAL_LEN_S = 12.0          # the clip length the scorer leans toward
LEN_OK = (4.0, 30.0)        # outside this, the recording is penalised hard
Q_RANK = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1, "no score": 0, "": 0}

# A recording's free-text `type` contains these for a song we want / a call we
# don't. We prefer song, tolerate call, and avoid alarm/flight notes.
SONG_WORDS = ("song", "dawn song", "subsong")
CALL_WORDS = ("call", "alarm", "flight call", "begging")

# content-type -> extension. xeno-canto is almost all mp3, but it serves the
# occasional wav/flac/ogg, so we whitelist by type the way the plate fetcher
# whitelists raster MIMEs — and refuse anything that isn't audio, so a stray
# HTML error page never lands in the cache wearing an .mp3 hat.
AUDIO_EXT = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/x-mpeg": "mp3",
    "audio/mp4": "m4a", "audio/aac": "aac",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/flac": "flac", "audio/x-flac": "flac",
    "audio/ogg": "ogg", "audio/vorbis": "ogg",
}
MAX_BYTES = 25 * 1024 * 1024   # a single song clip; anything larger is a soundscape
RETRY_MISS_DAYS = 7            # a species XC has nothing for isn't re-queried for this long

# --- talking to xeno-canto politely ----------------------------------------
BASE_DELAY = 1.0            # seconds between requests, floor
_MAX_DELAY = 20.0
MAX_RETRIES = 5
TIMEOUT = 30
USER_AGENT = "birdgallery/1.0 (personal bird dashboard; xeno-canto v3)"


def _parse_len(s: str) -> float:
    """'0:14' or '1:02:33' -> seconds. xeno-canto gives mm:ss (sometimes h:mm:ss)."""
    try:
        parts = [float(p) for p in str(s).split(":")]
    except (ValueError, AttributeError):
        return 0.0
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def _is_song(rec: dict) -> bool:
    t = (rec.get("type") or "").lower()
    return any(w in t for w in SONG_WORDS)


def _is_only_call(rec: dict) -> bool:
    t = (rec.get("type") or "").lower()
    return any(w in t for w in CALL_WORDS) and not _is_song(rec)


def _score(rec: dict) -> float:
    """Bigger is better. Song beats call, A beats D, a tidy clip beats a
    rambler, and a recording with a crowd of background species loses points."""
    s = 0.0
    s += 10.0 if _is_song(rec) else (3.0 if not _is_only_call(rec) else 0.0)
    s += 2.0 * Q_RANK.get((rec.get("q") or "").strip(), 0)

    length = _parse_len(rec.get("length", ""))
    if LEN_OK[0] <= length <= LEN_OK[1]:
        s += 4.0 - abs(length - IDEAL_LEN_S) / 6.0   # peak near IDEAL_LEN_S
    else:
        s -= 6.0                                     # too short or too long

    also = rec.get("also") or []
    also = [a for a in also if a and a.lower() not in ("", "identity unknown")]
    s -= 1.2 * len(also)                             # quieter backgrounds win

    if not (rec.get("file") or "").strip():
        s -= 1000.0                                  # unplayable; bury it
    return s


class SongFetcher:
    """Cache one song per species. Thread-safe enough for a background worker
    sitting beside the plate fetcher; one network thread is the intended use."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = (api_key or API_KEY).strip()
        self._delay = BASE_DELAY
        self._last_req = 0.0
        self._lock = threading.Lock()
        SONGS_DIR.mkdir(parents=True, exist_ok=True)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        self.index = self._load_index()
        self.misses = self._load_misses()

    # -- the cache ----------------------------------------------------------
    def _load_index(self) -> dict:
        try:
            return json.loads(INDEX_PATH.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_index(self) -> None:
        tmp = INDEX_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.index, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(INDEX_PATH)   # atomic-ish: never a half-written index

    def _load_misses(self) -> dict:
        try:
            return json.loads(MISSES_PATH.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_misses(self) -> None:
        tmp = MISSES_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.misses, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(MISSES_PATH)

    def _record_miss(self, key: str) -> None:
        self.misses[key] = int(time.time())
        self._save_misses()

    def has(self, scientific: str) -> bool:
        key = (scientific or "").strip().lower()
        rec = self.index.get(key)
        return bool(rec and (SONGS_DIR / rec["file"]).exists())

    def song_for(self, scientific: str) -> Optional[dict]:
        """What the Flask side reads to attach song_url + credit to an entry."""
        return self.index.get((scientific or "").strip().lower())

    # -- the network --------------------------------------------------------
    def _throttle(self) -> None:
        wait = self._delay - (time.monotonic() - self._last_req)
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.monotonic()

    def _get(self, url: str, *, params: Optional[dict] = None, stream: bool = False):
        """One request with exponential backoff on 429/5xx and an inter-request
        delay that creeps up when xeno-canto pushes back and decays when it
        doesn't — the same manners the Wikimedia plate fetch learned."""
        headers = {"User-Agent": USER_AGENT}
        for attempt in range(MAX_RETRIES):
            self._throttle()
            try:
                r = requests.get(url, params=params, headers=headers,
                                  stream=stream, timeout=TIMEOUT)
            except requests.RequestException:
                time.sleep(min(_MAX_DELAY, self._delay * (2 ** attempt)))
                continue

            if r.status_code == 429 or r.status_code >= 500:
                retry_after = r.headers.get("Retry-After")
                back = float(retry_after) if (retry_after or "").isdigit() \
                    else min(_MAX_DELAY, self._delay * (2 ** attempt))
                self._delay = min(_MAX_DELAY, self._delay * 1.5)   # adapt up
                if stream:
                    r.close()
                time.sleep(back)
                continue

            self._delay = max(BASE_DELAY, self._delay * 0.9)       # adapt down
            return r
        return None

    def _query(self, scientific: str, *, relaxed: bool) -> list[dict]:
        """Ask xeno-canto for this species. First pass is choosy (song, q>=C);
        the relaxed pass drops those filters so a rare bird still gets a voice."""
        gen, _, sp = (scientific or "").strip().partition(" ")
        if not gen or not sp:
            return []
        q = f'gen:{gen} sp:{sp} grp:birds'
        if not relaxed:
            q += ' type:song q:">C"'   # q better than C == A or B
        r = self._get(API_URL, params={"query": q, "key": self.api_key, "per_page": 100})
        if r is None or r.status_code != 200:
            return []
        try:
            return r.json().get("recordings", []) or []
        except ValueError:
            return []

    def _pick(self, scientific: str) -> Optional[dict]:
        recs = self._query(scientific, relaxed=False) or \
               self._query(scientific, relaxed=True)
        if not recs:
            return None
        recs.sort(key=_score, reverse=True)
        best = recs[0]
        return best if _score(best) > -100 else None

    def _download(self, rec: dict, scientific: str) -> Optional[dict]:
        file_url = (rec.get("file") or "").strip()
        if file_url.startswith("//"):
            file_url = "https:" + file_url
        r = self._get(file_url, stream=True)
        if r is None or r.status_code != 200:
            return None

        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = AUDIO_EXT.get(ctype)
        if ext is None:
            r.close()
            return None   # not audio — an error page in disguise; refuse it

        xcid = str(rec.get("id") or "x")
        raw_path = RAW_DIR / f"{xcid}.{ext}"
        size = 0
        try:
            with open(raw_path, "wb") as fh:
                for chunk in r.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("oversized")
                    fh.write(chunk)
        except (OSError, ValueError):
            r.close()
            raw_path.unlink(missing_ok=True)
            return None
        finally:
            r.close()
        if size < 2048:                       # a few KB isn't a real clip
            raw_path.unlink(missing_ok=True)
            return None

        # the working copy the page plays. (A loudness pass would go here, so the
        # tour doesn't lurch in volume bird to bird — left out to stay dependency
        # -light; drop an ffmpeg loudnorm in and point `file` at its output.)
        key = scientific.strip().lower()
        work_name = key.replace(" ", "_") + "." + ext
        work_path = SONGS_DIR / work_name
        work_path.write_bytes(raw_path.read_bytes())

        lic = (rec.get("lic") or "").strip()
        if lic.startswith("//"):
            lic = "https:" + lic
        recordist = (rec.get("rec") or "").strip()
        return {
            "file": work_name,
            "song_url": f"{SERVE_PREFIX}/{work_name}",
            "type": rec.get("type") or "",
            "q": rec.get("q") or "",
            "length": rec.get("length") or "",
            "credit": recordist,                       # who recorded it
            "song_credit": f"{recordist} · XC{xcid}" if recordist else f"XC{xcid}",
            "licence": lic,
            "xc_id": xcid,
            "xc_url": f"https://xeno-canto.org/{xcid}",
            "fetched": int(time.time()),
        }

    # -- the verbs the app calls -------------------------------------------
    def fetch(self, scientific: str, *, force: bool = False) -> Optional[dict]:
        """Ensure one song is cached for this species; return its index entry."""
        key = (scientific or "").strip().lower()
        if not key:
            return None
        if not self.api_key:
            raise RuntimeError("XENO_CANTO_KEY is not set — get a free key from "
                               "your xeno-canto account page (API has required "
                               "one since 2025-10-10).")
        with self._lock:
            if not force and self.has(key):
                return self.index[key]
            best = self._pick(key)
            if best is None:
                self._record_miss(key)
                return None
            entry = self._download(best, key)
            if entry is None:
                self._record_miss(key)
                return None
            self.index[key] = entry
            if self.misses.pop(key, None) is not None:
                self._save_misses()    # it resolved; forget any past miss
            self._save_index()
            return entry

    def fetch_many(self, names: Iterable[str], *, force: bool = False) -> dict:
        """Walk a list of scientific names (e.g. the recently-heard species),
        caching a song for each. Returns sci -> entry for the ones that landed."""
        got = {}
        for name in names:
            try:
                e = self.fetch(name, force=force)
            except RuntimeError:
                raise
            except Exception:
                e = None     # one bad species never stops the worker
            if e:
                got[name.strip().lower()] = e
        return got

    # -- the unresolved set -------------------------------------------------
    def is_unresolved(self, scientific: str, *, retry_miss_days: int = RETRY_MISS_DAYS) -> bool:
        """True if this species has no cached song AND we haven't recently looked
        and come up empty. The miss window keeps us from re-asking xeno-canto
        every run about a bird it simply doesn't have."""
        key = (scientific or "").strip().lower()
        if not key or self.has(key):
            return False
        missed_at = self.misses.get(key)
        if missed_at and (time.time() - missed_at) < retry_miss_days * 86400:
            return False
        return True

    def unresolved(self, names: Iterable[str], *, retry_miss_days: int = RETRY_MISS_DAYS) -> list[str]:
        """The subset of `names` worth fetching right now (uncached, not a recent
        miss), de-duplicated, original spelling preserved."""
        seen, out = set(), []
        for name in names:
            key = (name or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            if self.is_unresolved(key, retry_miss_days=retry_miss_days):
                out.append(name.strip())
        return out

    def fetch_unresolved(self, names: Iterable[str], *,
                         retry_miss_days: int = RETRY_MISS_DAYS,
                         force: bool = False) -> dict:
        """Fetch a song for every species in `names` that doesn't have one yet,
        skipping the cached and the recently-missed. This is the 'top everything
        up' verb — hand it the species on the tree (or the whole gallery) and it
        fills the gaps. Returns sci -> entry for the ones that newly landed."""
        todo = list(names) if force else self.unresolved(names, retry_miss_days=retry_miss_days)
        return self.fetch_many(todo, force=force)

    # -- adopting files you put there by hand -------------------------------
    def ingest_existing(self) -> dict:
        """Register audio files already sitting in SONGS_DIR that the index
        doesn't know about — e.g. recordings you downloaded from xeno-canto by
        hand. Reads the species from the filename (xeno-canto's own
        'XC123 - Common Name - Genus species.mp3', or a plain 'Genus species.mp3'
        / 'genus_species.mp3'), preserves the original in _raw/, writes the
        canonical working copy, and adds it to the index so the tree can sing it.
        Returns sci -> entry for everything newly adopted."""
        known = {v.get("file") for v in self.index.values() if isinstance(v, dict)}
        exts = set(AUDIO_EXT.values())
        added = {}
        for p in sorted(SONGS_DIR.glob("*")):
            if p.is_dir() or p.name in (INDEX_PATH.name, MISSES_PATH.name):
                continue
            ext = p.suffix.lower().lstrip(".")
            if ext not in exts or p.name in known:
                continue
            sci, xcid = self._sci_from_filename(p.name)
            if not sci:
                print(f"  ? {p.name}: no scientific name in the filename; skipped")
                continue
            key = sci.lower()
            canon = key.replace(" ", "_") + "." + ext
            try:
                raw = RAW_DIR / ((f"{xcid}." + ext) if xcid else canon)
                if not raw.exists():
                    raw.write_bytes(p.read_bytes())
                if p.name != canon:
                    (SONGS_DIR / canon).write_bytes(p.read_bytes())
            except OSError:
                continue
            self.index[key] = {
                "file": canon, "song_url": f"{SERVE_PREFIX}/{canon}",
                "type": "", "q": "", "length": "",
                "credit": "", "song_credit": (f"XC{xcid}" if xcid else "manual"),
                "licence": "", "xc_id": xcid or "",
                "xc_url": (f"https://xeno-canto.org/{xcid}" if xcid else ""),
                "fetched": int(time.time()), "source": "manual",
            }
            self.misses.pop(key, None)
            added[key] = self.index[key]
            print(f"  + {sci:32s} <- {p.name}")
        if added:
            self._save_index()
            self._save_misses()
        return added

    @staticmethod
    def _looks_binomial(s: str) -> bool:
        parts = s.split()
        return (len(parts) == 2
                and parts[0][:1].isalpha() and parts[1][:1].isalpha())

    def _sci_from_filename(self, name: str):
        stem = name.rsplit(".", 1)[0]
        m = re.match(r"^XC?(\d+)\b", stem)
        xcid = m.group(1) if m else ""
        if " - " in stem:                       # xeno-canto web download pattern
            tail = stem.split(" - ")[-1].strip()
            if self._looks_binomial(tail):
                return tail, xcid
        cand = stem.replace("_", " ").strip()   # canonical or plain binomial
        if self._looks_binomial(cand):
            return cand, xcid
        return "", xcid


# A background worker, the song twin of the plate worker: hand it a callable
# that returns the species currently worth having, and it tops up the cache on
# an interval without ever touching the request path.
def run_worker(species_source, *, interval_s: int = 1800,
               fetcher: Optional[SongFetcher] = None) -> threading.Thread:
    f = fetcher or SongFetcher()

    def loop():
        while True:
            try:
                f.fetch_unresolved(list(species_source()))
            except RuntimeError as exc:
                print(f"[song_fetcher] {exc}")
                return                       # no key: stop quietly, don't spin
            except Exception as exc:
                print(f"[song_fetcher] worker hiccup: {exc}")
            time.sleep(interval_s)

    t = threading.Thread(target=loop, name="song-fetcher", daemon=True)
    t.start()
    return t


def _read_names_file(path: str) -> list[str]:
    out = []
    for line in Path(path).read_text("utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Fetch and cache xeno-canto songs for the tree.")
    ap.add_argument("names", nargs="*", help='scientific names, e.g. "Turdus migratorius"')
    ap.add_argument("--from-file", metavar="PATH",
                    help="read scientific names one per line (# comments allowed)")
    ap.add_argument("--unresolved", action="store_true",
                    help="only fetch names with no cached song yet (skips recent misses)")
    ap.add_argument("--ingest", action="store_true",
                    help="adopt audio files already in the songs folder (e.g. hand-downloaded)")
    ap.add_argument("--status", action="store_true",
                    help="show what's cached and exit")
    ap.add_argument("--force", action="store_true",
                    help="re-fetch even if cached / recently missed")
    ap.add_argument("--retry-miss-days", type=int, default=RETRY_MISS_DAYS,
                    help=f"re-query a missed species after this many days (default {RETRY_MISS_DAYS})")
    args = ap.parse_args()

    sf = SongFetcher()

    if args.ingest:
        print("Ingesting existing files…")
        got = sf.ingest_existing()
        print(f"  adopted {len(got)} file(s).")

    if args.status:
        print(f"\nsongs cached: {len(sf.index)}   (dir: {SONGS_DIR.resolve()})")
        for k in sorted(sf.index):
            e = sf.index[k]
            tag = "" if e.get("source") != "manual" else "  [manual]"
            print(f"  {k:32s} {e.get('file',''):30s} "
                  f"q{e.get('q','?')} {e.get('length','')}{tag}")
        if sf.misses:
            print(f"\nmisses (no song found): {len(sf.misses)}")
            for k in sorted(sf.misses):
                print(f"  {k}")
        raise SystemExit(0)

    names = list(args.names)
    if args.from_file:
        names += _read_names_file(args.from_file)

    if not names and not args.ingest:
        ap.error('give some names, --from-file PATH, --ingest, or --status')

    if names:
        if args.unresolved:
            pending = sf.unresolved(names, retry_miss_days=args.retry_miss_days)
            print(f"{len(pending)} unresolved of {len(set(n.lower() for n in names))} "
                  f"species; fetching…")
            got = sf.fetch_unresolved(names, retry_miss_days=args.retry_miss_days,
                                      force=args.force)
        else:
            got = sf.fetch_many(names, force=args.force)
        for sci in names:
            e = sf.song_for(sci)
            if e:
                print(f"  ✓ {sci:32s} {e['file']:28s} q{e.get('q','?')} "
                      f"{e.get('length','')}  rec. {e.get('credit','')}".rstrip())
            else:
                print(f"  · {sci:32s} no song cached")
