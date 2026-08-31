---
title_en: Promotions and offers
title_ar: العروض والتخفيضات
source: Elite Data.xlsx, "Promotions & Offers" sheet (4 offers)
converted: 2026-08-28
converted_by: T0.6
review_status: complete
all_validity_dates_are_samples: true
---

<!-- ported from the Promotions & Offers sheet.

     ⚠️ EVERY ONE OF THE FOUR IS MARKED "Sample - set actual end date" IN THE
     VALIDITY COLUMN. Elite supplied no real dates. So this file states no date,
     no month, no "this month", no "ends soon", and nothing that implies a
     deadline — not because dates are missing from the file, but because
     inventing urgency about money is the worst kind of thing to get wrong.

     THE SECOND HAZARD IS ARITHMETIC, NOT WORDING. Two of these offers change a
     PRICE: AED 3,000 off the on-road price, and 15% off first-year insurance.
     services/finance.py knows nothing about them and must not — an on-road price
     or an instalment quoted with a Sample discount silently baked in is a number
     nobody can reproduce, and it would be lower than the invoice. Promotions are
     named to the customer; they are never applied to a calculation. -->

# Promotions and offers

**None of these carries an end date, so never state one.** Elite marked all four
"Sample — set actual end date". Say what the offer is; say the showroom will
confirm it applies. Never say "this month", "ending soon", "while stocks last",
or anything else that manufactures a deadline.

**And never apply one to a price.** The instalments and on-road prices this
system quotes are computed from list data only. A discount folded silently into
a quote produces a number the customer cannot reproduce and the invoice will not
match.

| Offer | Type | What it is |
|---|---|---|
| **Ramadan Value Drive** | Price discount | AED 3,000 off the on-road price on select Jetour models |
| **Complete Care Bundle** | Free accessories | Free window tinting and floor mats with any new purchase |
| **Drive Protected** | Discounted insurance | 15% off the first-year comprehensive insurance premium |
| **Extended Peace of Mind** | Extended maintenance | 2 extra years of free scheduled maintenance, on top of the standard 3-year service contract |

## The details worth having

**Ramadan Value Drive** applies to "select Jetour models" — **which ones is not
stated.** Do not name a model as included or excluded; that is a showroom
question.

**Complete Care Bundle** is specifically window tinting and all-weather floor
mats — ACC-002 and ACC-005 in the accessories list, AED 900 and AED 450. **Do not
volunteer the total as a saving**: "AED 1,350 of accessories free" is our
arithmetic on a Sample offer, not Elite's claim.

**Drive Protected** discounts the *first-year comprehensive* premium by 15%. The
insurance figure in an on-road price is Elite's own, and this offer is not
deducted from it anywhere in this system.

**Extended Peace of Mind** adds two years of free scheduled maintenance to the
3-year / 30,000 km service contract in `warranty.md`. It does **not** extend the
warranty itself, despite the sheet's type column reading "Extended Warranty /
Free Maintenance" — the detail column says maintenance, and the detail is the
specific claim.

## بالعربية

**لا تذكر أي تاريخ انتهاء لهذه العروض.** جميعها مسجلة لدى إيليت كعروض نموذجية بلا
تواريخ محددة. اذكر العرض، وأن المعرض سيؤكد سريانه.

| العرض | النوع | التفاصيل |
|---|---|---|
| **رمضان فاليو درايف** | خصم على السعر | 3,000 درهم خصم على السعر شامل الطريق لموديلات مختارة |
| **باقة العناية الكاملة** | إكسسوارات مجانية | تظليل نوافذ ودعاسات أرضية مجاناً مع أي شراء |
| **درايف بروتكتد** | خصم تأمين | 15% خصم على قسط التأمين الشامل للسنة الأولى |
| **راحة بال ممتدة** | صيانة ممتدة | سنتان إضافيتان من الصيانة الدورية المجانية |

---

## Not in this file

**Say "I don't have that" rather than estimating any of the following.**

- **Any start or end date, for any offer.** There are none. This is the single
  most important line in this file.
- **Whether an offer is running right now.** "Sample" means Elite supplied the
  shape, not a confirmation that it is live. The showroom confirms.
- **Which models the Ramadan Value Drive covers.** "Select models" is all we have.
- **Whether the four can be combined.** Nothing says they can, and nothing says
  they cannot.
- **Whether a promotion applies to a specific variant, colour or branch.**
- **The cash value of any bundle.** The accessory prices are known; presenting
  their sum as "your saving" is our claim, not Elite's.
- **Whether financing rates in `Financing & EMI` are promotional.** Those are
  separately marked Sample; see the finance service.

<!-- 🟡 ONE THING FOR ELITE. "Ramadan Value Drive" is a seasonal name and the
     sheet gives it no dates at all, so nothing here can tell whether it is
     current, historic or illustrative. Aisha naming a Ramadan offer in August
     would be odd at best. Worth asking Elite for real validity windows before
     launch — and until then this file's no-dates rule is what keeps it safe. -->
