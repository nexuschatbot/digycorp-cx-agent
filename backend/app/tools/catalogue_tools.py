"""Read-only catalogue tools (schema + thin fn). AD-H: plain Anthropic tool use.

Every function here is a THIN wrapper: validate, call the service in-process
(AD-J — never HTTP to our own API), return JSON. No business logic, no DB access.

`db` and `session_id` are injected by the executor and MUST NOT appear in any
input_schema — Claude never sees them and cannot forge them.

Descriptions are prompt engineering, written like briefing a new hire: they say
when to use the tool AND when not to. Vague descriptions produce wrong calls.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import catalogue

# --------------------------------------------------------------------------- #
# search_vehicles (FR-BK-09/10)
# --------------------------------------------------------------------------- #
SEARCH_VEHICLES_SCHEMA = {
    "name": "search_vehicles",
    "description": (
        "Find vehicles in the Elite catalogue that fit what a customer needs. "
        "Use this for needs-based discovery, when the customer describes a "
        "requirement rather than naming a model: 'I need a 7-seater for the "
        "family, around 130k', 'something economical for city driving', 'do you "
        "have any hybrids?'.\n\n"
        "Do NOT use this when the customer names a specific model ('tell me "
        "about the T2', 'what's the G700 like?') — use get_vehicle_details "
        "instead.\n\n"
        "Every argument is optional; pass only what the customer actually told "
        "you and never guess a value to fill the schema. Results come from the "
        "catalogue database — they are the only vehicle facts you may state.\n\n"
        "Read the response carefully:\n"
        "- 'matches' are vehicles meeting ALL the criteria you passed.\n"
        "- 'matches' may be a SELECTION rather than everything that fitted, and "
        "you are not told how many fitted. Never state, guess or imply a number "
        "of models — not how many are in the catalogue, not how many matched. "
        "If they want to see more, do not explain why you cannot; ask what "
        "matters to them and search again on that.\n"
        "- 'basis' on a match says WHY that car was chosen when others also "
        "fitted, and it is the reason you give for recommending it. "
        "most_seats = the roomiest; efficiency = the plug-in hybrid, cheapest "
        "to run; lowest_price = the most affordable; flagship = the "
        "top-of-range. These are CODES — translate them into your own words in "
        "the customer's language, never echo 'basis: lowest_price'. They are "
        "one-per-category picks, NOT a ranking: the first is not 'the best'. A "
        "match with no basis was not chosen over anything — give no reason "
        "rather than inventing one. They also do NOT necessarily include the "
        "top-of-range car, the cheapest, or the roomiest, so you cannot work "
        "out which model is the most or least anything by comparing them. If "
        "that is what the customer asked, search again passing 'basis' and "
        "answer from the ranked result — never from these.\n"
        "- 'ranked_by' (only when you passed basis) names the measure the "
        "matches are sorted by, best first. This is a real ranking and you may "
        "present it as one.\n"
        "- 'catalogue_digest' (only when you passed feature_query) lists every "
        "matching car with its full feature text, so you can answer questions "
        "about equipment. It carries NO PRICES on purpose. Pick the model it "
        "shows you, then call get_vehicle_details on that model before "
        "describing it — that is where price, showrooms and the picture come "
        "from. Never quote a price or claim anything beyond the feature text "
        "from the digest alone.\n"
        "- 'no_exact_match': true means nothing fits. 'nearest_alternatives' "
        "then holds the closest real cars, each with a 'relaxed' field naming "
        "the criterion it fails. Offer these honestly and explain the "
        "trade-off — e.g. our cheapest 7-seater is AED 73,000 when the customer "
        "asked for one under 60,000. Never present an alternative as if it met "
        "the original request.\n"
        "- price_aed_min/price_aed_max are always present: every model in the "
        "catalogue carries a published AED range. Quote the range as given and "
        "never round, estimate or extend it.\n\n"
        "HOW TO WRITE THE ANSWER (rule 17). Describe these in plain "
        "conversational sentences, the way you would text someone. This channel "
        "renders NO markdown, so any markup reaches the customer as literal "
        "characters: never wrap a model name in **asterisks**, never write "
        "'**Price:**' or any other bolded label, no headings, no bullet or "
        "numbered lists. Comparing two or three cars is still prose — a short "
        "paragraph each, not a formatted table."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "body_type": {
                "type": "string",
                "description": (
                    "Loose body-style word, matched as a substring: 'SUV', "
                    "'crossover'. Omit unless the customer indicated a style."
                ),
            },
            "min_seats": {
                "type": "integer",
                "description": (
                    "Minimum seats the customer needs. Matches any model that "
                    "can seat at least this many, including models with an "
                    "optional third row."
                ),
            },
            "max_price_aed": {
                "type": "integer",
                "description": (
                    "The customer's stated budget ceiling in AED. Pass only a "
                    "figure the customer actually gave."
                ),
            },
            "fuel": {
                "type": "string",
                "enum": ["petrol", "phev"],
                "description": (
                    "Powertrain. The catalogue contains ONLY petrol and "
                    "plug-in hybrid (phev). There are no fully electric, "
                    "diesel, or mild-hybrid models. If the customer wants an "
                    "EV, tell them honestly that we do not have one — do not "
                    "substitute 'phev' for 'electric'."
                ),
            },
            "brand": {
                "type": "string",
                "description": (
                    "Brand name. Only 'Jetour' currently exists in the "
                    "catalogue. If a customer asks for a brand we do not carry, "
                    "say so honestly rather than offering a substitute."
                ),
            },
            "feature_query": {
                "type": "string",
                "description": (
                    "What the customer asked about that NONE of the filters "
                    "above can express — equipment, technology or character "
                    "rather than size, budget, fuel or brand. Examples: 'best "
                    "sound system', 'fastest charging', 'good for desert "
                    "camping', 'biggest screen', 'tows a boat'.\n\n"
                    "Pass their own words. This does not filter anything; it "
                    "asks for 'catalogue_digest' in the response — the feature "
                    "list of every car that matched — because those details are "
                    "not searchable any other way. Combine it freely with the "
                    "filters: 'a seven-seater with good audio' is min_seats=7 "
                    "AND feature_query='good audio', which digests only the "
                    "seven-seaters.\n\n"
                    "Pass feature_query ONLY for a detail the catalogue actually "
                    "holds, the equipment and character in these examples. Do "
                    "NOT reach for it to chase something we do not keep at all, "
                    "such as paint or interior colours, warranty terms, stock or "
                    "arrival dates, or an exact power output: there is nothing to "
                    "find, so the search comes back with unrelated cars. Answer "
                    "honestly that you do not have that detail and offer a "
                    "specialist instead."
                ),
            },
            "basis": {
                "type": "string",
                "enum": ["flagship", "lowest_price", "most_seats"],
                "description": (
                    "Ask for cars RANKED on one measure instead of a spread of "
                    "recommendations. Use this whenever the customer asks which "
                    "car is the most or least something — a question with a "
                    "definite answer, not a description of a need.\n\n"
                    "'what's your most expensive car?', 'which is the top of "
                    "the range?', 'the priciest one?' -> flagship. 'the "
                    "cheapest?', 'what do you start at?' -> lowest_price. 'the "
                    "biggest?', 'which seats the most?' -> most_seats.\n\n"
                    "Combines with every other argument, so 'your most "
                    "expensive seven-seater' is basis=flagship with "
                    "min_seats=7.\n\n"
                    "The response then carries 'ranked_by' and the matches ARE "
                    "IN ORDER, best first — this is the one case where you may "
                    "say which car leads. Name the leader and answer the "
                    "question, then briefly note the ones behind it so the "
                    "customer still sees the range; do not describe all three "
                    "as equals when they were asked for one.\n\n"
                    "Do NOT pass this for ordinary discovery. A customer "
                    "describing a requirement — 'something for the family', "
                    "'economical for the city' — wants options that differ from "
                    "each other, and ranking them would return three near "
                    "identical cars instead."
                ),
            },
        },
        "required": [],
    },
}


def search_vehicles(
    db: Session,
    session_id: str,
    body_type: str | None = None,
    min_seats: int | None = None,
    max_price_aed: int | None = None,
    fuel: str | None = None,
    brand: str | None = None,
    feature_query: str | None = None,
    basis: str | None = None,
    **_ignored: object,
) -> dict:
    """Thin wrapper over catalogue.search (AD-J: in-process, never HTTP)."""
    return catalogue.search(
        db,
        body_type=body_type,
        min_seats=min_seats,
        max_price_aed=max_price_aed,
        fuel=fuel,
        brand=brand,
        feature_query=feature_query,
        basis=basis,
    )


# --------------------------------------------------------------------------- #
# get_vehicle_details (FR-BK-04, FR-CX-07)
# --------------------------------------------------------------------------- #
GET_VEHICLE_DETAILS_SCHEMA = {
    "name": "get_vehicle_details",
    "description": (
        "Everything we know about ONE specific model: specification, seats, "
        "price range, headline features, and the showrooms that stock it. Use "
        "this when the customer names a model — 'tell me about the T2', "
        "'what's the G700 like?', 'how many seats in the X70?'.\n\n"
        "Do NOT use this when the customer describes needs without naming a "
        "model — use search_vehicles instead.\n\n"
        "Model names are ambiguous in this catalogue. 'T2' may mean the Jetour "
        "T2 (petrol) or the Jetour T2 i-DM (plug-in hybrid) — genuinely "
        "different cars at different prices. The same applies to 'T1' and "
        "'X70'. If the response has 'ambiguous': true, do NOT choose for the "
        "customer: ask which one they mean, using the 'candidates' to describe "
        "the difference briefly. Booking the wrong variant would not be "
        "discovered until they arrived at the showroom.\n\n"
        "But if the customer has ALREADY told you which variant — 'the petrol "
        "T2', 'the hybrid T1', 'the standard one' — do not send a bare name "
        "that matches both: resolve it yourself and pass the exact full name "
        "('Jetour T2') or the code ('JET-T2'), so the response is the one car "
        "they meant. The ambiguity question is for customers who have not "
        "chosen, never for ones who have.\n\n"
        "If the response is {'error': 'vehicle_not_found'} we genuinely do not "
        "carry that car. Say so honestly. Do not offer the nearest-sounding "
        "model as though it were what they asked for.\n\n"
        "price_aed_min/price_aed_max may be null, meaning the price is not "
        "published. Say that plainly and offer to have a specialist confirm — "
        "never state a number that is not in the response.\n\n"
        "HOW TO WRITE THE ANSWER (rule 17). Describe the car in plain "
        "conversational sentences, the way you would text someone. This channel "
        "renders NO markdown, so any markup reaches the customer as literal "
        "characters: never wrap a model name in **asterisks**, never write "
        "'**Price:**' or any other bolded label, no headings, no bullet or "
        "numbered lists. Pick the two or three things that actually matter to "
        "this customer rather than reciting every field you were given. This "
        "applies to the AMBIGUOUS case too — separating the T2 from the T2 i-DM "
        "is a sentence about each, not a bolded spec sheet."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Model name, as FULLY as the customer said it: pass "
                    "'Jetour T2', never shorten it to 'T2'. An exact full name "
                    "resolves to one car; a shortened fragment matches every "
                    "variant ('T2' hits both the T2 and the T2 i-DM) and "
                    "forces an unnecessary which-one round trip. Send a "
                    "partial name only when the customer themselves was "
                    "partial, and expect candidates back."
                ),
            },
            "code": {
                "type": "string",
                "description": (
                    "Exact catalogue code if you already know it, e.g. "
                    "'JET-T2-IDM'. Prefer this once the customer has resolved "
                    "an ambiguous name."
                ),
            },
        },
        "required": [],
    },
}


def get_vehicle_details(
    db: Session,
    session_id: str,
    name: str | None = None,
    code: str | None = None,
    **_ignored: object,
) -> dict:
    return catalogue.get_details(db, name=name, code=code)


# --------------------------------------------------------------------------- #
# get_showrooms
# --------------------------------------------------------------------------- #
GET_SHOWROOMS_SCHEMA = {
    "name": "get_showrooms",
    "description": (
        "Where our showrooms are and when they are open. Use for 'where are "
        "you located?', 'do you have anywhere in Abu Dhabi?', 'what time do "
        "you close on Sunday?'.\n\n"
        "ALSO CALL THIS BEFORE OFFERING A CHOICE OF BRANCHES during booking. "
        "The available_showrooms list inside a vehicle result gives names only "
        "— no street address — so it can tell you WHERE a car is but cannot "
        "furnish the choice you are about to offer. Call this first whenever "
        "you are about to name more than one branch, or ask which one they "
        "would prefer.\n\n"
        "DO NOT CALL IT TO CONFIRM A BRANCH THE CUSTOMER HAS ALREADY NAMED. "
        "'slots today at Al Quoz' has already answered the branch question — "
        "looking the branch up does not make it more chosen, and it puts a row "
        "of branch options on their screen while you are asking about something "
        "else entirely. Call this when you are about to OFFER a choice, not "
        "when the choice is already made. If you need that named branch's "
        "address or opening hours, do call it — that is a fact you lack, not a "
        "choice you are offering.\n\n"
        "Pass vehicle_id whenever the customer is asking where to see a "
        "PARTICULAR car. Not every showroom stocks every model, so listing all "
        "of them could send a customer to a showroom where their car is not "
        "present. If you are inside a booking and cannot yet name the car, that "
        "is usually a sign you are calling this too early.\n\n"
        "emirate accepts what the customer actually says, including city and "
        "district names ('Al Ain', 'Deira', 'Abu Dhabi', 'Dubai'). If the "
        "response has an empty 'showrooms' list we have nothing there — say so "
        "honestly rather than offering the nearest branch as though it were "
        "what they asked for.\n\n"
        "When you pass emirate, results DELIBERATELY include nearby showrooms "
        "in the same emirate, and each carries 'match_type':\n"
        "- 'city': this showroom is actually in the place they named. Lead with "
        "these — they are already listed first.\n"
        "- 'emirate': same emirate, a different city or district. This is a "
        "genuine alternative, not what they asked for. Only offer it after the "
        "city matches, and name the distance honestly — for 'anything in Al "
        "Ain?', the Al Ain branch is the answer and Abu Dhabi (Rabdan) is 'we "
        "also have one in Abu Dhabi, about an hour and a half away'. Never "
        "present an 'emirate' match as though it were in the city they named.\n"
        "match_type is absent when you did not pass emirate — nothing was named "
        "to match against, so do not infer one.\n\n"
        "Each showroom carries the street address in both scripts: 'address' "
        "(English) and 'address_ar' (Arabic). Read out the one matching the "
        "language of the turn — never the English address in an Arabic reply.\n\n"
        "Do not work out a showroom's location from its name. Use 'city' and "
        "'match_type'; the name is display text and may not mention the place "
        "at all.\n\n"
        "hours_by_day gives opening hours per weekday as [open_hour, "
        "close_hour] on a 24-hour clock; null means closed that day. Phrase "
        "these naturally in the customer's language and on a 12-hour clock; do "
        "not read the raw numbers back."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "emirate": {
                "type": "string",
                "description": (
                    "Emirate, city or district as the customer said it, e.g. "
                    "'Dubai', 'Abu Dhabi', 'Al Ain', 'Deira', 'Al Quoz', "
                    "'Ajman'. Arabic spellings are understood. Omit to list all."
                ),
            },
            "vehicle_id": {
                "type": "integer",
                "description": (
                    "Return only showrooms stocking this vehicle. Use whenever "
                    "the question is about seeing a specific model."
                ),
            },
        },
        "required": [],
    },
}


def get_showrooms(
    db: Session,
    session_id: str,
    emirate: str | None = None,
    vehicle_id: int | None = None,
    **_ignored: object,
) -> dict:
    return catalogue.list_showrooms(db, emirate=emirate, vehicle_id=vehicle_id)
