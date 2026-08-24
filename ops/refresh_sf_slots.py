#!/usr/bin/env python3
"""Refresh Salesforce Slot__c so the org holds a FUTURE, bookable window.

Salesforce is the source of truth for slot AVAILABILITY (see app/services/slots.py:
"SALESFORCE IS THE SOURCE OF TRUTH for availability" — the app keeps only the slots
SF marks Available). So slots that exist in the app database but not in Salesforce
are invisible to Aisha, and a Salesforce window that has fallen into the past means
NOTHING is bookable at all.

Two phases:
  1. DELETE every Slot__c whose Start__c is before today (Dubai) — stale inventory
     that only consumes Developer Edition storage.
  2. UPSERT the app's slots for the next SF_WINDOW_DAYS days (default 7) by
     App_Id__c, carrying status, vehicle and showroom.

Developer Edition storage is ~5 MB at roughly 2 KB per record, so the window is
deliberately shorter than the app's own 14-day inventory. The script prints the
storage position before and after so the window can be tuned.

Run inside the PRODUCTION backend container on ELITE-DEV-VM:
    docker exec -e SF_DOMAIN -e SF_CK -e SF_CS elite-backend-1 \
        python3 /tmp/refresh_sf_slots.py

Dry-run by default; set CONFIRM=1 to apply. Env: SF_DOMAIN, SF_CK, SF_CS,
DATABASE_URL. Optional: SF_WINDOW_DAYS.
"""
import json, os, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta

import psycopg

DOMAIN = os.environ["SF_DOMAIN"].rstrip("/")
CK, CS = os.environ["SF_CK"], os.environ["SF_CS"]
API = os.environ.get("SF_API_VERSION", "v62.0")
CONFIRM = os.environ.get("CONFIRM") == "1"
WINDOW_DAYS = int(os.environ.get("SF_WINDOW_DAYS", "7"))
DSN = os.environ["DATABASE_URL"].replace("+psycopg", "")

_now_dubai = datetime.utcnow() + timedelta(hours=4)
TODAY = _now_dubai.replace(hour=0, minute=0, second=0, microsecond=0)
CUTOFF = TODAY.strftime("%Y-%m-%dT%H:%M:%S.000+0400")
HORIZON = TODAY + timedelta(days=WINDOW_DAYS)

STATUS = {"open": "Available", "locked": "Locked", "booked": "Booked"}


