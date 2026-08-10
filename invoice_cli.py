from __future__ import annotations

import argparse
from pathlib import Path

from invoice_engine import InvoiceEngine, find_pdfs
from invoice_models import RESOURCE_POLICIES
from version import APP_VERSION


def main() -> None:
    parser = argparse.ArgumentParser(description="Local/offline portfolio-aware utility invoice assurance engine v4")
    parser.add_argument("directory", help="Directory containing PDF invoices")
    parser.add_argument("--output", default="invoice_results_v4.csv", help="CSV output path")
    parser.add_argument("--portfolio-csv", help="Optional local PM/property master CSV. Bundled pm.csv is used by default.")
    parser.add_argument("--recursive", action="store_true", help="Include subfolders")
    parser.add_argument("--force", action="store_true", help="Reprocess unchanged PDFs")
    parser.add_argument("--resource-mode", choices=list(RESOURCE_POLICIES), default="Balanced")
    parser.add_argument("--initial-pages", type=int, default=3)
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    args = parser.parse_args()

    source = Path(args.directory)
    if not source.is_dir():
        raise SystemExit(f"Directory does not exist: {source}")
    output = Path(args.output).resolve(); output.parent.mkdir(parents=True, exist_ok=True)
    db_path = output.with_name("invoice_intelligence_v4.db")
    files = find_pdfs(source, recursive=args.recursive)
    print(f"Found {len(files)} PDF(s).")

    engine = InvoiceEngine(
        str(db_path), initial_pages=args.initial_pages, max_pages=args.max_pages,
        resource_mode=args.resource_mode, auto_seed_portfolio=not bool(args.portfolio_csv),
    )
    try:
        if args.portfolio_csv:
            stats = engine.db.import_property_master(args.portfolio_csv)
            print(f"Portfolio: {stats['properties']} canonical projects from {stats['source_rows']} source rows.")
        def progress(p: dict) -> None:
            result = p["result"]
            project = result.property_id or result.suggested_property_id or "-"
            print(f"[{p['index']}/{p['total']}] {result.status:15} {project:10} {result.vendor:10} {result.file_name}")
        results = engine.process_files(files, force=args.force, progress=progress, source_label=str(source), output_path=str(output))
        engine.export_csv(results, str(output))
        print(f"CSV: {output}")
        print(f"DB : {db_path}")
    finally:
        engine.close()


if __name__ == "__main__":
    main()
