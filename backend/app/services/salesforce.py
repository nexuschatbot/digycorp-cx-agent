"""Salesforce integration (read path) — fail-open by construction.

The app keeps running on its own database exactly as before; this module lets the
read paths CONSULT Salesforce so Salesforce can drive what the customer sees
(e.g. slot availability). Every public function swallows all errors and returns a
neutral value, so a Salesforce outage can never break a booking flow — the app
simply falls back to its local data.

Config comes from env (named in docker-compose's backend allowlist):
    SF_ENABLED           "true" to turn the integration on (default off)
    SF_DOMAIN            https://<mydomain>.my.salesforce.com
    SF_CLIENT_ID         Connected/External Client App consumer key
    SF_CLIENT_SECRET     consumer secret
    SF_API_VERSION       optional, default v62.0
Auth is OAuth 2.0 Client Credentials; the token is cached in-process.

Performance (matters for the live VOICE CALL, where any mid-turn Salesforce
round-trip is audible dead air):
  1. Keep-alive  — one pooled HTTPS connection per host is reused across calls,
     so there is no fresh TLS handshake per request.
  2. Pre-warm    — prewarm() loads vehicles/showrooms + the booked-slot snapshot
     at startup, so the first call turn is never a cold fetch.
  3. Availability snapshot — a background thread refreshes the set of booked slot
     ids every few seconds; booked_slot_app_ids() reads that in-memory set (0ms)
     instead of a live query, so a call never blocks on Salesforce. Freshness is
     the refresh interval (SF_SNAPSHOT_SECONDS, default 3s). If the snapshot was
     never populated (background thread off / first tick not done), the gate
     falls back to a live query, and any failure fails open (gate is a no-op).
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_log = logging.getLogger("app")

_API = os.getenv("SF_API_VERSION", "v62.0")
_TIMEOUT = float(os.getenv("SF_TIMEOUT_SECONDS", "4"))  # never hang a turn

# cached token: (access_token, instance_url, expires_monotonic)
_tok: tuple[str, str, float] | None = None
_TOKEN_TTL = 1800.0  # refresh well within Salesforce's session lifetime


def enabled() -> bool:
    return os.getenv("SF_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"} \
        and bool(os.getenv("SF_DOMAIN")) and bool(os.getenv("SF_CLIENT_ID"))


# --------------------------------------------------------------------------- #
# HTTP with keep-alive: one pooled HTTPS connection per host, reused across
# calls (no per-request TLS handshake). A single lock serialises Salesforce HTTP
# — calls are infrequent (the availability gate now reads an in-memory snapshot),
# so contention is negligible and correctness is simple. A dropped/idle socket is
# transparently reconnected once.
# --------------------------------------------------------------------------- #
_conn_lock = threading.Lock()
_conns: dict[str, http.client.HTTPSConnection] = {}


def _get_conn(host: str) -> http.client.HTTPSConnection:
    c = _conns.get(host)
    if c is None:
        c = http.client.HTTPSConnection(host, timeout=_TIMEOUT)
        _conns[host] = c
    return c


def _drop_conn(host: str) -> None:
    c = _conns.pop(host, None)
    if c is not None:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def _http(method: str, url: str, *, token: str | None = None, data: bytes | None = None,
          ctype: str | None = None):
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    path = parts.path + (("?" + parts.query) if parts.query else "")
    headers = {"Connection": "keep-alive", "Accept-Encoding": "identity"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if ctype:
        headers["Content-Type"] = ctype
    with _conn_lock:
        last_exc: Exception | None = None
        for attempt in (1, 2):  # retry once on a stale keep-alive socket
            try:
                c = _get_conn(host)
                c.request(method, path, body=data, headers=headers)
                r = c.getresponse()
                body = r.read().decode()  # must drain so the socket can be reused
                return r.status, (json.loads(body) if body else {})
            except (http.client.HTTPException, OSError, ConnectionError) as exc:
                last_exc = exc
                _drop_conn(host)
        raise last_exc  # both attempts failed -> caller's try/except fails open


def _token(force: bool = False) -> tuple[str, str] | None:
    global _tok
    if _tok and not force and _tok[2] > time.monotonic():
        return _tok[0], _tok[1]
    domain = os.getenv("SF_DOMAIN", "").rstrip("/")
    form = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": os.getenv("SF_CLIENT_ID", ""),
        "client_secret": os.getenv("SF_CLIENT_SECRET", ""),
    }).encode()
    try:
        st, j = _http("POST", domain + "/services/oauth2/token", data=form,
                      ctype="application/x-www-form-urlencoded")
        if st == 200 and "access_token" in j:
            _tok = (j["access_token"], j["instance_url"], time.monotonic() + _TOKEN_TTL)
            return _tok[0], _tok[1]
        _log.warning("salesforce: token failed status=%s", st)
    except Exception as exc:  # noqa: BLE001
        _log.warning("salesforce: token error %s", exc)
    return None


def _soql(query: str, *, retry: bool = True):
    tok = _token()
    if not tok:
        return None
    at, iu = tok
    url = f"{iu}/services/data/{_API}/query/?q=" + urllib.parse.quote(query)
    try:
        st, j = _http("GET", url, token=at)
        if st == 200:
            return j.get("records", [])
        if st == 401 and retry:  # token expired mid-flight — refresh once
            _token(force=True)
            return _soql(query, retry=False)
        _log.warning("salesforce: soql status=%s", st)
    except Exception as exc:  # noqa: BLE001
        _log.warning("salesforce: soql error %s", exc)
    return None


# --------------------------------------------------------------------------- #
# Availability: in-memory snapshot of booked slot ids, refreshed by a background
# thread so the gate never blocks a turn (critical for the live voice call).
# --------------------------------------------------------------------------- #
# Default relaxed to 30s (was 10s): with SALESFORCE AS THE SOURCE OF TRUTH the
# availability gate consults this snapshot on every turn, so one refresh every 10s
# would burn ~8,640 Developer-org API calls/day just polling. 30s keeps it well
# under the daily cap while staying fresh enough for a demo (a just-booked slot is
# also written back to SF and re-read on the next tick).
_SNAPSHOT_SECONDS = float(os.getenv("SF_SNAPSHOT_SECONDS", "30"))
_snap_lock = threading.Lock()
_booked_snapshot: set[int] = set()
_available_snapshot: set[int] = set()
_snapshot_ready = False
_bg_started = False


def refresh_booked_snapshot() -> bool:
    """Pull slot availability from Salesforce into two in-memory sets in ONE query:
    the Available App_Ids (SALESFORCE IS THE SOURCE OF TRUTH for what may be offered
    or booked) and the not-Available ones. Returns True on success. Fail-safe: on
    any error BOTH previous snapshots are kept, so a transient SF blip serves the
    last-good availability rather than emptying the calendar."""
    global _booked_snapshot, _available_snapshot, _snapshot_ready
    recs = _soql("SELECT App_Id__c, Status__c FROM Slot__c")
    if recs is None:
        return False
    avail: set[int] = set()
    booked: set[int] = set()
    for r in recs:
        try:
            aid = int(r["App_Id__c"])
        except (KeyError, TypeError, ValueError):
            continue
        if (r.get("Status__c") or "") == "Available":
            avail.add(aid)
        else:
            booked.add(aid)
    with _snap_lock:
        _available_snapshot = avail
        _booked_snapshot = booked
        _snapshot_ready = True
    return True


def _booked_live(app_ids: list[int]) -> set[int]:
    """Live availability query (fallback when the snapshot isn't ready)."""
    ids = [int(i) for i in app_ids if i is not None]
    if not ids:
        return set()
    blocked: set[int] = set()
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        in_list = ",".join(f"'{n}'" for n in chunk)
        recs = _soql(
            f"SELECT App_Id__c FROM Slot__c WHERE App_Id__c IN ({in_list}) "
            f"AND Status__c != 'Available'"
        )
        if recs is None:
            return set()  # SF unreachable -> gate is a no-op
        for r in recs:
            try:
                blocked.add(int(r["App_Id__c"]))
            except (KeyError, TypeError, ValueError):
                pass
    return blocked


def booked_slot_app_ids(app_ids: list[int]) -> set[int]:
    """Given candidate app slot ids, return the subset Salesforce marks NOT
    available (Booked/Locked). Reads the in-memory snapshot (0ms, no round-trip)
    when it is ready; otherwise falls back to a live query. Fail-open: returns an
    empty set on any problem, so the caller filters nothing and the local DB
    result stands."""
    ids = [int(i) for i in app_ids if i is not None]
    if not ids:
        return set()
    with _snap_lock:
        ready = _snapshot_ready
        snap = _booked_snapshot
    if ready:
        return {n for n in ids if n in snap}
    return _booked_live(ids)


def _available_live(app_ids: list[int]) -> set[int] | None:
    """Live query (fallback when the snapshot isn't ready): the subset of app_ids
    Salesforce marks Available. Returns None if Salesforce is UNREACHABLE, so the
    availability gate can tell 'nothing available' (empty set) apart from 'SF down'
    (None) and degrade accordingly."""
    ids = [int(i) for i in app_ids if i is not None]
    if not ids:
        return set()
    avail: set[int] = set()
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        in_list = ",".join(f"'{n}'" for n in chunk)
        recs = _soql(
            f"SELECT App_Id__c FROM Slot__c WHERE App_Id__c IN ({in_list}) "
            f"AND Status__c = 'Available'"
        )
        if recs is None:
            return None  # SF unreachable
        for r in recs:
            try:
                avail.add(int(r["App_Id__c"]))
            except (KeyError, TypeError, ValueError):
                pass
    return avail


def available_slot_app_ids(app_ids: list[int]) -> set[int] | None:
    """Given candidate app slot ids, return the subset Salesforce marks Available.
    SALESFORCE IS THE SOURCE OF TRUTH for what may be offered or booked: a slot not
    in Salesforce at all, or Booked/Locked there, is NOT in the returned set. Reads
    the in-memory snapshot (0ms) when ready; otherwise a live query. Returns None
    ONLY when Salesforce is unreachable and no snapshot has ever loaded — the gate
    treats that as a cold-start degrade (leave local result) rather than emptying
    the calendar; once any snapshot exists, a transient blip serves the last-good
    Available set."""
    ids = [int(i) for i in app_ids if i is not None]
    if not ids:
        return set()
    with _snap_lock:
        ready = _snapshot_ready
        snap = _available_snapshot
    if ready:
        return {n for n in ids if n in snap}
    return _available_live(ids)


# --------------------------------------------------------------------------- #
# Read-through: vehicles + showrooms sourced from Salesforce (fail-open).
# Each returns the EXACT dict shape the app's catalogue produces, or None on ANY
# problem, so the caller falls back to the local DB — no quality regression is
# possible (a missing SF field or outage silently reverts to local).
# --------------------------------------------------------------------------- #
def _i(x):
    try:
        return int(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _feats(text):
    return [line for line in (text or "").split("\n") if line.strip()]


_VCOLS = ("App_Id__c,Model_Code__c,Name,Brand__c,Body_Type__c,Seats_Min__c,Seats_Max__c,"
          "Fuel_Type__c,Price_Min__c,Price_Max__c,Image_URL__c,Headline_Features__c,"
          "Headline_Features_AR__c,Data_Provenance__c")
_SCOLS = ("App_Id__c,Name,Brand__c,City__c,Emirate__c,Address__c,Address_AR__c,"
          "Map_Link__c,Phone__c,Hours_By_Day__c,Latitude__c,Longitude__c")

# Short in-process cache for the catalogue reads (vehicles/showrooms rarely change).
# Cuts a Salesforce round-trip off most chat turns. Availability (slots) is served
# from the snapshot above. TTL tunable via SF_READ_CACHE_SECONDS (0 disables). The
# background refresher keeps these entries perpetually warm so calls never go cold.
# TTL is generous (the refresher re-warms them on its own cadence, below); the TTL
# only matters as an on-demand fallback if the refresher is off.
_CACHE_TTL = float(os.getenv("SF_READ_CACHE_SECONDS", "600"))
# Catalogue re-warm cadence (vehicles/showrooms rarely change) — decoupled from the
# fast availability snapshot to KEEP THE DAILY API BUDGET LOW. Developer Edition caps
# at 15k API calls/day; at these defaults the refresher costs ~1 call / SF_SNAPSHOT_SECONDS
# (availability) + 2 calls / SF_CATALOGUE_REFRESH_SECONDS (catalogue). Raise the
# snapshot interval in prod if the org's cap is tight.
_CATALOGUE_REFRESH = float(os.getenv("SF_CATALOGUE_REFRESH_SECONDS", "300"))
_cache: dict = {}
_VEH_SOQL = f"SELECT {_VCOLS} FROM Vehicle__c"
_SR_SOQL = f"SELECT {_SCOLS} FROM Showroom__c ORDER BY App_Id__c"


def _cached_soql(key, soql):
    now = time.monotonic()
    ent = _cache.get(key)
    if ent and ent[1] > now:
        return ent[0]
    recs = _soql(soql)
    if recs is not None and _CACHE_TTL > 0:
        _cache[key] = (recs, now + _CACHE_TTL)
    return recs


def _refresh_catalogue_cache() -> None:
    """Force-refresh the catalogue cache (bypasses TTL) so warm reads never lapse
    to a cold fetch during a call. Keeps the last good value on failure."""
    if _CACHE_TTL <= 0:
        return
    now = time.monotonic()
    for key, soql in (("vehicles", _VEH_SOQL), ("showrooms", _SR_SOQL)):
        recs = _soql(soql)
        if recs is not None:
            _cache[key] = (recs, now + _CACHE_TTL)


def _all_vehicles():
    return _cached_soql("vehicles", _VEH_SOQL)


def _all_showrooms_c():
    return _cached_soql("showrooms", _SR_SOQL)


def showroom_coords() -> dict:
    """{app_id:int -> (lat, lng)} for showrooms, sourced from Salesforce
    (Latitude__c/Longitude__c). Powers the nearest-showroom feature so a branch's
    map location is a Salesforce field. Empty dict on ANY problem, so the caller
    falls back to the app's local coordinates (fail-open)."""
    try:
        rows = _all_showrooms_c()
        if rows is None:
            return {}
        out: dict = {}
        for s in rows:
            aid = _i(s.get("App_Id__c"))
            lat, lng = s.get("Latitude__c"), s.get("Longitude__c")
            if aid is None or lat is None or lng is None:
                continue
            try:
                out[aid] = (float(lat), float(lng))
            except (TypeError, ValueError):
                pass
        return out
    except Exception as exc:  # noqa: BLE001
        _log.warning("salesforce: showroom_coords error %s", exc)
        return {}


# --------------------------------------------------------------------------- #
# Pre-warm + background refresh. Started once at app startup (see app.main) when
# the integration is enabled. The thread is a daemon, so no explicit shutdown.
# --------------------------------------------------------------------------- #
def prewarm() -> None:
    """Load caches + the availability snapshot once, synchronously, so the first
    request (or call turn) is already warm. Safe to call when disabled."""
    if not enabled():
        return
    try:
        _refresh_catalogue_cache()
        refresh_booked_snapshot()
        _log.info("salesforce: prewarm complete (booked=%d)", len(_booked_snapshot))
    except Exception as exc:  # noqa: BLE001
        _log.warning("salesforce: prewarm error %s", exc)


def _refresh_loop() -> None:
    # Availability snapshot runs every tick; the catalogue re-warm runs only every
    # Nth tick (SF_CATALOGUE_REFRESH_SECONDS) to keep the daily API call count low.
    every = max(1, round(_CATALOGUE_REFRESH / _SNAPSHOT_SECONDS)) if _SNAPSHOT_SECONDS > 0 else 1
    i = 0
    while True:
        time.sleep(_SNAPSHOT_SECONDS)
        i += 1
        try:
            refresh_booked_snapshot()
            if i % every == 0:
                _refresh_catalogue_cache()
        except Exception as exc:  # noqa: BLE001
            _log.warning("salesforce: refresh loop error %s", exc)


def start_background_refresh() -> None:
    """Start the daemon refresher (idempotent). Keeps the booked-slot snapshot and
    the catalogue cache fresh so no request ever waits on Salesforce."""
    global _bg_started
    if _bg_started or not enabled() or _SNAPSHOT_SECONDS <= 0:
        return
    _bg_started = True
    t = threading.Thread(target=_refresh_loop, name="sf-refresh", daemon=True)
    t.start()
    _log.info("salesforce: background refresher started (every %ss)", _SNAPSHOT_SECONDS)


def _sr_brief(s):
    return {"showroom_id": _i(s.get("App_Id__c")), "name": s.get("Name"), "emirate": s.get("Emirate__c")}


def _veh_dict(v, showrooms, full):
    out = {
        "vehicle_id": _i(v.get("App_Id__c")), "code": v.get("Model_Code__c"), "name": v.get("Name"),
        "brand": v.get("Brand__c"), "body_type": v.get("Body_Type__c"),
        "seats_min": _i(v.get("Seats_Min__c")), "seats_max": _i(v.get("Seats_Max__c")),
        "fuel": v.get("Fuel_Type__c"),
        "price_aed_min": _i(v.get("Price_Min__c")), "price_aed_max": _i(v.get("Price_Max__c")),
        "image_url": v.get("Image_URL__c"),
        "headline_features": _feats(v.get("Headline_Features__c")),
        "available_showrooms": [_sr_brief(s) for s in showrooms],
    }
    if full:
        out["data_provenance"] = v.get("Data_Provenance__c")
        # reproduce the app's raw image path list exactly: "/media/x" -> "seed/media/x"
        img = v.get("Image_URL__c")
        out["images"] = ["seed" + img] if (img and img.startswith("/media/")) else ([img] if img else [])
    return out


def get_details_sf(*, name=None, code=None):
    try:
        code = code.strip() if isinstance(code, str) and code.strip() else None
        name = name.strip() if isinstance(name, str) and name.strip() else None
        vehicles = _all_vehicles()
        showrooms = _all_showrooms_c()
        if vehicles is None or showrooms is None:
            return None
        if code:
            hit = [v for v in vehicles if (v.get("Model_Code__c") or "").upper() == code.upper()]
            if hit:
                return {"vehicle": _veh_dict(hit[0], showrooms, True)}
        if not name:
            return {"error": "vehicle_not_found", "query": code} if code else \
                {"error": "missing_query", "detail": "Provide a model name or code."}
        needle = name.lower()
        exact = [v for v in vehicles if (v.get("Name") or "").lower() == needle]
        if len(exact) == 1:
            return {"vehicle": _veh_dict(exact[0], showrooms, True)}
        hits = [v for v in vehicles if needle in (v.get("Name") or "").lower()]
        if not hits:
            hits = [v for v in vehicles if needle in (v.get("Model_Code__c") or "").lower()]
        if not hits:
            return {"error": "vehicle_not_found", "query": name}
        if len(hits) > 1:
            return {"ambiguous": True, "query": name,
                    "candidates": [_veh_dict(v, showrooms, False) for v in hits]}
        return {"vehicle": _veh_dict(hits[0], showrooms, True)}
    except Exception as exc:  # noqa: BLE001
        _log.warning("salesforce: get_details_sf error %s", exc)
        return None


def detail_for_dialog_sf(vehicle_id):
    try:
        vid = _i(vehicle_id)
        if vid is None:
            return None
        allv = _all_vehicles()
        showrooms = _all_showrooms_c()
        if allv is None or showrooms is None:  # SF failed -> fall back to local
            return None
        match = [x for x in allv if _i(x.get("App_Id__c")) == vid]
        if not match:  # unknown -> fall back to local
            return None
        v = match[0]
        return {
            "vehicle_id": _i(v.get("App_Id__c")), "name": v.get("Name"), "body_type": v.get("Body_Type__c"),
            "seats_min": _i(v.get("Seats_Min__c")), "seats_max": _i(v.get("Seats_Max__c")),
            "price_aed_min": _i(v.get("Price_Min__c")), "price_aed_max": _i(v.get("Price_Max__c")),
            "fuel": v.get("Fuel_Type__c"), "image_url": v.get("Image_URL__c"),
            "headline_features": _feats(v.get("Headline_Features__c")),
            "headline_features_ar": _feats(v.get("Headline_Features_AR__c")),
            "showrooms": [{"showroom_id": _i(s.get("App_Id__c")), "name": s.get("Name"),
                           "emirate": s.get("Emirate__c"), "city": s.get("City__c")} for s in showrooms],
        }
    except Exception as exc:  # noqa: BLE001
        _log.warning("salesforce: detail_for_dialog_sf error %s", exc)
        return None


def list_showrooms_sf(*, emirate=None, vehicle_id=None):
    try:
        showrooms = _all_showrooms_c()
        if showrooms is None:
            return None
        if vehicle_id is not None:
            vid = _i(vehicle_id)
            allv = _all_vehicles()
            if allv is None:
                return None
            if vid is None or not any(_i(x.get("App_Id__c")) == vid for x in allv):
                return {"error": "vehicle_not_found", "vehicle_id": vehicle_id, "showrooms": []}
            # current data: every vehicle stocks every showroom -> no subset filter
        try:
            from app.services.catalogue import resolve_place  # lazy: avoid import cycle
        except Exception:  # noqa: BLE001
            resolve_place = None
        rows = list(showrooms)
        city_slug = None
        place_named = emirate is not None and str(emirate).strip() != ""
        if place_named:
            place = resolve_place(emirate) if resolve_place else None
            if place is None:
                return {"showrooms": [], "unmatched_emirate": emirate}
            city_slug, emirate_slug = place
            rows = [s for s in rows if s.get("Emirate__c") == emirate_slug]
            if city_slug is not None:
                rows.sort(key=lambda s: (s.get("City__c") != city_slug, _i(s.get("App_Id__c")) or 0))
        out = []
        for s in rows:
            try:
                hbd = json.loads(s["Hours_By_Day__c"]) if s.get("Hours_By_Day__c") else {}
            except Exception:  # noqa: BLE001
                hbd = {}
            entry = {"showroom_id": _i(s.get("App_Id__c")), "name": s.get("Name"), "brand": s.get("Brand__c"),
                     "city": s.get("City__c"), "emirate": s.get("Emirate__c"), "address": s.get("Address__c"),
                     "address_ar": s.get("Address_AR__c"), "map_link": s.get("Map_Link__c"),
                     "phone": s.get("Phone__c"), "hours_by_day": hbd}
            if place_named:
                entry["match_type"] = "city" if (city_slug is not None and s.get("City__c") == city_slug) else "emirate"
            out.append(entry)
        return {"showrooms": out}
    except Exception as exc:  # noqa: BLE001
        _log.warning("salesforce: list_showrooms_sf error %s", exc)
        return None
