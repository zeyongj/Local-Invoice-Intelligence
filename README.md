# Local Invoice Intelligence v4 — Portfolio Edition

A 100% local/offline Windows desktop application for utility-invoice extraction, validation, portfolio mapping, duplicate/revision control, billing continuity, and historical anomaly detection.

## Design target

- Windows 11 Enterprise office workstation
- Low/mid-range CPU, 8 GB RAM, SSD recommended
- No cloud API
- No LLM
- No external OCR service
- No GPU requirement
- No database server required for Workstation Edition
- Born-digital PDFs with a usable text layer

v4 retains all v3 controls (spatial parsing, vendor parsers, confidence, accounting reconciliation, audit trail, review center, run/resume, safe cancel, SQLite integrity/backup, layout profiles) and adds a portfolio-aware control layer.

## v4 core capabilities

### 1. Property × Vendor × Account × Meter Master

The bundled `data/pm.csv` is an offline snapshot of the supplied PM portfolio file. It is used as the property-master seed.

The importer is **source-aware**:

- If a `PROJ #` source cell explicitly lists a parent first, all codes in that cell become aliases of the first/canonical project.
- Example: `5093`, `5093-1`, `5093-2`, `5093-3` all resolve to canonical internal project `5093`.
- It does **not** blindly strip suffixes. A standalone source code such as `5164-10` remains `5164-10` unless the source explicitly provides a parent.
- If one canonical project occurs on multiple source rows/addresses, v4 stores one canonical property plus multiple `property_locations`; no source row is silently overwritten.

Utility identities are then mapped as:

```text
Canonical Project
    └── Vendor
         └── Canonical Account
              └── Meter(s)
```

BC Hydro account compatibility remains vendor-aware:

```text
PDF raw:        902 741
Canonical:      902741
```

The raw account remains available in audit evidence while history, duplicate detection and master-data joins use the canonical string.

### 2. Portfolio-wide duplicate and revised-invoice detection

v4 supports:

- exact logical duplicate detection independent of filename/folder;
- cross-property duplicate warning if the same logical invoice is associated with inconsistent projects;
- possible revised/reissued invoice detection when vendor/account/billing identity matches but amount changes;
- invoice relationship records (`EXACT_DUPLICATE`, `POSSIBLE_REVISION`) stored separately from the source invoices.

Identical billing periods that are possible revisions are not incorrectly double-reported as billing overlaps.

### 3. Billing-period overlap and gap detection

When a parser extracts a billing/service period, v4 normalizes common deterministic formats to ISO dates and builds an account-level timeline.

Controls include:

- partial/full overlap with prior effective periods;
- uncovered gaps relative to historical billing cadence;
- larger gaps flagged as possible missing utility invoices;
- ambiguous numeric dates are intentionally not guessed.

### 4. Amount + consumption historical anomaly

v4 keeps separate historical baselines for:

- total amount;
- consumption;
- effective unit cost;
- same-calendar-month observations when enough history exists.

Robust statistics use median/MAD rather than a fragile mean/std baseline. It also checks amount-versus-consumption divergence (for example, a large bill increase not explained by a comparable usage increase).

These controls are deterministic anomaly signals, not tariff predictions.

## User interface

Run:

```bat
run_app.bat
```

or:

```powershell
py main.py
```

Main UI features:

- Windows-native Tk/ttk interface with Segoe UI and Windows `vista` theme where available;
- select invoice directory;
- include/exclude subfolders;
- select all or only part of the invoices;
- choose CSV output location;
- low-impact / balanced / high-performance process priority modes;
- real-time progress bar;
- rolling-median ETA;
- result highlights and exception details;
- canonical Project column;
- `Map account…` action;
- Review Center with field-level corrections and audit trail;
- Run History with safe resume;
- Portfolio Master window;
- database integrity check and backup.

### Portfolio Master window

The Portfolio Master shows:

- canonical properties;
- utility accounts;
- registered meters;
- unmapped invoices;
- project search;
- local PM CSV re-import/update;
- utility-master view.

`Map account…` lets the user select a canonical internal project. Once mapped, historical invoices for the same vendor/canonical account are backfilled to that property.

## Installation

```powershell
py -m pip install -r requirements.txt
```

Runtime dependency:

```text
PyMuPDF>=1.24,<2
```

Everything else uses the Python standard library.

## Optional Windows executable

For a developer packaging build:

```bat
build_windows_exe.bat
```

The script includes `data\pm.csv` in the PyInstaller bundle so the executable does not need network access to initialize the portfolio seed.

For enterprise distribution, code-sign the executable/installer and perform Windows 11 Enterprise UAT before production deployment.

## Database

GUI output uses:

```text
invoice_intelligence_v4.db
```

v4 SQLite schema version: `4`.

Important v4 tables:

```text
properties
property_aliases
property_locations
property_postal_codes
utility_accounts
utility_meters
invoices
invoice_relationships
invoice_anomalies
processing_runs
run_items
field_reviews
audit_events
layout_profiles
```

Older v2/v3 databases are migrated in place. A pre-migration SQLite backup is created when a schema upgrade is detected.

## CLI

```powershell
py invoice_cli.py "C:\AP\Utility Invoices" --output "C:\AP\Reports\invoice_results_v4.csv"
```

Use a different local portfolio file:

```powershell
py invoice_cli.py "C:\AP\Utility Invoices" --portfolio-csv "C:\AP\Master\pm.csv"
```

## CSV output highlights

The export includes v4 fields such as:

```text
property_id
property_name
account_number_raw
account_number_canonical
billing_period_start
billing_period_end
consumption
effective_unit_cost
portfolio_flags
revision_of_document_id
```

## Important operational principle

v4 is intentionally precision-oriented. Unknown account/property mappings, master-meter mismatches, possible revisions, severe anomalies, and billing-continuity exceptions are routed to review rather than silently guessed.

## Tests

Run:

```powershell
py -m pytest -q
```

The delivered build includes v2/v3 regression coverage plus v4 portfolio controls.

See:

- `ARCHITECTURE.md`
- `PERFORMANCE.md`
- `PORTFOLIO_DATA_NOTES.md`
- `SECURITY.md`
- `VALIDATION.txt`
