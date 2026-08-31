"""Convert Elite's `Data.xlsx` into the four structured seed fixtures (T0.5).

    python seed/convert_workbook.py ~/Downloads/Data.xlsx

WHY THIS SCRIPT EXISTS RATHER THAN HAND-TYPED JSON. The workbook carries 66
stock rows across 15 variants and 5 branches, and every one of them needs the
same five normalisations applied. Typing 330 numbers by hand is how a stock
answer quietly becomes wrong for one colour at one branch. It also makes the
normalisation AUDITABLE: the rules below are the conversion, so a re-import
cannot silently drop them (the plan's "normalise at conversion" decision).

It is a ONE-SHOT CONVERTER, not part of the app. `seed/*.json` is the reviewed
artefact that ships; this only regenerates it. openpyxl is deliberately NOT a
dependency — xlsx is a zip of XML and the stdlib reads it fine.

THE FIVE NORMALISATIONS, each of which the workbook needs:

  1. `G700 Topr Fire` -> `G700 Top Fire`. A confirmed spelling mistake (the
     other sheets already have it right). Pinned by a test.
  2. Model names differ per sheet: `Jetour T2 - Luxury Plus` / `Jetour T2
     Luxury Plus`, `Jetour T2  i - DM` / `Jetour T2 - i - DM`, `Jetour Dashing
     - 1.5 Standard` (model+variant in one cell) vs Model+Variant columns.
     Collapsed to one canonical name per model.
  3. Branch headers -> showroom ids: `Dubai - Alqouz` is the `al_quoz` slug.
  4. Merged cells. The workbook leaves Model/Variant blank on continuation rows
     and relies on the eye to carry them down. Forward-filled explicitly.
  5. Colour case: the T2 i-DM's silver is lowercase where every other row is
     title case. Same colour, so same string.

AND THE ONE READING THAT IS EASY TO GET WRONG. In the Stock sheet, `Exterior
Colors` and `Interior Colors` are TWO INDEPENDENT LISTS running down the same
block of rows, not a pairing. The G700 Comfort has six exterior colours and two
interior ones; the X70 FL Plus has three and one. The unit counts belong to the
EXTERIOR colour only. Reading them as pairs would produce claims Elite never
made ("Night Black with Orange interior: 30 at Al Quoz"). So Stock is keyed on
exterior colour, and the interior palette is recorded at MODEL level.
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- #
# xlsx reading, stdlib only
# --------------------------------------------------------------------------- #
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref).group(1)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_sheets(path: Path) -> dict[str, list[list[str]]]:
    z = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{_NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
    rels = {
        r.get("Id"): r.get("Target")
        for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    }
    out: dict[str, list[list[str]]] = {}
    for sh in ET.fromstring(z.read("xl/workbook.xml")).find(f"{_NS}sheets"):
        target = rels[sh.get(f"{_REL}id")]
        if not target.startswith("xl/"):
            target = "xl/" + target.lstrip("/")
        rows: list[list[str]] = []
        for row in ET.fromstring(z.read(target)).iter(f"{_NS}row"):
            cells: dict[int, str] = {}
            for c in row.findall(f"{_NS}c"):
                v, inline = c.find(f"{_NS}v"), c.find(f"{_NS}is")
                if c.get("t") == "s" and v is not None:
                    val = shared[int(v.text)]
                elif inline is not None:
                    val = "".join(t.text or "" for t in inline.iter(f"{_NS}t"))
                elif v is not None:
                    val = v.text
                else:
                    continue
                cells[_col_index(c.get("r"))] = val
            if cells:
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
        out[sh.get("name")] = rows
    return out


def cell(row: list[str], i: int) -> str:
    return (row[i] if i < len(row) else "").strip()


# --------------------------------------------------------------------------- #
# Normalisation tables
# --------------------------------------------------------------------------- #
# Every spelling of a model that appears anywhere in the workbook, mapped to the
# model_code that already exists in the database. The codes are KEPT AS THEY
# ARE even where the name changed (JET-T2 now names the T2 Luxury Plus, JET-X70P
# the X70 FL Plus): model_code is a natural key that slots, bookings, the
# doorstep exclusion list and the brochure filename all key off, and renaming it
# to tidy it would be a migration with no benefit to a customer.
MODEL_CODE_BY_SHEET_NAME = {
    "jetour g700": "JET-G700",
    "jetour t2 - luxury plus": "JET-T2",
    "jetour t2 luxury plus": "JET-T2",
    "jetour t2 i - dm": "JET-T2-IDM",
    "jetour t2 - i - dm": "JET-T2-IDM",
    "jetour t2 i-dm": "JET-T2-IDM",        # spelling used in fuel_economy.json
    "jetour t1": "JET-T1",
    "jetour t1 i - dm": "JET-T1-IDM",
    "jetour t1 i-dm": "JET-T1-IDM",        # spelling used in fuel_economy.json
    "jetour dashing": "JET-DASH",
    "jetour dashing - 1.5 standard": "JET-DASH",
    "jetour dashing - 1.6 luxury": "JET-DASH",
    "jetour x70 fl": "JET-X70FL",
    "jetour x70 fl plus": "JET-X70FLP",  # placeholder, remapped below
    "jetour x50": "JET-X50",
    # THE TYPO. `Topr Fire` appears in Models and Stock; On Road Pricing,
    # Warranty and Delivery Time all say `Top Fire`. Confirmed with Elite as a
    # spelling mistake, so it is corrected here once and pinned by a test.
    "g700 topr fire": "JET-G700",
    "g700 top fire": "JET-G700",
}
# `Jetour X70 FL Plus` must not fall through the `jetour x70 fl` prefix, and it
# keeps the existing JET-X70P code, not a new one.
MODEL_CODE_BY_SHEET_NAME["jetour x70 fl plus"] = "JET-X70P"

# The variant a workbook row names, once the model is known. Rows where the
# model cell already carries the variant ("Jetour Dashing - 1.5 Standard") set
# it here, because the Variant column is blank on those rows.
VARIANT_IN_MODEL_CELL = {
    "jetour dashing - 1.5 standard": "1.5 Standard",
    "jetour dashing - 1.6 luxury": "1.6 Luxury",
    "g700 topr fire": "Top Fire",
    "g700 top fire": "Top Fire",
}

# Branch column header -> showrooms.json id. Al Quoz is spelled `Alqouz` in the
# sheet and `al_quoz` in the database.
SHOWROOM_ID_BY_HEADER = {
    "dubai - alqouz": 1,
    "dubai - deira": 3,
    "abu dhabi": 4,
    "ajman": 6,
    "al ain": 5,
}


def norm(s: str) -> str:
    """Collapse whitespace and case for matching. Not for display."""
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def colour(s: str) -> str:
    """Display form of a colour. Only case is normalised — the T2 i-DM's
    `silver` is the same paint as every other row's `Silver`."""
    return re.sub(r"\s+", " ", (s or "").strip()).title()


