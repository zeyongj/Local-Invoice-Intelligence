from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import statistics
import time
from collections import deque
from pathlib import Path
from typing import Callable, Iterable, Optional

import fitz

from invoice_core import build_document_view, merge_views
from invoice_database import InvoiceDatabase
from invoice_models import (
    FieldValue,
    HistoricalStats,
    InvoiceResult,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_REVIEW,
)
from invoice_parsers import detect_vendor, get_parser

DEFAULT_INITIAL_PAGES = 3
DEFAULT_CONFIDENCE_THRESHOLD = 0.75
RECONCILIATION_TOLERANCE = 0.05

CSV_FIELDS = [
    "file_name", "file_path", "vendor", "account_number", "bill_date", "billing_period",
    "current_charges", "total_amount_due", "meter_number", "consumption", "consumption_unit",
    "previous_balance", "payments", "confidence", "status", "issues", "warnings",
    "duplicate_of", "pages_processed", "page_count", "processing_seconds",
]

ProgressCallback = Callable[[dict], None]


def find_pdfs(directory: str | Path, recursive: bool = False) -> list[Path]:
    base = Path(directory)
    iterator = base.rglob("*.pdf") if recursive else base.glob("*.pdf")
    return sorted((path for path in iterator if path.is_file()), key=lambda p: str(p).casefold())


def critical_fields_missing(fields: dict[str, FieldValue]) -> bool:
    return not fields["account_number"].found or not fields["total_amount_due"].found


def calculate_overall_confidence(fields: dict[str, FieldValue], vendor_confidence: float) -> float:
    weights = {
        "account_number": 0.25,
        "total_amount_due": 0.30,
        "bill_date": 0.15,
        "current_charges": 0.15,
        "meter_number": 0.05,
        "billing_period": 0.05,
        "consumption": 0.05,
    }
    score = vendor_confidence * 0.10
    total_weight = 0.10
    for key, weight in weights.items():
        fv = fields.get(key)
        if fv and fv.found:
            score += fv.confidence * weight
        total_weight += weight
    return round(max(0.0, min(1.0, score / total_weight)), 4)


def build_evidence(fields: dict[str, FieldValue]) -> dict[str, dict]:
    evidence: dict[str, dict] = {}
    for name, fv in fields.items():
        if not fv or not fv.found:
            continue
        norm_x = fv.norm_x
        norm_y = fv.norm_y
        norm_w = None
        norm_h = None
        if fv.x0 is not None and fv.x1 is not None and fv.page_width:
            norm_w = (fv.x1 - fv.x0) / fv.page_width
        if fv.y0 is not None and fv.y1 is not None and fv.page_height:
            norm_h = (fv.y1 - fv.y0) / fv.page_height
        evidence[name] = {
            "raw": fv.raw,
            "confidence": fv.confidence,
            "method": fv.method,
            "page": fv.page,
            "evidence": fv.evidence,
            "norm_x": norm_x,
            "norm_y": norm_y,
            "norm_w": norm_w,
            "norm_h": norm_h,
        }
    return evidence


def logical_fingerprint(result: InvoiceResult) -> Optional[str]:
    """Logical duplicate key; intentionally does not hash PDF bytes."""
    if not result.vendor or result.vendor == "UNKNOWN" or not result.account_number:
        return None
    period = result.bill_date or result.billing_period
    amount = result.total_amount_due
    if not period or amount is None:
        return None
    canonical = f"{result.vendor}|{result.account_number}|{period}|{float(amount):.2f}".upper()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def robust_z_score(value: float, samples: list[float]) -> Optional[float]:
    if len(samples) < 5:
        return None
    median = statistics.median(samples)
    deviations = [abs(item - median) for item in samples]
    mad = statistics.median(deviations)
    if mad <= 1e-9:
        if abs(value - median) <= 1e-9:
            return 0.0
        # Stable recurring bills: use a conservative relative fallback.
        scale = max(abs(median) * 0.05, 1.0)
        return (value - median) / scale
    return 0.6745 * (value - median) / mad


def validate_document_fields(vendor: str, fields: dict[str, FieldValue], confidence: float) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    warnings: list[str] = []
    account = fields["account_number"]
    total = fields["total_amount_due"]
    current = fields["current_charges"]
    previous = fields["previous_balance"]
    payments = fields["payments"]

    if vendor == "UNKNOWN":
        issues.append("Vendor could not be confidently identified.")
    if not account.found:
        issues.append("Account number was not found.")
    if not total.found:
        issues.append("Total amount due was not found.")
    if confidence < DEFAULT_CONFIDENCE_THRESHOLD:
        issues.append(f"Overall extraction confidence is low ({confidence:.1%}).")
    if total.found and float(total.value) < 0:
        warnings.append("Total amount due is negative; verify whether this is a credit balance.")

    if previous.found and payments.found and current.found and total.found:
        expected = float(previous.value) - abs(float(payments.value)) + float(current.value)
        difference = abs(expected - float(total.value))
        if difference > RECONCILIATION_TOLERANCE:
            issues.append(
                f"Accounting reconciliation discrepancy: expected ${expected:,.2f}, "
                f"document total ${float(total.value):,.2f}, difference ${difference:,.2f}."
            )
    return issues, warnings


