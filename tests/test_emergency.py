import pytest

from agent_service.emergency import check_emergency, is_emergency, reply_language

EMERGENCIES = [
    # English
    "I have chest pain and I can't breathe",
    "my father is unconscious, please help",
    "she fainted and won't wake up",
    "he is having a seizure",
    "heavy bleeding that won't stop",
    "I think I'm having a heart attack",
    "I want to kill myself",
    "my son swallowed poison",
    "shortness of breath since morning",
    "his face is drooping and his speech is slurred",
    "there was a road accident, serious injury",
    "CHEST PAIN!!!",
    "pain in my chest, very tight",
    # Hinglish
    "seene mein bahut dard ho raha hai",
    "saans nahi aa rahi",
    "papa behosh ho gaye hain",
    "bahut khoon beh raha hai",
    "mujhe lag raha hai main mar jaunga",
    "usne zeher kha liya",
    "mummy ko lakwa maar gaya hai",
    "chhati mein dard hai aur dam ghut raha hai",
    "behoooosh ho gayi",  # doubled letters
    "dil mein tez dard",
    # Hindi, Devanagari
    "मेरे सीने में बहुत दर्द है",
    "साँस लेने में तकलीफ़ हो रही है",
    "वो बेहोश हो गए हैं",
    "बहुत खून बह रहा है",
    "मैं आत्महत्या करना चाहता हूँ",
    "मरीज को मिर्गी का दौरा पड़ा है",
    "हार्ट अटैक आया है",
    # emergency inside an ordinary request
    "Mujhe seene mein dard hai. Kal ka appointment chahiye.",
    "please book dr rao for friday, also I can't breathe properly",
]

NOT_EMERGENCIES = [
    "I want to book an appointment with Dr Rao on Friday",
    "What are your visiting hours?",
    "Where is the emergency department?",  # an info question: no symptom
    "emergency department ka address kya hai",
    "Can I get my chest x-ray report?",
    "I need my blood test report",
    "mera bill kitna hai",
    "kal ka appointment cancel kar do",
    "stomach pain since yesterday, which doctor should I see",
    "मुझे अपॉइंटमेंट चाहिए",
    "Do you accept Star Health insurance?",
    "yes please book it",
    "my blood pressure appointment is tomorrow",
    "dard ki dawai kaunsi lun",  # pain, no emergency word (asking for medicine is the agent's no-advice rule)
    "",
]


@pytest.mark.parametrize("message", EMERGENCIES)
def test_emergencies_are_caught(message):
    assert is_emergency(message), message
    assert "108" in check_emergency(message)


@pytest.mark.parametrize("message", NOT_EMERGENCIES)
def test_ordinary_messages_are_not(message):
    assert check_emergency(message) is None, message


def test_reply_matches_the_patients_language():
    assert "emergency" in check_emergency("I have chest pain").lower() and "24 hours" in check_emergency("I have chest pain")
    assert "Yeh emergency" in check_emergency("mujhe seene mein dard hai")
    assert "आपातकाल" in check_emergency("मेरे सीने में दर्द है")
    assert reply_language("hello") == "en"


def test_every_reply_says_108_and_gives_no_advice():
    for lang in ("en", "hinglish", "hi"):
        from agent_service.emergency import _REPLIES

        assert "108" in _REPLIES[lang]
