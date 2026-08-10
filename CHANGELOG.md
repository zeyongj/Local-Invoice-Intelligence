# Changelog

## 4.0.0 — Portfolio Edition

- Added Property × Vendor × Account × Meter Master.
- Added bundled offline `data/pm.csv` portfolio seed.
- Added source-aware canonical project normalization.
- Enforced v4 internal rule: 5093-1 / 5093-2 / 5093-3 resolve to canonical project 5093.
- Added `property_locations` so repeated project IDs on multiple PM CSV rows do not overwrite source locations.
- Added manual utility-account-to-project mapping UI.
- Added meter master and mismatch review control.
- Added portfolio-wide exact duplicate relationship tracking.
- Added possible revised/reissued invoice relationships.
- Added billing period ISO normalization.
- Added billing overlap and gap controls.
- Added amount, consumption and effective-unit-cost Median/MAD anomaly controls.
- Added amount-consumption divergence signal.
- Added portfolio flags/anomaly persistence.
- Added v4 CSV output fields.
- Updated Windows GUI with canonical Project column and Portfolio Master window.
- Updated CLI with portfolio support.
- SQLite schema version 4 with v2/v3 migration path retained.

## 3.x

See v3 history in prior release.
