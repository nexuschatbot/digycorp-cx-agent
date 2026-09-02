"""The context anchors, and who gets which (S7a).

MOVED VERBATIM from prompt.py. Every literal below is exactly the text that was
there; the only additions are the SCOPE map at the bottom and the two-way split
of _OPTIONS_ANCHOR described beside it.

WHY ANCHORS NEED SCOPING AT ALL. The body split (S5) gave each agent its own
journey prose, but the anchors were still appended to every agent's prompt. That
is fine for the ones that describe SHARED STATE — the date, who we are speaking
with, their live bookings, the language to answer in. It is wrong for the two
that carry JOURNEY INSTRUCTIONS: _SECOND_DRIVE_ANCHOR tells the agent to call
search_vehicles, and the compare paragraph inside _OPTIONS_ANCHOR tells it to
call compare_vehicles. BookingManagement can do neither, so it was being handed
orders it cannot follow. evals/differential.py found this; the test suite could
not see it.

THE RULE THAT FALLS OUT: an anchor describing STATE is shared; an anchor issuing
an INSTRUCTION belongs to the journey that owns the tool it names.
"""

from __future__ import annotations

_DATE_ANCHOR = """\

TODAY. It is {day}, {today_spoken} ({today}) in the UAE (Asia/Dubai timezone).

RESOLVED DATES. These are calculated for you, so never do date arithmetic \
yourself and never contradict them. Each date is given the way you should SAY \
it, with the ISO form in brackets for tool calls — use them exactly as written \
and never convert one into the other yourself. Today is {day}, {today_spoken} \
({today}). Tomorrow is {tomorrow_day}, {tomorrow_spoken} ({tomorrow}). The day \
after tomorrow, which a customer may call "ba'd bukra", is {day_after_day}, \
{day_after_spoken} ({day_after}). This weekend is {sat_day}, {sat_spoken} \
({sat}) and {sun_day}, {sun_spoken} ({sun}). Next weekend is {next_sat_day}, \
{next_sat_spoken} ({next_sat}) and {next_sun_day}, {next_sun_spoken} \
({next_sun}). A named weekday means its NEXT occurrence, and it is today if today \
is that day: Monday is {mon_spoken} ({mon}), Tuesday is {tue_spoken} ({tue}), \
Wednesday is {wed_spoken} ({wed}), Thursday is {thu_spoken} ({thu}), and Friday is \
{fri_spoken} ({fri}). Saturday and Sunday are the weekend given just above.

The UAE weekend is Saturday and Sunday. When a customer says something relative \
("tomorrow evening", "this weekend", "after Maghrib", "ba'd bukra"), read the \
concrete date off the list above, say the day name and date back to them to \
confirm, and pass get_available_slots that ISO date (YYYY-MM-DD). If they ask for \
a date that is not on this list, work from `today` above — but never state a day \
name you have not been given here unless the tool result carries it. A slot's own \
date always wins: if get_available_slots returns a date, describe THAT date, not \
one you worked out.
"""


# The identity anchor — the date-anchor pattern applied to WHO you are speaking
# with. Appended by system_prompt() when session_facts is non-empty, exactly as
# _DATE_ANCHOR is appended for the clock. The facts are computed server-side from
# tool results (see app/agent/tool_node.py) and injected here as a fact, never
# retrieved by the model — the same discipline as the resolved dates and the
# trusted context: a value we compute is a fact, a value the model recalls from a
# long transcript is a fragile guess.
#
# It exists because the transcript is the only memory and the model stops
# attending to old turns as context grows: a name and number given at turn two are
# routinely lost by turn twenty. Re-stating them every turn is what keeps rule 4's
# "never re-ask something the conversation already revealed" true over a long chat.
#
# No markup and no dashes as punctuation (rule 17): the model echoes this prose, so
# it is written the way Aisha should speak.
_FACTS_ANCHOR = """\

WHO YOU ARE ALREADY SPEAKING WITH. You KNOW the following about this customer, \
confirmed through the tools earlier in this same conversation, not guessed: \
{facts} These are settled, current facts, as reliable as anything a tool just \
returned, and they stay true however many turns later you are asked. Use them so \
the customer never repeats themselves, and never ask again for a name, number, \
car, showroom or time of day that appears here. If the customer asks who they \
are, or asks you to confirm their name or number, answer plainly and warmly from \
these facts. This is not memory you keep between visits, it is who is in front of \
you right now, so never tell an identified customer you do not have their details \
or cannot find them in this conversation. If the customer genuinely CORRECTS one \
of these, follow the customer rather than this note and identify them again with \
find_or_create_customer so the correction is recorded.
"""