def money(s: str) -> int | None:
    s = (s or "").strip().replace(",", "")
    if not s or not re.fullmatch(r"\d+(\.\d+)?", s):
        return None
    return int(float(s))


# --------------------------------------------------------------------------- #
# Corrections to the curated marketing bullets
# --------------------------------------------------------------------------- #
# The English/Arabic headline bullets are PORTED VERBATIM from the previous
# fixture (migration rule zero: port, don't re-author) — with these exceptions,
# each of which a REVIEWED SOURCE contradicts. Nothing here is a style edit.
#
# Keyed by (model_code, bullet index) -> (english, arabic). Arabic is edited in
# lockstep because seed_vehicle_models asserts the two lists stay parallel.
BULLET_CORRECTIONS: dict[tuple[str, int], tuple[str, str]] = {
    # --- X70 FL: it is NOT the true seven-seater. Its specification table says
    #     `5/5+2`; the X70 FL PLUS's says `7`. The old bullet had them swapped,
    #     the seed's seat range had them swapped, and catalogue._ranked_for's
    #     comment described the swap as fact. This is the root of the backwards
    #     reference answer.
    ("JET-X70FL", 1): (
        "Five seats, with an occasional 5+2 third row",
        "خمسة مقاعد مع صف ثالث عَرَضي (5+2)",
    ),
    # 12.3" is the DIGITAL CLUSTER; the centre touchscreen is 10.2". The old
    # bullet called 12.3" the central control screen.
    ("JET-X70FL", 2): (
        '12.3" digital cluster + 10.2" touch infotainment',
        'لوحة عدادات رقمية 12.3 بوصة وشاشة لمس 10.2 بوصة',
    ),
    # "13.5 km/L fuel economy" REMOVED, for two reasons. Elite's own economy
    # dataset says 13.7, not 13.5 — and economy is now a COLUMN on the variant,
    # so a marketing bullet repeating it is a second source that can drift.
    # Replaced with a fact from the brochure that nothing else carries.
    ("JET-X70FL", 4): (
        "8 speakers, panoramic roof, 20-inch alloys",
        "8 مكبرات صوت، سقف بانورامي، جنوط 20 بوصة",
    ),
    # --- X70 FL Plus: the workbook sells ONE trim (Luxury), not two, and this
    #     is the car that genuinely seats seven.
    ("JET-X70P", 0): (
        "1.6L TGDI turbo, 197 hp, 7-speed DCT",
        "محرك 1.6 لتر تيربو حقن مباشر، 197 حصان، ناقل 7 سرعات",
    ),
    ("JET-X70P", 1): (
        "True seven-seater — second and third rows removable",
        "سبعة مقاعد حقيقية — الصف الثاني والثالث قابلان للإزالة",
    ),
    # --- X50: "17.9 km/L — most efficient in the UAE range" was wrong twice.
    #     Elite's dataset puts the X50 at 12.0-14.0 km/L, and the most efficient
    #     cars in the range are the two i-DM plug-in hybrids (15.8-18.5 and 18).
    #     Replaced with the warranty, which its brochure states outright.
    ("JET-X50", 4): (
        "10-year / 1,000,000 km warranty",
        "ضمان 10 سنوات / مليون كيلومتر",
    ),
}

