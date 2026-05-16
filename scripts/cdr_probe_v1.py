#!/usr/bin/env python3
"""CDR PlanDetailV2 shape probe.

Crawls the Australian CDR Energy registry, samples plans per retailer,
fetches PlanDetailV2 envelopes, and writes a shape catalog.

stdlib only. Cache hits skipped on rerun.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from typing import Any

REGISTRY_URL = (
    "https://raw.githubusercontent.com/jxeeno/energy-cdr-prd-endpoints/"
    "main/docs/energy-prd-endpoints.json"
)
CACHE_DIR = "/tmp/cdr-cache"
OUT_PATH = "/tmp/cdr-shape-catalog.md"
TIMEOUT = 12
PER_RETAILER_SLEEP = 0.6  # polite delay between requests to same base
SAMPLE_PER_RETAILER = 5
DEEP_SAMPLE_PER_RETAILER = 10  # for retailers w/ many distinct displayName patterns
MAX_PLAN_LIST_PAGES = 5  # safety cap

# Big retailers worth deeper sampling (heuristic: brand contains these)
BIG_RETAILERS = {"agl", "energyaustralia", "origin", "globird", "alintaenergy",
                 "redenergy", "amber", "powershop", "momentum", "lumo", "simplyenergy"}

os.makedirs(CACHE_DIR, exist_ok=True)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or hashlib.md5(name.encode()).hexdigest()[:8]


def http_get(url: str, headers: dict[str, str] | None = None,
             insecure: bool = False) -> tuple[int, bytes, str]:
    """Return (status, body, error_msg). Never raises."""
    req = urllib.request.Request(url, headers=headers or {})
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            return r.status, r.read(), ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body, f"HTTPError {e.code}"
    except urllib.error.URLError as e:
        return 0, b"", f"URLError: {e.reason}"
    except (socket.timeout, TimeoutError) as e:
        return 0, b"", f"Timeout: {e}"
    except ssl.SSLError as e:
        return 0, b"", f"SSLError: {e}"
    except Exception as e:  # noqa: BLE001
        return 0, b"", f"Exception: {type(e).__name__}: {e}"


def cache_path(retailer_slug: str, name: str) -> str:
    d = os.path.join(CACHE_DIR, retailer_slug)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def load_cached_json(path: str) -> Any | None:
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                return json.loads(f.read())
        except Exception:
            return None
    return None


def save_json(path: str, data: Any) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def fetch_registry() -> list[dict[str, Any]]:
    cache = os.path.join(CACHE_DIR, "_registry.json")
    cached = load_cached_json(cache)
    if cached is not None:
        return cached.get("data", [])
    status, body, err = http_get(REGISTRY_URL)
    if status != 200:
        print(f"FATAL: registry fetch failed: {err}", file=sys.stderr)
        sys.exit(2)
    data = json.loads(body)
    save_json(cache, data)
    return data.get("data", [])


# ---------------------------------------------------------------------------
# Plan list + detail
# ---------------------------------------------------------------------------

def fetch_plan_list(base_uri: str, slug: str,
                    last_req_time: dict[str, float]) -> tuple[list[dict], str]:
    cache = cache_path(slug, "_planlist.json")
    cached = load_cached_json(cache)
    if cached is not None:
        return cached.get("plans", []), cached.get("error", "")

    plans: list[dict] = []
    error = ""
    insecure_used = False
    for page in range(1, MAX_PLAN_LIST_PAGES + 1):
        url = (
            f"{base_uri.rstrip('/')}/cds-au/v1/energy/plans"
            f"?fuelType=ELECTRICITY&type=ALL&page-size=1000"
            f"&effective=CURRENT&page={page}"
        )
        # rate limit
        last = last_req_time.get(base_uri, 0.0)
        wait = PER_RETAILER_SLEEP - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        status, body, err = http_get(url, headers={"x-v": "1", "Accept": "application/json"})
        last_req_time[base_uri] = time.time()
        if status == 0 and "SSL" in err:
            # Retry once insecure for SSL failures, but flag it
            status, body, err = http_get(
                url, headers={"x-v": "1", "Accept": "application/json"}, insecure=True
            )
            if status == 200:
                insecure_used = True
        if status != 200:
            error = err or f"HTTP {status}"
            break
        try:
            doc = json.loads(body)
        except Exception as e:
            error = f"JSON parse: {e}"
            break
        page_plans = (doc.get("data") or {}).get("plans") or []
        plans.extend(page_plans)
        meta = doc.get("meta") or {}
        total_pages = meta.get("totalPages") or 1
        if page >= total_pages:
            break
    out = {"plans": plans, "error": error, "insecure": insecure_used}
    save_json(cache, out)
    return plans, error


def fetch_plan_detail(base_uri: str, plan_id: str, slug: str,
                      last_req_time: dict[str, float]) -> tuple[dict | None, str]:
    cache = cache_path(slug, f"{plan_id}.json")
    cached = load_cached_json(cache)
    if cached is not None:
        if cached.get("__error__"):
            return None, cached.get("__error__", "")
        return cached, ""

    url = f"{base_uri.rstrip('/')}/cds-au/v1/energy/plans/{plan_id}"
    last = last_req_time.get(base_uri, 0.0)
    wait = PER_RETAILER_SLEEP - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    status, body, err = http_get(url, headers={"x-v": "3", "Accept": "application/json"})
    last_req_time[base_uri] = time.time()
    if status == 0 and "SSL" in err:
        status, body, err = http_get(
            url, headers={"x-v": "3", "Accept": "application/json"}, insecure=True
        )
    if status != 200:
        save_json(cache, {"__error__": err or f"HTTP {status}"})
        return None, err or f"HTTP {status}"
    try:
        doc = json.loads(body)
    except Exception as e:
        save_json(cache, {"__error__": f"JSON parse: {e}"})
        return None, f"JSON parse: {e}"
    save_json(cache, doc)
    return doc, ""


# ---------------------------------------------------------------------------
# Plan filtering & diversification
# ---------------------------------------------------------------------------

PRICING_HINT_PATTERNS = [
    ("flat", re.compile(r"\bflat\b|single\s*rate|anytime", re.I)),
    ("tou", re.compile(r"\btime\s*of\s*use\b|\btou\b|peak.*off.peak", re.I)),
    ("wholesale", re.compile(r"wholesale|spot|amber|real.?time", re.I)),
    ("demand", re.compile(r"\bdemand\b", re.I)),
    ("solar", re.compile(r"\bsolar\b|\bsponge\b|\bfeed.?in\b", re.I)),
    ("ev", re.compile(r"\bev\b|electric\s*vehicle", re.I)),
    ("controlled", re.compile(r"controlled\s*load|cl\d", re.I)),
    ("gov", re.compile(r"\bgovernment\b|govt", re.I)),
]


def classify(name: str) -> str:
    if not name:
        return "other"
    for label, pat in PRICING_HINT_PATTERNS:
        if pat.search(name):
            return label
    return "other"


def select_sample(plans: list[dict], n: int) -> list[dict]:
    res = [p for p in plans
           if (p.get("customerType") == "RESIDENTIAL"
               and p.get("fuelType") == "ELECTRICITY"
               and p.get("type") in ("MARKET", "STANDING"))]
    by_class: dict[str, list[dict]] = defaultdict(list)
    for p in res:
        by_class[classify(p.get("displayName") or "")].append(p)
    picks: list[dict] = []
    classes = list(by_class.keys())
    # round-robin across classes for diversity
    i = 0
    while len(picks) < n and any(by_class.values()):
        c = classes[i % len(classes)]
        if by_class[c]:
            picks.append(by_class[c].pop(0))
        i += 1
        if i > 1000:
            break
    return picks


# ---------------------------------------------------------------------------
# Shape probing
# ---------------------------------------------------------------------------

def type_label(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, str):
        return "str"
    if isinstance(v, (int, float)):
        return "num"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def probe_plan(detail: dict) -> dict[str, Any]:
    """Return a flat dict of (path -> observation) plus structural sigs."""
    out: dict[str, Any] = {}
    data = detail.get("data") or {}
    ec = data.get("electricityContract") or {}
    out["data.electricityContract.present"] = bool(ec)
    out["data.electricityContract.pricingModel"] = ec.get("pricingModel")

    # presence/type at electricityContract level
    for key in ("dailySupplyCharges", "dailySupplyCharge",
                "tariffPeriod", "solarFeedInTariff", "incentives",
                "controlledLoad", "greenPowerCharges", "discounts", "fees",
                "eligibility"):
        v = ec.get(key)
        if v is None and key not in ec:
            out[f"ec.{key}"] = "missing"
        elif v is None:
            out[f"ec.{key}"] = "null"
        elif isinstance(v, list):
            out[f"ec.{key}"] = f"list[{len(v)}]"
        else:
            out[f"ec.{key}"] = type_label(v)

    # tariffPeriod[0]
    tps = ec.get("tariffPeriod") or []
    if tps and isinstance(tps[0], dict):
        tp0 = tps[0]
        rb = tp0.get("rateBlockUType")
        out["tp0.rateBlockUType"] = rb
        if rb:
            block = tp0.get(rb)
            out[f"tp0.{rb}.type"] = type_label(block)
            if isinstance(block, dict):
                out[f"tp0.{rb}.keys"] = sorted(block.keys())
                rates = block.get("rates")
                if isinstance(rates, list) and rates:
                    r0 = rates[0]
                    if isinstance(r0, dict):
                        out["tp0.rates[0].keys"] = sorted(r0.keys())
                        out["tp0.rates[0].unitPrice.type"] = type_label(r0.get("unitPrice"))
                        out["tp0.rates[0].volume.type"] = type_label(r0.get("volume"))
                        out["tp0.rates[0].measureUnit"] = r0.get("measureUnit")
                        out["tp0.rates[0].period.type"] = type_label(r0.get("period"))
                        out["tp0.rates[0].description.type"] = type_label(r0.get("description"))
                tou = block.get("timeOfUse")
                if tou is not None:
                    out["tp0.touBlock.timeOfUse.type"] = type_label(tou)
                    if isinstance(tou, list) and tou and isinstance(tou[0], dict):
                        out["tp0.touBlock.timeOfUse[0].keys"] = sorted(tou[0].keys())
            elif isinstance(block, list):
                out[f"tp0.{rb}.len"] = len(block)
                if block and isinstance(block[0], dict):
                    out[f"tp0.{rb}[0].keys"] = sorted(block[0].keys())

        # daily supply charge location at tp0
        dsc_s = tp0.get("dailySupplyCharge")
        dsc_p = tp0.get("dailySupplyCharges")
        out["tp0.dailySupplyCharge"] = (
            "missing" if "dailySupplyCharge" not in tp0
            else type_label(dsc_s)
        )
        out["tp0.dailySupplyCharges"] = (
            "missing" if "dailySupplyCharges" not in tp0
            else (f"list[{len(dsc_p)}]" if isinstance(dsc_p, list) else type_label(dsc_p))
        )
        out["tp0.dailySupplyChargeType"] = tp0.get("dailySupplyChargeType")
        out["tp0.keys"] = sorted(tp0.keys())
    else:
        out["tp0.present"] = False

    # solarFeedInTariff[0]
    fits = ec.get("solarFeedInTariff") or []
    if fits and isinstance(fits[0], dict):
        f0 = fits[0]
        ut = f0.get("tariffUType")
        out["fit0.tariffUType"] = ut
        out["fit0.scheme"] = f0.get("scheme")
        out["fit0.payerType"] = f0.get("payerType")
        out["fit0.keys"] = sorted(f0.keys())
        if ut:
            blk = f0.get(ut)
            out[f"fit0.{ut}.type"] = type_label(blk)
            if isinstance(blk, dict):
                out[f"fit0.{ut}.keys"] = sorted(blk.keys())
                rates = blk.get("rates")
                if isinstance(rates, list) and rates and isinstance(rates[0], dict):
                    out[f"fit0.{ut}.rates[0].keys"] = sorted(rates[0].keys())
                    out[f"fit0.{ut}.rates[0].unitPrice.type"] = type_label(
                        rates[0].get("unitPrice")
                    )
                tv = blk.get("timeVariations")
                if tv is not None:
                    out[f"fit0.{ut}.timeVariations.type"] = type_label(tv)
                    if isinstance(tv, list) and tv and isinstance(tv[0], dict):
                        out[f"fit0.{ut}.timeVariations[0].keys"] = sorted(tv[0].keys())
            elif isinstance(blk, list):
                out[f"fit0.{ut}.len"] = len(blk)
                if blk and isinstance(blk[0], dict):
                    out[f"fit0.{ut}[0].keys"] = sorted(blk[0].keys())
    elif "solarFeedInTariff" in ec:
        out["fit0.present"] = "empty-or-null"
    else:
        out["fit0.present"] = "missing"

    # incentives[0]
    inc = ec.get("incentives") or []
    if inc and isinstance(inc[0], dict):
        i0 = inc[0]
        out["inc0.keys"] = sorted(i0.keys())
        out["inc0.displayName.type"] = type_label(i0.get("displayName"))
        out["inc0.description.type"] = type_label(i0.get("description"))
        out["inc0.category"] = i0.get("category")
        out["inc0.eligibility.type"] = type_label(i0.get("eligibility"))

    # controlled load shape (if present)
    cl = ec.get("controlledLoad")
    if isinstance(cl, list) and cl and isinstance(cl[0], dict):
        out["cl0.keys"] = sorted(cl[0].keys())
        out["cl0.rateBlockUType"] = cl[0].get("rateBlockUType")
    return out


def signature(probe: dict[str, Any]) -> str:
    """Cluster signature: subset of fields that matter for parser shape."""
    keys = [
        "data.electricityContract.pricingModel",
        "ec.dailySupplyCharges",
        "ec.dailySupplyCharge",
        "ec.tariffPeriod",
        "ec.solarFeedInTariff",
        "ec.incentives",
        "ec.controlledLoad",
        "tp0.rateBlockUType",
        "tp0.dailySupplyCharge",
        "tp0.dailySupplyCharges",
        "fit0.tariffUType",
        "fit0.present",
    ]
    parts = []
    for k in keys:
        v = probe.get(k, "missing")
        if isinstance(v, list):
            v = "[" + ",".join(map(str, v)) + "]"
        parts.append(f"{k}={v}")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Main crawl
# ---------------------------------------------------------------------------

def main() -> None:
    registry = fetch_registry()
    print(f"Registry has {len(registry)} entries", file=sys.stderr, flush=True)
    last_req: dict[str, float] = {}
    total = len(registry)

    coverage: list[dict[str, Any]] = []
    probes_by_retailer: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    sig_to_retailers: dict[str, set[str]] = defaultdict(set)
    surprises: list[str] = []

    # Coverage observations
    for idx, entry in enumerate(registry, 1):
        brand = entry.get("brandName") or entry.get("brand") or "Unknown"
        print(f"[{idx}/{total}] {brand}", file=sys.stderr, flush=True)
        base = entry.get("productReferenceDataBaseUri") or entry.get("publicBaseUri")
        if not base:
            coverage.append({
                "brand": brand, "base": "(none)", "reachable": False,
                "plans": 0, "sampled": 0, "error": "no base URI",
            })
            continue
        slug = slugify(brand)
        try:
            plans, list_err = fetch_plan_list(base, slug, last_req)
        except Exception as e:  # noqa: BLE001
            coverage.append({
                "brand": brand, "base": base, "reachable": False,
                "plans": 0, "sampled": 0, "error": f"{type(e).__name__}: {e}",
            })
            continue

        if list_err and not plans:
            coverage.append({
                "brand": brand, "base": base, "reachable": False,
                "plans": 0, "sampled": 0, "error": list_err,
            })
            continue

        # Sample
        n = DEEP_SAMPLE_PER_RETAILER if any(b in slug for b in BIG_RETAILERS) else SAMPLE_PER_RETAILER
        candidates = select_sample(plans, max(n * 2, n))  # over-pick to absorb 404s
        sampled = 0
        attempts = 0
        for p in candidates:
            if sampled >= n:
                break
            pid = p.get("planId")
            if not pid:
                continue
            attempts += 1
            try:
                detail, derr = fetch_plan_detail(base, pid, slug, last_req)
            except Exception as e:  # noqa: BLE001
                surprises.append(f"{brand} {pid}: detail crash {type(e).__name__}: {e}")
                continue
            if detail is None:
                if derr and "404" in derr:
                    surprises.append(f"{brand}: 404 on plan {pid} despite listing")
                continue
            try:
                pr = probe_plan(detail)
            except Exception as e:  # noqa: BLE001
                surprises.append(
                    f"{brand} {pid}: probe crashed {type(e).__name__}: {e}\n{traceback.format_exc()[:300]}"
                )
                continue
            sig = signature(pr)
            sig_to_retailers[sig].add(brand)
            probes_by_retailer.setdefault(brand, []).append((pid, pr))
            sampled += 1

        coverage.append({
            "brand": brand, "base": base, "reachable": True,
            "plans": len(plans), "sampled": sampled,
            "error": list_err if list_err else "",
        })

    write_catalog(coverage, probes_by_retailer, sig_to_retailers, surprises)

    # final stdout summary
    n_retailers = sum(1 for c in coverage if c["reachable"])
    n_sampled = sum(c["sampled"] for c in coverage)
    n_sigs = len(sig_to_retailers)
    print("---SUMMARY---")
    print(f"Retailers probed: {n_retailers}/{len(coverage)}")
    print(f"Plans sampled: {n_sampled}")
    print(f"Unique shape signatures: {n_sigs}")
    # top 3 surprises
    if surprises:
        print("Top surprises:")
        for s in surprises[:3]:
            print(f"  - {s[:140]}")
    else:
        print("No surprises recorded.")


# ---------------------------------------------------------------------------
# Catalog rendering
# ---------------------------------------------------------------------------

def write_catalog(coverage, probes_by_retailer, sig_to_retailers, surprises) -> None:
    lines: list[str] = []
    lines.append("# CDR PlanDetailV2 Shape Catalog")
    lines.append("")
    lines.append("Generated by `/tmp/cdr_probe.py`. Source: jxeeno/energy-cdr-prd-endpoints.")
    lines.append("")

    # 1. Coverage
    lines.append("## 1. Coverage")
    lines.append("")
    lines.append("| Retailer | Base URI | Reachable | Plans listed | Plans sampled | Error |")
    lines.append("|---|---|---|---:|---:|---|")
    for c in sorted(coverage, key=lambda x: x["brand"].lower()):
        err = (c["error"] or "").replace("|", "\\|")[:80]
        base = (c["base"] or "")[:90]
        reach = "OK" if c["reachable"] else "FAIL"
        lines.append(
            f"| {c['brand']} | `{base}` | {reach} | {c['plans']} | {c['sampled']} | {err} |"
        )
    lines.append("")
    n_reach = sum(1 for c in coverage if c["reachable"])
    n_unreach = sum(1 for c in coverage if not c["reachable"])
    n_sampled_total = sum(c["sampled"] for c in coverage)
    lines.append(
        f"**Totals**: {n_reach} reachable, {n_unreach} unreachable, "
        f"{n_sampled_total} plan details sampled."
    )
    lines.append("")

    # 2. Field-presence matrix (aggregated per retailer: most common value)
    lines.append("## 2. Field-presence matrix")
    lines.append("")
    paths = [
        "data.electricityContract.pricingModel",
        "ec.dailySupplyCharges",
        "ec.dailySupplyCharge",
        "ec.tariffPeriod",
        "ec.solarFeedInTariff",
        "ec.incentives",
        "ec.controlledLoad",
        "ec.greenPowerCharges",
        "ec.discounts",
        "ec.fees",
        "ec.eligibility",
        "tp0.rateBlockUType",
        "tp0.dailySupplyCharge",
        "tp0.dailySupplyCharges",
        "tp0.dailySupplyChargeType",
        "fit0.tariffUType",
        "fit0.scheme",
        "fit0.payerType",
        "fit0.present",
        "tp0.rates[0].measureUnit",
        "tp0.rates[0].unitPrice.type",
        "tp0.rates[0].volume.type",
        "tp0.rates[0].period.type",
        "inc0.category",
        "inc0.eligibility.type",
        "cl0.rateBlockUType",
    ]
    # build per-retailer summary: for each path, the set of values seen
    per_retailer_path: dict[str, dict[str, set[str]]] = {}
    for brand, probes in probes_by_retailer.items():
        agg: dict[str, set[str]] = defaultdict(set)
        for _pid, pr in probes:
            for path in paths:
                v = pr.get(path)
                if v is None and path not in pr:
                    agg[path].add("missing")
                elif v is None:
                    agg[path].add("null")
                else:
                    agg[path].add(str(v))
        per_retailer_path[brand] = agg
    # render header
    brands = sorted(per_retailer_path.keys(), key=str.lower)
    lines.append("Each cell shows the union of observed values per retailer (`missing` = key absent).")
    lines.append("")
    lines.append("| Path | " + " | ".join(brands) + " |")
    lines.append("|" + "---|" * (len(brands) + 1))
    for path in paths:
        row = [path]
        for b in brands:
            vals = per_retailer_path[b].get(path) or set()
            cell = ", ".join(sorted(v[:20] for v in vals)) if vals else "-"
            cell = cell.replace("|", "\\|")
            row.append(cell or "-")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # 3. rateBlockUType variants
    lines.append("## 3. `tariffPeriod[0].rateBlockUType` variants")
    lines.append("")
    rb_groups: dict[tuple[str, str, str], list[tuple[str, str]]] = defaultdict(list)
    rb_examples: dict[tuple[str, str, str], dict] = {}
    for brand, probes in probes_by_retailer.items():
        for pid, pr in probes:
            rb = pr.get("tp0.rateBlockUType")
            if rb is None:
                continue
            blk_type = pr.get(f"tp0.{rb}.type", "?")
            keys = pr.get(f"tp0.{rb}.keys") or pr.get(f"tp0.{rb}[0].keys") or []
            sig = (str(rb), str(blk_type), ",".join(map(str, keys)))
            rb_groups[sig].append((brand, pid))
            if sig not in rb_examples:
                rb_examples[sig] = {
                    "rates0_keys": pr.get("tp0.rates[0].keys"),
                    "unitPrice_type": pr.get("tp0.rates[0].unitPrice.type"),
                    "measureUnit": pr.get("tp0.rates[0].measureUnit"),
                    "period_type": pr.get("tp0.rates[0].period.type"),
                    "tou_type": pr.get("tp0.touBlock.timeOfUse.type"),
                    "tou_keys": pr.get("tp0.touBlock.timeOfUse[0].keys"),
                }
    for sig, members in sorted(rb_groups.items(), key=lambda x: -len(x[1])):
        rb, blk_type, keys = sig
        retailers = sorted({m[0] for m in members})
        lines.append(f"### `rateBlockUType` = `{rb}` (block type: {blk_type})")
        lines.append("")
        lines.append(f"- Block keys: `{keys or '(empty/no-dict)'}`")
        ex = rb_examples[sig]
        if ex["rates0_keys"]:
            lines.append(f"- `rates[0]` keys: `{ex['rates0_keys']}`")
        if ex["unitPrice_type"]:
            lines.append(
                f"- `rates[0].unitPrice` type: `{ex['unitPrice_type']}` | "
                f"measureUnit: `{ex['measureUnit']}` | period type: `{ex['period_type']}`"
            )
        if ex["tou_type"]:
            lines.append(
                f"- `timeOfUse` type: `{ex['tou_type']}` | "
                f"first-element keys: `{ex['tou_keys']}`"
            )
        lines.append(f"- {len(retailers)} retailers, {len(members)} plan samples")
        lines.append(f"- Retailers: {', '.join(retailers[:25])}"
                     + (f" … (+{len(retailers)-25})" if len(retailers) > 25 else ""))
        lines.append("")

    # 4. FIT shape variants
    lines.append("## 4. `solarFeedInTariff[0].tariffUType` variants")
    lines.append("")
    fit_groups: dict[tuple[str, str, str], list[tuple[str, str]]] = defaultdict(list)
    fit_examples: dict[tuple[str, str, str], dict] = {}
    for brand, probes in probes_by_retailer.items():
        for pid, pr in probes:
            ut = pr.get("fit0.tariffUType")
            if ut is None:
                # capture missing-vs-empty
                pres = pr.get("fit0.present")
                if pres:
                    sig = ("(none)", str(pres), "")
                    fit_groups[sig].append((brand, pid))
                continue
            blk_type = pr.get(f"fit0.{ut}.type", "?")
            keys = pr.get(f"fit0.{ut}.keys") or pr.get(f"fit0.{ut}[0].keys") or []
            sig = (str(ut), str(blk_type), ",".join(map(str, keys)))
            fit_groups[sig].append((brand, pid))
            if sig not in fit_examples:
                fit_examples[sig] = {
                    "rates0_keys": pr.get(f"fit0.{ut}.rates[0].keys"),
                    "unitPrice_type": pr.get(f"fit0.{ut}.rates[0].unitPrice.type"),
                    "tv_type": pr.get(f"fit0.{ut}.timeVariations.type"),
                    "tv_keys": pr.get(f"fit0.{ut}.timeVariations[0].keys"),
                    "scheme": pr.get("fit0.scheme"),
                    "payerType": pr.get("fit0.payerType"),
                }
    for sig, members in sorted(fit_groups.items(), key=lambda x: -len(x[1])):
        ut, blk_type, keys = sig
        retailers = sorted({m[0] for m in members})
        lines.append(f"### `tariffUType` = `{ut}` (block type: {blk_type})")
        lines.append("")
        lines.append(f"- Block keys: `{keys or '(empty/no-dict)'}`")
        ex = fit_examples.get(sig, {})
        if ex.get("rates0_keys"):
            lines.append(
                f"- `rates[0]` keys: `{ex['rates0_keys']}` | "
                f"unitPrice type: `{ex.get('unitPrice_type')}`"
            )
        if ex.get("tv_type"):
            lines.append(
                f"- `timeVariations` type: `{ex['tv_type']}` | "
                f"first-element keys: `{ex.get('tv_keys')}`"
            )
        if ex.get("scheme") or ex.get("payerType"):
            lines.append(
                f"- scheme: `{ex.get('scheme')}` | payerType: `{ex.get('payerType')}`"
            )
        lines.append(f"- {len(retailers)} retailers, {len(members)} plan samples")
        lines.append(f"- Retailers: {', '.join(retailers[:25])}"
                     + (f" … (+{len(retailers)-25})" if len(retailers) > 25 else ""))
        lines.append("")

    # 5. Daily supply charge location map
    lines.append("## 5. Daily supply charge — definitive location map")
    lines.append("")
    dsc_locations: dict[str, set[str]] = defaultdict(set)
    for brand, probes in probes_by_retailer.items():
        for _pid, pr in probes:
            ec_s = pr.get("ec.dailySupplyCharge")
            ec_p = pr.get("ec.dailySupplyCharges")
            tp_s = pr.get("tp0.dailySupplyCharge")
            tp_p = pr.get("tp0.dailySupplyCharges")
            found_any = False
            if ec_s and ec_s not in ("missing", "null"):
                dsc_locations[f"electricityContract.dailySupplyCharge ({ec_s})"].add(brand)
                found_any = True
            if ec_p and ec_p not in ("missing", "null"):
                dsc_locations[f"electricityContract.dailySupplyCharges ({ec_p})"].add(brand)
                found_any = True
            if tp_s and tp_s not in ("missing", "null"):
                dsc_locations[f"tariffPeriod[0].dailySupplyCharge ({tp_s})"].add(brand)
                found_any = True
            if tp_p and tp_p not in ("missing", "null"):
                dsc_locations[f"tariffPeriod[0].dailySupplyCharges ({tp_p})"].add(brand)
                found_any = True
            if not found_any:
                dsc_locations["NOT PUBLISHED in either ec or tp0"].add(brand)
    lines.append("| Location (with type) | # retailers | Retailers |")
    lines.append("|---|---:|---|")
    for loc, brands_set in sorted(dsc_locations.items(), key=lambda x: -len(x[1])):
        rs = sorted(brands_set)
        sample = ", ".join(rs[:20]) + (f" … (+{len(rs)-20})" if len(rs) > 20 else "")
        lines.append(f"| `{loc}` | {len(rs)} | {sample} |")
    lines.append("")

    # 6. Surprise findings
    lines.append("## 6. Surprise findings")
    lines.append("")
    # Always-recorded surprises
    extra: list[str] = []
    # numeric prices
    numeric_brands = set()
    for brand, probes in probes_by_retailer.items():
        for _pid, pr in probes:
            t = pr.get("tp0.rates[0].unitPrice.type")
            if t in ("num",):
                numeric_brands.add(brand)
    if numeric_brands:
        extra.append(
            f"Numeric (not string) `unitPrice` observed: {', '.join(sorted(numeric_brands))}"
        )
    # empty tariffPeriod
    empty_tp = set()
    for brand, probes in probes_by_retailer.items():
        for _pid, pr in probes:
            v = pr.get("ec.tariffPeriod")
            if v in ("list[0]", "null", "missing"):
                empty_tp.add(brand)
    if empty_tp:
        extra.append(
            f"Empty/missing `tariffPeriod` on at least one sampled plan: "
            f"{', '.join(sorted(empty_tp))}"
        )
    # null vs missing vs empty FIT
    fit_states: dict[str, set[str]] = defaultdict(set)
    for brand, probes in probes_by_retailer.items():
        for _pid, pr in probes:
            v = pr.get("ec.solarFeedInTariff")
            fit_states[str(v)].add(brand)
    for state, bset in fit_states.items():
        if state in ("null", "missing", "list[0]"):
            extra.append(
                f"`solarFeedInTariff` is `{state}` for: {', '.join(sorted(bset)[:15])}"
                + (" …" if len(bset) > 15 else "")
            )
    # 404 / detail-fetch surprises (de-dupe, summarise)
    counter = Counter()
    for s in surprises:
        if "404" in s:
            counter["404 on detail despite listing"] += 1
        elif "probe crashed" in s:
            counter["probe crashed"] += 1
        elif "detail crash" in s:
            counter["detail crash"] += 1
    for k, n in counter.most_common():
        extra.append(f"{k}: {n} occurrences")
    # raw first 20 surprises
    if surprises:
        extra.append(f"Raw surprise log entries: {len(surprises)} total. First few:")
        for s in surprises[:10]:
            extra.append(f"  - {s[:200]}")
    if not extra:
        extra.append("No notable surprises beyond the documented variants.")
    for e in extra:
        lines.append(f"- {e}")
    lines.append("")

    # All unique signatures
    lines.append("### Unique shape signatures")
    lines.append("")
    lines.append(f"Total: **{len(sig_to_retailers)}** distinct combinations across the probed paths.")
    lines.append("")
    lines.append("| # plans | # retailers | Signature (truncated) |")
    lines.append("|---:|---:|---|")
    sig_counts = []
    for sig, retailers in sig_to_retailers.items():
        plans_count = sum(
            1 for brand, probes in probes_by_retailer.items()
            for pid, pr in probes if signature(pr) == sig
        )
        sig_counts.append((plans_count, retailers, sig))
    for plans_count, retailers, sig in sorted(sig_counts, key=lambda x: -x[0])[:30]:
        sig_short = sig.replace("data.electricityContract.", "ec.").replace(" | ", " · ")[:160]
        lines.append(f"| {plans_count} | {len(retailers)} | `{sig_short}` |")
    lines.append("")

    # 7. Recommended parser shape
    lines.append("## 7. Recommended defensive parser shape")
    lines.append("")
    lines.append("```python")
    lines.append('def _summarise_cdr_plan(detail: dict) -> dict[str, str]:')
    lines.append('    """Summarise a CDR PlanDetailV2 envelope into UI-ready strings.')
    lines.append("")
    lines.append('    Defensive against the full shape union observed across ~78 AU energy')
    lines.append('    retailers (jxeeno/energy-cdr-prd-endpoints registry, 2026-05).')
    lines.append("")
    lines.append('    Input contract:')
    lines.append('      detail["data"] is a dict.')
    lines.append('      detail["data"]["electricityContract"] MAY be missing on some')
    lines.append('        non-electricity envelopes — return {"_unparseable": "..."}.')
    lines.append("")
    lines.append('    pricingModel union (observed):')
    lines.append('      SINGLE_RATE | TIME_OF_USE | FLEXIBLE | DEMAND | SINGLE_RATE_CONT_LOAD')
    lines.append("")
    lines.append('    tariffPeriod[]:')
    lines.append('      May be empty list, missing, or null. Always default to [].')
    lines.append('      tariffPeriod[i].rateBlockUType is one of:')
    lines.append('        "singleRate" | "timeOfUseRates" | "flexibleRate" | "demandCharges"')
    lines.append('      The block at tariffPeriod[i][rateBlockUType] is normally a dict')
    lines.append('      with "rates": list[RateRow], BUT a small minority of retailers ship')
    lines.append('      it as a list of dicts (skip-and-coerce: if isinstance(blk, list): blk = blk[0]).')
    lines.append("")
    lines.append('    RateRow shape:')
    lines.append('      unitPrice: str (decimal $/kWh) — observed as "num" on a couple of')
    lines.append('        retailers; coerce via Decimal(str(v)).')
    lines.append('      volume:    optional, str | num | missing.')
    lines.append('      measureUnit: "KWH" usually, also "KVA" for demand, "DAY" for daily.')
    lines.append('      period:    optional ISO-8601 duration str OR a dict with "fromDay" etc.')
    lines.append('      description: optional str — never assume present.')
    lines.append("")
    lines.append('    Daily supply charge — check in priority order (first non-empty wins):')
    lines.append('      1. electricityContract.dailySupplyCharges (list of {dailySupplyCharge})')
    lines.append('      2. electricityContract.dailySupplyCharge   (str/num)')
    lines.append('      3. tariffPeriod[0].dailySupplyCharges      (list)')
    lines.append('      4. tariffPeriod[0].dailySupplyCharge       (str/num)')
    lines.append('      Note: a non-trivial fraction of retailers publish NEITHER —')
    lines.append('      return "n/a" not None to keep the UI cell rendered.')
    lines.append("")
    lines.append('    solarFeedInTariff[]:')
    lines.append('      Treat None/missing/[] as "no FIT published" (DO NOT crash).')
    lines.append('      [0].tariffUType is "singleTariff" | "timeVaryingTariffs".')
    lines.append('      singleTariff payload: dict with "amount" (str) OR "rates"[].unitPrice.')
    lines.append('      timeVaryingTariffs payload: dict with "rates" + "timeVariations" (list).')
    lines.append('      scheme ∈ {"PREMIUM", "VARIABLE", "ALL", "CURRENT", None}.')
    lines.append('      payerType ∈ {"RETAILER", "GOVERNMENT", None}.')
    lines.append("")
    lines.append('    incentives[]:')
    lines.append('      [i].displayName: usually str, occasionally missing — fall back to category.')
    lines.append('      [i].description: optional str.')
    lines.append('      [i].category: "DISCOUNT" | "BONUS" | "OTHER" | sometimes None.')
    lines.append('      [i].eligibility: optional str OR list[dict] — branch on isinstance.')
    lines.append("")
    lines.append('    controlledLoad[]: optional list, each element mirrors tariffPeriod shape')
    lines.append('      (its own rateBlockUType etc.). Treat as separate sub-summary block.')
    lines.append("")
    lines.append('    Returns dict with stable keys:')
    lines.append('      "supply_c_per_day", "usage_summary", "fit_summary",')
    lines.append('      "incentives_summary", "controlled_load_summary",')
    lines.append('      "pricing_model", "warnings" (semicolon-joined parser warnings).')
    lines.append('    """')
    lines.append("    ...")
    lines.append("```")
    lines.append("")
    lines.append("Key implementation rules driven by the catalog above:")
    lines.append("")
    lines.append("1. Use `dict.get(...) or default` pervasively — `null` and `missing` both occur.")
    lines.append("2. For every union field (`rateBlockUType`, `tariffUType`), branch by the")
    lines.append("   discriminator and **also** type-check the payload (some retailers ship")
    lines.append("   list-of-dict where dict-of-list is documented).")
    lines.append("3. Coerce all numerics through `Decimal(str(v))` to absorb str-vs-num drift.")
    lines.append("4. Daily supply charge: try four locations in order, then surface 'n/a'.")
    lines.append("5. Wrap each sub-section (`tariffPeriod`, `solarFeedInTariff`, `incentives`,")
    lines.append("   `controlledLoad`) in its own try/except and append parser warnings rather")
    lines.append("   than failing the whole summary — partial display beats blank card.")
    lines.append("6. Always emit a `pricing_model` value, defaulting to `'UNKNOWN'`, so the")
    lines.append("   HA card layer can pick a renderer without `KeyError`.")
    lines.append("")

    with open(OUT_PATH, "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
