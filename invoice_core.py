from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Callable, Iterable, Optional

import fitz

from invoice_models import DocumentView, FieldValue, LayoutProfile, TextLine

MIN_TEXT_CHARS = 40
WHITESPACE_RE = re.compile(r"[ \t]+")

MONEY_CAPTURE = r"(\(?-?\$?\s*\d[\d,]*(?:\.\d{2})\)?)"
ACCOUNT_CAPTURE = r"([A-Z0-9][A-Z0-9\- ]{4,25})"
METER_CAPTURE = r"([A-Z0-9][A-Z0-9\-]{3,24})"
CONSUMPTION_CAPTURE = r"([\d,]+(?:\.\d+)?)\s*(kWh|MWh|GJ|m3|m³|kW|GB)?"

MONEY_VALUE_RE = re.compile(MONEY_CAPTURE, re.I)
ACCOUNT_VALUE_RE = re.compile(ACCOUNT_CAPTURE, re.I)
METER_VALUE_RE = re.compile(METER_CAPTURE, re.I)
DATE_VALUE_RE = re.compile(
    r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})",
    re.I,
)

DATE_FORMATS = (
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%Y-%m-%d",
)


def rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I)


def normalize_space(value: str) -> str:
    return WHITESPACE_RE.sub(" ", value).strip()


