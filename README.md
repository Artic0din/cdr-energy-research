# CDR Energy Research

Comprehensive research and tooling for the Australian Consumer Data Right (CDR) Energy Product Reference Data (PRD) APIs. Used to power [PriceHawk](../pricehawk/) (Home Assistant integration) and inform other energy projects.

## What's here

```
cdr-energy-research/
├── README.md                      this file
├── CHANGELOG.md
├── docs/
│   ├── shape-catalog-v1.md        first sweep (78 retailers, 10,266 plans, 1,724 sigs)
│   ├── shape-catalog-v2.md        comprehensive sweep (117 retailers, all fields)
│   ├── parser-spec.md             defensive parser implementation contract
│   ├── api-reference.md           endpoints, query params, headers, errors
│   ├── enums-reference.md         spec enum values vs observed (with ⚠ flags)
│   ├── upcoming-changes.md        15 open CDS standards-maintenance Energy issues
│   ├── registry-comparison.md     EME refdata2 vs AER PDF vs jxeeno vs ACCC
│   ├── operations.md              runbook for re-running sweeps
│   └── brand-assets.md            logo cache layout + brand lookup HOWTO
├── data/
│   ├── aer-base-uris-jan2026.pdf       authoritative retailer registry
│   ├── aer-fact-sheet-feb2025.docx     AER guide
│   ├── eme-refdata.json                Energy Made Easy refdata2 snapshot
│   ├── retailer-index.json             merged retailer index (script output)
│   ├── registry-comparison.json        registry diff (script output)
│   └── logos/                          189 retailer/broker logos + _manifest.json
├── scripts/
│   ├── cdr_probe_v1.py            initial sample probe (5 plans/retailer)
│   ├── cdr_full_sweep_v1.py       first full sweep
│   └── cdr_full_sweep_v2.py       comprehensive sweep w/ EME refdata2 + ?brand=
└── cache/                         v1 symlink + v2 dir → /tmp (ephemeral, ~111.5 MB after a full v2 run; regenerate with scripts/cdr_full_sweep_v2.py)
```

## TL;DR — where to start

1. **Read `docs/shape-catalog-v2.md`** for the comprehensive shape inventory.
2. **Read `docs/parser-spec.md`** for the implementation contract.
3. **Read `docs/upcoming-changes.md`** for what to watch (especially Solar Sharer Offer landing 1 July 2026).
4. **Read `docs/api-reference.md`** for the operational HOWTO.
5. **Run `scripts/cdr_full_sweep_v2.py`** to refresh data.

## Key endpoints

| Purpose | Endpoint | Headers | Notes |
|---|---|---|---|
| Retailer registry | `GET https://api.energymadeeasy.gov.au/refdata2?keys=organisations,thirdParties` | none | 117 orgs + 72 brokers, no auth |
| Plan list | `GET {base}/cds-au/v1/energy/plans?fuelType=ELECTRICITY&type=ALL&effective=CURRENT&page-size=1000&brand={cdrBrand}&updated-since={iso}` | `x-v: 1` | `brand={cdrBrand}` (NOT `cdrCode`) for shared endpoints; `updated-since=` for incremental |
| Plan detail | `GET {base}/cds-au/v1/energy/plans/{planId}` | `x-v: 3` | v2 retired Mar 2025 |

`{base}` is `cdr.energymadeeasy.gov.au/<cdrCode>`. AER PDF is the authoritative source; **20 unique base URIs are SHARED across multiple brands** (Energy Locals hosts ARCLINE / Cooperative / RAA / Sonnen / Indigo etc; OVO Energy hosts MYOB OVO + OVO Energy + OVO Energy CTM). Co-hosted brands share one `cdrCode` but each has a distinct `cdrBrand`, so disambiguate with `?brand=<cdrBrand>` — filtering by `cdrCode` returns every co-hosted brand's plans.

## Headline findings from the sweeps

- **117 CDR-enrolled retailers** (vs 78 in jxeeno's GitHub registry)
- **20 shared base URIs** — multiple brands share endpoints; `?brand=<cdrBrand>` disambiguates (co-hosted brands share a `cdrCode`, so `cdrBrand` is the distinguishing filter)
- **10,266 residential ELEC plans** observed in v1 sweep (78 retailers)
- **1,724 distinct shape signatures** — extreme long tail; top 30 sigs cover only 13% of plans
- **0 retailers 404 on plan detail** when listed — reliability is excellent
- **`tariffPeriod[0].dailySupplyCharge` is the ONLY observed location for daily charges** — other 3 spec locations are 0/10,266
- **`tariffPeriod[0].dailySupplyCharge` is GST-EXCLUSIVE per spec** — most other AmountStrings are GST-inclusive
- **EV-overlay plans (AGL Night Saver EV, Origin 360 EV, Red EV Saver, Three for Free SA) are mis-classified** — they ship as TIME_OF_USE_CONT_LOAD with zero-priced rate rows; spec issue [SM#710](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/710) tracks the gap
- **Solar Sharer Offer (SSO) plans land 1 July 2026** — government-mandated zero-cost consumption window with daily volume cap, no spec value yet ([SM#719](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/719))
- **`fit0.scheme=OTHER` IS spec-valid** (despite informal reports otherwise)
- **`incentives.category` enum is `GIFT | ACCOUNT_CREDIT | OTHER`** (not the often-cited DISCOUNT/BONUS/OTHER)

## Sources

| Source | URL | Use |
|---|---|---|
| AER PDF (authoritative registry) | `aer.gov.au/documents/consumer-data-right-energy-retailer-base-uris-and-cdr-brands` | Truth source for brand → base URI mapping |
| EME refdata2 (richest metadata) | `api.energymadeeasy.gov.au/refdata2?keys=organisations,thirdParties` | 117 orgs, ABNs, contacts, logos, bill URLs |
| CDS spec (canonical schema) | `consumerdatastandardsaustralia.github.io/standards/#cdr-energy-api_get-generic-plan-detail` | Field types, enums, mandatory/optional |
| CDS standards-maintenance | `github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues?q=label%3A%22Energy%22` | In-flight schema changes |
| jxeeno community scrape | `github.com/jxeeno/energy-cdr-prd-endpoints` | Auto-updated weekly; **drifts from AER**; uses ACCC Register + EME refdata2 |
| ACCC Register API | `api.cdr.gov.au/cdr-register/v1/energy/data-holders/brands/summary` | Per [SM#561](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/561), unreliable for energy — `publicBaseUri` flips between PRD and outage URIs based on retailer enrollment status |

## Operational notes

- API is **public**, no auth required for PRD (per AER fact sheet).
- Polite usage: **1 req/sec per retailer**, 12-way parallel across retailers is fine.
- v1 sweep took **33.5 min** for 78 retailers / 10,266 plans on a residential connection.
- Cache hits make re-runs free. Use `?updated-since=<iso>` for incremental sync (5 min instead of 33 min).
- Spec versions: plan list = v1 (`x-v: 1`), plan detail = v3 (`x-v: 3`). v2 retired March 2025.

## Status

**Paused.** Last sweep and findings update: 2026-05-16.
**Private research repo.** Not for public publication without legal review (AER/CDR data is public but operational details should be reviewed).
