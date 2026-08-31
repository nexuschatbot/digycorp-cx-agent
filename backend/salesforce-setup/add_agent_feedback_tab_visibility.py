#!/usr/bin/env python3
"""Add the Test Drive Agent Feedback tab to the Elite_App_Access permission set
(the one that grants tab/app visibility to the GUI user) so it appears in the app
nav. Env: SF_DOMAIN, SF_CK, SF_CS."""
import base64, io, json, os, re, sys, time, zipfile
import urllib.request, urllib.parse, urllib.error

DOMAIN = os.environ["SF_DOMAIN"].rstrip("/"); CK = os.environ["SF_CK"]; CS = os.environ["SF_CS"]; API = "62.0"
TABS = ["Vehicle__c", "Showroom__c", "Slot__c", "Test_Drive__c",
        "Test_Drive_Feedback__c", "Test_Drive_Agent_Feedback__c"]


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

print("deploying Elite_App_Access (with agent-feedback tab)...")
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
print("DONE — hard-refresh Salesforce; the 'Test Drive Agent Feedback' tab should appear.")
