"""The per-turn language anchor (rule 3, FR-CX-02/03) — hard for pure, soft for mixed.

Pins the 2026-07-27 hardening: the original anchor was soft for every message and
lost to conversation momentum on the VM (a pure-English "no i want cheaper" after
two Arabic turns was answered in Arabic). The ambiguity the softness existed for
is now resolved server-side by lang.classify, so only a genuinely mixed-script
message keeps the judgement-call wording. DB-only and offline: nothing here
touches a model or the network.
"""

from app.lang import classify, detect
from app.agent.prompt import system_prompt


# --- classify: the server-side split ---------------------------------------

def test_pure_english_classifies_en():
    assert classify("no i want cheaper") == "en"


def test_pure_arabic_classifies_ar():
    assert classify("ما هي سيارتكم الرائدة؟") == "ar"


def test_arabic_with_a_model_name_is_mixed():
    # The code-switching case rule 3 defers on: an Arabic sentence carrying a
    # Latin-script model name must NOT earn a hard directive either way.
    assert classify("أبغى أجرب الـ Jetour T2") == "mixed"


def test_script_neutral_text_classifies_en():
    # Digits and punctuation belong to no language; a bare mobile falls back to en.
    assert classify("+971505550103") == "en"


def test_classify_never_raises_on_empty():
    assert classify("") == "en"
    assert classify(None) == "en"


def test_classify_and_detect_stay_distinct_on_mixed():
    # detect answers the storage question (mixed tags the turn ar for RTL);
    # classify answers the prompt question. They must not be collapsed.
    text = "أبغى أجرب الـ Jetour T2"
    assert detect(text) == "ar"
    assert classify(text) == "mixed"


# --- system_prompt: which anchor is appended -------------------------------

def test_pure_english_turn_gets_a_hard_english_directive():
    prompt = system_prompt(current_lang="en")
    assert "written entirely in English" in prompt
    assert "gets a wholly English reply" in prompt


def test_pure_arabic_turn_gets_a_hard_arabic_directive():
    prompt = system_prompt(current_lang="ar")
    assert "written entirely in Arabic" in prompt
    assert "gets a wholly Arabic reply" in prompt


def test_hard_anchor_names_the_one_word_case():
    # A bare "no" typed mid-Arabic-conversation was still answered in Arabic
    # (2026-07-27): the model reasoned from the conversation's flow. The anchor
    # must name one-word acknowledgements in the turn's own language and forbid
    # that exact rationalisation.
    en = system_prompt(current_lang="en")
    assert '"yes", "no"' in en
    ar = system_prompt(current_lang="ar")
    assert '"نعم", "لا"' in ar
    for p in (en, ar):
        assert "Do not reason from the flow of the conversation" in p


def test_mixed_turn_keeps_the_judgement_call():
    prompt = system_prompt(current_lang="mixed")
    assert "mixes Arabic and English" in prompt
    assert "written entirely in" not in prompt


def test_no_lang_appends_no_anchor():
    # Kickoff directives and failed voice notes pass turn_lang=None: the message
    # is not the customer's words, so it is no evidence of their language. The
    # BODY still names "THIS TURN'S LANGUAGE" (the deference clause refers to
    # the note), so assert on the anchors' directive text, not the title.
    prompt = system_prompt(current_lang=None)
    assert "written entirely in" not in prompt
    assert "mixes Arabic and English in one breath" not in prompt
