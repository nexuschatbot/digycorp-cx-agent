"""Finance service: on-road price and indicative instalments (T0.8).

The arithmetic is checked INDEPENDENTLY rather than by restating the formula —
a test that re-implements the code it tests proves only that the author typed
the same thing twice. Here the instalment is verified by running the
amortisation schedule month by month and asserting the balance actually reaches
zero, which is what "reducing balance" means and is true of no wrong formula.

Everything else in this file is about the module refusing to produce a
confident number it has no right to.

Offline: the seeded `db`, no vendor, no model.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import FinanceBank, VehicleVariant
from app.services import finance


def _variant(db, model_code: str, name: str | None):
    from app.models import VehicleModel

    m = db.scalar(select(VehicleModel).where(VehicleModel.model_code == model_code))
    return db.scalar(
        select(VehicleVariant).where(
            VehicleVariant.model_id == m.id, VehicleVariant.name == name
        )
    )


# --------------------------------------------------------------------------- #
# The arithmetic
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "principal,rate,months",
    [(192_360, 3.49, 60), (55_200, 3.99, 60), (91_200, 3.75, 48), (100_000, 3.49, 12)],
)
def test_the_instalment_actually_amortises_the_loan(principal, rate, months):
    """INDEPENDENT CHECK. Run the schedule: each month the balance grows by the
    monthly rate and shrinks by the instalment. If the instalment is right, the
    balance lands on zero at the end of the term.

    A flat-rate figure overshoots and leaves the balance deeply negative; a
    simple-interest one undershoots and leaves it positive. Only the annuity
    lands.
    """
    emi = finance.monthly_instalment(principal, rate, months)
    r = rate / 100 / 12
    balance = float(principal)
    for _ in range(months):
        balance = balance * (1 + r) - emi
    # Tolerance is the rounding of the instalment carried across the term, not
    # slack: emi is whole dirhams, so at most 0.5 AED a month compounds forward.
    assert abs(balance) < months, f"balance {balance:.2f} after {months} months"


def test_reducing_balance_is_not_flat_rate():
    """The distinction that makes this module worth having.

    A flat 3.49% on 135,200 over 60 months is about AED 2,647 a month; the
    reducing-balance instalment is about 2,459 — nearly AED 190 a month and
    AED 11,000 over the term. Both are honestly called "3.49%". UAE banks quote
    reducing balance, and Elite's column heading says so.
    """
    principal, rate, months = 135_200, 3.49, 60
    flat = round((principal + principal * rate / 100 * months / 12) / months)
    annuity = finance.monthly_instalment(principal, rate, months)
    assert annuity < flat
    assert flat - annuity > 150, "the two must not have quietly converged"


def test_a_zero_rate_does_not_divide_by_zero():
    """No 0% offer today, but the formula divides by zero at r = 0 and a
    promotional rate is exactly what turns up mid-campaign."""
    assert finance.monthly_instalment(60_000, 0.0, 60) == 1_000


def test_the_totals_multiply_out_to_what_the_customer_was_told(db):
    """A customer multiplying the monthly figure by the term must land on the
    total we printed. A more precise total that misses reads as a hidden fee."""
    v = _variant(db, "JET-G700", "Flagship")
    out = finance.finance_estimate(db, v.id)
    for opt in out["options"]:
        assert opt["monthly_aed"] * out["tenure_months"] == opt["total_payable_aed"]
        assert (
            opt["total_payable_aed"] - out["financed_aed"]
            == opt["cost_of_finance_aed"]
        )


# --------------------------------------------------------------------------- #
# What it refuses to price
# --------------------------------------------------------------------------- #
def test_a_coming_soon_car_gets_no_instalment_and_no_on_road_price(db):
    """The G700 Top Fire has no published price. An EMI on a null price is the
    exact failure `price_aed` was made nullable to make visible — and "coming
    soon" is a different sentence from "out of stock" or "AED 0"."""
    v = _variant(db, "JET-G700", "Top Fire")
    for out in (finance.finance_estimate(db, v.id), finance.on_road_price(db, v.id)):
        assert out["error"] == "price_not_published"
        assert out["reason"] == "coming_soon"
        assert "monthly_aed" not in str(out)


