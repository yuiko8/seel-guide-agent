"""PII masking applied before anything is written to traces/logs."""
from __future__ import annotations

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def mask_pii(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _CARD.sub("[card]", text)

    def _phone(m: re.Match) -> str:
        s = m.group(0)
        digits = re.sub(r"\D", "", s)
        # Don't mask prices/years/timestamps: require 9+ digits for a phone number.
        return "[phone]" if len(digits) >= 9 else s

    return _PHONE.sub(_phone, text)
