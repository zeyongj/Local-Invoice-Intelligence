from __future__ import annotations

import sqlite3
from pathlib import Path

import fitz

from invoice_database import InvoiceDatabase
from invoice_engine import InvoiceEngine
from invoice_models import STATUS_OK
from invoice_normalization import canonical_account


def make_pdf(path: Path, lines: list[tuple[float, float, str]]) -> None:
    doc = fitz.open(); page = doc.new_page(width=612, height=792)
    for x, y, text in lines:
        page.insert_text((x, y), text, fontsize=11)
    doc.save(path); doc.close()


def hydro_pdf(path: Path, account: str, date: str = "August 1, 2026", amount: float = 100.0) -> None:
    make_pdf(path, [
        (55,60,"BC Hydro"), (55,95,f"Account number: {account}"), (55,120,f"Bill date: {date}"),
        (55,150,f"Current charges ${amount:.2f}"), (55,175,f"Amount due ${amount:.2f}"),
        (55,200,"Meter number: 829301"), (55,225,"Electricity usage: 1,141 kWh"),
    ])


def test_bc_hydro_spaced_and_unspaced_accounts_share_identity(tmp_path: Path):
    a = tmp_path / "old-format.pdf"; b = tmp_path / "new-format.pdf"
    hydro_pdf(a, "902 741", "August 1, 2026", 100.0)
    hydro_pdf(b, "902741", "September 1, 2026", 110.0)
    engine = InvoiceEngine(str(tmp_path / "v3.db"))
    try:
        results = engine.process_files([a, b], force=True)
        assert results[0].account_number_raw == "902 741"
        assert results[0].account_number == "902741"
        assert results[1].account_number == "902741"
        history = engine.db.get_history("BC HYDRO", "902741")
        assert history.invoice_count == 2
    finally:
        engine.close()


def test_canonical_account_preserves_leading_zeroes():
    assert canonical_account("BC HYDRO", "001 902 741") == "001902741"


def test_near_duplicate_flags_revised_invoice(tmp_path: Path):
    a = tmp_path / "original.pdf"; b = tmp_path / "revised.pdf"
    hydro_pdf(a, "902741", "August 1, 2026", 100.0)
    hydro_pdf(b, "902741", "August 1, 2026", 115.0)
    engine = InvoiceEngine(str(tmp_path / "v3.db"))
    try:
        results = engine.process_files([a, b], force=True)
        assert results[1].near_duplicate_of is not None
        assert "Possible revised invoice" in (results[1].near_duplicate_reason or "")
    finally:
        engine.close()


def test_field_correction_is_audited_and_updates_canonical_account(tmp_path: Path):
    pdf = tmp_path / "hydro.pdf"; hydro_pdf(pdf, "902 741", amount=100.0)
    engine = InvoiceEngine(str(tmp_path / "v3.db"))
    try:
        result = engine.process_files([pdf], force=True)[0]
        engine.db.review_field(result.document_id, "account_number", "CORRECT", "009 027 41", actor="tester", reason="test")
        updated = engine.db.get_result_by_document(result.document_id)
        assert updated is not None
        assert updated.account_number == "00902741"
        assert updated.account_number_raw == "009 027 41"
        events = engine.db.audit_for_document(result.document_id)
        assert any(row["event_type"] == "FIELD_CORRECT" for row in events)
    finally:
        engine.close()


def test_cancelled_run_can_resume_without_reprocessing_completed_items(tmp_path: Path):
    files=[]
    for i in range(3):
        pdf=tmp_path/f"bill-{i}.pdf"; hydro_pdf(pdf, f"90274{i}", f"August {i+1}, 2026", 100+i); files.append(pdf)
    engine=InvoiceEngine(str(tmp_path/"v3.db"))
    flag={"cancel":False,"run_id":None}
    def progress(payload):
        flag["run_id"]=payload["run_id"]
        if payload["index"] == 1: flag["cancel"] = True
    try:
        first=engine.process_files(files, force=True, progress=progress, cancel_requested=lambda: flag["cancel"])
        assert len(first) == 1
        run=engine.db.get_run(flag["run_id"])
        assert run["status"] == "CANCELLED"
        second=engine.process_files([], force=True, run_id=flag["run_id"])
        assert len(second) == 2
        run2=engine.db.get_run(flag["run_id"])
        assert run2["status"] == "COMPLETED"
    finally:
        engine.close()


def test_v2_bc_hydro_account_is_backfilled_during_v3_migration(tmp_path: Path):
    db_path=tmp_path/"legacy.db"
    conn=sqlite3.connect(db_path)
    conn.executescript("""
    CREATE TABLE invoices (
      file_path TEXT PRIMARY KEY, file_name TEXT NOT NULL, file_size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
      vendor TEXT, account_number TEXT, bill_date TEXT, billing_period TEXT, current_charges REAL,
      total_amount_due REAL, meter_number TEXT, consumption REAL, consumption_unit TEXT, previous_balance REAL,
      payments REAL, confidence REAL, status TEXT, issues_json TEXT, warnings_json TEXT, evidence_json TEXT,
      logical_fingerprint TEXT, duplicate_of TEXT, page_count INTEGER, pages_processed INTEGER,
      processing_seconds REAL, error TEXT, processed_at TEXT NOT NULL
    );
    CREATE TABLE account_registry (
      vendor TEXT NOT NULL, account_number TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
      invoice_count INTEGER NOT NULL DEFAULT 0, last_amount REAL, last_bill_date TEXT,
      known_meters_json TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(vendor, account_number)
    );
    CREATE TABLE layout_profiles (
      vendor TEXT NOT NULL, field_name TEXT NOT NULL, page_num INTEGER NOT NULL, mean_x REAL NOT NULL,
      mean_y REAL NOT NULL, mean_w REAL NOT NULL, mean_h REAL NOT NULL, samples INTEGER NOT NULL,
      updated_at TEXT NOT NULL, PRIMARY KEY(vendor, field_name)
    );
    """)
    conn.execute("""INSERT INTO invoices VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
        "C:/legacy.pdf","legacy.pdf",100,1,"BC HYDRO","902 741","2026-01-01",None,100.0,100.0,"12345",None,None,
        None,None,0.9,"OK","[]","[]","{}",None,None,1,1,0.1,None,"2026-01-01T00:00:00"
    ))
    conn.commit(); conn.close()
    db=InvoiceDatabase(str(db_path))
    try:
        row=db.connection.execute("SELECT account_number_raw,account_number_canonical FROM invoices").fetchone()
        assert row["account_number_raw"] == "902 741"
        assert row["account_number_canonical"] == "902741"
        registry=db.connection.execute("SELECT account_number_canonical FROM account_registry").fetchone()
        assert registry["account_number_canonical"] == "902741"
    finally:
        db.close()
