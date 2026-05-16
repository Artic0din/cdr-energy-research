# Defensive Parser — Implementation Contract

Target: Python function consuming a CDR `EnergyPlanDetailV2` response and emitting UI-ready strings + structured data for PriceHawk's plan-summary card. Must handle the union of all observed shapes across 117 retailers / 10,266+ plans.

## Function signature

```python
from decimal import Decimal
from typing import TypedDict, Literal

class PlanSummary(TypedDict):
    plan_id: str
    brand_id: str
    brand_name: str
    display_name: str
    plan_type: Literal["MARKET", "STANDING", "REGULATED"]
    customer_type: Literal["RESIDENTIAL", "BUSINESS"] | None

    # Pricing
    pricing_model: str           # SINGLE_RATE | TIME_OF_USE | ... | UNKNOWN
    is_fixed: bool | None        # fixed-vs-variable
    cooling_off_days: int | None
    term_type: str | None        # 1_YEAR / ONGOING / OTHER ...
    bill_frequency: list[str]    # ISO 8601 durations like ["P1M"]
    payment_options: list[str]   # PAPER_BILL / DIRECT_DEBIT / ...
    meter_types: list[str]       # ["Type 4", "Type 5", ...]

    # Daily supply
    supply_c_per_day_excl_gst: Decimal | None
    supply_c_per_day_incl_gst: Decimal | None  # = excl * 1.10

    # Tariff windows (rendered)
    tariff_windows: list[dict]   # [{type, days, start, end, rate_excl_gst, rate_incl_gst, measure_unit}]

    # Solar FIT (multi-tier)
    fit_tiers: list[dict]        # [{scheme, payer_type, rate, period, time_window?}]

    # Sub-lists
    incentives: list[dict]       # [{display_name, description, category, eligibility}]
    discounts: list[dict]        # [{display_name, type, category, method, value}]
    fees: list[dict]             # [{type, term, amount, rate, description}]
    green_power_tiers: list[dict]  # [{type, scheme, percent_green, amount}]
    eligibility: list[dict]      # [{type, information, description}]
    controlled_load: list[dict]  # CL sub-summaries (different schema from main TP!)

    # Geography
    distributors: list[str]
    included_postcodes: list[str]  # may include ranges like "3000-3999"
    excluded_postcodes: list[str]

    # Deep links
    application_uri: str | None
    overview_uri: str | None
    terms_uri: str | None
    eligibility_uri: str | None
    pricing_uri: str | None
    bundle_uri: str | None

    # Freshness
    last_updated: str | None      # ISO 8601
    effective_from: str | None
    effective_to: str | None

    # Badges (derived)
    is_ev_overlay: bool           # zero rates in TOU — SM#710 workaround
    is_sso_candidate: bool        # description contains SSO markers — SM#719 workaround
    has_intrinsic_green: bool
    intrinsic_green_pct: Decimal | None

    # Free text
    terms: str | None
    variation: str | None         # mandatory if isFixed=False
    on_expiry_description: str | None
    additional_fee_information: str | None

    # Parser meta
    warnings: list[str]
    spec_version: str             # "v3"


def summarise_cdr_plan(detail: dict) -> PlanSummary:
    """..."""
```

## Implementation rules

### 1. Envelope

- `detail["data"]` may be missing → return `PlanSummary(plan_id=None, warnings=["envelope missing"])` and bail.
- `detail["data"]["electricityContract"]` may be missing for non-electricity plans (gas-only) → set pricing fields to None, return what's available.

### 2. Plan-level metadata (always present)

| Output field | Source | Notes |
|---|---|---|
| `plan_id` | `data.planId` | mandatory per spec |
| `brand_id` | `data.brand` | ASCIIString |
| `brand_name` | `data.brandName` | display name |
| `display_name` | `data.displayName` | optional, may be None |
| `plan_type` | `data.type` | mandatory: STANDING / MARKET / REGULATED |
| `customer_type` | `data.customerType` | optional, default = available to all |
| `last_updated` | `data.lastUpdated` | mandatory DateTimeString |
| `effective_from` / `effective_to` | `data.effectiveFrom` / `effectiveTo` | optional |
| `application_uri` | `data.applicationUri` | optional |
| `overview_uri` etc. | `data.additionalInformation.{overviewUri, termsUri, eligibilityUri, pricingUri, bundleUri}` | all optional |
| `distributors` | `data.geography.distributors` | mandatory list, ≥1 entry |
| `included_postcodes` / `excluded_postcodes` | `data.geography.{includedPostcodes, excludedPostcodes}` | optional; may contain ranges like "3000-3999" |

