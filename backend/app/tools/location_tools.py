"""Location tool: nearest showroom + driving distance/time + a Google Maps
directions link. Thin wrapper (AD-H/AD-J): validate, call services in-process,
return JSON. Coordinates for the branches come from the local Showroom rows
(latitude/longitude, already stored for the console branch map); the customer-
facing name/address/hours are taken from the same catalogue read the rest of the
app uses (Salesforce-sourced, fail-open to local), so this stays consistent with
everything else Aisha says. Fail-open throughout."""

from __future__ import annotations

import urllib.parse

from sqlalchemy.orm import Session

from app.models import Showroom
from app.services import catalogue, maps, salesforce


def _dest_label(showroom, disp: dict) -> str | None:
    """Best DESTINATION text for a directions link: the branch's OWN Google business
    query (pulled from its map_link) so directions land on the exact same marker the
    map link shows — not a raw pin at approximate coordinates. Falls back to name +
    address, or None if nothing usable."""
    link = getattr(showroom, "map_link", None)
    if link:
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("query", [None])[0]
            if q and q.strip():
                return q.strip()
        except Exception:  # noqa: BLE001
            pass
    name = disp.get("name") or getattr(showroom, "name", None)
    addr = disp.get("address") or getattr(showroom, "address", None)
    parts = [p for p in (name, addr) if p]
    return ", ".join(parts) if parts else None

# --------------------------------------------------------------------------- #
# nearest_showroom
# --------------------------------------------------------------------------- #
NEAREST_SHOWROOM_SCHEMA = {
    "name": "nearest_showroom",
    "description": (
        "Find the Elite showroom CLOSEST to where the customer is, with the "
        "driving distance and time, and a Google Maps directions link. Use this "
        "when the customer asks which branch is nearest/closest to them, how far "
        "a showroom is, or for directions to a showroom: 'which showroom is "
        "closest to me?', 'how far is your nearest branch?', 'can you send me "
        "directions?'.\n\n"
        "PASS WHATEVER LOCATION THEY MENTION in `location` — a landmark, mall, "
        "metro station, district, neighbourhood, CITY or EMIRATE ('Mall of the "
        "Emirates', 'Dubai Marina', 'Deira', 'Dubai', 'Fujairah', 'Sharjah'). If "
        "the customer SHARED THEIR GPS LOCATION (a turn carrying coordinates), "
        "pass `latitude` and `longitude` instead — that is the most accurate.\n\n"
        "ALWAYS CALL THIS THE MOMENT THE CUSTOMER SAYS WHERE THEY ARE, even a bare "
        "city or emirate — do NOT guess a branch or its distance yourself, and do "
        "NOT comment on whether we have a showroom in their city. CRITICAL: even "
        "when you already know a city is too broad, you MUST still call this FIRST "
        "with that city — never ask for a landmark on your own without calling it, "
        "because calling it is what shows the customer a one-tap 'use my current "
        "location' button beside your question.\n\n"
        "Handling the result:\n"
        "- resolved=false with ask_for_landmark=true means the location was a bare "
        "city or emirate ('Dubai', 'Abu Dhabi', 'Sharjah', 'Fujairah'), or we "
        "couldn't place them precisely. Warmly ASK FOR A NEARBY LANDMARK, mall, "
        "metro station or area — e.g. 'Could you share a nearby landmark so I can "
        "find your closest showroom?' — then call again with it. Do NOT say we lack "
        "a branch in their city, do NOT name a branch, and do NOT list branches or "
        "distances yet.\n"
        "- resolved=true means they gave a specific place (or GPS): present the "
        "nearest branch(es) with distance + directions.\n"
        "Reading the result when resolved=true:\n"
        "- 'nearest' is the closest branch. 'distance_km' and, when available, "
        "'drive_minutes' are its distance from the customer; 'distance_mode' is "
        "'driving' (real road distance) or 'straight_line' (approximate). State "
        "the distance naturally in the customer's language ('about 8 km, roughly "
        "15 minutes by car'); never read a raw field name.\n"
        "- THE APP SHOWS THE CUSTOMER A CARD for the nearest branch — with its "
        "distance, drive time, a Get directions button AND a Select button. So in "
        "your reply DO NOT paste the directions link (directions_url) and DO NOT "
        "repeat the street address: the card already shows them, and pasting the "
        "link just duplicates the button. Keep your text to ONE short, warm "
        "sentence naming the nearest branch and roughly how far it is.\n"
        "- ASK ONLY ONE THING: whether they'd like that branch (they can tap "
        "Select or reply). Do NOT ask which day or time in the same message — wait "
        "until they have chosen the branch, then, in a NEW message, ask when they "
        "would like to come in.\n"
        "- 'alternatives' holds the SECOND-nearest branch; the app shows it as its "
        "own card too, so you do not need to describe it. The customer can pick "
        "either the nearest or the second option."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": (
                    "Where the customer is, as they said it — a landmark, mall, "
                    "metro station, district, neighbourhood, city or emirate "
                    "('Mall of the Emirates', 'Dubai Marina', 'Deira', 'Dubai', "
                    "'Fujairah'). A bare city/emirate is accepted but the tool "
                    "will return ask_for_landmark (it won't route to a branch "
                    "from a whole city). Omit if passing latitude/longitude."
                ),
            },
            "latitude": {
                "type": "number",
                "description": (
                    "Customer latitude, ONLY when they shared their GPS "
                    "location. Pass together with longitude."
                ),
            },
            "longitude": {
                "type": "number",
                "description": (
                    "Customer longitude, ONLY when they shared their GPS "
                    "location. Pass together with latitude."
                ),
            },
            "vehicle_id": {
                "type": "integer",
                "description": (
                    "ALWAYS pass this when a specific model is in play — the "
                    "customer named one, or you are finding a branch for a car "
                    "they are about to book. Only branches that actually STOCK "
                    "that model are then ranked, so 'the closest one' can never "
                    "be a branch that does not carry the car. Omit it only when "
                    "the customer is asking about branches generally, with no "
                    "model in mind."
                ),
            },
        },
        "required": [],
    },
}


