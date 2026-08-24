#!/usr/bin/env python3
"""Add Latitude__c / Longitude__c to Showroom__c (so the map location lives in
Salesforce, the single source of truth for showrooms), grant FLS, put them on the
Showroom layout's Details tab, and seed the 5 branch coordinates.
Env: SF_DOMAIN, SF_CK, SF_CS."""
import base64, io, json, os, re, sys, time, zipfile
import urllib.request, urllib.parse, urllib.error

DOMAIN = os.environ["SF_DOMAIN"].rstrip("/"); CK = os.environ["SF_CK"]; CS = os.environ["SF_CS"]; API = "62.0"

# App_Id__c -> (latitude, longitude) — from the app's branch pins.
COORDS = {
    "1": (25.1279, 55.2331),   # Jetour Al Quoz (Dubai)
    "3": (25.2704, 55.3220),   # Jetour Deira (Dubai)
    "4": (24.4290, 54.4270),   # Jetour Abu Dhabi (Rabdan)
    "5": (24.2075, 55.7447),   # Jetour Al Ain
    "6": (25.4052, 55.5136),   # Jetour Ajman
}

# Current Showroom layout fields + the two new ones (a Layout deploy REPLACES the
# layout, so the full ordered list must be present).
SHOWROOM_LAYOUT_FIELDS = [
    "Name", "App_Id__c", "City__c", "Emirate__c", "Phone__c", "Address__c",
    "Hours__c", "Latitude__c", "Longitude__c",
]


def _req(method, url, token=None, json_body=None, form=None, raw=None, headers=None):
    data, ctype = None, None
    if json_body is not None: data, ctype = json.dumps(json_body).encode(), "application/json"
    elif form is not None: data, ctype = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    elif raw is not None: data = raw
    req = urllib.request.Request(url, data=data, method=method)
    if token: req.add_header("Authorization", "Bearer " + token)
    if ctype: req.add_header("Content-Type", ctype)
    for k, v in (headers or {}).items(): req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=120) as r: return r.status, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()


st, b = _req("POST", DOMAIN + "/services/oauth2/token",
             form={"grant_type": "client_credentials", "client_id": CK, "client_secret": CS})
j = json.loads(b); AT = j["access_token"]; IU = j["instance_url"]
print("connected", IU)

MS = f"{IU}/services/Soap/m/{API}"
DATA = f"{IU}/services/data/v{API}/"


