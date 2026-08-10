# v4 Architecture — Portfolio Edition

## System boundary

```text
Windows 11 Desktop UI
        │
        ├── Folder / partial invoice selection
        ├── Portfolio Master
        ├── Review Center
        └── Run / Resume
        │
        ▼
Invoice Engine
        │
        ├── PyMuPDF text/layout extraction
        ├── Vendor parser
        ├── Spatial index / layout profiles
        ├── Accounting validation
        └── Portfolio Intelligence
                │
                ├── Property/Utility Master
                ├── Duplicate / Revision
                ├── Billing Continuity
                └── Amount / Consumption / Unit-Cost History
        │
        ▼
SQLite v4
```

## Portfolio identity model

The canonical internal project is not inferred by blindly truncating a project code.

`PROJ #` is treated as a source record. Its **first explicit project code** becomes the canonical internal project for that source row and remaining codes in the same cell are aliases.

Example:

```text
Source:
5093
5093-1 (Tower 1)
5093-2 (Tower 2)
5093-3 (TH)

Canonical project: 5093
Aliases: 5093, 5093-1, 5093-2, 5093-3
```

A row containing only `5164-10` remains `5164-10`; v4 does not invent `5164`.

If project `5462` appears on three source rows, `properties` holds one canonical entity and `property_locations` holds all three source locations.

## Utility identity

```text
properties(project_id)
   1
   └──< utility_accounts(vendor, canonical_account)
             1
             └──< utility_meters(canonical_meter)
```

`utility_accounts` enforces a unique `(vendor, account_number_canonical)` identity in the Workstation database.

## Invoice relationships

`invoice_relationships` keeps document relationships outside the invoice row:

- `EXACT_DUPLICATE`
- `POSSIBLE_REVISION`

This makes the source document immutable from an audit perspective while allowing operational relationships to evolve.

## Billing timeline

Invoices store both the source `billing_period` and parsed:

```text
billing_period_start
billing_period_end
```

Continuity checks operate on normalized dates. Identical periods are reserved for revision logic before overlap logic.

## Anomaly layer

`invoice_anomalies` stores normalized control signals separately from user-facing issues/warnings.

Examples:

```text
AMOUNT_ANOMALY_HIGH
CONSUMPTION_ANOMALY_HIGH
UNIT_COST_ANOMALY_HIGH
AMOUNT_CONSUMPTION_DIVERGENCE
BILLING_OVERLAP_HIGH
BILLING_GAP_HIGH
METER_MASTER_MISMATCH
UNMAPPED_UTILITY_ACCOUNT
PORTFOLIO_EXACT_DUPLICATE
POSSIBLE_REVISED_INVOICE
```

## Workstation vs future Department Edition

This source package remains a single-workstation SQLite application. The v4 schema deliberately separates business identity from UI state so a future Department Edition can move persistence to a central internal service/database without rewriting the parsing/validation core.
