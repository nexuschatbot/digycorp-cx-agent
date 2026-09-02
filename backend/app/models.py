"""SQLAlchemy 2 ORM models for the Elite Test Drive backend.

Mirrors the "Data model" section of CLAUDE.md. List-valued fields use a portable
JSON column so the schema works on both SQLite (dev) and Postgres. Enum-like
status fields use Python `str`/`Enum` classes mapped via SQLAlchemy's `Enum`
type.

AuditEvent is append-only by convention (FR-AD-02): this module intentionally
provides no update/delete helpers for it — callers only ever insert.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


# --------------------------------------------------------------------------- #
# Enumerations (str-valued so they serialise cleanly and store as their value)
# --------------------------------------------------------------------------- #
class LocationType(str, enum.Enum):
    showroom = "showroom"
    doorstep = "doorstep"


class SlotStatus(str, enum.Enum):
    open = "open"
    locked = "locked"
    booked = "booked"


class BookingStatus(str, enum.Enum):
    confirmed = "confirmed"
    rescheduled = "rescheduled"
    cancelled = "cancelled"


class BookingAttendance(str, enum.Enum):
    """Did the customer actually turn up? (2026-07-23)

    A SECOND, INDEPENDENT AXIS — deliberately not more values on BookingStatus.
    That column answers "is this booking alive?" and is load-bearing: it drives
    the partial unique index that prevents double-booking, reschedule,
    cancellation, and which reminders exist. Folding an attendance outcome into
    it would mean a completed drive is no longer `confirmed`, which silently
    drops it out of that index and out of every active-booking lookup.

    So the two are orthogonal and both are always readable: a booking can be
    confirmed-and-pending, confirmed-and-completed, confirmed-and-noshow. A
    CANCELLED booking stays `pending` forever and the service REFUSES to change
    it — nobody failed to attend a drive that was called off, and marking one
    `noshow` would fire a "we missed you today" message at a customer who
    politely cancelled a week ago.

    Only ONE transition has a side effect: anything -> `noshow` writes a WhatsApp
    notice asking whether all is well and offering a new time. `completed` is
    recorded and nothing else, by product decision — its meaning is still open.
    """

    pending = "pending"
    completed = "completed"
    noshow = "noshow"


class WaitlistStatus(str, enum.Enum):
    open = "open"
    fulfilled = "fulfilled"
    cancelled = "cancelled"
    expired = "expired"


class Speaker(str, enum.Enum):
    customer = "customer"
    agent = "agent"
    # Neither voice (voice call, 2026-07-29): a marker turn the SYSTEM writes —
    # today only the call-summary row ("call:<seconds>") a realtime call leaves
    # behind. Not a flavour of `agent`: read-aloud must refuse it (these are not
    # Aisha's words), the console must not count it as her turn, and the client
    # renders it as a muted system line, never in her bubble.
    system = "system"
    # A human operator typing from the admin console during a takeover (human
    # takeover, 2026-08-31). Not `agent`: these are a person's words, so
    # read-aloud must refuse them and the console must not count them as
    # Aisha's turns. Not `system` either: they render as real bubbles in the
    # customer's thread, with the operator's name, never as a muted marker.
    #
    # MIGRATION NOTE — same as Actor.admin: SAEnum is a NATIVE enum on
    # Postgres, so this value needs ALTER TYPE (see the Alembic revision that
    # added it). On SQLite it is a VARCHAR and nothing is required.
    staff = "staff"


class Actor(str, enum.Enum):
    customer = "customer"
    agent = "agent"
    system = "system"
    # The admin console (2026-07-21). A human operator acting through the back
    # office is a fourth actor class, not a flavour of `system`: `system` is the
    # scheduler and the app acting on their own, and folding an operator into it
    # would make "a person cancelled this booking" and "the clock fired a
    # reminder" indistinguishable in the trail we show Elite (rule 15/16). The
    # distinction is unrecoverable after the fact — nothing else in the payload
    # marks it — so it is typed here rather than tagged in the JSON.
    #
    # MIGRATION NOTE. SAEnum compiles to a NATIVE enum type on Postgres, so
    # adding this value is NOT picked up by create_all on an existing database —
    # it needs `ALTER TYPE ... ADD VALUE`, or a drop and re-seed. The DB is
    # disposable by design (no Alembic; see CLAUDE.md), so the supported fix is
    # `python -m app.cli reset-demo`, or dropping the compose volume. On SQLite
    # this is a VARCHAR and nothing is required. Skip that step and the first
    # console action raises at WRITE time, not at startup.
    admin = "admin"


class AuditType(str, enum.Enum):
    intent = "intent"
    tool_call = "tool_call"
    notification = "notification"
    consent = "consent"
    handoff = "handoff"
    erasure = "erasure"
    # Human-callback escalation (rule 13, FR-ES). Its OWN type, deliberately NOT
    # `handoff`: `handoff` is the WhatsApp deep-link handoff (a channel bridge),
    # whereas this is Aisha offering a human specialist a callback for something
    # out of her lane (price negotiation, finance, a complaint) or after two
    # failures. Written by services/escalation when request_human_callback fires,
    # actor=agent. It is what the console dashboard's `handoffs` block counts —
    # before this existed that count was a hardcoded zero (nothing recorded an
    # escalation). No supervisor UI and no real callback are in scope: the row IS
    # the escalation, logged and auditable per FR-AD-02.
    #
    # MIGRATION NOTE — same as Actor.admin / NotificationKind.noshow: SAEnum is a
    # NATIVE enum on Postgres, so this value does not appear via create_all on an
    # existing database. `python -m app.cli reset-demo`, or drop the volume. Skip
    # it and the first escalation raises at WRITE time, not at startup.
    escalation = "escalation"
    # Voice notes (2026-07-21). Its own type rather than `intent`: a
    # transcription is not the customer expressing an intent, it is US turning
    # their audio into text, with a vendor, a model and a failure mode of its
    # own. Auditing it as `intent` would blur "the customer asked for X" with
    # "a US endpoint thought they said X", and the second is the one the PDPL
    # checklist needs to be able to find. Blob DELETION is not here — that is
    # `erasure`, alongside every other hard-delete.
    voice = "voice"
    # Post-drive feedback survey (2026-07-26). Its OWN type for the same reason
    # `voice` and `escalation` have theirs: a survey answer is neither the
    # customer expressing an intent nor a notification we sent, and folding it
    # into `intent` would bury satisfaction data inside the booking-request
    # trail — which is precisely the query an operator asking "what did people
    # say about the Deira team?" needs to be able to run. Written by
    # services/feedback on every transition: opened, sent, each answer, the
    # comment, and whichever terminal status it reaches.
    #
    # MIGRATION NOTE — same as Actor.admin / NotificationKind.noshow: SAEnum is a
    # NATIVE enum on Postgres, so this value does not appear via create_all on an
    # existing database. `python -m app.cli reset-demo`, or drop the volume.
    feedback = "feedback"


class NotificationKind(str, enum.Enum):
    confirmation = "confirmation"
    t24 = "t24"
    t2 = "t2"
    reschedule = "reschedule"
    cancellation = "cancellation"
    # A drive the customer did not attend (2026-07-23). Written the moment an
    # operator marks the booking `noshow` in the console, sent immediately — it
    # asks whether everything is alright and offers a new time, so the customer
    # answers on WhatsApp and the existing inbound handoff carries them back
    # into the chat already identified. Not a reminder: nothing schedules it,
    # and it fires exactly once per booking (Booking.noshow_notified_at).
    #
    # MIGRATION NOTE — same as Actor.admin below: SAEnum is a NATIVE enum on
    # Postgres, so this value does not appear via create_all on an existing
    # database. `python -m app.cli reset-demo`, or drop the volume. Skip it and
    # the first no-show raises at WRITE time, not at startup.
    noshow = "noshow"
    # The post-drive feedback survey's OPENING message (2026-07-26). Only the
    # opening rides these rails; Q1..Q4 and the close are sent directly by
    # services/feedback as the customer answers, because they are replies inside
    # a live conversation rather than scheduled notices. Routing the opening
    # through the dispatcher is what buys the consent gate, the bounded retry and
    # the notification audit trail for free — see NotificationKind.noshow, which
    # is written for the same reason.
    #
    # MIGRATION NOTE — as for `noshow` above: a NATIVE enum on Postgres, so this
    # value does not appear via create_all on an existing database. Run
    # `python -m app.cli reset-demo` or drop the volume.
    feedback = "feedback"
    # A model summary the customer ASKED for (C3, 2026-08-14). Unlike every kind
    # above it belongs to a PERSON rather than a booking — it is most useful
    # while somebody is still browsing — which is why Notification.ref became
    # nullable and customer_id was added beside it.
    #
    # MIGRATION NOTE — as for `noshow` and `feedback` above: a NATIVE enum on
    # Postgres, so this value does NOT appear via create_all on an existing
    # database. Applied to production with:
    #     ALTER TYPE notificationkind ADD VALUE IF NOT EXISTS 'brochure';
    # Skip it and the first brochure raises at WRITE time, not at startup.
    brochure = "brochure"


class NotificationStatus(str, enum.Enum):
    scheduled = "scheduled"
    sent = "sent"
    cancelled = "cancelled"


class Language(str, enum.Enum):
    en = "en"
    ar = "ar"


class FeedbackStatus(str, enum.Enum):
    """Where one post-drive survey has got to (2026-07-26).

    SIX values, and the four terminal ones are deliberately DISTINCT rather than
    collapsed into a single "closed": an operator reading the console needs to
    tell "they told us it was a 2" from "they never replied" from "they asked us
    to stop", and a survey that was interrupted by a real booking request is not
    a refusal to answer. Rolling them together would make the response rate
    unreadable and would quietly bury the one signal worth acting on.
    """

    # Written and RELEASED onto the notification rails — its opening exists as a
    # scheduled Notification row and goes out on the next dispatcher tick.
    pending = "pending"
    # Written but HELD BACK, because this customer already has a survey in
    # flight. No notification row exists yet; one is written when the earlier
    # survey closes. Distinct from `pending` on purpose: collapsing the two makes
    # "is this customer already being surveyed?" unanswerable, and the promotion
    # check then sees the queued survey itself as the thing blocking it and never
    # releases anything.
    queued = "queued"
    # The opening went out; we are waiting for the customer to agree to start.
    sent = "sent"
    # They are answering. `step` says which question is outstanding.
    in_progress = "in_progress"
    # All four scores plus the open comment (which may itself be empty — they
    # can finish without writing prose).
    completed = "completed"
    # They said "skip". Partial answers are KEPT: someone who rated the car a 2
    # and then stopped has told us the most useful thing in the survey.
    skipped = "skipped"
    # 24h passed mid-survey with no reply. Partial answers kept, same reasoning.
    expired = "expired"
    # A real booking intent arrived mid-survey ("cancel my booking") and won.
    # NOT a refusal — they were busy with something that actually mattered, and
    # trapping them in a satisfaction survey would have been the wrong answer.
    abandoned = "abandoned"


class FuelType(str, enum.Enum):
    """Machine-readable powertrain. The catalogue holds ONLY these two — there
    is no electric/diesel/mild-hybrid row, so search_vehicles must not imply
    otherwise: if a customer asks for an EV the honest answer is that we do not
    have one (FR-CX-07). Marketing wording ("Plug-in hybrid (PHEV) — i-DM")
    lives in `name`/`headline_features`, not here, so the agent can phrase the
    powertrain natively in either language (LOC-01).
    """

    petrol = "petrol"
    phev = "phev"


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str | None] = mapped_column(String(32))
    title_ar: Mapped[str | None] = mapped_column(String(32))
    name_en: Mapped[str | None] = mapped_column(String(255))
    name_ar: Mapped[str | None] = mapped_column(String(255))
    gender: Mapped[str | None] = mapped_column(String(16))
    nationality: Mapped[str | None] = mapped_column(String(64))
    emirate: Mapped[str | None] = mapped_column(String(64))
    mobile: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    language_pref: Mapped[Language] = mapped_column(
        SAEnum(Language), default=Language.en
    )

    # Consent flags + timestamps (PDPL; FR-MEM, FR-CF-05).
    memory_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    memory_consent_at: Mapped[datetime | None] = mapped_column(DateTime)
    whatsapp_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    whatsapp_consent_at: Mapped[datetime | None] = mapped_column(DateTime)

    winback_flag: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    memory_profile: Mapped["MemoryProfile | None"] = relationship(
        back_populates="customer", uselist=False
    )
    bookings: Mapped[list["Booking"]] = relationship(back_populates="customer")


class VehicleModel(Base):
    __tablename__ = "vehicle_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Catalogue model code, e.g. "JET-G700" — natural key kept alongside the
    # integer PK (slots/bookings FK to the integer id, so that stays numeric).
    model_code: Mapped[str | None] = mapped_column(
        String(32), unique=True, index=True
    )
    brand: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    # Free text on purpose ("Mid-size family SUV"). Customers use body type
    # loosely, so search matches this case-insensitively as a substring rather
    # than against a taxonomy we would have to invent (FR-BK-09).
    body_type: Mapped[str | None] = mapped_column(String(64))
    # Seats are a RANGE, not a value: the X70 FL is 5-7, the G700 is 5-6.
    # (This named the X70 PLUS until T0.5. It is the 7-7 car; the FL is the
    # one with the occasional 5+2 third row. The seed had them swapped.)
    # Discovery filters seats_max >= min_seats (FR-BK-09/10) — comparing on a
    # single value would silently hide the main family SUV from every
    # "I need a 7-seater" conversation. For a fixed-seat model min == max.
    seats_min: Mapped[int | None] = mapped_column(Integer)
    seats_max: Mapped[int | None] = mapped_column(Integer)
    # Numeric bracket bounds so discovery can filter on max_price_aed
    # (FR-BK-09/10).
    #
    # NULLABLE AGAIN SINCE 2026-08-28 (T0.7), and the note above it explains
    # exactly what that costs. Verbatim, it said: "If a future model genuinely
    # has no published price, this is a REAL DECISION TO REOPEN and not a column
    # to quietly re-null: restoring the column alone would let an unpriced car
    # sort as free in lowest_price and vanish from every budget query."
    #
    # That future model has arrived — the G700 Top Fire, coming soon, with no
    # price and no stock. So this is that decision, taken deliberately and with
    # the two consequences the old note named both handled:
    #
    #   1. NULL GUARDS ARE RESTORED in all four catalogue._ranked_for bases, so
    #      an unpriced car can never sort as free or top a "cheapest" answer.
    #      They are covered by tests, not just written back in.
    #   2. THE DB IS NO LONGER DISPOSABLE. The old note's advice — "drop and
    #      re-seed on the change" — assumed there was no Alembic. There is now
    #      (T0.3), so this change ships as a migration and existing rows survive.
    #
    # Null still MEANS "price not published", and the agent must say so rather
    # than invent a number (FR-CX-07).
    price_aed_min: Mapped[int | None] = mapped_column(Integer)
    price_aed_max: Mapped[int | None] = mapped_column(Integer)
    fuel_type: Mapped[FuelType] = mapped_column(SAEnum(FuelType), index=True)
    headline_features: Mapped[list] = mapped_column(JSON, default=list)
    # Arabic counterpart, same order and same length as headline_features.
    #
    # WHY A SECOND COLUMN AND NOT A TRANSLATION AT SPEAKING TIME. Everywhere
    # else the rule is that structured data drives logic and the agent phrases
    # it per language (LOC-01). These bullets are the exception: they are
    # marketing PROSE, already written, and there is nothing structured to
    # phrase from — an English string cannot be spoken into an Arabic
    # conversation, so the Arabic has to exist as data.
    #
    # DELIBERATELY NOT IN THE TOOL RESULT. _vehicle_dict returns only the
    # English list. Returning both would grow every search_vehicles result by
    # ~29% in tokens the model never speaks — it picks two or three bullets and
    # phrases them. The dialog reads this column over REST instead, where it
    # costs nothing. If you ever add it to the tool output, check the token
    # cost first (`agent usage` logs prompt_total).
    headline_features_ar: Mapped[list] = mapped_column(JSON, default=list)
    showroom_ids: Mapped[list] = mapped_column(JSON, default=list)
    # Interior colour palette as Elite lists it ("Black & Orange", "Ivory /
    # Blue"). JSON for the same reason showroom_ids and images are: a short
    # per-model list nothing filters or sorts on numerically.
    #
    # THREE THINGS IT IS NOT, all of which a reader will assume otherwise:
    #
    #   1. NOT PAIRED WITH THE EXTERIOR COLOUR. Elite's Models sheet runs the
    #      exterior and interior lists down the same block of rows, and they are
    #      different lengths — the G700 Comfort has six exteriors against two
    #      interiors. They are independent palettes, not a mapping. Aisha may say
    #      "the T1 comes in three interiors"; she may NOT say "the black one with
    #      ivory is available".
    #   2. NOT STOCKED. Stock's grain is (variant, showroom, EXTERIOR colour),
    #      because that is the grain Elite's unit counts are at. So this column
    #      answers what EXISTS, never what is available where.
    #   3. NOT PER-VARIANT. The sheet lists one palette per model, spanning its
    #      trims — unlike seats and price, which genuinely differ per trim.
    #
    interior_colours: Mapped[list] = mapped_column(JSON, default=list)
    # Exterior colour palette as Elite's Models sheet lists it, one per model.
    #
    # THIS COLUMN REVERSES A STANDING NOTE that used to sit here: "exterior
    # colours have no column of their own on purpose: they ARE the Stock key,
    # so they are already queryable per branch." That was true of AVAILABILITY
    # and wrong about EXISTENCE — a colour Elite offers with zero stock rows
    # anywhere did not exist in the database at all, and live customers asking
    # "what colours does it come in" were told we do not hold that (2026-09-01).
    # The workbook lists the palette per MODEL; the converter had parsed it
    # since day one and dropped it (see seed/convert_workbook.py).
    #
    # WHAT STOCK REMAINS FOR: which of these colours can be HAD, and where.
    # This column is what we make; Stock is what is on the ground. The same
    # three caveats as interior_colours apply: not paired with an interior,
    # per-model not per-variant, and never evidence of availability.
    exterior_colours: Mapped[list] = mapped_column(JSON, default=list)
    # Free-text source/traceability note for the row's data.
    data_provenance: Mapped[str | None] = mapped_column(Text)
    # Relative paths (from the backend repo root) to the model's image files.
    images: Mapped[list] = mapped_column(JSON, default=list)


class VehicleVariant(Base):
    """One purchasable TRIM of a VehicleModel (T0.7).

    WHY THIS TABLE EXISTS. Until now a "car" was a model row with a price RANGE
    — the G700 was 169,000-240,450 and nothing knew that 169,000 buys a Comfort
    and 240,450 buys a Flagship. Every commercial fact Elite supplied is
    per-trim, not per-model: on-road price, insurance, stock by colour and
    branch, and fuel economy. Without this table none of it has anywhere to live.

    THE MODEL ROW KEEPS ITS RANGE, DELIBERATELY. catalogue.search() still returns
    MODEL cards showing a price band (see T0.9), because a customer asking "what
    SUVs do you have" wants nine cars, not fifteen trims. The variant is what
    they narrow TO, not what they browse.

    TRIM NAMES DO NOT IMPLY RANK, and the T1 proves it: its Luxury is the
    four-wheel-drive range-topper and its Flagship is specified close to Comfort,
    which is why Luxury is priced ABOVE Flagship. Never sort trims by name, and
    never assume "Flagship" is the top of a ladder — sort by price.

    `name` IS NULLABLE because several models have exactly one unnamed trim
    (T2 Luxury Plus, T2 i-DM, T1 i-DM). A null name means "the only version",
    not "unknown".

    THIS LIST NAMED THE X50 UNTIL T0.5, on the workbook's authority — the
    workbook leaves its Variant cell blank. Its BROCHURE names the trim outright
    ("Trim | Premium") in the specification table, so it is seeded as "Premium".
    The workbook stays authoritative for commercial data; a name beats a blank.
    """

    __tablename__ = "vehicle_variants"
    __table_args__ = (
        UniqueConstraint("model_id", "name", name="uq_variant_model_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[int] = mapped_column(
        ForeignKey("vehicle_models.id"), index=True
    )
    # "Comfort", "Luxury", "Flagship", "1.5 Standard". NULL = the only trim.
    name: Mapped[str | None] = mapped_column(String(64))

    # NULLABLE FOR THE SAME REASON VehicleModel's price is: the G700 Top Fire is
    # coming soon and has no price. Null means "not published", never zero.
    price_aed: Mapped[int | None] = mapped_column(Integer)
    insurance_aed: Mapped[int | None] = mapped_column(Integer)
    registration_aed: Mapped[int | None] = mapped_column(Integer)

    # Per-trim, because they genuinely differ: the T1's Luxury is 4WD and
    # heavier, so it is thirstier than its own Comfort and Flagship.
    #
    # STORED AS km/L, which is Elite's authoritative column (their decision).
    # A RANGE, not a value — most figures are quoted as one, and collapsing
    # "12 to 14" to a single number would either overstate or understate it.
    km_per_l_min: Mapped[float | None] = mapped_column(Float)
    km_per_l_max: Mapped[float | None] = mapped_column(Float)

    # Seats are per-TRIM, not per-model, and the G700 is why: its Comfort is a
    # 5-seater (2+3) and its Luxury and Flagship are 6 (2+2+2). Storing this on
    # the model alone would make one of those answers wrong.
    seats_min: Mapped[int | None] = mapped_column(Integer)
    seats_max: Mapped[int | None] = mapped_column(Integer)

    # Not orderable yet. Distinct from "no stock": a sold-out trim can still be
    # ordered, a coming-soon one cannot. The agent must say so rather than
    # quote a price or a date it does not have.
    coming_soon: Mapped[bool] = mapped_column(Boolean, default=False)

    model: Mapped["VehicleModel"] = relationship()

    @property
    def on_road_aed(self) -> int | None:
        """Price + insurance + registration, or None if any part is unpublished.

        NOT a sum with nulls treated as zero — that would quote an on-road price
        BELOW the list price and look like a discount. All three or nothing.
        """
        parts = (self.price_aed, self.insurance_aed, self.registration_aed)
        return sum(parts) if all(p is not None for p in parts) else None


class Stock(Base):
    """Units on the ground, per variant x colour x showroom (T0.7).

    THE GRAIN IS THE POINT. Elite's sheet is 66 rows across 14 variants and 5
    branches, which makes "is the G700 Flagship in Matte Orange at Deira?"
    answerable — a question the old model-level catalogue could not touch.

    A SNAPSHOT, NOT A LEDGER. Nothing in this system decrements `units` when a
    car sells; the numbers are only as fresh as the last import. That is why the
    agent answers AVAILABILITY (yes/no) and never a count — a stale "twelve at
    Deira" is a promise we cannot keep, while a stale "yes, we have it" degrades
    to a conversation at the showroom.
    """

    __tablename__ = "stock"
    __table_args__ = (
        UniqueConstraint(
            "variant_id", "showroom_id", "colour", name="uq_stock_variant_showroom_colour"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variant_id: Mapped[int] = mapped_column(
        ForeignKey("vehicle_variants.id"), index=True
    )
    showroom_id: Mapped[int] = mapped_column(
        ForeignKey("showrooms.id"), index=True
    )
    # Exterior colour as Elite writes it ("Matte Orange", "Highway Grey").
    # Free text, not an enum: the palette differs per model and a taxonomy we
    # invented would fight the next import.
    colour: Mapped[str] = mapped_column(String(64), index=True)
    units: Mapped[int] = mapped_column(Integer, default=0)

    variant: Mapped["VehicleVariant"] = relationship()
    showroom: Mapped["Showroom"] = relationship()


class Accessory(Base):
    """A priced accessory (T0.7).

    IN THE DATABASE RATHER THAN A KNOWLEDGE MD, because it carries prices and
    prices live in exactly one place. Two price sources is how a customer is
    quoted one number in chat and another on the invoice.

    `model_id` IS NULLABLE: some accessories fit one model, some fit anything.
    """

    __tablename__ = "accessories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Elite's SKU ("ACC-003"). NEVER SPOKEN — no customer says a part code and
    # the agent must not read one out. It exists as a stable join key: matching
    # a re-import on the name string ("Paint Protection Film (PPF)", parentheses
    # and all) is fragile in a way matching on ACC-004 is not.
    part_code: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    name_en: Mapped[str] = mapped_column(String(128))
    name_ar: Mapped[str | None] = mapped_column(String(128))
    # WHAT THE THING ACTUALLY IS, and the reason this column was added rather
    # than left in the fixture. Three of the five names mean nothing on their
    # own: "Ceramic Coating - AED 1,800" tells a customer neither what it is nor
    # why it costs that, and "Paint Protection Film - AED 2,200" reads as absurd
    # beside tinting at 900 until you know it covers the bumper, hood and
    # mirrors and self-heals.
    #
    # The agent speaks only from tool results, so without this she cannot
    # describe an accessory at all — inventing a description would be exactly
    # the grounding failure the guardrails exist to prevent, and "I don't have
    # that" about a product Elite is selling is the alternative.
    #
    # ENGLISH ONLY: Elite supplied no Arabic for accessories, so `name_ar` is
    # null on every row and there is no description_ar to be null. An Arabic
    # conversation gets a price and an English sentence. That is a DATA gap to
    # take back to Elite, not one a column can close.
    description_en: Mapped[str | None] = mapped_column(Text)
    price_aed: Mapped[int | None] = mapped_column(Integer)
    model_id: Mapped[int | None] = mapped_column(
        ForeignKey("vehicle_models.id"), index=True
    )

    model: Mapped["VehicleModel"] = relationship()


class FinanceBank(Base):
    """A financing option (T0.7).

    EVERY RATE ELITE SUPPLIED IS MARKED "Sample". Nothing here may be presented
    as a quotation: services/finance computes an INDICATIVE instalment and the
    agent must say so, then hand a real quote to a human (rule 13). The columns
    exist to make the arithmetic honest, not to make the number binding.
    """

    __tablename__ = "finance_banks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    # Annual reducing-balance rate, e.g. 3.99. Nullable because a "Sample" row
    # may carry a band rather than a figure.
    rate_percent: Mapped[float | None] = mapped_column(Float)
    min_down_payment_percent: Mapped[float | None] = mapped_column(Float)
    max_tenure_months: Mapped[int | None] = mapped_column(Integer)
    # Sharia-compliant product. A real filter — customers ask for it by name.
    islamic: Mapped[bool] = mapped_column(Boolean, default=False)
    # Elite marked every supplied rate "Sample". Kept as a column so the agent's
    # caveat is driven by data rather than by a hardcoded assumption that all
    # rates are indicative forever.
    is_sample: Mapped[bool] = mapped_column(Boolean, default=True)


class Showroom(Base):
    __tablename__ = "showrooms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    emirate: Mapped[str | None] = mapped_column(String(64))
    # City slug ("al_ain", "deira", "al_quoz"). Distinct from `emirate` because a
    # city is not an emirate: Jetour Al Ain sits in emirate=abu_dhabi alongside
    # Rabdan. Without this column the only place "Al Ain" appears is inside
    # `name`/`address` — display strings — so answering "which of these is
    # actually in Al Ain?" would mean substring-matching prose, i.e. display text
    # doing a data field's job. list_showrooms reads THIS to stamp match_type.
    city: Mapped[str | None] = mapped_column(String(64), index=True)
    address: Mapped[str | None] = mapped_column(Text)
    # Natively-written Arabic address (LOC-01). A street address is one of the
    # few genuinely bilingual STRINGS in the schema — unlike price or seats, it
    # cannot be composed from structured parts at speaking time, so both scripts
    # have to be stored. Western digits in both ("شارع 4"), per LOC-09.
    address_ar: Mapped[str | None] = mapped_column(Text)
    map_link: Mapped[str | None] = mapped_column(Text)
    # Pin coordinates for the console dashboard's branch map (2026-07-23).
    #
    # STORED, NOT DERIVED FROM map_link. That column holds a Google Maps SEARCH
    # URL ("?query=Jetour+Showroom+Deira+Dubai"), not a coordinate pair, so there
    # is nothing in it to parse — a map built on it would have to geocode at
    # render time, i.e. ask a third party where our own branches are, on stage.
    #
    # NULLABLE, AND A NULL DROPS THE PIN rather than defaulting to 0,0 (which is
    # in the Atlantic). A branch we do not have coordinates for is a branch the
    # map cannot honestly place — the same "drop it, never render a shell" rule
    # the vehicle and booking cards follow (FR-CX-07).
    #
    # ⚠️ The seeded values are APPROXIMATE, PENDING ELITE SIGN-OFF: they are
    # publicly-listed district positions, not surveyed showroom entrances. They
    # are accurate enough for the emirate-level view the dashboard renders and
    # are NOT used for navigation — the customer-facing map pin is still
    # `map_link`, which Elite supplied.
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    phone: Mapped[str | None] = mapped_column(String(20))
    # Human-readable opening hours. Display only — nothing reads this column;
    # it must never drive slot generation (see hours_by_day below).
    hours: Mapped[str | None] = mapped_column(Text)
    # Structured opening hours keyed by weekday name ("mon".."sun"), each an
    # [open_hour, close_hour] pair of whole hours on a 24h clock, or null when
    # the showroom is closed that day. This is the machine-readable source of
    # truth for slot generation (FR-SL-02, S-22) — a JSON column so the same
    # schema works on SQLite (dev) and Postgres, matching the convention used
    # by headline_features/showroom_ids/images.
    hours_by_day: Mapped[dict] = mapped_column(JSON, default=dict)


class Slot(Base):
    __tablename__ = "slots"

    # The availability query's exact shape (services/slots.query_available):
    # vehicle_id and status are equality predicates, start is the range bound
    # AND the ORDER BY — so one composite index lets the engine seek straight to
    # the matching range and read it already sorted, instead of walking every
    # `open` row via ix_slots_status and sorting in a temp B-tree.
    # ix_slots_status stays: _release_expired_locks filters on status alone.
    __table_args__ = (
        Index("ix_slots_lookup", "vehicle_id", "status", "start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_type: Mapped[LocationType] = mapped_column(SAEnum(LocationType))
    showroom_id: Mapped[int | None] = mapped_column(
        ForeignKey("showrooms.id")
    )
    service_area: Mapped[str | None] = mapped_column(String(128))
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicle_models.id"))
    start: Mapped[datetime] = mapped_column(DateTime, index=True)
    end: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[SlotStatus] = mapped_column(
        SAEnum(SlotStatus), default=SlotStatus.open, index=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime)
    # WHICH session holds the lock — the executor's session_id (String(64),
    # matching AuditEvent.session). Without this, `status == locked` cannot tell
    # this session's lock from another's, and create_booking's first write guard
    # ("the slot MUST be locked by THIS session") degrades to "locked by
    # somebody", which is not a guard at all. Never settable by the model: the
    # value is injected by the executor, never declared in an input_schema.
    locked_by_session: Mapped[str | None] = mapped_column(String(64), index=True)
    # Rule 9, precondition 3: has the driving-licence reminder been acknowledged
    # for THIS lock? Set only by slots.acknowledge_licence_reminder (server-side),
    # never by a field the model can fill, and checked by booking.create.
    #
    # It lives on the LOCK, not the session or the customer, so its lifetime is
    # exactly the lock's: taking a new lock clears it, releasing or expiring a
    # lock clears it. That is what stops an acknowledgement from one slot
    # silently satisfying the guard for a different slot booked later in the same
    # conversation ("actually, make it 8pm" must re-remind).
    #
    # SCOPE, honestly stated: this enforces the ORDER (reminder acknowledged
    # before booking), not the DELIVERY (that Aisha actually said the words). A
    # model could call the tool and stay silent. Delivery is guarded by a standing
    # behaviour test instead — see tests/test_licence_reminder.py — because the
    # licence reminder is a courtesy, and the cost of guaranteeing it structurally
    # (server-rendered text) would be a colder, unlocalisable reminder.
    licence_ack: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    showroom: Mapped["Showroom | None"] = relationship()
    vehicle: Mapped["VehicleModel"] = relationship()


class Booking(Base):
    """A confirmed test drive.

    Two DB-level guarantees here, both deliberate — the service translates a
    violation into data, but the *guarantee* lives in the database, not in the
    calculation that tries to avoid it:

    1. ``ref`` is UNIQUE. TD-YYYY-XXXX is generated with a RANDOM base32 suffix
       (see services/booking.py), which can collide: two sessions can draw the
       same token. This constraint — not the draw — is what makes a duplicate
       reference impossible on stage. The loser of a collision retries.
    2. ``uq_bookings_active_slot`` is a PARTIAL unique index on slot_id,
       covering only ACTIVE bookings (confirmed/rescheduled). It must be
       partial: cancel releases the slot back to inventory but leaves the
       cancelled row still pointing at slot_id, so a plain UniqueConstraint
       would reject the next customer to book a slot that is genuinely free.
       A duplicate waitlist row is harmless; a double-booking means two
       customers arriving for the same car at the same time — that is worth a
       DB guarantee.
    """

    __tablename__ = "bookings"

    # SQLite and Postgres both support partial indexes, with different kwargs;
    # the predicate compares against the ENUM VALUES ("confirmed"), which is
    # what SAEnum stores. Keep this list in sync with BookingStatus: a new
    # active status added there must be added here, or it silently stops being
    # covered by the no-double-booking guarantee.
    __table_args__ = (
        Index(
            "uq_bookings_active_slot",
            "slot_id",
            unique=True,
            sqlite_where=text("status IN ('confirmed', 'rescheduled')"),
            postgresql_where=text("status IN ('confirmed', 'rescheduled')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(
        String(16), unique=True, index=True
    )  # TD-YYYY-XXXX (random base32 suffix)

    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    driver_name: Mapped[str | None] = mapped_column(String(255))
    driver_relationship: Mapped[str | None] = mapped_column(String(64))
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicle_models.id"))
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"))

    location: Mapped[LocationType] = mapped_column(SAEnum(LocationType))
    doorstep_address: Mapped[str | None] = mapped_column(Text)
    companions: Mapped[str | None] = mapped_column(String(255))
    child_seat: Mapped[bool] = mapped_column(Boolean, default=False)
    consultant_pref: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[BookingStatus] = mapped_column(
        SAEnum(BookingStatus), default=BookingStatus.confirmed, index=True
    )
    reason_code: Mapped[str | None] = mapped_column(String(64))

    # Attendance (2026-07-23) — the SECOND axis, set by a human in the console,
    # never by the agent. See BookingAttendance for why this is not more values
    # on `status`. Every booking starts `pending`; a cancelled one stays there.
    attendance: Mapped[BookingAttendance] = mapped_column(
        SAEnum(BookingAttendance), default=BookingAttendance.pending, index=True
    )
    # Stamped when the no-show notice is WRITTEN, and it is what makes that
    # message fire once rather than once per click: an operator who flips
    # noshow -> pending -> noshow (a correction, then a re-correction) must not
    # text the customer twice. A boolean would say the same thing; the timestamp
    # additionally answers "when did we chase them?", which an operator looking
    # at a stale booking actually asks.
    noshow_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Stamped when the feedback survey is OPENED (2026-07-26), and the same shape
    # of guard as noshow_notified_at directly above, for the same reason: an
    # operator who flips completed -> pending -> completed is correcting a
    # mis-click, not asking us to survey the customer twice. Once per booking,
    # ever — moving OFF `completed` does not retract a survey already sent,
    # because retracting a WhatsApp message is not possible.
    feedback_requested_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    customer: Mapped["Customer"] = relationship(back_populates="bookings")
    vehicle: Mapped["VehicleModel"] = relationship()
    slot: Mapped["Slot"] = relationship()


class Waitlist(Base):
    """Registered interest when no slot matches the customer's preferences
    (FR-SL-05) — so a no-match request is logged rather than dead-ending the
    conversation. Schema only: the ``join_waitlist`` tool, the services, the
    REST/console endpoints, and any seeding are intentionally NOT built here.

    ``customer_id`` is non-nullable by design: a waitlist entry always belongs to
    an *identified* customer. The future ``join_waitlist`` path must therefore
    capture and validate the +971 mobile (find_or_create_customer) BEFORE it can
    register interest. Do not "fix" a future NOT NULL error by making this column
    nullable — the constraint is the flow requirement.

    location_type / showroom_id / service_area mirror ``Slot`` so a showroom vs
    doorstep preference is expressed the same way it is for real slots. Whether a
    freed slot (cancel/reschedule) actively notifies waitlisted customers, or the
    waitlist is only a console-visible logged request, is deliberately left open —
    this table supports either; no notification trigger is built now.
    """

    __tablename__ = "waitlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicle_models.id"))

    # Showroom vs doorstep preference — mirrors Slot (enum, no default;
    # showroom_id null when doorstep, service_area used for doorstep only).
    location_type: Mapped[LocationType] = mapped_column(SAEnum(LocationType))
    showroom_id: Mapped[int | None] = mapped_column(ForeignKey("showrooms.id"))
    service_area: Mapped[str | None] = mapped_column(String(128))

    # Desired date range + daypart. daypart mirrors MemoryProfile.preferred_daypart
    # (nullable String(32), no default) — a DB column, distinct from the tool-layer
    # daypart input schema in CLAUDE.md; the two need not agree.
    desired_from: Mapped[datetime | None] = mapped_column(DateTime)
    desired_to: Mapped[datetime | None] = mapped_column(DateTime)
    daypart: Mapped[str | None] = mapped_column(String(32))

    # Duplicate-open-entry prevention lives in the service layer when the writer
    # is built (idempotency belongs there, AD-I) — not a DB constraint here.
    status: Mapped[WaitlistStatus] = mapped_column(
        SAEnum(WaitlistStatus), default=WaitlistStatus.open, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # One-directional accessors (mirror Slot/Booking) — no back_populates, so no
    # other table is touched. The console renders customer & vehicle names from these.
    customer: Mapped["Customer"] = relationship()
    vehicle: Mapped["VehicleModel"] = relationship()
    showroom: Mapped["Showroom | None"] = relationship()


class MemoryProfile(Base):
    """What we remember about a customer between conversations (FR-MEM, rule 12).

    ONE ROW PER CUSTOMER, CONSENT-GATED ON READ *AND* WRITE. The gate lives in
    services/memory.py, not here and not in the tools: gating writes alone would
    leave a stale row readable after a failed delete or a seed drift.

    WHY A STORED ROW AT ALL. These preferences could be derived on demand from
    the customer's past Bookings — no table, nothing to keep in sync, nothing
    that can go stale. Erasure is what rules that out: "forget me" would have to
    delete their bookings, which the business needs to keep, leaving a
    suppression flag as the only option — and a tombstone we can still read is
    not erasure (rule 12). The table exists so that deletion can be real.

    TWO WRITERS, DISJOINT COLUMNS, SO THERE IS NO PRECEDENCE RULE.
    memory.derive_from_booking owns preferred_models / preferred_showroom_id /
    preferred_daypart — observed behaviour, what the customer actually booked.
    memory.save owns notes — soft preferences a booking cannot see. Neither
    touches the other's columns. This is the line customers._fill_if_empty
    already draws: a value we computed server-side is a fact, a value arriving
    in chat is an assertion, and an assertion may not overwrite a fact.

    IDS, NEVER NAMES (the no-display-text rule). preferred_showroom_id is an FK
    and preferred_models holds vehicle_ids, because a stored English showroom
    name cannot be spoken in an Arabic turn (LOC-01) and goes stale on a rename.
    memory._profile_dict joins the names back at read time — the same asymmetry
    booking._booking_dict uses (stores slot_id, returns showroom_name).

    `notes` is the one exception and cannot be structurally fixed: it is free
    prose, so a model could write display text into it. Capped and framed in the
    tool description as guidance the agent rephrases, never copy it reads out.

    NO consultant_pref HERE, deliberately. It lives on Booking, per visit. Rule 8
    says a consultant preference is honoured instantly when asked and never
    volunteered — making it a structured column the agent can filter or offer on
    is exactly the presumption that rule forbids. Soft preferences go in `notes`.
    """

    __tablename__ = "memory_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), unique=True
    )
    preferred_models: Mapped[list] = mapped_column(JSON, default=list)
    preferred_showroom_id: Mapped[int | None] = mapped_column(
        ForeignKey("showrooms.id")
    )
    preferred_daypart: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)

    customer: Mapped["Customer"] = relationship(back_populates="memory_profile")


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    # CLOSES THE GAP services/erasure.py NAMED AND COULD NOT FIX (T4.6).
    #
    # Its docstring said it plainly: "ConversationTurn carries a session_id but
    # no customer_id, so there is no direct customer -> conversations edge
    # anywhere in the schema... A conversation where the customer was identified
    # but no audited tool call ever ran is therefore unreachable, and its
    # transcript would survive. Closing that gap properly means putting
    # customer_id on ConversationTurn at identification time — a schema change,
    # deliberately not smuggled in here."
    #
    # Alembic removed that objection (T0.3), so here it is.
    #
    # NULLABLE, AND STAYS NULLABLE. A turn spoken before anyone said who they
    # were genuinely has no customer, and back-filling it with a guess would be
    # worse than the gap: it would attach one person's words to another's record.
    # It is set at identification and back-filled across the session then.
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id"), index=True
    )
    speaker: Mapped[Speaker] = mapped_column(SAEnum(Speaker))
    lang: Mapped[Language] = mapped_column(SAEnum(Language))
    text: Mapped[str] = mapped_column(Text)  # native script, never translated
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Which cards this turn rendered (rule 18), so a page reload can draw them
    # again. Oldest row first. Null/empty on every customer turn and on agent
    # turns that showed no cards. TWO ENTRY SHAPES, one union:
    #
    #     {"vehicle_ids": [2, 5], "variant": "compact"|"full"}   # vehicle cards
    #     {"booking_refs": ["TD-2026-0142"]}                     # booking cards
    #
    # Readers key off WHICH KEY IS PRESENT, so the booking shape was purely
    # additive — rows written before booking cards existed still read correctly,
    # which matters because this table is not migrated (the DB is disposable, but
    # a live demo session is not).
    #
    # POINTERS, NOT A SNAPSHOT — and that is the whole design. Storing the card's
    # name, image and price here would freeze display text into a data field, the
    # bug CLAUDE.md calls out four times over: a stored English name cannot be
    # redrawn in an Arabic thread, and a stored price goes stale the moment the
    # catalogue moves. An id survives both. The reload resolves each id through
    # GET /api/vehicles/{id} — the same endpoint "View more" already uses — and
    # each ref through GET /api/booking/{ref}.
    #
    # FOR BOOKINGS THE POINTER RULE IS ALSO A SECURITY RULE. A booking card shows
    # the customer's name and mobile, released only to a session identified as
    # that customer (services/booking._booking_dict). Snapshotting one here would
    # have written that mobile into a table no gate is ever consulted about
    # again; re-fetching re-applies the gate on every single reload.
    #
    # CARDS ONLY. Slot chips are deliberately NOT recorded: a replayed chip pins a
    # slot_id that may have been booked by someone else since. A vehicle_id cannot
    # go stale that way, which is why the two are treated differently. A booking
    # ref is safe for the same reason — and if it has since been cancelled, the
    # GET 404s and the card correctly disappears rather than rendering a shell.
    cards: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)

    # The voice note this turn was spoken as, or NULL for a typed turn (voice
    # notes, 2026-07-21). Customer turns only — Aisha never speaks (TTS stays
    # out of scope), so an agent turn can never carry one.
    #
    # A POINTER, exactly like `cards`, and for the same reason plus a sharper
    # one. `text` already holds the transcript, so this is not how the words
    # survive a reload — it is how the PLAYER is rebuilt. Storing the audio
    # inline here would put megabytes into the transcript table and, worse,
    # would outlive the 48h retention and the "forget me" delete: the row that
    # replays the recording must not be a second copy of it. Resolving through
    # GET /api/audio/{id} means one blob, one lifetime, one deletion.
    #
    # An id whose blob has expired or been erased 404s and the player simply
    # disappears, leaving the transcript text — the same "drop it, never render
    # a shell" rule the booking cards follow (FR-CX-07).
    audio_id: Mapped[str | None] = mapped_column(String(36), nullable=True, default=None)


class VoiceNote(Base):
    """One recorded customer voice note: the blob, plus what we heard in it.

    INPUT ONLY. Aisha replies in text (see the 2026-07-21 scope override) — so
    there is no agent-side counterpart to this table and no `speaker` column.

    WHY THE BYTES LIVE IN A COLUMN. Vehicle imagery is served off a static mount
    (`/media`), and this deliberately is NOT. That media is shipped fixture data,
    identical for every viewer and never deleted; this is customer personal data
    with a 48h expiry and a hard-delete obligation under rule 12. Files on disk
    would put erasure at the mercy of the filesystem — a failed unlink, a stale
    container volume, a backup nobody thought about — and leave recordings
    readable by anyone who can guess a path. In a column, deletion is
    transactional and lives or dies with the audit row that records it.

    THE TRANSCRIPT IS STORED TWICE, ON PURPOSE. It is the ConversationTurn's
    `text` (that is what Aisha answered, and what the transcript must always
    show) and it is `transcript` here (that is what the VENDOR returned, next to
    the audio it came from). They start identical and are allowed to diverge:
    the turn is the conversation, this is the evidence. Keeping only the turn
    would make a mis-transcription unauditable after the fact.
    """

    __tablename__ = "voice_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Public handle, a uuid4 — NOT the primary key. The id appears in URLs the
    # browser holds, and a sequential integer would let anyone enumerate other
    # customers' recordings by counting. The session gate on GET /api/audio is
    # the real control; an unguessable id means that gate is never the only one.
    audio_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    # NULLABLE, and usually null at insert: a customer records before they are
    # identified far more often than after. Backfilled the moment the session
    # resolves to a customer (services/voice.attach_customer), which is what
    # makes "forget me" able to find recordings made earlier in the same
    # conversation. Without the backfill, erasure would silently miss the
    # recordings most likely to exist.
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id"), nullable=True, index=True
    )
    # Whatever the browser produced — webm/opus on Chrome and Android, mp4/aac
    # on Safari. Stored rather than normalised: we transcode nothing, and the
    # <audio> element needs to be told what it is being handed.
    mime: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio: Mapped[bytes] = mapped_column(LargeBinary)
    # What the vendor returned. Empty string on a failed transcription — the row
    # still exists, because the customer did record something and the player
    # must still work even when we could not make out a word.
    transcript: Mapped[str] = mapped_column(Text, default="")
    lang: Mapped[Language] = mapped_column(SAEnum(Language))
    stt_model: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    # Retention horizon (config.VOICE_RETENTION_HOURS). Purged lazily on the next
    # transcribe call and by `python -m app.cli purge-voice`; "forget me" ignores
    # this entirely and deletes at once.
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class SpeechClip(Base):
    """One synthesised reading of one of Aisha's replies (read-aloud, 2026-07-21).

    THE CACHE IS THE POINT. Replaying a message must not re-synthesise it: a
    customer who taps the speaker three times should cost one generation, and
    during a demo the same greeting is played by many attendees. `cache_key`
    hashes the SPOKEN text plus every input that changes the audio (language,
    voice, model), so a prompt tweak or a voice change misses correctly instead
    of serving stale audio that no longer matches the words on screen.

    SCOPED TO A SESSION, NOT GLOBAL, and that costs real cache hits — the same
    greeting is re-synthesised per attendee. It buys something worth more: these
    clips are AISHA'S REPLIES, which quote the customer's own name, mobile and
    booking reference back at them. A globally shared row containing a
    customer's phone number belongs to nobody, which makes "forget me" (rule 12)
    and retention meaningless for it. Per-session, deletion is exactly as simple
    as it is for VoiceNote, and the cost of the trade is pennies.

    NOT LINKED TO ConversationTurn BY FOREIGN KEY, deliberately. The clip is
    derived, disposable and expires on its own schedule; the turn is the
    transcript and must outlive it. A cascade in either direction would be
    wrong — deleting a clip must never touch the conversation, and a purged clip
    must leave the message perfectly readable, just no longer playable.
    """

    __tablename__ = "speech_clips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # sha256 of (spoken text | lang | voice | model). Unique WITH session_id.
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    # The turn this reads, for lookup. An int, matching ConversationTurn.id — but
    # not a FK; see the note above.
    turn_id: Mapped[int] = mapped_column(Integer, index=True)
    lang: Mapped[Language] = mapped_column(SAEnum(Language))
    voice: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    mime: Mapped[str] = mapped_column(String(64), default="audio/mpeg")
    size_bytes: Mapped[int] = mapped_column(Integer)
    audio: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    __table_args__ = (
        UniqueConstraint("session_id", "cache_key", name="uq_speech_session_key"),
    )


class AuditEvent(Base):
    """Append-only audit trail (FR-AD-02). Insert only — never update/delete."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    session: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[Actor] = mapped_column(SAEnum(Actor))
    type: Mapped[AuditType] = mapped_column(SAEnum(AuditType))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class Notification(Base):
    """A WhatsApp-simulator message row. Written as a SIDE EFFECT of the booking
    service — never by a tool. A send_confirmation tool could be forgotten by the
    model, double-fired, or called before the booking exists; a side effect that
    must ALWAYS happen cannot depend on the model remembering to trigger it.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # TD-YYYY-XXXX. NULLABLE since C3 (2026-08-14): a brochure is addressed to a
    # customer, not to a booking, and is usually asked for before one exists.
    # Every other kind still carries it, and the dispatcher resolves the
    # recipient from here first, falling back to customer_id below.
    ref: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True)
    # The recipient when there is no booking to route through (C3). Set only on
    # customer-addressed kinds; booking kinds leave it None and resolve via `ref`,
    # so nothing about the existing rows or their behaviour changed.
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id"), nullable=True, index=True
    )
    kind: Mapped[NotificationKind] = mapped_column(SAEnum(NotificationKind))
    lang: Mapped[Language] = mapped_column(SAEnum(Language))
    # The RENDERED customer-facing text. Empty until the template phase (Layla's
    # Arabic) renders it — an honest "" meaning "nothing has rendered this yet",
    # not a placeholder to be filled with English prose here. Customer-facing
    # copy must not live in the data layer: it would be display text in a data
    # field, and English-only copy breaks the moment the conversation is Arabic
    # (LOC-01). /api/console/notifications publishes this as a string, so it must
    # never carry a JSON blob either.
    body: Mapped[str] = mapped_column(Text, default="")
    # The structured FACTS a template will render from — vehicle name, slot
    # start, location, showroom/service area. Language-neutral by construction.
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(NotificationStatus), default=NotificationStatus.scheduled
    )
    send_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class HandoffToken(Base):
    """A one-time link that carries a WhatsApp customer into the web chat (2026-07-22).

    A customer WhatsApps "change my booking"; we identify them, mint one of these,
    and reply with a link `{PUBLIC_WEB_BASE_URL}/?h=<token>`. Opening it redeems
    the token, which mints a fresh web session and BINDS it to this customer in
    the agent checkpoint — so Aisha greets them already knowing who they are and
    why they came, without a single write ever trusting a client-supplied
    identity (the redeem route reads customer_id from HERE, never from the body).

    SINGLE-USE + SHORT TTL, both deliberate and both security, not tidiness. The
    token is the only credential the link carries; if it were reusable or
    long-lived, a forwarded WhatsApp message would let anyone open a session as
    this customer. `redeemed_at` set once means a second open fails; `expires_at`
    bounds the window before it is even opened. NEITHER is a substitute for the
    other.

    `token` IS an unguessable secret (secrets.token_urlsafe), so it is the lookup
    key, not the primary key — a sequential id in a URL would invite enumeration,
    exactly the reasoning behind VoiceNote.audio_id.

    `customer_id` is NULLABLE: an inbound message from a number we do not know
    still gets a link (to start a fresh booking on the web), it just binds no one
    on redeem. `wamid` is the Meta message id, stored UNIQUE so a webhook Meta
    retries (it retries until it gets a 200) mints one token, not one per
    delivery attempt — idempotency without a separate dedup store.

    DISPOSABLE, like SpeechClip: no FK back to Customer's row lifecycle beyond the
    id, and safe to purge once expired/redeemed. It holds no PII of its own — a
    customer_id and a booking ref, both already elsewhere.
    """

    __tablename__ = "handoff_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # The Meta inbound message id (wamid...). UNIQUE so retried webhook deliveries
    # collapse to one token. Nullable because a token could be minted by a path
    # that is not a webhook (there is none today, but the column should not force
    # one to invent a fake id).
    wamid: Mapped[str | None] = mapped_column(String(128), unique=True, index=True, nullable=True)
    # Whom the link identifies. NULL for an inbound number we could not match to a
    # customer — the link still works, it just binds nobody on redeem.
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id"), nullable=True, index=True
    )
    # The booking the customer referenced, resolved server-side at mint time, so
    # the greeting can name it and get_booking can confirm it. Null when there is
    # no single active booking to point at.
    booking_ref: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # What they seemed to want, parsed from the inbound text: reschedule | cancel
    # | book | general. Shapes the reply copy and the greeting directive; never a
    # committed decision (that still happens conversationally on the web).
    intent: Mapped[str] = mapped_column(String(32), default="general")
    # The language to greet and reply in — detected from the inbound text, or the
    # customer's stored preference.
    lang: Mapped[Language] = mapped_column(SAEnum(Language), default=Language.en)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # Set the moment the token is redeemed. Non-null == spent; a second redeem is
    # refused. The web session it minted is recorded for the audit trail.
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    redeemed_session: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DeviceLink(Base):
    """This browser has been used by this customer before (C10, 2026-08-14).

    THE PROBLEM IT SOLVES. `session_id` is per TAB (sessionStorage) and per
    conversation — close the tab and the customer is a stranger again, and every
    return visit re-asks the name and the mobile of somebody who booked with us
    last week. Recognition needs an identifier that outlives the conversation,
    and that is what this table keys on.

    A HASH, NEVER THE TOKEN. The cookie value is a secrets.token_urlsafe secret;
    what is stored here is its SHA-256. Same reasoning as a password digest: the
    cookie is a bearer credential, so a dump of this table must not hand anybody
    a working one. Lookup hashes the incoming cookie and matches on that.

    ⚠️ THIS IS A HINT, NOT AN AUTHENTICATION. A device is a thing, not a person —
    a shared iPad, a handed-down phone and a showroom tablet all break the
    assumption that holder == customer. So NOTHING here binds a customer on its
    own. The row yields a NAME, the agent asks whether it is still them, and only
    a spoken yes binds the session (services/recognition.confirm). Managing an
    existing booking still goes through identify_by_reference_or_mobile exactly
    as it did before — recognition never became a way in.

    CONSENT IS THE READ GATE, not just the write gate. `lookup` re-checks
    `Customer.memory_consent` on every read, so a customer who withdraws it stops
    being recognised on the next visit even if this row outlives the withdrawal.
    The row is deleted too (memory.forget, erasure.purge) — the re-check is the
    belt to that braces, because a delete that silently failed would otherwise
    keep recognising somebody who told us not to.

    ONE ROW PER (device, customer). A family phone legitimately links two people;
    `last_seen_at` is what picks the one to greet. `token_hash` is therefore
    indexed but NOT unique — the uniqueness is on the pair.
    """

    __tablename__ = "device_links"
    __table_args__ = (
        UniqueConstraint("token_hash", "customer_id", name="uq_device_link"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # SHA-256 hex of the `eg_device` cookie. 64 chars, indexed for the lookup.
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    # Refreshed every time this device is seen as this customer. Picks the most
    # recent person on a shared device, and is what an expiry sweep would read.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    # The session that last confirmed it. Audit trail only — never a lookup key.
    last_session: Mapped[str | None] = mapped_column(String(64), nullable=True)


class FeedbackSurvey(Base):
    """One post-drive feedback survey, conducted over WhatsApp (2026-07-26).

    THE FIRST STATEFUL CONVERSATION THIS SYSTEM HOLDS ON WHATSAPP, and this row
    is the entire state. Until now inbound WhatsApp only ever handed off to the
    web (mint a token, reply with a link) — nothing on that channel had a
    "where were we". A survey does: it asks four questions and has to know which
    one is outstanding when a bare "5" arrives.

    IT IS STILL NOT A SECOND AISHA. No model is involved at any point — the
    questions are fixed strings, the answers are list-reply ids, and `step` is
    advanced by ordinary Python. That is what keeps AD-C intact and keeps this
    off the agent's rails entirely: there is no tool, no prompt change, and the
    web agent cannot see or mention any of it (a satisfaction score the model
    could read is a score it could volunteer, which is the behaviour rule 12
    restrains for memory).

    ONE OPEN SURVEY PER CUSTOMER, enforced in services/feedback rather than by a
    constraint here, because "open" spans three statuses. A second completed
    booking queues as `pending` and sends only once the first closes. Without
    that rule a bare "5" from a customer with two live surveys is genuinely
    unattributable — and guessing would file real feedback against the wrong
    drive, which is worse than asking later.

    SCORES ARE NULLABLE AND STAY THAT WAY. A partial survey keeps what it got;
    the console renders the unanswered ones as em-dashes rather than as zeros. A
    zero would be a score nobody gave, and it would drag every average with it.

    `comment` IS FREE PROSE, the one field here that can hold anything. It is
    also why this table is in the rule 12 erasure sweep and in `would_delete`:
    scores alone are arguably anonymous, but "the salesman with the beard in
    Deira was rude" is not, and a customer told we cleared everything must not
    still be describable in our database.
    """

    __tablename__ = "feedback_surveys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The drive this is about. UNIQUE — one survey per booking, ever, which is
    # the DB-level half of the Booking.feedback_requested_at guard: even a race
    # between two operators clicking Completed cannot open two.
    booking_ref: Mapped[str] = mapped_column(
        String(16), ForeignKey("bookings.ref"), unique=True, index=True
    )
    # Denormalised from the booking on purpose: every inbound reply looks this up
    # by CUSTOMER (we have a phone number, not a ref), and the alternative is a
    # join on every message. Not PII beyond what the booking already holds.
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), index=True
    )

    status: Mapped[FeedbackStatus] = mapped_column(
        SAEnum(FeedbackStatus), default=FeedbackStatus.pending, index=True
    )
    # Which question is outstanding: 0 = not started (awaiting consent to the
    # opening), 1..4 = that rating question, 5 = the open comment. An integer
    # rather than an enum because the state machine only ever adds one to it.
    step: Mapped[int] = mapped_column(Integer, default=0)

    # The language the survey is conducted in — the customer's stored
    # language_pref, resolved once when the survey opens so the whole thread
    # stays in one language even if the preference is later edited.
    lang: Mapped[Language] = mapped_column(SAEnum(Language), default=Language.en)

    # The four scores, 1-5. Null = not answered (see the class docstring).
    q1_overall: Mapped[int | None] = mapped_column(Integer, nullable=True)
    q2_ease: Mapped[int | None] = mapped_column(Integer, nullable=True)
    q3_vehicle: Mapped[int | None] = mapped_column(Integer, nullable=True)
    q4_team: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The open close. Null = never reached; "" = reached and they had nothing to
    # add. Different facts, so they get different values.
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # "later" is honoured EXACTLY ONCE (see services/feedback): the first sets
    # this and re-sends after FEEDBACK_LATER_HOURS; a second is treated as a
    # decline and closes the survey. Asking a third time is nagging, which rule
    # 14 forbids as plainly as it forbids urgency tactics.
    later_count: Mapped[int] = mapped_column(Integer, default=0)

    # ⚠️ THE TIMESTAMPS ON THIS TABLE USE A PYTHON-SIDE `datetime.now` DEFAULT,
    # NOT `server_default=func.now()` LIKE EVERY OTHER TABLE HERE. That deviation
    # is deliberate and load-bearing, so do not "tidy" it back for consistency.
    #
    # `func.now()` is the DATABASE's clock, which is UTC on both SQLite and
    # Postgres, whereas every datetime this service computes — `now` in
    # services/feedback, the cutoff in expire_stale, FEEDBACK_LATER_HOURS — is
    # naive LOCAL time. Mixing the two silently shifts the expiry by the UTC
    # offset: measured on a +04 box, a survey answered one second ago read as four
    # hours idle, so FEEDBACK_EXPIRY_HOURS=24 actually meant 20.
    #
    # `onupdate=` is ALSO deliberately absent, and that is the half that actually
    # bit. With a SQL-expression onupdate, SQLAlchemy renders func.now() into the
    # UPDATE and OVERRIDES an explicitly assigned value — so every
    # `survey.updated_at = now` in the service was being silently discarded and
    # replaced by the DB's UTC clock. Verified, not assumed. Without onupdate,
    # this column has exactly one writer (the service), on one clock.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, index=True
    )
    # When the opening actually went out. Null while `pending`/queued. The expiry
    # is measured from the LAST activity, not from here — see updated_at.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When it reached a terminal status, whichever one.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Touched on every answer. The expiry sweep reads THIS, so a customer part
    # way through gets a fresh window each time they reply rather than being timed
    # out mid-survey because the opening was sent yesterday.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, index=True
    )

    booking: Mapped["Booking"] = relationship()
    customer: Mapped["Customer"] = relationship()




class AgentFeedback(Base):
    """Feedback on Aisha's chat (the AGENT), captured when the conversation
    closes: a 1-5 star rating + optional free-text comment. Distinct from the
    post-drive FeedbackSurvey. Client name / mobile / booking ref are snapshotted
    at write time so the Salesforce sidecar can push a self-contained record.
    Append-only; `synced_at` is the sidecar's restart-safe watermark."""

    __tablename__ = "agent_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    client_name: Mapped[str | None] = mapped_column(String(255))
    mobile: Mapped[str | None] = mapped_column(String(20))
    booking_ref: Mapped[str | None] = mapped_column(String(32))
    stars: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)