# Model rename, where the workbook's name differs from the old fixture's.
NAME_CORRECTIONS = {
    "JET-T2": "Jetour T2 Luxury Plus",
    "JET-X70P": "Jetour X70 FL Plus",
}

# body_type is free text the catalogue substring-matches on. The X70 FL's said
# "Mid-size 7-seater SUV", which is the claim its own brochure contradicts.
BODY_TYPE_CORRECTIONS = {
    "JET-X70FL": "Mid-size family SUV, 5 or 5+2 seats",
    "JET-X70P": "Mid-size 7-seater family SUV",
}

# Seats per VARIANT, from the brochure specification tables — NOT from the
# workbook, which does not carry seat counts at all.
#
# `None` means genuinely unknown and is stored as null rather than guessed:
#   * T1 i-DM — its brochure never states a seat count, and says in terms that
#     sharing the petrol T1's body does not prove it shares the seating. Until
#     T0.4b reads the remaining marketing pages this car will not match a
#     "how many seats" filter, which is the honest behaviour and a visible gap.
#   * G700 Top Fire — coming soon, nothing published.
SEATS_BY_VARIANT: dict[tuple[str, str | None], tuple[int, int] | None] = {
    ("JET-G700", "Comfort"): (5, 5),      # 2+3
    ("JET-G700", "Luxury"): (6, 6),       # 2+2+2
    ("JET-G700", "Flagship"): (6, 6),     # 2+2+2
    ("JET-G700", "Top Fire"): None,
    ("JET-T2", None): (5, 5),
    ("JET-T2-IDM", None): (5, 5),         # 2+3
    ("JET-T1", "Comfort"): (5, 5),        # 2+3, all three trims
    ("JET-T1", "Luxury"): (5, 5),
    ("JET-T1", "Flagship"): (5, 5),
    ("JET-T1-IDM", None): None,           # unconfirmed — see above
    ("JET-DASH", "1.5 Standard"): (5, 5),
    ("JET-DASH", "1.6 Luxury"): (5, 5),
    ("JET-X70FL", "Luxury"): (5, 7),      # spec table: 5 / 5+2
    ("JET-X70P", "Luxury"): (7, 7),       # spec table: 7, rows removable
    ("JET-X50", "Premium"): (5, 5),
}

# The X50's workbook rows leave the variant blank, but its brochure's
# specification table names the trim outright: "Trim | Premium". The workbook is
# authoritative for COMMERCIAL data (price, stock); the brochure is
# authoritative for what the trim is called, and a name beats a blank.
VARIANT_NAME_OVERRIDES = {"JET-X50": "Premium"}

# Interior colour palettes are emitted per MODEL, into vehicle_models.
# interior_colours. See that column's comment in models.py for the three things
# they are not — paired with the exterior colour, stocked, or per-variant.


def _rows_after_header(rows: list[list[str]], header_token: str) -> list[list[str]]:
    for i, r in enumerate(rows):
        if any(header_token in norm(c) for c in r):
            return rows[i + 1:]
    raise SystemExit(f"header row containing {header_token!r} not found")


