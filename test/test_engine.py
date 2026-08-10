from __future__ import annotations

from pathlib import Path

import fitz

from invoice_engine import InvoiceEngine, logical_fingerprint, robust_z_score
from invoice_models import InvoiceResult, STATUS_DUPLICATE, STATUS_OK


def make_pdf(path: Path, lines: list[tuple[float, float, str]]) -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    for x, y, text in lines:
        page.insert_text((x, y), text, fontsize=11)
    doc.save(path)
    doc.close()


def test_telus_end_to_end(tmp_path: Path):
    pdf = tmp_path / "telus.pdf"
    make_pdf(
        pdf,
        [
            (55, 60, "TELUS"),
            (55, 95, "Account number: 123456789"),
            (55, 120, "Bill date: August 1, 2026"),
            (55, 150, "Previous balance $100.00"),
            (55, 175, "Payments $100.00"),
            (55, 200, "Current charges $135.72"),
            (55, 225, "Total amount due $135.72"),
        ],
    )
    engine = InvoiceEngine(str(tmp_path / "history.db"))
    try:
        result = engine.process_one(str(pdf))
        assert result.vendor == "TELUS"
        assert result.account_number == "123456789"
        assert result.bill_date == "2026-08-01"
        assert result.current_charges == 135.72
        assert result.total_amount_due == 135.72
        assert result.status == STATUS_OK
    finally:
        engine.close()


def test_bc_hydro_meter_and_usage(tmp_path: Path):
    pdf = tmp_path / "hydro.pdf"
    make_pdf(
        pdf,
        [
            (55, 60, "BC Hydro"),
            (55, 95, "Account number: 9988776655"),
            (55, 120, "Bill date: July 31, 2026"),
            (55, 150, "Current charges $925.11"),
            (55, 175, "Amount due $925.11"),
            (55, 200, "Meter number: 829301"),
            (55, 225, "Electricity usage: 1,141 kWh"),
        ],
    )
    engine = InvoiceEngine(str(tmp_path / "history.db"))
    try:
        result = engine.process_one(str(pdf))
        assert result.vendor == "BC HYDRO"
        assert result.meter_number == "829301"
        assert result.consumption == 1141.0
        assert result.consumption_unit == "kWh"
    finally:
        engine.close()


def test_logical_duplicate_detection(tmp_path: Path):
    first = tmp_path / "bill-a.pdf"
    second = tmp_path / "renamed-copy.pdf"
    lines = [
        (55, 60, "TELUS"),
        (55, 95, "Account number: 123456789"),
        (55, 120, "Bill date: August 1, 2026"),
        (55, 150, "Current charges $135.72"),
        (55, 175, "Total amount due $135.72"),
    ]
    make_pdf(first, lines)
    make_pdf(second, lines)
    engine = InvoiceEngine(str(tmp_path / "history.db"))
    try:
        results = engine.process_files([first, second], force=True)
        assert results[0].status == STATUS_OK
        assert results[1].status == STATUS_DUPLICATE
        assert results[1].duplicate_of is not None
    finally:
        engine.close()


def test_robust_z_score_flags_large_outlier():
    baseline = [100.0, 102.0, 98.0, 101.0, 99.0, 100.0]
    assert abs(robust_z_score(500.0, baseline) or 0) > 6


def test_fingerprint_is_filename_independent():
    a = InvoiceResult(file_name="a.pdf", file_path="a.pdf", vendor="TELUS", account_number="123456789", bill_date="2026-08-01", total_amount_due=25.50)
    b = InvoiceResult(file_name="b.pdf", file_path="b.pdf", vendor="TELUS", account_number="123456789", bill_date="2026-08-01", total_amount_due=25.50)
    assert logical_fingerprint(a) == logical_fingerprint(b)

def test_layout_profile_learns_after_repeated_vendor(tmp_path: Path):
    files = []
    for i, y in enumerate((95, 97, 96), start=1):
        pdf = tmp_path / f"telus-{i}.pdf"
        make_pdf(
            pdf,
            [
                (55, 60, "TELUS"),
                (55, y, f"Account number: 12345678{i}"),
                (55, 120, f"Bill date: August {i}, 2026"),
                (55, 150, f"Current charges ${100+i:.2f}"),
                (55, 175, f"Total amount due ${100+i:.2f}"),
            ],
        )
        files.append(pdf)
    engine = InvoiceEngine(str(tmp_path / "history.db"))
    try:
        engine.process_files(files, force=True)
        profiles = engine.db.get_layout_profiles("TELUS")
        assert profiles["account_number"].samples >= 3
        assert 0.0 < profiles["account_number"].mean_y < 1.0
    finally:
        engine.close()


def test_duplicate_does_not_pollute_account_history(tmp_path: Path):
    first = tmp_path / "bill-original.pdf"
    second = tmp_path / "bill-copy.pdf"
    lines = [
        (55, 60, "TELUS"),
        (55, 95, "Account number: 444555666"),
        (55, 120, "Bill date: August 1, 2026"),
        (55, 150, "Current charges $88.00"),
        (55, 175, "Total amount due $88.00"),
    ]
    make_pdf(first, lines)
    make_pdf(second, lines)
    engine = InvoiceEngine(str(tmp_path / "history.db"))
    try:
        engine.process_files([first, second], force=True)
        history = engine.db.get_history("TELUS", "444555666")
        assert history.invoice_count == 1
        assert len(history.amounts) == 1
    finally:
        engine.close()
