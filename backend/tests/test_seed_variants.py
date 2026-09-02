"""The variant-level seed, and the four corrections a re-import could undo (T0.5).

Elite's workbook is the source, and it will be re-imported. Four things in it
are WRONG OR MISLEADING, and each was fixed once by hand:

  1. `G700 Topr Fire` is a typo for `Top Fire`.
  2. The X70 FL and X70 FL Plus had their SEAT COUNTS SWAPPED in the old seed.
  3. The T1's trims do NOT ascend Comfort -> Luxury -> Flagship, and a reviewer
     who "fixes" that is reintroducing a bug.
  4. The X50's trim is named `Premium` by its brochure and nowhere by the sheet.

None of the four is self-evident from the data, so each is asserted here. A
re-import that loses one fails a test instead of shipping a wrong answer.

Offline: reads the fixture and the seeded `db`, no vendor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Accessory, FinanceBank, Showroom, Stock, VehicleModel, VehicleVariant

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "seed"


def _fixture(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1. the typo
# --------------------------------------------------------------------------- #
def test_top_fire_is_never_spelled_topr_fire():
    """`Topr Fire` appears in two of the workbook's five sheets.

    Confirmed with Elite as a spelling mistake. The conversion corrects it; this
    fails if a re-import brings it back, in either the fixture or the database.
    """
    raw = (FIXTURE_DIR / "vehicle_models.json").read_text(encoding="utf-8")
    assert "Topr" not in raw
    names = [v["name"] for m in _fixture("vehicle_models.json") for v in m["variants"]]
    assert "Top Fire" in names


# --------------------------------------------------------------------------- #
# 2. the seat swap — the root cause of the backwards reference answer
# --------------------------------------------------------------------------- #
def test_the_x70_plus_is_the_true_seven_seater_and_the_fl_is_not(db):
    """The old seed had these EXACTLY swapped, and so did the comment in
    catalogue._ranked_for that justified the tiebreak.

    Brochure specification tables: X70 FL is `5/5+2`, X70 FL Plus is `7`. The
    marketing pages of BOTH say "5/7 seater"; the spec table is what we quote.
    """
    fl = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-X70FL"))
    plus = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-X70P"))

    assert (fl.seats_min, fl.seats_max) == (5, 7), "the X70 FL is 5, optionally 5+2"
    assert (plus.seats_min, plus.seats_max) == (7, 7), "the X70 FL Plus is a true 7"


def test_most_seats_now_ranks_the_plus_above_the_fl(db):
    """The tiebreak that produced the wrong answer, asserted end to end.

    `most_seats` sorts on -seats_max then -seats_min, so with the seats the right
    way round the car that is ALWAYS a seven-seater beats the one that optionally
    is. With the old data it resolved the other way.
    """
    from app.services import catalogue

    rows = db.scalars(select(VehicleModel)).all()
    ranked = catalogue._ranked_for(rows, "most_seats")
    codes = [v.model_code for v in ranked]
    assert codes[0] == "JET-X70P", f"expected the X70 FL Plus first, got {codes[:3]}"
    assert codes.index("JET-X70P") < codes.index("JET-X70FL")


# --------------------------------------------------------------------------- #
# 3. the T1 trim ladder — the correction that is a NON-change
# --------------------------------------------------------------------------- #
def test_the_t1_luxury_is_priced_above_the_t1_flagship(db):
    """DO NOT "FIX" THIS. It looks like a transposition and is not.

    The T1's Luxury is the four-wheel-drive range-topper — BorgWarner real-time
    4WD, central and rear limited-slip differentials, Snow/Mud/Sand/Rock modes,
    wading radar, 9 SONY speakers, a 15.6" screen and seat memory, at 1833 kg.
    Its Flagship is two-wheel drive and specified close to Comfort, at 1726 kg.

    So Luxury (129,000) above Flagship (125,785) is correct as Elite wrote it,
    and the insurance ladder (2,000 / 2,200 / 2,100) is correct for the same
    reason. An earlier reading of this plan recommended swapping both columns;
    the brochure disproved it and the swap was withdrawn. TRIM NAMES DO NOT
    IMPLY RANK.
    """
    t1 = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-T1"))
    by_name = {v.name: v for v in db.scalars(
        select(VehicleVariant).where(VehicleVariant.model_id == t1.id)
    ).all()}

    assert by_name["Comfort"].price_aed == 109_000
    assert by_name["Luxury"].price_aed == 129_000
    assert by_name["Flagship"].price_aed == 125_785
    assert by_name["Luxury"].price_aed > by_name["Flagship"].price_aed

    # Insurance moves WITH price, which is the second reason to believe it: a
    # transposition would have to have hit both columns identically.
    assert by_name["Comfort"].insurance_aed == 2_000
    assert by_name["Luxury"].insurance_aed == 2_200
    assert by_name["Flagship"].insurance_aed == 2_100


# --------------------------------------------------------------------------- #
# 4. the X50's trim name
# --------------------------------------------------------------------------- #
def test_the_x50_variant_is_named_premium_from_its_brochure(db):
    """The workbook leaves the cell blank; the brochure's spec table says
    "Trim | Premium". A name beats a blank, and a null here would mean "the only
    version" rather than "we could not read it"."""
    x50 = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-X50"))
    variants = db.scalars(
        select(VehicleVariant).where(VehicleVariant.model_id == x50.id)
    ).all()
    assert [v.name for v in variants] == ["Premium"]


