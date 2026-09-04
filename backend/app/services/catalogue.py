"""Catalogue service: vehicle discovery, model details, showroom lookup.

All business logic and DB access for the catalogue lives here (AD-I). Tools and
REST routes are thin wrappers over these functions.

Two rules run through this whole module:

1. Facts come from the data, never from phrasing. Every value returned here is
   read from the DB; nothing is inferred, rounded, or invented (FR-CX-07).
2. Null price is a MEANINGFUL state, not missing data. We never coerce it to 0,
   never render "None", and never let it masquerade as "under budget".
   THE EXAMPLE MOVED AT T0.5: this used to name JET-T1-IDM, which had no
   published price and now costs AED 144,000. Every one of the nine MODELS is
   priced today; the unpriced car is a VARIANT — the G700 Top Fire, coming soon
   — so the guards live on `_ranked_for`'s bases and `_apply_filters`' budget
   filter, and tests/test_unpriced_vehicle.py is what holds them there"
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import RETRIEVAL_ENABLED
from app.models import FuelType, Showroom, VehicleModel, VehicleVariant

log = logging.getLogger("app.services.catalogue")

# --------------------------------------------------------------------------- #
# Input normalisation
# --------------------------------------------------------------------------- #
# The `emirate` column holds slugs ("abu_dhabi"); customers type "Abu Dhabi".
# Cities are NOT emirates: Jetour Al Ain sits in emirate=abu_dhabi, so a
# customer asking "anything in Al Ain?" must not be filtered into an empty
# answer by a naive equality check. Arabic keys are here because the customer
# may type the city natively mid-sentence (FR-CX-03, LOC-01).
#
# Each alias resolves to (city_slug | None, emirate_slug). Widening a city to its
# emirate is correct — Rabdan is a real, if distant, alternative for someone in
# Al Ain — but the CALLER must be able to tell which result actually matched the
# place they named. That is what the city half of this tuple, and the `city`
# column it compares against, exist for: without them the only "Al Ain" in the
# database sits inside the showroom's NAME, and ranking would mean parsing
# display text. See list_showrooms' match_type.
#
# city_slug is None for a whole-emirate word ("Dubai"): the customer named no
# locality, so no result can claim to be a city match. Granularity is "the place
# a customer names" — district-level in Dubai/Abu Dhabi (Deira, Al Quoz,
# Rabdan), city-level elsewhere (Al Ain, Ajman) — matching Showroom.city.
PLACES: dict[str, tuple[str | None, str]] = {
    # Emirate-level words: no locality named.
    "dubai": (None, "dubai"),
    "دبي": (None, "dubai"),
    "abu_dhabi": (None, "abu_dhabi"),
    "abudhabi": (None, "abu_dhabi"),
    "أبوظبي": (None, "abu_dhabi"),
    "ابوظبي": (None, "abu_dhabi"),
    # Localities that resolve to an emirate rather than being one.
    "al_ain": ("al_ain", "abu_dhabi"),
    "alain": ("al_ain", "abu_dhabi"),
    "العين": ("al_ain", "abu_dhabi"),
    "rabdan": ("rabdan", "abu_dhabi"),
    "deira": ("deira", "dubai"),
    "ديرة": ("deira", "dubai"),
    "al_quoz": ("al_quoz", "dubai"),
    "alquoz": ("al_quoz", "dubai"),
    "القوز": ("al_quoz", "dubai"),
    # Ajman is both a city and an emirate — it is legitimately both halves.
    "ajman": ("ajman", "ajman"),
    "عجمان": ("ajman", "ajman"),
}

# Relaxation order for nearest_alternatives (FR-BK-10). min_seats and brand are
# deliberately absent: a 5-seater is not an "alternative" for someone who needs
# 7, and a Jetour is not an alternative for someone asking for a Toyota. In both
# cases a near-miss is a wrong answer, so the honest reply is "we don't have it".
RELAX_ORDER: tuple[str, ...] = ("max_price_aed", "body_type", "fuel")

MAX_RECOMMENDATIONS = 3  # FR-BK-09: at most 3 recommendations.

# Bases for the spread picker (_pick_spread). ENUMS, NOT DISPLAY TEXT —
# "lowest_price", never "Lowest price". The agent phrases each one per language
# at speaking time; a stored English label could not be spoken in an Arabic
# thread (LOC-01), which is the display-text-in-a-data-field mistake this schema
# has already paid for four times.
#
# Order is the order the cards are offered in, and it mirrors the axes of the
# narrowing question the agent closes on — space, running costs, budget — so
# every car she names is answerable by the question she asks next, instead of
# the recommendation and the question being unrelated moves.
#
# `flagship` is a FALLBACK, not a fourth offer. A filtered set often cannot fill
# three (ask for petrol and no PHEV exists to be the efficiency pick); without a
# spare basis the spread would silently fall back to row order — the exact bug
# it exists to fix. First three that RESOLVE win.
SPREAD_BASES: tuple[str, ...] = ("most_seats", "efficiency", "lowest_price", "flagship")


def resolve_place(value: str) -> tuple[str | None, str] | None:
    """Map free-text place input to (city_slug | None, emirate_slug).

    Returns None when the input matches nothing we cover — the caller then
    returns an empty list so the agent can honestly say "nothing there",
    rather than raising.
    """
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace(" ", "_").replace("-", "_")
    return PLACES.get(key)


def normalise_emirate(value: str) -> str | None:
    """Map free-text emirate/city input to an `emirate` column value."""
    place = resolve_place(value)
    return place[1] if place else None


def _coerce_int(value: object) -> int | None:
    """Best-effort int coercion. Claude may send "7" or 7.0 despite the schema."""
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly.
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def _stocking_showroom_ids(db: Session, vehicle_id) -> set | None:
    """Which showrooms actually stock this model, per VehicleModel.showroom_ids.

    Returns None when the vehicle cannot be resolved, which callers treat as
    "do not filter" — a lookup failure must never silently hide every branch.
    """
    vid = _coerce_int(vehicle_id)
    v = db.get(VehicleModel, vid) if vid is not None else None
    if v is None:
        return None
    return {int(x) for x in (v.showroom_ids or [])}


def _fleet_filter_details(db: Session, payload: dict) -> dict:
    """Apply fleet placement to a Salesforce read-through result.

    FLEET PLACEMENT LIVES IN THE APP DATABASE, NOT IN SALESFORCE. The Salesforce
    mirror has no vehicle-to-showroom relationship at all, so its read-through
    builds `available_showrooms` from EVERY showroom (see salesforce._veh_dict).
    Left alone that advertises a model at branches which do not stock it — the
    exact failure this filter exists to prevent, and one the local path has
    always guarded against via showroom_ids.

    Filtering here rather than in salesforce.py keeps that module free of a DB
    session. Fail-open by construction: an unresolvable vehicle is returned
    untouched.
    """
    if not isinstance(payload, dict):
        return payload
    for veh in ([payload.get("vehicle")] if payload.get("vehicle") else []) + list(
        payload.get("candidates") or []
    ):
        if not isinstance(veh, dict) or not isinstance(veh.get("available_showrooms"), list):
            continue
        allowed = _stocking_showroom_ids(db, veh.get("vehicle_id"))
        if allowed is None:
            continue
        veh["available_showrooms"] = [
            s for s in veh["available_showrooms"] if s.get("showroom_id") in allowed
        ]
    return payload


# --------------------------------------------------------------------------- #
# Variants (T0.9)
# --------------------------------------------------------------------------- #
# THE MODEL CARD IS STILL THE UNIT OF DISCOVERY. A customer asking "what SUVs do
# you have" wants nine cars, not fifteen trims, so search() returns models with a
# price RANGE exactly as before and _pick_spread is untouched.
#
# WHAT VARIANT-AWARENESS FIXES IS A RANGE THAT LIES BY OMISSION. The G700 spans
# AED 169,000-240,450 and seats 5-6, so it matches a 180,000 budget and it
# matches "I need six seats" — but only its Comfort fits the first and only its
# Luxury and Flagship fit the second, and NEITHER FITS BOTH. Before this, the
# agent was told the model matched and had no way to know which trim did; the
# customer found out at the showroom.
#
# So a constrained search now says which trims actually satisfy the constraint,
# and the card keeps its honest model-level range beside it.


def _variants_by_model(db: Session, model_ids: list[int]) -> dict[int, list]:
    """All variants for these models, in one query, grouped by model.

    ONE QUERY, not one per card: search returns up to three matches and up to
    three alternatives, and a per-card lookup would be six round trips for a
    fifteen-row table.
    """
    if not model_ids:
        return {}
    rows = db.scalars(
        select(VehicleVariant)
        .where(VehicleVariant.model_id.in_(model_ids))
        .order_by(VehicleVariant.model_id, VehicleVariant.id)
    ).all()
    out: dict[int, list] = {}
    for r in rows:
        out.setdefault(r.model_id, []).append(r)
    return out


def _variant_brief(v: VehicleVariant) -> dict:
    """The compact form that rides on a search match.

    NO on-road price and no insurance breakdown: those belong to Finance, which
    has its own tool, and putting them here would grow every search result with
    numbers the agent must not quote as a total anyway.
    """
    return {
        "variant_id": v.id,
        "name": v.name,
        "price_aed": v.price_aed,
        "seats_min": v.seats_min,
        "seats_max": v.seats_max,
        "coming_soon": bool(v.coming_soon),
    }


def _variant_full(v: VehicleVariant) -> dict:
    """Everything known about one trim, for get_variants and get_details.

    on_road_aed comes from the model property, which returns None unless price,
    insurance and registration are ALL present — a partial sum would land below
    the list price and read as a discount.
    """
    return {
        **_variant_brief(v),
        "insurance_aed": v.insurance_aed,
        "registration_aed": v.registration_aed,
        "on_road_aed": v.on_road_aed,
        # Elite's authoritative column is km/L. A RANGE, not a value: most
        # figures are quoted as one and collapsing them would over- or
        # understate. Null where nothing is published — never zero.
        "km_per_l_min": v.km_per_l_min,
        "km_per_l_max": v.km_per_l_max,
        # Null for almost every trim — see _variant_image_url for why it does
        # not fall back to the model's photo. On the FULL form only: the search
        # path serialises _variant_brief for up to six cards and this would be
        # six more strings the agent never speaks.
        "image_url": _variant_image_url(v),
    }


def _variant_image_url(v: VehicleVariant) -> str | None:
    """Hero photo of THIS TRIM when Elite shot it separately, else None.

    DELIBERATELY DOES NOT FALL BACK to the model's photo, unlike everywhere else
    a missing image is filled in. This value is rendered one row per trim: the
    G700's four trims falling back would draw the identical car four times and
    imply a difference between them that the photograph does not show. None means
    "not shot separately" — the ordinary case, thirteen of fifteen trims — and
    the row renders as text, which is what it did before this column existed.

    Only the Dashing pair is non-null today (the 1.5 in Dark Green, the 1.6 in
    Light Blue). And it says which car was photographed, never which colour can
    be had: that is Stock's question, as it is for VehicleModel.exterior_colours.
    """
    return _image_url(v.images)


def _variants_satisfying(
    variants: list, *, min_seats: int | None, max_price_aed: int | None
) -> list:
    """The trims that actually meet the constraints the customer gave.

    Only price and seats discriminate WITHIN a model — body type, fuel and brand
    are model-level and are the same for every trim, so filtering on them here
    would return either all of them or none and say nothing.

    A COMING-SOON TRIM NEVER SATISFIES A BUDGET, for the same reason a null price
    never does: we cannot promise a car fits a budget when we do not know what it
    costs. It CAN satisfy a seat constraint if its seats are published.
    """
    out = []
    for v in variants:
        if max_price_aed is not None:
            if v.price_aed is None or v.price_aed > max_price_aed:
                continue
        if min_seats is not None:
            # seats_max, matching _apply_filters: a 5-7 trim satisfies "I need 7".
            if v.seats_max is None or v.seats_max < min_seats:
                continue
        out.append(v)
    return out


def _satisfying_block(
    variants: list, *, min_seats: int | None = None, max_price_aed: int | None = None
) -> dict:
    """The `matching_variants` key, or nothing at all.

    OMITTED ENTIRELY WHEN NO CONSTRAINT DISCRIMINATES — an unfiltered "what SUVs
    do you have" pays not one token for it, and the search result keeps exactly
    the shape it had before T0.9.

    Present when the customer named a budget or a seat count, because that is
    when the model-level range stops being the whole truth. Compare its length
    against `variant_count` on the same card: fewer means some trims do NOT fit,
    and the agent should say so rather than let "the G700 fits" stand.
    """
    if min_seats is None and max_price_aed is None:
        return {}
    return {
        "matching_variants": [
            _variant_brief(x)
            for x in _variants_satisfying(
                variants, min_seats=min_seats, max_price_aed=max_price_aed
            )
        ]
    }


def _showroom_brief(s: Showroom) -> dict:
    return {"showroom_id": s.id, "name": s.name, "emirate": s.emirate}


def _image_url(paths: list | None) -> str | None:
    """Grounded HTTP URL for a model's image (rule 18, FR-CX-07) — derived from
    the stored path, never invented. Seed paths are 'seed/media/vehicles/<file>';
    the backend serves seed/media at /media (see main.py), so the URL is
    '/media/vehicles/<file>'. Returns None when there is no image — the card must
    render cleanly without one, never with a fabricated URL.
    """
    for p in paths or []:
        # Interior shots (the '-interior-' filename convention) never become
        # the hero image, whatever their position in the list.
        if isinstance(p, str) and p.startswith("seed/media/") and "-interior-" not in p:
            return "/" + p[len("seed/") :]  # seed/media/vehicles/x.png -> /media/vehicles/x.png
    return None


def _interior_image_urls(paths: list | None) -> list[str]:
    """Grounded URLs for a model's interior photography (rule 18, FR-CX-07) —
    the stored paths whose filename carries the '-interior-' marker, cropped
    from the Jetour brochures. Empty when the brochure had no usable interior
    photography (T1, T2 i-DM, X50): the caller renders nothing and the agent
    must describe the cabin in words rather than promise photos.
    """
    return [
        "/" + p[len("seed/") :]
        for p in paths or []
        if isinstance(p, str) and p.startswith("seed/media/") and "-interior-" in p
    ]


def _vehicle_dict(
    db: Session,
    v: VehicleModel,
    *,
    full: bool = False,
    variants: list | None = None,
) -> dict:
    """Serialise a vehicle. price_aed_min/max stay None when not published —
    the caller must never render that as "None" to a customer (FR-CX-07).

    STILL A MODEL CARD (T0.9). Two variant-DERIVED facts were added and no trim
    list was: `variant_count`, so the agent knows there is a choice to ask about
    rather than presenting a range as a single car, and the km/L band, because
    "my commute is 50km a day" is a running-costs question the catalogue could
    not previously answer at all.

    `variants` is the pre-fetched list for this model, passed by callers that
    serialise several cards so fifteen rows are not fetched six times.
    """
    showrooms = db.scalars(
        select(Showroom).where(Showroom.id.in_(v.showroom_ids or [])).order_by(Showroom.id)
    ).all()
    if variants is None:
        variants = _variants_by_model(db, [v.id]).get(v.id, [])
    economy = [
        (x.km_per_l_min, x.km_per_l_max)
        for x in variants
        if x.km_per_l_min is not None and x.km_per_l_max is not None
    ]
    out = {
        "vehicle_id": v.id,
        "code": v.model_code,
        "name": v.name,
        "brand": v.brand,
        "body_type": v.body_type,
        "seats_min": v.seats_min,
        "seats_max": v.seats_max,
        "fuel": v.fuel_type.value if v.fuel_type else None,
        "price_aed_min": v.price_aed_min,
        "price_aed_max": v.price_aed_max,
        # Explicit so the agent never has to infer "None means unpublished".
        # ONE grounded image URL per model (rule 18). On the base dict, not gated
        # by `full`, so the ambiguous `candidates` shape carries it too and the UI
        # can render a card per returned variant. None when there is no image.
        "image_url": _image_url(v.images),
        "headline_features": list(v.headline_features or []),
        "available_showrooms": [_showroom_brief(s) for s in showrooms],
        # The count, not the list. "The G700 comes in three trims" is what stops
        # a price RANGE being read out as one car's price; which three is a
        # follow-up question with its own tool (get_variants).
        "variant_count": len(variants),
        # Band across the trims that publish a figure. None when none does —
        # the T1 i-DM's economy is known but the G700 Top Fire's is not, and a
        # missing figure must never render as 0 km/L.
        "km_per_l_min": min(e[0] for e in economy) if economy else None,
        "km_per_l_max": max(e[1] for e in economy) if economy else None,
    }
    if full:
        out["data_provenance"] = v.data_provenance
        out["images"] = list(v.images or [])
        out["interior_colours"] = list(v.interior_colours or [])
        # The palette Elite MAKES the model in — availability stays a stock
        # question (see VehicleModel.exterior_colours).
        out["exterior_colours"] = list(v.exterior_colours or [])
        # Interior photos, so "what's it like inside?" can be SHOWN. On the
        # full dict only — search cards stay one hero image each. Empty list,
        # never absent: "no photos" is a fact the agent must not invent around.
        out["interior_image_urls"] = _interior_image_urls(v.images)
        # THE FULL PATH IS ONE NAMED MODEL, so the trims come with it rather
        # than costing a second round trip. Deliberately NOT on the base dict,
        # which serialises up to six cards per search.
        out["variants"] = [_variant_full(x) for x in variants]
    return out


def hero_image_urls(db: Session) -> list[str]:
    """Every model's hero photo, for the browser to warm its cache with.

    WHY THE CLIENT IS TOLD THIS RATHER THAN GUESSING IT. A card's photo is
    needed at the exact instant the card appears — mid-answer, when the customer
    is already reading — and fetching it then is the one moment it is most
    expensive. The whole set is ~90KB of AVIF across nine cars, so the browser
    can hold all of it before a single card is drawn and every card after that
    paints from cache.

    STILL GROUNDED (rule 18). These are the same paths _image_url derives from
    the same column; nothing here is a URL the frontend made up, which is
    exactly why this is an endpoint and not a list hardcoded in the client
    (AD-I: the backend owns the bytes AND the paths to them).

    HERO ONLY — not interiors, and not the @full renditions. Interiors are shown
    on demand and the dialog is a deliberate tap; preloading either would spend a
    customer's data on photographs most conversations never open.
    """
    rows = db.scalars(select(VehicleModel).order_by(VehicleModel.id)).all()
    return [u for u in (_image_url(v.images) for v in rows) if u]


def detail_for_dialog(db: Session, vehicle_id: int) -> dict | None:
    """Full detail for the UI's vehicle dialog. None when the id is unknown.

    THE UI PATH, NOT THE AGENT PATH. Deliberately separate from _vehicle_dict:

      - it carries headline_features_ar, which the tool result must NOT (adding
        it there would grow every search_vehicles payload ~29% in tokens the
        model never speaks);
      - it drops `code`, `brand`, `data_provenance` and
        `images` — an id the UI does not show, a single-brand constant, a flag
        derivable from the two price fields, an English citation blob and a raw
        path list already reduced to image_url;
      - it builds its own showroom dicts rather than calling _showroom_brief,
        because that helper feeds the tool payload and widening it would move
        the agent's token cost. `city` is here for the dialog's "available at"
        line and is not wanted in the agent's context.

    ENUMS AND NUMBERS, NOT DISPLAY TEXT. fuel comes back as "petrol"/"phev" and
    seats as a raw range; the UI maps them to labels per language. A stored
    English string could not be shown in an Arabic dialog (LOC-01), which is the
    same rule that put the Arabic bullets in the data in the first place.

    Both languages are always returned. It costs nothing here — no model is
    involved — and it means the caller need not know the conversation's language
    at fetch time.
    """
    # Read-through: prefer Salesforce for the dialog detail, fail-open to local.
    try:
        from app.services import salesforce as _sf
        if _sf.enabled():
            _r = _sf.detail_for_dialog_sf(vehicle_id)
            if _r is not None:
                return _r
    except Exception:  # noqa: BLE001
        pass

    v = db.get(VehicleModel, vehicle_id)
    if v is None:
        return None

    showrooms = db.scalars(
        select(Showroom).where(Showroom.id.in_(v.showroom_ids or [])).order_by(Showroom.id)
    ).all()
    variants = _variants_by_model(db, [v.id]).get(v.id, [])
    economy = [
        (x.km_per_l_min, x.km_per_l_max)
        for x in variants
        if x.km_per_l_min is not None and x.km_per_l_max is not None
    ]
    return {
        "vehicle_id": v.id,
        "name": v.name,
        "body_type": v.body_type,
        "seats_min": v.seats_min,
        "seats_max": v.seats_max,
        # None stays None: an unpublished price renders as NO price line, never
        # "None" and never a fabricated figure (FR-CX-07).
        "price_aed_min": v.price_aed_min,
        "price_aed_max": v.price_aed_max,
        "fuel": v.fuel_type.value if v.fuel_type else None,
        "image_url": _image_url(v.images),
        "interior_image_urls": _interior_image_urls(v.images),
        "headline_features": list(v.headline_features or []),
        "headline_features_ar": list(v.headline_features_ar or []),
        # Vehicle facts the dialog shows instead of the showroom list (UI
        # decision 2026-08-29): the trims with their list prices, the km/L band,
        # and Elite's interior palette. Same sourcing rules as the tool payload
        # — a trim with no published price carries null and renders as "coming
        # soon", never a number.
        "interior_colours": list(v.interior_colours or []),
        "exterior_colours": list(v.exterior_colours or []),
        "km_per_l_min": min(e[0] for e in economy) if economy else None,
        "km_per_l_max": max(e[1] for e in economy) if economy else None,
        "variants": [
            {
                **_variant_brief(x),
                "km_per_l_min": x.km_per_l_min,
                "km_per_l_max": x.km_per_l_max,
                # Null for every trim Elite did not shoot separately, which is
                # most of them — the dialog draws a thumbnail only where there
                # is a genuinely different car to show (_variant_image_url).
                "image_url": _variant_image_url(x),
            }
            for x in variants
        ],
        "showrooms": [
            {"showroom_id": s.id, "name": s.name, "emirate": s.emirate, "city": s.city}
            for s in showrooms
        ],
    }


# --------------------------------------------------------------------------- #
# search — needs-based discovery (FR-BK-09/10)
# --------------------------------------------------------------------------- #
def _apply_filters(
    rows: list[VehicleModel],
    *,
    body_type: str | None,
    min_seats: int | None,
    max_price_aed: int | None,
    fuel: str | None,
    brand: str | None,
    skip: tuple[str, ...] = (),
) -> list[VehicleModel]:
    out = []
    for v in rows:
        if brand and "brand" not in skip:
            if (v.brand or "").strip().lower() != brand:
                continue
        if body_type and "body_type" not in skip:
            # Substring, case-insensitive: customers say "SUV", the catalogue
            # says "Mid-size family SUV". Deliberately loose.
            if body_type not in (v.body_type or "").lower():
                continue
        if min_seats is not None and "min_seats" not in skip:
            # seats_max, NOT seats_min: the X70 FL (5-7) satisfies "I need 7".
            # (This example used to name the X70 Plus; the two were swapped in
            # the seed until T0.4 read the brochures. The Plus is 7-7.)
            if v.seats_max is None or v.seats_max < min_seats:
                continue
        if fuel and "fuel" not in skip:
            if not v.fuel_type or v.fuel_type.value != fuel:
                continue
        if max_price_aed is not None and "max_price_aed" not in skip:
            # NULL GUARD RESTORED (T0.7), and this one is a CRASH not a
            # mis-sort: `None > int` raises TypeError, so a coming-soon model
            # would take down every budget query the moment it was seeded.
            #
            # An unpriced car is EXCLUDED from a budget filter rather than
            # included — the old models.py note put it exactly right: it should
            # "vanish from every budget query". We cannot promise a car fits a
            # budget when we do not know what it costs.
            if v.price_aed_min is None or v.price_aed_min > max_price_aed:
                continue
        out.append(v)
    return out


def _ranked_for(rows: list[VehicleModel], basis: str) -> list[VehicleModel]:
    """Every model that can be ranked on one basis, best first. [] when the
    basis cannot be resolved from this candidate set (no PHEV in a
    petrol-filtered list; no published price anywhere) or when the basis is
    unknown. Never guesses — an unresolvable basis yields nothing, rather than
    an approximation dressed as an answer.

    Unpublished price is EXCLUDED from both price bases: we cannot call a car
    the cheapest when price_aed_min is null, nor the most expensive when
    price_aed_max is (FR-CX-07). Every tiebreak ends on id so the order is
    stable across runs — an answer that changed between two identical questions
    would read as indecision.

    THE WHOLE LIST, NOT THE WINNER, and that is the point of the split. The
    spread wants one car per basis; a superlative question wants the leader AND
    what sits behind it ("the G700 tops the range, then the T2"). Both come from
    THIS one sort, so the comparison that makes "cheapest" honest is the same
    comparison that makes "most expensive" honest, and neither can drift from
    the other.
    """
    if not rows:
        return []

    if basis == "most_seats":
        pool = [v for v in rows if v.seats_max is not None]
        # seats_min breaks a tie toward the car that is ALWAYS a seven-seater
        # over the one that optionally is.
        #
        # THE EXAMPLE THIS COMMENT USED TO GIVE WAS BACKWARDS, and the seed data
        # it described was wrong with it (T0.4). It read "the X70 FL (7-7) / X70
        # Plus (5-7)". The brochures say the opposite: the X70 FL's spec table is
        # `5/5+2` and the X70 FL PLUS's is `7`, with a removable second row. So
        # the Plus is the true seven-seater and the FL is the one that optionally
        # seats seven. Both were corrected in seed/vehicle_models.json at T0.5,
        # and tests/test_seed_variants.py now pins them: the FL is 5-7 and the
        # Plus is 7-7, so this tiebreak resolves to the PLUS. That is the whole
        # reason the reference query used to answer with the wrong car.
        key = lambda v: (-v.seats_max, -(v.seats_min or 0), v.id)  # noqa: E731
    elif basis == "efficiency":
        # Cheapest PHEV — with the price guard RESTORED (T0.7). price_aed_min
        # is nullable again now that a coming-soon model exists, and an unpriced
        # car with a null here would sort as FREE and take this basis.
        pool = [
            v for v in rows
            if v.fuel_type and v.fuel_type.value == "phev" and v.price_aed_min is not None
        ]
        key = lambda v: (v.price_aed_min, v.id)  # noqa: E731
    elif basis == "lowest_price":
        # GUARD RESTORED (T0.7). This is the basis the old models.py note named
        # explicitly: "restoring the column alone would let an unpriced car sort
        # as free in lowest_price". An unpriced car is not the cheapest car — it
        # is a car whose price we cannot state.
        pool = [v for v in rows if v.price_aed_min is not None]
        key = lambda v: (v.price_aed_min, v.id)  # noqa: E731
    elif basis == "flagship":
        # GUARD RESTORED (T0.7). Symmetrical to lowest_price: we cannot call a
        # car the most expensive when its ceiling is unpublished either.
        pool = [v for v in rows if v.price_aed_max is not None]
        key = lambda v: (-v.price_aed_max, v.id)  # noqa: E731
    else:  # unknown basis — never silently pick something
        return []

    return sorted(pool, key=key)


def _best_for(
    rows: list[VehicleModel], basis: str, used: set[int]
) -> VehicleModel | None:
    """The single best model for one basis, or None when it cannot be resolved.

    A thin wrapper over _ranked_for so the spread and the ranked answer can
    never disagree about which car is "best" on a given basis.
    """
    ranked = _ranked_for([v for v in rows if v.id not in used], basis)
    return ranked[0] if ranked else None


def _pick_spread(rows: list[VehicleModel]) -> list[tuple[VehicleModel, str | None]]:
    """Choose up to MAX_RECOMMENDATIONS models, each the best in a DISTINCT
    category, instead of taking the first three in seed order.

    Row order is an accident of the seed file. Taking the first three made
    search_vehicles({}) return the same G700/T2/T2 i-DM every time and
    fuel=petrol return the same T2/T1/Dashing — no cheapest car, no seven-seater
    — while the agent presented them as a considered pick. This makes the
    selection real and, via `basis`, explicable.

    Backfill carries basis=None: when fewer than three bases resolve we would
    rather return a car with no stated reason than invent a category for it.
    """
    picks: list[tuple[VehicleModel, str | None]] = []
    used: set[int] = set()

    for basis in SPREAD_BASES:
        if len(picks) >= MAX_RECOMMENDATIONS:
            break
        winner = _best_for(rows, basis, used)
        if winner is not None:
            picks.append((winner, basis))
            used.add(winner.id)

    for v in rows:  # seed order, only if the bases came up short
        if len(picks) >= MAX_RECOMMENDATIONS:
            break
        if v.id not in used:
            picks.append((v, None))
            used.add(v.id)

    return picks


def _digest(rows: list[VehicleModel]) -> list[dict]:
    """Names + features for the agent to reason over when a question is about
    something the five structured filters cannot express ("best sound system",
    "which charges fastest", "anything good for camping").

    NO PRICE, DELIBERATELY, AND NO SHOWROOMS. The digest exists to let the agent
    SELECT a model, not to describe one. Withholding price means it structurally
    cannot answer a price question from here and must call get_vehicle_details
    on whatever it picks — which re-grounds the claim (FR-CX-07) and fires the
    vehicle card (rule 18). Same principle as withholding a slot's lock TTL
    under rule 14: enforce by not supplying the fact, not by asking the prompt
    to resist it.
    """
    return [
        {
            "vehicle_id": v.id,
            "name": v.name,
            "headline_features": list(v.headline_features or []),
        }
        for v in rows
    ]


def search(
    db: Session,
    *,
    body_type: str | None = None,
    min_seats: int | None = None,
    max_price_aed: int | None = None,
    fuel: str | None = None,
    brand: str | None = None,
    feature_query: str | None = None,
    basis: str | None = None,
) -> dict:
    """Needs-based vehicle discovery (FR-BK-09/10).

    Returns matches, plus — when nothing matches — nearest_alternatives that
    relax ONE constraint at a time so the agent can explain the trade-off
    honestly instead of improvising (FR-BK-10). Never raises; invalid input is
    returned as data.

    feature_query is A SIGNAL, NOT A FILTER, and is deliberately never matched
    against anything here. It means "the customer asked about something the five
    structured filters cannot express", and its only effect is to return
    catalogue_digest so the agent can do the language judgement itself.

    Resist the urge to "finish" it by keyword-matching: the literal word "sound"
    appears NOWHERE in the catalogue — the features say "18-speaker Lexicon
    audio" and "12-channel Sony audio" — so substring matching answers "which
    car has the best sound system?" with nothing, and a synonym list is
    maintained forever and always one phrasing behind. Deciding that "sound"
    means "audio" is exactly the messy-speech-to-structure job the model is for;
    the digest hands it the text and keeps every FACT in the tool result.
    """
    if fuel is not None:
        fuel = str(fuel).strip().lower()
        if fuel == "":
            fuel = None
        elif fuel not in {f.value for f in FuelType}:
            # The catalogue has only petrol and PHEV. An EV/diesel request must
            # get an honest "we don't have that", not a silent empty list.
            return {
                "error": "unsupported_fuel",
                "requested": fuel,
                "available_fuel_types": sorted(f.value for f in FuelType),
                "matches": [],
                "no_exact_match": True,
                "nearest_alternatives": [],
            }

    min_seats = _coerce_int(min_seats)
    max_price_aed = _coerce_int(max_price_aed)
    if min_seats is not None and min_seats <= 0:
        min_seats = None
    if max_price_aed is not None and max_price_aed <= 0:
        max_price_aed = None
    body_type = body_type.strip().lower() if isinstance(body_type, str) and body_type.strip() else None
    brand = brand.strip().lower() if isinstance(brand, str) and brand.strip() else None

    rows = list(db.scalars(select(VehicleModel).order_by(VehicleModel.id)).all())
    criteria = dict(
        body_type=body_type,
        min_seats=min_seats,
        max_price_aed=max_price_aed,
        fuel=fuel,
        brand=brand,
    )
    matched = _apply_filters(rows, **criteria)

    # ADDITIVE VECTOR PASS (T1.6), OFF BY DEFAULT, AND ONLY WHEN THE SUBSTRING
    # FILTER FOUND NOTHING. `body_type` is free text matched as a substring, so
    # "family car", "off roader" and "something for camping" match zero rows and
    # the customer's phrasing is simply discarded. This rescues those.
    #
    # It deliberately does NOT fire when the substring already matched — measured
    # on this corpus, the embedding cannot beat lexical overlap there, and the
    # X50/"suv" miss is NOT fixed by it. See retrieval/catalogue_pass.py.
    #
    # IT MAY ONLY ADD, AND ONLY BY LOOSENING THE FREE-TEXT FIELD. Every numeric
    # constraint the customer stated is re-applied to each candidate, so a
    # budget is never widened by a similarity score. _pick_spread and
    # _ranked_for then run unchanged — the cap is still three.
    #
    # Gated on RETRIEVAL_ENABLED so "off" means byte-identical to pre-Phase-1
    # behaviour, not "computed and discarded".
    interpreted_loosely: str | None = None
    if RETRIEVAL_ENABLED and not matched and (body_type or feature_query):
        try:
            from app.retrieval import catalogue_pass

            extra = catalogue_pass.additional_candidates(
                rows,
                matched,
                body_type=body_type,
                feature_query=feature_query,
                # The SAME filter, minus only the free-text field being
                # loosened. Re-deriving it here would be a second definition of
                # "fits the budget" waiting to drift from the first.
                still_satisfies=lambda v: bool(
                    _apply_filters([v], **criteria, skip=("body_type", "brand"))
                ),
            )
            if extra:
                # HONEST ABOUT WHAT HAPPENED. The customer's words matched no
                # row literally, so the agent must not present these as having
                # met a stated criterion — it offers them as an interpretation
                # ("for a family I'd look at these") and can be corrected.
                interpreted_loosely = body_type or feature_query
                matched = list(extra)
        except Exception:  # noqa: BLE001 — retrieval must never break a search
            log.exception("catalogue vector pass skipped")

    # CAP AT THREE, SERVER-SIDE (FR-BK-09/10, rule 5). MAX_RECOMMENDATIONS was
    # previously applied only to nearest_alternatives, so the happy path — a
    # query that MATCHED — returned every hit, up to the whole nine-model
    # catalogue. The rule is "offer at most three recommendations", and per
    # CLAUDE.md business rules belong in the service layer, not the prompt:
    # handing the agent nine and trusting it to pick three is the same mistake
    # as expecting it to filter Friday prayer slots itself. get_available_slots
    # already caps its offers here rather than in the prompt; this matches it.
    #
    # NO `total_matches`, AND THAT IS A REVERSAL — read this before adding it back.
    # It used to travel with the result so the agent could say "nine fit, here
    # are three" and never let three sound like the whole catalogue. In front of
    # a customer that landed badly twice over: she volunteered inventory counts
    # unprompted ("we have 9 Jetour models in total... since we have 9 models
    # altogether"), and when pushed for the rest she explained the CAP as though
    # it were a system limit — "the catalogue only surfaces three at a time",
    # "there's no page 2". The cap is hospitality (FR-BK-09/10), not a ceiling
    # the software imposes, and narrating it as one is both a leak and untrue.
    #
    # Withheld rather than forbidden, per rule 14's lock-TTL corollary: a fact
    # the agent must never state is kept OUT of the tool result, not left in it
    # with the prompt asked to resist. She cannot leak a number she never got.
    # The honesty this once bought is now carried by the prompt instead — offer
    # to narrow, which needs no total to be truthful.
    #
    # WHICH three, when more than three fit, is now a real choice (_pick_spread):
    # one best-in-category per basis, rather than whichever rows the seed file
    # happens to list first. `basis` travels with each match so the agent can say
    # WHY that car is there — the prompt already asks for a reason per
    # recommendation and previously had nothing to build one from.
    #
    # At three or fewer nothing was withheld, so no selection judgement was made
    # and no `basis` is attached: labelling a two-item list would imply a
    # comparison that never happened.
    # A SUPERLATIVE IS A LOOKUP, NOT A RECOMMENDATION — `basis` is that route,
    # and its absence was a live grounding defect (FR-CX-07). SPREAD_BASES lists
    # four bases while MAX_RECOMMENDATIONS caps at three, so `flagship` — being
    # last — was ALWAYS the one dropped. Asked "what is the most expensive car?"
    # the agent received X70 FL / T2 i-DM / X50, never the G700, and with no
    # signal that anything had been withheld it named the priciest of those
    # three as "top-of-range". Every FACT it stated came from a tool; the
    # RANKING did not, because a one-per-category spread is not a ranking and
    # the tool description already says so. It ranked anyway — which is the
    # evidence that a warning alone does not hold, and why this is a route
    # rather than a sterner sentence.
    #
    # Reordering SPREAD_BASES would only move the casualty: something else
    # becomes permanently invisible and the agent still cannot tell. The gap was
    # that a determinate question had no way to be asked.
    #
    # THREE, NOT ONE, and that is deliberate. Returning only the winner answers
    # the question and ends the conversation; a showroom host names the leader
    # and keeps the range in view. It also keeps the card row honest — the
    # original complaint was not "too many cards", it was cards for cars she
    # never mentioned. Ranked, all three answer the question that was asked.
    #
    # Composes with the filters for free because _ranked_for runs on ALREADY
    # FILTERED rows: "the most expensive seven-seater" is basis=flagship with
    # min_seats=7, and needs no code of its own.
    if basis:
        offered = [(v, None) for v in _ranked_for(matched, basis)[:MAX_RECOMMENDATIONS]]
    elif len(matched) > MAX_RECOMMENDATIONS:
        offered = _pick_spread(matched)
    else:
        offered = [(v, None) for v in matched]

    # ONE QUERY FOR EVERY CARD'S TRIMS, not one per card.
    variants_by_model = _variants_by_model(db, [v.id for v, _ in offered])

    result: dict = {
        "matches": [
            {
                **_vehicle_dict(db, v, variants=variants_by_model.get(v.id, [])),
                **_satisfying_block(
                    variants_by_model.get(v.id, []),
                    min_seats=min_seats,
                    max_price_aed=max_price_aed,
                ),
                **({"basis": b} if b else {}),
            }
            for v, b in offered
        ],
        # KEYED ON `offered`, NOT `matched`. A basis that cannot resolve — the
        # most efficient car among petrol-only rows, a flagship where no price
        # is published — has candidates but no answer, and must fall through to
        # nearest_alternatives rather than return an empty match list dressed as
        # a success. On the spread path the two are identical: a non-empty
        # `matched` always yields a non-empty `offered` (the backfill loop sees
        # to it), so nothing about discovery moves.
        "no_exact_match": not offered,
        "nearest_alternatives": [],
    }
    if interpreted_loosely:
        # Read by the agent, not by the UI: these cars were chosen by MEANING,
        # not by a filter the customer's words actually satisfied. She should
        # offer them as her reading of the request, not as a match.
        result["interpreted_loosely"] = interpreted_loosely

    # RANKED_BY IS A RESULT-LEVEL KEY, NOT A PER-MATCH `basis`, and the two must
    # not be confused. Per-match `basis` means "why this car is here when others
    # also fitted" — a category label for a spread. In a ranked answer every car
    # is there for the same reason and what matters is the ORDER, so the measure
    # is stated once and the matches carry none. Reusing `basis` per match would
    # tell the agent these are one-per-category picks, which is precisely the
    # misreading that caused the defect. Omitted entirely on the spread path.
    if basis and offered:
        result["ranked_by"] = basis

    # Scoped to what actually matched, so "7-seater with good sound" digests two
    # cars rather than nine. Omitted entirely when there is no feature_query, so
    # the ordinary path pays nothing for it.
    if feature_query and matched:
        result["catalogue_digest"] = _digest(matched)

    # `offered`, matching no_exact_match above: an unresolvable basis has
    # matches but no answer, and relaxing a criterion is exactly the right
    # recovery ("no petrol car is a hybrid — the nearest is the T2 i-DM").
    if offered:
        return result

    # No exact match — relax one constraint at a time, in priority order.
    for relax in RELAX_ORDER:
        if criteria.get(relax) is None:
            continue  # not constraining anything; relaxing it changes nothing
        candidates = _apply_filters(rows, **criteria, skip=(relax,))
        if not candidates:
            continue
        if relax == "max_price_aed":
            # Cheapest first: "our 7-seater starts at AED 79,000".
            candidates.sort(key=lambda v: (v.price_aed_min is None, v.price_aed_min or 0))
        alt_variants = _variants_by_model(
            db, [v.id for v in candidates[:MAX_RECOMMENDATIONS]]
        )
        # The RELAXED criterion is excluded from the satisfying check: an
        # alternative is offered precisely because it fails that one, so
        # reporting "no trim satisfies your budget" beside it would be noise.
        kept = {k: v for k, v in dict(
            min_seats=min_seats, max_price_aed=max_price_aed
        ).items() if k != relax}
        result["nearest_alternatives"] = [
            {
                **_vehicle_dict(db, v, variants=alt_variants.get(v.id, [])),
                **_satisfying_block(alt_variants.get(v.id, []), **kept),
                "relaxed": [relax],
            }
            for v in candidates[:MAX_RECOMMENDATIONS]
        ]
        break

    return result


# --------------------------------------------------------------------------- #
# get_variants — the trims of one model (T0.9)
# --------------------------------------------------------------------------- #
def get_variants(db: Session, vehicle_id: int) -> dict:
    """Every trim of one model, priced and specified.

    WHY THIS IS ITS OWN CALL AND NOT PART OF DISCOVERY. search() answers "which
    car"; this answers "which one of that car". Folding it into every search
    result would put fifteen trims' worth of prices into a nine-model browse and
    invite the agent to compare trims before the customer has chosen a model.

    IT IS ALSO HOW SALES PRODUCES A `variant_id`. Finance needs one to quote an
    on-road price or an instalment; stock needs one to answer a colour question.
    The model id alone reaches neither.

    TRIMS ARE ORDERED BY PRICE, NOT BY NAME, and the T1 is why: its Luxury is the
    four-wheel-drive range-topper and its Flagship is specified close to Comfort,
    so alphabetical or seed order would present a ladder that does not exist.
    Unpriced trims sort last — the G700 Top Fire is coming soon, not cheapest.
    """
    v = db.get(VehicleModel, vehicle_id)
    if v is None:
        return {"error": "vehicle_not_found", "vehicle_id": vehicle_id}

    rows = _variants_by_model(db, [v.id]).get(v.id, [])
    rows = sorted(rows, key=lambda x: (x.price_aed is None, x.price_aed or 0, x.id))

    return {
        "vehicle_id": v.id,
        "name": v.name,
        "variants": [_variant_full(x) for x in rows],
        # Model-level and identical across trims, so it belongs here rather than
        # repeated fifteen times. NOT paired with an exterior colour and not
        # stocked — see VehicleModel.interior_colours before answering with it.
        "interior_colours": list(v.interior_colours or []),
        "exterior_colours": list(v.exterior_colours or []),
        # A trim named `null` means "the only version", not "unknown". Said
        # explicitly so the agent renders the model name alone rather than the
        # word None.
        "single_unnamed_variant": len(rows) == 1 and rows[0].name is None,
    }


# --------------------------------------------------------------------------- #
# get_details — one named model (FR-BK-04)
# --------------------------------------------------------------------------- #
def get_details(
    db: Session, *, name: str | None = None, code: str | None = None
) -> dict:
    """Full record for ONE model, by name or catalogue code.

    Model names in this catalogue are genuinely ambiguous: "T2" is both the
    Jetour T2 (petrol) and the T2 i-DM (PHEV) — different cars, different
    prices. An ambiguous query returns every candidate so the agent can ASK
    which one, because silently picking one could book a test drive for the
    wrong car and the customer would not find out until they arrived.

    Unknown models return an error rather than a fuzzy match: "do you have a
    Land Cruiser?" must get an honest no (FR-CX-07).
    """
    # Read-through: prefer Salesforce, fail-open to the local DB below. Returns the
    # identical dict shape, so vehicle cards (image/price/features) are unchanged.
    try:
        from app.services import salesforce as _sf
        if _sf.enabled():
            _r = _sf.get_details_sf(name=name, code=code)
            if _r is not None:
                return _fleet_filter_details(db, _r)
    except Exception:  # noqa: BLE001 — availability of SF must never break catalogue
        pass

    code = code.strip() if isinstance(code, str) and code.strip() else None
    name = name.strip() if isinstance(name, str) and name.strip() else None
    if not code and not name:
        return {"error": "missing_query", "detail": "Provide a model name or code."}

    # Exact code match wins outright — it is a natural key.
    if code:
        v = db.scalar(select(VehicleModel).where(VehicleModel.model_code == code.upper()))
        if v:
            return {"vehicle": _vehicle_dict(db, v, full=True)}

    if not name:
        return {"error": "vehicle_not_found", "query": code}

    needle = name.lower()
    rows = list(db.scalars(select(VehicleModel).order_by(VehicleModel.id)).all())

    # An exact full-name match is unambiguous even if it is a prefix of another
    # model ("Jetour X70 FL" vs "Jetour X70 FL Plus").
    #
    # THIS EXAMPLE USED TO BE "Jetour T2" vs "Jetour T2 i-DM" and stopped being
    # one at T0.5: Elite's workbook names that car "Jetour T2 Luxury Plus", so a
    # bare "Jetour T2" is now no model's exact name and correctly falls through
    # to the ambiguous branch below.
    exact = [v for v in rows if (v.name or "").lower() == needle]
    if len(exact) == 1:
        return {"vehicle": _vehicle_dict(db, exact[0], full=True)}

    hits = [v for v in rows if needle in (v.name or "").lower()]
    if not hits:
        # Try the bare code as a name fragment ("JET-T2" typed into `name`).
        hits = [v for v in rows if needle in (v.model_code or "").lower()]
    if not hits:
        return {"error": "vehicle_not_found", "query": name}
    if len(hits) > 1:
        return {
            "ambiguous": True,
            "query": name,
            "candidates": [_vehicle_dict(db, v) for v in hits],
        }
    return {"vehicle": _vehicle_dict(db, hits[0], full=True)}


# --------------------------------------------------------------------------- #
# list_showrooms
# --------------------------------------------------------------------------- #
def list_showrooms(
    db: Session, *, emirate: str | None = None, vehicle_id: int | None = None
) -> dict:
    """Showrooms, optionally filtered by emirate/city and by stocking vehicle.

    The vehicle filter reads the vehicle's own showroom_ids, so a model stocked
    at only some branches is never advertised at one where it is not present.
    NOTHING IN THE CURRENT SEED EXERCISES IT: the G700-dedicated Al Quoz branch
    (showroom 2) was removed on 2026-07-23, and every remaining model lists every
    remaining showroom. The filter stays because the constraint is real in the
    data model, not because a fixture happens to trip it — showroom_ids exists
    precisely so a future branch can stock a subset, and re-deriving that logic
    later would be worse than keeping a currently-inert filter.

    A place filter WIDENS to the emirate: "anything in Al Ain?" returns Rabdan
    too, because a 90-minute drive is a real alternative and hiding it would be
    the same dishonesty as inventing one. But widening without a match signal
    just moves the problem — the agent would have to work out which of two
    abu_dhabi rows is the Al Ain one, and the only "Al Ain" available to it
    would be the characters inside `name`. So every row carries `match_type`:

        "city"    — this showroom IS in the locality the customer named
        "emirate" — same emirate, different locality (a real alternative)

    and city matches sort first. Both come from comparing slug COLUMNS; nothing
    parses a display string. match_type is OMITTED entirely when no place was
    named: with nothing to match against, any value would be a claim we cannot
    support.
    """
    # Read-through: prefer Salesforce for showrooms, fail-open to local. Same shape
    # (brand, address, address_ar, map_link, hours_by_day, match_type) — no loss.
    try:
        from app.services import salesforce as _sf
        if _sf.enabled():
            _r = _sf.list_showrooms_sf(emirate=emirate, vehicle_id=vehicle_id)
            if _r is not None:
                if vehicle_id is not None and isinstance(_r.get("showrooms"), list):
                    allowed = _stocking_showroom_ids(db, vehicle_id)
                    if allowed is not None:
                        _r["showrooms"] = [
                            s for s in _r["showrooms"] if s.get("showroom_id") in allowed
                        ]
                return _r
    except Exception:  # noqa: BLE001
        pass

    rows = list(db.scalars(select(Showroom).order_by(Showroom.id)).all())

    if vehicle_id is not None:
        vid = _coerce_int(vehicle_id)
        v = db.get(VehicleModel, vid) if vid is not None else None
        if v is None:
            return {"error": "vehicle_not_found", "vehicle_id": vehicle_id, "showrooms": []}
        allowed = set(v.showroom_ids or [])
        rows = [s for s in rows if s.id in allowed]

    city_slug: str | None = None
    place_named = emirate is not None and str(emirate).strip() != ""
    if place_named:
        place = resolve_place(emirate)
        if place is None:
            # Unknown place — empty, not an error, so the agent says "nothing there".
            return {"showrooms": [], "unmatched_emirate": emirate}
        city_slug, emirate_slug = place
        rows = [s for s in rows if s.emirate == emirate_slug]
        # City matches first; stable by id within each group so the order is
        # reproducible rather than dependent on the DB's row order.
        if city_slug is not None:
            rows.sort(key=lambda s: (s.city != city_slug, s.id))

    out = []
    for s in rows:
        entry = {
            "showroom_id": s.id,
            "name": s.name,
            "brand": s.brand,
            "city": s.city,
            "emirate": s.emirate,
            "address": s.address,
            # BOTH SCRIPTS SHIP, unlike headline_features_ar (deliberately absent
            # from search_vehicles, where it would grow every payload for text the
            # model never speaks). An address is different: the agent DOES read it
            # out, and in an Arabic turn it must read the Arabic one. Six rows, one
            # line each — the token cost is real work, not dead weight.
            "address_ar": s.address_ar,
            "map_link": s.map_link,
            "phone": s.phone,
            # Structured hours; the agent phrases them in the customer's
            # language rather than reading a stored English string (LOC-01).
            "hours_by_day": s.hours_by_day,
        }
        if place_named:
            entry["match_type"] = (
                "city" if city_slug is not None and s.city == city_slug else "emirate"
            )
        out.append(entry)

    return {"showrooms": out}