def test_an_unknown_variant_is_named_not_guessed(db):
    assert finance.finance_estimate(db, 9999)["error"] == "variant_not_found"
    assert finance.on_road_price(db, 9999)["error"] == "variant_not_found"


# --------------------------------------------------------------------------- #
# The regulatory caps — refuse, never clamp
# --------------------------------------------------------------------------- #
def test_a_tenure_past_60_months_is_refused_with_the_cap_attached(db):
    """NOT CLAMPED TO 60. A customer asking about 7 years who receives a 5-year
    figure has been answered a question they did not ask, and would not notice.
    The cap travels with the refusal so the agent can explain it in a sentence.
    """
    v = _variant(db, "JET-T1", "Comfort")
    out = finance.finance_estimate(db, v.id, tenure_months=72)
    assert out["error"] == "tenure_exceeds_regulatory_cap"
    assert out["requested_months"] == 72
    assert out["max_months"] == 60
    assert "29/2011" in out["regulation"]
    assert "options" not in out, "a refusal must not also quote instalments"


def test_a_down_payment_below_20_percent_is_refused_with_the_shortfall(db):
    """Reg 29/2011 caps the loan at 80% of the car's value. The shortfall in
    dirhams is the thing the customer needs to hear, so it is computed here
    rather than left for the agent to work out."""
    v = _variant(db, "JET-X50", "Premium")  # 69,000
    out = finance.finance_estimate(db, v.id, down_payment_aed=5_000)
    assert out["error"] == "down_payment_below_regulatory_minimum"
    assert out["minimum_down_payment_aed"] == 13_800  # 20% of 69,000
    assert out["shortfall_aed"] == 8_800
    assert "options" not in out


def test_paying_the_whole_price_is_named_not_treated_as_an_error(db):
    """They are paying cash. That is not a failure, and the wording must not
    make them feel it was."""
    v = _variant(db, "JET-X50", "Premium")
    out = finance.finance_estimate(db, v.id, down_payment_aed=69_000)
    assert out["error"] == "no_financing_required"


# --------------------------------------------------------------------------- #
# Defaults and inputs
# --------------------------------------------------------------------------- #
def test_the_default_is_20_percent_down_over_60_months(db):
    v = _variant(db, "JET-X50", "Premium")
    out = finance.finance_estimate(db, v.id)
    assert out["down_payment_aed"] == 13_800
    assert out["down_payment_percent"] == 20.0
    assert out["tenure_months"] == 60
    assert out["financed_aed"] == 55_200


def test_an_explicit_dirham_amount_beats_a_percentage(db):
    """The customer who says "I can put down 40,000" has been more specific than
    the default; honouring the vaguer input answers a question they did not ask.
    """
    v = _variant(db, "JET-T1", "Luxury")  # 129,000
    out = finance.finance_estimate(
        db, v.id, down_payment_aed=40_000, down_payment_percent=25
    )
    assert out["down_payment_aed"] == 40_000
    assert out["financed_aed"] == 89_000


def test_the_financed_amount_is_the_list_price_not_the_on_road_price(db):
    """Reg 29/2011 caps the loan at 80% of "the value of the car" — the vehicle,
    not the cost of putting it on the road. Insurance and registration are
    returned alongside so the agent can say what the instalment does not cover.

    WORTH CONFIRMING WITH ELITE: their sheet says "80% financing" without naming
    the base, and the choice moves a G700 Flagship instalment by ~AED 110/month.
    """
    v = _variant(db, "JET-G700", "Flagship")
    out = finance.finance_estimate(db, v.id)
    assert out["price_aed"] == 240_450
    assert out["financed_aed"] == 240_450 - out["down_payment_aed"]
    assert out["on_road_aed"] == 246_450
    assert out["financed_aed"] < out["on_road_aed"]
    assert out["insurance_aed"] == 5_000
    assert out["registration_aed"] == 1_000


# --------------------------------------------------------------------------- #
# Banks
# --------------------------------------------------------------------------- #
def test_every_bank_is_quoted_by_default_including_the_islamic_one(db):
    """NOT the cheapest, silently. "Do you do Islamic finance" is a question
    customers arrive with, and a single auto-chosen rate hides the answer."""
    v = _variant(db, "JET-T1", "Comfort")
    out = finance.finance_estimate(db, v.id)
    assert len(out["options"]) == 3
    assert any(o["islamic"] for o in out["options"])
    assert {o["bank"] for o in out["options"]} == {
        "Emirates NBD", "Mashreq Bank", "Dubai Islamic Bank"
    }


