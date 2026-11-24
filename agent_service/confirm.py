"""What the patient reads before and after a write, and what counts as "yes". All in code, no model.

The read-back is built from the stored pending action, so the patient confirms exactly what will run.
The reply after a write is built from the real result: "booked" only on Done (design-log 3.2).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from agent_service.emergency import _norm, _words

# ---------------------------------------------------------------- "yes"

# A message is a yes when EVERY word is in a small agreeing vocabulary and at least one is a yes word.
# Any other word, any number and any "?" means it is not a yes. Words go through the same normaliser
# as the message (doubled letters collapse), so "yess" and "haan" match.
_YES = _words(
    "yes yeah yep yup y ok okay sure confirm confirmed correct proceed agreed "
    "haan han ha haa hanji theek thik sahi kardo bilkul zaroor हां हाँ ठीक सही बिल्कुल जरूर ज़रूर"
)
_AGREE = _words(
    "please pls plz go ahead do it this that the thanks thank you ji hai hain kar karo de dena den dein dijiye kijiye "
    "bhej bhejo now fine good of course absolutely definitely and "
    "जी है हैं कर करो दो दीजिए कीजिए दें प्लीज धन्यवाद शुक्रिया"
)
# Words that name the action count only for THE pending action, and then they are yes words themselves:
# "book it" confirms a booking and "cancel it" confirms a cancel, never the other way round.
_ACTION = {
    "book_appointment": _words("book booking booked बुक"),
    "reschedule_appointment": _words("reschedule move change shift रीशेड्यूल"),
    "cancel_appointment": _words("cancel cancelled कैंसल रद्द"),
    "create_payment_link": _words("send link payment pay लिंक भेज"),
}


def is_yes(message: str, tool: str | None = None) -> bool:
    if "?" in message or "？" in message or "؟" in message:
        return False
    words = _norm(message).split()
    own = _ACTION.get(tool or "", frozenset())
    return bool(words) and all(w in _YES | _AGREE | own for w in words) and any(w in _YES | own for w in words)


# ---------------------------------------------------------------- dates

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def when(iso: Any) -> str:
    """"Fri 4 Dec 2026, 5:00pm": weekday, date and time, so nothing is left to guess."""
    try:
        d = dt.datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    h = d.hour % 12 or 12
    return f"{_DAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]} {d.year}, {h}:{d.minute:02d}{'am' if d.hour < 12 else 'pm'}"


# ---------------------------------------------------------------- the texts

_ASK = {"en": "Reply yes to confirm.", "hinglish": "Confirm karne ke liye 'haan' likhein.", "hi": "पुष्टि के लिए 'हाँ' लिखें।"}

_READBACK = {
    "en": {
        "book_appointment": "Please confirm: book {patient} with {doctor} on {when}?",
        "reschedule_appointment": "Please confirm: move your appointment with {doctor} from {old} to {new}?",
        "cancel_appointment": "Please confirm: cancel your appointment with {doctor} on {when}?",
        "create_payment_link": "Please confirm: send a payment link for {what}, Rs {amount}?",
    },
    "hinglish": {
        "book_appointment": "Confirm karein: {patient} ka appointment {doctor} ke saath {when} par book karun?",
        "reschedule_appointment": "Confirm karein: {doctor} ke saath aapka appointment {old} se {new} par shift karun?",
        "cancel_appointment": "Confirm karein: {doctor} ke saath {when} wala appointment cancel karun?",
        "create_payment_link": "Confirm karein: {what}, Rs {amount} ka payment link bhejun?",
    },
    "hi": {
        "book_appointment": "कृपया पुष्टि करें: {doctor} के साथ {when} को {patient} का अपॉइंटमेंट बुक करूँ?",
        "reschedule_appointment": "कृपया पुष्टि करें: {doctor} के साथ आपका अपॉइंटमेंट {old} से बदलकर {new} कर दूँ?",
        "cancel_appointment": "कृपया पुष्टि करें: {doctor} के साथ {when} का अपॉइंटमेंट रद्द करूँ?",
        "create_payment_link": "कृपया पुष्टि करें: {what}, Rs {amount} का पेमेंट लिंक भेजूँ?",
    },
}

_DONE = {
    "en": {
        "book_appointment": "Booked: {doctor}, {when}, for {patient}.",
        "reschedule_appointment": "Done: your appointment with {doctor} is now {new}.",
        "cancel_appointment": "Done: your appointment with {doctor} on {when} is cancelled.",
        "create_payment_link": "Here is your payment link for {what}, Rs {amount}: {url}",
    },
    "hinglish": {
        "book_appointment": "Book ho gaya: {doctor}, {when}, {patient} ke liye.",
        "reschedule_appointment": "Ho gaya: {doctor} ke saath aapka appointment ab {new} par hai.",
        "cancel_appointment": "Ho gaya: {doctor} ke saath {when} ka appointment cancel ho gaya.",
        "create_payment_link": "Yeh raha {what}, Rs {amount} ka payment link: {url}",
    },
    "hi": {
        "book_appointment": "बुक हो गया: {doctor}, {when}, {patient} के लिए।",
        "reschedule_appointment": "हो गया: {doctor} के साथ आपका अपॉइंटमेंट अब {new} पर है।",
        "cancel_appointment": "हो गया: {doctor} के साथ {when} का अपॉइंटमेंट रद्द हो गया।",
        "create_payment_link": "यह रहा {what}, Rs {amount} का पेमेंट लिंक: {url}",
    },
}

# The hospital said the slot is gone (409). Says plainly what did not happen.
_TAKEN = {
    "en": {"book_appointment": "That time was just taken by someone else, so it was not booked. Tell me another time and I'll check what's free.",
           "reschedule_appointment": "That time was just taken by someone else, so your appointment was not moved. Tell me another time and I'll check what's free."},
    "hinglish": {"book_appointment": "Woh time abhi kisi aur ne le liya, isliye book nahi hua. Koi aur time bataiye, main check karta hoon.",
                 "reschedule_appointment": "Woh time abhi kisi aur ne le liya, isliye appointment shift nahi hua. Koi aur time bataiye, main check karta hoon."},
    "hi": {"book_appointment": "वह समय अभी किसी और ने ले लिया, इसलिए बुक नहीं हुआ। कोई और समय बताइए, मैं देखता हूँ।",
           "reschedule_appointment": "वह समय अभी किसी और ने ले लिया, इसलिए अपॉइंटमेंट नहीं बदला। कोई और समय बताइए, मैं देखता हूँ।"},
}

# After a 409 on a booking: what did not happen, then the next free slot as a full read-back (confirm.readback).
_TAKEN_OFFER = {
    "en": "That time was just taken by someone else, so it was not booked. Next free time with this doctor:",
    "hinglish": "Woh time abhi kisi aur ne le liya, isliye book nahi hua. Is doctor ke saath agla free time:",
    "hi": "वह समय अभी किसी और ने ले लिया, इसलिए बुक नहीं हुआ। इस डॉक्टर के साथ अगला खाली समय:",
}

_NOT_DONE = {
    "en": "Sorry, that was not done and nothing was changed. You can ask me to try again, or ask for our staff.",
    "hinglish": "Sorry, woh nahi ho paaya aur kuch bhi change nahi hua. Dobara try karne bolein, ya staff bol dein.",
    "hi": "माफ़ कीजिए, वह नहीं हो पाया और कुछ भी नहीं बदला। फिर से कोशिश करने को कहें, या स्टाफ़ कहें।",
}

# We could not find out what happened. Never says it was done, and tells the patient not to repeat it.
_UNKNOWN = {
    "en": "I couldn't confirm whether that went through, so please don't try it again. I'm passing this to our staff. They will check and reply here. Front desk: 020-4000-1234.",
    "hinglish": "Main confirm nahi kar paaya ki woh hua ya nahi, isliye dobara try mat karein. Main yeh staff ko de raha hoon, woh check karke yahin reply karenge. Front desk: 020-4000-1234.",
    "hi": "मैं पुष्टि नहीं कर पाया कि वह हुआ या नहीं, इसलिए दोबारा कोशिश न करें। मैं यह स्टाफ़ को दे रहा हूँ, वे जाँचकर यहीं जवाब देंगे। फ्रंट डेस्क: 020-4000-1234।",
}

_CHANGED = {
    "en": "Something changed since I asked, so I haven't done it. Nothing was changed. Please ask me again.",
    "hinglish": "Poochne ke baad kuch badal gaya, isliye maine yeh nahi kiya. Kuch bhi change nahi hua. Please dobara bolein.",
    "hi": "पूछने के बाद कुछ बदल गया, इसलिए मैंने यह नहीं किया। कुछ भी नहीं बदला। कृपया फिर से कहें।",
}


def _lang(language: str | None) -> str:
    return language if language in _ASK else "en"


def _fill(template: str, display: dict[str, Any]) -> str:
    shown = {k: (when(v) if k in ("when", "old", "new") else v) for k, v in display.items()}
    return template.format_map({"patient": "", "doctor": "", "when": "", "old": "", "new": "", "what": "", "amount": "", "url": "", **shown})


def readback(tool: str, display: dict[str, Any], language: str | None) -> str:
    lang = _lang(language)
    return f"{_fill(_READBACK[lang][tool], display)} {_ASK[lang]}"


def done_reply(tool: str, display: dict[str, Any], value: dict[str, Any], language: str | None) -> str:
    """Built from the hospital's own answer where it has one (the time that was really booked, the link)."""
    shown = dict(display)
    if tool == "book_appointment":
        shown["when"] = value.get("start", shown.get("when"))
    elif tool == "reschedule_appointment":
        shown["new"] = value.get("start", shown.get("new"))
    elif tool == "create_payment_link":
        shown["url"] = value.get("url", "")
    return _fill(_DONE[_lang(language)][tool], shown)


def failed_reply(tool: str, reason: str, language: str | None) -> str:
    lang = _lang(language)
    if "slot_taken" in reason and tool in _TAKEN[lang]:
        return _TAKEN[lang][tool]
    return _NOT_DONE[lang]


def taken_offer_prefix(language: str | None) -> str:
    return _TAKEN_OFFER[_lang(language)]


def taken_offer_reply(readback_text: str, language: str | None) -> str:
    return f"{taken_offer_prefix(language)} {readback_text}"


def unknown_reply(language: str | None) -> str:
    return _UNKNOWN[_lang(language)]


def changed_reply(language: str | None) -> str:
    return _CHANGED[_lang(language)]
