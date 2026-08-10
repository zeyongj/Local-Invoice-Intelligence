from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from invoice_models import HistoricalStats, InvoiceResult, LayoutProfile
from invoice_normalization import canonical_account
from portfolio_intelligence import effective_unit_cost, load_property_csv, parse_billing_period
from version import APP_VERSION, SCHEMA_VERSION

BASE_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS invoices (
    file_path TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    vendor TEXT,
    account_number TEXT,
    account_number_raw TEXT,
    account_number_canonical TEXT,
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
    near_duplicate_of TEXT,
    near_duplicate_reason TEXT,
    document_id TEXT,
    run_id TEXT,
    parser_version TEXT,
    rule_version TEXT,
    app_version TEXT,
    page_count INTEGER,
    pages_processed INTEGER,
    processing_seconds REAL,
    error TEXT,
    processed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoice_vendor_account_canonical ON invoices(vendor, account_number_canonical);
CREATE INDEX IF NOT EXISTS idx_invoice_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoice_bill_date ON invoices(bill_date);
CREATE INDEX IF NOT EXISTS idx_invoice_fingerprint ON invoices(logical_fingerprint);
CREATE INDEX IF NOT EXISTS idx_invoice_document_id ON invoices(document_id);
CREATE INDEX IF NOT EXISTS idx_invoice_run_id ON invoices(run_id);

CREATE TABLE IF NOT EXISTS account_registry (
    vendor TEXT NOT NULL,
    account_number_canonical TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    invoice_count INTEGER NOT NULL DEFAULT 0,
    last_amount REAL,
    last_bill_date TEXT,
    known_meters_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(vendor, account_number_canonical)
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

CREATE TABLE IF NOT EXISTS processing_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    total_items INTEGER NOT NULL DEFAULT 0,
    completed_items INTEGER NOT NULL DEFAULT 0,
    source_label TEXT,
    output_path TEXT,
    app_version TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON processing_runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_started ON processing_runs(started_at);

CREATE TABLE IF NOT EXISTS run_items (
    run_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    error TEXT,
    PRIMARY KEY(run_id, file_path),
    FOREIGN KEY(run_id) REFERENCES processing_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_run_items_status ON run_items(run_id, status);

CREATE TABLE IF NOT EXISTS field_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    decision TEXT NOT NULL,
    extracted_json TEXT,
    final_json TEXT,
    actor TEXT,
    reason TEXT,
    reviewed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_field_review_doc ON field_reviews(document_id, reviewed_at);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time TEXT NOT NULL,
    event_type TEXT NOT NULL,
    run_id TEXT,
    document_id TEXT,
    file_path TEXT,
    field_name TEXT,
    old_value_json TEXT,
    new_value_json TEXT,
    actor TEXT,
    reason TEXT,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_document ON audit_events(document_id, event_time);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id, event_time);

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

V4_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS properties (
    project_id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    strata_plan TEXT,
    pm TEXT,
    source_project_raw TEXT,
    source_name_raw TEXT,
    source_file TEXT,
    imported_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_properties_name ON properties(project_name);

CREATE TABLE IF NOT EXISTS property_aliases (
    alias TEXT NOT NULL,
    project_id TEXT NOT NULL,
    alias_type TEXT NOT NULL DEFAULT 'PROJECT_CODE',
    source_note TEXT,
    PRIMARY KEY(alias, project_id),
    FOREIGN KEY(project_id) REFERENCES properties(project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_property_alias ON property_aliases(alias);

CREATE TABLE IF NOT EXISTS property_postal_codes (
    project_id TEXT NOT NULL,
    postal_code TEXT NOT NULL,
    PRIMARY KEY(project_id, postal_code),
    FOREIGN KEY(project_id) REFERENCES properties(project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS property_locations (
    location_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    raw_project TEXT,
    raw_name TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    UNIQUE(project_id, source_fingerprint),
    FOREIGN KEY(project_id) REFERENCES properties(project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_property_locations_project ON property_locations(project_id);

CREATE TABLE IF NOT EXISTS utility_accounts (
    utility_account_id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor TEXT NOT NULL,
    account_number_canonical TEXT NOT NULL,
    account_number_display TEXT,
    property_id TEXT NOT NULL,
    utility_type TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    notes TEXT,
    UNIQUE(vendor, account_number_canonical),
    FOREIGN KEY(property_id) REFERENCES properties(project_id)
);
CREATE INDEX IF NOT EXISTS idx_utility_property ON utility_accounts(property_id, vendor);

CREATE TABLE IF NOT EXISTS utility_meters (
    meter_id INTEGER PRIMARY KEY AUTOINCREMENT,
    utility_account_id INTEGER NOT NULL,
    meter_number_canonical TEXT NOT NULL,
    meter_number_display TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    active_from TEXT,
    active_to TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE(utility_account_id, meter_number_canonical),
    FOREIGN KEY(utility_account_id) REFERENCES utility_accounts(utility_account_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_meter_number ON utility_meters(meter_number_canonical);

CREATE TABLE IF NOT EXISTS invoice_relationships (
    relationship_id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_type TEXT NOT NULL,
    from_document_id TEXT NOT NULL,
    to_document_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    details_json TEXT,
    UNIQUE(relation_type, from_document_id, to_document_id)
);
CREATE INDEX IF NOT EXISTS idx_invoice_rel_from ON invoice_relationships(from_document_id);
CREATE INDEX IF NOT EXISTS idx_invoice_rel_to ON invoice_relationships(to_document_id);

CREATE TABLE IF NOT EXISTS invoice_anomalies (
    anomaly_id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    anomaly_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    score REAL,
    message TEXT NOT NULL,
    details_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(document_id, anomaly_type, message)
);
CREATE INDEX IF NOT EXISTS idx_anomaly_document ON invoice_anomalies(document_id);
CREATE INDEX IF NOT EXISTS idx_anomaly_type ON invoice_anomalies(anomaly_type, severity);
"""

INVOICE_COLUMNS: dict[str, str] = {
    "account_number_raw": "TEXT",
    "account_number_canonical": "TEXT",
    "near_duplicate_of": "TEXT",
    "near_duplicate_reason": "TEXT",
    "document_id": "TEXT",
    "run_id": "TEXT",
    "parser_version": "TEXT",
    "rule_version": "TEXT",
    "app_version": "TEXT",
    "property_id": "TEXT",
    "property_name": "TEXT",
    "suggested_property_id": "TEXT",
    "billing_period_start": "TEXT",
    "billing_period_end": "TEXT",
    "effective_unit_cost": "REAL",
    "portfolio_flags_json": "TEXT",
    "revision_of_document_id": "TEXT",
    "revision_group_id": "TEXT",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class InvoiceDatabase:
    def __init__(self, path: str, create_backup: bool = True):
        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        existed = Path(self.path).exists() and Path(self.path).stat().st_size > 0
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL;")
        self.connection.execute("PRAGMA synchronous=NORMAL;")
        self.connection.execute("PRAGMA temp_store=MEMORY;")
        self.connection.execute("PRAGMA foreign_keys=ON;")
        if existed and create_backup and self._schema_needs_upgrade():
            self.backup(reason="pre_v4_migration")
        self._migrate()
        ok, message = self.integrity_check(quick=True)
        if not ok:
            raise RuntimeError(f"SQLite integrity check failed: {message}")
        self._mark_stale_runs_interrupted()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    def commit(self) -> None:
        self.connection.commit()

    def _schema_needs_upgrade(self) -> bool:
        try:
            version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
            return version < SCHEMA_VERSION
        except Exception:
            return True

    def _table_exists(self, name: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _column_names(self, table: str) -> set[str]:
        if not self._table_exists(table):
            return set()
        return {str(row[1]) for row in self.connection.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        # Legacy v2 databases do not have account_number_canonical. Add new
        # invoice columns before creating indexes that reference them.
        if self._table_exists("invoices"):
            invoice_columns = self._column_names("invoices")
            for column, declaration in INVOICE_COLUMNS.items():
                if column not in invoice_columns:
                    self.connection.execute(f"ALTER TABLE invoices ADD COLUMN {column} {declaration}")
        else:
            self.connection.executescript(BASE_SCHEMA)

        # Fresh databases are created from the compatible base schema above;
        # ensure all v4 invoice columns exist before any v4 queries run.
        invoice_columns = self._column_names("invoices")
        for column, declaration in INVOICE_COLUMNS.items():
            if column not in invoice_columns:
                self.connection.execute(f"ALTER TABLE invoices ADD COLUMN {column} {declaration}")

        # v2 account_registry used (vendor, account_number) as the primary key.
        registry_columns = self._column_names("account_registry")
        if registry_columns and "account_number_canonical" not in registry_columns:
            self.connection.execute("DROP TABLE account_registry")
            self.connection.execute(
                """
                CREATE TABLE account_registry (
                    vendor TEXT NOT NULL,
                    account_number_canonical TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    invoice_count INTEGER NOT NULL DEFAULT 0,
                    last_amount REAL,
                    last_bill_date TEXT,
                    known_meters_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(vendor, account_number_canonical)
                )
                """
            )

        # Backfill canonical account identities and stable document ids.
        rows = self.connection.execute(
            "SELECT file_path, vendor, account_number, account_number_raw, account_number_canonical, document_id FROM invoices"
        ).fetchall()
        for row in rows:
            raw = row["account_number_raw"] or row["account_number"]
            canonical = row["account_number_canonical"] or canonical_account(row["vendor"] or "UNKNOWN", raw or "")
            doc_id = row["document_id"] or self._legacy_document_id(row["file_path"])
            self.connection.execute(
                """
                UPDATE invoices
                SET account_number_raw = COALESCE(account_number_raw, ?),
                    account_number_canonical = COALESCE(account_number_canonical, ?),
                    account_number = COALESCE(account_number, ?),
                    document_id = COALESCE(document_id, ?),
                    app_version = COALESCE(app_version, ?)
                WHERE file_path = ?
                """,
                (raw, canonical, canonical, doc_id, APP_VERSION, row["file_path"]),
            )

        self.connection.executescript(BASE_SCHEMA)
        self.connection.executescript(V4_SCHEMA)
        self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.connection.execute(
            "INSERT OR REPLACE INTO app_meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
        self.connection.execute(
            "INSERT OR REPLACE INTO app_meta(key, value) VALUES('last_opened_with', ?)", (APP_VERSION,)
        )
        self.rebuild_account_registry()
        self.connection.commit()

    @staticmethod
    def _legacy_document_id(file_path: str) -> str:
        return "DOC-" + hashlib.sha256(str(file_path).encode("utf-8")).hexdigest()[:24]

    def integrity_check(self, quick: bool = True) -> tuple[bool, str]:
        pragma = "quick_check" if quick else "integrity_check"
        row = self.connection.execute(f"PRAGMA {pragma}").fetchone()
        message = str(row[0]) if row else "unknown"
        return message.lower() == "ok", message

    def backup(self, reason: str = "manual", keep: int = 10) -> Optional[str]:
        source = Path(self.path)
        if not source.exists() or source.stat().st_size == 0:
            return None
        backup_dir = source.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = backup_dir / f"{source.stem}_{reason}_{stamp}{source.suffix}"
        target = sqlite3.connect(destination)
        try:
            self.connection.backup(target)
        finally:
            target.close()
        backups = sorted(backup_dir.glob(f"{source.stem}_*{source.suffix}"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
        return str(destination)

    # ---------------------------- runs ----------------------------
    def create_run(self, paths: list[str | Path], source_label: str = "", output_path: str = "") -> str:
        run_id = "RUN-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        now = _now()
        resolved = [str(Path(path).resolve()) for path in paths]
        self.connection.execute(
            """INSERT INTO processing_runs
            (run_id, started_at, status, total_items, completed_items, source_label, output_path, app_version)
            VALUES (?, ?, 'RUNNING', ?, 0, ?, ?, ?)""",
            (run_id, now, len(resolved), source_label, output_path, APP_VERSION),
        )
        self.connection.executemany(
            "INSERT OR REPLACE INTO run_items(run_id, file_path, status) VALUES (?, ?, 'QUEUED')",
            [(run_id, path) for path in resolved],
        )
        self.audit("RUN_STARTED", run_id=run_id, details={"total_items": len(resolved), "source": source_label})
        self.connection.commit()
        return run_id

    def _mark_stale_runs_interrupted(self) -> None:
        rows = self.connection.execute("SELECT run_id FROM processing_runs WHERE status='RUNNING'").fetchall()
        for row in rows:
            self.connection.execute(
                "UPDATE processing_runs SET status='INTERRUPTED', ended_at=? WHERE run_id=?", (_now(), row["run_id"])
            )
            self.connection.execute(
                "UPDATE run_items SET status='QUEUED', started_at=NULL WHERE run_id=? AND status='PROCESSING'", (row["run_id"],)
            )
        self.connection.commit()

    def prepare_resume(self, run_id: str) -> list[str]:
        self.connection.execute(
            "UPDATE processing_runs SET status='RUNNING', ended_at=NULL, error=NULL WHERE run_id=?", (run_id,)
        )
        rows = self.connection.execute(
            "SELECT file_path FROM run_items WHERE run_id=? AND status IN ('QUEUED','FAILED','CANCELLED') ORDER BY file_path",
            (run_id,),
        ).fetchall()
        self.audit("RUN_RESUMED", run_id=run_id, details={"remaining": len(rows)})
        self.connection.commit()
        return [str(row["file_path"]) for row in rows]

    def mark_run_item(self, run_id: str, file_path: str, status: str, error: Optional[str] = None) -> None:
        now = _now()
        if status == "PROCESSING":
            self.connection.execute(
                "UPDATE run_items SET status=?, started_at=?, ended_at=NULL, error=NULL WHERE run_id=? AND file_path=?",
                (status, now, run_id, str(Path(file_path).resolve())),
            )
        else:
            self.connection.execute(
                "UPDATE run_items SET status=?, ended_at=?, error=? WHERE run_id=? AND file_path=?",
                (status, now, error, run_id, str(Path(file_path).resolve())),
            )
            completed = self.connection.execute(
                "SELECT COUNT(*) FROM run_items WHERE run_id=? AND status IN ('COMPLETED','FAILED','CACHED')",
                (run_id,),
            ).fetchone()[0]
            self.connection.execute("UPDATE processing_runs SET completed_items=? WHERE run_id=?", (completed, run_id))

    def finish_run(self, run_id: str, status: str, error: Optional[str] = None) -> None:
        self.connection.execute(
            "UPDATE processing_runs SET status=?, ended_at=?, error=? WHERE run_id=?", (status, _now(), error, run_id)
        )
        self.audit("RUN_FINISHED", run_id=run_id, details={"status": status, "error": error})
        self.connection.commit()

    def recent_runs(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM processing_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def get_run(self, run_id: str) -> Optional[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM processing_runs WHERE run_id=?", (run_id,)).fetchone()

    # ---------------------------- invoice cache / profiles ----------------------------
    def unchanged(self, path: str, size: int, mtime_ns: int) -> bool:
        row = self.connection.execute(
            "SELECT file_size, mtime_ns FROM invoices WHERE file_path=?", (str(Path(path).resolve()),)
        ).fetchone()
        return bool(row and int(row["file_size"]) == int(size) and int(row["mtime_ns"]) == int(mtime_ns))

    def get_document_id(self, path: str) -> Optional[str]:
        row = self.connection.execute(
            "SELECT document_id FROM invoices WHERE file_path=?", (str(Path(path).resolve()),)
        ).fetchone()
        return str(row["document_id"]) if row and row["document_id"] else None

    def get_layout_profiles(self, vendor: str) -> dict[str, LayoutProfile]:
        rows = self.connection.execute("SELECT * FROM layout_profiles WHERE vendor=?", (vendor,)).fetchall()
        return {
            row["field_name"]: LayoutProfile(
                vendor=row["vendor"], field_name=row["field_name"], page_num=int(row["page_num"]),
                mean_x=float(row["mean_x"]), mean_y=float(row["mean_y"]), mean_w=float(row["mean_w"]),
                mean_h=float(row["mean_h"]), samples=int(row["samples"]),
            ) for row in rows
        }

    def update_layout_profiles(self, result: InvoiceResult) -> None:
        if result.vendor == "UNKNOWN" or result.confidence < 0.75 or result.status in {"DUPLICATE", "FAILED"}:
            return
        now = _now()
        for field_name, evidence in result.evidence.items():
            if field_name == "consumption_unit" or float(evidence.get("confidence") or 0) < 0.88:
                continue
            nx, ny, nw, nh, page = (evidence.get(k) for k in ("norm_x", "norm_y", "norm_w", "norm_h", "page"))
            if None in (nx, ny, nw, nh, page):
                continue
            row = self.connection.execute(
                "SELECT * FROM layout_profiles WHERE vendor=? AND field_name=?", (result.vendor, field_name)
            ).fetchone()
            if not row:
                self.connection.execute(
                    """INSERT INTO layout_profiles
                    (vendor,field_name,page_num,mean_x,mean_y,mean_w,mean_h,samples,updated_at)
                    VALUES(?,?,?,?,?,?,?,1,?)""",
                    (result.vendor, field_name, int(page), float(nx), float(ny), float(nw), float(nh), now),
                )
                continue
            old_n = int(row["samples"]); new_n = old_n + 1
            mean = lambda old, new: float(old) + (float(new) - float(old)) / new_n
            page_mean = round((int(row["page_num"]) * old_n + int(page)) / new_n)
            self.connection.execute(
                """UPDATE layout_profiles SET page_num=?,mean_x=?,mean_y=?,mean_w=?,mean_h=?,samples=?,updated_at=?
                   WHERE vendor=? AND field_name=?""",
                (page_mean, mean(row["mean_x"], nx), mean(row["mean_y"], ny), mean(row["mean_w"], nw),
                 mean(row["mean_h"], nh), new_n, now, result.vendor, field_name),
            )

    # ---------------------------- history / duplicates ----------------------------
    def get_history(self, vendor: str, account_number: str, bill_date: Optional[str] = None,
                    exclude_path: Optional[str] = None, limit: int = 36) -> HistoricalStats:
        params: list[Any] = [vendor, account_number]
        exclude_sql = ""
        if exclude_path:
            exclude_sql = " AND file_path <> ?"
            params.append(str(Path(exclude_path).resolve()))
        params.append(limit)
        rows = self.connection.execute(
            f"""SELECT total_amount_due,current_charges,consumption,effective_unit_cost,meter_number,bill_date
            FROM invoices
            WHERE vendor=? AND account_number_canonical=? AND status='OK' AND revision_of_document_id IS NULL {exclude_sql}
            ORDER BY COALESCE(bill_date,processed_at) DESC LIMIT ?""", tuple(params)
        ).fetchall()
        amounts = [float(r["total_amount_due"]) for r in rows if r["total_amount_due"] is not None]
        consumptions = [float(r["consumption"]) for r in rows if r["consumption"] is not None and float(r["consumption"]) > 0]
        unit_costs = [float(r["effective_unit_cost"]) for r in rows if r["effective_unit_cost"] is not None]
        meters = {str(r["meter_number"]) for r in rows if r["meter_number"]}
        seasonal_amounts: list[float] = []; seasonal_consumptions: list[float] = []; seasonal_unit_costs: list[float] = []
        if bill_date and len(bill_date) >= 7:
            month = bill_date[5:7]
            for r in rows:
                if not r["bill_date"] or r["bill_date"][5:7] != month:
                    continue
                if r["total_amount_due"] is not None: seasonal_amounts.append(float(r["total_amount_due"]))
                if r["consumption"] is not None and float(r["consumption"]) > 0: seasonal_consumptions.append(float(r["consumption"]))
                if r["effective_unit_cost"] is not None: seasonal_unit_costs.append(float(r["effective_unit_cost"]))
        registry = self.connection.execute(
            "SELECT * FROM account_registry WHERE vendor=? AND account_number_canonical=?", (vendor, account_number)
        ).fetchone()
        first = rows[0] if rows else None
        return HistoricalStats(
            vendor=vendor, account_number=account_number,
            invoice_count=int(registry["invoice_count"]) if registry else len(rows),
            amounts=amounts, seasonal_amounts=seasonal_amounts,
            consumptions=consumptions, seasonal_consumptions=seasonal_consumptions,
            unit_costs=unit_costs, seasonal_unit_costs=seasonal_unit_costs, known_meters=meters,
            last_amount=float(registry["last_amount"]) if registry and registry["last_amount"] is not None else (float(first["total_amount_due"]) if first and first["total_amount_due"] is not None else None),
            last_consumption=float(first["consumption"]) if first and first["consumption"] is not None else None,
            last_unit_cost=float(first["effective_unit_cost"]) if first and first["effective_unit_cost"] is not None else None,
            last_bill_date=registry["last_bill_date"] if registry else (first["bill_date"] if first else None),
        )

    def find_duplicate(self, fingerprint: str, exclude_path: str) -> Optional[str]:
        row = self.connection.execute(
            """SELECT file_path FROM invoices WHERE logical_fingerprint=? AND file_path<>? AND status<>'FAILED'
               ORDER BY processed_at ASC LIMIT 1""",
            (fingerprint, str(Path(exclude_path).resolve())),
        ).fetchone()
        return str(row["file_path"]) if row else None

    def find_near_duplicate(self, vendor: str, account: str, period: str, amount: float, exclude_path: str) -> Optional[tuple[str, float]]:
        if not period:
            return None
        if len(period) == 10 and period[4] == "-":
            clause, period_value = "bill_date=?", period
        else:
            clause, period_value = "billing_period=?", period
        row = self.connection.execute(
            f"""SELECT file_path,total_amount_due FROM invoices
                WHERE vendor=? AND account_number_canonical=? AND {clause}
                  AND file_path<>? AND total_amount_due IS NOT NULL AND status<>'FAILED'
                ORDER BY processed_at DESC LIMIT 1""",
            (vendor, account, period_value, str(Path(exclude_path).resolve())),
        ).fetchone()
        if row and abs(float(row["total_amount_due"]) - float(amount)) > 0.009:
            return str(row["file_path"]), float(row["total_amount_due"])
        return None

    def rebuild_account_registry(self) -> None:
        self.connection.execute("DELETE FROM account_registry")
        rows = self.connection.execute(
            """SELECT vendor,account_number_canonical,total_amount_due,bill_date,meter_number,processed_at
               FROM invoices WHERE vendor IS NOT NULL AND account_number_canonical IS NOT NULL
                 AND status='OK' AND revision_of_document_id IS NULL
               ORDER BY COALESCE(bill_date,processed_at) ASC"""
        ).fetchall()
        grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault((row["vendor"], row["account_number_canonical"]), []).append(row)
        for (vendor, account), items in grouped.items():
            meters = sorted({str(r["meter_number"]) for r in items if r["meter_number"]})
            last = items[-1]
            self.connection.execute(
                """INSERT INTO account_registry
                (vendor,account_number_canonical,first_seen,last_seen,invoice_count,last_amount,last_bill_date,known_meters_json)
                VALUES(?,?,?,?,?,?,?,?)""",
                (vendor, account, items[0]["processed_at"], last["processed_at"], len(items), last["total_amount_due"],
                 last["bill_date"], _json(meters)),
            )

    def update_account_registry(self, result: InvoiceResult, is_new_file: bool) -> None:
        if result.vendor == "UNKNOWN" or not result.account_number or result.status != "OK":
            return
        now = _now()
        row = self.connection.execute(
            "SELECT * FROM account_registry WHERE vendor=? AND account_number_canonical=?",
            (result.vendor, result.account_number),
        ).fetchone()
        meter = result.meter_number
        if row is None:
            meters = [meter] if meter else []
            self.connection.execute(
                """INSERT INTO account_registry
                (vendor,account_number_canonical,first_seen,last_seen,invoice_count,last_amount,last_bill_date,known_meters_json)
                VALUES(?,?,?,?,?,?,?,?)""",
                (result.vendor, result.account_number, now, now, 1 if is_new_file else 0,
                 result.total_amount_due, result.bill_date, _json(meters)),
            )
            return
        meters = set(json.loads(row["known_meters_json"] or "[]"))
        if meter:
            meters.add(meter)
        count = int(row["invoice_count"]) + (1 if is_new_file else 0)
        self.connection.execute(
            """UPDATE account_registry SET last_seen=?,invoice_count=?,last_amount=?,last_bill_date=?,known_meters_json=?
               WHERE vendor=? AND account_number_canonical=?""",
            (now, count, result.total_amount_due if result.total_amount_due is not None else row["last_amount"],
             result.bill_date or row["last_bill_date"], _json(sorted(meters)), result.vendor, result.account_number),
        )

    def cancel_remaining_run_items(self, run_id: str) -> None:
        now = _now()
        self.connection.execute(
            "UPDATE run_items SET status='CANCELLED',ended_at=? WHERE run_id=? AND status IN ('QUEUED','PROCESSING')",
            (now, run_id),
        )
        completed = self.connection.execute(
            "SELECT COUNT(*) FROM run_items WHERE run_id=? AND status IN ('COMPLETED','FAILED','CACHED')",
            (run_id,),
        ).fetchone()[0]
        self.connection.execute("UPDATE processing_runs SET completed_items=? WHERE run_id=?", (completed, run_id))

    # ---------------------------- persistence ----------------------------
    def save(self, result: InvoiceResult) -> None:
        result.processed_at = result.processed_at or _now()
        self.connection.execute(
            """
            INSERT INTO invoices (
                file_path,file_name,file_size,mtime_ns,vendor,account_number,account_number_raw,account_number_canonical,
                bill_date,billing_period,current_charges,total_amount_due,meter_number,consumption,consumption_unit,
                previous_balance,payments,confidence,status,issues_json,warnings_json,evidence_json,logical_fingerprint,
                duplicate_of,near_duplicate_of,near_duplicate_reason,document_id,run_id,parser_version,rule_version,
                app_version,page_count,pages_processed,processing_seconds,error,processed_at,property_id,property_name,
                suggested_property_id,billing_period_start,billing_period_end,effective_unit_cost,portfolio_flags_json,
                revision_of_document_id,revision_group_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(file_path) DO UPDATE SET
                file_name=excluded.file_name,file_size=excluded.file_size,mtime_ns=excluded.mtime_ns,vendor=excluded.vendor,
                account_number=excluded.account_number,account_number_raw=excluded.account_number_raw,
                account_number_canonical=excluded.account_number_canonical,bill_date=excluded.bill_date,
                billing_period=excluded.billing_period,current_charges=excluded.current_charges,total_amount_due=excluded.total_amount_due,
                meter_number=excluded.meter_number,consumption=excluded.consumption,consumption_unit=excluded.consumption_unit,
                previous_balance=excluded.previous_balance,payments=excluded.payments,confidence=excluded.confidence,status=excluded.status,
                issues_json=excluded.issues_json,warnings_json=excluded.warnings_json,evidence_json=excluded.evidence_json,
                logical_fingerprint=excluded.logical_fingerprint,duplicate_of=excluded.duplicate_of,
                near_duplicate_of=excluded.near_duplicate_of,near_duplicate_reason=excluded.near_duplicate_reason,
                document_id=excluded.document_id,run_id=excluded.run_id,parser_version=excluded.parser_version,
                rule_version=excluded.rule_version,app_version=excluded.app_version,page_count=excluded.page_count,
                pages_processed=excluded.pages_processed,processing_seconds=excluded.processing_seconds,error=excluded.error,
                processed_at=excluded.processed_at,property_id=excluded.property_id,property_name=excluded.property_name,
                suggested_property_id=excluded.suggested_property_id,billing_period_start=excluded.billing_period_start,
                billing_period_end=excluded.billing_period_end,effective_unit_cost=excluded.effective_unit_cost,
                portfolio_flags_json=excluded.portfolio_flags_json,revision_of_document_id=excluded.revision_of_document_id,
                revision_group_id=excluded.revision_group_id
            """,
            (result.file_path,result.file_name,result.file_size,result.mtime_ns,result.vendor,result.account_number,
             result.account_number_raw,result.account_number,result.bill_date,result.billing_period,result.current_charges,
             result.total_amount_due,result.meter_number,result.consumption,result.consumption_unit,result.previous_balance,
             result.payments,result.confidence,result.status,_json(result.issues),_json(result.warnings),_json(result.evidence),
             result.logical_fingerprint,result.duplicate_of,result.near_duplicate_of,result.near_duplicate_reason,result.document_id,
             result.run_id,result.parser_version,result.rule_version,result.app_version,result.page_count,result.pages_processed,
             result.processing_seconds,result.error,result.processed_at,result.property_id,result.property_name,
             result.suggested_property_id,result.billing_period_start,result.billing_period_end,result.effective_unit_cost,
             _json(result.portfolio_flags),result.revision_of_document_id,result.revision_group_id),
        )

    def load_result(self, path: str) -> Optional[InvoiceResult]:
        row = self.connection.execute("SELECT * FROM invoices WHERE file_path=?", (str(Path(path).resolve()),)).fetchone()
        return self.row_to_result(row) if row else None

    def get_result_by_document(self, document_id: str) -> Optional[InvoiceResult]:
        row = self.connection.execute("SELECT * FROM invoices WHERE document_id=?", (document_id,)).fetchone()
        return self.row_to_result(row) if row else None

    def review_results(self, limit: int = 1000) -> list[InvoiceResult]:
        rows = self.connection.execute(
            "SELECT * FROM invoices WHERE status IN ('REVIEW_REQUIRED','FAILED') ORDER BY processed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self.row_to_result(row) for row in rows]

    def all_results(self, limit: int = 100000) -> list[InvoiceResult]:
        rows = self.connection.execute(
            "SELECT * FROM invoices ORDER BY processed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self.row_to_result(row) for row in rows]

    def row_to_result(self, row: sqlite3.Row) -> InvoiceResult:
        return InvoiceResult(
            file_name=row["file_name"], file_path=row["file_path"], vendor=row["vendor"] or "UNKNOWN",
            account_number=row["account_number_canonical"] or row["account_number"], account_number_raw=row["account_number_raw"],
            bill_date=row["bill_date"], billing_period=row["billing_period"], current_charges=row["current_charges"],
            total_amount_due=row["total_amount_due"], meter_number=row["meter_number"], consumption=row["consumption"],
            consumption_unit=row["consumption_unit"], previous_balance=row["previous_balance"], payments=row["payments"],
            confidence=float(row["confidence"] or 0), status=row["status"] or "REVIEW_REQUIRED",
            issues=json.loads(row["issues_json"] or "[]"), warnings=json.loads(row["warnings_json"] or "[]"),
            evidence=json.loads(row["evidence_json"] or "{}"), page_count=int(row["page_count"] or 0),
            pages_processed=int(row["pages_processed"] or 0), file_size=int(row["file_size"] or 0), mtime_ns=int(row["mtime_ns"] or 0),
            processing_seconds=float(row["processing_seconds"] or 0), error=row["error"], logical_fingerprint=row["logical_fingerprint"],
            duplicate_of=row["duplicate_of"], near_duplicate_of=row["near_duplicate_of"], near_duplicate_reason=row["near_duplicate_reason"],
            document_id=row["document_id"], run_id=row["run_id"], parser_version=row["parser_version"] or "",
            rule_version=row["rule_version"] or "", app_version=row["app_version"] or "", processed_at=row["processed_at"],
            property_id=row["property_id"], property_name=row["property_name"], suggested_property_id=row["suggested_property_id"],
            billing_period_start=row["billing_period_start"], billing_period_end=row["billing_period_end"],
            effective_unit_cost=float(row["effective_unit_cost"]) if row["effective_unit_cost"] is not None else None,
            portfolio_flags=json.loads(row["portfolio_flags_json"] or "[]"),
            revision_of_document_id=row["revision_of_document_id"], revision_group_id=row["revision_group_id"],
        )

    # ---------------------------- v4 portfolio master / utility controls ----------------------------
    def import_property_master(self, csv_path: str | Path, replace: bool = False) -> dict[str, int]:
        seeds = load_property_csv(csv_path)
        now = _now()
        # Rebuild source-derived aliases/locations/postal codes deterministically.
        # Canonical properties and utility mappings remain stable across imports.
        self.connection.execute("DELETE FROM property_locations")
        self.connection.execute("DELETE FROM property_postal_codes")
        self.connection.execute("DELETE FROM property_aliases")
        self.connection.execute("UPDATE properties SET active=0")
        source_rows = aliases = postals = locations = 0
        canonical_seen: set[str] = set()
        for seed in seeds:
            if seed.project_id not in canonical_seen:
                self.connection.execute(
                    """INSERT INTO properties(project_id,project_name,strata_plan,pm,source_project_raw,source_name_raw,source_file,imported_at,active)
                       VALUES(?,?,?,?,?,?,?,?,1)
                       ON CONFLICT(project_id) DO UPDATE SET project_name=excluded.project_name,strata_plan=excluded.strata_plan,
                       pm=excluded.pm,source_project_raw=excluded.source_project_raw,source_name_raw=excluded.source_name_raw,
                       source_file=excluded.source_file,imported_at=excluded.imported_at,active=1""",
                    (seed.project_id, seed.project_name, seed.strata_plan, seed.pm, seed.raw_project, seed.raw_project_name,
                     str(Path(csv_path).resolve()), now),
                )
                canonical_seen.add(seed.project_id)
            else:
                # Same internal project can legitimately have multiple source
                # rows/addresses. Keep one canonical property and all locations.
                self.connection.execute("UPDATE properties SET active=1,imported_at=? WHERE project_id=?", (now, seed.project_id))
            source_rows += 1
            for alias in seed.aliases:
                self.connection.execute(
                    "INSERT OR IGNORE INTO property_aliases(alias,project_id,alias_type,source_note) VALUES(?,?,?,?)",
                    (alias.upper(), seed.project_id, "PROJECT_CODE", "Imported from PROJ # source cell"),
                )
                aliases += 1
            for postal in seed.postal_codes:
                self.connection.execute(
                    "INSERT OR IGNORE INTO property_postal_codes(project_id,postal_code) VALUES(?,?)",
                    (seed.project_id, postal),
                )
                postals += 1
            fingerprint = hashlib.sha256((seed.raw_project + "\n" + seed.raw_project_name).encode("utf-8")).hexdigest()
            self.connection.execute(
                """INSERT OR IGNORE INTO property_locations(project_id,display_name,raw_project,raw_name,source_fingerprint)
                   VALUES(?,?,?,?,?)""",
                (seed.project_id, seed.project_name, seed.raw_project, seed.raw_project_name, fingerprint),
            )
            locations += 1
        self.audit("PORTFOLIO_MASTER_IMPORTED", actor="Local user", details={
            "source": str(csv_path), "source_rows": source_rows, "canonical_properties": len(canonical_seen)
        })
        self.connection.commit()
        return {"source_rows": source_rows, "properties": len(canonical_seen), "aliases": aliases,
                "postal_codes": postals, "locations": locations}

    def has_portfolio(self) -> bool:
        return self.connection.execute("SELECT 1 FROM properties WHERE active=1 LIMIT 1").fetchone() is not None

    def portfolio_counts(self) -> dict[str, int]:
        return {
            "properties": int(self.connection.execute("SELECT COUNT(*) FROM properties WHERE active=1").fetchone()[0]),
            "utility_accounts": int(self.connection.execute("SELECT COUNT(*) FROM utility_accounts WHERE active=1").fetchone()[0]),
            "meters": int(self.connection.execute("SELECT COUNT(*) FROM utility_meters WHERE active=1").fetchone()[0]),
            "unmapped_invoices": int(self.connection.execute(
                "SELECT COUNT(*) FROM invoices WHERE account_number_canonical IS NOT NULL AND property_id IS NULL AND status<>'FAILED'"
            ).fetchone()[0]),
        }

    def list_properties(self, query: str = "", limit: int = 2000) -> list[sqlite3.Row]:
        if query.strip():
            q = f"%{query.strip()}%"
            return self.connection.execute(
                """SELECT DISTINCT p.* FROM properties p
                   LEFT JOIN property_aliases a ON a.project_id=p.project_id
                   LEFT JOIN property_locations l ON l.project_id=p.project_id
                   WHERE p.project_id LIKE ? OR p.project_name LIKE ? OR p.strata_plan LIKE ? OR a.alias LIKE ?
                      OR l.display_name LIKE ? OR l.raw_name LIKE ?
                   ORDER BY p.project_id LIMIT ?""", (q, q, q, q, q, q, limit)
            ).fetchall()
        return self.connection.execute("SELECT * FROM properties ORDER BY project_id LIMIT ?", (limit,)).fetchall()

    def get_property(self, project_id: str) -> Optional[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM properties WHERE project_id=?", (project_id,)).fetchone()

    def property_locations(self, project_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM property_locations WHERE project_id=? ORDER BY location_id", (project_id,)
        ).fetchall()

    def resolve_project_alias(self, alias: str) -> Optional[str]:
        token = (alias or "").strip().upper()
        row = self.connection.execute(
            "SELECT project_id FROM property_aliases WHERE alias=? ORDER BY project_id LIMIT 1", (token,)
        ).fetchone()
        return str(row["project_id"]) if row else None

    def suggest_property_from_path(self, file_path: str) -> Optional[str]:
        # Fast fallback: extract project-like tokens from folder/file names and
        # perform indexed alias lookups. This avoids scanning all portfolio aliases.
        tokens = re.findall(r"(?<![0-9A-Z])([0-9]{4}(?:-[0-9A-Z]+|[A-Z])?)(?![0-9A-Z])", str(file_path).upper())
        for token in reversed(tokens):  # filename/deepest folder usually most specific
            project = self.resolve_project_alias(token)
            if project:
                return project
        return None

    def list_utility_accounts(self, limit: int = 5000) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT ua.*,p.project_name,(SELECT COUNT(*) FROM utility_meters m WHERE m.utility_account_id=ua.utility_account_id AND m.active=1) AS meter_count
               FROM utility_accounts ua JOIN properties p ON p.project_id=ua.property_id
               ORDER BY ua.property_id,ua.vendor,ua.account_number_canonical LIMIT ?""", (limit,)
        ).fetchall()

    def get_utility_account(self, vendor: str, account: str) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            """SELECT ua.*,p.project_name FROM utility_accounts ua JOIN properties p ON p.project_id=ua.property_id
               WHERE ua.vendor=? AND ua.account_number_canonical=? AND ua.active=1""",
            (vendor, account),
        ).fetchone()

    def map_utility_account(self, vendor: str, account: str, property_id: str, display_account: str = "",
                            meter_number: Optional[str] = None, actor: str = "Local user", notes: str = "") -> int:
        prop = self.get_property(property_id)
        if not prop:
            raise KeyError(f"Unknown canonical project: {property_id}")
        canonical = canonical_account(vendor, account)
        if not canonical:
            raise ValueError("Invalid utility account identity.")
        now = _now()
        existing = self.connection.execute(
            "SELECT * FROM utility_accounts WHERE vendor=? AND account_number_canonical=?", (vendor, canonical)
        ).fetchone()
        old_property = existing["property_id"] if existing else None
        self.connection.execute(
            """INSERT INTO utility_accounts(vendor,account_number_canonical,account_number_display,property_id,utility_type,
               active,first_seen,last_seen,notes) VALUES(?,?,?,?,?,1,?,?,?)
               ON CONFLICT(vendor,account_number_canonical) DO UPDATE SET property_id=excluded.property_id,
               account_number_display=COALESCE(NULLIF(excluded.account_number_display,''),utility_accounts.account_number_display),
               last_seen=excluded.last_seen,active=1,notes=CASE WHEN excluded.notes<>'' THEN excluded.notes ELSE utility_accounts.notes END""",
            (vendor, canonical, display_account or account, property_id, self._utility_type(vendor), now, now, notes),
        )
        row = self.connection.execute(
            "SELECT utility_account_id FROM utility_accounts WHERE vendor=? AND account_number_canonical=?", (vendor, canonical)
        ).fetchone()
        account_id = int(row["utility_account_id"])
        if meter_number:
            self.register_meter(account_id, meter_number)
        # Backfill every historical invoice for this utility account to the
        # canonical property. This makes duplicate/anomaly controls portfolio-wide.
        self.connection.execute(
            """UPDATE invoices SET property_id=?,property_name=?,suggested_property_id=NULL
               WHERE vendor=? AND account_number_canonical=?""",
            (property_id, prop["project_name"], vendor, canonical),
        )
        # Mapping is a human master-data decision. Remove only the portfolio
        # exceptions that the decision resolves; retain every unrelated issue.
        known_meters = self.known_meters_for_account(account_id)
        affected = self.connection.execute(
            "SELECT document_id,issues_json,portfolio_flags_json,meter_number,status FROM invoices WHERE vendor=? AND account_number_canonical=?",
            (vendor, canonical),
        ).fetchall()
        for inv in affected:
            issues = [m for m in json.loads(inv["issues_json"] or "[]") if "is not mapped to the Property × Vendor × Account × Meter Master" not in m]
            flags = [f for f in json.loads(inv["portfolio_flags_json"] or "[]") if f != "UNMAPPED_UTILITY_ACCOUNT"]
            if inv["meter_number"] and inv["meter_number"] in known_meters:
                issues = [m for m in issues if not m.startswith("Meter master mismatch for project ")]
                flags = [f for f in flags if f != "METER_MASTER_MISMATCH"]
            new_status = "OK" if inv["status"] == "REVIEW_REQUIRED" and not issues else inv["status"]
            self.connection.execute(
                "UPDATE invoices SET issues_json=?,portfolio_flags_json=?,status=? WHERE document_id=?",
                (_json(issues), _json(flags), new_status, inv["document_id"]),
            )
            resolved_types = ["UNMAPPED_UTILITY_ACCOUNT"]
            if inv["meter_number"] and inv["meter_number"] in known_meters:
                resolved_types.append("METER_MASTER_MISMATCH")
            placeholders = ",".join("?" for _ in resolved_types)
            self.connection.execute(
                f"DELETE FROM invoice_anomalies WHERE document_id=? AND anomaly_type IN ({placeholders})",
                (inv["document_id"], *resolved_types),
            )
        self.audit("UTILITY_ACCOUNT_MAPPED", actor=actor, old_value=old_property, new_value=property_id,
                   details={"vendor": vendor, "account": canonical, "meter": meter_number})
        self.connection.commit()
        return account_id

    @staticmethod
    def _utility_type(vendor: str) -> str:
        key = (vendor or "").upper()
        if key == "BC HYDRO": return "ELECTRICITY"
        if key == "FORTISBC": return "NATURAL_GAS"
        if key == "TELUS": return "TELECOM"
        return "UTILITY"

    def register_meter(self, utility_account_id: int, meter_number: str, display: str = "") -> None:
        from invoice_normalization import normalize_meter
        meter = normalize_meter(meter_number)
        if not meter:
            return
        now = _now()
        self.connection.execute(
            """INSERT INTO utility_meters(utility_account_id,meter_number_canonical,meter_number_display,active,first_seen,last_seen)
               VALUES(?,?,?,1,?,?) ON CONFLICT(utility_account_id,meter_number_canonical)
               DO UPDATE SET last_seen=excluded.last_seen,active=1""",
            (utility_account_id, meter, display or meter_number, now, now),
        )

    def known_meters_for_account(self, utility_account_id: int) -> set[str]:
        rows = self.connection.execute(
            "SELECT meter_number_canonical FROM utility_meters WHERE utility_account_id=? AND active=1", (utility_account_id,)
        ).fetchall()
        return {str(r["meter_number_canonical"]) for r in rows}

    def add_relationship(self, relation_type: str, from_document_id: str, to_document_id: str,
                         details: Optional[dict] = None) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO invoice_relationships(relation_type,from_document_id,to_document_id,created_at,details_json)
               VALUES(?,?,?,?,?)""", (relation_type, from_document_id, to_document_id, _now(), _json(details or {}))
        )

    def clear_anomalies(self, document_id: str) -> None:
        self.connection.execute("DELETE FROM invoice_anomalies WHERE document_id=?", (document_id,))

    def add_anomaly(self, document_id: str, anomaly_type: str, severity: str, message: str,
                    score: Optional[float] = None, details: Optional[dict] = None) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO invoice_anomalies(document_id,anomaly_type,severity,score,message,details_json,created_at)
               VALUES(?,?,?,?,?,?,?)""", (document_id, anomaly_type, severity, score, message, _json(details or {}), _now())
        )

    def anomalies_for_document(self, document_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM invoice_anomalies WHERE document_id=? ORDER BY CASE severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END, anomaly_id",
            (document_id,),
        ).fetchall()

    def billing_history(self, vendor: str, account: str, exclude_path: str = "", limit: int = 36) -> list[sqlite3.Row]:
        params: list[Any] = [vendor, account]
        ex = ""
        if exclude_path:
            ex = " AND file_path<>?"; params.append(str(Path(exclude_path).resolve()))
        params.append(limit)
        return self.connection.execute(
            f"""SELECT document_id,file_path,property_id,billing_period_start,billing_period_end,total_amount_due,status,
               revision_of_document_id FROM invoices WHERE vendor=? AND account_number_canonical=?
               AND billing_period_start IS NOT NULL AND billing_period_end IS NOT NULL
               AND status='OK' AND revision_of_document_id IS NULL {ex}
               ORDER BY billing_period_start DESC LIMIT ?""", tuple(params)
        ).fetchall()

    def duplicate_record(self, fingerprint: str, exclude_path: str) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            """SELECT document_id,file_path,property_id,total_amount_due FROM invoices
               WHERE logical_fingerprint=? AND file_path<>? AND status<>'FAILED' ORDER BY processed_at ASC LIMIT 1""",
            (fingerprint, str(Path(exclude_path).resolve())),
        ).fetchone()

    def revised_candidate(self, vendor: str, account: str, period_start: Optional[str], period_end: Optional[str],
                          bill_date: Optional[str], amount: float, exclude_path: str) -> Optional[sqlite3.Row]:
        if period_start and period_end:
            row = self.connection.execute(
                """SELECT document_id,file_path,property_id,total_amount_due,billing_period_start,billing_period_end
                   FROM invoices WHERE vendor=? AND account_number_canonical=? AND billing_period_start=? AND billing_period_end=?
                   AND file_path<>? AND total_amount_due IS NOT NULL AND status NOT IN ('FAILED','DUPLICATE')
                   ORDER BY processed_at DESC LIMIT 1""",
                (vendor, account, period_start, period_end, str(Path(exclude_path).resolve())),
            ).fetchone()
        elif bill_date:
            row = self.connection.execute(
                """SELECT document_id,file_path,property_id,total_amount_due,billing_period_start,billing_period_end
                   FROM invoices WHERE vendor=? AND account_number_canonical=? AND bill_date=? AND file_path<>?
                   AND total_amount_due IS NOT NULL AND status NOT IN ('FAILED','DUPLICATE') ORDER BY processed_at DESC LIMIT 1""",
                (vendor, account, bill_date, str(Path(exclude_path).resolve())),
            ).fetchone()
        else:
            return None
        if row and abs(float(row["total_amount_due"]) - float(amount)) > 0.009:
            return row
        return None

    # ---------------------------- review / audit ----------------------------
    FIELD_COLUMNS = {
        "account_number": "account_number_canonical",
        "bill_date": "bill_date", "billing_period": "billing_period", "current_charges": "current_charges",
        "total_amount_due": "total_amount_due", "meter_number": "meter_number", "consumption": "consumption",
        "consumption_unit": "consumption_unit", "previous_balance": "previous_balance", "payments": "payments",
    }

    def review_field(self, document_id: str, field_name: str, decision: str, final_value: Any = None,
                     actor: str = "Local user", reason: str = "") -> None:
        result = self.get_result_by_document(document_id)
        if not result:
            raise KeyError(f"Unknown document_id: {document_id}")
        if field_name not in self.FIELD_COLUMNS:
            raise ValueError(f"Unsupported review field: {field_name}")
        column = self.FIELD_COLUMNS[field_name]
        old_value = getattr(result, field_name)
        if decision.upper() == "ACCEPT":
            final_value = old_value
        elif decision.upper() != "CORRECT":
            raise ValueError("decision must be ACCEPT or CORRECT")

        if field_name in {"current_charges","total_amount_due","consumption","previous_balance","payments"} and final_value not in (None, ""):
            final_value = float(final_value)
        if field_name == "account_number":
            raw = str(final_value or "")
            final_value = canonical_account(result.vendor, raw)
            if not final_value:
                raise ValueError("Corrected account number is not valid for this vendor.")
            self.connection.execute(
                "UPDATE invoices SET account_number=?,account_number_canonical=?,account_number_raw=? WHERE document_id=?",
                (final_value, final_value, raw, document_id),
            )
        else:
            self.connection.execute(f"UPDATE invoices SET {column}=? WHERE document_id=?", (final_value, document_id))

        # Keep v4 derived/master fields consistent after human corrections.
        if field_name == "account_number":
            mapped = self.get_utility_account(result.vendor, str(final_value))
            if mapped:
                self.connection.execute(
                    "UPDATE invoices SET property_id=?,property_name=?,suggested_property_id=NULL WHERE document_id=?",
                    (mapped["property_id"], mapped["project_name"], document_id),
                )
            else:
                suggestion = self.suggest_property_from_path(result.file_path) if self.has_portfolio() else None
                self.connection.execute(
                    "UPDATE invoices SET property_id=NULL,property_name=NULL,suggested_property_id=? WHERE document_id=?",
                    (suggestion, document_id),
                )
        if field_name == "billing_period":
            start_date, end_date = parse_billing_period(str(final_value or ""))
            self.connection.execute(
                "UPDATE invoices SET billing_period_start=?,billing_period_end=? WHERE document_id=?",
                (start_date, end_date, document_id),
            )
        if field_name in {"current_charges", "total_amount_due", "consumption"}:
            cur = self.connection.execute(
                "SELECT current_charges,total_amount_due,consumption FROM invoices WHERE document_id=?", (document_id,)
            ).fetchone()
            unit_cost = effective_unit_cost(cur["current_charges"], cur["total_amount_due"], cur["consumption"]) if cur else None
            self.connection.execute("UPDATE invoices SET effective_unit_cost=? WHERE document_id=?", (unit_cost, document_id))

        self.connection.execute(
            """INSERT INTO field_reviews(document_id,field_name,decision,extracted_json,final_json,actor,reason,reviewed_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (document_id, field_name, decision.upper(), _json(old_value), _json(final_value), actor, reason, _now()),
        )
        self.audit("FIELD_" + decision.upper(), document_id=document_id, file_path=result.file_path, field_name=field_name,
                   old_value=old_value, new_value=final_value, actor=actor, reason=reason)
        self.rebuild_account_registry()
        self.connection.commit()

    def finalize_review(self, document_id: str, actor: str = "Local user", reason: str = "") -> None:
        result = self.get_result_by_document(document_id)
        if not result:
            raise KeyError(document_id)
        if not result.account_number or result.total_amount_due is None:
            raise ValueError("Account number and total amount due are required before finalizing review.")
        self.connection.execute("UPDATE invoices SET status='OK' WHERE document_id=?", (document_id,))
        self.audit("DOCUMENT_REVIEW_FINALIZED", document_id=document_id, file_path=result.file_path, actor=actor, reason=reason)
        self.rebuild_account_registry()
        self.connection.commit()

    def latest_field_reviews(self, document_id: str) -> dict[str, sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT * FROM field_reviews WHERE document_id=? ORDER BY reviewed_at ASC, review_id ASC", (document_id,)
        ).fetchall()
        return {row["field_name"]: row for row in rows}

    def audit(self, event_type: str, run_id: Optional[str] = None, document_id: Optional[str] = None,
              file_path: Optional[str] = None, field_name: Optional[str] = None, old_value: Any = None,
              new_value: Any = None, actor: str = "", reason: str = "", details: Optional[dict] = None) -> None:
        self.connection.execute(
            """INSERT INTO audit_events(event_time,event_type,run_id,document_id,file_path,field_name,old_value_json,
               new_value_json,actor,reason,details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), event_type, run_id, document_id, file_path, field_name, _json(old_value) if old_value is not None else None,
             _json(new_value) if new_value is not None else None, actor, reason, _json(details or {})),
        )

    def audit_for_document(self, document_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM audit_events WHERE document_id=? ORDER BY event_time,event_id", (document_id,)
        ).fetchall()
