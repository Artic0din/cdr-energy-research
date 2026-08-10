# Registry Sources — Comparison

Four sources can theoretically tell you "what retailer base URIs exist for the AU CDR Energy PRD". They disagree.

## Source comparison

| Source | URL | Format | Update freq | Coverage | Reliability for energy |
|---|---|---|---|---|---|
| **AER PDF** (authoritative) | `aer.gov.au/documents/consumer-data-right-energy-retailer-base-uris-and-cdr-brands` | PDF, manual | monthly | ~70 unique base URIs covering ~85 brands | ⭐⭐⭐⭐⭐ Truth source |
| **EME refdata2** (richest metadata) | `api.energymadeeasy.gov.au/refdata2?keys=organisations,thirdParties` | JSON, API | live? | 117 orgs (39 more than jxeeno) + 72 brokers | ⭐⭐⭐⭐ Best for org metadata |
| **jxeeno community** | `raw.githubusercontent.com/jxeeno/energy-cdr-prd-endpoints/main/docs/energy-prd-endpoints.json` | JSON | weekly auto-update | 78 brands | ⭐⭐⭐ Drifts from AER; 2 known base-URI bugs |
| **ACCC Register API** | `api.cdr.gov.au/cdr-register/v1/energy/data-holders/brands/summary` | JSON, API | live | depends on retailer enrollment | ⭐ **BROKEN for energy** per SM#561 |

## Why the ACCC Register API is broken for energy

Per [SM#561](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/561) (open since Dec 2022, still unresolved March 2026):

> AER / the Victorian agency are the data holder for Energy CDR PRD, whereas the provider is the data holder for consumer data requests. The `publicBaseUri` field returns DIFFERENT VALUES depending on the provider's status:
> - When provider is **active** for consumer data requests → returns provider's status/outage endpoint
> - When provider is **inactive** → returns the PRD endpoint at `cdr.energymadeeasy.gov.au`
>
> Under Energy CDR rules, providers are NOT required to implement product reference data endpoints.

→ **Don't use the ACCC Register API for energy PRD discovery.**

## Why jxeeno's registry drifts from AER

jxeeno's resolution priority for `productReferenceDataBaseUri`:
1. ACCC Register API `publicBaseUri` (only works for inactive retailers)
2. EME refdata2 API → constructs `cdr.energymadeeasy.gov.au/<cdrCode>`
3. Hardcoded fallback in `src/hardcode.js`

Because step 2 builds individual `/<cdrCode>/` URIs from EME refdata2 records, but AER PDF reflects ACTUAL endpoint registrations (which sometimes consolidate brands onto a SHARED endpoint), jxeeno's registry sometimes lists per-brand URIs that don't exist or don't return what's expected.

### Known jxeeno registry bugs (vs AER PDF Jan 2026)

| Brand | jxeeno says | AER says |
|---|---|---|
| ARCLINE by RACV | `/arcline/` | `/energy-locals/` |
| iO Energy Retail Services | `/io-energy/` | `/energy-locals/` |

→ **Use AER PDF or EME refdata2 + manual dedup, NOT jxeeno's registry as gospel.**

## Why EME refdata2 has 117 orgs vs AER's ~70 unique base URIs

EME refdata2 lists EVERY retailer org including ones that:
- Share a base URI with another brand (e.g. ARCLINE → energy-locals' base)
- Are not yet active (orgStatus may indicate)
- Are dormant or business-only

AER PDF de-facto lists base URIs (with brand annotations); EME lists brands (with rich metadata). Use both:

- **EME refdata2 → org metadata** (ABN, contact, logo, billing URL, retailer code, trading name)
- **AER PDF → canonical brand-to-base-URI map** (especially for shared endpoints)

## Brand-to-base-URI sharing (AER PDF, observed)

20 unique base URIs host multiple brands. The biggest:

### `cdr.energymadeeasy.gov.au/energy-locals/`
- ARCLINE by RACV (cdrBrand=arcline)
- Cooperative Power (cdrBrand=cooperative)
- Energy Locals Retail (cdrBrand=energy-locals)
- RAA Energy (cdrBrand=raa)
- iO Energy Retail Services (cdrBrand=io-energy)
- Indigo Power (cdrBrand=indigo)
- Sonnen (cdrBrand=sonnen)

### `cdr.energymadeeasy.gov.au/ovo-energy/`
- MYOB powered by OVO (cdrBrand=myob)
- OVO Energy (cdrBrand=ovo-energy)
- OVO Energy for CTM (cdrBrand=ovo-energy-ctm)

### `cdr.energymadeeasy.gov.au/radian/`
- iO Energy (cdrBrand=io-energy)
- Radian Energy (cdrBrand=radian)

### `cdr.energymadeeasy.gov.au/future-x/`
- Future X Power (cdrBrand=future-x)
- Future X Power (cdrBrand=sunswitch)

→ When fetching from a shared base URI, use the `brand=<cdrBrand>` query
parameter to disambiguate plans.

## Recommended discovery strategy for PriceHawk

```python
def discover_retailers() -> list[Retailer]:
    """Build the canonical retailer index for PriceHawk."""
    # 1. Pull EME refdata2 for rich metadata
    refdata = fetch_eme_refdata()  # api.energymadeeasy.gov.au/refdata2
    orgs = refdata["data"]["organisations"]

    # 2. Build base URI from cdrCode
    retailers = []
    for org_id, o in orgs.items():
        if not o.get("cdrCode"):
            continue
        retailers.append(Retailer(
            cdr_code=o["cdrCode"],
            cdr_brand=o.get("cdrBrand"),
            base_uri=f"https://cdr.energymadeeasy.gov.au/{o['cdrCode']}",
            display_name=o.get("tradingName") or o.get("orgName"),
            abn=o.get("abn"),
            website=o.get("websiteURL"),
            bill_url=o.get("electricityBillURL"),
            logo=f"https://energymadeeasy.gov.au{o['logo']}" if o.get("logo") else None,
            contact=o.get("residentialContact"),
        ))

    # 3. Override base URIs from AER PDF where they conflict (e.g. ARCLINE → /energy-locals/)
    aer_overrides = parse_aer_pdf("data/aer-base-uris-jan2026.pdf")
    for r in retailers:
        if r.cdr_brand in aer_overrides:
            r.base_uri = aer_overrides[r.cdr_brand]

    return retailers
```

## Refresh cadence

| Source | Recommended refresh |
|---|---|
| AER PDF | monthly (manual download to `data/`) |
| EME refdata2 | weekly (or daily for production) |
| jxeeno registry | unused (replaced by AER + EME) |
| ACCC Register | unused (broken for energy) |
| Plan list per retailer | use `?updated-since=` for incremental sync; full re-list weekly |
| Plan detail per planId | on demand; cache aggressively (planId is permanent per ID-Permanence rules) |