### 3. Contract-level

| Output field | Source | Notes |
|---|---|---|
| `pricing_model` | `electricityContract.pricingModel` | mandatory enum, default `"UNKNOWN"` |
| `is_fixed` | `electricityContract.isFixed` | mandatory bool |
| `cooling_off_days` | `electricityContract.coolingOffDays` | mandatory if plan_type=MARKET |
| `term_type` | `electricityContract.termType` | optional: 1_YEAR/2_YEAR/.../ONGOING/OTHER |
| `bill_frequency` | `electricityContract.billFrequency` | mandatory list of ISO 8601 durations |
| `payment_options` | `electricityContract.paymentOption` | mandatory list |
| `meter_types` | `electricityContract.meterTypes` | optional list, e.g. ["Type 4", "Type 5"] |
| `terms` | `electricityContract.terms` | optional free text |
| `variation` | `electricityContract.variation` | mandatory if isFixed=False |
| `on_expiry_description` | `electricityContract.onExpiryDescription` | optional |
| `additional_fee_information` | `electricityContract.additionalFeeInformation` | optional |
| `intrinsic_green_pct` | `electricityContract.intrinsicGreenPower.greenPercentage` | optional, RateString → Decimal |

### 4. Daily supply charge — definitive single location

```python
tp = ec.get("tariffPeriod") or []
if tp and isinstance(tp[0], dict):
    raw = tp[0].get("dailySupplyCharge")
    if raw:
        supply_excl = Decimal(str(raw))
        supply_incl = supply_excl * Decimal("1.10")  # GST per SM#474
```

→ **Do NOT check the other 3 spec locations** (`ec.dailySupplyCharges` plural, `ec.dailySupplyCharge` singular, `tariffPeriod[0].dailySupplyCharges` plural) — 0/10,266 plans use them. Defensive code = fallback to None and emit warning.

### 5. Tariff windows (TOU) — render to user-friendly bands

```python
for tp in ec.get("tariffPeriod", []):
    rbut = tp.get("rateBlockUType")
    if rbut == "singleRate":
        blk = tp.get("singleRate", {})
        # Single rate: one tariff window covering all hours
        for rate in blk.get("rates", []):
            tariff_windows.append({
                "type": "FLAT",
                "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
                "start": "00:00", "end": "24:00",
                "rate_excl_gst": Decimal(str(rate["unitPrice"])),
                "rate_incl_gst": Decimal(str(rate["unitPrice"])),  # already GST-incl per SM#474
                "measure_unit": rate.get("measureUnit", "KWH"),
            })
    elif rbut == "timeOfUseRates":
        for band in tp.get("timeOfUseRates", []):  # always a list
            for window in band.get("timeOfUse", []):
                for rate in band.get("rates", []):
                    tariff_windows.append({
                        "type": band.get("type", "OTHER"),  # PEAK/OFF_PEAK/SHOULDER/SHOULDER1/SHOULDER2
                        "days": window.get("days", []),
                        "start": window.get("startTime"),
                        "end": window.get("endTime"),
                        "rate_excl_gst": Decimal(str(rate["unitPrice"])),
                        "rate_incl_gst": Decimal(str(rate["unitPrice"])),
                        "measure_unit": rate.get("measureUnit", "KWH"),
                    })
    elif rbut == "demandCharges":
        # Residential never observed; defensive code
        warnings.append(f"demandCharges block in residential plan — unusual")
```

→ Use `tp.timeZone` if present (`LOCAL` or `AEST`), else fall back to `ec.timeZone`, else default `AEST`. SM#686 may remove `tp.timeZone` later.

→ `tp.startDate` and `endDate` are **mm-dd format** (recurring annual ranges, e.g. `"11-01"` to `"03-31"` for summer band), NOT full dates.

### 6. Controlled load — different schema than main tariffPeriod

```python
for cl in ec.get("controlledLoad", []):
    rbut = cl.get("rateBlockUType")
    if rbut == "singleRate":
        sr = cl.get("singleRate", {})
        # KEY: dailySupplyCharge is NESTED inside the rate block here
        cl_dsc = sr.get("dailySupplyCharge")  # different from main tariffPeriod
        cl_rates = sr.get("rates", [])
    elif rbut == "timeOfUseRates":
        for band in cl.get("timeOfUseRates", []):
            cl_dsc = band.get("dailySupplyCharge")  # nested
            cl_rates = band.get("rates", [])
            cl_windows = band.get("timeOfUse", [])
            for w in cl_windows:
                # CL timeOfUse has extra fields not in main TP timeOfUse:
                additional_info = w.get("additionalInfo")
                additional_uri = w.get("additionalInfoUri")
```

