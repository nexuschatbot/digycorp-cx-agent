#!/usr/bin/env python3
"""Create the 'Test Drive Agent Feedback' custom object (feedback on Aisha's chat,
captured when the conversation closes): object + fields + tab + add to the Elite
Test Drives app + layout + 'All' list view + FLS/CRUD on the Elite_Integration
permission set. Env: SF_DOMAIN, SF_CK, SF_CS."""
import base64, io, json, os, re, sys, time, zipfile
import urllib.request, urllib.parse, urllib.error

DOMAIN = os.environ["SF_DOMAIN"].rstrip("/"); CK = os.environ["SF_CK"]; CS = os.environ["SF_CS"]; API = "62.0"
OBJ = "Test_Drive_Agent_Feedback__c"
LAYOUT = f"{OBJ}-Test Drive Agent Feedback Layout"

# (api_name, label, xml_type_fragment)
FIELDS = [
    ("Client_Name__c", "Client Name", "<type>Text</type><length>255</length>"),
    ("Mobile__c", "Mobile", "<type>Phone</type>"),
    ("Star_Rating__c", "Star Rating", "<type>Number</type><precision>2</precision><scale>0</scale>"),
    ("Booking_Ref__c", "Booking Id", "<type>Text</type><length>32</length>"),
    ("Feedback_At__c", "Feedback Date/Time", "<type>DateTime</type>"),
    ("Comments__c", "Comments", "<type>LongTextArea</type><length>2000</length><visibleLines>4</visibleLines>"),
    ("Contact__c", "Contact",
     "<type>Lookup</type><referenceTo>Contact</referenceTo>"
     "<relationshipLabel>Agent Feedback</relationshipLabel><relationshipName>Agent_Feedback</relationshipName>"),
]
LAYOUT_ORDER = ["Name", "Client_Name__c", "Mobile__c", "Star_Rating__c",
                "Booking_Ref__c", "Feedback_At__c", "Contact__c", "Comments__c"]
LV_COLS = ["NAME", "Client_Name__c", "Mobile__c", "Star_Rating__c", "Booking_Ref__c", "Feedback_At__c"]


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
MS = f"{IU}/services/Soap/m/{API}"; DATA = f"{IU}/services/data/v{API}/"


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


def deploy(files, members_by_type, what):
    pkg_types = "".join(
        "<types>" + "".join(f"<members>{m}</members>" for m in mem) + f"<name>{t}</name></types>"
        for t, mem in members_by_type.items())
    pkg = ('<?xml version="1.0" encoding="UTF-8"?><Package xmlns="http://soap.sforce.com/2006/04/metadata">'
           f'{pkg_types}<version>{API}</version></Package>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", pkg)
        for path, content in files.items():
            z.writestr(path, content)
    zb = base64.b64encode(buf.getvalue()).decode()
    st, b = soap(f"<met:deploy><met:ZipFile>{zb}</met:ZipFile><met:DeployOptions>"
                 "<met:singlePackage>true</met:singlePackage><met:rollbackOnError>true</met:rollbackOnError>"
                 "</met:DeployOptions></met:deploy>")
    aid = tag(b, "id"); status = None
    for _ in range(48):
        time.sleep(5)
        st, b = soap(f"<met:checkDeployStatus><met:asyncProcessId>{aid}</met:asyncProcessId>"
                     "<met:includeDetails>true</met:includeDetails></met:checkDeployStatus>")
        if tag(b, "done") == "true": status = tag(b, "status"); break
    print(f"{what} deploy:", status)
    if status != "Succeeded":
        for m in re.findall(r"<(?:\w+:)?componentFailures>(.*?)</(?:\w+:)?componentFailures>", b, re.S):
            print("  FAIL", tag(m, "fullName"), "->", tag(m, "problem"))
        return False
    return True


# ---- object + fields + list view ----------------------------------------- #
fields_xml = "".join(
    f"<fields><fullName>{n}</fullName><label>{lbl}</label>{frag}</fields>" for n, lbl, frag in FIELDS)
lv_xml = ("<listViews><fullName>All</fullName><label>All</label><filterScope>Everything</filterScope>"
          + "".join(f"<columns>{c}</columns>" for c in LV_COLS) + "</listViews>")
obj_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">'
           '<label>Test Drive Agent Feedback</label><pluralLabel>Test Drive Agent Feedback</pluralLabel>'
           '<nameField><label>Feedback #</label><type>AutoNumber</type><displayFormat>AF-{0000}</displayFormat></nameField>'
           '<deploymentStatus>Deployed</deploymentStatus><sharingModel>ReadWrite</sharingModel>'
           f'{fields_xml}{lv_xml}</CustomObject>')

