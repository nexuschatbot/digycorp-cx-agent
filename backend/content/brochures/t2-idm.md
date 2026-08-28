---
model_code: JET-T2IDM
name_en: Jetour T2 i-DM
name_ar: جيتور T2 i-DM
model_year: 2027
body_type: SUV
fuel: phev
variants: []
seats: "5 (2+3)"
source_pdf: t2-idm-compressed.pdf
source_pages: 3
spec_page: 3
converted: 2026-08-28
converted_by: T0.4
review_status: draft-partial
---

<!-- ported from t2-idm-compressed.pdf.

     PARTIAL. The specification page carries TWO configuration columns that the
     workbook does not name, and its left-hand column (dimensions, engine, motor,
     battery, range) does not survive extraction. Pages 1-2 are image-only and
     have not been read. What is below is what could be read with confidence;
     the gaps are listed explicitly at the end rather than filled in.
     `review_status: draft-partial`. -->

# Jetour T2 i-DM

The plug-in hybrid T2. A separate model from the **T2 Luxury Plus**, not a
variant of it — different powertrain, and the workbook prices them separately
(159,000 against 146,000).

**Five seats** in a 2+3 layout, and the most economical car in the range on
Elite's own figures: **18 km/L**.

## At a glance

| | |
|---|---|
| Powertrain | Plug-in hybrid (i-DM) |
| Seats | **5** (2+3) |
| Audio | **SONY, 12 speakers including a trunk subwoofer** |
| Fuel economy | **18 km/L** (5.5–6.7 L/100km) — best in range |
| Engine warranty | **10 years / 1,000,000 km** |
| Battery warranty | **8 years / 160,000 km** |
| Charging | Euro standard, fast and slow; Type G slow-charge gun |
| Model year | 2027 |

## نظرة سريعة

| | |
|---|---|
| نظام الدفع | هجين قابل للشحن (i-DM) |
| المقاعد | **5** (2+3) |
| الصوت | **سوني، 12 مكبر صوت مع مضخم في الصندوق** |
| استهلاك الوقود | **18 كم/لتر** — الأفضل في المجموعة |
| ضمان المحرك | **10 سنوات / مليون كيلومتر** |
| ضمان البطارية | **8 سنوات / 160,000 كم** |

---

## Where it sits in the range

**Most economical car Elite sells.** 18 km/L on the catalogue figure — ahead of
the T1 i-DM (15.8–18.5) and roughly 30% better than the X70 FL (13.7). Worth
noting the honest caveat: `seed/fuel_economy.json` records 18 as a single value
where the L/100km range (5.5–6.7) actually converts to about 14.9–18.2, so 18 is
the optimistic end. Quote it as "up to 18 km/L".

**Five seats, like the petrol T2.** Neither T2 is a seven-seater.

**Its audio matches the petrol T2** — SONY, 12 speakers — and adds a trunk
subwoofer, which no other brochure in the range mentions.

---

## Specifications

<!-- Read from the two-column configuration table on page 3. Where both columns
     agree the value is given once; the columns are NOT named in the workbook,
     which lists T2 i-DM as a single model with no variants. -->

### Seats

| | |
|---|---|
| Seat configuration | 2+3 |
| Seat material | PVC · Fabric + PVC (Dark Green) · PVC (Blue White) |
| Main seat adjustment | Electric 6-way + 4-way lumbar support |
| Auxiliary seat adjustment | Electric 4-way |
| Main and passenger seat function | Ventilated |
| Power seat memory | Driver's and passenger's seat |
| Sleep headrest | Front row |
| Second row adjustment | Manual angle adjustment, backrest folding |
| Rear seat reclining form | Pick a stack |

### Audio and multimedia

| | |
|---|---|
| **Brand audio** | **SONY** |
| **Number of speakers** | **12, including 1 trunk subwoofer** |
| Adjust volume with speed | Yes |
| Central audio and entertainment display | Touch panel |
| Voice-controlled region | Driver's seat + passenger's seat + rear seat |
| Multimedia / charging interface | USB |

### Charging

| | |
|---|---|
| Car-mounted slow charging gun | Euro standard |
| Charging port (fast + slow) | Euro standard |
| Slow-charge charging gun model | Type G |
| External discharge gun | Ecology |

### Climate and comfort

| | |
|---|---|
| Automatic air conditioning | Yes |
| Backseat air outlet | Yes |
| Temperature differential control | Yes |
| Air quality management system (AQS) | Yes |
| **Parking air conditioner** | Yes |
| Refrigerator | Yes |
| Bottle opener | Yes |
| Car picnic table | Yes |

### Lighting and glass

| | |
|---|---|
| Interior ambient light | Chromatic, changes with the music |
| Light show | Yes |
| LED tail light | Yes |
| Laser lamps | Yes |
| Headlight height adjustable | Yes |
| Headlights turn off late | Yes |
| Front / rear power windows | Yes / Yes |
| Window one-key lift | All windows |
| Window anti-clamping | Yes |
| Multi-layer soundproof glass | Front windshield + front doors and windows |
| Heat-insulating glass | Whole vehicle — windows, front and rear windshields, sunroof |
| Exterior mirrors | Electric adjust, heated, electric folding |
| Interior mirror | Auto-dimming |
| Wipers | Front, rear, sensor |
| Automatic defogging | Yes |

### Distinctive features

| | |
|---|---|
| Dynamic transparent car bottom | Yes |
| Intellectual dodge | Yes |
| Rear door light | Yes |

### Regulatory and warranty

| | |
|---|---|
| E-Call | Yes |
| Overspeed alarm | 120 ± 5 km/h, warns until speed drops below 120, sounds three times |
| **Engine warranty** | **10 years / 1,000,000 km** |
| **Battery warranty** | **8 years / 160,000 km** |
| Model year | 2027 |

---

## Not in this brochure

**Say "I don't have that" rather than estimating any of the following. This file
is deliberately incomplete and marked `draft-partial`.**

- **Dimensions, wheelbase, weight, ground clearance and luggage capacity.** The
  left-hand column of the spec page does not survive extraction and pages 1–2
  have not been read. **Do not borrow the T2 Luxury Plus's figures** — it is a
  different model with a different powertrain.
- **Engine, motor and battery specification.** No displacement, power, torque,
  battery capacity or electric-only range could be read. The G700's PHEV figures
  are not transferable.
- **The two configuration columns are unnamed.** The workbook lists T2 i-DM as
  one model with no variants, so this file does not invent trim names.
- Body and interior colours.
- Pricing — catalogue only (159,000).
