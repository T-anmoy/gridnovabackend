"""BE-7 N1 — Hindi/Bengali replies are generated in English, guarded in English, then
translated under deterministic checks. A translation that says anything the guarded
English did not is never sent: the English goes out and `translation_rejected` is
recorded against the lead."""
import pytest

from app.agents import guardrails, indic, translation_check as tc
from app.llm import client as llm_module
from app.memory.models import MessageRole

EN = ("Thanks, Anil — a 5 kW rooftop is a common size for us. Would Tue 20 Oct, 11:00 IST suit "
      "for a short call? You can also write to info@gridnova.in.")
HI = ("धन्यवाद, Anil — 5 kW रूफटॉप हमारे लिए एक आम साइज़ है। क्या मंगलवार 20 अक्टूबर, 11:00 IST पर "
      "एक छोटी कॉल ठीक रहेगी? आप info@gridnova.in पर भी लिख सकते हैं।")
BN = ("ধন্যবাদ, Anil — 5 kW রুফটপ আমাদের কাছে একটি সাধারণ সাইজ। মঙ্গলবার 20 অক্টোবর, 11:00 IST-এ "
      "একটি ছোট কল কি সুবিধাজনক হবে? আপনি info@gridnova.in-এ লিখতেও পারেন।")


def reasons(english, translation, locale, **kw):
    return tc.verify_translation(english, translation, locale, **kw).reasons


# ── Normalisation (step 6: the existing checks read Hindi/Bengali) ─────────────

@pytest.mark.parametrize("text, value", [
    ("३ लाख", 300_000), ("৮ লাখ টাকা", 800_000), ("आठ लाख", 800_000), ("তিন লাখ", 300_000),
    ("₹८,००,०००", 800_000), ("2 करोड़", 20_000_000), ("२ करोड", 20_000_000), ("৫ হাজার", 5_000),
    ("50 हज़ार रुपये", 50_000), ("1.5 লক্ষ", 150_000), ("डेढ़ लाख", 150_000), ("5000 টাকা", 5_000),
])
def test_indic_amounts_read_as_their_value(text, value):
    assert value in guardrails._numbers_in(text)


def test_english_text_is_untouched_by_normalisation():
    for text in ("we can do this for about eight lakh", "₹45,000", "Tue 20 Oct, 11:00 IST", ""):
        assert indic.normalise(text) == text


def test_a_customers_hindi_figure_justifies_the_english_one_and_vice_versa():
    assert guardrails.check_money("3 lakh a month is a lot to carry.", "हमारा बिल ३ लाख है") is None
    assert guardrails.check_money("৩ লাখ অনেক টাকা।", "our bill is 3 lakh") is None
    assert guardrails.check_money("We can do it for 8 lakh.", "बिल ३ लाख है")["code"] == guardrails.NEW_MONEY


@pytest.mark.parametrize("day, english", [("सोमवार", "Monday"), ("शुक्रवार", "Fri"), ("রবিবার", "Sunday"),
                                          ("বৃহস্পতিবার", "Thursday")])
def test_indic_day_names_match_their_english_slot(day, english):
    assert guardrails.check_times(f"{day} 11:00 ठीक रहेगा?", f"{english} 11:00") is None
    assert guardrails.check_times(f"{day} ठीक रहेगा?", "Wednesday")["code"] == guardrails.UNOFFERED_TIME


def test_a_reply_written_in_hindi_is_blocked_because_no_check_can_read_it():
    result = guardrails.enforce("आपको सब्सिडी ज़रूर मिलेगी और 25 साल की वारंटी है।", allowed_text="",
                                fallback="fallback")
    assert result.blocked and guardrails.UNREADABLE_LANGUAGE in result.codes
    # A Hindi name or a quoted word inside an English reply is fine.
    assert guardrails.check_language("Thanks, Anil — your site in कोलकाता sounds good.") is None


# ── Translation checks: allowed ────────────────────────────────────────────────

@pytest.mark.parametrize("locale, text", [("hi", HI), ("bn", BN)])
def test_a_faithful_translation_passes(locale, text):
    assert reasons(EN, text, locale) == []


def test_allowed_statements_the_english_made_may_be_translated():
    en = "Our standard warranty is set out in your proposal. Whether you qualify for the subsidy is decided by the scheme."
    hi = "हमारी मानक वारंटी आपके प्रस्ताव में दी गई है। आप सब्सिडी के लिए पात्र हैं या नहीं, यह योजना तय करती है।"
    bn = "আমাদের স্ট্যান্ডার্ড ওয়ারেন্টি আপনার প্রস্তাবে দেওয়া আছে। আপনি ভর্তুকির যোগ্য কি না, তা প্রকল্প ঠিক করে।"
    assert reasons(en, hi, "hi") == [] and reasons(en, bn, "bn") == []


