#!/usr/bin/env python3
"""
Elite Group -> Salesforce FULL MIRROR schema (custom objects) via Metadata API.

For an org that ALLOWS custom objects (Developer/Enterprise/Pro+API). Creates:
    Vehicle__c, Showroom__c, Slot__c, Test_Drive__c, Test_Drive_Feedback__c
plus custom fields on Contact. Then a permission set with FLS, assigned to the
Run-As user. Idempotent.  Env: SF_DOMAIN, SF_CK, SF_CS, SF_RUNAS(optional).
"""
import base64, io, json, os, re, sys, time, zipfile
import urllib.request, urllib.parse, urllib.error

DOMAIN = os.environ["SF_DOMAIN"].rstrip("/"); CK = os.environ["SF_CK"]; CS = os.environ["SF_CS"]
RUNAS = os.environ.get("SF_RUNAS", "")
API = "62.0"


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
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


st, b = _req("POST", DOMAIN + "/services/oauth2/token",
             form={"grant_type": "client_credentials", "client_id": CK, "client_secret": CS})
if st != 200: print("AUTH FAILED", b); sys.exit(1)
j = json.loads(b); AT = j["access_token"]; IU = j["instance_url"]
print("connected", IU)


def esc(s): return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
def F(name, label, **kw): kw.update(name=name, label=label); return kw

CUSTOM = {
    "Vehicle__c": dict(label="Vehicle", plural="Vehicles",
        name={"type": "Text", "label": "Vehicle Name"}, fields=[
        F("Model_Code__c", "Model Code", type="Text", length=32, externalId=True, unique=True),
        F("Brand__c", "Brand", type="Text", length=64), F("Body_Type__c", "Body Type", type="Text", length=64),
        F("Seats_Min__c", "Seats Min", type="Number", precision=2, scale=0),
        F("Seats_Max__c", "Seats Max", type="Number", precision=2, scale=0),
        F("Price_Min__c", "Price Min (AED)", type="Number", precision=12, scale=0),
        F("Price_Max__c", "Price Max (AED)", type="Number", precision=12, scale=0),
        F("Fuel_Type__c", "Fuel Type", type="Picklist", values=["petrol", "phev"])]),
    "Showroom__c": dict(label="Showroom", plural="Showrooms",
        name={"type": "Text", "label": "Showroom Name"}, fields=[
        F("App_Id__c", "App Id", type="Text", length=16, externalId=True, unique=True),
        F("City__c", "City", type="Text", length=64), F("Emirate__c", "Emirate", type="Text", length=64),
        F("Address__c", "Address", type="LongTextArea", length=1000, visibleLines=3),
        F("Phone__c", "Phone", type="Phone"),
        F("Hours__c", "Hours", type="LongTextArea", length=2000, visibleLines=4)]),
    "Slot__c": dict(label="Slot", plural="Slots",
        name={"type": "Text", "label": "Slot Name"}, fields=[
        F("App_Id__c", "App Id", type="Text", length=16, externalId=True, unique=True),
        F("Vehicle_Ref__c", "Vehicle", type="Lookup", referenceTo="Vehicle__c",
          relationshipName="Slots", relationshipLabel="Slots"),
        F("Showroom_Ref__c", "Showroom", type="Lookup", referenceTo="Showroom__c",
          relationshipName="Slots", relationshipLabel="Slots"),
        F("Start__c", "Start", type="DateTime"), F("End__c", "End", type="DateTime"),
        F("Status__c", "Status", type="Picklist", values=["open", "locked", "booked"]),
        F("Location_Type__c", "Location Type", type="Picklist", values=["showroom", "doorstep"])]),
    "Test_Drive__c": dict(label="Test Drive", plural="Test Drives",
        name={"type": "Text", "label": "Test Drive Name"}, fields=[
        F("Booking_Ref__c", "Booking Ref", type="Text", length=16, externalId=True, unique=True),
        F("Contact__c", "Contact", type="Lookup", referenceTo="Contact",
          relationshipName="Test_Drives", relationshipLabel="Test Drives"),
        F("Vehicle_Ref__c", "Vehicle", type="Lookup", referenceTo="Vehicle__c",
          relationshipName="Test_Drives", relationshipLabel="Test Drives"),
        F("Showroom_Ref__c", "Showroom", type="Lookup", referenceTo="Showroom__c",
          relationshipName="Test_Drives", relationshipLabel="Test Drives"),
        F("Slot_Ref__c", "Slot", type="Lookup", referenceTo="Slot__c",
          relationshipName="Test_Drives", relationshipLabel="Test Drives"),
        F("Start__c", "Start", type="DateTime"),
        F("Location_Type__c", "Location Type", type="Picklist", values=["showroom", "doorstep"]),
        F("Status__c", "Status", type="Picklist", values=["confirmed", "rescheduled", "cancelled"]),
        F("Attendance__c", "Attendance", type="Picklist", values=["pending", "completed", "noshow"]),
        F("Channel__c", "Channel", type="Picklist", values=["chat", "call", "whatsapp"]),
        F("Driver_Name__c", "Driver Name", type="Text", length=255),
        F("Doorstep_Address__c", "Doorstep Address", type="LongTextArea", length=1000, visibleLines=3)]),
    "Test_Drive_Feedback__c": dict(label="Test Drive Feedback", plural="Test Drive Feedback",
        name={"type": "AutoNumber", "label": "Feedback #", "displayFormat": "FB-{00000}"}, fields=[
        F("Test_Drive_Ref__c", "Test Drive", type="Lookup", referenceTo="Test_Drive__c",
          relationshipName="Feedback", relationshipLabel="Feedback"),
        F("Overall__c", "Overall", type="Number", precision=2, scale=0),
        F("Ease__c", "Ease", type="Number", precision=2, scale=0),
        F("Vehicle_Score__c", "Vehicle Score", type="Number", precision=2, scale=0),
        F("Team__c", "Team", type="Number", precision=2, scale=0),
        F("Comment__c", "Comment", type="LongTextArea", length=2000, visibleLines=4)]),
}
CONTACT_FIELDS = [
    F("Mobile_Ext__c", "Mobile (External Id)", type="Text", length=20, externalId=True, unique=True),
    F("Language__c", "Language", type="Picklist", values=["en", "ar"]),
    F("Emirate__c", "Emirate", type="Text", length=64),
    F("Memory_Consent__c", "Memory Consent", type="Checkbox"),
    F("WhatsApp_Consent__c", "WhatsApp Consent", type="Checkbox"),
]


