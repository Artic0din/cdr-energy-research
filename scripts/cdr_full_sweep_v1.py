#!/usr/bin/env python3
"""Full CDR PlanDetailV2 sweep + signature catalog.

Stdlib-only. Resumable. Polite (1 req/sec/retailer, 6-way parallel).
Cache layout reuses /tmp/cdr-cache/{slug}/{planId}.json from earlier probe.

Output:
- /tmp/cdr-shape-catalog-full.md
- /tmp/cdr-cache/_progress.json (checkpoint)
- /tmp/cdr-cache/_failed.jsonl (per-plan failures)
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

CACHE_DIR = "/tmp/cdr-cache"
PROGRESS_PATH = os.path.join(CACHE_DIR, "_progress.json")
FAILED_PATH = os.path.join(CACHE_DIR, "_failed.jsonl")
REGISTRY_URL = "https://raw.githubusercontent.com/jxeeno/energy-cdr-prd-endpoints/main/docs/energy-prd-endpoints.json"
REGISTRY_CACHE = os.path.join(CACHE_DIR, "_registry.json")
CATALOG_PATH = "/tmp/cdr-shape-catalog-full.md"

REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
PARALLEL_RETAILERS = 12
PAGE_SIZE = 1000
PER_RETAILER_GAP = 1.0  # seconds between requests to same retailer

WALL_CLOCK_START = time.time()

# Per-retailer locks ensure serial calls per retailer
retailer_locks: dict[str, Lock] = defaultdict(Lock)
last_request_at: dict[str, float] = defaultdict(float)

# Append lock for failed.jsonl
failed_lock = Lock()
# Progress write lock
progress_lock = Lock()

os.makedirs(CACHE_DIR, exist_ok=True)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "unknown"


def http_get(url: str, headers: dict[str, str] | None = None) -> tuple[Any | None, str | None, int]:
    """Returns (json, error_msg, status_code)."""
    req = urllib.request.Request(url, headers=headers or {})
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
    """Rate-limited per slug, with retry on 429/5xx."""
    backoff = 2.0
    last_err = None
    for attempt in range(MAX_RETRIES):
        # Acquire per-retailer lock so calls serialize
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
        # Retry on 429 or 5xx or transient net error
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


def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def append_failed(rec: dict) -> None:
    with failed_lock, open(FAILED_PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")


# ------------------------------------------------------------------
# Registry + listing
# ------------------------------------------------------------------

def fetch_registry() -> list[dict]:
    cached = load_json(REGISTRY_CACHE)
    if cached and isinstance(cached, dict) and "data" in cached:
        return cached["data"]
    j, err, _ = http_get(REGISTRY_URL)
    if not j:
        raise RuntimeError(f"Registry fetch failed: {err}")
    save_json(REGISTRY_CACHE, j)
    return j["data"]


def fetch_plan_list(base: str, slug: str) -> tuple[list[dict], str | None]:
    """Paginated full plan list. Cached as _planlist.json."""
    cache_file = os.path.join(cache_dir(slug), "_planlist.json")
    cached = load_json(cache_file)
    if cached is not None:
        # Old probe stored full dict response; new format stores list
        if isinstance(cached, list):
            return cached, None
        if isinstance(cached, dict):
            # Old probe format: {"plans": [...], "error": str, "insecure": bool}
            if isinstance(cached.get("plans"), list):
                return cached["plans"], None
            # API raw response format: {"data": {"plans": [...]}}
            data = cached.get("data")
            if isinstance(data, dict) and isinstance(data.get("plans"), list):
                return data["plans"], None
        # Malformed — refetch

    all_plans: list[dict] = []
    page = 1
    while True:
        url = (f"{base.rstrip('/')}/cds-au/v1/energy/plans"
               f"?fuelType=ELECTRICITY&type=ALL&effective=CURRENT"
               f"&page-size={PAGE_SIZE}&page={page}")
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
        if page > 20:  # safety
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


# ------------------------------------------------------------------
# Filter
# ------------------------------------------------------------------

def is_residential_electricity(plan: dict) -> bool:
    if plan.get("fuelType") != "ELECTRICITY":
        return False
    if plan.get("customerType") != "RESIDENTIAL":
        return False
    if plan.get("type") not in ("MARKET", "STANDING"):
        return False
    return True


# ------------------------------------------------------------------
# Per-retailer worker
# ------------------------------------------------------------------

def process_retailer(brand: str, base: str) -> dict:
    """Returns stats dict."""
    slug = slugify(brand)
    stats = {
        "brand": brand,
        "slug": slug,
        "base": base,
        "plans_listed": 0,
        "plans_filtered": 0,
        "plans_fetched": 0,
        "plans_cached": 0,
        "plans_failed": 0,
        "list_error": None,
    }
    plans, list_err = fetch_plan_list(base, slug)
    if list_err and not plans:
        stats["list_error"] = list_err
        return stats
    stats["plans_listed"] = len(plans)
    filtered = [p for p in plans if is_residential_electricity(p)]
    stats["plans_filtered"] = len(filtered)

    fetched = 0
    failed = 0
    cached = 0
    for i, p in enumerate(filtered, 1):
        pid = p.get("planId")
        if not pid:
            continue
        safe_id = pid.replace("/", "_")
        cache_file = os.path.join(cache_dir(slug), f"{safe_id}.json")
        was_cached = os.path.exists(cache_file)
        d, err = fetch_plan_detail(base, pid, slug)
        if d is None:
            failed += 1
            append_failed({"slug": slug, "planId": pid, "error": err})
            continue
        fetched += 1
        if was_cached:
            cached += 1
        # Progress checkpoint every 100
        if i % 100 == 0:
            checkpoint(slug, brand, i, len(filtered))
    stats["plans_fetched"] = fetched
    stats["plans_cached"] = cached
    stats["plans_failed"] = failed
    checkpoint(slug, brand, len(filtered), len(filtered))
    return stats


def checkpoint(slug: str, brand: str, done: int, total: int) -> None:
    with progress_lock:
        prog = load_json(PROGRESS_PATH) or {}
        prog[slug] = {
            "brand": brand,
            "done": done,
            "total": total,
            "ts": time.time(),
        }
        save_json(PROGRESS_PATH, prog)


# ------------------------------------------------------------------
# Signature extraction
# ------------------------------------------------------------------

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


def sorted_keys(d: dict | None) -> str:
    if not isinstance(d, dict):
        return ""
    return ",".join(sorted(d.keys()))


def extract_signature(detail: dict) -> tuple[str, list[str], dict]:
    """Returns (sig_id, tokens, snapshot)."""
    tokens: list[str] = []
    snapshot: dict = {}

    data = detail.get("data") if isinstance(detail, dict) else None
    if not isinstance(data, dict):
        tokens.append("data:MISSING")
        return _hash(tokens), tokens, snapshot

    ec = data.get("electricityContract")
    if not isinstance(ec, dict):
        tokens.append("electricityContract:MISSING")
        return _hash(tokens), tokens, snapshot

    pm = ec.get("pricingModel", "MISSING")
    tokens.append(f"pricingModel:{pm}")
    snapshot["pricingModel"] = pm

    for key in ("dailySupplyCharges", "dailySupplyCharge", "tariffPeriod",
                "solarFeedInTariff", "incentives", "controlledLoad",
                "greenPowerCharges", "discounts", "fees"):
        v = ec.get(key, "__MISSING__")
        if v == "__MISSING__":
            tokens.append(f"ec.{key}:MISSING")
        else:
            tokens.append(f"ec.{key}:{jtype(v)}")
            snapshot[f"ec.{key}"] = jtype(v)

    # tariffPeriod[0]
    tp = ec.get("tariffPeriod") or []
    if isinstance(tp, list) and tp and isinstance(tp[0], dict):
        tp0 = tp[0]
        rbut = tp0.get("rateBlockUType", "MISSING")
        tokens.append(f"tp0.rateBlockUType:{rbut}")
        snapshot["tp0.rateBlockUType"] = rbut
        if rbut and rbut != "MISSING":
            blk = tp0.get(rbut)
            tokens.append(f"tp0.{rbut}:{jtype(blk)}")
            snapshot[f"tp0.{rbut}"] = jtype(blk)
            # Inner rate block keys + rates[0] keys
            inner_dict = blk if isinstance(blk, dict) else (
                blk[0] if isinstance(blk, list) and blk and isinstance(blk[0], dict) else None
            )
            if isinstance(inner_dict, dict):
                tokens.append(f"tp0.{rbut}.keys:{sorted_keys(inner_dict)}")
                rates = inner_dict.get("rates") or []
                if isinstance(rates, list) and rates and isinstance(rates[0], dict):
                    tokens.append(f"tp0.rates[0].keys:{sorted_keys(rates[0])}")
                    for f in ("unitPrice", "volume", "measureUnit", "period"):
                        v = rates[0].get(f, "__MISSING__")
                        tokens.append(f"tp0.rates[0].{f}:{ 'MISSING' if v == '__MISSING__' else jtype(v)}")
        for k in ("dailySupplyCharge", "dailySupplyCharges", "dailySupplyChargeType"):
            v = tp0.get(k, "__MISSING__")
            tokens.append(f"tp0.{k}:{ 'MISSING' if v == '__MISSING__' else (v if k.endswith('Type') else jtype(v))}")
    else:
        tokens.append("tp0:MISSING")

    # solarFeedInTariff[0]
    fit = ec.get("solarFeedInTariff") or []
    if isinstance(fit, list) and fit and isinstance(fit[0], dict):
        fit0 = fit[0]
        fut = fit0.get("tariffUType", "MISSING")
        tokens.append(f"fit0.tariffUType:{fut}")
        if fut and fut != "MISSING":
            blk = fit0.get(fut)
            tokens.append(f"fit0.{fut}:{jtype(blk)}")
            inner_dict = blk if isinstance(blk, dict) else (
                blk[0] if isinstance(blk, list) and blk and isinstance(blk[0], dict) else None
            )
            if isinstance(inner_dict, dict):
                tokens.append(f"fit0.{fut}.keys:{sorted_keys(inner_dict)}")
                rates = inner_dict.get("rates") or []
                if isinstance(rates, list) and rates and isinstance(rates[0], dict):
                    tokens.append(f"fit0.rates[0].keys:{sorted_keys(rates[0])}")
        scheme = fit0.get("scheme", "MISSING")
        tokens.append(f"fit0.scheme:{scheme}")
        payer = fit0.get("payerType", "MISSING")
        tokens.append(f"fit0.payerType:{payer}")
    else:
        tokens.append("fit0:MISSING")

    # incentives[0]
    incs = ec.get("incentives") or []
    if isinstance(incs, list) and incs and isinstance(incs[0], dict):
        tokens.append(f"inc0.keys:{sorted_keys(incs[0])}")
        cat = incs[0].get("category", "MISSING")
        tokens.append(f"inc0.category:{cat}")
    else:
        tokens.append("inc0:MISSING")

    return _hash(tokens), tokens, snapshot


def _hash(tokens: list[str]) -> str:
    h = hashlib.sha1("|".join(tokens).encode()).hexdigest()
    return f"SIG_{h[:12]}"


# ------------------------------------------------------------------
# Render
# ------------------------------------------------------------------

def render_catalog(stats_list, sig_to_plans, sig_to_tokens,
                   sig_to_snapshot, surprises) -> None:
    lines: list[str] = []
    lines.append("# CDR PlanDetailV2 Shape Catalog (Full Sweep)\n")
    lines.append(f"_Generated by `/tmp/cdr_full_sweep.py`. "
                 f"Source registry: jxeeno/energy-cdr-prd-endpoints._\n")

    # Section 1: Sweep summary
    n_retailers = len(stats_list)
    n_reachable = sum(1 for s in stats_list if not s["list_error"])
    n_listed = sum(s["plans_listed"] for s in stats_list)
    n_filtered = sum(s["plans_filtered"] for s in stats_list)
    n_fetched = sum(s["plans_fetched"] for s in stats_list)
    n_failed = sum(s["plans_failed"] for s in stats_list)
    duration = time.time() - WALL_CLOCK_START
    cache_size = sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _, fs in os.walk(CACHE_DIR) for f in fs
    )
    lines.append("## 1. Sweep summary\n")
    lines.append(f"- Retailers in registry: **{n_retailers}**")
    lines.append(f"- Retailers reachable: **{n_reachable}**")
    lines.append(f"- Retailers list-failed: **{n_retailers - n_reachable}**")
    lines.append(f"- Plans listed (residential ∪ business): **{n_listed:,}**")
    lines.append(f"- Plans filtered (residential ELECTRICITY MARKET/STANDING): **{n_filtered:,}**")
    lines.append(f"- Plan details fetched OK: **{n_fetched:,}**")
    lines.append(f"- Plan detail failures: **{n_failed:,}**")
    lines.append(f"- Distinct shape signatures: **{len(sig_to_plans):,}**")
    lines.append(f"- Wall-clock: **{duration:.1f}s** ({duration / 60:.1f}m)")
    lines.append(f"- Cache size: **{cache_size / 1024 / 1024:.1f} MB**\n")

    # Section 2: Per-retailer coverage
    lines.append("## 2. Per-retailer coverage\n")
    lines.append("| Retailer | Listed | Filtered (Res. Elec.) | Fetched | Failed | Distinct sigs | List error |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    retailer_sigs: dict[str, set[str]] = defaultdict(set)
    for sig, plans in sig_to_plans.items():
        for slug, _pid in plans:
            retailer_sigs[slug].add(sig)
    for s in sorted(stats_list, key=lambda x: x["brand"].lower()):
        sigs = len(retailer_sigs.get(s["slug"], set()))
        err = (s["list_error"] or "")[:60]
        lines.append(
            f"| {s['brand']} | {s['plans_listed']:,} | {s['plans_filtered']:,} | "
            f"{s['plans_fetched']:,} | {s['plans_failed']:,} | {sigs} | {err} |"
        )
    lines.append("")

    # Section 3: Signature catalog
    lines.append("## 3. Signature catalog\n")
    lines.append(f"Total signatures: **{len(sig_to_plans)}**, ranked by plan count.\n")
    sig_ranked = sorted(sig_to_plans.items(), key=lambda kv: -len(kv[1]))
    for sig, plans in sig_ranked:
        retailer_count = Counter(slug for slug, _ in plans)
        lines.append(f"### {sig} — {len(plans):,} plans across {len(retailer_count)} retailers\n")
        # Sample 3 from different retailers if possible
        seen_retailers: set[str] = set()
        samples: list[tuple[str, str]] = []
        for slug, pid in plans:
            if slug not in seen_retailers:
                samples.append((slug, pid))
                seen_retailers.add(slug)
            if len(samples) >= 3:
                break
        if len(samples) < 3:
            for slug, pid in plans:
                if (slug, pid) not in samples:
                    samples.append((slug, pid))
                if len(samples) >= 3:
                    break
        lines.append("**Sample planIds:**")
        for slug, pid in samples:
            lines.append(f"- `{slug}/{pid}`")
        lines.append("")
        lines.append("**Tokens:**")
        for tok in sig_to_tokens[sig]:
            lines.append(f"- `{tok}`")
        lines.append("")
        lines.append("**Per-retailer count (top 10):**")
        for slug, cnt in retailer_count.most_common(10):
            lines.append(f"- {slug}: {cnt:,}")
        if len(retailer_count) > 10:
            lines.append(f"- _(and {len(retailer_count) - 10} more)_")
        lines.append("")

    # Section 4: Field-presence heatmap
    lines.append("## 4. Field-presence heatmap\n")
    # For each path, count present-vs-missing per retailer (using snapshots)
    path_to_retailer_present: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    path_to_retailer_total: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    interesting_paths = [
        "ec.dailySupplyCharges", "ec.dailySupplyCharge", "ec.tariffPeriod",
        "ec.solarFeedInTariff", "ec.incentives", "ec.controlledLoad",
        "ec.greenPowerCharges", "ec.discounts", "ec.fees", "ec.eligibility",
    ]
    for sig, plans in sig_to_plans.items():
        snap = sig_to_snapshot[sig]
        for slug, _ in plans:
            for path in interesting_paths:
                path_to_retailer_total[path][slug] += 1
                if path in snap and snap[path] != "MISSING":
                    path_to_retailer_present[path][slug] += 1
    sampled_retailers = sorted(set(
        slug for plans in sig_to_plans.values() for slug, _ in plans
    ))
    if sampled_retailers:
        # Show top 12 retailers by plan volume
        slug_volume = Counter(slug for plans in sig_to_plans.values() for slug, _ in plans)
        top_retailers = [s for s, _ in slug_volume.most_common(12)]
        lines.append("Top 12 retailers by plan volume. Cell = plans-with-field / total-plans.\n")
        header = "| Path | " + " | ".join(top_retailers) + " |"
        sep = "|---|" + "|".join(["---:"] * len(top_retailers)) + "|"
        lines.append(header)
        lines.append(sep)
        for path in interesting_paths:
            row = [path]
            for slug in top_retailers:
                p = path_to_retailer_present[path].get(slug, 0)
                t = path_to_retailer_total[path].get(slug, 0)
                row.append(f"{p}/{t}" if t else "—")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    # Section 5: Daily-supply-charge location ranking
    lines.append("## 5. Daily supply charge — location ranking\n")
    location_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    location_paths = [
        ("ec.dailySupplyCharges", "electricityContract.dailySupplyCharges (plural)"),
        ("ec.dailySupplyCharge", "electricityContract.dailySupplyCharge (singular)"),
        ("tp0.dailySupplyCharges", "tariffPeriod[0].dailySupplyCharges (plural)"),
        ("tp0.dailySupplyCharge", "tariffPeriod[0].dailySupplyCharge (singular)"),
    ]
    # Walk ALL cached details to count, not just signatures
    for sig, plans in sig_to_plans.items():
        toks = set(sig_to_tokens[sig])
        for slug, _ in plans:
            for path, _label in location_paths:
                # check tokens for path:non-MISSING
                for t in toks:
                    if t.startswith(path + ":") and not t.endswith(":MISSING"):
                        location_counts[path][slug] += 1
                        break
    lines.append("| Location | Type observed | Total plans | # retailers |")
    lines.append("|---|---|---:|---:|")
    for path, label in location_paths:
        total = sum(location_counts[path].values())
        ret_count = len([s for s, c in location_counts[path].items() if c > 0])
        # Find type distribution from tokens
        types = Counter()
        for sig, plans in sig_to_plans.items():
            for t in sig_to_tokens[sig]:
                if t.startswith(path + ":") and not t.endswith(":MISSING"):
                    types[t.split(":", 1)[1]] += len(plans)
        type_str = ", ".join(f"{ty}({cnt})" for ty, cnt in types.most_common(3)) or "—"
        lines.append(f"| `{label}` | {type_str} | {total:,} | {ret_count} |")
    lines.append("")
    # Plans with NO daily supply charge at all
    no_dsc = 0
    for sig, plans in sig_to_plans.items():
        toks = sig_to_tokens[sig]
        has = any(
            t.startswith(p + ":") and not t.endswith(":MISSING")
            for p, _ in location_paths
            for t in toks
        )
        if not has:
            no_dsc += len(plans)
    lines.append(f"**Plans with NO daily supply charge in any of the 4 locations: {no_dsc:,}**\n")

    # Section 6: rateBlockUType variants
    lines.append("## 6. `tariffPeriod[0].rateBlockUType` variants\n")
    rbut_to_sigs: dict[str, list[str]] = defaultdict(list)
    rbut_block_type: dict[str, Counter] = defaultdict(Counter)
    for sig, tokens in sig_to_tokens.items():
        rbut_val = None
        block_type = None
        for t in tokens:
            if t.startswith("tp0.rateBlockUType:"):
                rbut_val = t.split(":", 1)[1]
            elif rbut_val and t.startswith(f"tp0.{rbut_val}:"):
                block_type = t.split(":", 1)[1]
        if rbut_val:
            rbut_to_sigs[rbut_val].append(sig)
            if block_type:
                rbut_block_type[rbut_val][block_type] += len(sig_to_plans[sig])
    for rbut in sorted(rbut_to_sigs.keys()):
        sigs = rbut_to_sigs[rbut]
        plan_count = sum(len(sig_to_plans[s]) for s in sigs)
        retailer_count = len({slug for s in sigs for slug, _ in sig_to_plans[s]})
        lines.append(f"### `rateBlockUType` = `{rbut}`\n")
        lines.append(f"- Plans: **{plan_count:,}** | Retailers: **{retailer_count}** | Distinct sigs: **{len(sigs)}**")
        bt = rbut_block_type.get(rbut)
        if bt:
            lines.append(f"- Nested block types: " + ", ".join(f"`{ty}` ({cnt:,})" for ty, cnt in bt.most_common()))
        lines.append(f"- Signatures: {', '.join(sigs[:8])}{', …' if len(sigs) > 8 else ''}\n")

    # Section 7: solarFeedInTariff variants
    lines.append("## 7. `solarFeedInTariff[0].tariffUType` variants\n")
    fut_to_sigs: dict[str, list[str]] = defaultdict(list)
    fut_block_type: dict[str, Counter] = defaultdict(Counter)
    fut_scheme: dict[str, Counter] = defaultdict(Counter)
    for sig, tokens in sig_to_tokens.items():
        fut_val = None
        block_type = None
        scheme = None
        for t in tokens:
            if t.startswith("fit0.tariffUType:"):
                fut_val = t.split(":", 1)[1]
            elif fut_val and t.startswith(f"fit0.{fut_val}:"):
                block_type = t.split(":", 1)[1]
            elif t.startswith("fit0.scheme:"):
                scheme = t.split(":", 1)[1]
        key = fut_val or "MISSING"
        fut_to_sigs[key].append(sig)
        if block_type:
            fut_block_type[key][block_type] += len(sig_to_plans[sig])
        if scheme:
            fut_scheme[key][scheme] += len(sig_to_plans[sig])
    for fut in sorted(fut_to_sigs.keys()):
        sigs = fut_to_sigs[fut]
        plan_count = sum(len(sig_to_plans[s]) for s in sigs)
        retailer_count = len({slug for s in sigs for slug, _ in sig_to_plans[s]})
        lines.append(f"### `tariffUType` = `{fut}`\n")
        lines.append(f"- Plans: **{plan_count:,}** | Retailers: **{retailer_count}** | Sigs: **{len(sigs)}**")
        bt = fut_block_type.get(fut)
        if bt:
            lines.append(f"- Nested types: " + ", ".join(f"`{ty}` ({cnt:,})" for ty, cnt in bt.most_common()))
        sc = fut_scheme.get(fut)
        if sc:
            lines.append(f"- Schemes: " + ", ".join(f"`{s}` ({cnt:,})" for s, cnt in sc.most_common()))
        lines.append("")

    # Section 8: Surprises
    lines.append("## 8. Surprise findings\n")
    if surprises:
        for s in surprises:
            lines.append(f"- {s}")
    else:
        lines.append("- (none recorded)")
    lines.append("")

    # Section 9: Recommended parser shape
    lines.append("## 9. Recommended defensive parser\n")
    lines.append(_parser_doc(sig_to_plans, sig_to_tokens))

    with open(CATALOG_PATH, "w") as f:
        f.write("\n".join(lines))


def _parser_doc(sig_to_plans, sig_to_tokens) -> str:
    pricing_models = Counter()
    rateblocks = Counter()
    futs = Counter()
    schemes = Counter()
    for sig, tokens in sig_to_tokens.items():
        n = len(sig_to_plans[sig])
        for t in tokens:
            if t.startswith("pricingModel:"):
                pricing_models[t.split(":", 1)[1]] += n
            elif t.startswith("tp0.rateBlockUType:"):
                rateblocks[t.split(":", 1)[1]] += n
            elif t.startswith("fit0.tariffUType:"):
                futs[t.split(":", 1)[1]] += n
            elif t.startswith("fit0.scheme:"):
                schemes[t.split(":", 1)[1]] += n
    pm_str = ", ".join(f"{k}({v:,})" for k, v in pricing_models.most_common())
    rb_str = ", ".join(f"{k}({v:,})" for k, v in rateblocks.most_common())
    fut_str = ", ".join(f"{k}({v:,})" for k, v in futs.most_common())
    sc_str = ", ".join(f"{k}({v:,})" for k, v in schemes.most_common())
    return f"""