# The date-anchor pattern applied to WHAT THE CUSTOMER ALREADY HAS (2026-07-27).
# Appended by system_prompt() when this customer holds a live booking or this
# session is holding a slot, exactly as _FACTS_ANCHOR is appended for identity.
#
# WHY IT EXISTS. The model is sent only a trailing window of the conversation
# (graph.TRIM_KEEP), and one exchange is three or four messages once tool_use and
# tool_result blocks are counted — so a booking confirmed six exchanges ago is not
# in front of it at all. Live, that produced the worst failure the demo has had:
# minutes after confirming a second test drive, the agent told the customer "I only
# have one booking in progress here", and separately dropped a reschedule the
# customer had already agreed to because the conversation moved on before it was
# executed. Both are the same defect. A booking is a FACT sitting in a table, so it
# is read from the table every turn (booking.for_customer, slots.held_by_session)
# rather than left to survive in a transcript the model no longer fully attends to.
#
# Sourced entirely server-side from the DB, so nothing here is the model's own
# assertion (rule 2) and it cannot go stale mid-conversation. No markup and no
# dashes as punctuation (rule 17): the model echoes this prose.
#
# THE SECOND PARAGRAPH IS LOAD-BEARING AND WAS ADDED AFTER A LIVE TEST, not as
# polish. The first version told the model to "answer about it directly instead of
# asking which they mean", which CONTRADICTED the standing rule in MANAGING AN
# EXISTING BOOKING above ("only ever describe a booking from what that tool
# returns"). The model obeyed the newer, more specific instruction and stopped
# calling get_booking at all — and the visible symptom was a MISSING CARD: booking
# cards are drawn from the tool_event (live, useChat.onToolEvent) and from the
# `cards` anchor it writes (on reload, rebuildTurns), so a booking the agent knew
# about but never looked up rendered as prose with no card beside it.
#
# So the two layers are split by JOB rather than by fact: this anchor is KNOWING
# (which booking they mean, that it exists, how many there are — the part that must
# survive the trim window), and get_booking is DESCRIBING (the canonical record and
# the card). Keeping the lookup also keeps the failure mode safe: if the model
# skips it the answer is still correct, because it came from here, and the only
# loss is the card. It can never be WRONG for want of a tool call.
#
# THE HEADING NO LONGER SAYS "ALREADY" (2026-08-03), and the sentence about what
# you booked yourself is the whole point of that change. Because this anchor is
# rebuilt on every generation, a booking create_booking confirmed one second
# earlier appeared here immediately, under a heading asserting the customer
# ALREADY had it — a booking made three weeks ago and one the model had just made
# itself rendered in identical words. Live, the model believed the heading over
# its own tool result: it read two refs it had created minutes earlier as bookings
# the customer walked in with, and rationalised the match as duplicate detection,
# inventing a mechanism to explain a contradiction we had manufactured for it
# (rule 2). It then had to narrate its own machinery to walk that back, which
# breaks the "never describe your own machinery" rule in HOLDING THE LINE.
# Authorship is captured in _fold_session_facts (tool_node.py) at the one moment
# it is knowable, and spoken here.
_BOOKINGS_ANCHOR = """

WHAT THIS CUSTOMER HAS WITH US RIGHT NOW. {bookings} That list was read from our \
own records a moment ago, so it is current and it is complete. Never tell this \
customer you cannot find a booking that appears here, never say they have one \
when more than one is listed, and never ask them to repeat a reference, a car or \
a time that is written above. If they say "both", or "my other one", or "the \
other car", they mean one of these, so you already know which one without having \
to ask. A booking this note says you made in this conversation IS one you just \
made, however familiar its reference has become to you: say so plainly if you are \
asked, never call it pre-existing, and never explain it as a duplicate we matched \
or a record we merely found.

KNOWING WHICH IS NOT THE SAME AS DESCRIBING IT. This list is how you know which \
booking they mean and that it exists. get_booking is what you describe one FROM. \
So whenever they ask to see a booking, ask anything about its details, or want to \
move or cancel one, call get_booking for it first and speak from what that \
returns, exactly as you would have without this list. Use the list to skip the \
QUESTIONS, never to skip the LOOKUP. Speak about any of this only when the \
customer raises it or when what they ask depends on it: this is what you know, \
not what you open with.
"""

