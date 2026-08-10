from __future__ import annotations

import re
from typing import Optional

_LABEL_PREFIX = re.compile(r"^(?:account|acct)?\s*(?:number|no\.?|#)?\s*[:\-]?\s*", re.I)


def clean_account_raw(raw: str) -> str:
    value = (raw or "").strip().rstrip(" .,:;")
    value = _LABEL_PREFIX.sub("", value)
    return re.sub(r"[\t\r\n]+", " ", value).strip()


def canonical_account(vendor: str, raw: str) -> Optional[str]:
    """Return a stable vendor-aware account identity without converting to int.

    BC Hydro historically printed spaces inside numeric account numbers (e.g.
    ``902 741``) while newer bills may print ``902741``. Those forms therefore
    map to the same canonical string. Leading zeroes are preserved.
    """
    value = clean_account_raw(raw)
    if not value:
        return None

    vendor_key = (vendor or "UNKNOWN").strip().upper()

    if vendor_key == "BC HYDRO":
        value = re.sub(r"\s+", "", value)
        if not value.isdigit() or not 5 <= len(value) <= 24:
            return None
        return value

    # For telecom/gas/generic identities, remove only whitespace. Hyphens are
    # intentionally preserved because they may be semantically meaningful.
    value = re.sub(r"\s+", "", value)
    if not (4 <= len(value) <= 32):
        return None
    if sum(ch.isdigit() for ch in value) < 3:
        return None
    return value


def normalize_meter(raw: str) -> Optional[str]:
    value = (raw or "").strip().rstrip(" .,:;")
    value = re.sub(r"\s+", "", value)
    if not 3 <= len(value) <= 32:
        return None
    if sum(ch.isdigit() for ch in value) < 2:
        return None
    return value
