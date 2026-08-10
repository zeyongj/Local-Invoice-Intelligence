from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import os
import statistics
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable, Optional

import fitz

from invoice_core import build_document_view, merge_views
from invoice_database import InvoiceDatabase
from invoice_models import (
    FieldValue,
    HistoricalStats,
    InvoiceResult,
    RESOURCE_POLICIES,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_REVIEW,
)
from invoice_parsers import detect_vendor, get_parser
from portfolio_intelligence import effective_unit_cost, parse_billing_period, period_length_days
from version import APP_VERSION, RULE_VERSION

DEFAULT_INITIAL_PAGES = 3
DEFAULT_CONFIDENCE_THRESHOLD = 0.75
RECONCILIATION_TOLERANCE = 0.05

CSV_FIELDS = [
    "document_id", "run_id", "file_name", "file_path", "vendor", "property_id", "property_name",
    "account_number_raw", "account_number_canonical", "bill_date", "billing_period",
    "billing_period_start", "billing_period_end",
    "current_charges", "total_amount_due", "meter_number", "consumption", "consumption_unit", "effective_unit_cost",
    "previous_balance", "payments", "confidence", "status", "issues", "warnings",
    "duplicate_of", "near_duplicate_of", "near_duplicate_reason", "parser_version", "rule_version",
    "app_version", "portfolio_flags", "revision_of_document_id", "pages_processed", "page_count", "processing_seconds", "processed_at",
]

ProgressCallback = Callable[[dict], None]
CancelCallback = Callable[[], bool]


def find_pdfs(directory: str | Path, recursive: bool = False) -> list[Path]:
    base = Path(directory)
    iterator = base.rglob("*.pdf") if recursive else base.glob("*.pdf")
    return sorted((path for path in iterator if path.is_file()), key=lambda p: str(p).casefold())


def apply_resource_policy(name: str) -> None:
    """Best-effort Windows process priority control; harmless elsewhere."""
    policy = RESOURCE_POLICIES.get(name, RESOURCE_POLICIES["Balanced"])
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetCurrentProcess()
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        NORMAL_PRIORITY_CLASS = 0x00000020
        priority = BELOW_NORMAL_PRIORITY_CLASS if policy.process_priority == "below_normal" else NORMAL_PRIORITY_CLASS
        kernel32.SetPriorityClass(handle, priority)
    except Exception:
        pass


def critical_fields_missing(fields: dict[str, FieldValue]) -> bool:
    return not fields["account_number"].found or not fields["total_amount_due"].found


