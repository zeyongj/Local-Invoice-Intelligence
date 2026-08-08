from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from invoice_models import HistoricalStats, InvoiceResult, LayoutProfile

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS invoices (
    file_path TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    vendor TEXT,
    account_number TEXT,
    bill_date TEXT,
    billing_period TEXT,
    current_charges REAL,
    total_amount_due REAL,
    meter_number TEXT,
    consumption REAL,
    consumption_unit TEXT,
    previous_balance REAL,
    payments REAL,
    confidence REAL,
    status TEXT,
    issues_json TEXT,
    warnings_json TEXT,
    evidence_json TEXT,
    logical_fingerprint TEXT,
    duplicate_of TEXT,
    page_count INTEGER,
    pages_processed INTEGER,
    processing_seconds REAL,
    error TEXT,
    processed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoice_vendor_account ON invoices(vendor, account_number);
CREATE INDEX IF NOT EXISTS idx_invoice_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoice_bill_date ON invoices(bill_date);
CREATE INDEX IF NOT EXISTS idx_invoice_fingerprint ON invoices(logical_fingerprint);

CREATE TABLE IF NOT EXISTS account_registry (
    vendor TEXT NOT NULL,
    account_number TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    invoice_count INTEGER NOT NULL DEFAULT 0,
    last_amount REAL,
    last_bill_date TEXT,
    known_meters_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(vendor, account_number)
);

CREATE TABLE IF NOT EXISTS layout_profiles (
    vendor TEXT NOT NULL,
    field_name TEXT NOT NULL,
    page_num INTEGER NOT NULL,
    mean_x REAL NOT NULL,
    mean_y REAL NOT NULL,
    mean_w REAL NOT NULL,
    mean_h REAL NOT NULL,
    samples INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(vendor, field_name)
);
"""


class InvoiceDatabase:
    def __init__(self, path: str):
        self.path = str(Path(path))
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL;")
        self.connection.execute("PRAGMA synchronous=NORMAL;")
        self.connection.execute("PRAGMA temp_store=MEMORY;")
        self.connection.executescript(SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self) -> None:
        self.connection.close()

    def commit(self) -> None:
        self.connection.commit()

    def unchanged(self, path: str, size: int, mtime_ns: int) -> bool:
        resolved = str(Path(path).resolve())
        row = self.connection.execute(
            "SELECT file_size, mtime_ns FROM invoices WHERE file_path = ?",
            (resolved,),
        ).fetchone()
        return bool(row and int(row["file_size"]) == int(size) and int(row["mtime_ns"]) == int(mtime_ns))

    def exists(self, path: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM invoices WHERE file_path = ?", (str(Path(path).resolve()),)
        ).fetchone()
        return row is not None

    def get_layout_profiles(self, vendor: str) -> dict[str, LayoutProfile]:
        rows = self.connection.execute(
            "SELECT * FROM layout_profiles WHERE vendor = ?", (vendor,)
        ).fetchall()
        return {
            row["field_name"]: LayoutProfile(
                vendor=row["vendor"],
                field_name=row["field_name"],
                page_num=int(row["page_num"]),
                mean_x=float(row["mean_x"]),
                mean_y=float(row["mean_y"]),
                mean_w=float(row["mean_w"]),
                mean_h=float(row["mean_h"]),
                samples=int(row["samples"]),
            )
            for row in rows
        }

    def update_layout_profiles(self, result: InvoiceResult) -> None:
        if result.vendor == "UNKNOWN" or result.confidence < 0.75:
            return
        now = datetime.now().isoformat(timespec="seconds")
        for field_name, evidence in result.evidence.items():
            if field_name == "consumption_unit":
                continue
            if float(evidence.get("confidence") or 0.0) < 0.88:
                continue
            nx = evidence.get("norm_x")
            ny = evidence.get("norm_y")
            nw = evidence.get("norm_w")
            nh = evidence.get("norm_h")
            page = evidence.get("page")
            if None in (nx, ny, nw, nh, page):
                continue
            row = self.connection.execute(
                "SELECT * FROM layout_profiles WHERE vendor = ? AND field_name = ?",
                (result.vendor, field_name),
            ).fetchone()
            if row is None:
                self.connection.execute(
                    """
                    INSERT INTO layout_profiles
                    (vendor, field_name, page_num, mean_x, mean_y, mean_w, mean_h, samples, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (result.vendor, field_name, int(page), float(nx), float(ny), float(nw), float(nh), now),
                )
                continue
            old_n = int(row["samples"])
            new_n = old_n + 1
            def online_mean(old: float, value: float) -> float:
                return old + (value - old) / new_n
            # A stable vendor invoice usually keeps a field on the same page. If not,
            # slowly move the page prior to the most recent page by using rounded online mean.
            page_mean = round((int(row["page_num"]) * old_n + int(page)) / new_n)
            self.connection.execute(
                """
                UPDATE layout_profiles
                SET page_num = ?, mean_x = ?, mean_y = ?, mean_w = ?, mean_h = ?, samples = ?, updated_at = ?
                WHERE vendor = ? AND field_name = ?
                """,
                (
                    page_mean,
                    online_mean(float(row["mean_x"]), float(nx)),
                    online_mean(float(row["mean_y"]), float(ny)),
                    online_mean(float(row["mean_w"]), float(nw)),
                    online_mean(float(row["mean_h"]), float(nh)),
                    new_n,
                    now,
                    result.vendor,
                    field_name,
                ),
            )

    def get_history(self, vendor: str, account_number: str, exclude_path: Optional[str] = None, limit: int = 24) -> HistoricalStats:
        params: list[object] = [vendor, account_number]
        exclude_sql = ""
        if exclude_path:
            exclude_sql = " AND file_path <> ?"
            params.append(str(Path(exclude_path).resolve()))
        params.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT total_amount_due, meter_number, bill_date
            FROM invoices
            WHERE vendor = ? AND account_number = ?
              AND total_amount_due IS NOT NULL
              AND status NOT IN ('FAILED', 'DUPLICATE')
              {exclude_sql}
            ORDER BY COALESCE(bill_date, processed_at) DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        amounts = [float(row["total_amount_due"]) for row in rows if row["total_amount_due"] is not None]
        meters = {str(row["meter_number"]) for row in rows if row["meter_number"]}
        registry = self.connection.execute(
            "SELECT * FROM account_registry WHERE vendor = ? AND account_number = ?",
            (vendor, account_number),
        ).fetchone()
        return HistoricalStats(
            vendor=vendor,
            account_number=account_number,
            invoice_count=int(registry["invoice_count"]) if registry else len(rows),
            amounts=amounts,
            known_meters=meters,
            last_amount=float(registry["last_amount"]) if registry and registry["last_amount"] is not None else (amounts[0] if amounts else None),
            last_bill_date=registry["last_bill_date"] if registry else (rows[0]["bill_date"] if rows else None),
        )

    def find_duplicate(self, fingerprint: str, exclude_path: str) -> Optional[str]:
        row = self.connection.execute(
            """
            SELECT file_path FROM invoices
            WHERE logical_fingerprint = ? AND file_path <> ? AND status <> 'FAILED'
            ORDER BY processed_at ASC LIMIT 1
            """,
            (fingerprint, str(Path(exclude_path).resolve())),
        ).fetchone()
        return str(row["file_path"]) if row else None

    def update_account_registry(self, result: InvoiceResult, is_new_file: bool) -> None:
        if result.vendor == "UNKNOWN" or not result.account_number:
            return
        now = datetime.now().isoformat(timespec="seconds")
        row = self.connection.execute(
            "SELECT * FROM account_registry WHERE vendor = ? AND account_number = ?",
            (result.vendor, result.account_number),
        ).fetchone()
        meter = result.meter_number
        if row is None:
            meters = [meter] if meter else []
            self.connection.execute(
                """
                INSERT INTO account_registry
                (vendor, account_number, first_seen, last_seen, invoice_count, last_amount, last_bill_date, known_meters_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.vendor,
                    result.account_number,
                    now,
                    now,
                    1 if is_new_file else 0,
                    result.total_amount_due,
                    result.bill_date,
                    json.dumps(meters),
                ),
            )
            return
        try:
            meters = set(json.loads(row["known_meters_json"] or "[]"))
        except Exception:
            meters = set()
        if meter:
            meters.add(meter)
        count = int(row["invoice_count"]) + (1 if is_new_file else 0)
        self.connection.execute(
            """
            UPDATE account_registry
            SET last_seen = ?, invoice_count = ?, last_amount = ?, last_bill_date = ?, known_meters_json = ?
            WHERE vendor = ? AND account_number = ?
            """,
            (
                now,
                count,
                result.total_amount_due if result.total_amount_due is not None else row["last_amount"],
                result.bill_date or row["last_bill_date"],
                json.dumps(sorted(meters)),
                result.vendor,
                result.account_number,
            ),
        )

    def save(self, result: InvoiceResult) -> None:
        self.connection.execute(
            """
            INSERT INTO invoices (
                file_path, file_name, file_size, mtime_ns, vendor, account_number,
                bill_date, billing_period, current_charges, total_amount_due,
                meter_number, consumption, consumption_unit, previous_balance, payments,
                confidence, status, issues_json, warnings_json, evidence_json,
                logical_fingerprint, duplicate_of, page_count, pages_processed,
                processing_seconds, error, processed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(file_path) DO UPDATE SET
                file_name=excluded.file_name, file_size=excluded.file_size, mtime_ns=excluded.mtime_ns,
                vendor=excluded.vendor, account_number=excluded.account_number, bill_date=excluded.bill_date,
                billing_period=excluded.billing_period, current_charges=excluded.current_charges,
                total_amount_due=excluded.total_amount_due, meter_number=excluded.meter_number,
                consumption=excluded.consumption, consumption_unit=excluded.consumption_unit,
                previous_balance=excluded.previous_balance, payments=excluded.payments,
                confidence=excluded.confidence, status=excluded.status, issues_json=excluded.issues_json,
                warnings_json=excluded.warnings_json, evidence_json=excluded.evidence_json,
                logical_fingerprint=excluded.logical_fingerprint, duplicate_of=excluded.duplicate_of,
                page_count=excluded.page_count, pages_processed=excluded.pages_processed,
                processing_seconds=excluded.processing_seconds, error=excluded.error,
                processed_at=excluded.processed_at
            """,
            (
                result.file_path, result.file_name, result.file_size, result.mtime_ns,
                result.vendor, result.account_number, result.bill_date, result.billing_period,
                result.current_charges, result.total_amount_due, result.meter_number,
                result.consumption, result.consumption_unit, result.previous_balance,
                result.payments, result.confidence, result.status,
                json.dumps(result.issues, ensure_ascii=False),
                json.dumps(result.warnings, ensure_ascii=False),
                json.dumps(result.evidence, ensure_ascii=False),
                result.logical_fingerprint, result.duplicate_of, result.page_count,
                result.pages_processed, result.processing_seconds, result.error,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )

    def rows_for_paths(self, paths: list[str]) -> list[sqlite3.Row]:
        if not paths:
            return []
        resolved = [str(Path(path).resolve()) for path in paths]
        placeholders = ",".join("?" for _ in resolved)
        return self.connection.execute(
            f"SELECT * FROM invoices WHERE file_path IN ({placeholders}) ORDER BY file_name",
            tuple(resolved),
        ).fetchall()