def validate_history(result: InvoiceResult, history: HistoricalStats) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    warnings: list[str] = []
    amount = result.total_amount_due
    if amount is not None and history.amounts:
        z = robust_z_score(float(amount), history.amounts)
        median = statistics.median(history.amounts)
        if z is not None and abs(z) >= 6.0:
            issues.append(
                f"Historical amount anomaly: ${float(amount):,.2f} vs median ${median:,.2f} "
                f"(robust z={z:.1f}, {len(history.amounts)} prior bills)."
            )
        elif z is not None and abs(z) >= 4.0:
            warnings.append(
                f"Unusual amount vs account history: ${float(amount):,.2f} vs median ${median:,.2f} "
                f"(robust z={z:.1f})."
            )
        if history.last_amount not in (None, 0) and len(history.amounts) >= 3:
            change = (float(amount) - float(history.last_amount)) / abs(float(history.last_amount))
            if abs(change) >= 3.0:
                warnings.append(f"Amount changed {change:+.0%} from the most recent recorded bill.")

    if result.meter_number and history.invoice_count >= 3 and len(history.known_meters) == 1:
        known = next(iter(history.known_meters))
        if result.meter_number != known:
            warnings.append(f"Meter number differs from established history ({known} → {result.meter_number}).")
    return issues, warnings