→ **Do NOT recurse into the main tariffPeriod parser** — CL's schema is different.

### 7. Solar FIT — iterate ALL entries, not just `[0]`

```python
for fit in ec.get("solarFeedInTariff") or []:
    scheme = fit.get("scheme")           # PREMIUM | CURRENT | VARIABLE | OTHER
    payer = fit.get("payerType")         # GOVERNMENT | RETAILER
    discriminator = fit.get("tariffUType")
    if discriminator == "singleTariff":
        st = fit.get("singleTariff", {})
        for rate in st.get("rates", []):
            fit_tiers.append({
                "scheme": scheme, "payer_type": payer,
                "rate": Decimal(str(rate["unitPrice"])),
                "period": st.get("period"),
                "measure_unit": rate.get("measureUnit", "KWH"),
            })
    elif discriminator == "timeVaryingTariffs":
        for band in fit.get("timeVaryingTariffs", []):
            for rate in band.get("rates", []):
                for w in band.get("timeVariations", []):
                    fit_tiers.append({
                        "scheme": scheme, "payer_type": payer,
                        "rate": Decimal(str(rate["unitPrice"])),
                        "type": band.get("type"),  # PEAK / OFF_PEAK / SHOULDER
                        "time_window": {"days": w["days"], "start": w["startTime"], "end": w["endTime"]},
                    })
```

→ Sumo Power and Red Energy ship up to 9-tier FIT — iterate all.

### 8. Discounts — 4-branch UNION via methodUType

```python
for d in ec.get("discounts", []):
    method = d.get("methodUType")
    if method == "percentOfBill":
        value = {"kind": "percent_of_bill", "rate": Decimal(str(d["percentOfBill"]["rate"]))}
    elif method == "percentOfUse":
        value = {"kind": "percent_of_use", "rate": Decimal(str(d["percentOfUse"]["rate"]))}
    elif method == "fixedAmount":
        value = {"kind": "fixed_amount", "amount": Decimal(str(d["fixedAmount"]["amount"]))}
    elif method == "percentOverThreshold":
        value = {
            "kind": "percent_over_threshold",
            "rate": Decimal(str(d["percentOverThreshold"]["rate"])),
            "usage_amount": Decimal(str(d["percentOverThreshold"]["usageAmount"])),
        }
    discounts.append({
        "display_name": d.get("displayName"),
        "type": d.get("type"),       # CONDITIONAL | GUARANTEED | OTHER
        "category": d.get("category"),  # PAY_ON_TIME | DIRECT_DEBIT | GUARANTEED_DISCOUNT | OTHER
        "method": method,
        "value": value,
    })
```

### 9. Incentives, eligibility, fees — straight pass-through with enum acceptance

```python
for i in ec.get("incentives", []):
    incentives.append({
        "display_name": i["displayName"],
        "description": i["description"],
        "category": i.get("category"),  # GIFT | ACCOUNT_CREDIT | OTHER
        "eligibility": i.get("eligibility"),  # optional STRING (not list — common confusion)
    })

for e in ec.get("eligibility", []):
    eligibility.append({
        "type": e.get("type"),  # OTHER | EXISTING_CUST | NEW_CUSTOMER | EXISTING_SOLAR |
                                 # EXISTING_BATTERY | EXISTING_SMART_METER | LOYALTY_MEMBER |
                                 # ORG_MEMBER | SENIOR_CARD | THIRD_PARTY_ONLY | ONLINE_ONLY |
                                 # CONTINGENT_PLAN | ...
        "information": e.get("information"),  # mandatory free-text
        "description": e.get("description"),  # optional
    })

for f in ec.get("fees", []):
    fees.append({
        "type": f["type"],  # 16 enum values
        "term": f["term"],  # FIXED / 1-5_YEAR / PERCENT_OF_BILL / ANNUAL / DAILY / etc.
        "amount": Decimal(str(f["amount"])) if f.get("amount") else None,
        "rate": Decimal(str(f["rate"])) if f.get("rate") else None,  # required if term=PERCENT_OF_BILL
        "description": f.get("description"),
    })
```

### 10. Green power charges — tier-based