def parse_models(rows: list[list[str]]) -> dict[str, dict]:
    """Models sheet -> {model_code: {variants: [...], interior: [...]}}.

    Forward-fills the merged Model/Variant cells, and keeps variant ORDER as the
    sheet has it (Comfort, Luxury, Flagship) rather than sorting — the sheet's
    order is Elite's, and trim names do not sort into a ladder anyway.
    """
    out: dict[str, dict] = {}
    code = None
    for r in _rows_after_header(rows, "model"):
        raw_model = cell(r, 1)
        if raw_model:
            key = norm(raw_model)
            if key not in MODEL_CODE_BY_SHEET_NAME:
                raise SystemExit(f"unknown model in Models sheet: {raw_model!r}")
            code = MODEL_CODE_BY_SHEET_NAME[key]
            out.setdefault(code, {"variants": [], "interior": [], "exterior": []})
            forced = VARIANT_IN_MODEL_CELL.get(key)
            if forced and forced not in out[code]["variants"]:
                out[code]["variants"].append(forced)
        if code is None:
            continue
        variant = cell(r, 2)
        if variant and variant not in out[code]["variants"]:
            out[code]["variants"].append(variant)
        ext, inte = colour(cell(r, 5)), colour(cell(r, 6))
        if ext and ext != "Coming Soon" and ext not in out[code]["exterior"]:
            out[code]["exterior"].append(ext)
        if inte and inte != "Coming Soon" and inte not in out[code]["interior"]:
            out[code]["interior"].append(inte)
    return out


def parse_pricing(rows: list[list[str]]) -> dict[tuple[str, str | None], dict]:
    """On Road Pricing -> {(model_code, variant): {price, insurance, reg}}.

    Forward-fills the Model column, which is blank on the G700's and T1's
    second and third trim rows.
    """
    out: dict[tuple[str, str | None], dict] = {}
    code = None
    for r in _rows_after_header(rows, "component"):
        raw_model = cell(r, 1)
        if raw_model:
            key = norm(raw_model)
            if key not in MODEL_CODE_BY_SHEET_NAME:
                raise SystemExit(f"unknown model in On Road Pricing: {raw_model!r}")
            code = MODEL_CODE_BY_SHEET_NAME[key]
        if code is None:
            continue
        variant = cell(r, 2) or VARIANT_IN_MODEL_CELL.get(norm(raw_model)) or None
        out[(code, variant)] = {
            "price_aed": money(cell(r, 4)),
            "insurance_aed": money(cell(r, 5)),
            "registration_aed": money(cell(r, 6)),
            "coming_soon": norm(cell(r, 3)) == "coming soon",
        }
    return out


def parse_stock(rows: list[list[str]]) -> list[dict]:
    """Stock -> one record per (model, variant, EXTERIOR colour, showroom).

    The interior colour column is ignored here on purpose — see the module
    docstring. Rows whose only colour is "Coming Soon" (the Top Fire) produce no
    stock at all: a car with no published colours has no inventory to report.
    """
    header_i = next(
        i for i, r in enumerate(rows) if any("alqouz" in norm(c) for c in r)
    )
    header = rows[header_i]
    branch_cols: list[tuple[int, int]] = []
    for i, c in enumerate(header):
        sid = SHOWROOM_ID_BY_HEADER.get(norm(c))
        if sid:
            branch_cols.append((i, sid))
    if len(branch_cols) != 5:
        raise SystemExit(f"expected 5 branch columns, found {len(branch_cols)}")

    out: list[dict] = []
    code = variant = None
    for r in rows[header_i + 1:]:
        raw_model = cell(r, 1)
        if raw_model:
            key = norm(raw_model)
            if key not in MODEL_CODE_BY_SHEET_NAME:
                raise SystemExit(f"unknown model in Stock: {raw_model!r}")
            code = MODEL_CODE_BY_SHEET_NAME[key]
            variant = cell(r, 2) or VARIANT_IN_MODEL_CELL.get(key) or None
        if code is None:
            continue
        ext = colour(cell(r, 5))
        if not ext or ext == "Coming Soon":
            continue
        for col, showroom_id in branch_cols:
            units = money(cell(r, col))
            out.append(
                {
                    "model_code": code,
                    "variant": variant,
                    "colour": ext,
                    "showroom_id": showroom_id,
                    "units": 0 if units is None else units,
                }
            )
    return out


