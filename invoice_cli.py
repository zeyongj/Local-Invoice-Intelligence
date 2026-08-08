from __future__ import annotations

import argparse
from pathlib import Path

from invoice_engine import InvoiceEngine, find_pdfs


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Local Invoice Intelligence CLI")
    parser.add_argument("directory", help="Directory containing PDF invoices")
    parser.add_argument("--output", default="invoice_results.csv", help="CSV output path")
    parser.add_argument("--recursive", action="store_true", help="Include subfolders")
    parser.add_argument("--force", action="store_true", help="Reprocess unchanged PDFs")
    parser.add_argument("--initial-pages", type=int, default=3)
    parser.add_argument("--max-pages", type=int, default=0)
    args = parser.parse_args()

    files = find_pdfs(args.directory, recursive=args.recursive)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    db_path = output.with_name("invoice_intelligence.db")
    engine = InvoiceEngine(str(db_path), initial_pages=args.initial_pages, max_pages=args.max_pages)

    def progress(info: dict) -> None:
        result = info["result"]
        marker = "cached" if info["skipped"] else "parsed"
        amount = f"${result.total_amount_due:,.2f}" if result.total_amount_due is not None else "—"
        print(f"[{info['index']:>4}/{info['total']}] {marker:<6} {result.status:<16} {result.vendor:<10} {amount:>12}  {result.file_name}")

    try:
        results = engine.process_files(files, force=args.force, progress=progress)
        engine.export_csv(results, str(output))
    finally:
        engine.close()
    print(f"\nCSV: {output.resolve()}\nDB:  {db_path.resolve()}")


if __name__ == "__main__":
    main()
