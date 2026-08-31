#!/usr/bin/env python3
"""
Seed Salesforce Vehicle__c + Showroom__c from the app's seed JSON (upsert by
external id -> re-running is safe).  Env: SF_DOMAIN, SF_CK, SF_CS,
SEED_DIR (default /opt/elite/elite-group-backend/seed).
"""
import json, os, sys, time, urllib.request, urllib.parse, urllib.error

DOMAIN = os.environ["SF_DOMAIN"].rstrip("/"); CK = os.environ["SF_CK"]; CS = os.environ["SF_CS"]
SEED = os.environ.get("SEED_DIR", "/opt/elite/elite-group-backend/seed")
API = "v62.0"


def _req(method, url, token=None, json_body=None, form=None):
    data, ctype = None, None
    if json_body is not None: data, ctype = json.dumps(json_body).encode(), "application/json"
    elif form is not None: data, ctype = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method)
    if token: req.add_header("Authorization", "Bearer " + token)
    if ctype: req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            t = r.read().decode(); return r.status, (json.loads(t) if t else {})
    except urllib.error.HTTPError as e:
        t = e.read().decode()
        try: return e.code, json.loads(t)
        except Exception: return e.code, t


# --- token (retry a couple of times in case of a transient hiccup) ---
AT = IU = None
for attempt in range(3):
    st, j = _req("POST", DOMAIN + "/services/oauth2/token",
                 form={"grant_type": "client_credentials", "client_id": CK, "client_secret": CS})
    if st == 200 and isinstance(j, dict) and "access_token" in j:
        AT, IU = j["access_token"], j["instance_url"]; break
    print(f"  token attempt {attempt+1}: HTTP {st} -> {str(j)[:200]}")
    time.sleep(3)
if not AT:
    print("AUTH FAILED"); sys.exit(1)
print("connected", IU)


def patch(obj, extfield, value, body):
    url = f"{IU}/services/data/{API}/sobjects/{obj}/{extfield}/{urllib.parse.quote(str(value))}"
    return _req("PATCH", url, token=AT, json_body=body)


def load(name):
    return json.load(open(os.path.join(SEED, name), encoding="utf-8"))


print("seeding showrooms...")
ns = 0
for s in load("showrooms.json"):
    body = {"Name": s["name"], "City__c": s.get("city"), "Emirate__c": s.get("emirate"),
            "Address__c": s.get("address"), "Phone__c": s.get("phone"), "Hours__c": s.get("hours")}
    st, j = patch("Showroom__c", "App_Id__c", s["id"], {k: v for k, v in body.items() if v is not None})
    print(f"  showroom {s['name']:<28} {st}  {'' if 200<=st<300 else j}")
    ns += 1 if 200 <= st < 300 else 0

print("seeding vehicles...")
nv = 0
for v in load("vehicle_models.json"):
    body = {"Name": v["name"], "Brand__c": v.get("brand"), "Body_Type__c": v.get("body_type"),
            "Seats_Min__c": v.get("seats_min"), "Seats_Max__c": v.get("seats_max"),
            "Price_Min__c": v.get("price_aed_min"), "Price_Max__c": v.get("price_aed_max"),
            "Fuel_Type__c": v.get("fuel_type")}
    st, j = patch("Vehicle__c", "Model_Code__c", v["model_code"], {k: x for k, x in body.items() if x is not None})
    print(f"  vehicle  {v['name']:<28} {st}  {'' if 200<=st<300 else j}")
    nv += 1 if 200 <= st < 300 else 0

print(f"\nSEEDED: {ns} showrooms, {nv} vehicles")