def test_a_named_bank_narrows_to_one_option(db):
    v = _variant(db, "JET-T1", "Comfort")
    dib = db.scalar(select(FinanceBank).where(FinanceBank.islamic.is_(True)))
    out = finance.finance_estimate(db, v.id, bank_id=dib.id)
    assert [o["bank"] for o in out["options"]] == ["Dubai Islamic Bank"]


def test_a_higher_rate_costs_more_every_month(db):
    """Sanity on the ordering: the three sample rates are 3.49 / 3.75 / 3.99."""
    v = _variant(db, "JET-T1", "Comfort")
    opts = finance.finance_estimate(db, v.id)["options"]
    by_rate = sorted(opts, key=lambda o: o["rate_percent"])
    monthly = [o["monthly_aed"] for o in by_rate]
    assert monthly == sorted(monthly)


# --------------------------------------------------------------------------- #
# The caveat is structural, not remembered
# --------------------------------------------------------------------------- #
def test_every_answer_carries_the_indicative_flag_and_the_sample_marking(db):
    """Elite marked all three rates "Sample". The agent's caveat is driven by
    these fields rather than by a prompt rule it might drop in a long thread."""
    v = _variant(db, "JET-T1", "Comfort")
    est = finance.finance_estimate(db, v.id)
    assert est["indicative"] is True
    assert est["all_rates_are_samples"] is True
    assert all(o["is_sample"] for o in est["options"])
    assert finance.on_road_price(db, v.id)["indicative"] is True
    assert finance.options(db)["indicative"] is True


def test_the_debt_burden_cap_is_stated_as_a_condition_not_assumed_met(db):
    """We never know the customer's income and must not ask for it in a sales
    chat, so DBR cannot be checked here. It is surfaced as the bank's condition
    rather than silently treated as satisfied."""
    v = _variant(db, "JET-T1", "Comfort")
    out = finance.finance_estimate(db, v.id)
    assert out["subject_to"]["debt_burden_ratio_max_percent"] == 50.0
    assert "bank" in out["subject_to"]["note"].lower()


def test_the_options_endpoint_publishes_the_regulatory_caps(db):
    caps = finance.options(db)["regulatory_caps"]
    assert caps["max_financed_percent"] == 80.0
    assert caps["min_down_payment_percent"] == 20.0
    assert caps["max_tenure_months"] == 60
    assert "29/2011" in caps["regulation"]


# --------------------------------------------------------------------------- #
# On-road price
# --------------------------------------------------------------------------- #
def test_the_on_road_price_is_itemised_and_adds_up(db):
    """Itemised because "why is it more than the price you said?" is the next
    question, and the three lines are the whole answer."""
    v = _variant(db, "JET-X50", "Premium")
    out = finance.on_road_price(db, v.id)
    assert (out["price_aed"], out["insurance_aed"], out["registration_aed"]) == (
        69_000, 1_600, 1_000
    )
    assert out["on_road_aed"] == 71_600
    assert out["on_road_aed"] == sum(
        (out["price_aed"], out["insurance_aed"], out["registration_aed"])
    )


def test_a_partial_on_road_price_is_refused_rather_than_summed(db):
    """A total missing insurance lands BELOW the list price and reads as a
    discount. All three components or nothing."""
    v = _variant(db, "JET-X50", "Premium")
    v.insurance_aed = None
    db.flush()
    out = finance.on_road_price(db, v.id)
    assert out["error"] == "incomplete_on_road_price"
    assert "on_road_aed" not in out


def test_the_bank_panel_is_reachable_as_a_tool_with_no_car(db):
    """finance.options had no tool while the finance card claimed "which banks
    do you work with" as its own utterance — so the one financing question a
    cold session can ask ("tell me the installment plans", observed live three
    times in one morning, 2026-09-01) was unanswerable by construction."""
    from app.tools.finance_tools import get_finance_options

    out = get_finance_options(db, "s")
    assert [b["bank"] for b in out["banks"]]
    assert "max_tenure_months" in out["regulatory_caps"]
    assert out["indicative"] is True