```python
for g in ec.get("greenPowerCharges", []):
    for tier in g.get("tiers", []):
        green_power_tiers.append({
            "type": g.get("type"),    # FIXED_PER_DAY/WEEK/MONTH/UNIT or PERCENT_OF_USE/BILL
                                       # (SM#572 may add FIXED_PER_QUARTER for Ergon)
            "scheme": g.get("scheme"),  # GREENPOWER | OTHER (only GREENPOWER observed)
            "percent_green": Decimal(str(tier.get("percentGreen", "0"))),
            "amount": Decimal(str(tier.get("amount", "0"))),
        })
```

### 11. Derived badges

```python
# EV-overlay detection (SM#710 workaround)
zero_rates = sum(1 for w in tariff_windows if w["rate_excl_gst"] == Decimal(0))
total_rates = len(tariff_windows)
is_ev_overlay = (
    pricing_model in ("TIME_OF_USE", "TIME_OF_USE_CONT_LOAD")
    and zero_rates > 0
    and total_rates > zero_rates  # at least one paid rate
)

# SSO detection (SM#719 workaround, after 1 July 2026)
description_text = (
    (display_name or "") + " " +
    (variation or "") + " " +
    " ".join(t.get("description", "") or "" for t in tariff_windows)
).upper()
is_sso_candidate = any(marker in description_text for marker in [
    "SOLAR SHARER", "SSO", "FREE ELECTRICITY WINDOW", "SOLAR SOAK"
])

# Has intrinsic green
has_intrinsic_green = intrinsic_green_pct is not None and intrinsic_green_pct > 0
```

### 12. Error handling

- Wrap each sub-section in `try/except` and append warnings rather than failing the whole summary
- Partial display beats blank card
- Always emit `pricing_model` (default `"UNKNOWN"`) so the HA card layer can pick a renderer without `KeyError`
- Coerce all numeric AmountStrings via `Decimal(str(v))` to absorb str-vs-num drift (Origin ships some `volume` fields as numbers)

### 13. Sample edge-case test fixtures

From `cache/v1/`:
- `origin-energy/ORI909049MRE7@EME` — TIME_OF_USE_CONT_LOAD with 3-band TOU, controlled load, 9 fees, 3 green-power charges
- `origin-energy/ORI1027274MRE3@EME` — SINGLE_RATE_CONT_LOAD variant
- `agl/AGD728737MR@VEC` — TOU with `inc0.category=GIFT` (rare)
- `red-energy/...` — multi-tier FIT (up to 9 tiers)
- `flow-power/...` — `tariffUType=timeVaryingTariffs` with `scheme=VARIABLE`
- `amber/...` — most plans MISSING solarFeedInTariff entirely
- `ergon/...` — quarterly fixed greenPower (SM#572 gap)

## Test plan

```python
def test_handles_all_v1_cache():
    """Ensure parser doesn't crash on any of the 10,266 cached plans."""
    cache = "/tmp/cdr-cache"
    crashes = []
    for slug in os.listdir(cache):
        if slug.startswith("_"): continue
        for f in os.listdir(os.path.join(cache, slug)):
            if f.startswith("_") or not f.endswith(".json"): continue
            with open(os.path.join(cache, slug, f)) as fp:
                detail = json.load(fp)
            try:
                summary = summarise_cdr_plan(detail)
                assert summary["plan_id"] is not None or summary["warnings"]
            except Exception as e:
                crashes.append((slug, f, str(e)))
    assert not crashes, f"Crashes: {crashes[:10]}"

def test_gst_handling():
    """Daily supply charge is GST-exclusive; tariff rates are GST-inclusive."""
    detail = load_fixture("agl_simple_single_rate.json")
    s = summarise_cdr_plan(detail)
    assert s["supply_c_per_day_incl_gst"] == s["supply_c_per_day_excl_gst"] * Decimal("1.10")
    assert s["tariff_windows"][0]["rate_incl_gst"] == s["tariff_windows"][0]["rate_excl_gst"]

def test_ev_overlay_detection():
    """AGL Night Saver EV mis-classified as TOU_CONT_LOAD with zero rates."""
    detail = load_fixture("agl_night_saver_ev.json")
    s = summarise_cdr_plan(detail)
    assert s["is_ev_overlay"] is True

def test_multi_tier_fit():
    """Red Energy ships up to 9-tier FIT — all should be parsed."""
    detail = load_fixture("red_energy_9tier_fit.json")
    s = summarise_cdr_plan(detail)
    assert len(s["fit_tiers"]) >= 3
```