def field_xml(fd):
    x = ["<fields>", f"<fullName>{fd['name']}</fullName>", f"<label>{esc(fd['label'])}</label>", f"<type>{fd['type']}</type>"]
    t = fd["type"]
    if t == "Text":
        x.append(f"<length>{fd.get('length',255)}</length>")
        if fd.get("externalId"): x.append("<externalId>true</externalId>")
        if fd.get("unique"): x.append("<unique>true</unique>")
    elif t == "LongTextArea":
        x.append(f"<length>{fd.get('length',1000)}</length><visibleLines>{fd.get('visibleLines',3)}</visibleLines>")
    elif t == "Number":
        x.append(f"<precision>{fd.get('precision',10)}</precision><scale>{fd.get('scale',0)}</scale>")
    elif t == "Checkbox":
        x.append("<defaultValue>false</defaultValue>")
    elif t == "Lookup":
        x.append(f"<referenceTo>{fd['referenceTo']}</referenceTo>")
        x.append(f"<relationshipName>{fd['relationshipName']}</relationshipName>")
        x.append(f"<relationshipLabel>{esc(fd['relationshipLabel'])}</relationshipLabel>")
        x.append("<deleteConstraint>SetNull</deleteConstraint>")
    elif t == "Picklist":
        x.append("<valueSet><valueSetDefinition><sorted>false</sorted>")
        for v in fd["values"]:
            x.append(f"<value><fullName>{esc(v)}</fullName><default>false</default><label>{esc(v)}</label></value>")
        x.append("</valueSetDefinition></valueSet>")
    x.append("</fields>")
    return "".join(x)


def object_xml(spec, standard=False):
    x = ['<?xml version="1.0" encoding="UTF-8"?>', '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">']
    if not standard:
        x.append(f"<label>{esc(spec['label'])}</label><pluralLabel>{esc(spec['plural'])}</pluralLabel>")
        nf = spec["name"]; x.append(f"<nameField><label>{esc(nf['label'])}</label><type>{nf['type']}</type>")
        if nf["type"] == "AutoNumber": x.append(f"<displayFormat>{esc(nf['displayFormat'])}</displayFormat>")
        x.append("</nameField><deploymentStatus>Deployed</deploymentStatus><sharingModel>ReadWrite</sharingModel>")
    fields = spec["fields"] if not standard else spec
    for fd in fields: x.append(field_xml(fd))
    x.append("</CustomObject>"); return "".join(x)


