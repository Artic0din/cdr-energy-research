# Changelog

All notable changes to this research repo.

## [Unreleased]

- Added deterministic pull-request validation for Python sources and committed JSON research data.

### Changed
- Corrected Repo Assist to allow up to four single-task, issue-backed pull requests per run while retaining Graphite handoff, persistent memory, and bounded spending.
- Replaced Linear tracking rules with GitHub Issues and the user-level Development Project for local and Cursor Cloud agents.

### Added
- Initial repository structure with docs/, data/, scripts/, cache/
- v1 shape catalog (78 retailers, 10,266 plans, 1,724 signatures) at `docs/shape-catalog-v1.md`
- v2 sweep script using EME refdata2 (117 retailers) + comprehensive field probe at `scripts/cdr_full_sweep_v2.py`
- AER PDF (Jan 2026) authoritative retailer registry at `data/aer-base-uris-jan2026.pdf`
- AER fact sheet (Feb 2025) at `data/aer-fact-sheet-feb2025.docx`
- EME refdata2 snapshot at `data/eme-refdata.json` (117 orgs + 72 brokers)
- Defensive parser implementation contract at `docs/parser-spec.md`
- API operational reference at `docs/api-reference.md`
- 15 open CDS standards-maintenance Energy issues catalog at `docs/upcoming-changes.md`
- Registry source comparison (EME vs AER vs jxeeno vs ACCC) at `docs/registry-comparison.md`
- Operations runbook at `docs/operations.md`

### Findings
- 117 CDR-enrolled retailers (vs jxeeno's 78); 39 missing from prior probe
- 20 base URIs are shared across multiple brands (Energy Locals + 6 sub-brands; OVO Energy + 2 sub-brands; etc.)
- ACCC Register API is BROKEN for energy discovery per SM#561 (4 years unresolved)
- EME refdata2 is the richest metadata source (ABN, contacts, logos, bill URLs)
- All daily supply charges live at `tariffPeriod[0].dailySupplyCharge` (string, GST-EXCLUSIVE)
- 1,724 distinct shape signatures across 10,266 plans — extreme long tail
- EV-overlay plans (AGL Night Saver EV, Origin 360 EV, etc.) are mis-classified — workaround: detect zero rates in TOU plans
- Solar Sharer Offer (SSO) plans land 1 July 2026 with no spec value (SM#719)

## v2 sweep results (2026-05-16)

- 117/117 retailers reachable (vs 78 in v1)
- 17,779 plans fetched (+73% vs v1)
- 5,087 distinct shape signatures (3× v1's 1,724)
- 148 EV-overlay candidates (zero-rate within TOU plans) — SM#710 concrete count
- 0 failures across 17,779 plan detail fetches
- 7.7 min wall-clock (cache-warm)
- 167 MB cache

### Enum surprises (vs spec)

- `discounts[].type` spec value `OTHER` **NEVER used** in 3,450 obs
- `discounts[].methodUType` spec value `percentOverThreshold` **NEVER used**
- `discounts[].category` spec value `GUARANTEED_DISCOUNT` **NEVER used**
- `fees[].term` 10 of 13 spec values **NEVER used** (only FIXED, PERCENT_OF_BILL, ANNUAL observed)
- `fees[].type` rare values rarely used: EXIT, ESTABLISHMENT, MEMBERSHIP, CONTRIBUTION
- AGL has **3 separate org records** (AGL Sales, AGL Retail Energy, AGL) all sharing `/agl/` cdrCode
- Origin has 3 org records (Electricity, LPG, Retail Limited)
- Energy Locals has 4 org records (incl. EL Retail Energy with multiple variations)
