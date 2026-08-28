"""The four knowledge MD files, and the rules they exist to hold (T0.6).

These are prose, so most of what makes them good cannot be tested. Three things
can, and each is a rule that would otherwise live only in someone's memory:

  1. NO PROMOTION MAY CARRY A DATE. Elite marked all four "Sample - set actual
     end date" and supplied none. Manufactured urgency about money is the worst
     kind of thing to get wrong, so the absence is asserted rather than trusted.
  2. THE WARRANTY TRAP MUST STAY DOCUMENTED. "10 years / 1,000,000 km" is the
     whole vehicle on an X50 and the ENGINE ALONE on a G700, whose vehicle cover
     is 8 years / 300,000 km. A file that lost that warning would read as correct.
  3. THE DELIVERY CAVEAT MUST TRAVEL WITH THE NUMBER. 21 days is for a car in
     stock; a factory order adds 4-8 weeks.

Offline: reads files, nothing else.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

KNOWLEDGE = Path(__file__).resolve().parent.parent / "content" / "knowledge"
FILES = ("warranty.md", "delivery.md", "registration.md", "promotions.md")


def _read(name: str) -> str:
    return (KNOWLEDGE / name).read_text(encoding="utf-8")


def _frontmatter(name: str) -> dict:
    text = _read(name)
    assert text.startswith("---\n"), f"{name} has no frontmatter"
    _, fm, _ = text.split("---\n", 2)
    return yaml.safe_load(fm)


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", FILES)
def test_each_file_exists_with_parseable_frontmatter(name):
    fm = _frontmatter(name)
    assert fm["title_en"] and fm["title_ar"]
    assert "Data.xlsx" in fm["source"], "provenance must name the sheet it came from"
    assert fm["review_status"] == "complete"


@pytest.mark.parametrize("name", FILES)
def test_each_file_states_what_it_does_not_know(name):
    """The same discipline as the brochures: an absence recorded up front is a
    fact the agent can act on; an absence discovered mid-conversation is a guess
    waiting to happen."""
    text = _read(name)
    assert "## Not in this file" in text
    body = text.split("## Not in this file", 1)[1]
    assert body.count("\n- ") >= 4, f"{name} names too few gaps to be honest"


@pytest.mark.parametrize("name", FILES)
def test_each_file_carries_arabic(name):
    """LOC-01: these are read out to customers, and half of them in Arabic."""
    text = _read(name)
    assert re.search(r"[؀-ۿ]", text), f"{name} has no Arabic at all"


# --------------------------------------------------------------------------- #
# 1. Promotions carry no dates. The single most important rule here.
# --------------------------------------------------------------------------- #
MONTHS = (
    "january february march april may june july august september october "
    "november december"
).split()


def test_no_promotion_is_given_a_date_of_any_kind():
    """Elite supplied none — every validity cell reads "Sample - set actual end
    date". Stating one would invent a deadline about money.

    Checked against the OFFER CONTENT, not the whole file: the commentary is
    allowed to explain why there are no dates, and the frontmatter records the
    conversion date.
    """
    text = _read("promotions.md")
    body = text.split("---\n", 2)[2]
    body = body.split("<!--", 1)[0] + body.split("-->", 1)[-1]
    lowered = body.lower()

    # A bare year, or any recognisable date form.
    assert not re.search(r"\b20\d{2}\b", body), "a year appears in the offer text"
    assert not re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", body)
    for month in MONTHS:
        # "may" is also an ordinary English verb, so require a date-ish shape.
        assert not re.search(rf"\b\d+\s+{month}\b", lowered), month
        assert not re.search(rf"\b{month}\s+\d", lowered), month


def test_no_promotion_text_manufactures_urgency():
    """"Ends soon", "this month", "while stocks last" are deadlines by another
    name, and we have no deadline to report."""
    body = _read("promotions.md")
    # Only the parts that would be SPOKEN — the offer table and its details,
    # not the instructions telling the agent never to say these things.
    spoken = body.split("## The details worth having", 1)[1].split("---", 1)[0]
    for phrase in (
        "ends soon", "this month", "while stocks last", "limited time",
        "hurry", "last chance", "expires", "valid until", "offer ends",
    ):
        assert phrase not in spoken.lower(), phrase


def test_the_promotions_file_is_flagged_as_undated_in_its_frontmatter():
    """Queryable, not just written in prose — the retrieval layer indexes
    frontmatter, and this is the flag that keeps the rule attached to the text.
    """
    assert _frontmatter("promotions.md")["all_validity_dates_are_samples"] is True


def test_all_four_offers_are_present():
    text = _read("promotions.md")
    for offer in (
        "Ramadan Value Drive",
        "Complete Care Bundle",
        "Drive Protected",
        "Extended Peace of Mind",
    ):
        assert offer in text


def test_the_file_forbids_folding_a_discount_into_a_quoted_price():
    """services/finance.py knows nothing about promotions and must not. A Sample
    discount baked into an instalment produces a number the customer cannot
    reproduce and the invoice will not match."""
    text = _read("promotions.md").lower()
    assert "never applied to a calculation" in text or "never apply one to a price" in text


# --------------------------------------------------------------------------- #
# 2. The warranty trap
# --------------------------------------------------------------------------- #
def test_the_warranty_file_warns_that_ten_years_means_two_different_things():
    """On an X50 that figure is the whole vehicle; on a G700 it is the engine
    alone and the vehicle cover is 8 years / 300,000 km. A file that stated the
    headline without the warning would read as correct and mislead by a factor
    of nearly four on distance."""
    text = _read("warranty.md")
    assert "10 years / 1,000,000 km" in text
    assert "8 years / 300,000 km" in text
    assert "6 years / unlimited km" in text
    lowered = text.lower()
    assert "engine" in lowered and "vehicle" in lowered and "battery" in lowered
    # The warning itself, not merely the figures.
    assert "without saying which warranty" in lowered


def test_only_the_plug_in_hybrids_have_a_battery_warranty():
    """Three PHEVs — G700, T2 i-DM, T1 i-DM — and no petrol car. A battery
    warranty attached to a petrol model would be an obvious tell that the
    transcription drifted."""
    text = _read("warranty.md")
    hybrid_block = text.split("## The plug-in hybrids", 1)[1].split("## The petrol", 1)[0]
    assert "Battery" in hybrid_block
    for phev in ("G700", "T2 i-DM", "T1 i-DM"):
        assert phev in hybrid_block

    petrol_block = text.split("## The petrol cars", 1)[1].split("## G700 Top Fire", 1)[0]
    assert "Battery" not in petrol_block


def test_the_petrol_t1_is_recorded_as_the_exception_it_is():
    """Elite splits the T1's cover like a hybrid's, minus the battery — the only
    petrol car they do that for. Flattening it to "10 years" would overstate the
    vehicle cover, which is 6 years / unlimited km."""
    text = _read("warranty.md")
    assert "The petrol T1 is the exception" in text


def test_the_coming_soon_model_is_not_given_a_warranty(db=None):
    """It must not inherit the G700's just because of the name."""
    text = _read("warranty.md")
    block = text.split("## G700 Top Fire", 1)[1].split("---", 1)[0]
    assert "Coming soon" in block
    assert "1,000,000" not in block