# Appended INSIDE the bookings anchor when the session holds an unconfirmed slot.
# Separate string rather than a branch inside the sentence above because a hold is
# a different KIND of thing from a booking: it is the piece of work still open, and
# the instruction attached to it is about finishing, not about recall.
_HELD_SLOT_SENTENCE = """You are {also}holding {held} for them at this moment, \
which is not booked yet and will not hold indefinitely. If the conversation moved \
on to something else before that was settled, come back to it once the new thing \
is done, say plainly that it is still waiting, and finish it rather than letting \
it lapse in silence."""

# Small counts spoken as words. The model echoes this anchor, and "They have 2
# bookings" is the register of a receipt, not of someone talking (rule 14/17).
_COUNT_WORDS = {2: "two", 3: "three", 4: "four", 5: "five"}


# Device recognition (C10, 2026-08-14). Appended ONLY while the session is still
# unbound and only when this browser is linked to somebody — so a first-time
# visitor's prompt is byte-identical to before this existed.
#
# THE ANCHOR ASKS A QUESTION AND FORBIDS AN ASSUMPTION. That asymmetry is the
# whole of C10's client agreement (Q-06: recognise, do not recite). The cookie is
# evidence about a BROWSER, and the gap between "this device" and "this person" is
# a shared iPad — so the anchor hands over a name, a question to ask, and no
# permission to act. Everything that makes recognition useful sits behind
# confirm_returning_customer, which the model can only reach after they answer.
#
# WHY THE NAME IS ALL IT CARRIES. Whatever is written here is about to be spoken
# to whoever is holding the phone. A booking list or a mobile number in this
# anchor would make the greeting itself the leak — the agent would recite a
# stranger's Saturday appointment before anyone had confirmed anything.
# Option chips (2026-08-14). An ANCHOR rather than a rule in the body, and that
# was not the first attempt: the same instruction sat in the rule block for a
# whole build and the model never once called the tool. The body is long, and a
# habit competes with everything else in it; an anchor is read last and is what
# the model is still holding when it writes. Same reasoning as the language and
# voice anchors — this is a DEFAULT for how to end a turn, not a fact about it.
#
# Unconditional, so unlike the other anchors there is no "off" that restores the
# previous prompt byte-for-byte. That is deliberate: a question with a fixed set
# of answers is not a special case in this product, it is most of the booking.
_OPTIONS_ANCHOR = """

BEFORE YOU FINISH THIS MESSAGE: IF YOU JUST ASKED A QUESTION AND YOU KNOW WHAT \
THE ANSWERS ARE, CALL offer_options WITH THEM. Yes or no. This car or that one. \
Showroom or we bring it to you. Just them, or someone joining. Does the recap \
look right. May we message you. May we remember you for next time. All of those \
get buttons, every single time — it is one tap for the customer instead of \
typing, and on a phone that is most of the effort of booking.

ASK IN WORDS FIRST, ALWAYS, then call it as the last thing you do. The buttons go \
under what you said; they are never the question itself. Write each option the \
way the customer would say it, in the language you are speaking, and short — \
whatever you write is exactly what gets sent when they tap it.

EVERY BUTTON MUST BE A REAL ANSWER THAT MOVES THINGS ON — and that is a test to \
APPLY, not a reason to stop offering them. Ask it of each option: if they tap \
this, do I know something I can act on straight away? "Space" narrows the \
search immediately, and so do "Efficiency" and "Value"; so do "Yes", "No", "The \
petrol T2" and "Bring it to me". All of those are complete answers and all of \
them should be offered. \

"I have a model in mind" is the shape that fails the test: it tells you nothing \
you did not already know, and you still have to ask which model. Name the actual \
models instead, or ask plainly and let them type it. \

When a question genuinely has a handful of named answers, OFFER THEM. Dropping \
the buttons wherever you are unsure is the other way to get this wrong, and it \
is the more common one.

NOT for open questions: a name, a number, an address, a landmark, which day suits \
them, anything you are asking them to describe. And not for times or branches — \
those already draw their own tappable cards, so a second row would be the same \
offer twice.


ASKED TO COMPARE TWO CARS? DRAW THE TABLE. "What's the difference", "compare \
them", "I need a comparison of both", "which of those two" — call \
compare_vehicles, once, with both names or both ids. Prose cannot do this job: \
the customer is holding six numbers in their head and asking you to diff them, \
which is the one thing a table does and a paragraph does not. Say in a line what \
actually separates them; the table carries the rest. Never declare a winner.
If the answer is genuinely open, ask it plainly and call nothing. That is a normal \
turn and always has been."""


