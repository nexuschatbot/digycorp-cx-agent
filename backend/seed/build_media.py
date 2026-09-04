"""Re-encode Elite's supplied vehicle photography into the two renditions the UI
actually paints, and write them into seed/media/vehicles.

A ONE-SHOT FIXTURE TOOL, like convert_workbook.py beside it — not something the
app imports and not something CI runs. Elite sends a folder of mixed PNG/WebP/
AVIF at whatever size their design team exported; this turns that into files
sized for the two places a vehicle photo is ever shown, and nothing else.

    python seed/build_media.py ~/Downloads/Images          # writes the fixtures
    python seed/build_media.py ~/Downloads/Images --dry-run

WHY RE-ENCODE AT ALL. The photos that shipped before this were the vendor's
originals: jet-t2-idm.png was 723KB of 1524x603 PNG painted into a 240x134 card,
which is ~35x more pixels than the card can show and about 30x more bytes than
the same picture needs. Nine hero images came to 2.1MB, every one of them
blocking the moment a card appeared.

THE TWO WIDTHS ARE MEASURED, NOT GUESSED. The card image is 240px wide
(styles.css .vcard) and the detail dialog is `min(30rem, 100%)` = 480px wide
(.vdlg), so at 2x for retina the only sizes worth storing are 480 and 960. A
1440-wide rendition was considered and dropped: nothing in this UI is ever wider
than 480 CSS px, so those bytes could never reach a screen.

AVIF WITH A WEBP SIBLING. AVIF is roughly 30% smaller than WebP at matched
quality and is what the browser takes; the WebP exists so a pre-16.4 Safari gets
a photo rather than the empty-card diamond. The frontend pairs them in a
<picture> — see components/VehiclePhoto.tsx, which offers the AVIF as the
<source> and the WebP as the <img> fallback (that order is the whole mechanism),
deriving one path from the other with the helpers in media.ts. That derivation
is the same kind catalogue._image_url does on the same string: neither invents
anything, because the sibling is on disk beside it — written right here.

FLATTENED ONTO WHITE, DELIBERATELY. One source (T1 iDM Interior_Grey_Black_1)
carries an alpha channel, but its minimum alpha is 191 across 0.49% of pixels —
a soft edge, not a cut-out. Cards paint on --bg-sunken, so preserving that
translucency would tint those pixels with the card colour on one image out of
thirty-five and no others. Composited on white it is invisible, and every output
is then the same kind of file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

# THE MAP IS EXPLICIT, AND THAT IS THE POINT. Elite's filenames carry a model, a
# section and a colour ("X70 FL Plus_Exterior_Drak Grey_1.png") in a convention
# that is nearly regular and not quite: "T1Exterior" has no separator, "G700
# interior_" lowercases the section and adds a stray space, "Drak" and "Balck"
# are typos, and "T1" is a prefix of "T1 iDM" so any name-matching rule has to be
# ordered to avoid filing the hybrid's photos under the petrol car. A table that
# a human checked once is worth more than a parser that is subtly wrong on two
# rows, and the next delivery gets checked the same way.
#
# The slug and the '-interior-' marker are the OUTPUT convention, and both are
# load-bearing: catalogue._image_url picks the first non-interior path as the
# hero and _interior_image_urls picks the rest by that marker (tests/
# test_catalogue.py pins both). Keep them.
#
# Colour is not carried into the filenames. The photos cover 11 of the 40
# exterior colours Elite lists, so a colour on a card would be right about a
# quarter of the time and silent the rest — dropped by decision, 2026-09-04.
MAPPING: list[tuple[str, str]] = [
    # source filename (in the supplied folder)      ->  output stem
    ("G700_Exterior_Matte Orange_1.webp", "jet-g700-1"),
    ("G700_Exterior_Matte Orange_2.webp", "jet-g700-2"),
    ("G700 interior_ Matte Orange_1.avif", "jet-g700-interior-1"),
    ("G700 interior_ Matte Orange_2.avif", "jet-g700-interior-2"),
    ("T2 Luxury Plus Exterior_Matte Grey_1.avif", "jet-t2-1"),
    ("T2 Luxury Plus Exterior_Matte Grey_2.avif", "jet-t2-2"),
    ("T2 Luxury Plus Interior_Green_1.avif", "jet-t2-interior-1"),
    ("T2 Luxury Plus Interior_Green_2.avif", "jet-t2-interior-2"),
    ("T2 iDM Exterior_Silver_1.avif", "jet-t2-idm-1"),
    ("T2 iDM Exterior_Silver_2.png", "jet-t2-idm-2"),
    ("T2 iDM Interior_Ivory Blue_1.avif", "jet-t2-idm-interior-1"),
    ("T2 iDM Interior_Ivory Blue_2.avif", "jet-t2-idm-interior-2"),
    ("T1Exterior_Silver_1.png", "jet-t1-1"),
    ("T1Exterior_Silver_2.png", "jet-t1-2"),
    ("T1 Interior_Balck_Light Grey_1.avif", "jet-t1-interior-1"),
    ("T1 Interior_Balck_Light Grey_2.avif", "jet-t1-interior-2"),
    ("T1 iDM Exterior_Silver_1.png", "jet-t1-idm-1"),
    ("T1 iDM Exterior_Silver_2.png", "jet-t1-idm-2"),
    ("T1 iDM Interior_Grey_Black_1.avif", "jet-t1-idm-interior-1"),
    ("T1 iDM Interior_Grey_Black_2.avif", "jet-t1-idm-interior-2"),
    # THE DASHING IS THE ONE MODEL SHOT PER TRIM. The Dark Grey car is the only
    # exterior not tied to a trim in its filename, so it is the MODEL's hero —
    # what a customer browsing "Dashing" sees before choosing 1.5 or 1.6. The
    # two trim-specific sets below hang off vehicle_variants instead.
    ("Dashing_Exterior_Dark Grey_2.png", "jet-dash-1"),
    ("Dashing 1.5_Exterior_Dark Green_1.png", "jet-dash-v11-1"),
    ("Dashing 1.6_Exterior_Light Blue_1.png", "jet-dash-v12-1"),
    ("Dashing 1.6_Exterior_Light Blue_2.png", "jet-dash-v12-2"),
    # Both interiors came from the 1.5 shoot, but interior_colours is a per-MODEL
    # palette (see models.py) and Black/Ivory is the whole Dashing palette — so
    # they belong to the model, not to the trim they happened to be shot in.
    ("Dashing 1.5_Interior_Black.avif", "jet-dash-interior-1"),
    ("Dashing 1.5_Interior_Ivory_1.png", "jet-dash-interior-2"),
    ("X70 FL Plus_Exterior_Drak Grey_1.png", "jet-x70p-1"),
    ("X70 FL Plus_Interior_Beige_1.avif", "jet-x70p-interior-1"),
    ("X70 FL_Exterior_Black_1.png", "jet-x70fl-1"),
    ("X70 FL_Exterior_Black_2.png", "jet-x70fl-2"),
    ("X70 FL_Interior_White_1.avif", "jet-x70fl-interior-1"),
    ("X70 FL_Interior_White_2.avif", "jet-x70fl-interior-2"),
    ("X50_Exterior_Silver_1.png", "jet-x50-1"),
    ("X50_Interior_Grey_Black_1.avif", "jet-x50-interior-1"),
    ("X50_Interior_Grey_Black_2.avif", "jet-x50-interior-2"),
]

# suffix -> target width in device pixels. "" is the card rendition (the one
# preloaded at boot and the one 95% of views ever fetch); "@full" is the dialog.
WIDTHS: dict[str, int] = {"": 480, "@full": 960}

# Quality settings chosen by inspection at card size, not by copying defaults.
# AVIF speed 4 is the slow-ish end of "still worth waiting for" — this runs once
# per delivery, so encode time is free and every byte saved is paid back on every
# view forever.
AVIF_QUALITY = 58
AVIF_SPEED = 4
WEBP_QUALITY = 80
WEBP_METHOD = 6

# EIGHT OF THE EXTERIORS WERE SCREEN-CAPTURED, not exported, and carry the
# capture's own UI: a rounded back-arrow button floating in the sky about 4% in
# from the left edge. It survives at every size and it is unmistakably not part
# of the car (see the module note in the PR; found 2026-09-04 in the layout
# harness, on the compare tile).
#
# WHY A CROP AND NOT A PATCH. Painting the arrow out means inventing pixels, and
# the automated detector could not find its bounds reliably — it kept locking on
# to bright trim and reflections on the cars themselves, so a bad box would have
# smeared a wheel arch instead of the sky. Cropping removes it with no invention
# at all, and these frames have room to give: the car sits centred with empty
# ground either side.
#
# SYMMETRIC, so the car stays centred — taking 9% off only the left would slide
# every one of these off-centre against the fourteen frames that need no crop.
# 9% clears the arrow (its right edge sits at ~7%) with margin.
CHROME_TRIM = 0.09
CHROME_CAPTURES = {
    "T1 iDM Exterior_Silver_1.png",
    "T1 iDM Exterior_Silver_2.png",
    "T1Exterior_Silver_1.png",
    "T1Exterior_Silver_2.png",
    "T2 iDM Exterior_Silver_2.png",
    "X50_Exterior_Silver_1.png",
    "X70 FL_Exterior_Black_1.png",
    "X70 FL_Exterior_Black_2.png",
}

OUT_DIR = Path(__file__).resolve().parent / "media" / "vehicles"


def _load_flat(path: Path) -> Image.Image:
    """Open as RGB, compositing any alpha onto white (see the module docstring)."""
    im = Image.open(path)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB")


def _resize(im: Image.Image, width: int) -> Image.Image:
    """Downscale to `width`, preserving aspect. NEVER UPSCALES: two sources are
    already narrower than the 960 rendition (G700 interior_2 is 534px wide), and
    stretching them would spend bytes manufacturing blur. A smaller-than-target
    file is correct — the browser scales it, and it is still the best we hold.
    """
    if im.width <= width:
        return im
    height = round(im.height * width / im.width)
    return im.resize((width, height), Image.LANCZOS)


def build(src_dir: Path, *, dry_run: bool = False) -> int:
    missing = [name for name, _ in MAPPING if not (src_dir / name).is_file()]
    if missing:
        print(f"error: {len(missing)} source file(s) not in {src_dir}:", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        return 1

    extra = sorted(
        p.name
        for p in src_dir.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.name not in {n for n, _ in MAPPING}
    )
    if extra:
        # Loud rather than silent: an unmapped file is almost always a photo
        # Elite sent that nobody added to MAPPING, and it would otherwise just
        # never appear in the app with no error anywhere.
        print(f"warning: {len(extra)} file(s) in {src_dir} are not in MAPPING and are ignored:")
        for name in extra:
            print(f"  {name}")

    if not dry_run:
        OUT_DIR.mkdir(parents=True, exist_ok=True)

    total_in = total_out = 0
    for name, stem in MAPPING:
        src = src_dir / name
        total_in += src.stat().st_size
        base = _load_flat(src)
        if name in CHROME_CAPTURES:
            w, h = base.size
            base = base.crop((round(w * CHROME_TRIM), 0, round(w * (1 - CHROME_TRIM)), h))
        for suffix, width in WIDTHS.items():
            im = _resize(base, width)
            for ext, kwargs in (
                ("avif", {"quality": AVIF_QUALITY, "speed": AVIF_SPEED}),
                ("webp", {"quality": WEBP_QUALITY, "method": WEBP_METHOD}),
            ):
                out = OUT_DIR / f"{stem}{suffix}.{ext}"
                if not dry_run:
                    im.save(out, **kwargs)
                    total_out += out.stat().st_size
        mark = "  [chrome cropped]" if name in CHROME_CAPTURES else ""
        print(f"  {name}  ->  {stem}  ({base.width}x{base.height}){mark}")

    print(f"\n{len(MAPPING)} sources -> {len(MAPPING) * len(WIDTHS) * 2} files")
    print(f"in  {total_in / 1024 / 1024:.2f} MB")
    if not dry_run:
        print(f"out {total_out / 1024 / 1024:.2f} MB  (all renditions, both formats)")
        card = sum(
            (OUT_DIR / f"{stem}.avif").stat().st_size
            for _, stem in MAPPING
            if "-interior-" not in stem
        )
        print(f"    {card / 1024:.0f} KB of that is the AVIF hero set the browser preloads")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src_dir", type=Path, help="folder of Elite's supplied photography")
    ap.add_argument("--dry-run", action="store_true", help="check the mapping, write nothing")
    args = ap.parse_args()
    if not args.src_dir.is_dir():
        print(f"error: {args.src_dir} is not a directory", file=sys.stderr)
        return 1
    return build(args.src_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