def calculate_overall_confidence(fields: dict[str, FieldValue], vendor_confidence: float) -> float:
    weights = {
        "account_number": 0.25, "total_amount_due": 0.30, "bill_date": 0.15,
        "current_charges": 0.15, "meter_number": 0.05, "billing_period": 0.05, "consumption": 0.05,
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
        norm_w = (fv.x1 - fv.x0) / fv.page_width if None not in (fv.x0, fv.x1) and fv.page_width else None
        norm_h = (fv.y1 - fv.y0) / fv.page_height if None not in (fv.y0, fv.y1) and fv.page_height else None
        evidence[name] = {
            "raw": fv.raw, "value": fv.value, "confidence": fv.confidence, "method": fv.method,
            "page": fv.page, "evidence": fv.evidence,
            "x0": fv.x0, "y0": fv.y0, "x1": fv.x1, "y1": fv.y1,
            "page_width": fv.page_width, "page_height": fv.page_height,
            "norm_x": fv.norm_x, "norm_y": fv.norm_y, "norm_w": norm_w, "norm_h": norm_h,
        }
    return evidence


def logical_fingerprint(result: InvoiceResult) -> Optional[str]:
    if not result.vendor or result.vendor == "UNKNOWN" or not result.account_number:
        return None
    period = (f"{result.billing_period_start}/{result.billing_period_end}" if result.billing_period_start and result.billing_period_end
              else result.bill_date or result.billing_period)
    if not period or result.total_amount_due is None:
        return None
    canonical = f"{result.vendor}|{result.account_number}|{period}|{float(result.total_amount_due):.2f}".upper()
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
        scale = max(abs(median) * 0.05, 1.0)
        return (value - median) / scale
    return 0.6745 * (value - median) / mad


def validate_document_fields(vendor: str, fields: dict[str, FieldValue], confidence: float) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    warnings: list[str] = []
    account, total = fields["account_number"], fields["total_amount_due"]
    current, previous, payments = fields["current_charges"], fields["previous_balance"], fields["payments"]
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


def validate_history(result: InvoiceResult, history: HistoricalStats) -> tuple[list[str], list[str], list[str]]:
    issues: list[str] = []
    warnings: list[str] = []
    flags: list[str] = []

    amount = result.total_amount_due
    if amount is not None and history.amounts:
        baseline = history.seasonal_amounts if len(history.seasonal_amounts) >= 5 else history.amounts
        z = robust_z_score(float(amount), baseline)
        median = statistics.median(baseline)
        label = "same-month history" if baseline is history.seasonal_amounts else "account history"
        if z is not None and abs(z) >= 6.0:
            issues.append(f"Historical amount anomaly vs {label}: ${float(amount):,.2f} vs median ${median:,.2f} (robust z={z:.1f}, {len(baseline)} prior bills).")
            flags.append("AMOUNT_ANOMALY_HIGH")
        elif z is not None and abs(z) >= 4.0:
            warnings.append(f"Unusual amount vs {label}: ${float(amount):,.2f} vs median ${median:,.2f} (robust z={z:.1f}).")
            flags.append("AMOUNT_ANOMALY_MEDIUM")

    if result.consumption is not None and history.consumptions:
        baseline_c = history.seasonal_consumptions if len(history.seasonal_consumptions) >= 5 else history.consumptions
        zc = robust_z_score(float(result.consumption), baseline_c)
        med_c = statistics.median(baseline_c)
        label_c = "same-month history" if baseline_c is history.seasonal_consumptions else "account history"
        if zc is not None and abs(zc) >= 6.0:
            issues.append(f"Historical consumption anomaly vs {label_c}: {float(result.consumption):,.2f} vs median {med_c:,.2f} (robust z={zc:.1f}).")
            flags.append("CONSUMPTION_ANOMALY_HIGH")
        elif zc is not None and abs(zc) >= 4.0:
            warnings.append(f"Unusual consumption vs {label_c}: {float(result.consumption):,.2f} vs median {med_c:,.2f} (robust z={zc:.1f}).")
            flags.append("CONSUMPTION_ANOMALY_MEDIUM")

    if result.effective_unit_cost is not None and history.unit_costs:
        baseline_u = history.seasonal_unit_costs if len(history.seasonal_unit_costs) >= 5 else history.unit_costs
        zu = robust_z_score(float(result.effective_unit_cost), baseline_u)
        med_u = statistics.median(baseline_u)
        if zu is not None and abs(zu) >= 6.0:
            issues.append(f"Effective unit-cost anomaly: {result.effective_unit_cost:.4f} vs historical median {med_u:.4f} (robust z={zu:.1f}).")
            flags.append("UNIT_COST_ANOMALY_HIGH")
        elif zu is not None and abs(zu) >= 4.0:
            warnings.append(f"Unusual effective unit cost: {result.effective_unit_cost:.4f} vs historical median {med_u:.4f}.")
            flags.append("UNIT_COST_ANOMALY_MEDIUM")

    # Amount-consumption divergence: a control signal, not a tariff model.
    if (amount is not None and result.consumption is not None and history.last_amount not in (None, 0)
            and history.last_consumption not in (None, 0)):
        amount_change = (float(amount) - float(history.last_amount)) / abs(float(history.last_amount))
        consumption_change = (float(result.consumption) - float(history.last_consumption)) / abs(float(history.last_consumption))
        divergence = abs(amount_change - consumption_change)
        if divergence >= 1.0 and (abs(amount_change) >= 0.5 or abs(consumption_change) >= 0.5):
            warnings.append(f"Amount/consumption divergence: amount {amount_change:+.0%}, consumption {consumption_change:+.0%}; usage alone may not explain the bill movement.")
            flags.append("AMOUNT_CONSUMPTION_DIVERGENCE")

    if result.meter_number and history.invoice_count >= 3 and len(history.known_meters) == 1:
        known = next(iter(history.known_meters))
        if result.meter_number != known:
            warnings.append(f"Meter number differs from established invoice history ({known} → {result.meter_number}).")
            flags.append("METER_HISTORY_CHANGE")
    return issues, warnings, flags


def validate_billing_continuity(result: InvoiceResult, rows: list) -> tuple[list[str], list[str], list[str]]:
    issues: list[str] = []; warnings: list[str] = []; flags: list[str] = []
    if not result.billing_period_start or not result.billing_period_end:
        return issues, warnings, flags
    from datetime import date
    current_start = date.fromisoformat(result.billing_period_start)
    current_end = date.fromisoformat(result.billing_period_end)
    current_len = (current_end - current_start).days + 1
    lengths: list[int] = []
    prior_ends = []
    for row in rows:
        try:
            s = date.fromisoformat(row["billing_period_start"]); e = date.fromisoformat(row["billing_period_end"])
        except Exception:
            continue
        lengths.append((e - s).days + 1)
        # Identical periods are handled as revised invoices; do not double-report overlap.
        if s == current_start and e == current_end:
            continue
        overlap_start = max(s, current_start); overlap_end = min(e, current_end)
        if overlap_start <= overlap_end:
            days = (overlap_end - overlap_start).days + 1
            issues.append(f"Billing period overlap: {days} day(s) overlap with prior invoice {row['document_id']} ({s.isoformat()} to {e.isoformat()}).")
            flags.append("BILLING_OVERLAP_HIGH")
        if e < current_start:
            prior_ends.append(e)
    if prior_ends:
        prior_end = max(prior_ends)
        gap = (current_start - prior_end).days - 1
        typical = statistics.median(lengths) if len(lengths) >= 3 else current_len
        high_threshold = max(14, int(round(typical * 0.45)))
        if gap >= high_threshold:
            issues.append(f"Possible billing-period gap: {gap} uncovered day(s) between {prior_end.isoformat()} and {current_start.isoformat()}; a utility invoice may be missing.")
            flags.append("BILLING_GAP_HIGH")
        elif gap >= 5:
            warnings.append(f"Billing-period gap of {gap} day(s) before the current period; verify provider cadence.")
            flags.append("BILLING_GAP_MEDIUM")
    return issues, warnings, flags


def write_results_csv(results: list[InvoiceResult], destination: str) -> None:
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for item in results:
            writer.writerow({
                "document_id": item.document_id, "run_id": item.run_id, "file_name": item.file_name,
                "file_path": item.file_path, "vendor": item.vendor, "property_id": item.property_id, "property_name": item.property_name,
                "account_number_raw": item.account_number_raw,
                "account_number_canonical": item.account_number, "bill_date": item.bill_date,
                "billing_period": item.billing_period, "billing_period_start": item.billing_period_start, "billing_period_end": item.billing_period_end,
                "current_charges": item.current_charges,
                "total_amount_due": item.total_amount_due, "meter_number": item.meter_number,
                "consumption": item.consumption, "consumption_unit": item.consumption_unit, "effective_unit_cost": item.effective_unit_cost,
                "previous_balance": item.previous_balance, "payments": item.payments, "confidence": item.confidence,
                "status": item.status, "issues": " | ".join(item.issues), "warnings": " | ".join(item.warnings),
                "duplicate_of": item.duplicate_of, "near_duplicate_of": item.near_duplicate_of,
                "near_duplicate_reason": item.near_duplicate_reason, "parser_version": item.parser_version,
                "rule_version": item.rule_version, "app_version": item.app_version, "portfolio_flags": " | ".join(item.portfolio_flags),
                "revision_of_document_id": item.revision_of_document_id, "pages_processed": item.pages_processed, "page_count": item.page_count,
                "processing_seconds": item.processing_seconds, "processed_at": item.processed_at,
            })


class InvoiceEngine:
    def __init__(self, db_path: str, initial_pages: int = DEFAULT_INITIAL_PAGES, max_pages: int = 0,
                 resource_mode: str = "Balanced", auto_seed_portfolio: bool = False):
        apply_resource_policy(resource_mode)
        self.db = InvoiceDatabase(db_path)
        bundled_portfolio = Path(__file__).resolve().parent / "data" / "pm.csv"
        if auto_seed_portfolio and not self.db.has_portfolio() and bundled_portfolio.exists():
            self.db.import_property_master(bundled_portfolio)
        self.initial_pages = max(1, int(initial_pages))
        self.max_pages = max(0, int(max_pages))
        self.resource_mode = resource_mode if resource_mode in RESOURCE_POLICIES else "Balanced"

    def close(self) -> None:
        self.db.close()

    def process_one(self, file_path: str, run_id: Optional[str] = None) -> InvoiceResult:
        start = time.perf_counter()
        path = Path(file_path)
        stat = path.stat()
        result = InvoiceResult(
            file_name=path.name, file_path=str(path.resolve()), file_size=stat.st_size, mtime_ns=stat.st_mtime_ns,
            document_id=self.db.get_document_id(file_path) or ("DOC-" + uuid.uuid4().hex),
            run_id=run_id, app_version=APP_VERSION, rule_version=RULE_VERSION,
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
                    result.issues.append("PDF does not contain a usable text layer. OCR is intentionally disabled in this build.")
                    return self._finish(result, start)

                vendor, vendor_confidence = detect_vendor(first_view.text)
                parser = get_parser(vendor)
                result.parser_version = parser.VERSION
                profiles = self.db.get_layout_profiles(vendor) if vendor != "UNKNOWN" else {}
                fields = parser.parse(first_view, profiles)
                final_view = first_view

                should_deep_parse = effective_pages > first_count and (
                    critical_fields_missing(fields) or not fields["meter_number"].found or not fields["consumption"].found
                )
                if should_deep_parse:
                    extra_view = build_document_view(doc, range(first_count, effective_pages))
                    final_view = merge_views(first_view, extra_view)
                    vendor2, vendor_confidence2 = detect_vendor(final_view.text)
                    if vendor_confidence2 > vendor_confidence:
                        vendor, vendor_confidence = vendor2, vendor_confidence2
                        parser = get_parser(vendor)
                        result.parser_version = parser.VERSION
                        profiles = self.db.get_layout_profiles(vendor) if vendor != "UNKNOWN" else {}
                    fields = parser.parse(final_view, profiles)
                    result.pages_processed = final_view.pages_processed

                confidence = calculate_overall_confidence(fields, vendor_confidence)
                issues, warnings = validate_document_fields(vendor, fields, confidence)

                result.vendor = vendor
                account_fv = fields["account_number"]
                result.account_number = account_fv.value if account_fv.found else None
                result.account_number_raw = account_fv.raw if account_fv.found else None
                for attr in (
                    "bill_date", "billing_period", "current_charges", "total_amount_due", "meter_number",
                    "consumption", "consumption_unit", "previous_balance", "payments",
                ):
                    fv = fields[attr]
                    setattr(result, attr, fv.value if fv.found else None)
                result.confidence = confidence
                result.evidence = build_evidence(fields)
                result.issues.extend(issues)
                result.warnings.extend(warnings)

                result.billing_period_start, result.billing_period_end = parse_billing_period(result.billing_period)
                result.effective_unit_cost = effective_unit_cost(result.current_charges, result.total_amount_due, result.consumption)

                # Property × Vendor × Account × Meter Master. The canonical
                # account identity is the join key; raw PDF formatting is retained.
                if result.account_number:
                    utility = self.db.get_utility_account(vendor, result.account_number)
                    if utility:
                        result.property_id = str(utility["property_id"])
                        result.property_name = str(utility["project_name"])
                        if result.meter_number:
                            known = self.db.known_meters_for_account(int(utility["utility_account_id"]))
                            meter_conf = float(result.evidence.get("meter_number", {}).get("confidence") or 0)
                            if not known and meter_conf >= 0.88:
                                self.db.register_meter(int(utility["utility_account_id"]), result.meter_number)
                            elif known and result.meter_number not in known:
                                result.issues.append(
                                    f"Meter master mismatch for project {result.property_id}: invoice meter {result.meter_number} "
                                    f"is not in registered meter(s) {', '.join(sorted(known))}."
                                )
                                result.portfolio_flags.append("METER_MASTER_MISMATCH")
                    elif self.db.has_portfolio():
                        result.suggested_property_id = self.db.suggest_property_from_path(result.file_path)
                        suffix = f" Suggested project from path: {result.suggested_property_id}." if result.suggested_property_id else ""
                        result.issues.append(
                            f"Utility account {vendor} {result.account_number} is not mapped to the Property × Vendor × Account × Meter Master.{suffix}"
                        )
                        result.portfolio_flags.append("UNMAPPED_UTILITY_ACCOUNT")

                    history = self.db.get_history(
                        vendor, result.account_number, bill_date=result.bill_date, exclude_path=result.file_path
                    )
                    hist_issues, hist_warnings, hist_flags = validate_history(result, history)
                    result.issues.extend(hist_issues); result.warnings.extend(hist_warnings); result.portfolio_flags.extend(hist_flags)

                    billing_rows = self.db.billing_history(vendor, result.account_number, exclude_path=result.file_path)
                    bill_issues, bill_warnings, bill_flags = validate_billing_continuity(result, billing_rows)
                    result.issues.extend(bill_issues); result.warnings.extend(bill_warnings); result.portfolio_flags.extend(bill_flags)

                result.logical_fingerprint = logical_fingerprint(result)
                if result.logical_fingerprint:
                    duplicate = self.db.duplicate_record(result.logical_fingerprint, result.file_path)
                    if duplicate:
                        result.duplicate_of = str(duplicate["file_path"])
                        result.status = STATUS_DUPLICATE
                        result.portfolio_flags.append("PORTFOLIO_EXACT_DUPLICATE")
                        if result.property_id and duplicate["property_id"] and result.property_id != duplicate["property_id"]:
                            result.issues.append(
                                f"CRITICAL cross-property duplicate: current project {result.property_id}, prior project {duplicate['property_id']}."
                            )
                            result.portfolio_flags.append("CROSS_PROPERTY_DUPLICATE")
                    elif result.account_number and result.total_amount_due is not None:
                        revised = self.db.revised_candidate(
                            result.vendor, result.account_number, result.billing_period_start, result.billing_period_end,
                            result.bill_date, float(result.total_amount_due), result.file_path,
                        )
                        if revised:
                            result.near_duplicate_of = str(revised["file_path"])
                            old_amount = float(revised["total_amount_due"])
                            result.near_duplicate_reason = (
                                f"Same vendor/account/billing identity but amount differs: prior ${old_amount:,.2f}, "
                                f"current ${float(result.total_amount_due):,.2f}. Possible revised invoice / reissued invoice."
                            )
                            result.revision_of_document_id = str(revised["document_id"])
                            group_key = f"{result.vendor}|{result.account_number}|{result.billing_period_start or result.bill_date}|{result.billing_period_end or ''}"
                            result.revision_group_id = "REV-" + hashlib.sha256(group_key.encode("utf-8")).hexdigest()[:20]
                            result.issues.append(result.near_duplicate_reason)
                            result.portfolio_flags.append("POSSIBLE_REVISED_INVOICE")

                result.portfolio_flags = list(dict.fromkeys(result.portfolio_flags))
                if result.status != STATUS_DUPLICATE:
                    result.status = STATUS_REVIEW if result.issues else STATUS_OK

        except Exception as exc:
            result.status = STATUS_FAILED
            result.error = f"{type(exc).__name__}: {exc}"
            result.issues.append(result.error)
        return self._finish(result, start)

    def _persist_portfolio_controls(self, result: InvoiceResult) -> None:
        if not result.document_id:
            return
        self.db.clear_anomalies(result.document_id)
        severity_map = {
            "CROSS_PROPERTY_DUPLICATE": "CRITICAL",
            "PORTFOLIO_EXACT_DUPLICATE": "HIGH",
            "POSSIBLE_REVISED_INVOICE": "HIGH",
            "BILLING_OVERLAP_HIGH": "HIGH",
            "BILLING_GAP_HIGH": "HIGH",
            "AMOUNT_ANOMALY_HIGH": "HIGH",
            "CONSUMPTION_ANOMALY_HIGH": "HIGH",
            "UNIT_COST_ANOMALY_HIGH": "HIGH",
            "METER_MASTER_MISMATCH": "HIGH",
            "UNMAPPED_UTILITY_ACCOUNT": "MEDIUM",
        }
        combined = result.issues + result.warnings
        for flag in result.portfolio_flags:
            words = [part.lower() for part in flag.split("_") if len(part) > 3]
            message = next((m for m in combined if any(w in m.lower() for w in words)), flag.replace("_", " ").title())
            self.db.add_anomaly(result.document_id, flag, severity_map.get(flag, "MEDIUM"), message)
        if result.duplicate_of:
            other = self.db.get_document_id(result.duplicate_of)
            if other:
                self.db.add_relationship("EXACT_DUPLICATE", result.document_id, other, {"file": result.duplicate_of})
        if result.revision_of_document_id:
            self.db.add_relationship("POSSIBLE_REVISION", result.document_id, result.revision_of_document_id,
                                     {"group": result.revision_group_id, "reason": result.near_duplicate_reason})

    @staticmethod
    def _finish(result: InvoiceResult, start: float) -> InvoiceResult:
        result.processing_seconds = round(time.perf_counter() - start, 4)
        return result

    def process_files(self, paths: list[str | Path], force: bool = False, progress: Optional[ProgressCallback] = None,
                      cancel_requested: Optional[CancelCallback] = None, run_id: Optional[str] = None,
                      source_label: str = "", output_path: str = "") -> list[InvoiceResult]:
        path_list = [Path(path) for path in paths]
        if run_id:
            resume_paths = self.db.prepare_resume(run_id)
            if resume_paths:
                path_list = [Path(path) for path in resume_paths]
        else:
            run_id = self.db.create_run(path_list, source_label=source_label, output_path=output_path)

        total = len(path_list)
        results: list[InvoiceResult] = []
        recent_times: deque[float] = deque(maxlen=RESOURCE_POLICIES[self.resource_mode].recent_eta_window)
        commit_every = RESOURCE_POLICIES[self.resource_mode].commit_every
        cancelled = False

        try:
            for index, path in enumerate(path_list, start=1):
                if cancel_requested and cancel_requested():
                    cancelled = True
                    break
                resolved = str(path.resolve())
                self.db.mark_run_item(run_id, resolved, "PROCESSING")
                skipped = False
                result: Optional[InvoiceResult] = None
                try:
                    stat = path.stat()
                    if not force and self.db.unchanged(resolved, stat.st_size, stat.st_mtime_ns):
                        result = self.db.load_result(resolved)
                        skipped = result is not None
                        if result:
                            result.run_id = run_id
                            self.db.mark_run_item(run_id, resolved, "CACHED")
                    if result is None:
                        was_existing = self.db.get_document_id(resolved) is not None
                        result = self.process_one(resolved, run_id=run_id)
                        self.db.save(result)
                        self._persist_portfolio_controls(result)
                        if result.status not in {STATUS_DUPLICATE, STATUS_FAILED}:
                            self.db.update_account_registry(result, is_new_file=not was_existing)
                            self.db.update_layout_profiles(result)
                        self.db.audit(
                            "DOCUMENT_PROCESSED", run_id=run_id, document_id=result.document_id, file_path=result.file_path,
                            details={"status": result.status, "parser_version": result.parser_version,
                                     "rule_version": result.rule_version, "confidence": result.confidence},
                        )
                        self.db.mark_run_item(run_id, resolved, "FAILED" if result.status == STATUS_FAILED else "COMPLETED", result.error)
                        recent_times.append(max(result.processing_seconds, 0.001))
                except Exception as exc:
                    result = InvoiceResult(
                        file_name=path.name, file_path=resolved, file_size=path.stat().st_size if path.exists() else 0,
                        mtime_ns=path.stat().st_mtime_ns if path.exists() else 0, status=STATUS_FAILED,
                        issues=[f"{type(exc).__name__}: {exc}"], error=f"{type(exc).__name__}: {exc}", run_id=run_id,
                        document_id="DOC-" + uuid.uuid4().hex, app_version=APP_VERSION, rule_version=RULE_VERSION,
                    )
                    self.db.save(result)
                    self.db.mark_run_item(run_id, resolved, "FAILED", result.error)

                results.append(result)
                if index % commit_every == 0:
                    self.db.commit()
                if progress:
                    eta = statistics.median(recent_times) * (total - index) if recent_times else 0.0
                    progress({
                        "run_id": run_id, "index": index, "total": total, "fraction": index / total if total else 1.0,
                        "eta_seconds": eta, "current_file": path.name, "skipped": skipped, "result": result,
                    })

            if cancelled:
                self.db.cancel_remaining_run_items(run_id)
                self.db.finish_run(run_id, RUN_CANCELLED)
            else:
                self.db.finish_run(run_id, RUN_COMPLETED)
            self.db.commit()
            return results
        except Exception as exc:
            self.db.finish_run(run_id, RUN_FAILED, str(exc))
            self.db.commit()
            raise

    def export_csv(self, results: list[InvoiceResult], destination: str) -> None:
        write_results_csv(results, destination)
