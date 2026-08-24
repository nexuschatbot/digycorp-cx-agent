#!/usr/bin/env python3
"""Redeploy the 'All' list views for Slot + Test Drive WITH useful columns
(so booked slots are visibly distinct, and Test Drives show customer/vehicle)."""
import base64, io, os, re, sys, time, zipfile, json
import urllib.request, urllib.parse, urllib.error

DOMAIN = os.environ["SF_DOMAIN"].rstrip("/"); CK = os.environ["SF_CK"]; CS = os.environ["SF_CS"]; API = "62.0"

def _req(method, url, raw=None, headers=None, form=None):
    data = None
    if form is not None: data = urllib.parse.urlencode(form).encode()
    elif raw is not None: data = raw
    r = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items(): r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp: return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()

st, b = _req("POST", DOMAIN + "/services/oauth2/token",
             form={"grant_type": "client_credentials", "client_id": CK, "client_secret": CS})
j = json.loads(b); AT = j["access_token"]; IU = j["instance_url"]
print("connected", IU)

VIEWS = {
    "Slot__c": ["Status__c", "Vehicle_Ref__c", "Showroom_Ref__c", "Start__c", "Location_Type__c"],
    "Test_Drive__c": ["Status__c", "Contact__c", "Customer_Mobile__c", "Vehicle_Ref__c",
                      "Showroom_Ref__c", "Start__c", "Channel__c"],
}

def obj_xml(cols):
    colx = "".join(f"<columns>{c}</columns>" for c in cols)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata"><listViews>'
            '<fullName>All</fullName><label>All</label><filterScope>Everything</filterScope>'
            f'{colx}</listViews></CustomObject>')

members = "".join(f"<members>{o}.All</members>" for o in VIEWS)
pkg = ('<?xml version="1.0" encoding="UTF-8"?><Package xmlns="http://soap.sforce.com/2006/04/metadata">'
       f'<types>{members}<name>ListView</name></types><version>{API}</version></Package>')
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("package.xml", pkg)
    for o, cols in VIEWS.items(): z.writestr(f"objects/{o}.object", obj_xml(cols))
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

print("deploying list-view columns...")
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