def test_indic_digits_in_a_translation_match_the_english_figures():
    en = "PM Surya Ghar pays ₹30,000/kW for the first 2kW."
    hi = "पीएम सूर्य घर योजना पहले २kW के लिए ₹३०,०००/kW देती है।"
    assert reasons(en, hi, "hi") == []


def test_mock_translation_skips_only_the_script_check():
    assert reasons(EN, f"[MOCK] {EN}", "hi", mock=True) == []
    assert any("target script" in r for r in reasons(EN, EN, "hi"))


# ── Translation checks: blocked ────────────────────────────────────────────────

@pytest.mark.parametrize("locale, base, addition, expect", [
    ("hi", HI, " यह आठ लाख रुपये में हो जाएगा।", "money differ"),
    ("bn", BN, " এটা ৮ লাখ টাকায় হবে।", "money differ"),
    ("hi", HI, " आपको 40% बचत होगी।", "numbers differ"),
    ("hi", HI, " 25 साल की वारंटी मिलेगी।", "warranty statement"),
    ("bn", BN, " ২৫ বছরের ওয়ারেন্টি পাবেন।", "warranty statement"),
    ("hi", HI, " आपको सब्सिडी ज़रूर मिलेगी।", "subsidy certainty"),
    ("bn", BN, " আপনি অবশ্যই ভর্তুকি পাবেন।", "subsidy certainty"),
    ("hi", HI, " आप इस योजना के लिए पात्र हैं।", "eligibility statement"),
    ("bn", BN, " আপনি এই প্রকল্পের যোগ্য।", "eligibility statement"),
    ("hi", HI, " आपका प्रस्ताव भेज दिया गया है।", "approved or sent"),
    ("bn", BN, " আপনার প্রস্তাব অনুমোদিত হয়েছে।", "approved or sent"),
    ("hi", HI, " हमारे पैनल डीसीआर हैं।", "dcr statement"),
    ("bn", BN, " আমাদের প্যানেল DCR।", "dcr statement"),
    ("hi", HI, " बुधवार भी चलेगा।", "times differ"),
    ("hi", HI, " या anil@example.com पर लिखें।", "emails differ"),
    ("bn", BN, " অথবা +91 91234 56789 নম্বরে ফোন করুন।", "phones differ"),
    ("hi", HI, " हम Adani पैनल लगाते हैं।", "equipment make"),
    ("hi", HI, " कई लाख लगेंगे।", "written in words"),
])
def test_a_translation_that_adds_anything_is_rejected(locale, base, addition, expect):
    found = reasons(EN, base + addition, locale)
    assert any(expect in r for r in found), found


def test_a_dropped_figure_or_time_is_rejected():
    assert any("numbers differ" in r for r in reasons(EN, HI.replace("5 kW ", ""), "hi"))
    assert any("times differ" in r for r in reasons(EN, HI.replace("11:00 ", ""), "hi"))
    assert any("not kept exactly" in r for r in reasons("Would 11:00 AM work?", "क्या सुबह 11:00 बजे ठीक है?", "hi"))


def test_a_translation_left_in_english_or_wildly_resized_is_rejected():
    assert any("target script" in r for r in reasons(EN, EN, "bn"))
    assert any("length ratio" in r for r in reasons(EN, HI + " " + HI + " " + HI, "hi"))
    assert any("length ratio" in r for r in reasons(EN, "धन्यवाद 5 kW मंगलवार 20 अक्टूबर 11:00 info@gridnova.in", "hi"))
    assert reasons(EN, "  ", "hi") == ["translation was empty"]


# ── End to end through the Sales Agent ─────────────────────────────────────────

@pytest.fixture
def lead(memory, db_session):
    customer = memory.get_or_create_customer(email="anil@example.com", name="Anil", performed_by="test")
    lead = memory.create_lead(customer_id=customer.id, performed_by="test")
    db_session.commit()
    return customer, lead