def _req(method, path, body=None, token=None, absolute=False):
    url = path if absolute else f"{IU}/services/data/{API}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", "Bearer " + (token or AT))
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            t = resp.read().decode()
            return resp.status, (json.loads(t) if t else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


# --- auth ------------------------------------------------------------------ #
form = urllib.parse.urlencode({"grant_type": "client_credentials",
                               "client_id": CK, "client_secret": CS}).encode()
rq = urllib.request.Request(DOMAIN + "/services/oauth2/token", data=form, method="POST")
rq.add_header("Content-Type", "application/x-www-form-urlencoded")
j = json.load(urllib.request.urlopen(rq))
AT, IU = j["access_token"], j["instance_url"]
print("connected:", IU)
print(f"today (Dubai) : {CUTOFF}")
print(f"window        : {WINDOW_DAYS} days -> {HORIZON:%Y-%m-%d}")
print("mode          :", "APPLY" if CONFIRM else "DRY-RUN (set CONFIRM=1 to apply)")


def storage():
    st, L = _req("GET", "limits/")
    if st == 200 and "DataStorageMB" in L:
        d = L["DataStorageMB"]
        return f"{d['Max'] - d['Remaining']} of {d['Max']} MB"
    return "unknown"


print("storage before:", storage())


def query_all(soql):
    recs, url = [], "query/?q=" + urllib.parse.quote(soql)
    while url:
        st, d = _req("GET", url)
        if st != 200:
            print("  query failed:", st, d)
            return recs
        recs += d.get("records", [])
        nxt = d.get("nextRecordsUrl")
        url = (IU + nxt) if nxt else None
        if url:
            st, d = _req("GET", url, absolute=True)
            recs += d.get("records", []) if st == 200 else []
            nxt2 = d.get("nextRecordsUrl") if st == 200 else None
            url = (IU + nxt2) if nxt2 else None
    return recs


# --- 1) delete stale past slots -------------------------------------------- #
stale = query_all(f"SELECT Id FROM Slot__c WHERE Start__c < {CUTOFF}")
print(f"\n[1] stale past slots in Salesforce: {len(stale)}")
if CONFIRM and stale:
    ids = [r["Id"] for r in stale]
    gone = 0
    for i in range(0, len(ids), 200):
        st, res = _req("DELETE", "composite/sobjects?allOrNone=false&ids=" + ",".join(ids[i:i + 200]))
        if st == 200:
            gone += sum(1 for x in res if x.get("success"))
    print(f"    deleted {gone}/{len(ids)}")

# --- 2) push the future window --------------------------------------------- #
veh = {r["Model_Code__c"]: r["Id"] for r in query_all("SELECT Id, Model_Code__c FROM Vehicle__c")}
shr = {r["App_Id__c"]: r["Id"] for r in query_all("SELECT Id, App_Id__c FROM Showroom__c")}
print(f"\n[2] resolved {len(veh)} vehicles, {len(shr)} showrooms")

with psycopg.connect(DSN) as conn:
    rows = conn.execute(
        'SELECT s.id, s.location_type, s.showroom_id, s.service_area, s.start, s."end", '
        's.status, v.model_code, v.name, sh.name '
        'FROM slots s JOIN vehicle_models v ON v.id = s.vehicle_id '
        'LEFT JOIN showrooms sh ON sh.id = s.showroom_id '
        'WHERE s.start >= %s AND s.start < %s ORDER BY s.start',
        (TODAY, HORIZON)).fetchall()

recs = []
for sid, loc, sh_id, area, start, end, status, code, vname, shname in rows:
    place = shname if loc == "showroom" else f"Doorstep, {area or 'Dubai'}"
    rec = {
        "attributes": {"type": "Slot__c"},
        "App_Id__c": str(sid),
        "Name": f"{vname} — {place} — {start:%d %b %H:%M}"[:80],
        "Start__c": start.strftime("%Y-%m-%dT%H:%M:%S.000+0400"),
        "End__c": end.strftime("%Y-%m-%dT%H:%M:%S.000+0400"),
        "Status__c": STATUS.get(status, "Available"),
        "Location_Type__c": loc,
        "Vehicle_Ref__c": veh.get(code),
    }
    if loc == "showroom" and sh_id is not None:
        rec["Showroom_Ref__c"] = shr.get(str(sh_id))
    recs.append(rec)

by_status = {}
for r in recs:
    by_status[r["Status__c"]] = by_status.get(r["Status__c"], 0) + 1
print(f"    slots to push: {len(recs)}  {by_status}")
print(f"    estimated storage: ~{len(recs) * 2 / 1024:.1f} MB at 2 KB/record")

if CONFIRM and recs:
    ok = bad = 0
    for i in range(0, len(recs), 200):
        st, res = _req("PATCH", "composite/sobjects/Slot__c/App_Id__c",
                       {"allOrNone": False, "records": recs[i:i + 200]})
        if st == 200:
            for x in res:
                if x.get("success"):
                    ok += 1
                else:
                    bad += 1
                    if bad <= 3:
                        print("    error:", str(x.get("errors"))[:180])
        else:
            bad += len(recs[i:i + 200])
            print("    batch failed:", st, str(res)[:200])
    print(f"    upserted {ok}" + (f", {bad} FAILED" if bad else ""))
    print("storage after:", storage())

print("\nDONE." if CONFIRM else "\nDRY-RUN complete — re-run with CONFIRM=1 to apply.")