def _resolve_origin(location, latitude, longitude):
    """Return (coords, label, url_origin, precise, emirate, error). Exactly one of
    coords or error is set. `precise` is False for a city/emirate-level match (ask
    for a landmark). `emirate` is the customer's emirate code (dubai, abu_dhabi,
    ajman, sharjah, …) used to decide which branch(es) to show, or None."""
    # 1) GPS coordinates win — most accurate. Reverse-geocode to the emirate so the
    #    same grouping rule applies as for a typed landmark.
    if latitude is not None and longitude is not None:
        try:
            coords = (float(latitude), float(longitude))
        except (TypeError, ValueError):
            coords = None
        if coords is not None:
            emirate = None
            try:
                emirate = maps.reverse_geocode_emirate(coords[0], coords[1])
            except Exception:  # noqa: BLE001
                emirate = None
            return coords, "your shared location", f"{coords[0]},{coords[1]}", True, emirate, None
    # 2) Free-text place -> geocode. A city/emirate is fine as far as coords go, but
    #    `precise` gates whether we ask for a landmark first.
    if location and str(location).strip():
        g = maps.geocode(str(location))
        if g is None:
            return None, None, None, False, None, {
                "resolved": False, "reason": "could_not_locate",
                "location": str(location).strip(), "ask_for_landmark": True,
            }
        coords = (g["lat"], g["lng"])
        return coords, (g.get("formatted") or str(location).strip()), \
            f"{coords[0]},{coords[1]}", bool(g.get("precise")), g.get("emirate"), None
    # 3) Nothing usable.
    return None, None, None, False, None, {
        "resolved": False, "reason": "no_location", "ask_for_landmark": True,
    }