__all__ = [
    "LocationType",
    "SlotStatus",
    "BookingStatus",
    "WaitlistStatus",
    "Speaker",
    "Actor",
    "AuditType",
    "NotificationKind",
    "NotificationStatus",
    "Language",
    "Customer",
    "VehicleModel",
    "Showroom",
    "Slot",
    "Booking",
    "Waitlist",
    "MemoryProfile",
    "ConversationTurn",
    "AuditEvent",
    "Notification",
    "HandoffToken",
    "FeedbackStatus",
    "FeedbackSurvey",
    "AgentFeedback",
]


class TurnTrace(Base):
    """One row per customer turn — the router's flight recorder (doc §9).

    WHY THIS EXISTS SEPARATELY FROM LANGSMITH. LangSmith is for debugging one
    conversation: a navigable tree of a single run. This is for the questions you
    can only answer across thousands of them — what is our misroute rate, how
    often does the classifier tie, what does a conversation cost, how much of the
    Salesforce quota does one booking burn, is p95 drifting. Those are aggregates,
    and they need a table we own, in our region, under our retention policy.

    IT IS ALSO THE EVAL PIPELINE. Every misroute observed here becomes a case in
    evals/routing_cases.py. That is the whole maintenance loop for routing
    accuracy: measure in production, pin the failure in CI, never regress it.

    NULLABLE ALMOST EVERYWHERE, DELIBERATELY. A turn that exits at the pre-filter
    never calls the classifier, so it has no confidence and no margin — and that
    absence is the measurement, not missing data: `router_confidence IS NULL`
    counts exactly the turns that cost nothing. Writing 1.0 there instead would
    quietly destroy the statistic this table exists to produce.

    PII. user_message and agent_response carry whatever the customer typed —
    names, mobiles, addresses. This table is therefore in scope for the PDPL
    retention job and for erasure, exactly like conversation_turns.
    """

    __tablename__ = "turn_trace"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    channel: Mapped[str | None] = mapped_column(String(32))

    user_message: Mapped[str | None] = mapped_column(Text)
    detected_lang: Mapped[str | None] = mapped_column(String(8))

    # --- routing ----------------------------------------------------------- #
    routed_to: Mapped[str | None] = mapped_column(String(32), index=True)
    previous_agent: Mapped[str | None] = mapped_column(String(32))
    # WHICH PROMPT ANSWERED THIS TURN (T2.4), e.g. {"base": "v1", "aisha": "v2"}.
    #
    # THIS IS WHAT MAKES A QUALITY CHANGE ATTRIBUTABLE. Prompts are editable
    # without a deploy from Phase 2, so "she got colder last Tuesday" has no
    # commit to point at — the git history of app/agents/ says WHAT changed and
    # this column says WHICH TURNS RAN IT. Without both, a prompt regression is
    # unfalsifiable.
    #
    # Nullable: turns recorded before this column existed carry no value, and a
    # null must read as "not recorded", never as "v1".
    prompt_versions: Mapped[dict | None] = mapped_column(JSON)
    # PER-STEP ORCHESTRATION RECORD (T3.5): which agent ran, how it was chosen,
    # how confident, how long, and whether it worked.
    #
    # DELIBERATELY NO BRIEF TEXT, and that is what unblocks this column. The
    # plan sequenced T3.5 after the erasure work "because the column carries
    # brief text, so it carries the profile" — family size, area, budget, the
    # soft facts a customer volunteers. It does not: what lands here is the
    # DECISION, not the request. So this adds no personal data that
    # `user_message` on this same row does not already hold, and the erasure
    # gap for turn_trace is exactly as it was — still real, still T4.6's.
    #
    # WHY THE SCORES ARE HERE AT ALL. The selection threshold was calibrated on
    # 32 synthetic utterances; these rows are how it gets re-calibrated against
    # real briefs, which is the only way that number stops being a guess.
    steps: Mapped[list | None] = mapped_column(JSON)
    # Which layer of the cascade decided: "prefilter" | "sticky" | "classifier".
    # The share of turns settled without a model call is a cost AND latency
    # metric, and it is the first thing to look at when the voice p95 moves.
    routed_by: Mapped[str | None] = mapped_column(String(16), index=True)
    router_reason: Mapped[str | None] = mapped_column(String(128))
    router_confidence: Mapped[float | None] = mapped_column(Float)
    router_margin: Mapped[float | None] = mapped_column(Float)
    router_second_best: Mapped[str | None] = mapped_column(String(32))
    switched: Mapped[bool] = mapped_column(Boolean, default=False)
    clarified: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- what the turn did -------------------------------------------------- #
    tool_calls: Mapped[list] = mapped_column(JSON, default=list)
    agent_response: Mapped[str | None] = mapped_column(Text)

    # --- what it cost ------------------------------------------------------- #
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    router_latency_ms: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    sf_api_calls: Mapped[int | None] = mapped_column(Integer)
    cache_hits: Mapped[int | None] = mapped_column(Integer)

    # server_default=func.now() like every other table here: the DATABASE's
    # clock, UTC on both SQLite and Postgres. See the note on HandoffToken for
    # why mixing that with a Python-side naive local default silently shifts time.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