def parse_banks(rows: list[list[str]]) -> list[dict]:
    out: list[dict] = []
    for r in _rows_after_header(rows, "financing type"):
        name = cell(r, 1)
        if not name or not cell(r, 2):
            continue  # the trailing free-text assumptions paragraph
        rate = re.search(r"([\d.]+)\s*%", cell(r, 3))
        down = re.search(r"([\d.]+)\s*%", cell(r, 4))
        tenure = re.search(r"(\d+)", cell(r, 5))
        out.append(
            {
                "id": len(out) + 1,
                "name": name,
                "financing_type": cell(r, 2),
                "rate_percent": float(rate.group(1)) if rate else None,
                "min_down_payment_percent": float(down.group(1)) if down else None,
                "max_tenure_months": int(tenure.group(1)) if tenure else None,
                # "Shariah-compliant (Murabaha)" is the sheet's own wording.
                "islamic": "shariah" in norm(cell(r, 2)) or "islamic" in norm(name),
                # EVERY row Elite supplied is marked "Sample". Kept as data so
                # the agent's caveat is driven by the row, not by a hardcoded
                # assumption that all rates are indicative forever.
                "is_sample": True,
                "notes": cell(r, 6),
            }
        )
    return out


def parse_accessories(rows: list[list[str]]) -> list[dict]:
    out: list[dict] = []
    for r in _rows_after_header(rows, "part name"):
        part = cell(r, 1)
        if not part:
            continue
        out.append(
            {
                "id": len(out) + 1,
                "part_code": cell(r, 0),
                "name_en": part,
                # No Arabic anywhere in the accessories sheet — not for the name
                # and not for the description. Left NULL rather than machine
                # translated: a wrong Arabic part name is worse than none. This
                # is a DATA gap to take back to Elite, not one a column closes.
                "name_ar": None,
                "description_en": cell(r, 2),
                "price_aed": money(cell(r, 3)),
                # Every accessory Elite listed fits any car; none is model-tied.
                "model_id": None,
            }
        )
    return out


def load_fuel_economy() -> dict[tuple[str, str | None], dict]:
    """seed/fuel_economy.json, keyed the same way as everything else.

    Its model/variant strings are Elite's, so they go through the same
    normalisation. Single-variant models are keyed with a None variant there and
    matched positionally against the model's only variant.
    """
    data = json.loads((HERE / "fuel_economy.json").read_text(encoding="utf-8"))
    out: dict[tuple[str, str | None], dict] = {}
    for row in data["variants"]:
        key = norm(row["model"])
        code = MODEL_CODE_BY_SHEET_NAME.get(key)
        if code is None:
            raise SystemExit(f"fuel_economy.json names an unknown model: {row['model']!r}")
        variant = row["variant"] or VARIANT_IN_MODEL_CELL.get(key)
        out[(code, variant)] = row
    return out


def _lookup(table: dict, code: str, variant: str | None):
    """Join across sheets that name the same trim differently.

    The X70 FL is `Luxury` in Models and Stock but blank in On Road Pricing;
    the X50 is blank everywhere and named `Premium` only by its brochure. Rather
    than force one spelling on Elite's workbook, every join tries the named key
    first and falls back to the single-unnamed-trim key.
    """
    if (code, variant) in table:
        return table[(code, variant)]
    if (code, None) in table:
        return table[(code, None)]
    return None


