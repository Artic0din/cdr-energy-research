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
- Shared base URIs: 20 base URIs serve multiple brands — always pass `?brand=<cdrCode>` on shared endpoints.
- `tariffPeriod[0].dailySupplyCharge` is GST-EXCLUSIVE; most other AmountStrings are GST-inclusive. Do not "fix" this asymmetry.

## Data and cache

- `cache/` points into `/tmp` and is ephemeral — never rely on it existing; regenerate with `scripts/cdr_full_sweep_v2.py` (cold run ≈ 1 h 40 m, warm minutes).
- `docs/shape-catalog-v2.md`, `docs/enums-reference.md`, `data/retailer-index.json`, `data/registry-comparison.json`, `data/eme-refdata.json` are script outputs — regenerate, don't hand-edit.
- `data/aer-base-uris-jan2026.pdf` and `data/aer-fact-sheet-feb2025.docx` are authoritative data inputs — never move or delete.
