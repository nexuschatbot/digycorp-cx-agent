#!/usr/bin/env python3
"""
Elite -> Salesforce booking write-back sidecar (+ PDPL erasure reaping).

Watches the app's Postgres and pushes new/changed bookings to Salesforce
(Contact + Test_Drive__c + Slot status). ALSO honours "forget me": when a
booking/customer the sidecar synced later vanishes from the app DB (hard-deleted
by erasure), it DELETES the Salesforce Test Drive + Contact and frees the slot.
A cancellation is NOT a delete (the row stays with status=cancelled) — only true
erasure removes the row, which is the signal used here.

The app is never touched. Fail-safe: errors are logged and the loop continues.
Env: DATABASE_URL, SF_DOMAIN, SF_CLIENT_ID, SF_CLIENT_SECRET, SF_ENABLED,
SF_SYNC_POLL_SECONDS.
"""
import json, os, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta
import psycopg

API = "v62.0"
DSN = os.environ["DATABASE_URL"].replace("+psycopg", "")
DOMAIN = os.environ.get("SF_DOMAIN", "").rstrip("/")
CK = os.environ.get("SF_CLIENT_ID", ""); CS = os.environ.get("SF_CLIENT_SECRET", "")
POLL = int(os.environ.get("SF_SYNC_POLL_SECONDS", "5"))
ENABLED = os.environ.get("SF_ENABLED", "false").lower() in {"1", "true", "yes", "on"}

def log(*a): print("[sf-sync]", *a, flush=True)

_tok = None
def token(force=False):
    global _tok
    if _tok and not force and _tok[2] > time.monotonic():
        return _tok[0], _tok[1]
    form = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "client_id": CK, "client_secret": CS}).encode()
    r = urllib.request.Request(DOMAIN + "/services/oauth2/token", data=form, method="POST")
    r.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(r, timeout=15) as resp:
        j = json.loads(resp.read())
    _tok = (j["access_token"], j["instance_url"], time.monotonic() + 1800)
    return _tok[0], _tok[1]

def _send(method, path, body=None):
    at, iu = token()
    url = f"{iu}/services/data/{API}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + at)
    if data is not None: req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        if e.code == 401:
            token(force=True); return _send(method, path, body)
        if e.code != 404:  # 404 on delete = already gone, fine
            log(method, path, "->", e.code, e.read().decode()[:160])
        return e.code

def patch(obj, extfield, value, body):
    return _send("PATCH", f"sobjects/{obj}/{extfield}/{urllib.parse.quote(str(value))}", body)
def delete_ext(obj, extfield, value):
    return _send("DELETE", f"sobjects/{obj}/{extfield}/{urllib.parse.quote(str(value))}")

def query(soql):
    at, iu = token()
    url = f"{iu}/services/data/{API}/query/?q=" + urllib.parse.quote(soql)
    req = urllib.request.Request(url); req.add_header("Authorization", "Bearer " + at)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()).get("records", [])
    except urllib.error.HTTPError as e:
        if e.code == 401:
            token(force=True); return query(soql)
        return []

def split_name(en, ar):
    name = (en or ar or "").strip(); parts = name.split()
    if len(parts) >= 2: return parts[0], " ".join(parts[1:])
    if len(parts) == 1: return "", parts[0]
    return "", "Customer"

def iso(dt): return dt.strftime("%Y-%m-%dT%H:%M:%S.000+0400")

SQL = """
SELECT b.ref, b.status, b.location, b.driver_name, b.attendance, b.updated_at,
       c.mobile, c.name_en, c.name_ar, c.language_pref, c.emirate, c.email,
       v.model_code, s.id AS slot_app_id, s.start AS slot_start, s.showroom_id
FROM bookings b
JOIN customers c ON c.id = b.customer_id
JOIN vehicle_models v ON v.id = b.vehicle_id
JOIN slots s ON s.id = b.slot_id
WHERE b.updated_at > %s
ORDER BY b.updated_at
"""

