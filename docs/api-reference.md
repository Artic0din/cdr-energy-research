# CDR Energy PRD API Reference

Operational reference for the Australian Consumer Data Right Energy Product Reference Data APIs. No auth required.

## Base URIs

Format: `https://cdr.energymadeeasy.gov.au/<cdrCode>`

Authoritative source: AER PDF (`data/aer-base-uris-jan2026.pdf`). Updated monthly. **Do NOT use the ACCC Register API** — its `publicBaseUri` field is unreliable for energy (flips between PRD and status/outage URIs based on retailer's CDR enrollment status; see [SM#561](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/561)).

Richest metadata source: EME refdata2 (`https://api.energymadeeasy.gov.au/refdata2?keys=organisations,thirdParties`). Returns 117 organisations + 72 third parties with `cdrCode, cdrBrand, tradingName, orgName, abn, websiteURL, electricityBillURL, gasBillURL, logo, retailerCode, residentialContact, smallBusinessContact, orgStatus, orgId`.

### Shared base URIs

Three constructed base URIs in the committed EME snapshot host multiple CDR brands:

| Base URI | Brands hosted |
|---|---|
| `/energy-locals/` | Cooperative Power, Energy Locals Retail, RAA Energy, Indigo Power |
| `/ovo-energy/` | MYOB powered by OVO, OVO Energy, OVO Energy for CTM |
| `/future-x/` | Future X Power (`cdrBrand=future-x`), Sunswitch (`cdrBrand=sunswitch`) |

→ When fetching from a shared endpoint, **use `?brand=<cdrBrand>`** to filter to one brand's plans.

## Endpoints

### `GET /cds-au/v1/energy/plans` — list all generally-available plans

| Param | In | Type | Default | Notes |
|---|---|---|---|---|
| `type` | query | enum | `ALL` | `STANDING \| MARKET \| REGULATED \| ALL` |
| `fuelType` | query | enum | `ALL` | `ELECTRICITY \| GAS \| DUAL \| ALL` |
| `effective` | query | enum | `CURRENT` | `CURRENT \| FUTURE \| ALL` — filters by effectiveFrom/To |
| **`updated-since`** | query | DateTimeString | (none) | **incremental sync key** — only plans modified after this datetime |
| **`brand`** | query | string | (none) | **filter by `cdrBrand`** — solves shared-endpoint disambiguation |
| `page` | query | positive int | 1 | 1-indexed |
| `page-size` | query | positive int | 25 | Tested: 1000 works |
| **`x-v`** | header | string | — | **mandatory: `1`** for plans list |
| `x-min-v` | header | string | (none) | min acceptable version (server returns highest in [min, x-v]) |

Response (top-level):
```json
{
  "data": {"plans": [{"planId", "displayName", "type", "fuelType", "brand", "brandName",
                       "customerType", "geography": {...}, "additionalInformation": {...}, ...}]},
  "links": {"self", "first", "prev", "next", "last"},
  "meta": {"totalRecords": N, "totalPages": M}
}
```

### `GET /cds-au/v1/energy/plans/{planId}` — full plan detail

| Param | In | Type | Notes |
|---|---|---|---|
| `planId` | path | EnergyPlanId | mandatory |
| **`x-v`** | header | string | **mandatory: `3`** (v2 RETIRED 3 March 2025) |
| `x-min-v` | header | string | optional |

Response:
```json
{
  "data": { ... full EnergyPlan with electricityContract / gasContract ... },
  "links": {"self": "..."},
  "meta": {}
}
```

Plan detail can ALSO be invoked as `GET /cds-au/v1/energy/plans?planId={id}` per AER fact sheet.

## HTTP errors (CDR standard envelope)

| Status | Codes | Meaning |
|---|---|---|
| 200 | — | OK |
| 400 | Invalid Field, Missing Required Field, Missing Required Header, Invalid Version, Invalid Page Size | Bad Request |
| 404 | Unavailable Resource, Invalid Resource | Not Found |
| 406 | Unsupported Version | When no version in `[x-min-v, x-v]` is supported by server |
| 422 | various | Unprocessable Entity |

Error envelope: `ResponseErrorListV2`:
```json
{"errors": [{"code": "...", "title": "...", "detail": "...", "meta": {}}]}
```

## Rate limiting

- AER endpoints don't publish formal rate limits but respond cleanly under one request per second per base URI.
- Twelve-way parallelism across different base URIs tested fine.
- AEMO secondary data holder (consumer-data side) has stricter limits — see [SM#651](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/651) for proposed `429 + Retry-After` passthrough.

## Versioning

| Endpoint | Current | Deprecated |
|---|---|---|
| GetGenericPlans | v1 | (none — stays v1 even though detail moved to v3) |
| GetGenericPlanDetail | v3 | v2 retired 3 March 2025; v1 obsolete |

## Common field types

| Type | Format |
|---|---|
| `AmountString` | Dollar amount as string. Can be negative (account credit). Spec doesn't mandate decimal precision. |
| `RateString` | Percentage as string. Format ambiguous in spec; observed values look like decimal fractions (e.g. `"0.05"` = 5%). |
| `DateString` | `YYYY-MM-DD` |
| `DateTimeString` | ISO 8601 with timezone (e.g. `2026-05-16T10:30:00+10:00`) |
| `URIString` | URI per RFC 3986 |
| `ASCIIString` | ASCII-only string |
| `EnergyPlanId` | Free-form unique ID per ID-Permanence rules. Examples: `AGD739070MR@VEC`, `ORI909049MRE7@EME` — no enforced format. |
| `ExternalRef` | Often ISO 8601 duration (excluding recurrence syntax) |

### GST handling — IMPORTANT

Per spec (and [SM#474](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/474)):
- `tariffPeriod[].dailySupplyCharge` is **EXCLUSIVE OF GST** ("in dollars per day exclusive of GST")
- Most other AmountStrings are **GST-INCLUSIVE**
- The 10% GST adjustment is your responsibility for display.

## Sample queries

### Pull all of AGL's residential electricity plans (current)
```
GET https://cdr.energymadeeasy.gov.au/agl/cds-au/v1/energy/plans?fuelType=ELECTRICITY&type=ALL&effective=CURRENT&page-size=1000
Headers: x-v: 1
```

### Pull only ARCLINE plans from the shared Energy Locals endpoint
```
GET https://cdr.energymadeeasy.gov.au/energy-locals/cds-au/v1/energy/plans?brand=arcline&fuelType=ELECTRICITY&page-size=1000
Headers: x-v: 1
```

### Incremental sync — only plans changed since last poll
```
GET https://cdr.energymadeeasy.gov.au/agl/cds-au/v1/energy/plans?fuelType=ELECTRICITY&updated-since=2026-05-15T00:00:00+10:00
Headers: x-v: 1
```

### Single plan detail
```
GET https://cdr.energymadeeasy.gov.au/agl/cds-au/v1/energy/plans/AGD739070MR@VEC
Headers: x-v: 3
```

## ID Permanence

Per CDR ID Permanence rules:
- `planId` is **stable across plan content updates**
- `lastUpdated` changes when plan content changes
- When a plan is retired, `effectiveTo` is set to a past datetime
- ID re-use is forbidden — once a plan is retired, its planId is gone forever