# --------------------------------------------------------------------------- #
# Shape and integrity
# --------------------------------------------------------------------------- #
def test_the_catalogue_is_nine_models_and_fifteen_variants(db):
    assert db.query(VehicleModel).count() == 9
    assert db.query(VehicleVariant).count() == 15


def test_exactly_one_variant_is_coming_soon_and_it_has_no_price(db):
    """The G700 Top Fire is the reason price_aed went nullable again (T0.7).

    Coming soon is NOT the same as sold out: a sold-out trim can be ordered, this
    one cannot. It must carry no price rather than a zero.
    """
    soon = db.scalars(
        select(VehicleVariant).where(VehicleVariant.coming_soon.is_(True))
    ).all()
    assert [v.name for v in soon] == ["Top Fire"]
    assert soon[0].price_aed is None
    assert soon[0].on_road_aed is None


def test_every_bookable_variant_can_quote_a_complete_on_road_price(db):
    """on_road_aed is all three parts or nothing — a partial sum would quote a
    number BELOW the list price and read as a discount."""
    incomplete = [
        (v.model.model_code, v.name)
        for v in db.scalars(
            select(VehicleVariant).where(VehicleVariant.coming_soon.is_(False))
        ).all()
        if v.on_road_aed is None
    ]
    assert incomplete == []


def test_a_models_price_range_spans_exactly_its_priced_variants(db):
    """The model card's range is DERIVED. If the two ever disagree, a customer
    is shown one price on the card and another on the trim."""
    for model in db.scalars(select(VehicleModel)).all():
        prices = [
            v.price_aed
            for v in db.scalars(
                select(VehicleVariant).where(VehicleVariant.model_id == model.id)
            ).all()
            if v.price_aed is not None
        ]
        assert model.price_aed_min == min(prices), model.model_code
        # The G700's ceiling must ignore its unpriced Top Fire, not be dragged
        # to None by it.
        assert model.price_aed_max == max(prices), model.model_code


def test_a_models_seat_range_spans_exactly_its_variants(db):
    """The G700 forces this: Comfort seats 5, Luxury and Flagship seat 6."""
    g700 = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-G700"))
    assert (g700.seats_min, g700.seats_max) == (5, 6)
    by_name = {v.name: v for v in db.scalars(
        select(VehicleVariant).where(VehicleVariant.model_id == g700.id)
    ).all()}
    assert (by_name["Comfort"].seats_min, by_name["Comfort"].seats_max) == (5, 5)
    assert (by_name["Luxury"].seats_min, by_name["Luxury"].seats_max) == (6, 6)


