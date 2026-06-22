# birdgallery

A live plate-book for a [BirdNET-Go](https://github.com/tphakala/birdnet-go)
feeder. It shows the species heard most recently, each beside an illustration in
the manner of Audubon's *Birds of America*, with the time it was last heard.

One data/illustration core feeds two renderers off a single gallery model:

- **Web** — a field-guide dashboard for your phone or laptop.
- **E-ink** — the same layout composed to an image and pushed to a panel on the Pi.

## How it gets its data

It reads BirdNET-Go's local JSON API (`/api/v2/analytics/species/summary`) rather
than the SQLite file, so there's no lock contention while BirdNET-Go is writing.
On the local subnet that endpoint needs no authentication. The client parses
defensively across key-name variants, because BirdNET-Go's JSON shape shifts
between releases. If your fields look different, see **Troubleshooting**.

## Illustrations

Audubon's plates are public domain. Renderers only read local files named
`illustrations/<genus_species>.jpg`; nothing fetches images at render time.
Populate the cache once with:

```bash
python fetch_plates.py            # for every species heard so far
python fetch_plates.py --dry-run  # preview matches without downloading
```

This queries Wikimedia Commons live and downloads the top match — it never
fabricates URLs. To override a pick, drop your own image into `illustrations/`
with the matching name. Audubon painted ~489 North American species, so most
Michigan feeder birds resolve; anything outside that set shows a "plate not yet
collected" leaf until you add one by hand.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.yaml config.yaml.bak    # optional
```

Edit `config.yaml` — at minimum, point `birdnet.base_url` at your instance
(e.g. `http://localhost:8080` on the Pi, or `http://birdpi.local:8080` from a
laptop). Then:

```bash
python run_web.py --probe   # sanity-check the connection; prints raw JSON
```

## Run the web dashboard

```bash
python run_web.py           # http://<host>:8000
```

The page re-pulls detections every `web.refresh_seconds`.

## Run the e-ink display

Start in preview mode (no hardware needed): with `eink.driver: save`,

```bash
python run_eink.py          # writes eink_out.png — open it to check the layout
```

Set `eink.width/height/palette` to your panel (`bw`, `gray16`, or `acep7` for
7-colour panels). When the layout looks right, switch `eink.driver`:

- **Waveshare** — `driver: waveshare`, and set `waveshare_module` to your panel's
  driver (e.g. `waveshare_epd.epd7in5_V2`).
- **Inky (Pimoroni)** — `driver: inky` (auto-detects the panel).

Then run on a loop, e.g. every 5 minutes:

```bash
python run_eink.py --loop 300
```

A preview PNG is always written alongside, even when pushing to hardware.

### Run it as a service

`systemd` unit (web):

```ini
[Unit]
Description=birdgallery web
After=network-online.target
[Service]
WorkingDirectory=/home/pi/birdgallery
ExecStart=/home/pi/birdgallery/.venv/bin/python run_web.py
Restart=on-failure
[Install]
WantedBy=multi-user.target
```

Swap `run_web.py` for `run_eink.py --loop 300` for the display.

## Plate numbers (optional)

To show real Audubon plate numbers as Roman numerals, add verified entries to
`plate_meta.yaml`. Unlisted species simply omit the number — nothing is guessed.

## Troubleshooting

- **Can't reach BirdNET-Go** — confirm the port (default 8080) and that you're on
  the same subnet; `python run_web.py --probe`.
- **Names show but times say "no record"** — your build names the timestamp
  differently. Run `--probe`, find the field, and add it to the `_LAST` tuple in
  `bg/birdnet.py`.
- **Fonts look plain on the e-ink** — install DejaVu serif
  (`sudo apt install fonts-dejavu`), or point `_FONT_DIRS` in `bg/eink.py` at a
  serif you prefer.

## Layout

```
config.yaml          your settings
plate_meta.yaml      optional verified plate numbers
run_web.py           web entrypoint
run_eink.py          e-ink entrypoint
fetch_plates.py      populate the illustration cache
bg/
  birdnet.py         API client + defensive normalisation + --probe
  plates.py          cache lookup + Wikimedia Commons fetcher
  gallery.py         the shared gallery model both renderers use
  reltime.py         "12 min ago"
  web.py             Flask app
  eink.py            PIL composition + palette + display push
  templates/ static/ the web look
illustrations/       cached plates (you populate this)
```
