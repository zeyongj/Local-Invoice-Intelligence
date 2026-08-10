from __future__ import annotations

from pathlib import Path

import fitz

from invoice_database import InvoiceDatabase
from invoice_engine import InvoiceEngine
from portfolio_intelligence import load_property_csv, parse_billing_period

ROOT = Path(__file__).resolve().parents[1]
PM_CSV = ROOT / "data" / "pm.csv"


def make_pdf(path: Path, lines: list[tuple[float, float, str]]) -> None:
    doc = fitz.open(); page = doc.new_page(width=612, height=792)
    for x, y, text in lines:
        page.insert_text((x, y), text, fontsize=11)
    doc.save(path); doc.close()


def hydro_pdf(path: Path, account: str, bill_date: str, period: str, amount: float,
              consumption: float = 1000.0, meter: str = "829301") -> None:
    make_pdf(path, [
        (55,60,"BC Hydro"),
        (55,90,f"Account number: {account}"),
        (55,115,f"Bill date: {bill_date}"),
        (55,140,f"Billing period: {period}"),
        (55,165,f"Current charges ${amount:.2f}"),
        (55,190,f"Amount due ${amount:.2f}"),
        (55,215,f"Meter number: {meter}"),
        (55,240,f"Electricity usage: {consumption:,.0f} kWh"),
    ])


def seed_and_map(engine: InvoiceEngine, account: str = "902741", project: str = "5093") -> None:
    engine.db.import_property_master(PM_CSV)
    engine.db.map_utility_account("BC HYDRO", account, project, display_account=account, meter_number="829301", actor="test")


def test_pm_csv_source_aware_project_normalization(tmp_path: Path):
    db = InvoiceDatabase(str(tmp_path / "portfolio.db"), create_backup=False)
    try:
        stats = db.import_property_master(PM_CSV)
        assert stats["source_rows"] == 447
        assert stats["properties"] == 442
        assert db.resolve_project_alias("5093") == "5093"
        assert db.resolve_project_alias("5093-1") == "5093"
        assert db.resolve_project_alias("5093-2") == "5093"
        assert db.resolve_project_alias("5093-3") == "5093"
        # Do not invent a parent project where the source does not provide one.
        assert db.resolve_project_alias("5164-10") == "5164-10"
        assert db.resolve_project_alias("5164") is None
        # Repeated internal project codes retain every source location.
        assert len(db.property_locations("5462")) == 3
    finally:
        db.close()


def test_billing_period_parser_common_utility_formats():
    assert parse_billing_period("July 1, 2026 - July 31, 2026") == ("2026-07-01", "2026-07-31")
    assert parse_billing_period("Jul 1 - Jul 31, 2026") == ("2026-07-01", "2026-07-31")
    assert parse_billing_period("2026-07-01 to 2026-07-31") == ("2026-07-01", "2026-07-31")


def test_property_account_meter_master_maps_spaced_and_unspaced_hydro(tmp_path: Path):
    pdf = tmp_path / "5093" / "hydro.pdf"; pdf.parent.mkdir()
    hydro_pdf(pdf, "902 741", "August 1, 2026", "July 1, 2026 - July 31, 2026", 120.0)
    engine = InvoiceEngine(str(tmp_path / "v4.db"))
    try:
        seed_and_map(engine, "902741", "5093")
        result = engine.process_files([pdf], force=True)[0]
        assert result.account_number == "902741"
        assert result.account_number_raw == "902 741"
        assert result.property_id == "5093"
        assert result.meter_number == "829301"
        assert "UNMAPPED_UTILITY_ACCOUNT" not in result.portfolio_flags
    finally:
        engine.close()


def test_revised_invoice_same_period_is_not_billing_overlap(tmp_path: Path):
    first = tmp_path / "first.pdf"; revised = tmp_path / "revised.pdf"
    hydro_pdf(first, "902741", "August 1, 2026", "July 1, 2026 - July 31, 2026", 100.0)
    hydro_pdf(revised, "902741", "August 2, 2026", "July 1, 2026 - July 31, 2026", 115.0)
    engine = InvoiceEngine(str(tmp_path / "v4.db"))
    try:
        seed_and_map(engine)
        results = engine.process_files([first, revised], force=True)
        assert "POSSIBLE_REVISED_INVOICE" in results[1].portfolio_flags
        assert results[1].revision_of_document_id == results[0].document_id
        assert "BILLING_OVERLAP_HIGH" not in results[1].portfolio_flags
    finally:
        engine.close()


def test_billing_overlap_and_gap_detection(tmp_path: Path):
    engine = InvoiceEngine(str(tmp_path / "v4.db"))
    try:
        seed_and_map(engine)
        jan=tmp_path/"jan.pdf"; feb=tmp_path/"feb.pdf"; overlap=tmp_path/"overlap.pdf"
        hydro_pdf(jan,"902741","February 1, 2026","January 1, 2026 - January 31, 2026",100)
        hydro_pdf(feb,"902741","March 1, 2026","February 1, 2026 - February 28, 2026",100)
        hydro_pdf(overlap,"902741","April 1, 2026","February 20, 2026 - March 31, 2026",100)
        engine.process_files([jan,feb],force=True)
        r=engine.process_files([overlap],force=True)[0]
        assert "BILLING_OVERLAP_HIGH" in r.portfolio_flags

        # Separate account: Jan/Feb then April, leaving all of March uncovered.
        engine.db.map_utility_account("BC HYDRO","812345","5093",display_account="812345",meter_number="829302")
        a=tmp_path/"a.pdf"; b=tmp_path/"b.pdf"; c=tmp_path/"c.pdf"
        hydro_pdf(a,"812345","February 1, 2026","January 1, 2026 - January 31, 2026",100,meter="829302")
        hydro_pdf(b,"812345","March 1, 2026","February 1, 2026 - February 28, 2026",100,meter="829302")
        hydro_pdf(c,"812345","May 1, 2026","April 1, 2026 - April 30, 2026",100,meter="829302")
        engine.process_files([a,b],force=True)
        gap=engine.process_files([c],force=True)[0]
        assert "BILLING_GAP_HIGH" in gap.portfolio_flags
    finally:
        engine.close()


