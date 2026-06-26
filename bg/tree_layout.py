"""Perches the most-recent cutouts onto the oak (templates/tree.html, /tree).

Two things vary per bird: how big it is, and where it sits. Size comes from a
species table (with a sensible default); position comes from a fixed list of
perch SLOTS, ordered sturdiest/lowest-first. We sort the birds big -> small and
drop them into slots low -> high, so the giants land at the base and the canopy
tips get the smallest birds — which is also exactly the placement the eye
expects. A couple of species (nuthatches) used to get a head-down trunk pose,
but that needs a cutout actually drawn head-down; for now every bird is treated
as a normal upright perched (or ground-standing) bird.

Everything here is data you can nudge:
  SLOTS        feet-anchor coordinates on the tree, in (x%, y%), rank order
  HEIGHT_PCT   display height per size tier, as a % of the stage height
  TIER_BY_SCI  which species belongs to which tier (scientific name, lowercased)

The /tree page in ?debug=1 lets you drag birds around and copy a fresh SLOTS
list straight back into this file — so you never have to guess coordinates.
"""
from __future__ import annotations

from typing import Optional

# --- where birds sit -------------------------------------------------------
# (x%, y%) of the bird's *feet* (bottom-centre), measured on the tree image.
# Rank order matters: index 0 is the lowest / sturdiest spot, the last index is
# the highest / flimsiest. Tuned by eye to the example oak — drag in ?debug=1.
SLOTS: list[tuple[float, float]] = [
    (50.0, 95.0),   # 0  base of trunk, dead centre — the giant, feet in grass
    (33.0, 96.0),   # 1  ground, left
    (65.0, 93.0),   # 2  ground / lowest limb, right
    (40.0, 65.0),   # 3  thick lower-left limb
    (61.0, 61.0),   # 4  thick lower-right limb
    (50.0, 53.0),   # 5  central crook
    (25.0, 57.0),   # 6  mid-left limb
    (76.0, 59.0),   # 7  mid-right limb
    (35.0, 39.0),   # 8  upper-left foliage
    (64.0, 35.0),   # 9  upper-right foliage
    (49.0, 25.0),   # 10 near the crown
    (17.0, 49.0),   # 11 outer-left twig
]

# Birds whose feet land below this y get a heavier cast shadow (they're standing
# on grass, not gripping a branch).
GROUND_Y = 84.0

# --- how big birds are -----------------------------------------------------
# Display height as a % of the stage (tree) height. Ratios roughly follow the
# real-size guide, compressed a little so chickadees stay visible and swans
# don't eat the whole tree. Bump any value to taste.
HEIGHT_PCT: dict = {
    1: 6.0, 2: 7.5, 3: 10.0, 4: 13.0, 5: 16.5,
    "mallard": 21.0, "heron": 25.0, "swan": 30.0,
}
DEFAULT_TIER = 2  # unknown species -> a small songbird

# Birds wider than this (visible width / height) get their height trimmed so
# elongated silhouettes don't read oversized. ~1.5 is a typical perched
# songbird (the tail makes them a touch wider than tall), so most birds are
# untouched; a flat, horizontal nighthawk sits well above it and shrinks.
REF_ASPECT = 1.5

# scientific name (lowercased) -> tier key above. Extend freely; anything not
# listed falls back to DEFAULT_TIER.
TIER_BY_SCI: dict[str, object] = {
    # tier 1 — tiny (chickadee, gnatcatcher, kinglet, goldfinch, warblers)
    "poecile atricapillus": 1, "poecile carolinensis": 1,
    "polioptila caerulea": 1, "setophaga ruticilla": 1,
    "regulus calendula": 1, "regulus satrapa": 1,
    "spinus tristis": 1, "spinus pinus": 1, "corthylio calendula": 1,
    "setophaga petechia": 1, "setophaga coronata": 1, "mniotilta varia": 1,
    # tier 2 — small (sparrow, ovenbird, swallow, nuthatch, titmouse, junco)
    "passer domesticus": 2, "seiurus aurocapilla": 2,
    "hirundo rustica": 2, "tachycineta bicolor": 2, "petrochelidon pyrrhonota": 2,
    "spizella passerina": 2, "melospiza melodia": 2, "zonotrichia albicollis": 2,
    "junco hyemalis": 2, "sitta carolinensis": 2, "sitta canadensis": 2,
    "baeolophus bicolor": 2, "haemorhous mexicanus": 2, "thryothorus ludovicianus": 2,
    # tier 3 — medium (waxwing, cowbird, grosbeak, bluebird)
    "bombycilla cedrorum": 3, "molothrus ater": 3,
    "pheucticus ludovicianus": 3, "sialia sialis": 3, "agelaius phoeniceus": 3,
    # tier 4 — core (robin, cardinal, kestrel, nighthawk, jay, starling)
    "turdus migratorius": 4, "cardinalis cardinalis": 4,
    "falco sparverius": 4, "chordeiles minor": 4, "cyanocitta cristata": 4,
    "sturnus vulgaris": 4, "dryobates pubescens": 3, "melanerpes carolinus": 4,
    # tier 5 — large (mourning dove, grackle)
    "zenaida macroura": 5, "quiscalus quiscula": 5, "colaptes auratus": 5,
    # tier 6 — the giants
    "anas platyrhynchos": "mallard",
    "ardea herodias": "heron", "nycticorax nycticorax": "heron",
    "branta canadensis": "heron", "buteo jamaicensis": "heron",
    "cygnus buccinator": "swan", "cygnus olor": "swan", "cygnus columbianus": "swan",
}


