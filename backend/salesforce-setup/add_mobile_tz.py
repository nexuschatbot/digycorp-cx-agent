#!/usr/bin/env python3
"""Add Test_Drive__c.Customer_Mobile__c (+FLS +layout) and set the run-as user's
timezone to Asia/Dubai. Env: SF_DOMAIN, SF_CK, SF_CS, SF_RUNAS."""
import base64, io, os, re, sys, time, zipfile, json
import urllib.request, urllib.parse, urllib.error

DOMAIN = os.environ["SF_DOMAIN"].rstrip("/"); CK = os.environ["SF_CK"]; CS = os.environ["SF_CS"]
RUNAS = os.environ["SF_RUNAS"]; API = "62.0"

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
            b = r.read().decode(); return r.status, b
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

st, b = _req("POST", DOMAIN + "/services/oauth2/token",
             form={"grant_type": "client_credentials", "client_id": CK, "client_secret": CS})
j = json.loads(b); AT = j["access_token"]; IU = j["instance_url"]
print("connected", IU)

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

def deploy(files, members, mtype):
    types = "".join(f"<members>{m}</members>" for m in members)
    pkg = ('<?xml version="1.0" encoding="UTF-8"?><Package xmlns="http://soap.sforce.com/2006/04/metadata">'
           f'<types>{types}<name>{mtype}</name></types><version>{API}</version></Package>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", pkg)
        for path, content in files.items(): z.writestr(path, content)
    zb = base64.b64encode(buf.getvalue()).decode()
    st, b = soap(f"<met:deploy><met:ZipFile>{zb}</met:ZipFile><met:DeployOptions>"
                 "<met:singlePackage>true</met:singlePackage><met:rollbackOnError>true</met:rollbackOnError>"
                 "</met:DeployOptions></met:deploy>")
    aid = tag(b, "id"); status = None
    for _ in range(40):
        time.sleep(5)
        st, b = soap(f"<met:checkDeployStatus><met:asyncProcessId>{aid}</met:asyncProcessId>"
                     "<met:includeDetails>true</met:includeDetails></met:checkDeployStatus>")
        if tag(b, "done") == "true": status = tag(b, "status"); break
    if status != "Succeeded":
        for m in re.findall(r"<(?:\w+:)?componentFailures>(.*?)</(?:\w+:)?componentFailures>", b, re.S):
            print("   FAIL", tag(m, "fullName"), "->", tag(m, "problem"))
    return status

# 1. field
print("1) deploy Customer_Mobile__c ...")
field_obj = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">'
             '<fields><fullName>Customer_Mobile__c</fullName><label>Customer Mobile</label>'
             '<type>Phone</type></fields></CustomObject>')
print("   ", deploy({"objects/Test_Drive__c.object": field_obj}, ["Test_Drive__c.Customer_Mobile__c"], "CustomField"))

# 2. FLS
DATA = f"{IU}/services/data/v{API}/"
psid = json.loads(_req("GET", DATA + "query/?q=" + urllib.parse.quote("SELECT Id FROM PermissionSet WHERE Name='Elite_Integration'"), AT)[1])["records"][0]["Id"]
_req("POST", DATA + "sobjects/FieldPermissions", AT, json_body={
    "ParentId": psid, "SobjectType": "Test_Drive__c", "Field": "Test_Drive__c.Customer_Mobile__c",
    "PermissionsRead": True, "PermissionsEdit": True})
print("2) FLS applied")

# 3. layout (with mobile after Contact)
print("3) redeploy Test Drive layout ...")
FIELDS = ["Name", "Booking_Ref__c", "Status__c", "Attendance__c", "Channel__c",
          "Contact__c", "Customer_Mobile__c", "Vehicle_Ref__c", "Showroom_Ref__c",
          "Slot_Ref__c", "Start__c", "Location_Type__c", "Driver_Name__c", "Doorstep_Address__c"]
def li(f): return f"<layoutItems><behavior>{'Required' if f=='Name' else 'Edit'}</behavior><field>{f}</field></layoutItems>"
mid = (len(FIELDS) + 1) // 2
layout = ('<?xml version="1.0" encoding="UTF-8"?>'
          '<Layout xmlns="http://soap.sforce.com/2006/04/metadata"><layoutSections>'
          '<editHeading>true</editHeading><label>Information</label>'
          f'<layoutColumns>{"".join(li(f) for f in FIELDS[:mid])}</layoutColumns>'
          f'<layoutColumns>{"".join(li(f) for f in FIELDS[mid:])}</layoutColumns>'
          '<style>TwoColumnsTopToBottom</style></layoutSections></Layout>')
print("   ", deploy({"layouts/Test_Drive__c-Test Drive Layout.layout": layout},
                     ["Test_Drive__c-Test Drive Layout"], "Layout"))

# 4. timezone
print("4) set user timezone Asia/Dubai ...")
uid = json.loads(_req("GET", DATA + "query/?q=" + urllib.parse.quote(f"SELECT Id FROM User WHERE Username='{RUNAS}'"), AT)[1])["records"][0]["Id"]
st, b = _req("PATCH", DATA + f"sobjects/User/{uid}", AT, json_body={"TimeZoneSidKey": "Asia/Dubai"})
print("   user TZ update:", st)
print("DONE")
