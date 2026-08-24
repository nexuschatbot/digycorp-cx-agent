#!/usr/bin/env python3
"""Grant App + Tab visibility via a NEW permission set 'Elite_App_Access' (so the
Elite Integration FLS permission set is left untouched), then assign it to the user.
Env: SF_DOMAIN, SF_CK, SF_CS, SF_RUNAS."""
import base64, io, json, os, re, sys, time, zipfile
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
        with urllib.request.urlopen(req, timeout=120) as r: return r.status, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()

st, b = _req("POST", DOMAIN + "/services/oauth2/token",
             form={"grant_type": "client_credentials", "client_id": CK, "client_secret": CS})
j = json.loads(b); AT = j["access_token"]; IU = j["instance_url"]
print("connected", IU)

TABS = ["Vehicle__c", "Showroom__c", "Slot__c", "Test_Drive__c", "Test_Drive_Feedback__c"]
tabxml = "".join(f"<tabSettings><tab>{t}</tab><visibility>Visible</visibility></tabSettings>" for t in TABS)
ps = ('<?xml version="1.0" encoding="UTF-8"?>'
      '<PermissionSet xmlns="http://soap.sforce.com/2006/04/metadata">'
      '<label>Elite App Access</label><hasActivationRequired>false</hasActivationRequired>'
      '<applicationVisibilities><application>Elite_Test_Drives</application><visible>true</visible></applicationVisibilities>'
      f'{tabxml}</PermissionSet>')
pkg = ('<?xml version="1.0" encoding="UTF-8"?><Package xmlns="http://soap.sforce.com/2006/04/metadata">'
       '<types><members>Elite_App_Access</members><name>PermissionSet</name></types>'
       f'<version>{API}</version></Package>')
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("package.xml", pkg); z.writestr("permissionsets/Elite_App_Access.permissionset", ps)
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

print("deploying Elite_App_Access permission set...")
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

DATA = f"{IU}/services/data/v{API}/"
def dget(p): st, x = _req("GET", DATA + p, AT); return json.loads(x) if x else {}
def dpost(p, body):
    st, x = _req("POST", DATA + p, AT, json_body=body)
    try: return st, json.loads(x)
    except Exception: return st, x

psid = dget("query/?q=" + urllib.parse.quote("SELECT Id FROM PermissionSet WHERE Name='Elite_App_Access'"))["records"][0]["Id"]
uid = dget("query/?q=" + urllib.parse.quote(f"SELECT Id FROM User WHERE Username='{RUNAS}'"))["records"][0]["Id"]
st, j = dpost("sobjects/PermissionSetAssignment", {"PermissionSetId": psid, "AssigneeId": uid})
dup = (200 <= st < 300) or ("DUPLICATE" in json.dumps(j))
print("assign Elite_App_Access to", RUNAS, ":", "OK" if dup else j)
print("\nDONE — refresh Salesforce, App Launcher -> 'Elite Test Drives'.")