```python
def _summarise_cdr_plan(detail: dict) -> dict[str, str]:
    \"\"\"Summarise a CDR PlanDetailV2 envelope into UI-ready strings.

    Defensive against the union of shapes observed across the AU CDR registry.
    Coverage stats from this sweep:
      pricingModel:       {pm_str}
      rateBlockUType:     {rb_str}
      tariffUType:        {fut_str}
      fit0.scheme values: {sc_str}

    Implementation rules:
      1. detail["data"] may be missing → return {{"_unparseable": ...}}.
      2. detail["data"]["electricityContract"] may be missing → ditto.
      3. For each union discriminator, branch on the value AND type-check
         the payload — some retailers ship dict-where-list documented and
         vice versa.
      4. Daily supply charge: try in order
         ec.dailySupplyCharges → ec.dailySupplyCharge →
         tp[0].dailySupplyCharges → tp[0].dailySupplyCharge → 'n/a'.
      5. Coerce all numerics through Decimal(str(v)) (str-vs-num drift
         observed on rates[0].volume on a minority of retailers).
      6. Iterate ALL solarFeedInTariff[] entries — Sumo + Red ship up to
         9-tier FIT bands.
      7. controlledLoad[] uses its own rateBlockUType — recurse into the
         tariffPeriod parser.
      8. Wrap each sub-section in try/except and append parser warnings;
         partial display beats blank card.
      9. Always emit a 'pricing_model' key (default 'UNKNOWN') so the HA
         card layer can pick a renderer without KeyError.

    Returns:
      {{ supply_c_per_day, usage_summary, fit_summary,
         incentives_summary, controlled_load_summary,
         pricing_model, warnings }}
    \"\"\"
    ...
```
"""


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    print("Fetching registry...", file=sys.stderr, flush=True)
    registry = fetch_registry()
    print(f"  {len(registry)} retailers", file=sys.stderr, flush=True)

    # Phase 1: parallel fetch
    print(f"\n=== Phase 1: parallel fetch ({PARALLEL_RETAILERS} workers) ===", file=sys.stderr, flush=True)
    stats_list: list[dict] = []
    with ThreadPoolExecutor(max_workers=PARALLEL_RETAILERS) as ex:
        futures = {}
        for entry in registry:
            brand = entry.get("brandName") or "Unknown"
            base = entry.get("productReferenceDataBaseUri") or entry.get("publicBaseUri")
            if not base:
                stats_list.append({
                    "brand": brand, "slug": slugify(brand), "base": "(none)",
                    "plans_listed": 0, "plans_filtered": 0, "plans_fetched": 0,
                    "plans_cached": 0, "plans_failed": 0, "list_error": "no base URI",
                })
                continue
            futures[ex.submit(process_retailer, brand, base)] = brand
        done_count = 0
        for fut in as_completed(futures):
            brand = futures[fut]
            try:
                stats = fut.result()
                stats_list.append(stats)
            except Exception as e:  # noqa: BLE001
                stats_list.append({
                    "brand": brand, "slug": slugify(brand), "base": "?",
                    "plans_listed": 0, "plans_filtered": 0, "plans_fetched": 0,
                    "plans_cached": 0, "plans_failed": 0,
                    "list_error": f"crash:{type(e).__name__}:{e}",
                })
            done_count += 1
            if done_count % 5 == 0 or done_count == len(futures):
                print(f"  [{done_count}/{len(futures)}] retailers done", file=sys.stderr, flush=True)

    # Phase 2: signature extraction
    print("\n=== Phase 2: signature extraction ===", file=sys.stderr, flush=True)
    sig_to_plans: dict[str, list[tuple[str, str]]] = defaultdict(list)
    sig_to_tokens: dict[str, list[str]] = {}
    sig_to_snapshot: dict[str, dict] = {}
    surprises: list[str] = []
    surprise_seen: set[str] = set()

    n_processed = 0
    for stats in stats_list:
        slug = stats["slug"]
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
                sig, tokens, snap = extract_signature(detail)
            except Exception as e:  # noqa: BLE001
                surprises.append(f"signature crash {slug}/{pid}: {type(e).__name__}:{e}")
                continue
            sig_to_plans[sig].append((slug, pid))
            if sig not in sig_to_tokens:
                sig_to_tokens[sig] = tokens
                sig_to_snapshot[sig] = snap
            # Surprise detection
            data = detail.get("data") if isinstance(detail, dict) else None
            if not isinstance(data, dict) and "data:MISSING" not in surprise_seen:
                surprises.append(f"`data` not a dict — first seen on {slug}/{pid}")
                surprise_seen.add("data:MISSING")
            elif isinstance(data, dict) and "electricityContract" not in data and "ec:MISSING" not in surprise_seen:
                surprises.append(f"`electricityContract` missing — first seen on {slug}/{pid}")
                surprise_seen.add("ec:MISSING")
            ec = (data or {}).get("electricityContract") if isinstance(data, dict) else None
            if isinstance(ec, dict):
                tp = ec.get("tariffPeriod")
                if tp == [] and "empty_tp" not in surprise_seen:
                    surprises.append(f"`tariffPeriod` is `[]` — first seen on {slug}/{pid}")
                    surprise_seen.add("empty_tp")
                # Numeric where spec says string
                if isinstance(tp, list) and tp and isinstance(tp[0], dict):
                    dsc = tp[0].get("dailySupplyCharge")
                    if isinstance(dsc, (int, float)) and "dsc_num" not in surprise_seen:
                        surprises.append(f"`tariffPeriod[0].dailySupplyCharge` is numeric (spec says string) — first seen on {slug}/{pid}")
                        surprise_seen.add("dsc_num")
            n_processed += 1
            if n_processed % 1000 == 0:
                print(f"  processed {n_processed} details, {len(sig_to_plans)} sigs so far", file=sys.stderr, flush=True)

    # Detect retailers that 404 detail despite listing — count from failed.jsonl
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
            surprises.append(f"`{slug}` returned 404 on {cnt} plan detail(s) despite listing them")

    # Phase 3: render
    print("\n=== Phase 3: render catalog ===", file=sys.stderr, flush=True)
    render_catalog(stats_list, sig_to_plans, sig_to_tokens, sig_to_snapshot, surprises)
    print(f"Wrote {CATALOG_PATH}", file=sys.stderr, flush=True)

    # 5-line summary
    n_reachable = sum(1 for s in stats_list if not s["list_error"])
    n_fetched = sum(s["plans_fetched"] for s in stats_list)
    n_failed = sum(s["plans_failed"] for s in stats_list)
    duration = time.time() - WALL_CLOCK_START
    print("---SUMMARY---")
    print(f"Retailers reachable: {n_reachable}/{len(stats_list)}")
    print(f"Plan details fetched: {n_fetched:,} (failed: {n_failed:,})")
    print(f"Distinct shape signatures: {len(sig_to_plans):,}")
    print(f"Wall-clock: {duration:.1f}s")
    print(f"Catalog: {CATALOG_PATH}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted — progress checkpointed, resume by re-running.", file=sys.stderr)
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
