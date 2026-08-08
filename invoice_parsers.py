from __future__ import annotations

import re
from typing import Optional

from invoice_core import (
    ACCOUNT_CAPTURE,
    ACCOUNT_VALUE_RE,
    CONSUMPTION_CAPTURE,
    DATE_VALUE_RE,
    METER_CAPTURE,
    METER_VALUE_RE,
    MONEY_CAPTURE,
    MONEY_VALUE_RE,
    SpatialIndex,
    best_field,
    line_pattern_extract,
    normalize_account,
    parse_identifier,
    normalize_space,
    parse_date,
    parse_money,
    parse_number,
    rx,
    spatial_extract,
    template_extract,
)
from invoice_models import DocumentView, FieldValue, LayoutProfile

MONTH_DATE = r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})"

VENDOR_SIGNATURES: dict[str, list[tuple[re.Pattern[str], float]]] = {
    "TELUS": [
        (rx(r"\bTELUS\b"), 1.0),
        (rx(r"\btelus\.com\b"), 0.8),
        (rx(r"\bMy TELUS\b"), 0.8),
    ],
    "BC HYDRO": [
        (rx(r"\bBC\s+Hydro\b"), 1.0),
        (rx(r"\bbchydro\.com\b"), 0.9),
        (rx(r"\bElectricity charges\b"), 0.4),
    ],
    "FORTISBC": [
        (rx(r"\bFortisBC\b"), 1.0),
        (rx(r"\bfortisbc\.com\b"), 0.9),
        (rx(r"\bNatural gas\b"), 0.3),
    ],
}


def detect_vendor(text: str) -> tuple[str, float]:
    sample = text[:30000]
    winner, winner_score = "UNKNOWN", 0.0
    for vendor, rules in VENDOR_SIGNATURES.items():
        score = sum(weight for pattern, weight in rules if pattern.search(sample))
        if score > winner_score:
            winner, winner_score = vendor, score
    if winner_score <= 0:
        return "UNKNOWN", 0.0
    return winner, min(0.99, 0.70 + winner_score * 0.15)