class InvoiceEngine:
    def __init__(self, db_path: str, initial_pages: int = DEFAULT_INITIAL_PAGES, max_pages: int = 0):
        self.db = InvoiceDatabase(db_path)
        self.initial_pages = max(1, int(initial_pages))
        self.max_pages = max(0, int(max_pages))

    def close(self) -> None:
        self.db.close()

    def process_one(self, file_path: str) -> InvoiceResult:
        start = time.perf_counter()
        path = Path(file_path)
        stat = path.stat()
        result = InvoiceResult(
            file_name=path.name,
            file_path=str(path.resolve()),
            file_size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        try:
            with fitz.open(file_path) as doc:
                if doc.page_count <= 0:
                    raise ValueError("PDF contains no pages.")
                result.page_count = doc.page_count
                effective_pages = min(doc.page_count, self.max_pages) if self.max_pages > 0 else doc.page_count
                first_count = min(self.initial_pages, effective_pages)
                first_view = build_document_view(doc, range(first_count))
                result.pages_processed = first_view.pages_processed
                if not first_view.has_text_layer:
                    result.status = STATUS_REVIEW
                    result.issues.append(
                        "PDF does not contain a usable text layer. OCR is intentionally disabled in this build."
                    )
                    return self._finish(result, start)

                vendor, vendor_confidence = detect_vendor(first_view.text)
                profiles = self.db.get_layout_profiles(vendor) if vendor != "UNKNOWN" else {}
                parser = get_parser(vendor)
                fields = parser.parse(first_view, profiles)
                final_view = first_view

                should_deep_parse = (
                    effective_pages > first_count
                    and (critical_fields_missing(fields) or not fields["meter_number"].found or not fields["consumption"].found)
                )
                if should_deep_parse:
                    extra_view = build_document_view(doc, range(first_count, effective_pages))
                    final_view = merge_views(first_view, extra_view)
                    vendor2, vendor_confidence2 = detect_vendor(final_view.text)
                    if vendor_confidence2 > vendor_confidence:
                        vendor, vendor_confidence = vendor2, vendor_confidence2
                        profiles = self.db.get_layout_profiles(vendor) if vendor != "UNKNOWN" else {}
                        parser = get_parser(vendor)
                    fields = parser.parse(final_view, profiles)
                    result.pages_processed = final_view.pages_processed

                confidence = calculate_overall_confidence(fields, vendor_confidence)
                issues, warnings = validate_document_fields(vendor, fields, confidence)
                result.vendor = vendor
                result.account_number = fields["account_number"].value
                result.bill_date = fields["bill_date"].value
                result.billing_period = fields["billing_period"].value
                result.current_charges = fields["current_charges"].value
                result.total_amount_due = fields["total_amount_due"].value
                result.meter_number = fields["meter_number"].value
                result.consumption = fields["consumption"].value
                result.consumption_unit = fields["consumption_unit"].value
                result.previous_balance = fields["previous_balance"].value
                result.payments = fields["payments"].value
                result.confidence = confidence
                result.evidence = build_evidence(fields)
                result.issues.extend(issues)
                result.warnings.extend(warnings)

            if result.vendor != "UNKNOWN" and result.account_number:
                history = self.db.get_history(result.vendor, result.account_number, exclude_path=result.file_path)
                hist_issues, hist_warnings = validate_history(result, history)
                result.issues.extend(hist_issues)
                result.warnings.extend(hist_warnings)

            result.logical_fingerprint = logical_fingerprint(result)
            if result.logical_fingerprint:
                duplicate = self.db.find_duplicate(result.logical_fingerprint, result.file_path)
                if duplicate:
                    result.duplicate_of = duplicate
                    result.status = STATUS_DUPLICATE
                    result.issues.append(f"Logical duplicate of {Path(duplicate).name}.")
                else:
                    result.status = STATUS_REVIEW if result.issues else STATUS_OK
            else:
                result.status = STATUS_REVIEW if result.issues else STATUS_OK

        except Exception as exc:
            result.status = STATUS_FAILED
            result.error = f"{type(exc).__name__}: {exc}"
            result.issues.append(result.error)
        return self._finish(result, start)

    @staticmethod
    def _finish(result: InvoiceResult, start: float) -> InvoiceResult:
        result.processing_seconds = round(time.perf_counter() - start, 4)
        return result

    def process_files(
        self,
        files: Iterable[str | Path],
        *,
        force: bool = False,
        progress: Optional[ProgressCallback] = None,
        commit_every: int = 25,
    ) -> list[InvoiceResult]:
        paths = [Path(path) for path in files]
        total = len(paths)
        results: list[InvoiceResult] = []
        recent_durations: deque[float] = deque(maxlen=12)
        batch_start = time.perf_counter()

        for index, path in enumerate(paths, start=1):
            stat = path.stat()
            is_new = not self.db.exists(str(path))
            skipped = False
            if not force and self.db.unchanged(str(path), stat.st_size, stat.st_mtime_ns):
                skipped = True
                row = self.db.connection.execute(
                    "SELECT * FROM invoices WHERE file_path = ?", (str(path.resolve()),)
                ).fetchone()
                if row:
                    result = self._result_from_row(row)
                else:
                    result = InvoiceResult(file_name=path.name, file_path=str(path.resolve()))
            else:
                result = self.process_one(str(path))
                self.db.save(result)
                if result.status not in {STATUS_DUPLICATE, STATUS_FAILED}:
                    self.db.update_account_registry(result, is_new_file=is_new)
                    self.db.update_layout_profiles(result)
                recent_durations.append(max(result.processing_seconds, 0.001))

            results.append(result)
            if index % max(1, commit_every) == 0 or index == total:
                self.db.commit()

            elapsed = time.perf_counter() - batch_start
            remaining = total - index
            if recent_durations:
                eta = statistics.median(recent_durations) * remaining
            else:
                eta = (elapsed / index) * remaining if index else 0.0
            if progress:
                progress(
                    {
                        "index": index,
                        "total": total,
                        "fraction": index / total if total else 1.0,
                        "eta_seconds": max(0.0, eta),
                        "elapsed_seconds": elapsed,
                        "current_file": path.name,
                        "result": result,
                        "skipped": skipped,
                    }
                )
        return results

    @staticmethod
    def _result_from_row(row) -> InvoiceResult:
        try:
            issues = json.loads(row["issues_json"] or "[]")
        except Exception:
            issues = []
        try:
            warnings = json.loads(row["warnings_json"] or "[]")
        except Exception:
            warnings = []
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except Exception:
            evidence = {}
        return InvoiceResult(
            file_name=row["file_name"], file_path=row["file_path"], vendor=row["vendor"] or "UNKNOWN",
            account_number=row["account_number"], bill_date=row["bill_date"], billing_period=row["billing_period"],
            current_charges=row["current_charges"], total_amount_due=row["total_amount_due"],
            meter_number=row["meter_number"], consumption=row["consumption"], consumption_unit=row["consumption_unit"],
            previous_balance=row["previous_balance"], payments=row["payments"], confidence=float(row["confidence"] or 0.0),
            status=row["status"], issues=issues, warnings=warnings, evidence=evidence,
            page_count=int(row["page_count"] or 0), pages_processed=int(row["pages_processed"] or 0),
            file_size=int(row["file_size"] or 0), mtime_ns=int(row["mtime_ns"] or 0),
            processing_seconds=float(row["processing_seconds"] or 0.0), error=row["error"],
            logical_fingerprint=row["logical_fingerprint"], duplicate_of=row["duplicate_of"],
        )

    def export_csv(self, results: list[InvoiceResult], destination: str) -> None:
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for item in results:
                writer.writerow(
                    {
                        "file_name": item.file_name,
                        "file_path": item.file_path,
                        "vendor": item.vendor,
                        "account_number": item.account_number,
                        "bill_date": item.bill_date,
                        "billing_period": item.billing_period,
                        "current_charges": item.current_charges,
                        "total_amount_due": item.total_amount_due,
                        "meter_number": item.meter_number,
                        "consumption": item.consumption,
                        "consumption_unit": item.consumption_unit,
                        "previous_balance": item.previous_balance,
                        "payments": item.payments,
                        "confidence": item.confidence,
                        "status": item.status,
                        "issues": " | ".join(item.issues),
                        "warnings": " | ".join(item.warnings),
                        "duplicate_of": item.duplicate_of,
                        "pages_processed": item.pages_processed,
                        "page_count": item.page_count,
                        "processing_seconds": item.processing_seconds,
                    }
                )