def tier_for(scientific: str) -> object:
    return TIER_BY_SCI.get((scientific or "").strip().lower(), DEFAULT_TIER)


def height_for(tier: object) -> float:
    return HEIGHT_PCT.get(tier, HEIGHT_PCT[DEFAULT_TIER])


def _apply_bbox(e: dict, target_h: float, *, center: bool, damp: bool = True) -> None:
    """Translate a target *visible* height into the canvas height to actually
    render, plus the anchor point, using the cutout's alpha bounding box if the
    caller measured one (e['bbox'] = (left, top, right, bottom) as fractions of
    the canvas). Falls back to 'the bird fills the canvas' when no bbox is given.

    Shape correction (when damp): a bird sized purely by height balloons in width
    if it's an elongated, horizontal silhouette (a perched nighthawk, a swallow),
    so it reads far larger than a compact bird of the same real size. Past a
    reference aspect we trim the height ~1/sqrt(aspect), keeping the visible
    *area* — what the eye reads as 'size' — roughly tier-proportional instead of
    the height. The reference is set to a typical perched songbird (tail makes
    them ~1.5 wide), so ordinary birds are untouched and only the genuinely
    elongated ones shrink. Ground birds pass damp=False: they're the aesthetic
    giants, sized by eye, not compared against the perched flock.

    vis_h  the height the bird itself occupies, % of the stage (post-correction)
    bird_h the canvas height to set on the <img> so the bird hits vis_h
    anchor_x / anchor_y  the canvas point (0..1) pinned to (x, y)
    """
    l, t, r, b = e.get("bbox") or (0.0, 0.0, 1.0, 1.0)
    vh = max(0.05, b - t)
    cx = (l + r) / 2.0

    if damp:
        aspect = e.get("aspect") or REF_ASPECT
        if aspect > REF_ASPECT:
            target_h *= (REF_ASPECT / aspect) ** 0.5

    e["vis_h"] = round(target_h, 2)
    e["bird_h"] = round(target_h / vh, 2)
    e["anchor_x"] = round(cx, 4)
    e["anchor_y"] = round((t + b) / 2.0 if center else b, 4)


def build_layout(entries: list[dict]) -> list[dict]:
    """Annotate each entry (a gallery dict that already has a cutout) with the
    fields the template needs: x, y, bird_h, vis_h, anchor, anchor_x/anchor_y,
    flip, rotate, ground, rank, tier, z. Mutates and returns the same dicts.
    Only the first len(SLOTS) most recent entries are placed; extras are dropped
    (the tree only has so many branches).

    If the caller has tagged entries with e['bbox'] (the cutout's alpha bounding
    box), sizing and anchoring track the visible bird; otherwise they track the
    canvas, which floats padded birds a little but still renders."""
    items = list(entries)[: len(SLOTS)]
    for e in items:
        t = tier_for(e.get("scientific_name", ""))
        e["tier"] = t
        e["_target_h"] = height_for(t)

    # biggest bird -> lowest/sturdiest slot
    order = sorted(range(len(items)),
                   key=lambda i: items[i]["_target_h"], reverse=True)
    for slot_rank, idx in enumerate(order):
        e = items[idx]
        x, y = SLOTS[slot_rank]
        e.update(x=x, y=y, rank=slot_rank, anchor="feet",
                 flip=False, rotate=0, ground=(y >= GROUND_Y),
                 z=int(round(y)))
        _apply_bbox(e, e["_target_h"], center=False, damp=not e["ground"])

    for e in items:
        e.pop("_target_h", None)
    # render order: lowest (largest, nearest) last so it paints in front
    items.sort(key=lambda e: e.get("z", 0))
    return items
