# Local Invoice Intelligence / Invoice Validation Engine

A lightweight, offline Python desktop application for analyzing born-digital PDF utility invoices on Windows 11 Enterprise. It does **not** call external APIs, cloud services, LLMs, or remote databases.

## What it extracts

- Vendor
- Account number
- Bill date
- Billing period
- Current charges
- Total amount due
- Previous balance / payments
- Meter number
- Consumption and unit
- Extraction confidence and evidence

Built-in vendor parsers: **TELUS**, **BC Hydro**, **FortisBC**, plus a generic fallback.

## Four performance / reliability improvements implemented

1. **Spatial index** — PDF lines are indexed by page and Y-coordinate buckets. Nearby anchor/value lookup is near-linear instead of a full all-lines × all-lines scan.
2. **Historical account registry** — vendor/account history is stored locally in SQLite. The engine checks robust amount anomalies (median/MAD), recent amount jumps, and established meter continuity.
3. **Logical invoice fingerprint** — SHA-256 of canonical business fields (`vendor | account | bill date/period | total amount`) catches renamed/copied duplicates without reading every PDF byte for a file hash.
4. **Non-AI template learning** — high-confidence field positions are learned as normalized vendor layout profiles. After at least three samples, later invoices can search the expected page region first. No model training or network access is involved.

## Windows UI

The GUI is built with Python's standard `tkinter/ttk` and uses the native Windows `vista` theme when available, Segoe UI, native file dialogs, and Windows DPI awareness.

It supports:

- Choose a directory
- Include or exclude subfolders
- Select all invoices or only a subset (Ctrl/Shift multi-select)
- Choose the exact CSV output location
- Persistent SQLite history database beside the CSV
- Progress bar
- Median-based estimated remaining time
- Live results as each PDF finishes
- Summary highlights: processed files, non-duplicate total amount, OK, review, duplicate counts
- Status-colored results
- Per-invoice review notes and warnings
- Open selected source PDF
- Incremental processing: unchanged files are read from cache instead of reparsed

## Requirements

- Windows 11 Enterprise (also runs on macOS/Linux for development)
- Python 3.10+
- PyMuPDF

Install:

```powershell
py -m pip install -r requirements.txt
```

## Run the GUI

Double-click:

```text
run_app.bat
```

or:

```powershell
py main.py
```

## Run from CLI

```powershell
py invoice_cli.py "C:\AP\Utility Invoices" --output "C:\AP\Reports\invoice_results.csv"
```

Recursive scan:

```powershell
py invoice_cli.py "C:\AP\Utility Invoices" --recursive --output "C:\AP\Reports\invoice_results.csv"
```

Reprocess unchanged files:

```powershell
py invoice_cli.py "C:\AP\Utility Invoices" --force
```

## Output

The selected CSV is the shareable analysis output. A persistent `invoice_intelligence.db` is created in the same directory to retain:

- prior invoice results
- account history
- vendor layout profiles
- duplicate fingerprints

The database is intentionally persistent because history is required for anomaly detection and template learning.

## Safety / accounting design

The engine prioritizes precision over forced automation. If a PDF has no usable text layer, required fields are missing, confidence is low, accounting reconciliation fails, or historical controls trigger, the invoice is marked `REVIEW_REQUIRED`. Scanned-image OCR is intentionally disabled in this build.

Duplicate invoices are stored for audit visibility but are excluded from historical amount baselines, account counts, and layout learning so that duplicates do not contaminate controls.

## Tests

```powershell
py -m pytest -q
```

The included test suite covers synthetic TELUS and BC Hydro PDFs, meter/usage extraction, duplicate detection, robust anomaly scoring, fingerprint stability, and local spatial indexing.

See `PERFORMANCE.md` for detailed complexity analysis.
