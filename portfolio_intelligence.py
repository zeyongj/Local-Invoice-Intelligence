from __future__ import annotations

import csv
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

from invoice_normalization import canonical_account, normalize_meter

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

PROJECT_TOKEN_RE = re.compile(r"^\s*([0-9]{4}(?:-[0-9A-Z]+|[A-Z])?)\b", re.I)
POSTAL_RE = re.compile(r"\b([A-Z]\d[A-Z]\s?\d[A-Z]\d)\b", re.I)
DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
DATE_MONTH_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(\d{4})\b",
    re.I,
)
DATE_SLASH_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
PERIOD_SPLIT_RE = re.compile(r"\s+(?:to|through|thru)\s+|\s+[–—-]\s+", re.I)


def _clean(value: str) -> str:
    return " ".join((value or "").replace("\r", "\n").split())


def extract_project_codes(raw_project: str) -> list[str]:
    """Return project codes in source order without inventing a base project.

    Important v4 rule: when the source cell explicitly lists a parent code first,
    aliases such as 5093-1/5093-2/5093-3 belong to canonical project 5093. We do
    *not* blindly strip suffixes from rows such as 5164-10 where no 5164 parent is
    present in the source cell.
    """
    out: list[str] = []
    for line in (raw_project or "").replace("\r", "\n").split("\n"):
        match = PROJECT_TOKEN_RE.match(line)
        if not match:
            continue
        code = match.group(1).upper()
        if code not in out:
            out.append(code)
    return out


def canonical_project_code(raw_project: str) -> Optional[str]:
    codes = extract_project_codes(raw_project)
    return codes[0] if codes else None


@dataclass(slots=True)
class PropertySeed:
    project_id: str
    project_name: str
    strata_plan: str = ""
    pm: str = ""
    raw_project: str = ""
    raw_project_name: str = ""
    aliases: list[str] = field(default_factory=list)
    postal_codes: list[str] = field(default_factory=list)


def load_property_csv(path: str | Path) -> list[PropertySeed]:
    """Load the supplied property-management CSV as an offline portfolio seed.

    The importer keeps the original source strings for auditability while using
    the first explicit project code as the canonical internal project id.
    """
    source = Path(path)
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        field_map = {re.sub(r"\s+", " ", (name or "").strip()).upper(): name for name in (reader.fieldnames or [])}
        proj_key = field_map.get("PROJ #")
        name_key = field_map.get("PROJECT NAME")
        pm_key = field_map.get("PM")
        strata_key = next((orig for norm, orig in field_map.items() if norm.startswith("STRATA") and "PLAN" in norm), None)
        if not proj_key or not name_key:
            raise ValueError("Portfolio CSV must contain PROJ # and PROJECT NAME columns.")
        seeds: list[PropertySeed] = []
        for row in reader:
            raw_project = row.get(proj_key, "") or ""
            project_id = canonical_project_code(raw_project)
            if not project_id:
                continue
            raw_name = row.get(name_key, "") or ""
            name_lines = [ln.strip() for ln in raw_name.replace("\r", "\n").split("\n") if ln.strip()]
            project_name = name_lines[0] if name_lines else project_id
            aliases = extract_project_codes(raw_project)
            postals = []
            for match in POSTAL_RE.findall(raw_name):
                normalized = re.sub(r"\s+", "", match.upper())
                normalized = normalized[:3] + " " + normalized[3:]
                if normalized not in postals:
                    postals.append(normalized)
            seeds.append(PropertySeed(
                project_id=project_id,
                project_name=project_name,
                strata_plan=_clean(row.get(strata_key, "") or "") if strata_key else "",
                pm=_clean(row.get(pm_key, "") or "") if pm_key else "",
                raw_project=raw_project.strip(),
                raw_project_name=raw_name.strip(),
                aliases=aliases or [project_id],
                postal_codes=postals,
            ))
    return seeds


def _make_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date_token(text: str) -> Optional[date]:
    text = _clean(text)
    iso = DATE_ISO_RE.search(text)
    if iso:
        return _make_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    mon = DATE_MONTH_RE.search(text)
    if mon:
        month = MONTHS.get(mon.group(1).lower().rstrip("."))
        return _make_date(int(mon.group(3)), int(month or 0), int(mon.group(2))) if month else None
    slash = DATE_SLASH_RE.search(text)
    if slash:
        # Utility invoices in this deployment are Canadian. Ambiguous numeric
        # dates are interpreted month/day only when the first component > 12 is
        # impossible; otherwise we leave them unresolved rather than guessing.
        a, b, y = map(int, slash.groups())
        if a > 12 and b <= 12:
            return _make_date(y, b, a)
        if b > 12 and a <= 12:
            return _make_date(y, a, b)
        return None
    return None


def parse_billing_period(raw: str | None) -> tuple[Optional[str], Optional[str]]:
    if not raw:
        return None, None
    text = _clean(raw)
    parts = PERIOD_SPLIT_RE.split(text, maxsplit=1)
    if len(parts) == 2:
        first = parse_date_token(parts[0])
        second = parse_date_token(parts[1])
        if first and second:
            if second < first:
                return None, None
            return first.isoformat(), second.isoformat()

    # Common compact form: "Jul 1 - Jul 31, 2026" where the first date omits year.
    compact = re.search(
        r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
        r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})\s*[–—-]\s*"
        r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
        r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})(?:,)?\s+(\d{4})\b",
        text, re.I,
    )
    if compact:
        m1 = MONTHS[compact.group(1).lower().rstrip(".")]
        d1 = int(compact.group(2)); m2 = MONTHS[compact.group(3).lower().rstrip(".")]
        d2 = int(compact.group(4)); y2 = int(compact.group(5))
        y1 = y2 if m1 <= m2 else y2 - 1
        first, second = _make_date(y1, m1, d1), _make_date(y2, m2, d2)
        if first and second and second >= first:
            return first.isoformat(), second.isoformat()
    return None, None


def period_length_days(start: str | None, end: str | None) -> Optional[int]:
    if not start or not end:
        return None
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    except ValueError:
        return None


def robust_z_score(value: float, samples: Iterable[float]) -> Optional[float]:
    vals = [float(v) for v in samples]
    if len(vals) < 5:
        return None
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals)
    if mad <= 1e-12:
        if abs(value - med) <= 1e-12:
            return 0.0
        scale = max(abs(med) * 0.05, 1.0)
        return (value - med) / scale
    return 0.6745 * (value - med) / mad


def effective_unit_cost(current_charges: Optional[float], total_due: Optional[float], consumption: Optional[float]) -> Optional[float]:
    if consumption is None or float(consumption) <= 0:
        return None
    amount = current_charges if current_charges is not None else total_due
    if amount is None:
        return None
    return float(amount) / float(consumption)


def normalize_utility_identity(vendor: str, account_raw: str, meter_raw: str | None = None) -> tuple[Optional[str], Optional[str]]:
    return canonical_account(vendor, account_raw), normalize_meter(meter_raw or "") if meter_raw else None
