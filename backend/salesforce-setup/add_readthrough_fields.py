#!/usr/bin/env python3
"""Add the fields Salesforce needs to fully back the vehicle/showroom cards
without any quality loss: Vehicle__c.App_Id__c and Showroom__c Brand/Address_AR/
Map_Link/Hours_By_Day. + FLS. Env: SF_DOMAIN, SF_CK, SF_CS."""
import base64, io, os, re, sys, time, zipfile, json
import urllib.request, urllib.parse, urllib.error

DOMAIN = os.environ["SF_DOMAIN"].rstrip("/"); CK = os.environ["SF_CK"]; CS = os.environ["SF_CS"]; API = "62.0"

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

# object -> list of (name, label, type, length)
NEW = {
    "Vehicle__c": [("App_Id__c", "App Id", "Text", 16, True)],
    "Showroom__c": [
        ("Brand__c", "Brand", "Text", 64, False),
        ("Address_AR__c", "Address (AR)", "LongTextArea", 1000, False),
        ("Map_Link__c", "Map Link", "LongTextArea", 500, False),
        ("Hours_By_Day__c", "Hours By Day (JSON)", "LongTextArea", 2000, False),
    ],
}
def fxml(name, label, typ, length, extid):
    x = [f"<fields><fullName>{name}</fullName><label>{label}</label><type>{typ}</type>"]
    if typ == "Text":
        x.append(f"<length>{length}</length>")
        if extid: x.append("<externalId>true</externalId><unique>true</unique>")
    elif typ == "LongTextArea":
        x.append(f"<length>{length}</length><visibleLines>3</visibleLines>")
    x.append("</fields>"); return "".join(x)

def objxml(fields):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">'
            + "".join(fxml(*f) for f in fields) + "</CustomObject>")

members = "".join(f"<members>{o}.{f[0]}</members>" for o, fs in NEW.items() for f in fs)
pkg = ('<?xml version="1.0" encoding="UTF-8"?><Package xmlns="http://soap.sforce.com/2006/04/metadata">'
       f'<types>{members}<name>CustomField</name></types><version>{API}</version></Package>')
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("package.xml", pkg)
    for o, fs in NEW.items(): z.writestr(f"objects/{o}.object", objxml(fs))
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

print("deploying read-through fields...")
st, b = soap(f"<met:deploy><met:ZipFile>{zb}</met:ZipFile><met:DeployOptions>"
             "<met:singlePackage>true</met:singlePackage><met:rollbackOnError>true</met:rollbackOnError>"
             "</met:DeployOptions></met:deploy>")
aid = tag(b, "id"); status = None
for _ in range(40):
    time.sleep(5)
    st, b = soap(f"<met:checkDeployStatus><met:asyncProcessId>{aid}</met:asyncProcessId>"
                 "<met:includeDetails>true</met:includeDetails></met:checkDeployStatus>")
    if tag(b, "done") == "true": status = tag(b, "status"); break
print("deploy status:", status)
if status != "Succeeded":
    for m in re.findall(r"<(?:\w+:)?componentFailures>(.*?)</(?:\w+:)?componentFailures>", b, re.S):
        print("  FAIL", tag(m, "fullName"), "->", tag(m, "problem"))
    sys.exit(1)

# FLS
DATA = f"{IU}/services/data/v{API}/"
psid = json.loads(_req("GET", DATA + "query/?q=" + urllib.parse.quote("SELECT Id FROM PermissionSet WHERE Name='Elite_Integration'"), AT)[1])["records"][0]["Id"]
n = 0
for o, fs in NEW.items():
    for f in fs:
        _req("POST", DATA + "sobjects/FieldPermissions", AT, json_body={
            "ParentId": psid, "SobjectType": o, "Field": f"{o}.{f[0]}",
            "PermissionsRead": True, "PermissionsEdit": True}); n += 1
print(f"FLS applied for {n} fields")
print("DONE")