def _run(memory, db_session, lead, locale, message="namaste, solar ke baare mein soch raha hoon"):
    from app.agents.sales_agent import sales_agent
    customer, row = lead
    result = sales_agent.handle_message(memory=memory, customer_id=customer.id, lead_id=row.id,
                                        message=message, locale=locale)
    db_session.commit()
    audit = [a for a in memory.list_audit_logs(entity_type="lead", entity_id=str(row.id)) if "guardrail" in a.action]
    return result, audit


def test_hindi_reply_is_generated_in_english_then_translated(memory, db_session, lead, monkeypatch):
    seen = {}

    def generate(**kw):
        seen["locale"] = kw.get("locale")
        return EN

    def translate(text, locale, lead_id=None):
        seen["translated"] = (text, locale)
        return HI

    monkeypatch.setattr(llm_module.llm_client, "generate_reply", generate)
    monkeypatch.setattr(llm_module.llm_client, "translate_reply", translate)
    monkeypatch.setattr(type(llm_module.llm_client), "mock_mode", property(lambda self: False))
    result, audit = _run(memory, db_session, lead, "hi", message="info@gridnova.in pe likh sakta hoon? Tue 20 Oct, 11:00 IST?")
    assert seen["translated"] == (EN, "hi")
    assert result["reply_text"] == HI and audit == []


def test_a_rejected_translation_sends_the_guarded_english_and_is_audited(memory, db_session, lead, monkeypatch):
    english = "Happy to help — what are you picturing for the roof?"
    monkeypatch.setattr(llm_module.llm_client, "generate_reply", lambda **kw: english)
    monkeypatch.setattr(llm_module.llm_client, "translate_reply",
                        lambda text, locale, lead_id=None: "मदद करके खुशी होगी — आपको सब्सिडी ज़रूर मिलेगी, यह आठ लाख में होगा।")
    monkeypatch.setattr(type(llm_module.llm_client), "mock_mode", property(lambda self: False))
    result, audit = _run(memory, db_session, lead, "hi")
    assert result["reply_text"] == english
    logged = [m.content for m in memory.get_lead_context(lead[1].id)["messages"] if m.role == MessageRole.agent]
    assert logged[-1] == english
    assert len(audit) == 1 and audit[0].action == "reply_flagged_by_guardrail"
    codes = [v["code"] for v in audit[0].after_value["violations"]]
    assert codes == [tc.TRANSLATION_REJECTED]
    assert "आठ लाख" in audit[0].after_value["violations"][0]["rejected_translation"]


def test_a_failed_translation_call_sends_english(memory, db_session, lead, monkeypatch):
    english = "Happy to help — what are you picturing for the roof?"
    monkeypatch.setattr(llm_module.llm_client, "generate_reply", lambda **kw: english)
    monkeypatch.setattr(llm_module.llm_client, "translate_reply", lambda text, locale, lead_id=None: None)
    result, audit = _run(memory, db_session, lead, "bn")
    assert result["reply_text"] == english
    assert [v["code"] for v in audit[0].after_value["violations"]] == [tc.TRANSLATION_REJECTED]


def test_a_blocked_english_reply_is_never_translated_from_the_original(memory, db_session, lead, monkeypatch):
    """The translation input is the GUARDED reply: a rogue price is gone before translation."""
    seen = []
    monkeypatch.setattr(llm_module.llm_client, "generate_reply", lambda **kw: "We can do this for 7,50,000 all in!")
    monkeypatch.setattr(llm_module.llm_client, "translate_reply",
                        lambda text, locale, lead_id=None: seen.append(text) or None)
    result, audit = _run(memory, db_session, lead, "hi")
    assert seen and "7,50,000" not in seen[0] and "7,50,000" not in result["reply_text"]
    codes = [v["code"] for v in audit[0].after_value["violations"]]
    assert guardrails.NEW_MONEY in codes and tc.TRANSLATION_REJECTED in codes


def test_english_conversations_never_call_translation(memory, db_session, lead, monkeypatch):
    monkeypatch.setattr(llm_module.llm_client, "translate_reply",
                        lambda *a, **kw: pytest.fail("English must not be translated"))
    for locale in ("en", "xx", None):
        _run(memory, db_session, lead, locale or "en", message="hi, thinking about solar")


def test_mock_mode_translation_is_deterministic_and_marked(memory, db_session, lead):
    result, audit = _run(memory, db_session, lead, "hi", message="hi, thinking about solar")
    assert result["reply_text"].startswith("[MOCK] ") and audit == []
    assert llm_module.llm_client.translate_reply("Hello there", "bn") == "[MOCK] Hello there"
    assert llm_module.llm_client.translate_reply("Hello there", "en") is None