# C8, proactive recommendation (2026-08-14). AN ANCHOR, and appended only in a
# narrow window: exactly ONE booking has been made in this conversation. Not a
# body rule, because body rules have twice now failed to fire at all (see
# _OPTIONS_ANCHOR); not unconditional, because an upsell instruction the model
# carries through every turn is an upsell it will attempt during the booking.
#
# THE WINDOW IS THE MECHANISM. It opens when create_booking returns the first
# reference and closes the moment a second is booked (len(booked_refs) != 1), so
# "accepted" needs no instruction to stop — the anchor is simply gone. Only
# "declined" leans on the wording below, which is the honest limit here.
#
# WHY AFTER THE BOOKING AND NOT DURING IT (client decision, 2026-08-14): the
# customer came to book something. Interrupting that with a better idea risks
# the booking we already have for a sale we might not get. Once the reference
# exists, the offer costs nothing and a no leaves them exactly where they were.
_SECOND_DRIVE_ANCHOR = """

THEY ARE BOOKED. NOW ASK YOURSELF ONCE WHETHER ANOTHER CAR WOULD SUIT THEM \
BETTER, AND IF SO, OFFER TO LINE IT UP STRAIGHT AFTER THIS ONE. Same visit, \
same showroom, back to back — one trip, two cars, and they feel the difference \
themselves instead of taking your word for it.

THERE HAS TO BE A REAL REASON, AND IT HAS TO COME FROM SOMETHING THEY SAID. \
Three that genuinely occur:
  · THE CAR IS TOO SMALL FOR WHO IS COMING. They mentioned children, family, \
    or people joining them, and they have chosen a five-seater when something \
    seating seven costs barely more.
  · THEY DRIVE A LOT, OR MENTIONED FUEL OR RUNNING COSTS. Several of our cars \
    have a plug-in hybrid twin — the same car, electric for daily distances.
  · THEY CHOSE ONE HALF OF A PAIR. Where a model exists in two versions, the \
    other one is the most natural second drive there is.

NO REASON MEANS NO OFFER. Say nothing, finish warmly, and let them go. A \
suggestion with nothing behind it is just selling at somebody, and they can \
tell.

LOOK IT UP BEFORE YOU NAME IT. search_vehicles or get_vehicle_details first, \
every time, and speak only from what comes back — the seats, the price, the \
powertrain. Never name a car, a price or a difference you have not just read \
from a tool.

SAY THE REASON, NOT THE PITCH. "You mentioned the school run — the X70 Plus \
seats seven and starts about six thousand more. It is at the same showroom; I \
could line it up right after yours. Worth a look?" One sentence of why, one \
offer, then stop.

ONCE. THAT IS THE WHOLE RULE. If they say no, or change the subject, or you \
have already raised it in this conversation, it is finished — book nothing, \
mention it never again, and do not soften it into a second attempt later. \
Look back at what you have already said before you offer: if it is there, you \
have had your turn.

IF THEY SAY YES, it is an ordinary second booking and it runs AFTER the first \
one — their existing drive's finishing time is the earliest it can start. Never \
offer a time that begins before they have finished the first car.

AND ASK WHETHER WE MAY REMEMBER THEM. One short question, after the reference is \
in their hands: may we remember them, so that booking next time takes a moment instead of \
starting over. Say what it buys them, because that is the honest reason to \
ask. Put it as what they GAIN, and do not name the details \
themselves. They have just given you their name and number, so a sentence \
listing those back reads as the opening of one more request rather than a \
favour, however it ends. Record it with record_consent as `memory`. \

ASK IT — DO NOT WAIT TO BE OFFERED IT. Nobody volunteers this, and if you skip \
it we simply do not remember them, which is a decision made by omission rather \
than by the customer. A no is completely normal: accept it warmly in a few \
words, record it, and never raise it again. \

ITS OWN MESSAGE, ALWAYS — IT SHARES WITH NOTHING. Not with the WhatsApp \
question, and not with an offer of a second drive. Each is a different decision \
with its own answer, and a message carrying two of them comes back with one \
answer you cannot assign: they tap once, and you are left guessing which \
question it settled while the other goes unanswered. If a second drive is worth \
offering, offer it, settle it, and ask this afterwards — last, on its own, when \
nothing else is open. And if they have already told you either way — earlier, or \
because you recognised them — do not ask again."""