def push(row):
    cols = ["ref","status","location","driver_name","attendance","updated_at","mobile",
            "name_en","name_ar","language_pref","emirate","email","model_code",
            "slot_app_id","slot_start","showroom_id"]
    d = dict(zip(cols, row))
    first, last = split_name(d["name_en"], d["name_ar"])
    c_body = {"FirstName": first, "LastName": last, "MobilePhone": d["mobile"],
              "Language__c": d["language_pref"], "Emirate__c": d["emirate"]}
    if d["email"]: c_body["Email"] = d["email"]
    patch("Contact", "Mobile_Ext__c", d["mobile"], {k: v for k, v in c_body.items() if v is not None})
    # Link the Contact by RECORD ID (reliable) rather than relationship-by-external
    # -id, which can fail to resolve right after a Contact is (re)created.
    crecs = query(f"SELECT Id FROM Contact WHERE Mobile_Ext__c='{d['mobile']}'")
    td = {"Name": d["ref"], "Start__c": iso(d["slot_start"]), "Status__c": d["status"],
          "Location_Type__c": d["location"], "Attendance__c": d["attendance"], "Channel__c": "chat",
          "Customer_Mobile__c": d["mobile"],
          "Vehicle_Ref__r": {"Model_Code__c": d["model_code"]},
          "Slot_Ref__r": {"App_Id__c": str(d["slot_app_id"])}}
    td["Contact__c"] = crecs[0]["Id"] if crecs else None
    if not crecs:
        td["Contact__r"] = {"Mobile_Ext__c": d["mobile"]}
        del td["Contact__c"]
    if d["showroom_id"] is not None:
        td["Showroom_Ref__r"] = {"App_Id__c": str(d["showroom_id"])}
    if d["driver_name"]: td["Driver_Name__c"] = d["driver_name"]
    patch("Test_Drive__c", "Booking_Ref__c", d["ref"], td)
    slot_status = "Available" if d["status"] == "cancelled" else "Booked"
    patch("Slot__c", "App_Id__c", d["slot_app_id"], {"Status__c": slot_status})
    log(f"pushed booking {d['ref']} status={d['status']} slot={d['slot_app_id']}->{slot_status}")
    return d["ref"], d["slot_app_id"], d["mobile"]

def reap(conn, tracked):
    """Delete SF records for bookings/customers the sidecar synced that have since
    been hard-deleted from the app (PDPL 'forget me'). Cancellations don't count —
    the row still exists with status=cancelled."""
    app_refs = {r[0] for r in conn.execute("SELECT ref FROM bookings").fetchall()}
    app_mobiles = {r[0] for r in conn.execute("SELECT mobile FROM customers").fetchall()}
    for ref in list(tracked):
        if ref in app_refs:
            continue
        slot_app_id, mobile = tracked.pop(ref)
        delete_ext("Test_Drive__c", "Booking_Ref__c", ref)
        if slot_app_id is not None:
            patch("Slot__c", "App_Id__c", slot_app_id, {"Status__c": "Available"})
        contact_note = ""
        if mobile and mobile not in app_mobiles:
            delete_ext("Contact", "Mobile_Ext__c", mobile)
            contact_note = f" + contact {mobile}"
        log(f"reaped (forget-me) {ref}{contact_note}; slot {slot_app_id}->Available")


FEEDBACK_SQL = ("SELECT id, client_name, mobile, booking_ref, stars, comment, created_at, session_id "
                "FROM agent_feedback WHERE synced_at IS NULL ORDER BY id")

def push_feedback(conn):
    """Push new agent-conversation feedback rows to Test_Drive_Agent_Feedback__c."""
    rows = conn.execute(FEEDBACK_SQL).fetchall()
    for fid, client_name, mobile, booking_ref, stars, comment, created_at, session_id in rows:
        body = {"Star_Rating__c": stars, "Comments__c": comment,
                "Booking_Ref__c": booking_ref, "Client_Name__c": client_name,
                "Mobile__c": mobile, "Feedback_At__c": iso(created_at),
                "Session_Id__c": session_id}
        if mobile:
            crecs = query(f"SELECT Id FROM Contact WHERE Mobile_Ext__c='{mobile}'")
            if crecs:
                body["Contact__c"] = crecs[0]["Id"]
        body = {k: v for k, v in body.items() if v is not None}
        st = _send("POST", "sobjects/Test_Drive_Agent_Feedback__c", body)
        if st in (200, 201):
            conn.execute("UPDATE agent_feedback SET synced_at = now() WHERE id = %s", (fid,))
            conn.commit()
            log(f"pushed agent feedback #{fid} stars={stars} ref={booking_ref}")
        else:
            conn.rollback()
            log(f"agent feedback #{fid} push failed status={st}")

def main():
    if not ENABLED or not DOMAIN or not CK:
        log("disabled (SF_ENABLED/creds not set) — idling")
        while True: time.sleep(60)
    log(f"started, polling every {POLL}s -> {DOMAIN}")
    last_seen = datetime.now() - timedelta(minutes=2)
    tracked = {}  # ref -> (slot_app_id, mobile), for erasure reaping
    while True:
        try:
            with psycopg.connect(DSN) as conn:
                rows = conn.execute(SQL, (last_seen,)).fetchall()
                for row in rows:
                    try:
                        ref, slot_app_id, mobile = push(row)
                        tracked[ref] = (slot_app_id, mobile)
                        last_seen = max(last_seen, row[5])
                    except Exception as exc:  # noqa: BLE001
                        log("push error:", exc)
                try:
                    reap(conn, tracked)
                except Exception as exc:  # noqa: BLE001
                    log("reap error:", exc)
                try:
                    push_feedback(conn)
                except Exception as exc:  # noqa: BLE001
                    log("feedback error:", exc)
        except Exception as exc:  # noqa: BLE001
            log("loop error:", exc)
        time.sleep(POLL)

if __name__ == "__main__":
    main()