# ---- tab ----------------------------------------------------------------- #
tab_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<CustomTab xmlns="http://soap.sforce.com/2006/04/metadata">'
           '<customObject>true</customObject><motif>Custom51: Hexagon</motif></CustomTab>')

# ---- layout -------------------------------------------------------------- #
def li(f): return f"<layoutItems><behavior>{'Readonly' if f=='Name' else 'Edit'}</behavior><field>{f}</field></layoutItems>"
mid = (len(LAYOUT_ORDER) + 1) // 2
layout_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
              '<Layout xmlns="http://soap.sforce.com/2006/04/metadata"><layoutSections>'
              '<editHeading>true</editHeading><label>Feedback</label>'
              f'<layoutColumns>{"".join(li(f) for f in LAYOUT_ORDER[:mid])}</layoutColumns>'
              f'<layoutColumns>{"".join(li(f) for f in LAYOUT_ORDER[mid:])}</layoutColumns>'
              '<style>TwoColumnsTopToBottom</style></layoutSections></Layout>')

# ---- app (Elite Test Drives) with the new tab added ---------------------- #
app_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<CustomApplication xmlns="http://soap.sforce.com/2006/04/metadata">'
           '<label>Elite Test Drives</label><navType>Standard</navType><uiType>Lightning</uiType>'
           '<formFactors>Large</formFactors>'
           '<tabs>Test_Drive__c</tabs><tabs>Slot__c</tabs><tabs>Vehicle__c</tabs>'
           '<tabs>Showroom__c</tabs><tabs>Test_Drive_Feedback__c</tabs>'
           f'<tabs>{OBJ}</tabs><tabs>standard-Contact</tabs>'
           '</CustomApplication>')

files = {
    f"objects/{OBJ}.object": obj_xml,
    f"tabs/{OBJ}.tab": tab_xml,
    f"layouts/{LAYOUT}.layout": layout_xml,
    "applications/Elite_Test_Drives.app": app_xml,
}
members = {
    "CustomObject": [OBJ],
    "CustomTab": [OBJ],
    "Layout": [LAYOUT],
    "CustomApplication": ["Elite_Test_Drives"],
}
if not deploy(files, members, "object+tab+layout+app"):
    sys.exit(1)

# ---- CRUD + FLS on the integration permission set ------------------------ #
psid = json.loads(_req("GET", DATA + "query/?q=" + urllib.parse.quote(
    "SELECT Id FROM PermissionSet WHERE Name='Elite_Integration'"), AT)[1])["records"][0]["Id"]
st, b = _req("POST", DATA + "sobjects/ObjectPermissions", AT, json_body={
    "ParentId": psid, "SobjectType": OBJ,
    "PermissionsRead": True, "PermissionsCreate": True, "PermissionsEdit": True,
    "PermissionsDelete": True, "PermissionsViewAllRecords": True, "PermissionsModifyAllRecords": True})
print("object CRUD:", "ok" if st in (200, 201) or "DUPLICATE" in b else b[:160])
for n, lbl, frag in FIELDS:
    st, b = _req("POST", DATA + "sobjects/FieldPermissions", AT, json_body={
        "ParentId": psid, "SobjectType": OBJ, "Field": f"{OBJ}.{n}",
        "PermissionsRead": True, "PermissionsEdit": True})
    print(f"FLS {n}:", "ok" if st in (200, 201) or "DUPLICATE" in b else b[:120])
print("DONE")
print("Tab URL:", IU.replace('.my.salesforce.com', '.lightning.force.com') + f"/lightning/o/{OBJ}/list")