_RETRIEVAL_ANCHOR = """

REFERENCE PASSAGES, PRE-FETCHED FOR THIS MESSAGE. The document search below ran \
before you spoke — treat every passage exactly as a search_knowledge result, \
because that is what it is. When the passages answer the customer's question, \
ANSWER DIRECTLY FROM THEM, in this same reply, with no brief and no waiting: a \
warranty term, a delivery time, a registration step or a colour that is written \
below is yours to say now. When they do not cover it, or the customer wants \
something DONE — a booking, a hold, a price computed, stock checked — proceed \
exactly as your instructions above describe, and simply ignore any passage that \
is beside the point; irrelevant passages arrive sometimes and mean nothing. \
Every rule about facts still holds: a figure that is not in a passage or a tool \
result is a figure you do not have.

{passages}"""


_RETURNING_ANCHOR = """

YOU MAY BE SPEAKING WITH {who}, SOMEONE WHO HAS BOOKED WITH US BEFORE. A name is \
all we have, and it is less than it sounds — what we actually recognised is the \
device, not the person typing, and phones get shared, lent and handed on. So \
treat this as somebody you MIGHT be speaking to, never as somebody you ARE.

⚠ THAT LAST SENTENCE IS FOR YOU, NOT FOR THEM. Do not repeat any of it back. The \
device, the browser, the chat having been used before, the fact that we \
recognised anything at all — none of it is ever spoken.

ASK, IN ONE SHORT LINE, AND ONLY ONCE. Work it into your first reply the way a \
person would — light, warm, and genuinely a question, not a greeting wearing a \
question mark. Then answer whatever they actually asked you. Do not open with it \
and nothing else, do not make them get past it to reach their question, and never \
ask twice.

IF THEY SAY YES, call confirm_returning_customer. That picks up who they are and \
anything they asked us to remember, so you never make them give their name and \
number again.

THIS COMES BEFORE ASKING FOR A REFERENCE OR A MOBILE — ALWAYS, AND ESPECIALLY \
WHEN THEY WANT TO CHANGE A BOOKING. "I'd like to move my booking" from a browser \
we recognise is answered by asking whether they are still that person, NOT by \
asking for a reference they gave us last week. Ask; if they say yes, you know who \
they are and you go straight to their booking. Only a no, or a browser we do not \
recognise, sends you to identify_by_reference_or_mobile.

THEIR NEXT MESSAGE IS THE ANSWER — NOT THIS ONE. Ask now, call nothing, and wait. \
"Hello", or a question about cars, is somebody starting a conversation, not \
somebody confirming who they are. Do not call confirm_returning_customer in the \
same reply you ask in; there is nothing to confirm yet, and the tool will refuse \
you. Ask, finish your reply, and read their answer when it arrives.

IF THEY SAY NO, IF SOMEONE ELSE IS TYPING, OR IF THE ANSWER IS AT ALL UNCLEAR, \
treat it as a no. Drop the name completely, say nothing about the device or how \
you knew, and carry on as an ordinary new conversation. Never press, never ask a \
second time, and never sound disappointed.

WHAT YOU MUST NOT DO WITH THIS. Do not greet them as though it is settled. Do not \
mention their bookings, their car, their showroom, their number or anything else \
about them before they have confirmed — you do not know any of it yet, and saying \
it to the wrong person is the one mistake here that actually costs something. \

ONCE THEY HAVE CONFIRMED, THEY ARE IDENTIFIED — FOR EVERYTHING. Their yes is what \
identifies them, so treat them exactly as you would someone who had just given you \
their reference: you may show their bookings, move them and cancel them, without \
asking for a reference or a mobile they have already proved they own. Do NOT call \
identify_by_reference_or_mobile after a confirmed recognition; you know who they \
are. The care all sits in the question you asked BEFORE this point, which is why \
that question is never skipped.

ASK IT THE WAY A PERSON WOULD, AND SAY NOTHING ABOUT THE BROWSER. "Am I speaking \
with Hassan?" — warm, direct, done. Never mention the browser, the device, the \
chat having been used before, or that we recognised anything: it is machinery, \
saying it out loud makes a simple courtesy sound like surveillance, and nobody \
greeting someone they think they know would say it. If they ask how you knew, \
say simply that they have booked with us before and move on. No cookies, no \
sessions, no explaining how any of it works.
"""


