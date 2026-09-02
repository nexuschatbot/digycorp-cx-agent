"""Read-only finance tools (schema + thin fn) for the FINANCE sub-agent.

Thin wrappers over services/finance.py, in the house style: validate, call the
service in-process, return JSON. No business logic here — every caveat, every
refusal and the whole of Reg 29/2011 lives in the service, which is where the
tests for it already are.

SUB-AGENT TOOLS, NOT AISHA'S. These are bound through SUBAGENT_REGISTRY and are
deliberately absent from REGISTRY, so TOOL_SCHEMAS — and with it the single
agent's ~14.8k cached prefix (graph.py:431) — stays byte-stable while the
single-agent path is still the live one. Promote them into REGISTRY only
together with the prompt prose that tells Aisha when to call them.

DESCRIPTIONS ADDRESS A FACT-GATHERER, NOT A HOST. A sub-agent returns JSON to
Aisha and never speaks to the customer, so there is no rule-17 markdown
guidance here and no wording advice. What it needs instead is which id to pass,
and how to read a refusal — the service refuses far more often than it fails,
and a refusal carries the number that explains it.

EVERY ERROR IS AN ANSWER. `price_not_published` on a coming-soon car,
`tenure_exceeds_regulatory_cap` on 72 months, `down_payment_below_regulatory_
minimum` — none of these are failures to report as "something went wrong". They
are the finding, and they carry the figures Aisha needs to say why.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import finance

# --------------------------------------------------------------------------- #
# get_finance_options
# --------------------------------------------------------------------------- #
# WRAPS services/finance.options, WHICH HAD NO TOOL (2026-09-01). The finance
# card lists "which banks do you work with" as one of its own utterances, and
# its only tools required a variant_id — so the one financing question that
# needs NO car ("tell me the installment plans", asked cold, observed live
# three times in one morning) was unanswerable by construction and fell
# through to a callback offer.
GET_FINANCE_OPTIONS_SCHEMA = {
    "name": "get_finance_options",
    "description": (
        "The financing panel itself: which banks Elite works with, their "
        "indicative rates, minimum down payment, maximum tenure, and whether "
        "a Shariah-compliant option exists — plus the UAE regulatory caps "
        "that bound every plan. Use it when the customer asks about financing "
        "IN GENERAL and no specific car or trim is on the table yet: 'which "
        "banks do you work with', 'do you do Islamic finance', 'what are the "
        "installment plans', 'how does financing work'.\n\n"
        "NEEDS NO variant_id, and that is the point — it answers before a car "
        "is chosen. The moment the customer names a trim, get_finance_estimate "
        "is the tool for actual monthly figures; this one carries no monthly "
        "amount because there is no price to compute one from.\n\n"
        "Read the response:\n"
        "- 'banks' is one entry per bank: bank, rate_percent, islamic, "
        "min_down_payment_percent, max_tenure_months, is_sample.\n"
        "- 'regulatory_caps' carries the UAE Central Bank limits (maximum "
        "financed share, minimum down payment, maximum tenure, debt burden). "
        "They bound what any bank may offer and are safe to state.\n"
        "- 'all_rates_are_samples': true means every rate is illustrative. "
        "This is never a bank quote; say so."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def get_finance_options(db: Session, session_id: str, **_) -> dict:
    """The bank panel and regulatory caps. No inputs. Never raises."""
    return finance.options(db)


# --------------------------------------------------------------------------- #
# get_on_road_price
# --------------------------------------------------------------------------- #
GET_ON_ROAD_PRICE_SCHEMA = {
    "name": "get_on_road_price",
    "description": (
        "What one trim actually costs to put on the road: list price, "
        "first-year insurance and registration, itemised, plus the total. Use "
        "it when the customer asks what a car costs 'all in', 'on the road', "
        "'out the door', or what the extras on top of the list price are.\n\n"
        "TAKES A variant_id, NOT A MODEL id. A model has several trims at "
        "different prices, so there is no on-road price for 'the T2'. If you "
        "only have a model, the Sales agent's get_variants produces the "
        "variant_id — do not guess one.\n\n"
        "Read the response:\n"
        "- Success carries price_aed, insurance_aed, registration_aed and "
        "on_road_aed. Report all four; the itemisation is the answer to 'why "
        "is it more than the price you quoted'.\n"
        "- 'error': 'price_not_published' means the car is coming soon or has "
        "no published price. That is a FACT about the car, not a failure. "
        "Report it as such and never estimate a figure.\n"
        "- 'error': 'incomplete_on_road_price' means insurance or registration "
        "is missing, so no total can be given. The list price in the response "
        "is still true — report it and say the on-road total is unavailable. "
        "Never add up the parts you were given.\n"
        "- 'indicative': true is on every success. These are Elite's figures, "
        "not a quotation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "variant_id": {
                "type": "integer",
                "description": (
                    "The trim's id, from get_variants. Not a model/vehicle id."
                ),
            }
        },
        "required": ["variant_id"],
    },
}


def get_on_road_price(db: Session, session_id: str, *, variant_id=None, **_) -> dict:
    """List price + insurance + registration for one trim. Never raises."""
    if variant_id is None:
        return {
            "error": "missing_variant_id",
            "detail": "Pass the trim's variant_id, from get_variants.",
        }
    try:
        vid = int(variant_id)
    except (TypeError, ValueError):
        return {"error": "invalid_variant_id", "variant_id": variant_id}

    return finance.on_road_price(db, vid)


# --------------------------------------------------------------------------- #
# get_finance_estimate
# --------------------------------------------------------------------------- #
GET_FINANCE_ESTIMATE_SCHEMA = {
    "name": "get_finance_estimate",
    "description": (
        "Indicative monthly instalments for one trim, one option per bank Elite "
        "works with. Use it for 'what would the monthly payment be', 'can I "
        "finance it', 'do you do Islamic finance', or any question about "
        "instalments, down payment or tenure.\n\n"
        "TAKES A variant_id, NOT A MODEL id — see get_on_road_price. Pass only "
        "what the customer actually stated: leave down payment and tenure out "
        "and the defaults are the regulatory 20% down over 60 months.\n\n"
        "ALL BANKS COME BACK, not the cheapest, and that is deliberate — the "
        "Islamic option must stay visible to the customer who came for it. "
        "Report the range rather than silently picking one.\n\n"
        "Read the response:\n"
        "- 'options' is one entry per bank: bank, rate_percent, monthly_aed, "
        "total_payable_aed, cost_of_finance_aed, islamic, is_sample.\n"
        "- 'financed_aed' excludes insurance and registration — the monthly "
        "figure does not cover them, and those are carried separately so you "
        "can say so.\n"
        "- 'all_rates_are_samples': true means every rate is illustrative. "
        "This is ALWAYS an estimate and never a bank quote; say so.\n"
        "- 'subject_to' names the debt-burden check we cannot perform. It is a "
        "condition to state, never an assumption to make.\n"
        "- The refusals carry their own numbers and are the answer, not an "
        "error to hide: 'tenure_exceeds_regulatory_cap' (with max_months), "
        "'down_payment_below_regulatory_minimum' (with the shortfall in "
        "dirhams), 'no_financing_required' (the down payment covers the car), "
        "'price_not_published' (coming soon). Report the figure it gives you."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "variant_id": {
                "type": "integer",
                "description": (
                    "The trim's id, from get_variants. Not a model/vehicle id."
                ),
            },
            "down_payment_aed": {
                "type": "integer",
                "description": (
                    "A specific down payment in dirhams, only if the customer "
                    "named one. Wins over down_payment_percent."
                ),
            },
            "down_payment_percent": {
                "type": "number",
                "description": (
                    "A down payment as a percentage, only if the customer put "
                    "it that way. Omit both and the regulatory 20% is used."
                ),
            },
            "tenure_months": {
                "type": "integer",
                "description": (
                    "Loan term in months, only if the customer named one. The "
                    "regulatory maximum is 60; asking for more is refused, not "
                    "shortened."
                ),
            },
            "bank_id": {
                "type": "integer",
                "description": (
                    "One named bank only. Omit to get every bank, which is "
                    "almost always what you want."
                ),
            },
        },
        "required": ["variant_id"],
    },
}


def get_finance_estimate(
    db: Session,
    session_id: str,
    *,
    variant_id=None,
    down_payment_aed=None,
    down_payment_percent=None,
    tenure_months=None,
    bank_id=None,
    **_,
) -> dict:
    """Indicative EMI per bank for one trim. Never raises."""
    if variant_id is None:
        return {
            "error": "missing_variant_id",
            "detail": "Pass the trim's variant_id, from get_variants.",
        }
    try:
        vid = int(variant_id)
    except (TypeError, ValueError):
        return {"error": "invalid_variant_id", "variant_id": variant_id}

    def _int_or_none(value, field):
        if value is None:
            return None, None
        try:
            return int(value), None
        except (TypeError, ValueError):
            return None, {"error": f"invalid_{field}", field: value}

    down, err = _int_or_none(down_payment_aed, "down_payment_aed")
    if err:
        return err
    months, err = _int_or_none(tenure_months, "tenure_months")
    if err:
        return err
    bank, err = _int_or_none(bank_id, "bank_id")
    if err:
        return err

    percent = None
    if down_payment_percent is not None:
        try:
            percent = float(down_payment_percent)
        except (TypeError, ValueError):
            return {
                "error": "invalid_down_payment_percent",
                "down_payment_percent": down_payment_percent,
            }

    return finance.finance_estimate(
        db,
        vid,
        down_payment_aed=down,
        down_payment_percent=percent,
        tenure_months=months,
        bank_id=bank,
    )
