#!/usr/bin/env python3
"""Reseed the read-through fields from the app DB (runs in backend container):
Vehicle App_Id; Showroom Brand/Address_AR/Map_Link/Hours_By_Day."""
import json, os, urllib.request, urllib.parse, urllib.error
import psycopg

DSN = os.environ["DATABASE_URL"].replace("+psycopg", "")
DOMAIN = os.environ["SF_DOMAIN"].rstrip("/"); CK = os.environ["SF_CK"]; CS = os.environ["SF_CS"]; API = "v62.0"
_d = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": CK, "client_secret": CS}).encode()
_j = json.loads(urllib.request.urlopen(urllib.request.Request(DOMAIN + "/services/oauth2/token", data=_d, method="POST"), timeout=20).read())
AT, IU = _j["access_token"], _j["instance_url"]

def patch(obj, ext, val, body):
    url = f"{IU}/services/data/{API}/sobjects/{obj}/{ext}/{urllib.parse.quote(str(val))}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="PATCH")
    req.add_header("Authorization", "Bearer " + AT); req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r: return r.status
    except urllib.error.HTTPError as e: return e.code

with psycopg.connect(DSN) as conn:
    nv = 0
    for vid, code in conn.execute("SELECT id, model_code FROM vehicle_models").fetchall():
        if 200 <= patch("Vehicle__c", "Model_Code__c", code, {"App_Id__c": str(vid)}) < 300: nv += 1
    print(f"vehicles App_Id set: {nv}")
    ns = 0
    for sid, brand, addr_ar, maplink, hbd in conn.execute(
            "SELECT id, brand, address_ar, map_link, hours_by_day FROM showrooms").fetchall():
        body = {"Brand__c": brand}
        if addr_ar: body["Address_AR__c"] = addr_ar
        if maplink: body["Map_Link__c"] = maplink
        if hbd is not None: body["Hours_By_Day__c"] = json.dumps(hbd)
        if 200 <= patch("Showroom__c", "App_Id__c", sid, body) < 300: ns += 1
    print(f"showrooms enriched: {ns}")
