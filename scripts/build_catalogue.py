#!/usr/bin/env python3
"""Build a compact national CDR plan catalogue from the swept cache.

Pure transform: reads the plan-detail JSONs that ``cdr_full_sweep_v2.py``
caches to ``/tmp/cdr-cache/{slug}/{planId}.json`` (each file is the full CDR
``GET /energy/plans/{planId}`` response, i.e. a ``{"data": {...}}`` envelope),
keeps only the plans that are still in each retailer's CURRENT plan list (the
``_planlist*.json`` files the sweep writes), narrows to residential-electricity
plans with a non-empty ``electricityContract``, and trims each to the minimal
entry PriceHawk needs to filter and cost a plan.

Filtering by the current plan list matters on a cache-warm run: the detail cache
can still hold plans a retailer has since delisted, and those must not leak into
the published catalogue.

The ``electricityContract`` block is copied through unchanged — PriceHawk's
verified mapper (``map_plan_detail``) consumes each catalogue entry directly as
its bare ``data`` object, reading ``planId``/``displayName``/``brandName`` off
the top level and the full ``electricityContract`` (with its nested
``tariffPeriod``/``controlledLoad``/``solarFeedInTariff``/``discounts``).

Outputs (to ``dist/`` by default):
  - ``catalogue.json.gz`` — gzip of
    ``{"schema_version", "generated_at", "plans": [...]}``.
  - ``manifest.json`` — ``{"schema_version", "generated_at", "plan_count",
    "retailer_count", "bytes_gzipped"}``.

Stdlib only. No network. The national sweep is the Action's job; this script is
a deterministic transform over whatever the sweep already cached.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

# Reuse the sweep's verified helpers rather than re-deriving the cache layout
# or the residential-electricity predicate (single source of truth).
from cdr_full_sweep_v2 import is_residential_electricity, load_json

SCHEMA_VERSION = 1
DEFAULT_CACHE_DIR = "/tmp/cdr-cache"
DEFAULT_OUT_DIR = "dist"

# Top-level (bare ``data``-object) keys retained on every catalogue entry.
# Everything else (additionalInformation, *Uri fields, free-text terms,
# meteringCharges, gas blocks, lastUpdated, effectiveFrom/To, etc.) is dropped.
_GEOGRAPHY_KEYS = ("distributors", "includedPostcodes", "excludedPostcodes")


def trim_plan(data: dict[str, Any]) -> dict[str, Any]:
    """Trim a plan-detail ``data`` object to a compact catalogue entry.

    ``data`` is the bare plan object (the contents of the response's ``data``
    envelope). The caller guarantees it is residential-electricity and carries
    a non-empty ``electricityContract``.
    """
    geo_src = data.get("geography") or {}
    geography = {k: geo_src.get(k) for k in _GEOGRAPHY_KEYS if geo_src.get(k) is not None}
    return {
        "planId": data.get("planId"),
        "brand": data.get("brand"),
        "brandName": data.get("brandName"),
        "displayName": data.get("displayName"),
        "geography": geography,
        # Copied through verbatim — PriceHawk's mapper consumes this shape.
        "electricityContract": data.get("electricityContract"),
    }


_PLANLIST_PREFIX = "_planlist"


def _current_plan_ids(slug_dir: str) -> "set[str]":
    """Plan IDs in the slug's CURRENT plan list(s) (the sweep's source of truth).

    The sweep writes the live ``GET /energy/plans`` result per retailer to
    ``_planlist.json`` (unique base URI) or ``_planlist_{cdrCode}.json`` (one per
    brand on a shared base URI). Each is the list of plan summaries the retailer
    currently advertises. A slug may have several brand-scoped lists; we union
    their plan IDs.

    Returns the set of current ``planId`` strings. An empty set means no plan
    list was found for this slug, in which case the caller must drop the slug
    entirely rather than fall back to the (possibly stale) detail cache.
    """
    plan_ids: set[str] = set()
    for fname in os.listdir(slug_dir):
        if not (fname.startswith(_PLANLIST_PREFIX) and fname.endswith(".json")):
            continue
        listing = load_json(os.path.join(slug_dir, fname))
        if not isinstance(listing, list):
            continue
        for plan in listing:
            if isinstance(plan, dict):
                pid = plan.get("planId")
                if isinstance(pid, str):
                    plan_ids.add(pid)
    return plan_ids


def _iter_cached_details(cache_dir: str) -> "list[dict[str, Any]]":
    """Yield cached plan-detail ``data`` objects for CURRENTLY-listed plans.

    Mirrors the sweep's cache layout: ``{cache_dir}/{slug}/{planId}.json``, each
    file a ``{"data": {...}}`` envelope, plus per-retailer ``_planlist*.json``
    files holding the live plan lists. Skips sweep bookkeeping files and dirs
    (prefixed with ``_``).

    On a cache-warm run the detail cache can retain plans that the retailer has
    since removed from its current list; those stale files must not leak into the
    catalogue. We therefore include a detail only when its ``planId`` is present
    in the slug's current plan list. A slug with no readable plan list contributes
    nothing (its detail cache cannot be trusted to be current).
    """
    details: list[dict[str, Any]] = []
    if not os.path.isdir(cache_dir):
        raise FileNotFoundError(f"cache dir not found: {cache_dir}")
    for slug in sorted(os.listdir(cache_dir)):
        if slug.startswith("_"):
            continue
        slug_dir = os.path.join(cache_dir, slug)
        if not os.path.isdir(slug_dir):
            continue
        current_ids = _current_plan_ids(slug_dir)
        if not current_ids:
            continue
        for fname in sorted(os.listdir(slug_dir)):
            if fname.startswith("_") or not fname.endswith(".json"):
                continue
            envelope = load_json(os.path.join(slug_dir, fname))
            if not isinstance(envelope, dict):
                continue
            data = envelope.get("data")
            if not isinstance(data, dict):
                continue
            if data.get("planId") not in current_ids:
                continue
            details.append(data)
    return details


def build_plans(cache_dir: str) -> "list[dict[str, Any]]":
    """Read the cache and return trimmed entries for qualifying plans.

    Qualifying = ``is_residential_electricity`` AND a non-empty
    ``electricityContract``. Deduplicates on ``planId`` (a plan can appear under
    multiple retailer slugs when base URIs are shared; first occurrence wins).
    """
    plans: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for data in _iter_cached_details(cache_dir):
        if not is_residential_electricity(data):
            continue
        contract = data.get("electricityContract")
        if not contract:
            continue
        plan_id = data.get("planId")
        if isinstance(plan_id, str) and plan_id in seen_ids:
            continue
        if isinstance(plan_id, str):
            seen_ids.add(plan_id)
        plans.append(trim_plan(data))
    return plans


def _retailer_count(plans: "list[dict[str, Any]]") -> int:
    """Distinct retailers, keyed by ``brand`` (falling back to ``brandName``)."""
    return len({p.get("brand") or p.get("brandName") for p in plans})


def build_catalogue(cache_dir: str, out_dir: str, generated_at: str) -> dict[str, Any]:
    """Build catalogue + manifest from ``cache_dir`` into ``out_dir``.

    Returns the manifest dict. Raises ``SystemExit`` (non-zero) if no plans
    qualify — an empty catalogue must never be published.
    """
    plans = build_plans(cache_dir)
    if not plans:
        raise SystemExit(
            f"refusing to write an empty catalogue: 0 qualifying plans in {cache_dir}"
        )

    catalogue = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "plans": plans,
    }
    payload = json.dumps(catalogue, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    gzipped = gzip.compress(payload, mtime=0)

    os.makedirs(out_dir, exist_ok=True)
    catalogue_path = os.path.join(out_dir, "catalogue.json.gz")
    manifest_path = os.path.join(out_dir, "manifest.json")

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "plan_count": len(plans),
        "retailer_count": _retailer_count(plans),
        "bytes_gzipped": len(gzipped),
    }

    _atomic_write_bytes(catalogue_path, gzipped)
    _atomic_write_bytes(
        manifest_path,
        json.dumps(manifest, indent=2).encode("utf-8") + b"\n",
    )
    return manifest


def _atomic_write_bytes(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _parse_args(argv: "list[str] | None") -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        default=DEFAULT_CACHE_DIR,
        help=f"swept plan-detail cache (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help=f"output directory for catalogue + manifest (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help=(
            "ISO8601 UTC timestamp stamped into both outputs. "
            "Defaults to current UTC time; pass an explicit value for "
            "deterministic builds (e.g. in tests)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: "list[str] | None" = None) -> int:
    args = _parse_args(argv)
    generated_at = args.generated_at or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    manifest = build_catalogue(args.cache_dir, args.out_dir, generated_at)
    print(
        f"catalogue: {manifest['plan_count']} plans across "
        f"{manifest['retailer_count']} retailers, "
        f"{manifest['bytes_gzipped']} bytes gzipped -> {args.out_dir}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