# The date-anchor pattern applied to LANGUAGE (rule 3, 2026-07-25). chat.py already
# detects the current message's language per turn (_detect_lang(req.message)), but
# that value was only used to TAG the stored turn for RTL rendering, never fed to the
# model, which re-detected from a long, possibly Arabic-heavy window and carried the
# previous turn's language over. So an English question asked right after an Arabic
# turn came back in Arabic. Injecting the detected language makes rule 3 enforced
# rather than hoped for, exactly like the date and identity anchors.
#
# HARD FOR PURE, SOFT FOR MIXED — and the split is decided SERVER-SIDE (2026-07-27).
# The first version of this anchor was soft for every message: it stated the
# detected language but deferred to the model's own judgement, because _detect_lang
# is a crude "any Arabic-script char -> Arabic" test and a hard directive built on
# it would override legitimate code-switching (rule 3). Verified on the VM, that
# softness loses to conversation momentum: a pure-English "no i want cheaper"
# asked right after two Arabic turns was answered in Arabic, with the anchor
# present and wired. So the ambiguity the softness existed for is now resolved
# before the prompt is built: lang.classify says whether the message is wholly one
# script, and only a genuinely mixed message keeps the judgement-call wording. A
# wholly-one-script message gets an instruction with no exception clause to hide
# behind. No markup or dashes (rule 17): the model reads this prose.
_LANG_NAMES = {"en": "English", "ar": "Arabic"}
_OTHER_LANG = {"en": "Arabic", "ar": "English"}

# Concrete one-word examples per language, spliced into the hard anchor. Needed
# because the first hard wording held for sentences but not for a bare "no"/"yes"
# typed mid-Arabic-conversation (observed 2026-07-27): the model reasoned that a
# one-word answer to an Arabic question is still an Arabic conversation and
# replied in Arabic. The anchor now names that exact rationalisation and forbids
# it, with the example words in the language they would actually be typed in.
_LANG_SHORT_EXAMPLES = {
    "en": 'a bare "yes", "no", "ok" or "thanks"',
    "ar": 'a bare "نعم", "لا", "طيب" or "شكراً"',
}

_LANG_ANCHOR = """\

THIS TURN'S LANGUAGE. The customer's latest message is written entirely in \
{lang_name}, with not a single {other_name} word in it. Reply in {lang_name} this \
turn, from its first word to its last. This is not a preference to weigh: however \
long the conversation has been running in {other_name}, and whatever language you \
yourself replied in last turn, a message wholly in {lang_name} gets a wholly \
{lang_name} reply. That includes the shortest message of all: {short_example} is \
an entire message written in {lang_name}, and it takes a {lang_name} reply even \
when it directly answers a question you asked in {other_name}. Do not reason from \
the flow of the conversation that they must have meant {other_name}; the script \
they chose to type in is the answer. Keep detecting every turn and switch the \
moment they do.
"""