def test_the_t1_idm_has_no_seat_count_and_that_is_deliberate(db):
    """A KNOWN GAP, seeded as null rather than guessed.

    Its brochure never states a seat count and says in terms that sharing the
    petrol T1's body does not prove it shares the seating. The cost is visible
    and accepted: this car will not match a "how many seats" filter until T0.4b
    reads the remaining marketing pages. A guessed 5 would have hidden that.
    """
    t1idm = db.scalar(
        select(VehicleModel).where(VehicleModel.model_code == "JET-T1-IDM")
    )
    assert t1idm.seats_min is None and t1idm.seats_max is None

    from app.services import catalogue

    rows = db.scalars(select(VehicleModel)).all()
    kept = catalogue._apply_filters(
        rows, min_seats=5, max_price_aed=None, body_type=None, fuel=None, brand=None
    )
    assert t1idm not in kept, "a car with no published seat count cannot satisfy a seat filter"
    # ...but it is not hidden from an unfiltered search.
    all_rows = catalogue._apply_filters(
        rows, min_seats=None, max_price_aed=None, body_type=None, fuel=None, brand=None
    )
    assert t1idm in all_rows


# --------------------------------------------------------------------------- #
# Stock
# --------------------------------------------------------------------------- #
def test_stock_is_66_variant_colour_combinations_across_5_branches(db):
    rows = db.scalars(select(Stock)).all()
    assert len(rows) == 330
    assert len({(r.variant_id, r.colour) for r in rows}) == 66
    assert len({r.showroom_id for r in rows}) == 5
    assert sum(r.units for r in rows) == 7_544


def test_every_stock_row_points_at_a_real_variant_and_showroom(db):
    """SQLite does not enforce foreign keys by default, so the seed order is the
    only thing keeping these honest — assert it rather than trust it."""
    variant_ids = {v.id for v in db.scalars(select(VehicleVariant)).all()}
    showroom_ids = {s.id for s in db.scalars(select(Showroom)).all()}
    for r in db.scalars(select(Stock)).all():
        assert r.variant_id in variant_ids
        assert r.showroom_id in showroom_ids


def test_the_coming_soon_variant_has_no_stock_rows_at_all(db):
    """Its workbook row's only "colour" is the words "Coming Soon". A car with no
    published colours has no inventory to report — zero rows, not zero units."""
    soon = db.scalar(select(VehicleVariant).where(VehicleVariant.coming_soon.is_(True)))
    assert db.query(Stock).filter(Stock.variant_id == soon.id).count() == 0


def test_zero_unit_rows_are_kept_because_a_known_no_is_an_answer(db):
    """38 of the 66 combinations are zero somewhere. Dropping those rows would
    turn "none of that colour at that branch" — a fact Elite supplied — into
    "we have no record", which is a different and worse answer."""
    zeros = db.query(Stock).filter(Stock.units == 0).count()
    assert zeros == 38


def test_stock_is_keyed_on_exterior_colour_only(db):
    """The workbook's Interior Colors column is a MODEL-LEVEL palette typed down
    the same block of rows, not a pairing — the G700 Comfort has six exterior
    colours and two interior ones. Reading them as pairs would invent claims.

    The proof is arithmetic: interior colour names must never appear as a stock
    colour, and the G700's six stock colours are its six EXTERIOR ones.
    """
    colours = {r.colour for r in db.scalars(select(Stock)).all()}
    assert colours, "no stock seeded — the rest of this test would pass vacuously"
    for interior_only in ("Orange", "Ivory", "Beige", "Black & Orange"):
        assert interior_only not in colours, (
            f"{interior_only!r} is an INTERIOR colour and must never key stock"
        )