# --------------------------------------------------------------------------- #
# 3. The delivery caveat
# --------------------------------------------------------------------------- #
def test_the_21_day_figure_never_appears_without_its_stock_condition():
    """21 days is for a car ALREADY IN STOCK. Quoting it for a car we do not
    have is the failure this file exists to prevent — a customer told "three
    weeks" who waits three months did not mishear us."""
    text = _read("delivery.md")
    assert "21 days" in text.lower()
    assert "4–8 weeks" in text or "4-8 weeks" in text
    assert "in stock" in text.lower()
    # The headline sentence must carry both halves.
    headline = text.split("# Delivery times", 1)[1][:400].lower()
    assert "21 days" in headline and "in stock" in headline


def test_delivery_points_at_live_stock_rather_than_assuming_it():
    """Availability is data, not an assumption — the stock service answers it per
    variant, colour and branch."""
    assert "Check stock before quoting a date" in _read("delivery.md")


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def test_registration_records_the_dependency_that_actually_delays_people():
    """Registration cannot start until insurance is issued. That is the real
    dependency in the list and the usual reason someone is waiting."""
    text = _read("registration.md")
    assert "cannot start until insurance is issued" in text.lower()


def test_registration_says_the_digital_mulkiya_is_enough_to_drive():
    """Customers assume they are stuck until the physical card arrives."""
    text = _read("registration.md").lower()
    assert "digital mulkiya" in text
    assert "10–30 minutes" in text or "10-30 minutes" in text


def test_the_inspection_exemption_is_recorded_as_good_news_not_a_step():
    """New vehicles are exempt for three years. It is an absence in the sheet,
    and worth volunteering because people expect the opposite."""
    text = _read("registration.md").lower()
    assert "not required" in text
    assert "three years" in text or "3 years" in text


def test_personalised_plates_hand_off_rather_than_quoting_a_timeline():
    text = _read("registration.md").lower()
    assert "hand off" in text
    assert "auction" in text


def test_no_registration_fee_is_invented_for_the_express_option():
    """Elite states that a fee exists, never its amount."""
    text = _read("registration.md")
    express = text.split("## Express plates", 1)[1].split("##", 1)[0]
    assert "never a number" in express.lower()
    assert not re.search(r"AED\s*\d", express)