class InvoiceParser:
    vendor = "GENERIC"

    account_patterns = [
        rx(r"(?:account|acct)\s*(?:number|no\.?|#)?\s*[:\-]?\s*" + ACCOUNT_CAPTURE)
    ]
    current_charge_patterns = [
        rx(r"\bcurrent\s+charges?\b\s*[:\-]?\s*" + MONEY_CAPTURE),
        rx(r"\bnew\s+charges?\b\s*[:\-]?\s*" + MONEY_CAPTURE),
    ]
    total_due_patterns = [
        rx(r"\btotal\s+amount\s+due\b\s*[:\-]?\s*" + MONEY_CAPTURE),
        rx(r"\bamount\s+due\b\s*[:\-]?\s*" + MONEY_CAPTURE),
        rx(r"\btotal\s+due\b\s*[:\-]?\s*" + MONEY_CAPTURE),
    ]
    previous_balance_patterns = [
        rx(r"\bprevious\s+balance\b\s*[:\-]?\s*" + MONEY_CAPTURE)
    ]
    payment_patterns = [
        rx(r"\bpayments?(?:\s+received)?\b\s*[:\-]?\s*" + MONEY_CAPTURE)
    ]
    meter_patterns = [
        rx(r"\bmeter\s*(?:number|no\.?|#)?\s*[:\-]?\s*" + METER_CAPTURE)
    ]
    bill_date_patterns = [
        rx(r"\bbill\s+date\b\s*[:\-]?\s*" + MONTH_DATE),
        rx(r"\binvoice\s+date\b\s*[:\-]?\s*" + MONTH_DATE),
    ]
    billing_period_patterns = [
        rx(r"\bbilling\s+period\b\s*[:\-]?\s*(.{5,60})"),
        rx(r"\bservice\s+period\b\s*[:\-]?\s*(.{5,60})"),
    ]
    consumption_patterns = [
        rx(r"\b(?:usage|consumption)\b\s*[:\-]?\s*" + CONSUMPTION_CAPTURE)
    ]

    account_anchors = [rx(r"\baccount\s*(?:number|no\.?|#)\b")]
    total_anchors = [rx(r"\btotal\s+amount\s+due\b"), rx(r"\bamount\s+due\b")]
    current_anchors = [rx(r"\bcurrent\s+charges?\b"), rx(r"\bnew\s+charges?\b")]
    meter_anchors = [rx(r"\bmeter\s*(?:number|no\.?|#)\b")]
    bill_date_anchors = [rx(r"\bbill\s+date\b"), rx(r"\binvoice\s+date\b")]

    def parse(
        self,
        view: DocumentView,
        profiles: Optional[dict[str, LayoutProfile]] = None,
    ) -> dict[str, FieldValue]:
        profiles = profiles or {}
        index = SpatialIndex(view.lines)
        fields: dict[str, FieldValue] = {}

        fields["account_number"] = best_field(
            line_pattern_extract(view.lines, self.account_patterns, parse_identifier),
            template_extract(index, profiles.get("account_number"), ACCOUNT_VALUE_RE, parse_identifier),
            spatial_extract(view.lines, index, self.account_anchors, ACCOUNT_VALUE_RE, parse_identifier),
        )
        fields["current_charges"] = best_field(
            line_pattern_extract(view.lines, self.current_charge_patterns, parse_money),
            template_extract(index, profiles.get("current_charges"), MONEY_VALUE_RE, parse_money),
            spatial_extract(view.lines, index, self.current_anchors, MONEY_VALUE_RE, parse_money),
        )
        fields["total_amount_due"] = best_field(
            line_pattern_extract(view.lines, self.total_due_patterns, parse_money),
            template_extract(index, profiles.get("total_amount_due"), MONEY_VALUE_RE, parse_money),
            spatial_extract(view.lines, index, self.total_anchors, MONEY_VALUE_RE, parse_money),
        )
        fields["previous_balance"] = line_pattern_extract(
            view.lines, self.previous_balance_patterns, parse_money
        )
        fields["payments"] = line_pattern_extract(view.lines, self.payment_patterns, parse_money)
        fields["meter_number"] = best_field(
            line_pattern_extract(view.lines, self.meter_patterns, parse_identifier),
            template_extract(index, profiles.get("meter_number"), METER_VALUE_RE, parse_identifier),
            spatial_extract(view.lines, index, self.meter_anchors, METER_VALUE_RE, parse_identifier),
        )
        fields["bill_date"] = best_field(
            line_pattern_extract(view.lines, self.bill_date_patterns, parse_date),
            template_extract(index, profiles.get("bill_date"), DATE_VALUE_RE, parse_date),
            spatial_extract(view.lines, index, self.bill_date_anchors, DATE_VALUE_RE, parse_date),
        )
        fields["billing_period"] = line_pattern_extract(
            view.lines, self.billing_period_patterns, normalize_space, confidence=0.88
        )
        consumption, unit = self.extract_consumption(view, profiles, index)
        fields["consumption"] = consumption
        fields["consumption_unit"] = unit
        return fields

    def extract_consumption(
        self,
        view: DocumentView,
        profiles: dict[str, LayoutProfile],
        index: SpatialIndex,
    ) -> tuple[FieldValue, FieldValue]:
        best_number, best_unit = FieldValue(), FieldValue()
        for line in view.lines:
            text = normalize_space(line.text)
            for pattern in self.consumption_patterns:
                match = pattern.search(text)
                if not match:
                    continue
                number = parse_number(match.group(1))
                if number is None:
                    continue
                best_number = FieldValue(
                    value=number,
                    raw=match.group(1),
                    confidence=0.90,
                    method="label_regex",
                    page=line.page + 1,
                    evidence=text,
                    x0=line.x0,
                    y0=line.y0,
                    x1=line.x1,
                    y1=line.y1,
                    page_width=line.page_width,
                    page_height=line.page_height,
                )
                if len(match.groups()) >= 2 and match.group(2):
                    best_unit = FieldValue(
                        value=normalize_space(match.group(2)),
                        raw=match.group(2),
                        confidence=0.90,
                        method="label_regex",
                        page=line.page + 1,
                        evidence=text,
                        x0=line.x0,
                        y0=line.y0,
                        x1=line.x1,
                        y1=line.y1,
                        page_width=line.page_width,
                        page_height=line.page_height,
                    )
                return best_number, best_unit
        return best_number, best_unit


class TelusParser(InvoiceParser):
    vendor = "TELUS"
    account_patterns = [
        rx(r"\baccount\s*(?:number|#)?\s*[:\-]?\s*([0-9][0-9\- ]{6,18})")
    ]
    current_charge_patterns = [
        rx(r"\bcurrent\s+charges?\b\s*[:\-]?\s*" + MONEY_CAPTURE),
        rx(r"\bthis\s+month(?:'s)?\s+charges?\b\s*[:\-]?\s*" + MONEY_CAPTURE),
    ]


class BCHydroParser(InvoiceParser):
    vendor = "BC HYDRO"
    account_patterns = [
        rx(r"\baccount\s*(?:number|no\.?|#)?\s*[:\-]?\s*([0-9][0-9\- ]{7,20})")
    ]
    meter_patterns = [
        rx(r"\bmeter\s*(?:number|no\.?|#)?\s*[:\-]?\s*([A-Z0-9\-]{4,20})")
    ]
    consumption_patterns = [
        rx(r"\b(?:electricity\s+)?(?:usage|consumption)\b\s*[:\-]?\s*([\d,]+(?:\.\d+)?)\s*(kWh|MWh)")
    ]


class FortisBCParser(InvoiceParser):
    vendor = "FORTISBC"
    account_patterns = [
        rx(r"\baccount\s*(?:number|no\.?|#)?\s*[:\-]?\s*([0-9][0-9\- ]{7,20})")
    ]
    consumption_patterns = [
        rx(r"\b(?:natural\s+gas\s+)?(?:usage|consumption)\b\s*[:\-]?\s*([\d,]+(?:\.\d+)?)\s*(GJ|m3|m³)")
    ]


PARSER_MAP = {
    "TELUS": TelusParser,
    "BC HYDRO": BCHydroParser,
    "FORTISBC": FortisBCParser,
}


def get_parser(vendor: str) -> InvoiceParser:
    return PARSER_MAP.get(vendor, InvoiceParser)()