def soap(inner):
    env = ('<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope '
           'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
           'xmlns:met="http://soap.sforce.com/2006/04/metadata"><soapenv:Header>'
           f'<met:SessionHeader><met:sessionId>{AT}</met:sessionId></met:SessionHeader></soapenv:Header>'
           f'<soapenv:Body>{inner}</soapenv:Body></soapenv:Envelope>')
    return _req("POST", MS, raw=env.encode(),
                headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": '""'})


def tag(x, n):
    m = re.search(r"<(?:\w+:)?%s>(.*?)</(?:\w+:)?%s>" % (n, n), x, re.S)
    return m.group(1) if m else None


def deploy(zip_bytes, what):
    zb = base64.b64encode(zip_bytes).decode()
    st, b = soap(f"<met:deploy><met:ZipFile>{zb}</met:ZipFile><met:DeployOptions>"
                 "<met:singlePackage>true</met:singlePackage><met:rollbackOnError>true</met:rollbackOnError>"
                 "</met:DeployOptions></met:deploy>")
    aid = tag(b, "id"); status = None
    for _ in range(40):
        time.sleep(5)
        st, b = soap(f"<met:checkDeployStatus><met:asyncProcessId>{aid}</met:asyncProcessId>"
                     "<met:includeDetails>true</met:includeDetails></met:checkDeployStatus>")
        if tag(b, "done") == "true": status = tag(b, "status"); break
    print(f"{what} deploy status:", status)
    if status != "Succeeded":
        for m in re.findall(r"<(?:\w+:)?componentFailures>(.*?)</(?:\w+:)?componentFailures>", b, re.S):
            print("  FAIL", tag(m, "fullName"), "->", tag(m, "problem"))
        return False
    return True


# ---- 1) create the two Number fields ------------------------------------- #
def fxml(name, label):
    return (f"<fields><fullName>{name}</fullName><label>{label}</label>"
            f"<type>Number</type><precision>10</precision><scale>7</scale></fields>")

obj = ('<?xml version="1.0" encoding="UTF-8"?>'
       '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">'
       + fxml("Latitude__c", "Latitude") + fxml("Longitude__c", "Longitude")
       + "</CustomObject>")
pkg = ('<?xml version="1.0" encoding="UTF-8"?><Package xmlns="http://soap.sforce.com/2006/04/metadata">'
       '<types><members>Showroom__c.Latitude__c</members><members>Showroom__c.Longitude__c</members>'
       f'<name>CustomField</name></types><version>{API}</version></Package>')
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("package.xml", pkg); z.writestr("objects/Showroom__c.object", obj)
if not deploy(buf.getvalue(), "fields"):
    sys.exit(1)

# ---- 2) FLS on the integration permission set ---------------------------- #
psid = json.loads(_req("GET", DATA + "query/?q=" + urllib.parse.quote(
    "SELECT Id FROM PermissionSet WHERE Name='Elite_Integration'"), AT)[1])["records"][0]["Id"]
for fld in ("Latitude__c", "Longitude__c"):
    st, b = _req("POST", DATA + "sobjects/FieldPermissions", AT, json_body={
        "ParentId": psid, "SobjectType": "Showroom__c", "Field": f"Showroom__c.{fld}",
        "PermissionsRead": True, "PermissionsEdit": True})
    print(f"FLS {fld}:", "ok" if st in (200, 201) else b[:160])

# ---- 3) put them on the Showroom layout ---------------------------------- #
def item(field):
    beh = "Required" if field == "Name" else "Edit"
    return f"<layoutItems><behavior>{beh}</behavior><field>{field}</field></layoutItems>"

fields = SHOWROOM_LAYOUT_FIELDS
mid = (len(fields) + 1) // 2
left = "".join(item(f) for f in fields[:mid]); right = "".join(item(f) for f in fields[mid:])
layout = ('<?xml version="1.0" encoding="UTF-8"?>'
          '<Layout xmlns="http://soap.sforce.com/2006/04/metadata"><layoutSections>'
          '<editHeading>true</editHeading><label>Information</label>'
          f'<layoutColumns>{left}</layoutColumns><layoutColumns>{right}</layoutColumns>'
          '<style>TwoColumnsTopToBottom</style></layoutSections></Layout>')
lpkg = ('<?xml version="1.0" encoding="UTF-8"?><Package xmlns="http://soap.sforce.com/2006/04/metadata">'
        '<types><members>Showroom__c-Showroom Layout</members><name>Layout</name></types>'
        f'<version>{API}</version></Package>')
lbuf = io.BytesIO()
with zipfile.ZipFile(lbuf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("package.xml", lpkg)
    z.writestr("layouts/Showroom__c-Showroom Layout.layout", layout)
deploy(lbuf.getvalue(), "layout")  # non-fatal: fields already usable via API/FLS

# ---- 4) seed the coordinates --------------------------------------------- #
recs = json.loads(_req("GET", DATA + "query/?q=" + urllib.parse.quote(
    "SELECT Id, App_Id__c FROM Showroom__c"), AT)[1])["records"]
n = 0
for r in recs:
    aid = str(r.get("App_Id__c"))
    if aid in COORDS:
        lat, lng = COORDS[aid]
        st, b = _req("PATCH", DATA + f"sobjects/Showroom__c/{r['Id']}", AT,
                     json_body={"Latitude__c": lat, "Longitude__c": lng})
        print(f"seed App_Id {aid}: {lat},{lng} ->", "ok" if st == 204 else f"{st} {b[:160]}")
        if st == 204: n += 1
print(f"seeded {n} showrooms")
print("DONE")
