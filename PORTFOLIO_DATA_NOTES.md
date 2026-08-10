# Portfolio Data Notes

## Source

Bundled snapshot:

```text
data/pm.csv
```

Source URL supplied for this build:

```text
https://raw.githubusercontent.com/zeyongj/zeyongj.github.io/refs/heads/main/data/pm.csv
```

The runtime application does not fetch this URL. The file is bundled locally.

## Canonical internal-project rule

For each CSV source row, project codes are read in source order.

The first explicit code is the canonical internal project. Remaining codes in the same `PROJ #` cell are aliases to that project.

### Required example

```text
5093
5093-1 (7680 Tower 1)
5093-2 (7760 Tower 2)
5093-3 (7700 TH)
```

becomes:

```text
canonical project = 5093
5093-1 -> 5093
5093-2 -> 5093
5093-3 -> 5093
```

This reflects the internal project-number rule supplied for v4.

## No blind suffix stripping

A source row containing only:

```text
5164-10
```

remains `5164-10` because the source does not explicitly provide `5164` as the canonical project.

## Repeated project numbers on multiple rows

The source currently contains repeated canonical project numbers with different address rows. v4 therefore uses:

```text
properties             one canonical internal project
property_locations     one or more source rows/locations
```

This prevents later source rows from overwriting earlier locations.

## Raw data retention

`properties` / `property_locations` retain the original project and property-name text so normalization remains auditable and reversible.
