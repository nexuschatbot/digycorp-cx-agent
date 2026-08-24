#!/usr/bin/env python3
"""Verify record counts and add an 'All' list view to each custom object."""
import base64, io, json, os, re, sys, time, zipfile
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

OBJS = ["Vehicle__c", "Showroom__c", "Slot__c", "Test_Drive__c", "Test_Drive_Feedback__c"]
print("=== record counts ===")
for o in OBJS:
    st, x = _req("GET", f"{IU}/services/data/v{API}/query/?q=" + urllib.parse.quote(f"SELECT COUNT() FROM {o}"), AT)
    tot = json.loads(x).get("totalSize") if 200 <= st < 300 else x
    print(f"  {o:<26} {tot}")

# deploy an 'All' list view per object
def objxml():
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata"><listViews>'
            '<fullName>All</fullName><label>All</label><filterScope>Everything</filterScope>'
            '</listViews></CustomObject>')
members = "".join(f"<members>{o}.All</members>" for o in OBJS)
pkg = ('<?xml version="1.0" encoding="UTF-8"?><Package xmlns="http://soap.sforce.com/2006/04/metadata">'
       f'<types>{members}<name>ListView</name></types><version>{API}</version></Package>')
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("package.xml", pkg)
    for o in OBJS: z.writestr(f"objects/{o}.object", objxml())
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

print("=== deploying 'All' list views ===")
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
print("DONE. Refresh the tab and pick the 'All' list view.")