# --------------------------------------------------------------------------- #
# Finance and accessories
# --------------------------------------------------------------------------- #
def test_every_finance_rate_is_flagged_as_a_sample(db):
    """Elite marked all three "Sample". Nothing computed from them may be
    presented as a quotation (rule 13), and the caveat is driven by this column
    rather than by an assumption that all rates are indicative forever."""
    banks = db.scalars(select(FinanceBank)).all()
    assert len(banks) == 3
    assert all(b.is_sample for b in banks)
    assert all(b.rate_percent is not None for b in banks)


def test_exactly_one_bank_is_shariah_compliant(db):
    """Customers ask for Islamic finance by name, so it is a real filter."""
    islamic = [b.name for b in db.scalars(select(FinanceBank)).all() if b.islamic]
    assert islamic == ["Dubai Islamic Bank"]


def test_central_bank_caps_are_respected_by_every_seeded_bank(db):
    """UAE Central Bank Regulation 29/2011: max 80% financed (so at least a 20%
    down payment) and max 60 months."""
    for b in db.scalars(select(FinanceBank)).all():
        assert b.min_down_payment_percent >= 20.0, b.name
        assert b.max_tenure_months <= 60, b.name


def test_accessories_are_priced_and_unattached(db):
    accs = db.scalars(select(Accessory)).all()
    assert len(accs) == 5
    assert all(a.price_aed and a.price_aed > 0 for a in accs)
    # None of the five Elite listed is model-specific; the column exists for the
    # day one is.
    assert all(a.model_id is None for a in accs)


def test_every_accessory_can_explain_itself(db):
    """A price with no description is a number the agent cannot justify.

    "Ceramic Coating — AED 1,800" tells a customer neither what it is nor why it
    costs that, and "Paint Protection Film — AED 2,200" reads as absurd beside
    tinting at 900 until you know it covers bumper, hood and mirrors and
    self-heals. The agent speaks only from tool results, so a missing description
    is not a thinner answer — it is no answer, or an invented one.
    """
    for a in db.scalars(select(Accessory)).all():
        assert a.description_en, a.name_en
        assert len(a.description_en) > 20, a.name_en


def test_part_codes_are_present_and_unique(db):
    """The re-import join key. Matching on the name string instead means
    matching "Paint Protection Film (PPF)" character for character."""
    codes = [a.part_code for a in db.scalars(select(Accessory)).all()]
    assert all(codes)
    assert len(set(codes)) == len(codes)
    assert set(codes) == {"ACC-001", "ACC-002", "ACC-003", "ACC-004", "ACC-005"}


def test_accessories_have_no_arabic_and_that_is_a_data_gap_not_a_bug(db):
    """RECORDED, NOT ASSERTED AS DESIRABLE. Elite supplied no Arabic for any
    accessory, so an Arabic conversation gets a price and an English sentence.

    This test exists so the day Elite supplies Arabic, it fails and someone
    wires it through — rather than the null quietly persisting because nothing
    ever looked. Delete it then; do not "fix" it with a machine translation.
    """
    assert all(a.name_ar is None for a in db.scalars(select(Accessory)).all())


# --------------------------------------------------------------------------- #
# Interior colours
# --------------------------------------------------------------------------- #
def test_every_model_carries_its_interior_palette(db):
    """Nine models, one to three interiors each, straight from the Models sheet."""
    for m in db.scalars(select(VehicleModel)).all():
        assert m.interior_colours, m.model_code

    t1 = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-T1"))
    assert t1.interior_colours == [
        "Black & Orange", "Black & Green", "Black & Light Grey"
    ]


