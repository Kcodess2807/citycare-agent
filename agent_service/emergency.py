"""The emergency check. Plain code, no model: it runs in `ChatService.handle` before the router and
before anything else, so it cannot fail because an API call failed (design-log 3.1).

Matching works on normalised words (lowercase, no punctuation, repeated letters collapsed, Devanagari
spelling variants merged), in three ways: a phrase, a single strong word, or two word groups that
both appear anywhere in the message ("seene" + "dard"). It leans towards false alarms on purpose:
a false alarm costs one reply with 108 in it, a miss costs a patient. There is no negation handling.

Known limit: spelling variants that are not listed (and typos beyond doubled letters) are missed.
"""

from __future__ import annotations

import re
import unicodedata

_DROP = dict.fromkeys(map(ord, "'’`़​‌‍"))  # apostrophes, nukta, zero-width joiners
_DROP[0x0901] = 0x0902  # chandrabindu -> anusvara, so साँस and सांस match


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().translate(_DROP)
    # Anything that is not a letter, mark or digit is a separator. Marks stay: Devanagari vowel signs are marks.
    text = "".join(c if unicodedata.category(c)[0] in "LMN" else " " for c in text)
    return re.sub(r"(.)\1+", r"\1", text)  # "behoosh" -> "behosh". Vocabulary goes through the same function.


def _words(s: str) -> frozenset[str]:
    return frozenset(_norm(s).split())


def _phrases(s: str) -> tuple[str, ...]:
    return tuple(f" {_norm(p).strip()} " for p in s.split(","))


# One strong word is enough.
_WORDS = _words(
    "behosh unconscious unconcious fainted fainting faint collapsed seizure seizures convulsion convulsions "
    "stroke poison poisoned poisoning overdose suicide suicidal breathless breathlessness choking gasping "
    "slurred slurring zeher jehar khudkushi aatmahatya lakwa paralysis paralyzed paralysed mirgi "
    "बेहोश जहर आत्महत्या लकवा मिर्गी खुदकुशी"
)

_PHRASES = _phrases(
    "heart attack,dil ka daura,दिल का दौरा,हार्ट अटैक,medical emergency,this is an emergency,its an emergency,"
    "emergency hai,emergency hua,emergency ho,इमरजेंसी है,need an ambulance,need ambulance,call an ambulance,"
    "call ambulance,send an ambulance,send ambulance,ambulance bulao,ambulance chahiye,एम्बुलेंस बुलाओ,"
    "cant breathe,cannot breathe,unable to breathe,short of breath,shortness of breath,difficulty breathing,"
    "trouble breathing,dam ghut,दम घुट,"
    "bahut khoon,bohot khoon,zyada khoon,jyada khoon,khoon bahut,बहुत खून,खून बहुत,"
    "kill myself,end my life,end it all,want to die,wanna die,dont want to live,no reason to live,hurt myself,"
    "harm myself,self harm,mar jaunga,mar jaungi,marna chahta,marna chahti,jeena nahi chahta,jeena nahi chahti,"
    "jaan de dunga,jaan de dungi,मर जाऊंगा,मर जाऊंगी,मरना चाहता,मरना चाहती,जीना नहीं चाहता,जीना नहीं चाहती,जान दे दूंगा,"
    "face drooping,face droop,slurred speech,speech slurred,cant speak,muh tedha,zubaan ladkhada,"
    "daura pad,daure pad,fits aa,"
    "serious injury,severe injury,had an accident,in an accident,road accident,car accident,bike accident,"
    "accident hua,accident ho gaya,accident ho gayi,badi chot,gehri chot,gambhir chot,गंभीर चोट"
)

# Two groups: the message needs a word from each.
_PAIRS = [
    (
        _words("chest seena seene sina sine chhati chati dil heart सीने सीना छाती दिल"),
        _words("pain painful dard hurts hurting tightness tight pressure jakdan दर्द जकड़न"),
    ),
    (
        _words("saans sans breath breathe breathing सांस साँस"),
        _words(
            "nahi nahin not cant cannot unable taklif takleef tkleef problem trouble difficulty difficult hard "
            "mushkil dikkat ruk rukk band short shortness phool phul नहीं तकलीफ दिक्कत मुश्किल रुक बंद फूल"
        ),
    ),
    (_words("khoon khun blood bleeding rakt खून रक्त"), _words("bah beh behta behne nikal heavy severe nonstop profuse wont rukk ruk बह बहने निकल रुक")),
    (_words("face muh chehra चेहरा"), _words("droop drooping drooped tedha tirchha टेढ़ा टेढा")),
    (_words("hosh होश"), _words("nahi nahin gaya khoya नहीं खो")),
    (_words("chot injury injured fracture fractured चोट"), _words("gambhir serious severe badi gehri critical गंभीर")),
]

_REPLIES = {
    "en": (
        "This sounds like an emergency. Please call 108 now, or go to the nearest emergency room. "
        "Our Emergency department at the main entrance is open 24 hours. Our staff have been alerted."
    ),
    "hinglish": (
        "Yeh emergency lag rahi hai. Abhi 108 par call karein, ya sabse paas ke emergency room jaayein. "
        "Hamara Emergency department main entrance par 24 ghante khula hai. Hamare staff ko bata diya gaya hai."
    ),
    "hi": (
        "यह आपातकाल लग रहा है। अभी 108 पर कॉल करें, या नज़दीकी इमरजेंसी रूम जाएँ। "
        "हमारा इमरजेंसी विभाग मुख्य प्रवेश द्वार पर 24 घंटे खुला है। हमारे स्टाफ़ को सूचित कर दिया गया है।"
    ),
}

_HINGLISH = _words(
    "mujhe mere meri mera hai hain ho hua hui raha rahi rahe nahi nahin bahut bohot dard taklif mein ko ka ki ke "
    "kal aaj abhi chahiye kar karo bhi aur par se"
)


def reply_language(message: str) -> str:
    if any("ऀ" <= c <= "ॿ" for c in message):
        return "hi"
    return "hinglish" if len(_words(message) & _HINGLISH) >= 2 else "en"


def is_emergency(message: str) -> bool:
    text = _norm(message)
    words = set(text.split())
    padded = f" {' '.join(text.split())} "
    return bool(
        words & _WORDS
        or any(p in padded for p in _PHRASES)
        or any(words & a and words & b for a, b in _PAIRS)
    )


def reply_for(language: str | None) -> str:
    """The fixed emergency reply in a language (en, hi, hinglish). For the router's flag, which knows the language."""
    return _REPLIES.get(language or "", _REPLIES["en"])


def check_emergency(message: str) -> str | None:
    """The fixed reply to send if `message` describes an emergency, else None."""
    return _REPLIES[reply_language(message)] if is_emergency(message) else None