obj_members = "".join(f"<members>{n}</members>" for n in CUSTOM)
fld_members = "".join(f"<members>Contact.{fd['name']}</members>" for fd in CONTACT_FIELDS)
pkg = ('<?xml version="1.0" encoding="UTF-8"?><Package xmlns="http://soap.sforce.com/2006/04/metadata">'
       f'<types>{obj_members}<name>CustomObject</name></types>'
       f'<types>{fld_members}<name>CustomField</name></types><version>{API}</version></Package>')
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("package.xml", pkg)
    for n, spec in CUSTOM.items(): z.writestr(f"objects/{n}.object", object_xml(spec))
    z.writestr("objects/Contact.object", object_xml(CONTACT_FIELDS, standard=True))
zb = base64.b64encode(buf.getvalue()).decode()

MS = f"{IU}/services/Soap/m/{API}"
def soap(inner):
    env = ('<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope '
           'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
           'xmlns:met="http://soap.sforce.com/2006/04/metadata"><soapenv:Header>'
           f'<met:SessionHeader><met:sessionId>{AT}</met:sessionId></met:SessionHeader></soapenv:Header>'
           f'<soapenv:Body>{inner}</soapenv:Body></soapenv:Envelope>')
    return _req("POST", MS, raw=env.encode(), headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": '""'})
def tag(x, n):
    m = re.search(r"<(?:\w+:)?%s>(.*?)</(?:\w+:)?%s>" % (n, n), x, re.S); return m.group(1) if m else None

print("deploying full mirror (%d custom objects + Contact fields)..." % len(CUSTOM))
st, b = soap(f"<met:deploy><met:ZipFile>{zb}</met:ZipFile><met:DeployOptions>"
             "<met:singlePackage>true</met:singlePackage><met:rollbackOnError>true</met:rollbackOnError>"
             "</met:DeployOptions></met:deploy>")
aid = tag(b, "id"); status = None
for _ in range(48):
    time.sleep(5)
    st, b = soap(f"<met:checkDeployStatus><met:asyncProcessId>{aid}</met:asyncProcessId>"
                 "<met:includeDetails>true</met:includeDetails></met:checkDeployStatus>")
    if tag(b, "done") == "true": status = tag(b, "status"); break
print("  deploy status:", status)
if status != "Succeeded":
    for m in re.findall(r"<(?:\w+:)?componentFailures>(.*?)</(?:\w+:)?componentFailures>", b, re.S):
        print("   FAIL", tag(m, "fullName"), "->", tag(m, "problem"))
    sys.exit(1)

# ---- FLS ----------------------------------------------------------------------
DATA = f"{IU}/services/data/v{API}/"
def dget(p): st, x = _req("GET", DATA + p, AT); return (json.loads(x) if x else {})
def dpost(p, body):
    st, x = _req("POST", DATA + p, AT, json_body=body)
    try: return st, json.loads(x)
    except Exception: return st, x
def dupe(st, j): return (200 <= st < 300) or ("DUPLICATE" in json.dumps(j)) or ("already" in json.dumps(j).lower())

q = dget("query/?q=" + urllib.parse.quote("SELECT Id FROM PermissionSet WHERE Name='Elite_Integration'"))
psid = q["records"][0]["Id"] if q.get("records") else None
if not psid:
    st, jj = dpost("sobjects/PermissionSet", {"Name": "Elite_Integration", "Label": "Elite Integration"})
    psid = jj.get("id")
print("  permission set:", psid)
for obj in CUSTOM:
    dpost("sobjects/ObjectPermissions", {"ParentId": psid, "SobjectType": obj, "PermissionsRead": True,
        "PermissionsCreate": True, "PermissionsEdit": True, "PermissionsDelete": True,
        "PermissionsViewAllRecords": True, "PermissionsModifyAllRecords": True})
allfp = [(o, fd["name"]) for o, s in CUSTOM.items() for fd in s["fields"]] + [("Contact", fd["name"]) for fd in CONTACT_FIELDS]
fail = 0
for obj, name in allfp:
    st, jj = dpost("sobjects/FieldPermissions", {"ParentId": psid, "SobjectType": obj,
        "Field": f"{obj}.{name}", "PermissionsRead": True, "PermissionsEdit": True})
    if not dupe(st, jj): fail += 1; print("   fieldperm FAIL", obj, name, jj)
print(f"  field perms applied ({fail} failures)")
if RUNAS:
    u = dget("query/?q=" + urllib.parse.quote(f"SELECT Id FROM User WHERE Username='{RUNAS}'"))
    uid = u["records"][0]["Id"] if u.get("records") else None
    if uid:
        st, jj = dpost("sobjects/PermissionSetAssignment", {"PermissionSetId": psid, "AssigneeId": uid})
        print("  assign PS:", "OK" if dupe(st, jj) else jj)
print("\nFULL MIRROR SCHEMA DONE.")