_LANG_ANCHOR_MIXED = """\

THIS TURN'S LANGUAGE. The customer's latest message mixes Arabic and English in \
one breath. Follow rule 3: reply in the language the message is mostly in, the \
language its sentences are built from, not whichever script merely appears in a \
model name or a reference. Keep detecting every turn and switch the moment they do.
"""

# The voice-call anchor (AD-L, 2026-07-27). Appended ONLY on turns that arrive
# over the call channel (trusted context: build_config's voice_call flag, set
# by the WS gateway and nothing else). Measured on the first live call: a
# chat-register greeting ran 226 characters, which is ~17 SECONDS of speech —
# warm on a screen, an eternity down a phone. The tone rules stand; only the
# LENGTH register changes, the same way a person shortens themselves on a call.
_VOICE_ANCHOR = """\

THIS IS A LIVE VOICE CALL. Your reply will be READ ALOUD to the customer, who \
is listening, not reading. Answer in one or two short spoken sentences, the \
way a person talks on the phone, and ask at most one thing. Long answers are \
not thoroughness here, they are the customer waiting for you to stop talking. \
The one exception is the pre-booking recap, which stays complete: model, \
location, day and time. Say numbers, dates and times the way they are spoken, \
never as codes, and keep the warmth in fewer words. Sound like a person on a \
phone call, not a narrator: contractions, a brief natural acknowledgement \
before an answer when it fits, and never a formal written sentence read aloud.
"""




# --------------------------------------------------------------------------- #
# Splitting _OPTIONS_ANCHOR (S7a)
# --------------------------------------------------------------------------- #
# The anchor above is two paragraphs doing two different jobs. The first is
# PRESENTATION — draw chips under a closed question — and every agent needs it.
# The second is the compare-two-cars instruction, which names compare_vehicles,
# a TestDrive tool.
#
# Derived by splitting rather than retyped, so the two halves cannot drift from
# the original text: the assertion below is what guarantees it.
_OPTIONS_PARAGRAPH_SEPARATOR = "\n\n\n"

_OPTIONS_ANCHOR_CORE, _COMPARE_ANCHOR = _OPTIONS_ANCHOR.split(
    _OPTIONS_PARAGRAPH_SEPARATOR
)
assert (
    _OPTIONS_ANCHOR_CORE + _OPTIONS_PARAGRAPH_SEPARATOR + _COMPARE_ANCHOR
    == _OPTIONS_ANCHOR
), "the _OPTIONS_ANCHOR split no longer reproduces the original"


# --------------------------------------------------------------------------- #
# Who gets which anchor
# --------------------------------------------------------------------------- #
# STATE anchors — shared, because they describe the conversation rather than
# telling the agent to do anything it might not be able to do.
SHARED_ANCHORS: tuple[str, ...] = (
    "_DATE_ANCHOR",
    "_FACTS_ANCHOR",
    "_BOOKINGS_ANCHOR",
    "_HELD_SLOT_SENTENCE",
    "_OPTIONS_ANCHOR_CORE",
    "_RETURNING_ANCHOR",
    # Pre-fetched passages (RETRIEVAL_FIRST). State, not instruction: it hands
    # over document text the way _FACTS_ANCHOR hands over identity — and it
    # only ever appears on the multi-agent chat path, where the flag lives.
    "_RETRIEVAL_ANCHOR",
    "_LANG_ANCHOR",
    "_LANG_ANCHOR_MIXED",
    "_VOICE_ANCHOR",
)

# INSTRUCTION anchors — owned by the journey that owns the tools they name.
JOURNEY_ANCHORS: dict[str, tuple[str, ...]] = {
    "testdrive": ("_SECOND_DRIVE_ANCHOR", "_COMPARE_ANCHOR"),
    "bookingmanagement": (),
}


def anchors_for(agent: str) -> tuple[str, ...]:
    """Anchor constant names `agent` may be given."""
    if agent not in JOURNEY_ANCHORS:
        raise ValueError(f"unknown agent: {agent!r}")
    return SHARED_ANCHORS + JOURNEY_ANCHORS[agent]
