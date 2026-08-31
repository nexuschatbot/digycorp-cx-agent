#!/usr/bin/env python3
"""Create custom-object Tabs + an 'Elite Test Drives' Lightning app (Metadata API)."""
import base64, io, os, re, sys, time, zipfile, json
import urllib.request, urllib.parse, urllib.error

DOMAIN = os.environ["SF_DOMAIN"].rstrip("/"); CK = os.environ["SF_CK"]; CS = os.environ["SF_CS"]
API = "62.0"

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

OBJECTS = ["Vehicle__c", "Showroom__c", "Slot__c", "Test_Drive__c", "Test_Drive_Feedback__c"]
MOTIF = "Custom20: Car"

def tab_xml():
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<CustomTab xmlns="http://soap.sforce.com/2006/04/metadata">'
            f'<customObject>true</customObject><motif>{MOTIF}</motif></CustomTab>')

app_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
    '<CustomApplication xmlns="http://soap.sforce.com/2006/04/metadata">'
    '<label>Elite Test Drives</label><navType>Standard</navType><uiType>Lightning</uiType>'
    '<formFactors>Large</formFactors>'
    '<tabs>Test_Drive__c</tabs><tabs>Slot__c</tabs><tabs>Vehicle__c</tabs>'
    '<tabs>Showroom__c</tabs><tabs>Test_Drive_Feedback__c</tabs><tabs>standard-Contact</tabs>'
    '</CustomApplication>')

tab_members = "".join(f"<members>{o}</members>" for o in OBJECTS)
pkg = ('<?xml version="1.0" encoding="UTF-8"?><Package xmlns="http://soap.sforce.com/2006/04/metadata">'
       f'<types>{tab_members}<name>CustomTab</name></types>'
       '<types><members>Elite_Test_Drives</members><name>CustomApplication</name></types>'
       f'<version>{API}</version></Package>')

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("package.xml", pkg)
    for o in OBJECTS: z.writestr(f"tabs/{o}.tab", tab_xml())
    z.writestr("applications/Elite_Test_Drives.app", app_xml)
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

print("deploying tabs + app...")
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
print("\nDONE. App Launcher -> 'Elite Test Drives'. List URL example:")
print(f"  {IU.replace('.my.salesforce.com', '.lightning.force.com')}/lightning/o/Vehicle__c/list")
