"""Finance service: on-road price and indicative monthly instalments (T0.8).

Two calculations and a great many caveats. Everything here is INDICATIVE — Elite
marked every rate they supplied "Sample", and `FinanceBank.is_sample` carries
that into the data so the caveat is driven by a column rather than by somebody
remembering. Nothing this module returns is a quotation; negotiation and a real
quote go to a human (rule 13).

THE THREE THINGS THIS MODULE REFUSES TO DO, each because doing it would produce
a confident number that is wrong:

1. It will not price an unpriced car. The G700 Top Fire is coming soon with no
   published price, and "coming soon" is not "AED 0". An EMI on a null price is
   the exact failure `price_aed` was made nullable to make visible.
2. It will not quietly clamp a request past a regulatory cap. Asked for 72
   months, it REFUSES and names the 60-month maximum rather than returning a
   60-month figure the customer did not ask for and would not notice. The
   refusal carries the cap so the agent can explain it in one sentence.
3. It will not sum a partial on-road price. Missing insurance would produce a
   total BELOW the list price and read as a discount, so it is all three
   components or nothing — the same rule `VehicleVariant.on_road_aed` enforces.

WHAT IS FINANCED, AND WHY IT IS THE LIST PRICE. UAE Central Bank Regulation
29/2011 caps a car loan at 80% of "the value of the car". The value of the car is
the vehicle price — insurance and registration are costs of putting it on the
road, not part of the asset. So the 20% down payment is computed against
`price_aed` and the customer pays insurance and registration alongside. Elite's
sheet says "80% financing / 20% customer down payment" without naming the base;
this reading follows the regulation rather than the sheet's silence, and it is
worth confirming with them because it moves the instalment by roughly AED 110 a
month on a G700 Flagship.

WHAT THE REGULATION SAYS AND WHAT WE CAN ACTUALLY ENFORCE. Reg 29/2011 sets
three limits: at most 80% financed, at most 60 months, and a debt-burden ratio
of at most 50% of gross monthly income. The first two are arithmetic and are
enforced below. THE THIRD CANNOT BE: we never know the customer's income, and
asking for it in a sales chat is neither appropriate nor something we would
store. So DBR is surfaced as a stated condition of the estimate, never silently
assumed satisfied — the bank checks it, and the customer must hear that.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FinanceBank, VehicleVariant

# --------------------------------------------------------------------------- #
# UAE Central Bank Regulation 29/2011
# --------------------------------------------------------------------------- #
# Named constants rather than literals sprinkled through the arithmetic: these
# are law, they are quoted to customers, and when one changes it must change in
# exactly one place.
MAX_FINANCED_PERCENT = 80.0
MIN_DOWN_PAYMENT_PERCENT = 100.0 - MAX_FINANCED_PERCENT  # 20%
MAX_TENURE_MONTHS = 60
# Enforced by the BANK, not by us — see the module docstring.
MAX_DEBT_BURDEN_PERCENT = 50.0

DEFAULT_TENURE_MONTHS = 60


def _variant_brief(v: VehicleVariant) -> dict:
    return {
        "variant_id": v.id,
        "model_id": v.model_id,
        "model_name": v.model.name if v.model else None,
        "variant_name": v.name,
        "coming_soon": bool(v.coming_soon),
    }


# --------------------------------------------------------------------------- #
# On-road price
# --------------------------------------------------------------------------- #
def on_road_price(db: Session, variant_id: int) -> dict:
    """List price + insurance + registration, itemised.

    ITEMISED ON PURPOSE. A single total invites "why is it more than the price
    you said?", and the answer — first-year insurance and registration — is the
    whole content of the question. The agent should be able to read the three
    lines out, so it returns three lines.
    """
    v = db.get(VehicleVariant, variant_id)
    if v is None:
        return {"error": "variant_not_found", "variant_id": variant_id}

    if v.coming_soon or v.price_aed is None:
        # NOT an error the agent should apologise for — it is a fact about the
        # car, and the honest reply is "it's coming soon, we don't have pricing
        # yet", never a number.
        return {
            "error": "price_not_published",
            "variant": _variant_brief(v),
            "reason": "coming_soon" if v.coming_soon else "no_published_price",
        }

    total = v.on_road_aed
    if total is None:
        # Priced, but insurance or registration is missing. Refusing beats
        # summing what we have: a partial total lands BELOW the list price and
        # reads as a discount.
        return {
            "error": "incomplete_on_road_price",
            "variant": _variant_brief(v),
            "price_aed": v.price_aed,
            "insurance_aed": v.insurance_aed,
            "registration_aed": v.registration_aed,
        }

    return {
        "variant": _variant_brief(v),
        "price_aed": v.price_aed,
        "insurance_aed": v.insurance_aed,
        "registration_aed": v.registration_aed,
        "on_road_aed": total,
        # Elite's figures, indicative like everything else here.
        "indicative": True,
    }


# --------------------------------------------------------------------------- #
# EMI
# --------------------------------------------------------------------------- #
def monthly_instalment(principal: int, annual_rate_percent: float, months: int) -> int:
    """Reducing-balance (annuity) instalment, rounded to the nearest dirham.

        EMI = P * r / (1 - (1 + r)^-n)        r = annual rate / 12

    REDUCING BALANCE, NOT FLAT RATE, and the difference is not academic: a flat
    3.49% on 135,200 over 60 months gives about AED 2,647 a month, the annuity
    about AED 2,459 — nearly AED 190 a month, or AED 11,000 over the term. Both
    are "3.49%". UAE banks advertise reducing-balance rates, and Elite's sheet
    says so in the column heading, so that is what is computed.

    A ZERO RATE IS HANDLED SEPARATELY, not because we have one today but because
    the formula divides by zero at r = 0 and a promotional 0% offer is exactly
    the kind of thing that turns up mid-campaign.
    """
    if months <= 0:
        raise ValueError("months must be positive")
    r = annual_rate_percent / 100.0 / 12.0
    if r == 0:
        return round(principal / months)
    return round(principal * r / (1 - (1 + r) ** -months))


def _option(bank: FinanceBank, principal: int, months: int) -> dict:
    emi = monthly_instalment(principal, bank.rate_percent or 0.0, months)
    # Totals derived from the ROUNDED instalment, deliberately. A customer
    # multiplying the monthly figure by the term must get the total we printed;
    # a more precise total that does not match their arithmetic reads as a
    # hidden fee.
    total_payable = emi * months
    return {
        "bank_id": bank.id,
        "bank": bank.name,
        "islamic": bool(bank.islamic),
        # "profit rate" for the Islamic product is the sheet's own wording; the
        # arithmetic is identical, which is why one function serves both.
        "rate_percent": bank.rate_percent,
        "monthly_aed": emi,
        "total_payable_aed": total_payable,
        "cost_of_finance_aed": total_payable - principal,
        "is_sample": bool(bank.is_sample),
    }


def finance_estimate(
    db: Session,
    variant_id: int,
    *,
    down_payment_aed: int | None = None,
    down_payment_percent: float | None = None,
    tenure_months: int | None = None,
    bank_id: int | None = None,
) -> dict:
    """An indicative instalment per bank, or for one named bank.

    ALL BANKS BY DEFAULT, not the cheapest. "Which banks do you work with" and
    "do you do Islamic finance" are both common questions, and answering with a
    single silently-chosen rate hides the Shariah-compliant option from the
    customer who came for it.

    down_payment_aed wins over down_payment_percent when both are given: the
    customer who says "I can put down 40,000" has been more specific than the
    default, and honouring the vaguer input would answer a question they did not
    ask.
    """
    v = db.get(VehicleVariant, variant_id)
    if v is None:
        return {"error": "variant_not_found", "variant_id": variant_id}

    if v.coming_soon or v.price_aed is None:
        return {
            "error": "price_not_published",
            "variant": _variant_brief(v),
            "reason": "coming_soon" if v.coming_soon else "no_published_price",
        }

    price = v.price_aed
    months = DEFAULT_TENURE_MONTHS if tenure_months is None else int(tenure_months)
    if months <= 0:
        return {"error": "invalid_tenure", "requested_months": months}
    if months > MAX_TENURE_MONTHS:
        # REFUSE, DO NOT CLAMP. See the module docstring: a quietly shortened
        # term answers a question the customer did not ask, and they would not
        # notice. The cap travels with the refusal so the agent can say why.
        return {
            "error": "tenure_exceeds_regulatory_cap",
            "requested_months": months,
            "max_months": MAX_TENURE_MONTHS,
            "regulation": "UAE Central Bank Regulation 29/2011",
        }

    if down_payment_aed is not None:
        down = int(down_payment_aed)
    elif down_payment_percent is not None:
        down = round(price * float(down_payment_percent) / 100.0)
    else:
        down = round(price * MIN_DOWN_PAYMENT_PERCENT / 100.0)

    if down < 0:
        return {"error": "invalid_down_payment", "down_payment_aed": down}
    if down >= price:
        # Nothing left to finance. Not an error in the customer's world — they
        # are paying cash — so it is named rather than dressed as a failure.
        return {
            "error": "no_financing_required",
            "variant": _variant_brief(v),
            "price_aed": price,
            "down_payment_aed": down,
        }

    down_percent = down / price * 100.0
    if down_percent < MIN_DOWN_PAYMENT_PERCENT:
        # Same refusal-not-clamp rule. Quoting a legal 20% instalment to someone
        # who asked about 10% would look like an answer and be a different
        # question — and the shortfall in dirhams is the thing they need to hear.
        minimum = -(-price * MIN_DOWN_PAYMENT_PERCENT // 100)  # ceil, in AED
        return {
            "error": "down_payment_below_regulatory_minimum",
            "variant": _variant_brief(v),
            "price_aed": price,
            "requested_down_payment_aed": down,
            "minimum_down_payment_aed": int(minimum),
            "minimum_down_payment_percent": MIN_DOWN_PAYMENT_PERCENT,
            "shortfall_aed": int(minimum) - down,
            "regulation": "UAE Central Bank Regulation 29/2011",
        }

    if bank_id is not None:
        bank = db.get(FinanceBank, bank_id)
        if bank is None:
            return {"error": "bank_not_found", "bank_id": bank_id}
        banks = [bank]
    else:
        banks = list(db.scalars(select(FinanceBank).order_by(FinanceBank.id)).all())
    if not banks:
        return {"error": "no_finance_options"}

    financed = price - down
    options = [_option(b, financed, months) for b in banks]

    return {
        "variant": _variant_brief(v),
        "price_aed": price,
        "down_payment_aed": down,
        "down_payment_percent": round(down_percent, 1),
        "financed_aed": financed,
        "tenure_months": months,
        "options": options,
        # Insurance and registration are NOT in `financed` — see the module
        # docstring on what the 80% is measured against. Carried so the agent
        # can say what the monthly figure does and does not cover.
        "insurance_aed": v.insurance_aed,
        "registration_aed": v.registration_aed,
        "on_road_aed": v.on_road_aed,
        # STRUCTURAL, not prose. The agent's caveat is driven by these rather
        # than by a rule it might forget under a long conversation.
        "indicative": True,
        "all_rates_are_samples": all(o["is_sample"] for o in options),
        # The cap we cannot check. Stated as a condition, never assumed met.
        "subject_to": {
            "debt_burden_ratio_max_percent": MAX_DEBT_BURDEN_PERCENT,
            "note": (
                "Bank approval, income verification and a debt-burden ratio "
                "check are the bank's, not ours."
            ),
        },
    }


def options(db: Session) -> dict:
    """The financing panel itself — "which banks do you work with"."""
    banks = list(db.scalars(select(FinanceBank).order_by(FinanceBank.id)).all())
    return {
        "banks": [
            {
                "bank_id": b.id,
                "bank": b.name,
                "islamic": bool(b.islamic),
                "rate_percent": b.rate_percent,
                "min_down_payment_percent": b.min_down_payment_percent,
                "max_tenure_months": b.max_tenure_months,
                "is_sample": bool(b.is_sample),
            }
            for b in banks
        ],
        "regulatory_caps": {
            "max_financed_percent": MAX_FINANCED_PERCENT,
            "min_down_payment_percent": MIN_DOWN_PAYMENT_PERCENT,
            "max_tenure_months": MAX_TENURE_MONTHS,
            "max_debt_burden_percent": MAX_DEBT_BURDEN_PERCENT,
            "regulation": "UAE Central Bank Regulation 29/2011",
        },
        "indicative": True,
        "all_rates_are_samples": all(bool(b.is_sample) for b in banks),
    }