def main() -> None:
    src = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else None
    if src is None or not src.exists():
        raise SystemExit(f"usage: python {sys.argv[0]} <path to Data.xlsx>")

    sheets = {norm(k): v for k, v in read_sheets(src).items()}
    models_sheet = parse_models(sheets["models"])
    pricing = parse_pricing(sheets["on road pricing"])
    stock_rows = parse_stock(sheets["stock"])
    banks = parse_banks(sheets["financing & emi"])
    accessories = parse_accessories(sheets["list of accessories and pricing"])
    economy = load_fuel_economy()

    # Ported wholesale from the fixture being replaced: ids, model_code, brand,
    # body_type, headline bullets (EN + AR), showroom_ids and images. Only the
    # corrections declared above are applied on top.
    previous = {
        r["model_code"]: r
        for r in json.loads((HERE / "vehicle_models.json").read_text(encoding="utf-8"))
    }

    models: list[dict] = []
    variant_id = 0
    variant_index: dict[tuple[str, str | None], int] = {}

    for code, prev in sorted(previous.items(), key=lambda kv: kv[1]["id"]):
        sheet = models_sheet[code]
        names: list[str | None] = list(sheet["variants"]) or [
            VARIANT_NAME_OVERRIDES.get(code)
        ]

        variants = []
        for name in names:
            price = _lookup(pricing, code, name) or {}
            fuel = _lookup(economy, code, name) or {}
            seats = SEATS_BY_VARIANT.get((code, name), "missing")
            if seats == "missing":
                raise SystemExit(f"no seat decision recorded for {code} / {name!r}")
            variant_id += 1
            variant_index[(code, name)] = variant_id
            variants.append(
                {
                    "id": variant_id,
                    "name": name,
                    "price_aed": price.get("price_aed"),
                    "insurance_aed": price.get("insurance_aed") or None,
                    "registration_aed": price.get("registration_aed") or None,
                    "km_per_l_min": fuel.get("km_per_l_min"),
                    "km_per_l_max": fuel.get("km_per_l_max"),
                    "seats_min": seats[0] if seats else None,
                    "seats_max": seats[1] if seats else None,
                    "coming_soon": bool(price.get("coming_soon")),
                }
            )

        prices = [v["price_aed"] for v in variants if v["price_aed"] is not None]
        seat_lows = [v["seats_min"] for v in variants if v["seats_min"] is not None]
        seat_highs = [v["seats_max"] for v in variants if v["seats_max"] is not None]

        bullets_en = list(prev["headline_features"])
        bullets_ar = list(prev["headline_features_ar"])
        for (bcode, idx), (en, ar) in BULLET_CORRECTIONS.items():
            if bcode == code:
                bullets_en[idx], bullets_ar[idx] = en, ar

        models.append(
            {
                "id": prev["id"],
                "model_code": code,
                "brand": prev["brand"],
                "name": NAME_CORRECTIONS.get(code, prev["name"]),
                "body_type": BODY_TYPE_CORRECTIONS.get(code, prev["body_type"]),
                # The MODEL keeps a seat and price RANGE spanning its trims —
                # catalogue.search() still returns model cards. Both are derived
                # from the variants below, never entered twice.
                "seats_min": min(seat_lows) if seat_lows else None,
                "seats_max": max(seat_highs) if seat_highs else None,
                "price_aed_min": min(prices) if prices else None,
                "price_aed_max": max(prices) if prices else None,
                "fuel_type": prev["fuel_type"],
                "headline_features": bullets_en,
                "headline_features_ar": bullets_ar,
                "showroom_ids": prev["showroom_ids"],
                "data_provenance": (
                    "Prices, variants, stock and colours from Elite's Data.xlsx "
                    "(supplied 2026-08); seats and specifications from the Jetour "
                    "brochures in content/brochures/; fuel economy from Elite's "
                    "separate dataset (seed/fuel_economy.json). Marketing bullets "
                    "from the Jetour UAE catalogue (Jetour-UAE-Model-Catalogue.docx), "
                    "July 2026. Prices indicative and subject to change; confirm "
                    "with the showroom."
                ),
                "images": prev["images"],
                "interior_colours": sheet["interior"],
                "variants": variants,
            }
        )

    stock: list[dict] = []
    for row in stock_rows:
        key = (row["model_code"], row["variant"])
        vid = variant_index.get(key)
        if vid is None:
            vid = variant_index.get((row["model_code"], None))
        if vid is None:
            single = [
                v for (c, _n), v in variant_index.items() if c == row["model_code"]
            ]
            vid = single[0] if len(single) == 1 else None
        if vid is None:
            raise SystemExit(f"stock row matches no variant: {row}")
        stock.append(
            {
                "variant_id": vid,
                "showroom_id": row["showroom_id"],
                "colour": row["colour"],
                "units": row["units"],
                # Denormalised for human review only; the seeder ignores them.
                "_model_code": row["model_code"],
                "_variant": row["variant"],
            }
        )

    _write("vehicle_models.json", models)
    _write("stock.json", stock)
    _write("finance_banks.json", banks)
    _write("accessories.json", accessories)

    n_variants = sum(len(m["variants"]) for m in models)
    print(f"{len(models)} models, {n_variants} variants")
    print(f"{len(stock)} stock rows ({len({(s['_model_code'], s['_variant'], s['colour']) for s in stock})} variant x colour)")
    print(f"{sum(s['units'] for s in stock)} units, {len(banks)} banks, {len(accessories)} accessories")


def _write(name: str, payload) -> None:
    (HERE / name).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote seed/{name}")


if __name__ == "__main__":
    main()
