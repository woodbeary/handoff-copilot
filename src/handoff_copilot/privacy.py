"""PII handling: mask before any prompt, read contacts locally, ask for named fields only.

Two jobs that look alike and must stay apart:

* `redact` runs on every line before a model sees it. It masks emails (written or
  spoken), phone numbers and the names the ATS already knows, and it errs toward
  masking too much.
* `find_contacts` runs locally, never feeds a model, and only reads contacts the
  candidate states. It errs toward missing a spoken address rather than inventing one,
  because what it finds is shown to a person as "confirm this address".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

_LOCAL = r"[a-z0-9_%+-]+(?:\.[a-z0-9_%+-]+)*"
_DOMAIN = r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}"
WRITTEN_EMAIL = re.compile(rf"\b{_LOCAL}@{_DOMAIN}\b", re.IGNORECASE)

_WORD = r"[a-z0-9_%+-]+"
SPOKEN_EMAIL = re.compile(
    rf"\b({_WORD}(?:(?:\s+dot\s+|\.){_WORD})*)\s+at\s+({_WORD}(?:(?:\s+dot\s+|\.){_WORD})+)\b",
    re.IGNORECASE,
)
PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
EMAIL_CUE = re.compile(r"\be-?mail\b", re.IGNORECASE)


def _spoken_to_written(local: str, domain: str) -> str:
    def join(part: str) -> str:
        return re.sub(r"\s+dot\s+", ".", part.strip(), flags=re.IGNORECASE).lower()

    return f"{join(local)}@{join(domain)}"


def mask_email(address: str) -> str:
    local, _, domain = address.partition("@")
    host, _, tld = domain.rpartition(".")
    return f"{local[:1]}***@{host[:1]}***.{tld}"


def mask_phone(number: str) -> str:
    digits = re.sub(r"\D", "", number)
    return f"…{digits[-4:]}"


def redact(text: str, names: Mapping[str, str] | None = None) -> str:
    """Mask emails, phone numbers and known names. Safe to run twice."""
    text = WRITTEN_EMAIL.sub(lambda m: mask_email(m.group(0)), text)
    text = SPOKEN_EMAIL.sub(lambda m: mask_email(_spoken_to_written(m[1], m[2])), text)
    text = PHONE.sub(lambda m: mask_phone(m.group(0)), text)
    for name, tag in sorted((names or {}).items(), key=lambda kv: -len(kv[0])):
        if name.strip():
            text = re.sub(rf"\b{re.escape(name)}\b", tag, text, flags=re.IGNORECASE)
    return text


FORMATTED_PHONE = re.compile(r"(?<!\d)\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")


def contains_contact(text: str) -> bool:
    """An email or a phone number written with separators.

    Bare ten-digit runs don't count here: Greenhouse ids are often ten digits long.
    """
    return bool(WRITTEN_EMAIL.search(text) or FORMATTED_PHONE.search(text))


def find_contacts(text: str) -> list[tuple[str, str]]:
    """Contacts stated in one line, as ("email" | "phone", normalized value).

    Written addresses and phone numbers count anywhere. A spoken address ("taylor dot b
    at example dot net") counts only when the same line says "email", so "I worked at
    example dot com" is never read as an address.
    """
    found: list[tuple[str, str]] = []
    found += [("email", m.group(0).lower()) for m in WRITTEN_EMAIL.finditer(text)]
    if EMAIL_CUE.search(text):
        for m in SPOKEN_EMAIL.finditer(text):
            if "@" not in m.group(0):
                found.append(("email", _spoken_to_written(m[1], m[2])))
    found += [("phone", re.sub(r"\D", "", m.group(0))[-10:]) for m in PHONE.finditer(text)]
    return list(dict.fromkeys(found))


def keep_fields(record: Mapping, allowed: Iterable[str]) -> dict:
    """Drop every key the allowlist doesn't name, even if the server sent it."""
    allowed = set(allowed)
    return {k: v for k, v in record.items() if k in allowed}
