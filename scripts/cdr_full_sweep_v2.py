#!/usr/bin/env python3
"""CDR Energy PRD comprehensive sweep v2.

Improvements over v1:
- Source registry from Energy Made Easy refdata2 (117 orgs, vs jxeeno's 78)
- Use ?brand=<cdrCode> to disambiguate plans on shared base URIs
- Probe ALL fields (top-level plan, contract, all sub-lists, deep TOU windows)
- Capture enum value distributions across all plans
- Output comprehensive catalog: shape signatures + retailer matrix +
  enum reference + GST flags + parser spec + registry comparison

Stdlib only. Resumable. Polite (1 req/sec/retailer, 12-way parallel).

Cache layout: /tmp/cdr-cache/{slug}/{planId}.json (compatible with v1 cache)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Any

REPO_ROOT = "/Users/ryanfoyle/Development/cdr-energy-research"
CACHE_DIR = "/tmp/cdr-cache"
PROGRESS_PATH = os.path.join(CACHE_DIR, "_progress_v2.json")
FAILED_PATH = os.path.join(CACHE_DIR, "_failed_v2.jsonl")
EME_REFDATA = os.path.join(REPO_ROOT, "data", "eme-refdata.json")
EME_REFDATA_URL = "https://api.energymadeeasy.gov.au/refdata2?keys=organisations,thirdParties"

CATALOG_PATH = os.path.join(REPO_ROOT, "docs", "shape-catalog-v2.md")
ENUMS_PATH = os.path.join(REPO_ROOT, "docs", "enums-reference.md")
REGISTRY_CMP_PATH = os.path.join(REPO_ROOT, "data", "registry-comparison.json")
RETAILER_INDEX_PATH = os.path.join(REPO_ROOT, "data", "retailer-index.json")

REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
PARALLEL_RETAILERS = 12
PAGE_SIZE = 1000
PER_RETAILER_GAP = 1.0

WALL_CLOCK_START = time.time()

retailer_locks: dict[str, Lock] = defaultdict(Lock)
last_request_at: dict[str, float] = defaultdict(float)
failed_lock = Lock()
progress_lock = Lock()

os.makedirs(CACHE_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "unknown"


def http_get(url: str, headers: dict[str, str] | None = None) -> tuple[Any | None, str | None, int]:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = resp.read()
            try:
                return json.loads(data), None, resp.status
            except json.JSONDecodeError as e:
                return None, f"json:{e}", resp.status
    except urllib.error.HTTPError as e:
        return None, f"http:{e.code}", e.code
    except urllib.error.URLError as e:
        return None, f"url:{e.reason}", 0
    except (TimeoutError, ConnectionError) as e:
        return None, f"net:{type(e).__name__}", 0
    except Exception as e:  # noqa: BLE001
        return None, f"err:{type(e).__name__}:{e}", 0


def polite_get(url: str, headers: dict, slug: str) -> tuple[Any | None, str | None, int]:
    backoff = 2.0
    last_err = None
    for _ in range(MAX_RETRIES):
        with retailer_locks[slug]:
            now = time.time()
            wait = (last_request_at[slug] + PER_RETAILER_GAP) - now
            if wait > 0:
                time.sleep(wait)
            last_request_at[slug] = time.time()
            j, err, status = http_get(url, headers)
        if j is not None:
            return j, None, status
        last_err = err
        retryable = status == 429 or 500 <= status < 600 or status == 0
        if not retryable:
            return None, err, status
        time.sleep(backoff)
        backoff *= 2
    return None, last_err, 0


def cache_dir(slug: str) -> str:
    p = os.path.join(CACHE_DIR, slug)
    os.makedirs(p, exist_ok=True)
    return p


def load_json(path: str) -> Any | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_json(path: str, data: Any, indent: int | None = None) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
    os.replace(tmp, path)


def append_failed(rec: dict) -> None:
    with failed_lock, open(FAILED_PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# Registry from EME refdata2
# ---------------------------------------------------------------------------

def fetch_eme_refdata() -> dict:
    cached = load_json(EME_REFDATA)
    if cached is not None:
        return cached
    j, err, _ = http_get(EME_REFDATA_URL)
    if not j:
        raise RuntimeError(f"EME refdata fetch failed: {err}")
    save_json(EME_REFDATA, j, indent=2)
    return j


def build_retailer_list(refdata: dict) -> list[dict]:
    """Returns deduped list of retailer entries.

    Dedupes by cdrCode (collapses duplicate org records like
    Origin/Electricity + Origin/LPG + Origin/Retail Limited all sharing
    cdrCode=origin). Drops orgs that have NO electricityBillURL AND a
    gasBillURL (gas-only retailers — we only care about electricity).
    """
    orgs = refdata.get("data", {}).get("organisations", {})
    seen_cdr_codes: set[str] = set()
    out = []
    for org_id, o in orgs.items():
        cdr_code = o.get("cdrCode")
        if not cdr_code:
            continue
        if cdr_code in seen_cdr_codes:
            continue
        # Skip gas-only retailers (no electricityBillURL, has gasBillURL)
        if not o.get("electricityBillURL") and o.get("gasBillURL"):
            continue
        seen_cdr_codes.add(cdr_code)
        out.append({
            "orgId": org_id,
            "cdrCode": cdr_code,
            "cdrBrand": o.get("cdrBrand") or cdr_code,
            "tradingName": o.get("tradingName"),
            "orgName": o.get("orgName"),
            "abn": o.get("abn"),
            "websiteURL": o.get("websiteURL"),
            "electricityBillURL": o.get("electricityBillURL"),
            "gasBillURL": o.get("gasBillURL"),
            "logo": o.get("logo"),
            "retailerCode": o.get("retailerCode"),
            "residentialContact": o.get("residentialContact"),
            "smallBusinessContact": o.get("smallBusinessContact"),
            "orgStatus": o.get("orgStatus"),
            "baseUri": f"https://cdr.energymadeeasy.gov.au/{cdr_code}",
            "slug": slugify(o.get("orgName") or cdr_code),
        })
    return out


# ---------------------------------------------------------------------------
# List + detail fetch
# ---------------------------------------------------------------------------

def fetch_plan_list(base: str, slug: str, brand_filter: str | None = None) -> tuple[list[dict], str | None]:
    cache_name = f"_planlist_{brand_filter}.json" if brand_filter else "_planlist.json"
    cache_file = os.path.join(cache_dir(slug), cache_name)
    cached = load_json(cache_file)
    if cached is not None:
        if isinstance(cached, list):
            return cached, None
        if isinstance(cached, dict):
            if isinstance(cached.get("plans"), list):
                return cached["plans"], None
            data = cached.get("data")
            if isinstance(data, dict) and isinstance(data.get("plans"), list):
                return data["plans"], None

    all_plans: list[dict] = []
    page = 1
    while True:
        params = [
            f"fuelType=ELECTRICITY",
            f"type=ALL",
            f"effective=CURRENT",
            f"page-size={PAGE_SIZE}",
            f"page={page}",
        ]
        if brand_filter:
            params.append(f"brand={brand_filter}")
        url = f"{base.rstrip('/')}/cds-au/v1/energy/plans?{'&'.join(params)}"
        j, err, _ = polite_get(url, {"x-v": "1"}, slug)
        if j is None:
            return all_plans, err
        try:
            plans = j.get("data", {}).get("plans", []) or []
            meta = j.get("meta", {}) or {}
            total_pages = int(meta.get("totalPages", 1) or 1)
        except (AttributeError, TypeError, ValueError) as e:
            return all_plans, f"shape:{e}"
        all_plans.extend(plans)
        if page >= total_pages:
            break
        page += 1
        if page > 30:
            break
    save_json(cache_file, all_plans)
    return all_plans, None


def fetch_plan_detail(base: str, plan_id: str, slug: str) -> tuple[dict | None, str | None]:
    safe_id = plan_id.replace("/", "_")
    cache_file = os.path.join(cache_dir(slug), f"{safe_id}.json")
    cached = load_json(cache_file)
    if cached is not None:
        return cached, None
    url = f"{base.rstrip('/')}/cds-au/v1/energy/plans/{plan_id}"
    j, err, status = polite_get(url, {"x-v": "3"}, slug)
    if j is None:
        return None, f"{err} (status={status})"
    save_json(cache_file, j)
    return j, None


def is_residential_electricity(p: dict) -> bool:
    return (
        p.get("fuelType") == "ELECTRICITY"
        and p.get("customerType") == "RESIDENTIAL"
        and p.get("type") in ("MARKET", "STANDING")
    )


# ---------------------------------------------------------------------------
# Per-retailer worker
# ---------------------------------------------------------------------------

def process_retailer(r: dict, shared_base_brands: dict) -> dict:
    """Fetch and cache plans for one brand.

    For shared base URIs, use ?brand= to filter; for unique, fetch all.
    """
    slug = r["slug"]
    base = r["baseUri"]
    cdr_code = r["cdrCode"]
    stats = {
        "cdrCode": cdr_code, "slug": slug, "base": base,
        "tradingName": r.get("tradingName"),
        "orgName": r.get("orgName"),
        "shared_base": len(shared_base_brands.get(base, [])) > 1,
        "plans_listed": 0, "plans_filtered": 0,
        "plans_fetched": 0, "plans_failed": 0, "list_error": None,
    }
    use_brand_filter = stats["shared_base"]
    plans, list_err = fetch_plan_list(
        base, slug, brand_filter=cdr_code if use_brand_filter else None
    )
    if list_err and not plans:
        stats["list_error"] = list_err
        return stats
    stats["plans_listed"] = len(plans)
    filtered = [p for p in plans if is_residential_electricity(p)]
    stats["plans_filtered"] = len(filtered)

    fetched = failed = 0
    for i, p in enumerate(filtered, 1):
        pid = p.get("planId")
        if not pid:
            continue
        d, err = fetch_plan_detail(base, pid, slug)
        if d is None:
            failed += 1
            append_failed({"slug": slug, "cdrCode": cdr_code, "planId": pid, "error": err})
            continue
        fetched += 1
        if i % 100 == 0:
            checkpoint(slug, cdr_code, i, len(filtered))
    stats["plans_fetched"] = fetched
    stats["plans_failed"] = failed
    checkpoint(slug, cdr_code, len(filtered), len(filtered))
    return stats


def checkpoint(slug: str, cdr_code: str, done: int, total: int) -> None:
    with progress_lock:
        prog = load_json(PROGRESS_PATH) or {}
        prog[cdr_code] = {"slug": slug, "done": done, "total": total, "ts": time.time()}
        save_json(PROGRESS_PATH, prog)


# ---------------------------------------------------------------------------
# Comprehensive signature
# ---------------------------------------------------------------------------

def jtype(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, str):
        return "string"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, list):
        return f"list[{len(v)}]"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def sk(d: dict | None) -> str:
    if not isinstance(d, dict):
        return ""
    return ",".join(sorted(d.keys()))


def extract_sig_v2(detail: dict) -> tuple[str, list[str], dict, dict]:
    """Returns (sig_id, tokens, snapshot, enum_values).

    enum_values is a dict of {enum_path: [observed_value]} for cross-plan rollup.
    """
    tokens: list[str] = []
    snap: dict = {}
    enums: dict = {}

    data = detail.get("data") if isinstance(detail, dict) else None
    if not isinstance(data, dict):
        tokens.append("data:MISSING")
        return _hash(tokens), tokens, snap, enums

    # Plan-level
    for k in ("type", "fuelType", "customerType"):
        v = data.get(k)
        if v is not None:
            tokens.append(f"plan.{k}:{v}")
            enums.setdefault(f"plan.{k}", []).append(v)

    geo = data.get("geography") or {}
    for k in ("includedPostcodes", "excludedPostcodes", "distributors"):
        v = geo.get(k)
        if v is not None:
            tokens.append(f"plan.geography.{k}:{jtype(v)}")
            if k == "distributors" and isinstance(v, list):
                for d in v:
                    enums.setdefault("geography.distributors[]", []).append(d)

    addinfo = data.get("additionalInformation") or {}
    for k in ("overviewUri", "termsUri", "eligibilityUri", "pricingUri", "bundleUri"):
        if k in addinfo:
            tokens.append(f"plan.additionalInformation.{k}:present")

    for k in ("planId", "displayName", "description", "brand", "brandName",
              "applicationUri", "lastUpdated", "effectiveFrom", "effectiveTo"):
        if k in data:
            tokens.append(f"plan.{k}:present")

    # meteringCharges (top-level, NOT under contract)
    mc = data.get("meteringCharges")
    if mc is not None:
        tokens.append(f"plan.meteringCharges:{jtype(mc)}")

    ec = data.get("electricityContract")
    if not isinstance(ec, dict):
        tokens.append("electricityContract:MISSING")
        return _hash(tokens), tokens, snap, enums

    # Contract-level core
    pm = ec.get("pricingModel", "MISSING")
    tokens.append(f"ec.pricingModel:{pm}")
    enums.setdefault("ec.pricingModel", []).append(pm)
    snap["pricingModel"] = pm

    for k in ("isFixed", "timeZone", "termType", "coolingOffDays",
              "additionalFeeInformation", "onExpiryDescription",
              "variation", "terms", "benefitPeriod"):
        v = ec.get(k, "__MISSING__")
        if v == "__MISSING__":
            tokens.append(f"ec.{k}:MISSING")
        else:
            tokens.append(f"ec.{k}:{jtype(v)}")
            if k in ("timeZone", "termType"):
                enums.setdefault(f"ec.{k}", []).append(v)

    for k in ("paymentOption", "billFrequency", "meterTypes"):
        v = ec.get(k)
        if v is None:
            tokens.append(f"ec.{k}:MISSING")
        else:
            tokens.append(f"ec.{k}:{jtype(v)}")
            if isinstance(v, list):
                for x in v:
                    enums.setdefault(f"ec.{k}[]", []).append(x)

    igp = ec.get("intrinsicGreenPower")
    if igp:
        tokens.append(f"ec.intrinsicGreenPower:dict")
        if isinstance(igp, dict) and "greenPercentage" in igp:
            tokens.append("ec.intrinsicGreenPower.greenPercentage:present")

    # Top-level lists with type fingerprints
    for k in ("dailySupplyCharges", "dailySupplyCharge", "tariffPeriod",
              "solarFeedInTariff", "incentives", "controlledLoad",
              "greenPowerCharges", "discounts", "fees", "eligibility"):
        v = ec.get(k, "__MISSING__")
        tokens.append(f"ec.{k}:{ 'MISSING' if v == '__MISSING__' else jtype(v)}")

    # tariffPeriod[0] deep
    tp = ec.get("tariffPeriod") or []
    if isinstance(tp, list) and tp and isinstance(tp[0], dict):
        tp0 = tp[0]
        for k in ("type", "displayName", "startDate", "endDate", "timeZone",
                  "dailySupplyCharge", "dailySupplyChargeType",
                  "dailySupplyCharges", "bandedDailySupplyCharges",
                  "rateBlockUType"):
            v = tp0.get(k, "__MISSING__")
            label = jtype(v) if v != "__MISSING__" else "MISSING"
            tokens.append(f"tp0.{k}:{label}")
            if k in ("type", "rateBlockUType", "dailySupplyChargeType", "timeZone") and v != "__MISSING__":
                enums.setdefault(f"tp0.{k}", []).append(v)

        rbut = tp0.get("rateBlockUType")
        if rbut and rbut != "MISSING":
            blk = tp0.get(rbut)
            tokens.append(f"tp0.{rbut}.type:{jtype(blk)}")
            inner = blk if isinstance(blk, dict) else (
                blk[0] if isinstance(blk, list) and blk and isinstance(blk[0], dict) else None
            )
            if isinstance(inner, dict):
                tokens.append(f"tp0.{rbut}.keys:{sk(inner)}")
                if "generalUnitPrice" in inner:
                    tokens.append("tp0.singleRate.generalUnitPrice:present")
                rates = inner.get("rates") or []
                if isinstance(rates, list) and rates and isinstance(rates[0], dict):
                    tokens.append(f"tp0.rates[0].keys:{sk(rates[0])}")
                    for f in ("unitPrice", "volume", "measureUnit", "period"):
                        v = rates[0].get(f, "__MISSING__")
                        tokens.append(f"tp0.rates[0].{f}:{ 'MISSING' if v == '__MISSING__' else jtype(v)}")
                # TOU windows
                tou = inner.get("timeOfUse") or []
                if isinstance(tou, list) and tou and isinstance(tou[0], dict):
                    tokens.append(f"tp0.tou[0].keys:{sk(tou[0])}")
                    for d in (tou[0].get("days") or []):
                        enums.setdefault("tou.days[]", []).append(d)
            # for timeOfUseRates list, capture per-band type values
            if isinstance(blk, list):
                for bandi, band in enumerate(blk[:5]):
                    if isinstance(band, dict) and band.get("type"):
                        enums.setdefault(f"tp.{rbut}[].type", []).append(band["type"])

    # solarFeedInTariff[0]
    fit = ec.get("solarFeedInTariff") or []
    if isinstance(fit, list) and fit and isinstance(fit[0], dict):
        fit0 = fit[0]
        for k in ("scheme", "payerType", "tariffUType", "displayName", "description", "startDate", "endDate"):
            v = fit0.get(k, "__MISSING__")
            label = jtype(v) if v != "__MISSING__" else "MISSING"
            tokens.append(f"fit0.{k}:{label}")
            if k in ("scheme", "payerType", "tariffUType") and v != "__MISSING__":
                enums.setdefault(f"fit0.{k}", []).append(v)
        fut = fit0.get("tariffUType")
        if fut:
            blk = fit0.get(fut)
            tokens.append(f"fit0.{fut}.type:{jtype(blk)}")
            inner = blk if isinstance(blk, dict) else (
                blk[0] if isinstance(blk, list) and blk and isinstance(blk[0], dict) else None
            )
            if isinstance(inner, dict):
                tokens.append(f"fit0.{fut}.keys:{sk(inner)}")
                rates = inner.get("rates") or []
                if rates and isinstance(rates[0], dict):
                    tokens.append(f"fit0.rates[0].keys:{sk(rates[0])}")

    # incentives, discounts, greenPowerCharges, fees, eligibility — enum aggregation
    for k_field, sub_keys, list_path in [
        ("incentives", ("category",), "ec.incentives[]"),
        ("discounts", ("type", "category", "methodUType"), "ec.discounts[]"),
        ("greenPowerCharges", ("type", "scheme"), "ec.greenPowerCharges[]"),
        ("fees", ("type", "term"), "ec.fees[]"),
        ("eligibility", ("type",), "ec.eligibility[]"),
    ]:
        items = ec.get(k_field) or []
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    for sk_ in sub_keys:
                        if sk_ in it and it[sk_] is not None:
                            enums.setdefault(f"{list_path}.{sk_}", []).append(it[sk_])
            if items and isinstance(items[0], dict):
                tokens.append(f"{k_field}[0].keys:{sk(items[0])}")

    # controlledLoad[0] deep — uses different schema (DSC nested in rate block)
    cl = ec.get("controlledLoad") or []
    if isinstance(cl, list) and cl and isinstance(cl[0], dict):
        cl0 = cl[0]
        rbut = cl0.get("rateBlockUType")
        tokens.append(f"cl0.rateBlockUType:{rbut}")
        if rbut:
            enums.setdefault("cl0.rateBlockUType", []).append(rbut)
            blk = cl0.get(rbut)
            inner = blk if isinstance(blk, dict) else (
                blk[0] if isinstance(blk, list) and blk and isinstance(blk[0], dict) else None
            )
            if isinstance(inner, dict):
                tokens.append(f"cl0.{rbut}.keys:{sk(inner)}")
                # KEY: CL nests dailySupplyCharge inside the rate block
                if "dailySupplyCharge" in inner:
                    tokens.append("cl0.dailySupplyCharge_nested:present")

    # Detect potential EV-overlay zero-rate plans (SM#710)
    # Walk all rate unitPrice values, count zero-priced
    zero_rates = 0
    total_rates = 0
    def walk_rates(o):
        nonlocal zero_rates, total_rates
        if isinstance(o, list):
            for x in o:
                walk_rates(x)
        elif isinstance(o, dict):
            if "unitPrice" in o:
                up = o.get("unitPrice")
                total_rates += 1
                try:
                    if float(str(up)) == 0.0:
                        zero_rates += 1
                except (ValueError, TypeError):
                    pass
            for v in o.values():
                walk_rates(v)
    walk_rates(ec.get("tariffPeriod"))
    if total_rates > 0 and zero_rates > 0:
        tokens.append(f"FLAG.zero_rate_overlay:{zero_rates}/{total_rates}")

    return _hash(tokens), tokens, snap, enums


def _hash(tokens: list[str]) -> str:
    h = hashlib.sha1("|".join(tokens).encode()).hexdigest()
    return f"SIG_{h[:12]}"


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render_catalog(retailers, stats, sig_to_plans, sig_to_tokens,
                   enum_global, surprises, ev_overlay_plans):
    lines = []
    lines.append("# CDR PlanDetailV2 Shape Catalog v2 (Comprehensive Sweep)\n")
    lines.append(f"_Generated by `scripts/cdr_full_sweep_v2.py` on "
                 f"{time.strftime('%Y-%m-%d %H:%M', time.localtime())}._\n")
    lines.append("**Sources**: AER PDF (Jan 2026) + EME refdata2 + per-plan-detail fetches.\n")

    # Section 1 — Sweep summary
    n_retailers = len(stats)
    n_reachable = sum(1 for s in stats if not s.get("list_error"))
    n_listed = sum(s["plans_listed"] for s in stats)
    n_filtered = sum(s["plans_filtered"] for s in stats)
    n_fetched = sum(s["plans_fetched"] for s in stats)
    n_failed = sum(s["plans_failed"] for s in stats)
    n_unique_bases = len({s["base"] for s in stats})
    n_shared = sum(1 for s in stats if s.get("shared_base"))
    cache_size = sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _, fs in os.walk(CACHE_DIR) for f in fs
    )
    duration = time.time() - WALL_CLOCK_START
    lines.append("## 1. Sweep summary\n")
    lines.append(f"- Retailers in EME refdata2: **{n_retailers}**")
    lines.append(f"- Unique base URIs: **{n_unique_bases}** (with **{n_shared}** brands sharing endpoints)")
    lines.append(f"- Retailers reachable: **{n_reachable}**")
    lines.append(f"- Plans listed: **{n_listed:,}**")
    lines.append(f"- Plans filtered (RES ELEC MARKET/STANDING): **{n_filtered:,}**")
    lines.append(f"- Plan details fetched OK: **{n_fetched:,}**")
    lines.append(f"- Plan detail failures: **{n_failed:,}**")
    lines.append(f"- Distinct shape signatures: **{len(sig_to_plans):,}**")
    lines.append(f"- EV-overlay candidates (zero-rate within TOU): **{len(ev_overlay_plans):,}**")
    lines.append(f"- Wall-clock: **{duration:.1f}s ({duration/60:.1f}m)**")
    lines.append(f"- Cache size: **{cache_size/1024/1024:.1f} MB**\n")

    # Section 2 — Retailer matrix
    lines.append("## 2. Retailer coverage (sorted by plans fetched, desc)\n")
    lines.append("| Brand (cdrCode) | Trading Name | Base URI | Shared? | Listed | Filtered | Fetched | Failed |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|")
    for s in sorted(stats, key=lambda x: -x["plans_fetched"]):
        shared = "✓" if s.get("shared_base") else ""
        base_short = s["base"].replace("https://cdr.energymadeeasy.gov.au/", "/")
        lines.append(
            f"| `{s['cdrCode']}` | {s.get('tradingName') or s.get('orgName') or '?'} "
            f"| `{base_short}` | {shared} | {s['plans_listed']:,} | {s['plans_filtered']:,} "
            f"| {s['plans_fetched']:,} | {s['plans_failed']:,} |"
        )
    lines.append("")

    # Section 3 — Top signatures (top 30 with details)
    lines.append("## 3. Top 30 shape signatures (by plan count)\n")
    lines.append(f"Total distinct signatures: **{len(sig_to_plans):,}**.\n")
    sig_ranked = sorted(sig_to_plans.items(), key=lambda kv: -len(kv[1]))
    for sig, plans in sig_ranked[:30]:
        retailer_count = Counter(slug for slug, _ in plans)
        lines.append(f"### {sig} — {len(plans):,} plans across {len(retailer_count)} retailers")
        # 3 sample IDs
        seen = set(); samples = []
        for slug, pid in plans:
            if slug not in seen:
                samples.append((slug, pid)); seen.add(slug)
            if len(samples) >= 3: break
        for slug, pid in samples:
            lines.append(f"- `{slug}/{pid}`")
        lines.append("**Tokens:**")
        for tok in sig_to_tokens[sig][:35]:
            lines.append(f"- `{tok}`")
        if len(sig_to_tokens[sig]) > 35:
            lines.append(f"- _(+{len(sig_to_tokens[sig])-35} more tokens)_")
        lines.append("**Top retailers:**")
        for slug, cnt in retailer_count.most_common(5):
            lines.append(f"- {slug}: {cnt}")
        lines.append("")

    # Section 4 — Field-presence matrix (top 15 retailers)
    lines.append("## 4. Field-presence matrix (top 15 retailers by volume)\n")
    important_paths = [
        "ec.dailySupplyCharge", "ec.dailySupplyCharges", "ec.tariffPeriod",
        "ec.solarFeedInTariff", "ec.incentives", "ec.controlledLoad",
        "ec.greenPowerCharges", "ec.discounts", "ec.fees", "ec.eligibility",
        "ec.intrinsicGreenPower", "ec.isFixed", "ec.timeZone", "ec.termType",
        "ec.coolingOffDays", "ec.meterTypes", "ec.additionalFeeInformation",
        "tp0.bandedDailySupplyCharges", "tp0.dailySupplyChargeType",
        "tp0.singleRate.generalUnitPrice", "cl0.dailySupplyCharge_nested",
        "FLAG.zero_rate_overlay",
    ]
    slug_volume = Counter(slug for plans in sig_to_plans.values() for slug, _ in plans)
    top_retailers = [s for s, _ in slug_volume.most_common(15)]
    presence = defaultdict(lambda: defaultdict(int))
    total = defaultdict(lambda: defaultdict(int))
    for sig, plans in sig_to_plans.items():
        toks = sig_to_tokens[sig]
        for slug, _ in plans:
            for path in important_paths:
                total[path][slug] += 1
                for t in toks:
                    if t.startswith(path + ":") and not t.endswith(":MISSING"):
                        presence[path][slug] += 1
                        break
    if top_retailers:
        lines.append("| Path | " + " | ".join(top_retailers) + " |")
        lines.append("|---|" + "|".join(["---:"] * len(top_retailers)) + "|")
        for path in important_paths:
            row = [path]
            for slug in top_retailers:
                p = presence[path].get(slug, 0)
                t = total[path].get(slug, 0)
                row.append(f"{p}/{t}" if t else "—")
            lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Section 5 — Daily supply charge location
    lines.append("## 5. Daily supply charge — definitive location\n")
    lines.append("Every plan with DSC publishes it at `tariffPeriod[0].dailySupplyCharge` (string, GST-EXCLUSIVE per spec).")
    lines.append("Other 3 spec locations (ec.dailySupplyCharges plural, ec.dailySupplyCharge singular,")
    lines.append("tariffPeriod[0].dailySupplyCharges plural) are 0/N in the wild.")
    lines.append("Controlled-load DSC lives nested at `controlledLoad[0].singleRate.dailySupplyCharge` per spec.\n")

    # Section 6 — Enum value distributions (the meat)
    lines.append("## 6. Enum value distributions (observed)\n")
    for path, vals in sorted(enum_global.items()):
        c = Counter(vals)
        if not c:
            continue
        top = c.most_common(20)
        lines.append(f"### `{path}` — {sum(c.values()):,} occurrences across {len(c)} distinct values\n")
        for v, n in top:
            lines.append(f"- `{v}` × {n:,}")
        if len(c) > 20:
            lines.append(f"- _(+{len(c)-20} more values)_")
        lines.append("")

    # Section 7 — EV overlay candidates
    lines.append("## 7. EV-overlay zero-rate candidates (SM#710 watch)\n")
    lines.append(f"Plans with at least one `unitPrice == 0` inside tariffPeriod: **{len(ev_overlay_plans):,}**.\n")
    if ev_overlay_plans:
        lines.append("First 20 examples:\n")
        for slug, pid, ratio in ev_overlay_plans[:20]:
            lines.append(f"- `{slug}/{pid}` ({ratio})")
        lines.append("")

    # Section 8 — Surprises
    lines.append("## 8. Surprise findings\n")
    if surprises:
        for s in surprises:
            lines.append(f"- {s}")
    else:
        lines.append("- (none recorded)")
    lines.append("")

    # Section 9 — Recommended parser stub
    lines.append("## 9. Recommended defensive parser (Python stub)\n")
    lines.append("See `docs/parser-spec.md` for full implementation contract.\n")

    save_text(CATALOG_PATH, "\n".join(lines))


def render_enums(enum_global) -> None:
    lines = []
    lines.append("# CDR Energy PRD — Enum Reference\n")
    lines.append(f"_Generated by `cdr_full_sweep_v2.py` on "
                 f"{time.strftime('%Y-%m-%d', time.localtime())}._\n")
    lines.append("Compares CDS spec enum values vs values observed in the wild "
                 "across the full sweep. Discrepancies flagged.\n")
    SPEC = {
        "plan.type": ["STANDING", "MARKET", "REGULATED"],
        "plan.fuelType": ["ELECTRICITY", "GAS", "DUAL"],
        "plan.customerType": ["RESIDENTIAL", "BUSINESS"],
        "ec.pricingModel": ["SINGLE_RATE", "SINGLE_RATE_CONT_LOAD", "TIME_OF_USE",
                            "TIME_OF_USE_CONT_LOAD", "FLEXIBLE", "FLEXIBLE_CONT_LOAD", "QUOTA"],
        "ec.timeZone": ["LOCAL", "AEST"],
        "ec.termType": ["1_YEAR", "2_YEAR", "3_YEAR", "4_YEAR", "5_YEAR", "ONGOING", "OTHER"],
        "ec.paymentOption[]": ["PAPER_BILL", "CREDIT_CARD", "DIRECT_DEBIT", "BPAY", "OTHER"],
        "tp0.type": ["ENVIRONMENTAL", "REGULATED", "NETWORK", "METERING",
                     "RETAIL_SERVICE", "RCTI", "OTHER"],
        "tp0.dailySupplyChargeType": ["SINGLE", "BAND"],
        "tp0.timeZone": ["LOCAL", "AEST"],
        "tp0.rateBlockUType": ["singleRate", "timeOfUseRates", "demandCharges"],
        "tp.timeOfUseRates[].type": ["PEAK", "OFF_PEAK", "SHOULDER", "SHOULDER1", "SHOULDER2"],
        "tou.days[]": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
        "fit0.scheme": ["PREMIUM", "CURRENT", "VARIABLE", "OTHER"],
        "fit0.payerType": ["GOVERNMENT", "RETAILER"],
        "fit0.tariffUType": ["singleTariff", "timeVaryingTariffs"],
        "ec.incentives[].category": ["GIFT", "ACCOUNT_CREDIT", "OTHER"],
        "ec.discounts[].type": ["CONDITIONAL", "GUARANTEED", "OTHER"],
        "ec.discounts[].category": ["PAY_ON_TIME", "DIRECT_DEBIT", "GUARANTEED_DISCOUNT", "OTHER"],
        "ec.discounts[].methodUType": ["percentOfBill", "percentOfUse", "fixedAmount", "percentOverThreshold"],
        "ec.greenPowerCharges[].type": ["FIXED_PER_DAY", "FIXED_PER_WEEK", "FIXED_PER_MONTH",
                                         "FIXED_PER_UNIT", "PERCENT_OF_USE", "PERCENT_OF_BILL"],
        "ec.greenPowerCharges[].scheme": ["GREENPOWER", "OTHER"],
        "ec.fees[].type": ["EXIT", "ESTABLISHMENT", "LATE_PAYMENT", "DISCONNECTION",
                           "DISCONNECT_MOVE_OUT", "DISCONNECT_NON_PAY", "RECONNECTION",
                           "CONNECTION", "PAYMENT_PROCESSING", "CC_PROCESSING",
                           "CHEQUE_DISHONOUR", "DD_DISHONOUR", "MEMBERSHIP",
                           "CONTRIBUTION", "PAPER_BILL", "OTHER"],
        "ec.fees[].term": ["FIXED", "1_YEAR", "2_YEAR", "3_YEAR", "4_YEAR", "5_YEAR",
                           "PERCENT_OF_BILL", "ANNUAL", "DAILY", "WEEKLY", "MONTHLY",
                           "BIANNUAL", "VARIABLE"],
        "cl0.rateBlockUType": ["singleRate", "timeOfUseRates"],
    }
    for path, spec_vals in sorted(SPEC.items()):
        observed = enum_global.get(path) or []
        c = Counter(observed)
        spec_set = set(spec_vals)
        observed_set = set(c.keys())
        unseen = spec_set - observed_set
        novel = observed_set - spec_set
        lines.append(f"### `{path}`\n")
        lines.append(f"**Spec enum** ({len(spec_vals)}): {', '.join('`'+v+'`' for v in spec_vals)}")
        lines.append(f"**Observed**: {sum(c.values()):,} total")
        for v, n in c.most_common():
            star = " ⚠ NOT IN SPEC" if v not in spec_set else ""
            lines.append(f"- `{v}` × {n:,}{star}")
        if unseen:
            lines.append(f"**Spec values NOT observed**: {', '.join('`'+v+'`' for v in sorted(unseen))}")
        if novel:
            lines.append(f"**⚠ Observed values NOT in spec**: {', '.join('`'+v+'`' for v in sorted(novel))}")
        lines.append("")
    save_text(ENUMS_PATH, "\n".join(lines))


def save_text(path: str, content: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== Loading EME refdata2 ===", file=sys.stderr, flush=True)
    refdata = fetch_eme_refdata()
    retailers = build_retailer_list(refdata)
    print(f"  {len(retailers)} CDR-enrolled retailers", file=sys.stderr, flush=True)

    # Map shared base URIs
    base_to_brands = defaultdict(list)
    for r in retailers:
        base_to_brands[r["baseUri"]].append(r["cdrCode"])
    n_unique_bases = len(base_to_brands)
    n_shared = sum(1 for v in base_to_brands.values() if len(v) > 1)
    print(f"  {n_unique_bases} unique base URIs, {n_shared} shared", file=sys.stderr, flush=True)

    # Save retailer index
    save_json(RETAILER_INDEX_PATH, {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "retailers": retailers,
        "baseToBrands": dict(base_to_brands),
    }, indent=2)

    # Phase 1: parallel fetch
    print(f"\n=== Phase 1: parallel fetch ({PARALLEL_RETAILERS} workers) ===", file=sys.stderr, flush=True)
    stats: list[dict] = []
    with ThreadPoolExecutor(max_workers=PARALLEL_RETAILERS) as ex:
        futures = {ex.submit(process_retailer, r, base_to_brands): r for r in retailers}
        done = 0
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                stats.append(fut.result())
            except Exception as e:  # noqa: BLE001
                stats.append({
                    "cdrCode": r["cdrCode"], "slug": r["slug"], "base": r["baseUri"],
                    "tradingName": r.get("tradingName"), "orgName": r.get("orgName"),
                    "shared_base": False,
                    "plans_listed": 0, "plans_filtered": 0, "plans_fetched": 0,
                    "plans_failed": 0, "list_error": f"crash:{type(e).__name__}:{e}",
                })
            done += 1
            if done % 5 == 0 or done == len(futures):
                print(f"  [{done}/{len(futures)}]", file=sys.stderr, flush=True)

    # Phase 2: comprehensive signature extraction over all cached details
    print("\n=== Phase 2: signature extraction ===", file=sys.stderr, flush=True)
    sig_to_plans: dict[str, list[tuple[str, str]]] = defaultdict(list)
    sig_to_tokens: dict[str, list[str]] = {}
    enum_global: dict[str, list] = defaultdict(list)
    surprises: list[str] = []
    surprise_seen: set[str] = set()
    ev_overlay_plans: list[tuple[str, str, str]] = []

    n_processed = 0
    slug_to_stat = {s["slug"]: s for s in stats}
    for slug in os.listdir(CACHE_DIR):
        if slug.startswith("_"):
            continue
        d = os.path.join(CACHE_DIR, slug)
        if not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            if fname.startswith("_") or not fname.endswith(".json"):
                continue
            pid = fname[:-5]
            path = os.path.join(d, fname)
            detail = load_json(path)
            if detail is None:
                continue
            try:
                sig, tokens, snap, enums = extract_sig_v2(detail)
            except Exception as e:  # noqa: BLE001
                surprises.append(f"signature crash {slug}/{pid}: {type(e).__name__}:{e}")
                continue
            sig_to_plans[sig].append((slug, pid))
            if sig not in sig_to_tokens:
                sig_to_tokens[sig] = tokens
            for k, vs in enums.items():
                enum_global[k].extend(vs)
            for tok in tokens:
                if tok.startswith("FLAG.zero_rate_overlay:"):
                    ratio = tok.split(":", 1)[1]
                    ev_overlay_plans.append((slug, pid, ratio))
            n_processed += 1
            if n_processed % 1000 == 0:
                print(f"  processed {n_processed} plans, {len(sig_to_plans)} sigs", file=sys.stderr, flush=True)

    # Detect 404s from failed.jsonl
    if os.path.exists(FAILED_PATH):
        retailer_404s: Counter = Counter()
        with open(FAILED_PATH) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get("error") and "404" in str(rec["error"]):
                        retailer_404s[rec["slug"]] += 1
                except json.JSONDecodeError:
                    pass
        for slug, cnt in retailer_404s.most_common(10):
            surprises.append(f"`{slug}`: 404 on {cnt} plan(s) despite listing")

    # Render
    print("\n=== Phase 3: render ===", file=sys.stderr, flush=True)
    render_catalog(retailers, stats, sig_to_plans, sig_to_tokens,
                   enum_global, surprises, ev_overlay_plans)
    render_enums(enum_global)

    # Registry comparison artifact
    save_json(REGISTRY_CMP_PATH, {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "totalEmeOrgs": len(retailers),
        "uniqueBaseUris": n_unique_bases,
        "sharedBaseCount": n_shared,
        "stats": stats,
    }, indent=2)

    # Final stdout
    n_reachable = sum(1 for s in stats if not s.get("list_error"))
    n_fetched = sum(s["plans_fetched"] for s in stats)
    duration = time.time() - WALL_CLOCK_START
    print("---SUMMARY---")
    print(f"Retailers reachable: {n_reachable}/{len(stats)}")
    print(f"Plans fetched: {n_fetched:,}")
    print(f"Distinct shape signatures: {len(sig_to_plans):,}")
    print(f"EV-overlay candidates: {len(ev_overlay_plans):,}")
    print(f"Wall-clock: {duration:.1f}s")
    print(f"Catalog:  {CATALOG_PATH}")
    print(f"Enums:    {ENUMS_PATH}")
    print(f"Registry: {REGISTRY_CMP_PATH}")
    print(f"Index:    {RETAILER_INDEX_PATH}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted — progress checkpointed, resume by re-running.", file=sys.stderr)
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