def nearest_showroom(
    db: Session,
    session_id: str,
    location: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    vehicle_id: object = None,
    **_ignored: object,
) -> dict:
    coords, origin_label, url_origin, precise, cust_emirate, err = _resolve_origin(
        location, latitude, longitude)
    if err is not None:
        return err

    # Branch coordinates come from Salesforce (Showroom__c.Latitude__c/Longitude__c),
    # keyed by App Id, with the app's local coordinates as a fail-open fallback.
    sf_coords: dict = {}
    try:
        sf_coords = salesforce.showroom_coords()
    except Exception:  # noqa: BLE001
        sf_coords = {}

    def _coord_of(s: Showroom):
        c = sf_coords.get(s.id)
        if c:
            return c
        if s.latitude is not None and s.longitude is not None:
            return (float(s.latitude), float(s.longitude))
        return None

    candidates = db.query(Showroom).all()

    # FLEET PLACEMENT GATES THE RANKING. Without this, "which is closest?" answers
    # from geography alone and can hand the customer a branch that does not stock
    # the car they just asked about — the branch card is then an offer we cannot
    # honour. Placement lives on VehicleModel.showroom_ids (Salesforce carries no
    # vehicle-to-showroom relationship), so it is read from the app database.
    # Fail-open: an unresolvable vehicle leaves the ranking untouched.
    if vehicle_id is not None:
        from app.services.catalogue import _stocking_showroom_ids
        allowed = _stocking_showroom_ids(db, vehicle_id)
        if allowed is not None:
            candidates = [s for s in candidates if s.id in allowed]
            if not candidates:
                return {"resolved": False, "reason": "vehicle_not_stocked_anywhere",
                        "vehicle_id": vehicle_id}

    usable = [(s, _coord_of(s)) for s in candidates]
    usable = [(s, c) for s, c in usable if c is not None]
    if not usable:
        return {"resolved": False, "reason": "no_showroom_coordinates"}

    def _hav(sc):
        return maps.haversine_km(coords[0], coords[1], sc[1][0], sc[1][1])

    ranked = sorted(usable, key=_hav)  # nearest first, list of (showroom, (lat,lng))

    def _ask_landmark():
        return {"resolved": False, "reason": "need_landmark",
                "ask_for_landmark": True, "location": origin_label}

    # WHICH branch(es) to show — SIMPLE RULE (no emirate grouping):
    #   * a bare city or emirate on its own ('Dubai', 'Abu Dhabi') is not precise
    #     enough for an accurate distance, so ask for a nearby landmark first;
    #   * once we have a precise place (a landmark/area) or the customer's shared
    #     GPS, show the TWO NEAREST showrooms overall, ordered by distance —
    #     wherever they happen to be. This is predictable everywhere: the second
    #     card is always the genuine second-closest branch, never a far branch
    #     forced in just because it shares an emirate.
    if not precise:
        return _ask_landmark()
    display = ranked[:2]  # the 2 nearest branches overall, nearest first

    # Real driving distance/time for just the branches we'll show, then order by
    # drive time when we have it (else by straight-line, already sorted).
    dm = maps.drive_times(coords, [c for _, c in display])
    have_driving = any(x is not None for x in dm)
    order = sorted(
        range(len(display)),
        key=lambda i: (dm[i]["minutes"] if (have_driving and dm[i]) else _hav(display[i])),
    )
    display = [display[i] for i in order]
    dm = [dm[i] for i in order]

    # Customer-facing display fields from the normal catalogue read (SF-sourced,
    # fail-open to local), matched by showroom id.
    by_id: dict = {}
    try:
        listing = catalogue.list_showrooms(db)
        for s in (listing.get("showrooms") or []):
            by_id[s.get("showroom_id")] = s
    except Exception:  # noqa: BLE001
        by_id = {}

    def _entry(i: int, sc: tuple) -> dict:
        s, c = sc
        disp = by_id.get(s.id, {})
        d = dm[i] if i < len(dm) else None
        hav = maps.haversine_km(coords[0], coords[1], c[0], c[1])
        return {
            "showroom_id": s.id,
            "name": disp.get("name") or s.name,
            "city": disp.get("city") or s.city,
            "emirate": disp.get("emirate") or s.emirate,
            "address": disp.get("address") or s.address,
            "address_ar": disp.get("address_ar"),
            "phone": disp.get("phone"),
            "hours_by_day": disp.get("hours_by_day"),
            "distance_km": (d["km"] if d else hav),
            "drive_minutes": (d["minutes"] if d else None),
            "distance_mode": ("driving" if d else "straight_line"),
            "directions_url": maps.directions_url(url_origin, c[0], c[1], _dest_label(s, disp)),
            "map_link": s.map_link,
        }

    nearest = _entry(0, display[0])
    alternatives = [_entry(i, display[i]) for i in range(1, len(display))]
    return {
        "resolved": True,
        "origin": origin_label,
        "distance_mode": "driving" if have_driving else "straight_line",
        "coordinates_source": "salesforce" if sf_coords else "local",
        "nearest": nearest,
        "alternatives": alternatives,
    }
