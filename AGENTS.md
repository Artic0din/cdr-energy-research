# AGENTS.md — cdr-energy-research

Repo-specific rules only.
Universal rules live in `~/agent-hub/AGENTS.md` and apply here in full.

## What this repo is

Private research repo for Australian CDR Energy Product Reference Data APIs.
Downstream consumer: PriceHawk (`~/Development/pricehawk/`).
Not for public publication without legal review.

## API rules

- PRD API is public, no auth — but be polite: max 1 request/second per retailer; 12-way parallelism across retailers is acceptable.
- Spec versions are load-bearing: plan list uses `x-v: 1`, plan detail uses `x-v: 3` (v2 retired March 2025). Verify before changing.
- Shared base URIs: from the committed `data/eme-refdata.json`, **3 base URIs serve multiple brands (9 brands total share an endpoint)** — always pass a brand filter on shared endpoints. Use `?brand=<cdrBrand>`, NOT `cdrCode`: on a shared base every co-hosted brand carries the *same* `cdrCode` (e.g. Indigo, Cooperative, RAA and Energy Locals all have `cdrCode=energy-locals`) but a *distinct* `cdrBrand` (`indigo`, `cooperative`, `raa`, `energy-locals`). Only `cdrBrand` disambiguates the brands sharing one endpoint; filtering by `cdrCode` would return every co-hosted brand's plans. The three shared bases are `energy-locals` (4 brands), `ovo-energy` (3), `future-x` (2). `build_retailer_list` dedupes by `cdrBrand`, so the sweep yields **109 retailers across 103 unique base URIs**.
- `tariffPeriod[0].dailySupplyCharge` is GST-EXCLUSIVE; most other AmountStrings are GST-inclusive. Do not "fix" this asymmetry.

## Data and cache

- `cache/` points into `/tmp` and is ephemeral — never rely on it existing; regenerate with `scripts/cdr_full_sweep_v2.py` (cold run ≈ 1 h 40 m, warm minutes).
- `docs/shape-catalog-v2.md`, `docs/enums-reference.md`, `data/retailer-index.json`, `data/registry-comparison.json`, `data/eme-refdata.json` are script outputs — regenerate, don't hand-edit.
- `data/aer-base-uris-jan2026.pdf` and `data/aer-fact-sheet-feb2025.docx` are authoritative data inputs — never move or delete.
