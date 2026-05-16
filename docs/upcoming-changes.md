# Upcoming CDR Energy Spec Changes — Watch List

15 open issues with `Energy` label in [`ConsumerDataStandardsAustralia/standards-maintenance`](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues?q=is%3Aissue+is%3Aopen+label%3A%22Energy%22). The Zendesk help center routes change requests to this repo (no separate ticket system).

Sorted by impact on PriceHawk and downstream tooling.

## URGENT — lands 1 July 2026 (~6 weeks)

### [SM#719](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/719) Solar Sharer Offer (SSO) in CDR Energy PRD

> "The SSO introduces a tariff band that is fundamentally different from any existing rate type — it is a government-mandated, zero-cost consumption window with a daily volume cap. EnergyPlanTariffPeriodV2.timeOfUseRates[].type enum currently supports PEAK | OFF_PEAK | SHOULDER | DEMAND. None of these can distinctly identify an SSO free-electricity period."

- Created 2026-05-14, 0 comments, no DSB response yet
- **Critical action**: as of 1 July 2026, retailers ship SSO plans but spec has no value
- Likely workarounds: retailers will use `OFF_PEAK` or `OTHER` with description prefix
- PriceHawk should: pre-bake "Free SSO window" detection by scanning rate `unitPrice == 0` AND scanning description text for SSO indicators

## HIGH — material schema changes likely in next 6–12 months

### [SM#710](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/710) EnergyPlanContract pricingModel insufficient for EV-overlay plans

> "Currently the pricingModel ENUMs are not fit for emerging products. Retailers are offering plans where there is an underlying single rate tariff however there are hours where the customer is not charged (zero-rated) which is an overlayed retail tariff (AGL's Night Saver EV, Three for Free (SA), Origin's 360 EV, Red Energy's Red EV Saver)."

- 12 comments, updated 2026-04-02, active discussion
- Today these plans ship as `TIME_OF_USE_CONT_LOAD` with zero-priced rate rows — confusing
- Expect new `pricingModel` enum value (e.g. `SINGLE_RATE_OVERLAY`) within months
- PriceHawk should: detect zero rates in TOU plans, badge as "Free EV window"

### [SM#662](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/662) Add `tariffCode` field to GetGenericPlanDetail

> "An electricity meter will be assigned to a tariff type by the relevant DNSP. The tariff type defines the time periods for various tariff types, such as peak, off-peak, and shoulder, and also defines the network charges that the responsible retailer must bear for the consumption recorded by that meter."

- 9 comments, updated 2025-07-31
- If approved, lets app match plan to user's actual DNSP-assigned tariff code (visible on user's bill)
- Until landed: PriceHawk needs manual mapping from postcode → DNSP → tariff code

### [SM#686](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/686) Remove redundant `tariffPeriod[].timeZone`

- 7 comments, updated 2025-07-03
- Proposes deleting `timeZone` at tariffPeriod level — redundant with electricityContract.timeZone + ISO 8601 timezone offsets in startTime/endTime
- Watch for breaking change in next major version (v4?)
- PriceHawk: tolerate field absence

## MEDIUM — gaps that block specific use cases

### [SM#619](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/619) `tariffPeriod.type` enum needs more values

- Existing PEAK/OFF_PEAK/SHOULDER/SHOULDER1/SHOULDER2 doesn't cover all real cases
- Open since Oct 2023, low activity
- Possible additions: NETWORK, SOLAR_SPONGE, CONTROLLED_LOAD pieces

### [SM#607](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/607) Variable/range fees not supported

- Reconnection fees vary by business hours / weekend / remote vs local
- No clean way to express. Open since Aug 2023, no activity in 3 years

### [SM#572](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/572) Fixed-quarterly GreenPower not supported

- Ergon Energy ships quarterly fixed amounts; spec only has DAY/WEEK/MONTH/UNIT
- Currently shoehorned into `description` field
- Open since Jan 2023, no progress

### [SM#474](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/474) GST clarification — CRITICAL for display correctness

> "Rates and fees in Energy data may or may not have GST included. The current standards explicitly note which attributes exclude GST. Attributes are otherwise assumed to be GST-INCLUSIVE."

- 7 comments, updated 2025-10-07
- `tariffPeriod[].dailySupplyCharge` is **EXPLICITLY GST-EXCLUSIVE** per spec
- Most other AmountStrings are **GST-INCLUSIVE** by default
- PriceHawk MUST add 10% to dailySupplyCharge before displaying as cents/day to user

## LOW — consumer-data-side, doesn't affect PRD

### [SM#680](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/680) ISO Jurisdiction Code

- AEMO ServicePoints API returns "ISO" (not in enum)
- Affects authenticated consumer-data endpoints, not PRD

### [SM#651](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/651) HTTP 429 + Retry-After passthrough from AEMO

- Affects authenticated consumer-data endpoints, not PRD
- Worth knowing if PriceHawk later supports authenticated NMI lookups

### [SM#601](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/601) Cancelled Invoices in Invoice APIs

- Authenticated invoice endpoints, not PRD

## Foundational issue (4 years unresolved)

### [SM#561](https://github.com/ConsumerDataStandardsAustralia/standards-maintenance/issues/561) Update Register API to return separate PRD endpoint

- Created Dec 2022 by jxeeno (the GitHub registry maintainer)
- AER + Vic agency are PRD data holders; retailers are NOT required to implement PRD endpoints
- ACCC Register API's `publicBaseUri` returns DIFFERENT URLs based on retailer enrollment status — broken for energy discovery
- AER's PDF + community-maintained scrapes (jxeeno, this repo) are the only options
- 15 comments, last updated 2026-03-20 — still no resolution after 4 years

## How to monitor

```bash
# Open Energy issues sorted by recent activity
gh issue list --repo ConsumerDataStandardsAustralia/standards-maintenance \
  --label Energy --state open --search "sort:updated-desc" --limit 20

# Watch for SSO landing
gh issue view 719 --repo ConsumerDataStandardsAustralia/standards-maintenance --comments
```

Suggest setting up a weekly `gh` poll (or RSS via the GitHub Atom feed) and pinging when any of #719, #710, #662, #686, #474 transition state.
