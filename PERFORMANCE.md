# Complexity and Performance Analysis

Let:

- `N` = number of discovered PDF files
- `W_i` = number of extracted PDF words in invoice `i`
- `L_i` = number of reconstructed text lines in invoice `i`
- `M` = number of invoice records already stored in SQLite
- `H` = historical window size (bounded at 24 bills)
- `A` = number of anchor lines for a field (normally a tiny constant)
- `k` = number of nearby lines returned from a spatial bucket query (normally small)

## Directory discovery

The application discovers PDFs and sorts their paths for deterministic display:

- Discovery: `O(N)`
- Sorting: `O(N log N)`
- Memory: `O(N)` paths

For normal AP folders (hundreds to tens of thousands of invoices), PDF parsing dominates this cost.

## PDF extraction

PyMuPDF word extraction plus line reconstruction is approximately:

`O(W_i)` time and `O(L_i)` retained line memory per active PDF.

The application does not load all PDF contents into RAM. It processes one invoice at a time, so PDF-content working memory is approximately:

`O(max L_i)`

rather than `O(sum L_i)`.

## Two-pass parsing

Only the first three pages are parsed initially. Remaining pages are read only when critical fields, meter, or consumption are still missing.

For invoices whose important fields are on the first pages, work is proportional to those pages rather than the entire PDF. This is especially useful for long telecom statements.

## Regex parsing

The number of fields and regex patterns is fixed and small. With bounded, non-pathological expressions, parsing is approximately:

`O(L_i)` expected time.

## Spatial search: before vs now

A naive anchor/value implementation compares every line with every other line:

`O(L_i^2)` worst-case.

This version builds a page/Y-bucket index once:

- Index build: `O(L_i)`
- Anchor scan: `O(L_i)`
- Nearby lookup: `O(A * k)` expected

So spatial extraction is approximately:

`O(L_i + A*k)` → effectively `O(L_i)` for ordinary invoices.

## Layout-profile lookup

Vendor/field layout profiles have a primary key in SQLite. After a profile has at least three samples, the engine searches only a normalized page region.

- Profile DB lookup: indexed, approximately `O(log P)` where `P` is tiny
- Region search: `O(k)` expected after bucket indexing

Profiles therefore reduce candidate work and false positives on recurring vendor layouts.

## Historical anomaly checks

`invoices(vendor, account_number)` is indexed. The engine retrieves at most 24 prior bills:

- Indexed lookup: approximately `O(log M + H)`
- Median/MAD: `O(H log H)` because Python median sorts; `H <= 24`, so this is effectively constant
- Meter continuity: `O(H)`

## Logical duplicate detection

Canonical fingerprint construction is constant-size business data:

- Hash creation: `O(1)` with respect to PDF size
- Indexed fingerprint lookup: approximately `O(log M)`

This is intentionally cheaper than cryptographically hashing all PDF bytes, which would require `O(file_size)` I/O merely to determine duplicate identity.

## Incremental processing

For unchanged files the program checks path, size, and nanosecond modification time against SQLite, then loads the cached result.

A repeat run therefore costs approximately:

`O(N log M + sum W_changed)`

instead of reparsing every PDF.

In an AP workflow where 20,000 historical files exist but only 50 are new/changed, the expensive PDF parsing term applies only to those 50 files.

## Batch memory

The parser's PDF working set is one active document, but the GUI retains the selected file list and result objects for display/export:

`O(N + max L_i)` memory.

For very large archives (e.g. hundreds of thousands of files), the next scalability step would be UI pagination/virtualization and streaming CSV export; it is not necessary for ordinary clerk-level utility invoice batches.

## ETA

Estimated remaining time uses the median processing duration of the most recent 12 parsed invoices multiplied by the number remaining. Median is intentionally used instead of mean so that one unusually large statement does not make the ETA unstable.

## Low-spec Windows recommendations

- Keep OCR disabled unless truly necessary.
- Use the default single-document processing path; it avoids memory spikes and leaves CPU available for Excel/Outlook/accounting software.
- Put the SQLite DB and CSV on a local SSD when possible; copy/export later to a network drive if required.
- Leave incremental processing enabled.
- Use vendor profiles/history over adding general-purpose ML models.
