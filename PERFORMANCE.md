# v4 Complexity and Performance Analysis

Let:

- `N` = number of PDFs in a batch
- `W` = words extracted from one PDF
- `L` = reconstructed text lines
- `M` = invoices already indexed in SQLite
- `P` = canonical properties in the portfolio
- `A` = utility accounts
- `H` = bounded historical observations (default <= 36)

## PDF extraction

PyMuPDF extraction and line reconstruction are approximately linear in document content:

```text
O(W)
```

This remains the dominant CPU cost for born-digital utility invoices.

## Spatial extraction

v2-style all-lines geometric comparison can approach `O(L²)`.

v3/v4 retain the page/Y-bucket spatial index:

```text
index build: O(L)
local anchor query: O(k) expected
```

where `k` is the small number of lines in nearby buckets.

## Portfolio master import

For `R` PM CSV source rows and `Q` aliases/postal/location records:

```text
O(R + Q)
```

SQLite indexed inserts are effectively bounded by B-tree `O(log P)` operations. Import is not performed per invoice.

## Property lookup

Vendor/account mapping uses the unique SQLite index:

```text
(vendor, account_number_canonical)
```

Lookup:

```text
O(log A)
```

Project alias lookup:

```text
O(log P)
```

Path-based property suggestion checks aliases only for unmapped accounts. It is intentionally a fallback path; mapped accounts use the direct utility-account index.

## Portfolio duplicate / revision

Exact logical fingerprint lookup uses an indexed hash:

```text
O(log M)
```

Revision candidate lookup uses indexed vendor/account plus billing identity and is approximately:

```text
O(log M + r)
```

where `r` is a very small set of same-account/same-period candidates.

## Billing continuity

Historical billing periods are bounded (`H <= 36`):

```text
O(H)
```

No unbounded scan of the entire invoice table is required.

## Historical anomalies

Amount, consumption and unit-cost histories are bounded to `H <= 36`.

Median/MAD on each list:

```text
O(H log H)
```

Since `H` is a small constant in practice, this is negligible compared with PDF parsing.

## Incremental processing

Unchanged files are detected using path + file size + nanosecond mtime and are loaded from SQLite rather than reparsed.

A warm run therefore approaches:

```text
O(N log M)
```

for metadata/index lookups, while a cold/changed run is dominated by:

```text
O(sum(W_changed))
```

## Memory

The engine streams one invoice at a time:

```text
read → parse → validate → save → release
```

Therefore PDF working memory is approximately:

```text
O(W_max)
```

rather than `O(N × W)`.

The property master and histories remain in SQLite; the application does not load all historical invoices into RAM.

## ETA

The GUI uses the median of recent actual processing times multiplied by remaining items. Median smoothing prevents one unusually large statement from destabilizing the estimate.

## Low-spec workstation guidance

Recommended default:

```text
Balanced / one processing stream
```

The additional v4 database checks are cheap relative to PDF extraction. The largest performance risk remains enabling local OCR on image-only PDFs; OCR is intentionally outside the default v4 processing path.
