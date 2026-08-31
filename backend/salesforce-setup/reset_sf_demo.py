#!/usr/bin/env python3
"""reset-demo, but for Salesforce: make the org MIRROR the app's current DB.

Run AFTER `python -m app.cli reset-demo`, from the repo root on the host:

    docker exec -i elite-group-backend-1 python - < salesforce-setup/reset_sf_demo.py

(The backend image copies only app/ and seed/, so the script is piped in on
stdin; it needs the container for DATABASE_URL/Postgres and the SF_* env.)

Phases, in order:
  1. WIPE transactional data in the org: Test_Drive_Agent_Feedback__c,
     Test_Drive__c, Slot__c (the whole stale window — the thing that breaks
     booking pushes), and every Contact carrying Mobile_Ext__c (synced ones).
  2. Upsert Vehicle__c (by Model_Code__c) + Showroom__c (by App_Id__c) from the
     LIVE DB — not seed JSON: the DB is what the app serves. Includes the
     read-through enrichment (App_Id, brand, Arabic address, map link, hours
     JSON, coordinates) so the separate enrichment scripts are not needed.
  3. Seed Slot__c from the DB: the next SF_SEED_DAYS days (default 3) PLUS,
     always, every slot a live booking references — a booking push must never
     again fail on a slot the org has not heard of. Developer Edition storage
     is ~5 MB (~2 KB/record): 3 days ≈ 800 slots ≈ 1.6 MB. What the window
     drops is LOGGED, never silent.
  4. Push current live bookings (Contact upsert + Test_Drive__c + slot flip),
     so the dashboard matches the DB immediately, not on next change.
  5. Push unsynced agent_feedback rows and stamp synced_at.

Everything is upsert-by-external-id, so re-running is safe. Uses the composite
collections API (200 records/call) to stay far under the 15k/day API cap.
Live-vendor script BY DESIGN — run manually, never from tests.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import psycopg

API = os.environ.get("SF_API_VERSION", "v62.0")
DSN = os.environ["DATABASE_URL"].replace("+psycopg", "")
DOMAIN = os.environ.get("SF_DOMAIN", "").rstrip("/")
CK = os.environ.get("SF_CLIENT_ID", "")
CS = os.environ.get("SF_CLIENT_SECRET", "")
ENABLED = os.environ.get("SF_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
SEED_DAYS = int(os.environ.get("SF_SEED_DAYS", "3"))


def log(*a):
    print("[reset-sf]", *a, flush=True)


if not ENABLED or not DOMAIN or not CK or not CS:
    log("refusing to run: SF_ENABLED/SF_DOMAIN/SF_CLIENT_ID/SF_CLIENT_SECRET not all set")
    sys.exit(1)

log(f"target org: {DOMAIN}")
log("this WIPES the org's slots, test drives, feedback and synced contacts, then reseeds from the app DB")

# --- auth + tiny REST layer -------------------------------------------------- #
_tok = None


def token(force=False):
    global _tok
    if _tok and not force and _tok[2] > time.monotonic():
        return _tok[0], _tok[1]
    form = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "client_id": CK, "client_secret": CS}).encode()
    req = urllib.request.Request(DOMAIN + "/services/oauth2/token", data=form, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read())
    _tok = (j["access_token"], j["instance_url"], time.monotonic() + 1800)
    return _tok[0], _tok[1]


def rest(method, path, body=None, retry=True):
    at, iu = token()
    url = f"{iu}/services/data/{API}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + at)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            t = r.read().decode()
            return r.status, (json.loads(t) if t else None)
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry:
            token(force=True)
            return rest(method, path, body, retry=False)
        return e.code, e.read().decode()[:300]


def query_all(soql):
    recs, st, j = [], *rest("GET", "query/?q=" + urllib.parse.quote(soql))
    while True:
        if st != 200:
            log("  query failed:", st, j)
            return recs
        recs.extend(j.get("records", []))
        nxt = j.get("nextRecordsUrl")
        if not nxt:
            return recs
        st, j = rest("GET", nxt.split(f"/{API}/", 1)[1])


def wipe(obj, where=""):
    ids = [r["Id"] for r in query_all(f"SELECT Id FROM {obj}" + (f" WHERE {where}" if where else ""))]
    gone = 0
    for i in range(0, len(ids), 200):
        chunk = ",".join(ids[i:i + 200])
        st, j = rest("DELETE", f"composite/sobjects?ids={chunk}&allOrNone=false")
        if st == 200:
            gone += sum(1 for x in j if x.get("success"))
    log(f"  wiped {gone}/{len(ids)} {obj}")
    return gone


def upsert_batch(obj, ext_field, records, what):
    """Collections upsert, 200 per call. Records must each carry ext_field."""
    ok = bad = 0
    for i in range(0, len(records), 200):
        chunk = [{**r, "attributes": {"type": obj}} for r in records[i:i + 200]]
        st, j = rest("PATCH", f"composite/sobjects/{obj}/{ext_field}",
                     {"allOrNone": False, "records": chunk})
        if st == 200:
            for x in j:
                if x.get("success"):
                    ok += 1
                else:
                    bad += 1
                    if bad <= 3:
                        log("  upsert error:", str(x.get("errors"))[:200])
        else:
            bad += len(chunk)
            log("  batch failed:", st, str(j)[:200])
    log(f"  upserted {ok} {what}" + (f" ({bad} FAILED)" if bad else ""))
    return ok


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000+0400")  # app times are naive Dubai


def feats(v):
    return "\n".join(v) if isinstance(v, list) else (v or "")


# --- 1) wipe ------------------------------------------------------------------ #
log("1) wiping transactional data…")
wipe("Test_Drive_Agent_Feedback__c")
wipe("Test_Drive__c")
wipe("Slot__c")
wipe("Contact", "Mobile_Ext__c != null")

with psycopg.connect(DSN) as conn:
    # --- 2) reference data ---------------------------------------------------- #
    log("2) upserting vehicles + showrooms from the DB…")
    vrows = conn.execute(
        "SELECT id, model_code, brand, name, body_type, seats_min, seats_max, "
        "price_aed_min, price_aed_max, fuel_type, headline_features, "
        "headline_features_ar, images FROM vehicle_models ORDER BY id").fetchall()
    vehicles = []
    for (vid, code, brand, name, body, smin, smax, pmin, pmax, fuel, hf, hfar, images) in vrows:
        img = None
        if isinstance(images, list) and images:
            img = str(images[0])
            if img.startswith("seed"):     # stored "seed/media/…", served "/media/…"
                img = img[len("seed"):]
        vehicles.append({"Model_Code__c": code, "App_Id__c": str(vid), "Name": name,
                         "Brand__c": brand, "Body_Type__c": body,
                         "Seats_Min__c": smin, "Seats_Max__c": smax,
                         "Price_Min__c": pmin, "Price_Max__c": pmax,
                         "Fuel_Type__c": fuel, "Image_URL__c": img,
                         "Headline_Features__c": feats(hf),
                         "Headline_Features_AR__c": feats(hfar)})
    upsert_batch("Vehicle__c", "Model_Code__c", vehicles, "vehicles")

    srows = conn.execute(
        "SELECT id, brand, name, emirate, city, address, address_ar, map_link, "
        "latitude, longitude, phone, hours, hours_by_day FROM showrooms ORDER BY id").fetchall()
    showrooms = []
    for (sid, brand, name, emirate, city, addr, addr_ar, maplink, lat, lng, phone, hours, hbd) in srows:
        showrooms.append({"App_Id__c": str(sid), "Name": name, "Brand__c": brand,
                          "Emirate__c": emirate, "City__c": city, "Address__c": addr,
                          "Address_AR__c": addr_ar, "Map_Link__c": maplink,
                          "Latitude__c": float(lat) if lat is not None else None,
                          "Longitude__c": float(lng) if lng is not None else None,
                          "Phone__c": phone, "Hours__c": hours,
                          "Hours_By_Day__c": json.dumps(hbd) if hbd is not None else None})
    upsert_batch("Showroom__c", "App_Id__c", showrooms, "showrooms")

    # Resolve record ids once so slots/bookings link by Id (deterministic).
    veh_id = {r["Model_Code__c"]: r["Id"] for r in query_all("SELECT Id, Model_Code__c FROM Vehicle__c")}
    sr_id = {r["App_Id__c"]: r["Id"] for r in query_all("SELECT Id, App_Id__c FROM Showroom__c")}

    # --- 3) slots -------------------------------------------------------------- #
    log(f"3) seeding slots (next {SEED_DAYS} days + every booked slot)…")
    horizon = datetime.now() + timedelta(days=SEED_DAYS)
    slot_rows = conn.execute(
        "SELECT s.id, s.location_type, s.showroom_id, s.service_area, s.vehicle_id, "
        "s.start, s.\"end\", s.status, v.model_code, v.name, sh.name "
        "FROM slots s JOIN vehicle_models v ON v.id = s.vehicle_id "
        "LEFT JOIN showrooms sh ON sh.id = s.showroom_id "
        "WHERE s.start < %s OR s.id IN (SELECT slot_id FROM bookings) "
        "ORDER BY s.start", (horizon,)).fetchall()
    total_slots = conn.execute("SELECT count(*) FROM slots").fetchone()[0]
    status_map = {"open": "Available", "locked": "Locked", "booked": "Booked"}
    slots = []
    for (sid, loc, sh_id, area, _vid, start, end, status, code, vname, shname) in slot_rows:
        place = shname if loc == "showroom" else f"Doorstep, {area or 'Dubai'}"
        name = f"{vname} — {place} — {start:%d %b %H:%M}"[:80]
        rec = {"App_Id__c": str(sid), "Name": name,
               "Start__c": iso(start), "End__c": iso(end),
               "Status__c": status_map.get(status, "Available"),
               "Location_Type__c": loc,
               "Vehicle_Ref__c": veh_id.get(code)}
        if loc == "showroom" and sh_id is not None:
            rec["Showroom_Ref__c"] = sr_id.get(str(sh_id))
        slots.append(rec)
    dropped = total_slots - len(slots)
    log(f"  window covers {len(slots)} of {total_slots} slots "
        f"({dropped} beyond {SEED_DAYS} days NOT seeded — raise SF_SEED_DAYS to widen, "
        f"mind the ~5 MB Developer Edition storage cap at ~2 KB/record)")
    upsert_batch("Slot__c", "App_Id__c", slots, "slots")

    # --- 4) live bookings ------------------------------------------------------ #
    log("4) pushing live bookings…")
    brows = conn.execute(
        "SELECT b.ref, b.status, b.location, b.driver_name, b.attendance, "
        "c.mobile, c.name_en, c.name_ar, c.language_pref, c.emirate, c.email, "
        "v.model_code, s.id, s.start, s.showroom_id "
        "FROM bookings b JOIN customers c ON c.id = b.customer_id "
        "JOIN vehicle_models v ON v.id = b.vehicle_id "
        "JOIN slots s ON s.id = b.slot_id "
        "WHERE b.status IN ('confirmed','rescheduled') ORDER BY b.id").fetchall()
    pushed = 0
    for (ref, status, loc, driver, att, mobile, nen, nar, lang, emirate, email,
         code, slot_id, slot_start, sh_id) in brows:
        nm = (nen or nar or "").split()
        first, last = (nm[0], " ".join(nm[1:])) if len(nm) >= 2 else ("", nm[0] if nm else "Customer")
        cbody = {"FirstName": first, "LastName": last, "MobilePhone": mobile,
                 "Language__c": lang, "Emirate__c": emirate}
        if email:
            cbody["Email"] = email
        rest("PATCH", f"sobjects/Contact/Mobile_Ext__c/{urllib.parse.quote(mobile)}",
             {k: v for k, v in cbody.items() if v is not None})
        crecs = query_all(f"SELECT Id FROM Contact WHERE Mobile_Ext__c='{mobile}'")
        td = {"Name": ref, "Start__c": iso(slot_start), "Status__c": status,
              "Location_Type__c": loc, "Attendance__c": att, "Channel__c": "chat",
              "Customer_Mobile__c": mobile, "Vehicle_Ref__c": veh_id.get(code),
              "Slot_Ref__r": {"App_Id__c": str(slot_id)}}
        if crecs:
            td["Contact__c"] = crecs[0]["Id"]
        if sh_id is not None:
            td["Showroom_Ref__c"] = sr_id.get(str(sh_id))
        if driver:
            td["Driver_Name__c"] = driver
        st, j = rest("PATCH", f"sobjects/Test_Drive__c/Booking_Ref__c/{urllib.parse.quote(ref)}", td)
        if st in (200, 201, 204):
            pushed += 1
        else:
            log(f"  booking {ref} FAILED: {st} {j}")
    log(f"  pushed {pushed}/{len(brows)} bookings")

    # --- 5) agent feedback ----------------------------------------------------- #
    log("5) pushing agent feedback…")
    frows = conn.execute(
        "SELECT id, client_name, mobile, booking_ref, stars, comment, created_at "
        "FROM agent_feedback ORDER BY id").fetchall()
    fok = 0
    for (fid, cname, mobile, bref, stars, comment, created) in frows:
        body = {"Star_Rating__c": stars, "Comments__c": comment, "Booking_Ref__c": bref,
                "Client_Name__c": cname, "Mobile__c": mobile, "Feedback_At__c": iso(created)}
        if mobile:
            crecs = query_all(f"SELECT Id FROM Contact WHERE Mobile_Ext__c='{mobile}'")
            if crecs:
                body["Contact__c"] = crecs[0]["Id"]
        st, _ = rest("POST", "sobjects/Test_Drive_Agent_Feedback__c",
                     {k: v for k, v in body.items() if v is not None})
        if st in (200, 201):
            fok += 1
            conn.execute("UPDATE agent_feedback SET synced_at = now() WHERE id = %s", (fid,))
    conn.commit()
    log(f"  pushed {fok}/{len(frows)} feedback rows")

log("DONE. The backend's availability snapshot refreshes itself within ~10 s.")
log("Restart the sidecar so its erasure tracking starts clean:  docker restart elite-group-sf-sync-1")
