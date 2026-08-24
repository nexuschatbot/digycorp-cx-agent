"""Google Maps helpers for the nearest-showroom feature — fail-open by design.

The feature works in three degrees, degrading gracefully:
  * With GOOGLE_MAPS_API_KEY set: geocode a spoken landmark -> coordinates, and
    get real DRIVING distance + time (Distance Matrix).
  * Without the key: geocode() returns None (caller asks for a landmark or uses
    the customer's shared GPS) and distances fall back to straight-line Haversine.
  * A Google Maps DIRECTIONS LINK never needs a key, so "Get directions" always
    works regardless.

Every function swallows errors and returns a neutral value, so a Maps outage or a
missing key can never break a conversation.

Config (env; also add each to docker-compose's backend allowlist to take effect):
    GOOGLE_MAPS_API_KEY          server-side key, restricted to the VM IP +
                                 Geocoding API + Distance Matrix API
    GOOGLE_MAPS_TIMEOUT_SECONDS  optional, default 4 (never hang a turn)
    GOOGLE_MAPS_REGION           optional, default "ae" (UAE geocode bias)
"""
from __future__ import annotations

import json
import logging
import math
import os
import urllib.parse
import urllib.request

_log = logging.getLogger("app")

_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
_TIMEOUT = float(os.getenv("GOOGLE_MAPS_TIMEOUT_SECONDS", "4"))
_REGION = os.getenv("GOOGLE_MAPS_REGION", "ae")

# Geocoder result types that mean "a whole city / emirate / country", i.e. too
# broad to route to a nearest branch — the caller should ask for a landmark.
_BROAD_TYPES = {"country", "administrative_area_level_1", "locality"}


def enabled() -> bool:
    """True when a Google Maps API key is configured (geocode + drive times)."""
    return bool(_KEY)


def _get_json(url: str):
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        _log.warning("maps: GET error %s", exc)
        return None


def geocode(text: str):
    """UAE-biased geocode of a free-text place/landmark.

    Returns {"lat","lng","formatted","precise"} or None. `precise` is False when
    the best match is only a city/emirate (e.g. "Dubai") — the caller should then
    ask the customer for a nearby landmark rather than guessing a branch.
    Returns None when there is no key, no input, or no result (fail-open)."""
    if not _KEY or not text or not text.strip():
        return None
    params = urllib.parse.urlencode({
        "address": text.strip(),
        "region": _REGION,
        "components": "country:AE",
        "key": _KEY,
    })
    j = _get_json("https://maps.googleapis.com/maps/api/geocode/json?" + params)
    if not j or j.get("status") != "OK" or not j.get("results"):
        if j and j.get("status") not in ("OK", "ZERO_RESULTS"):
            _log.warning("maps: geocode status=%s", j.get("status"))
        return None
    top = j["results"][0]
    loc = top.get("geometry", {}).get("location", {})
    if "lat" not in loc or "lng" not in loc:
        return None
    types = set(top.get("types", []))
    return {
        "lat": float(loc["lat"]),
        "lng": float(loc["lng"]),
        "formatted": top.get("formatted_address") or text.strip(),
        "precise": not bool(types & _BROAD_TYPES),
        "emirate": _emirate_from_components(top.get("address_components")),
    }


def _norm_emirate(name: str | None) -> str | None:
    """Normalise a Google emirate name to the app's code: 'Abu Dhabi Emirate' ->
    'abu_dhabi', 'Ras Al Khaimah' -> 'ras_al_khaimah', 'Dubai' -> 'dubai'."""
    if not name:
        return None
    n = name.strip().lower().replace("emirate", "").replace("-", " ")
    n = "_".join(n.split())
    return n or None


def _emirate_from_components(components) -> str | None:
    """Pull the emirate (administrative_area_level_1) from a geocode result's
    address_components, normalised to the app's emirate code, or None."""
    if not isinstance(components, list):
        return None
    for c in components:
        if isinstance(c, dict) and "administrative_area_level_1" in (c.get("types") or []):
            return _norm_emirate(c.get("long_name") or c.get("short_name"))
    return None


def reverse_geocode_emirate(lat: float, lng: float) -> str | None:
    """Reverse-geocode a GPS point to its emirate code (for the 'share my
    location' path). Fail-open: None on no key / no result / any error."""
    if not _KEY:
        return None
    params = urllib.parse.urlencode({
        "latlng": f"{lat},{lng}", "result_type": "administrative_area_level_1",
        "region": _REGION, "key": _KEY,
    })
    j = _get_json("https://maps.googleapis.com/maps/api/geocode/json?" + params)
    if not j or j.get("status") != "OK" or not j.get("results"):
        return None
    for r in j["results"]:
        em = _emirate_from_components(r.get("address_components"))
        if em:
            return em
    return None


def haversine_km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """Great-circle (straight-line) distance in km, rounded to 0.1."""
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dphi = math.radians(b_lat - a_lat)
    dl = math.radians(b_lng - a_lng)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(h)), 1)


def drive_times(origin: tuple[float, float], dests: list[tuple[float, float]]):
    """Driving distance/time from one origin to each destination.

    Returns a list aligned to `dests` of {"km","minutes","distance_text",
    "duration_text"} or None per entry. Fail-open: without a key (or on any
    error) every entry is None and the caller uses Haversine instead."""
    if not _KEY or not dests:
        return [None] * len(dests)
    o = f"{origin[0]},{origin[1]}"
    d = "|".join(f"{la},{ln}" for la, ln in dests)
    params = urllib.parse.urlencode({
        "origins": o, "destinations": d, "mode": "driving",
        "units": "metric", "region": _REGION, "key": _KEY,
    })
    j = _get_json("https://maps.googleapis.com/maps/api/distancematrix/json?" + params)
    out: list = [None] * len(dests)
    if not j or j.get("status") != "OK":
        if j:
            _log.warning("maps: matrix status=%s", j.get("status"))
        return out
    try:
        elements = j["rows"][0]["elements"]
        for i, e in enumerate(elements):
            if i < len(out) and e.get("status") == "OK":
                out[i] = {
                    "km": round(e["distance"]["value"] / 1000.0, 1),
                    "minutes": int(round(e["duration"]["value"] / 60.0)),
                    "distance_text": e["distance"].get("text"),
                    "duration_text": e["duration"].get("text"),
                }
    except Exception as exc:  # noqa: BLE001
        _log.warning("maps: matrix parse error %s", exc)
    return out


def directions_url(origin: str, dest_lat: float, dest_lng: float,
                   dest_label: str | None = None) -> str:
    """A Google Maps driving-directions deep link (no API key required).

    `origin` may be a "lat,lng" string or free text — Google resolves either.
    `dest_label`, when given (the branch's own Google business query, e.g.
    "Jetour Showroom Deira Dubai"), is used as the DESTINATION so Maps lands on the
    actual business marker — the same place the branch's map link points to —
    rather than dropping a raw pin at possibly-approximate stored coordinates. The
    coordinates remain the fallback when no label is available."""
    label = (dest_label or "").strip()
    destination = label if label else f"{dest_lat},{dest_lng}"
    params = urllib.parse.urlencode({
        "api": "1",
        "origin": origin,
        "destination": destination,
        "travelmode": "driving",
    })
    return "https://www.google.com/maps/dir/?" + params
