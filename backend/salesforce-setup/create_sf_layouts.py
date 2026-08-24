#!/usr/bin/env python3
"""Deploy full page layouts (all fields visible) for the 5 custom objects."""
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

# object -> (layout fullName, name-field behavior, ordered field list)
LAYOUTS = {
    "Test_Drive__c-Test Drive Layout": ("Required", [
        "Name", "Booking_Ref__c", "Status__c", "Attendance__c", "Channel__c",
        "Contact__c", "Vehicle_Ref__c", "Showroom_Ref__c", "Slot_Ref__c",
        "Start__c", "Location_Type__c", "Driver_Name__c", "Doorstep_Address__c"]),
    "Slot__c-Slot Layout": ("Required", [
        "Name", "App_Id__c", "Vehicle_Ref__c", "Showroom_Ref__c",
        "Start__c", "End__c", "Status__c", "Location_Type__c"]),
    "Vehicle__c-Vehicle Layout": ("Required", [
        "Name", "Model_Code__c", "Brand__c", "Body_Type__c", "Fuel_Type__c",
        "Seats_Min__c", "Seats_Max__c", "Price_Min__c", "Price_Max__c",
        "Showroom_Count__c", "Image_URL__c", "Headline_Features__c",
        "Headline_Features_AR__c", "Data_Provenance__c"]),
    "Showroom__c-Showroom Layout": ("Required", [
        "Name", "App_Id__c", "City__c", "Emirate__c", "Phone__c", "Address__c", "Hours__c"]),
    "Test_Drive_Feedback__c-Test Drive Feedback Layout": ("Readonly", [
        "Name", "Test_Drive_Ref__c", "Overall__c", "Ease__c",
        "Vehicle_Score__c", "Team__c", "Comment__c"]),
}

def item(field, name_behavior):
    beh = name_behavior if field == "Name" else "Edit"
    return f"<layoutItems><behavior>{beh}</behavior><field>{field}</field></layoutItems>"

def layout_xml(name_behavior, fields):
    mid = (len(fields) + 1) // 2
    left = "".join(item(f, name_behavior) for f in fields[:mid])
    right = "".join(item(f, name_behavior) for f in fields[mid:])
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Layout xmlns="http://soap.sforce.com/2006/04/metadata"><layoutSections>'
            '<editHeading>true</editHeading><label>Information</label>'
            f'<layoutColumns>{left}</layoutColumns><layoutColumns>{right}</layoutColumns>'
            '<style>TwoColumnsTopToBottom</style></layoutSections></Layout>')

members = "".join(f"<members>{n}</members>" for n in LAYOUTS)
pkg = ('<?xml version="1.0" encoding="UTF-8"?><Package xmlns="http://soap.sforce.com/2006/04/metadata">'
       f'<types>{members}<name>Layout</name></types><version>{API}</version></Package>')
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("package.xml", pkg)
    for name, (nb, fields) in LAYOUTS.items():
        z.writestr(f"layouts/{name}.layout", layout_xml(nb, fields))
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

print("deploying layouts...")
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
print("LAYOUTS DONE — refresh a Test Drive/Slot/Vehicle record to see all fields.")