def test_interior_and_exterior_palettes_cannot_be_pairs(db):
    """THE READING THIS WHOLE COLUMN DEPENDS ON.

    Elite's sheet runs the exterior and interior lists down the same block of
    rows, so they LOOK like a mapping. They are not: they are different lengths
    for eight of the nine models. Reading them as pairs would have Aisha claiming
    "Night Black with Orange interior", which Elite never said.

    NOTE WHAT AN OVERLAP TEST WOULD HAVE PROVEN — nothing. "Black", "White" and
    "Green" appear in both palettes, correctly: a black car can have a black
    interior. Length is the evidence, not vocabulary.

    AND NOTE THE ONE MODEL THAT MATCHES. The X70 FL has exactly two of each
    (Black, White / Black, White), so anyone checking that car alone would
    conclude they pair and be wrong about the other eight. It is asserted here
    as the exception precisely so it is never used as the rule.
    """
    variants_by_model: dict[int, set[int]] = {}
    for v in db.scalars(select(VehicleVariant)).all():
        variants_by_model.setdefault(v.model_id, set()).add(v.id)

    matched = []
    for m in db.scalars(select(VehicleModel)).all():
        ids = variants_by_model.get(m.id, set())
        exterior = {
            r.colour for r in db.scalars(select(Stock)).all() if r.variant_id in ids
        }
        assert m.interior_colours, m.model_code
        if len(exterior) == len(m.interior_colours):
            matched.append(m.model_code)

    assert matched == ["JET-X70FL"], (
        "exactly one model has equal-length palettes, by coincidence. If this "
        f"list grew to {matched}, re-check whether Elite started supplying "
        "genuine pairings — the answer changes what we may say."
    )


def test_the_g700_palette_is_shorter_than_its_exterior_list(db):
    """The starkest instance of that asymmetry: six exteriors, two interiors."""
    g700 = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-G700"))
    exterior = {
        r.colour
        for r in db.scalars(select(Stock)).all()
        if r.variant_id in {v.id for v in db.scalars(
            select(VehicleVariant).where(VehicleVariant.model_id == g700.id)
        ).all()}
    }
    assert len(exterior) == 6
    assert len(g700.interior_colours) == 2


# --------------------------------------------------------------------------- #
# The fixture itself
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["vehicle_models.json", "stock.json",
                                  "finance_banks.json", "accessories.json"])
def test_each_fixture_parses_and_is_non_empty(name):
    assert _fixture(name)


def test_the_arabic_bullets_stay_index_aligned_with_the_english():
    """Corrections were applied to both lists in lockstep. A model whose Arabic
    drifted out of step would speak a different car in an Arabic conversation."""
    for m in _fixture("vehicle_models.json"):
        assert len(m["headline_features"]) == len(m["headline_features_ar"]), m["name"]


def test_no_bullet_still_quotes_a_fuel_economy_figure():
    """Economy is a COLUMN on the variant now. Two marketing bullets used to
    quote it and BOTH were wrong against Elite's own dataset — the X70 FL's
    "13.5 km/L" (the sheet says 13.7) and the X50's "17.9 km/L — most efficient
    in the UAE range" (the sheet says 12.0-14.0, and the most efficient cars in
    the range are the two i-DM hybrids). A second source is how they drifted."""
    for m in _fixture("vehicle_models.json"):
        for bullet in m["headline_features"]:
            assert "km/L" not in bullet, f"{m['name']}: {bullet}"


# --------------------------------------------------------------------------- #
# 5. the exterior palette the converter used to drop (2026-09-01)
# --------------------------------------------------------------------------- #
def test_every_model_ships_an_exterior_palette(db):
    """parse_models collected the palette from the Models sheet since day one
    and the emitter never read it back out, so it shipped nowhere — customers
    asking "what colours does it come in" were told we do not hold that while
    the workbook listed six. The seeder now fails outright on a model without
    one; this pins the fixture so a re-import cannot regress it silently."""
    for r in _fixture("vehicle_models.json"):
        assert r.get("exterior_colours"), r["model_code"]
    for m in db.scalars(select(VehicleModel)).all():
        assert m.exterior_colours, m.model_code


def test_the_g700_palette_is_the_workbook_s_in_the_workbook_s_order(db):
    """One pinned sample, order included: the sheet's order is Elite's, exactly
    as it is for trims."""
    m = db.scalar(select(VehicleModel).where(VehicleModel.model_code == "JET-G700"))
    assert m.exterior_colours == [
        "Night Black", "White", "Highway Grey", "Sand", "Dark Blue", "Matte Orange",
    ]