def normalize_account(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^(?:number|no\.?|#)\s*[:\-]?\s*", "", value, flags=re.I)
    value = value.rstrip(" .,:;")
    return re.sub(r"\s+", "", value)


def parse_identifier(value: str) -> Optional[str]:
    clean = normalize_account(value)
    if len(clean) < 4 or len(clean) > 30:
        return None
    # Reject labels / prose that happen to fit the generic alphanumeric regex.
    if sum(ch.isdigit() for ch in clean) < 3:
        return None
    if clean.casefold() in {"accountnumber", "meternumber", "accountno", "meterno"}:
        return None
    return clean


def parse_money(raw: str) -> Optional[float]:
    if not raw:
        return None
    raw = raw.strip()
    negative_parentheses = raw.startswith("(") and raw.endswith(")")
    clean = raw.replace("$", "").replace(",", "").replace("(", "").replace(")", "").replace(" ", "")
    try:
        value = float(clean)
    except ValueError:
        return None
    if negative_parentheses:
        value = -abs(value)
    return round(value, 2)


def parse_number(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def parse_date(raw: str) -> Optional[str]:
    from datetime import datetime

    raw = normalize_space(raw)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def fv_from_line(value, raw: str, confidence: float, method: str, line: TextLine, evidence: str) -> FieldValue:
    return FieldValue(
        value=value,
        raw=raw,
        confidence=confidence,
        method=method,
        page=line.page + 1,
        evidence=evidence,
        x0=line.x0,
        y0=line.y0,
        x1=line.x1,
        y1=line.y1,
        page_width=line.page_width,
        page_height=line.page_height,
    )


def best_field(*candidates: FieldValue) -> FieldValue:
    found = [candidate for candidate in candidates if candidate and candidate.found]
    return max(found, key=lambda item: item.confidence) if found else FieldValue()


class SpatialIndex:
    """Near-linear page/Y-bucket spatial index for PDF text lines."""

    def __init__(self, lines: list[TextLine], bucket_px: float = 12.0):
        self.bucket_px = max(4.0, bucket_px)
        self.by_page: dict[int, list[TextLine]] = defaultdict(list)
        self.buckets: dict[tuple[int, int], list[TextLine]] = defaultdict(list)
        for line in lines:
            self.by_page[line.page].append(line)
            bucket = int(line.center_y // self.bucket_px)
            self.buckets[(line.page, bucket)].append(line)

    def query_band(self, page: int, y: float, radius_px: float = 18.0) -> list[TextLine]:
        center_bucket = int(y // self.bucket_px)
        span = int(math.ceil(radius_px / self.bucket_px))
        out: list[TextLine] = []
        for bucket in range(center_bucket - span, center_bucket + span + 1):
            for line in self.buckets.get((page, bucket), ()):
                if abs(line.center_y - y) <= radius_px:
                    out.append(line)
        return out

    def query_region(self, page: int, x_min: float, x_max: float, y_min: float, y_max: float) -> list[TextLine]:
        start_bucket = int(y_min // self.bucket_px)
        end_bucket = int(y_max // self.bucket_px)
        out: list[TextLine] = []
        seen: set[int] = set()
        for bucket in range(start_bucket, end_bucket + 1):
            for line in self.buckets.get((page, bucket), ()):
                ident = id(line)
                if ident in seen:
                    continue
                seen.add(ident)
                if line.x1 >= x_min and line.x0 <= x_max and line.y1 >= y_min and line.y0 <= y_max:
                    out.append(line)
        return out


def extract_lines_from_page(page: fitz.Page, page_index: int) -> list[TextLine]:
    words = page.get_text("words", sort=True)
    if not words:
        return []
    groups: dict[tuple[int, int], list[tuple]] = defaultdict(list)
    for word in words:
        groups[(int(word[5]), int(word[6]))].append(word)
    page_rect = page.rect
    out: list[TextLine] = []
    for group_words in groups.values():
        group_words.sort(key=lambda word: word[0])
        text = normalize_space(" ".join(str(word[4]) for word in group_words))
        if not text:
            continue
        out.append(
            TextLine(
                page=page_index,
                text=text,
                x0=min(word[0] for word in group_words),
                y0=min(word[1] for word in group_words),
                x1=max(word[2] for word in group_words),
                y1=max(word[3] for word in group_words),
                page_width=float(page_rect.width),
                page_height=float(page_rect.height),
            )
        )
    out.sort(key=lambda line: (line.page, line.y0, line.x0))
    return out


def build_document_view(doc: fitz.Document, page_indexes: Iterable[int]) -> DocumentView:
    lines: list[TextLine] = []
    processed = 0
    for page_index in page_indexes:
        lines.extend(extract_lines_from_page(doc.load_page(page_index), page_index))
        processed += 1
    text = "\n".join(line.text for line in lines)
    return DocumentView(
        lines=lines,
        text=text,
        page_count=doc.page_count,
        pages_processed=processed,
        has_text_layer=len(text.strip()) >= MIN_TEXT_CHARS,
    )


def merge_views(first: DocumentView, second: DocumentView) -> DocumentView:
    lines = first.lines + second.lines
    lines.sort(key=lambda item: (item.page, item.y0, item.x0))
    return DocumentView(
        lines=lines,
        text="\n".join(line.text for line in lines),
        page_count=max(first.page_count, second.page_count),
        pages_processed=first.pages_processed + second.pages_processed,
        has_text_layer=first.has_text_layer or second.has_text_layer,
    )


def line_pattern_extract(
    lines: Iterable[TextLine],
    patterns: list[re.Pattern[str]],
    converter: Callable[[str], object],
    confidence: float = 0.96,
) -> FieldValue:
    best = FieldValue()
    for line in lines:
        text = normalize_space(line.text)
        for pattern in patterns:
            match = pattern.search(text)
            if not match:
                continue
            raw = normalize_space(match.group(1))
            try:
                value = converter(raw)
            except Exception:
                value = None
            if value is None:
                continue
            candidate = fv_from_line(value, raw, confidence, "label_regex", line, text)
            if candidate.confidence > best.confidence:
                best = candidate
    return best


def spatial_extract(
    lines: list[TextLine],
    index: SpatialIndex,
    anchors: list[re.Pattern[str]],
    value_pattern: re.Pattern[str],
    converter: Callable[[str], object],
) -> FieldValue:
    """O(L + A*k) rather than O(L²): scan anchors once, then query nearby buckets."""
    best = FieldValue()
    for anchor_line in lines:
        anchor_text = normalize_space(anchor_line.text)
        if not any(pattern.search(anchor_text) for pattern in anchors):
            continue

        candidates = index.query_band(anchor_line.page, anchor_line.center_y, radius_px=20.0)
        below = index.query_region(
            anchor_line.page,
            max(0.0, anchor_line.x0 - 10),
            min(anchor_line.page_width, anchor_line.x1 + 420),
            anchor_line.y1 - 2,
            min(anchor_line.page_height, anchor_line.y1 + 72),
        )
        candidates.extend(below)

        seen: set[int] = set()
        for candidate_line in candidates:
            if candidate_line is anchor_line or id(candidate_line) in seen:
                continue
            seen.add(id(candidate_line))
            text = normalize_space(candidate_line.text)
            match = value_pattern.search(text)
            if not match:
                continue
            raw = normalize_space(match.group(1))
            try:
                value = converter(raw)
            except Exception:
                value = None
            if value is None:
                continue

            y_distance = abs(candidate_line.center_y - anchor_line.center_y)
            if y_distance <= 10 and candidate_line.x0 >= anchor_line.x1 - 8:
                gap = max(0.0, candidate_line.x0 - anchor_line.x1)
                confidence = 0.90 - min(gap / 1200.0, 0.12)
            elif candidate_line.y0 >= anchor_line.y1 - 2 and candidate_line.y0 - anchor_line.y1 <= 70:
                gap = max(0.0, candidate_line.y0 - anchor_line.y1)
                confidence = 0.80 - min(gap / 500.0, 0.12)
            else:
                continue

            if confidence > best.confidence:
                best = fv_from_line(
                    value,
                    raw,
                    confidence,
                    "spatial_index",
                    candidate_line,
                    f"anchor={anchor_text!r}; value={text!r}",
                )
    return best


def template_extract(
    index: SpatialIndex,
    profile: Optional[LayoutProfile],
    value_pattern: re.Pattern[str],
    converter: Callable[[str], object],
) -> FieldValue:
    """Use learned normalized page region to narrow candidate search without AI."""
    if not profile or profile.samples < 3:
        return FieldValue()
    page = profile.page_num - 1
    lines = index.by_page.get(page)
    if not lines:
        return FieldValue()
    page_width = lines[0].page_width
    page_height = lines[0].page_height
    expected_x = profile.mean_x * page_width
    expected_y = profile.mean_y * page_height
    radius_x = max(90.0, page_width * 0.22)
    radius_y = max(45.0, page_height * 0.10)
    candidates = index.query_region(
        page,
        max(0.0, expected_x - radius_x),
        min(page_width, expected_x + radius_x),
        max(0.0, expected_y - radius_y),
        min(page_height, expected_y + radius_y),
    )
    best = FieldValue()
    for line in candidates:
        match = value_pattern.search(normalize_space(line.text))
        if not match:
            continue
        raw = normalize_space(match.group(1))
        try:
            value = converter(raw)
        except Exception:
            value = None
        if value is None:
            continue
        dx = abs(line.norm_x - profile.mean_x) / 0.22
        dy = abs(line.norm_y - profile.mean_y) / 0.10
        distance = min(1.0, math.sqrt(dx * dx + dy * dy) / math.sqrt(2.0))
        sample_bonus = min(0.08, math.log10(max(profile.samples, 3)) * 0.04)
        confidence = 0.78 + sample_bonus + (1.0 - distance) * 0.10
        candidate = fv_from_line(value, raw, min(confidence, 0.94), "layout_profile", line, line.text)
        if candidate.confidence > best.confidence:
            best = candidate
    return best