def test_amount_consumption_and_unit_cost_anomaly(tmp_path: Path):
    engine = InvoiceEngine(str(tmp_path / "v4.db"))
    try:
        seed_and_map(engine)
        # Five stable baseline bills.
        months=[("January",1,31),("February",1,28),("March",1,31),("April",1,30),("May",1,31)]
        files=[]
        for i,(month,start_day,end_day) in enumerate(months, start=1):
            p=tmp_path/f"base-{i}.pdf"
            hydro_pdf(p,"902741",f"{month} {end_day}, 2026",f"{month} {start_day}, 2026 - {month} {end_day}, 2026",100.0+i,1000+i*5)
            files.append(p)
        engine.process_files(files,force=True)
        spike=tmp_path/"spike.pdf"
        hydro_pdf(spike,"902741","June 30, 2026","June 1, 2026 - June 30, 2026",900.0,1100)
        r=engine.process_files([spike],force=True)[0]
        assert "AMOUNT_ANOMALY_HIGH" in r.portfolio_flags
        assert "UNIT_COST_ANOMALY_HIGH" in r.portfolio_flags
    finally:
        engine.close()

def test_consumption_anomaly_independent_of_amount(tmp_path: Path):
    engine = InvoiceEngine(str(tmp_path / "v4.db"))
    try:
        seed_and_map(engine)
        periods=[
            ("January 1, 2026 - January 31, 2026","January 31, 2026"),
            ("February 1, 2026 - February 28, 2026","February 28, 2026"),
            ("March 1, 2026 - March 31, 2026","March 31, 2026"),
            ("April 1, 2026 - April 30, 2026","April 30, 2026"),
            ("May 1, 2026 - May 31, 2026","May 31, 2026"),
        ]
        files=[]
        for i,(period,bill) in enumerate(periods):
            p=tmp_path/f"normal-c-{i}.pdf"; hydro_pdf(p,"902741",bill,period,100+i,1000+i*3); files.append(p)
        engine.process_files(files,force=True)
        spike=tmp_path/"consumption-spike.pdf"
        # Amount is only modestly higher, but consumption is an extreme outlier.
        hydro_pdf(spike,"902741","June 30, 2026","June 1, 2026 - June 30, 2026",115.0,6000)
        r=engine.process_files([spike],force=True)[0]
        assert "CONSUMPTION_ANOMALY_HIGH" in r.portfolio_flags
        assert "AMOUNT_CONSUMPTION_DIVERGENCE" in r.portfolio_flags
    finally:
        engine.close()

def test_unmapped_invoice_becomes_clean_after_human_master_mapping(tmp_path: Path):
    pdf=tmp_path/"5093-1"/"hydro.pdf"; pdf.parent.mkdir()
    hydro_pdf(pdf,"902 741","August 1, 2026","July 1, 2026 - July 31, 2026",100)
    engine=InvoiceEngine(str(tmp_path/"v4.db"))
    try:
        engine.db.import_property_master(PM_CSV)
        r=engine.process_files([pdf],force=True)[0]
        assert "UNMAPPED_UTILITY_ACCOUNT" in r.portfolio_flags
        assert r.suggested_property_id == "5093"
        assert any(a["anomaly_type"] == "UNMAPPED_UTILITY_ACCOUNT" for a in engine.db.anomalies_for_document(r.document_id or ""))
        engine.db.map_utility_account("BC HYDRO","902741","5093",display_account="902 741",meter_number="829301",actor="tester")
        updated=engine.db.get_result_by_document(r.document_id or "")
        assert updated is not None
        assert updated.property_id == "5093"
        assert "UNMAPPED_UTILITY_ACCOUNT" not in updated.portfolio_flags
        assert all("is not mapped to" not in msg for msg in updated.issues)
        assert updated.status == "OK"
        assert all(a["anomaly_type"] != "UNMAPPED_UTILITY_ACCOUNT" for a in engine.db.anomalies_for_document(r.document_id or ""))
    finally:
        engine.close()


def test_possible_revision_does_not_contaminate_account_registry(tmp_path: Path):
    first=tmp_path/"original.pdf"; revised=tmp_path/"revised.pdf"
    hydro_pdf(first,"902741","August 1, 2026","July 1, 2026 - July 31, 2026",100)
    hydro_pdf(revised,"902741","August 2, 2026","July 1, 2026 - July 31, 2026",130)
    engine=InvoiceEngine(str(tmp_path/"v4.db"))
    try:
        seed_and_map(engine)
        engine.process_files([first,revised],force=True)
        history=engine.db.get_history("BC HYDRO","902741")
        assert history.invoice_count == 1
        assert history.amounts == [100.0]
    finally:
        engine.close()
