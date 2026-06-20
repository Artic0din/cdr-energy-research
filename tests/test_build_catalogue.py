"""Tests for ``scripts/build_catalogue.py``.

The fixture cache mirrors the layout ``cdr_full_sweep_v2`` writes:
``{cache_dir}/{slug}/{planId}.json`` (plan details, each a full CDR
``{"data": {...}, "meta": {...}, "links": {...}}`` envelope) plus
``{cache_dir}/{slug}/_planlist*.json`` (the live plan list the sweep cached for
that retailer). The builder keeps only plans still present in the current list,
then narrows to residential-electricity plans.

The residential-electricity plan-detail envelopes are vendored into
``tests/fixtures/cdr`` (self-contained, no external paths) — small copies of
PriceHawk's real CDR fixtures. The GAS and BUSINESS envelopes are derived from a
real one with the discriminating field flipped, so both must be excluded.
"""
from __future__ import annotations

import copy
import gzip
import json
import os

import pytest

import build_catalogue

FIXTURE_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "cdr")
RESIDENTIAL_FIXTURES = (
    "globird_single_rate.json",
    "globird_tou_controlled_load.json",
    "covau_tou.json",
)
GENERATED_AT = "2026-06-19T00:00:00Z"

# Keys a trimmed catalogue entry must retain (the bare ``data``-object shape
# PriceHawk's mapper consumes).
EXPECTED_ENTRY_KEYS = {
    "planId",
    "brand",
    "brandName",
    "displayName",
    "geography",
    "electricityContract",
}
# Bloat keys present on the raw plan-detail ``data`` object that MUST be dropped.
DROPPED_DATA_KEYS = (
    "additionalInformation",
    "lastUpdated",
    "effectiveFrom",
    "effectiveTo",
    "customerType",
    "fuelType",
    "type",
    "meteringCharges",
    "gasContract",
)


def _envelope(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_cached_plan(cache_dir: str, slug: str, envelope: dict) -> None:
    """Write an envelope to ``{cache_dir}/{slug}/{planId}.json`` (sweep layout)."""
    slug_dir = os.path.join(cache_dir, slug)
    os.makedirs(slug_dir, exist_ok=True)
    plan_id = envelope["data"]["planId"]
    safe_id = plan_id.replace("/", "_")
    with open(os.path.join(slug_dir, f"{safe_id}.json"), "w", encoding="utf-8") as f:
        json.dump(envelope, f)


def _append_to_planlist(cache_dir: str, slug: str, plan_id: str) -> None:
    """Add ``plan_id`` to the slug's ``_planlist.json`` (the live plan list).

    Mirrors what ``cdr_full_sweep_v2.fetch_plan_list`` caches: a JSON list of
    plan summaries, each with a ``planId``. The builder reads these to learn each
    retailer's CURRENT plans and ignore stale cached details.
    """
    slug_dir = os.path.join(cache_dir, slug)
    os.makedirs(slug_dir, exist_ok=True)
    path = os.path.join(slug_dir, "_planlist.json")
    existing = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
    existing.append({"planId": plan_id})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f)


def _write_cached_plan_listed(cache_dir: str, slug: str, envelope: dict) -> None:
    """Cache a plan detail AND record it in the slug's current plan list."""
    _write_cached_plan(cache_dir, slug, envelope)
    _append_to_planlist(cache_dir, slug, envelope["data"]["planId"])


@pytest.fixture()
def fixture_cache(tmp_path) -> str:
    """A swept-cache dir: 3 residential-electricity plans + 1 gas + 1 business.

    Only the 3 residential-electricity plans should survive into the catalogue.
    Every plan is recorded in its retailer's current plan list, so the current-
    list filter keeps them all.
    """
    cache_dir = os.path.join(str(tmp_path), "cdr-cache")

    base = None
    for name in RESIDENTIAL_FIXTURES:
        env = _envelope(os.path.join(FIXTURE_SRC, name))
        slug = env["data"]["brand"]
        _write_cached_plan_listed(cache_dir, slug, env)
        if base is None:
            base = env

    assert base is not None, "no residential fixtures loaded"

    # GAS plan — derived from a real envelope, fuelType flipped. Listed as
    # current, but excluded by is_residential_electricity (fuelType != ELECTRICITY).
    gas = copy.deepcopy(base)
    gas["data"]["planId"] = "GAS-EXCLUDE-001@EME"
    gas["data"]["fuelType"] = "GAS"
    _write_cached_plan_listed(cache_dir, "gasco", gas)

    # BUSINESS plan — customerType flipped. Listed as current, but excluded by
    # is_residential_electricity.
    biz = copy.deepcopy(base)
    biz["data"]["planId"] = "BIZ-EXCLUDE-001@EME"
    biz["data"]["customerType"] = "BUSINESS"
    _write_cached_plan_listed(cache_dir, "bizco", biz)

    # Sweep bookkeeping noise that must be ignored.
    os.makedirs(os.path.join(cache_dir, "_progress_v2.json.d"), exist_ok=True)
    with open(os.path.join(cache_dir, "_progress_v2.json"), "w", encoding="utf-8") as f:
        json.dump({"globird": {"done": 2, "total": 2}}, f)

    return cache_dir


def test_only_residential_electricity_kept(fixture_cache):
    plans = build_catalogue.build_plans(fixture_cache)
    assert len(plans) == len(RESIDENTIAL_FIXTURES)
    plan_ids = {p["planId"] for p in plans}
    assert "GAS-EXCLUDE-001@EME" not in plan_ids
    assert "BIZ-EXCLUDE-001@EME" not in plan_ids


def test_trimmed_entry_keeps_required_keys_and_drops_bloat(fixture_cache):
    plans = build_catalogue.build_plans(fixture_cache)
    for entry in plans:
        assert set(entry.keys()) == EXPECTED_ENTRY_KEYS
        assert entry["planId"]
        assert set(entry["geography"].keys()) <= set(build_catalogue._GEOGRAPHY_KEYS)
        for dropped in DROPPED_DATA_KEYS:
            assert dropped not in entry


def test_electricity_contract_copied_unchanged(fixture_cache):
    """The electricityContract block must reach the catalogue byte-for-byte."""
    src = _envelope(os.path.join(FIXTURE_SRC, "globird_single_rate.json"))
    expected_contract = src["data"]["electricityContract"]
    expected_id = src["data"]["planId"]

    plans = build_catalogue.build_plans(fixture_cache)
    entry = next(p for p in plans if p["planId"] == expected_id)
    assert entry["electricityContract"] == expected_contract


def test_catalogue_gzip_round_trips(fixture_cache, tmp_path):
    out_dir = os.path.join(str(tmp_path), "dist")
    manifest = build_catalogue.build_catalogue(fixture_cache, out_dir, GENERATED_AT)

    catalogue_path = os.path.join(out_dir, "catalogue.json.gz")
    with gzip.open(catalogue_path, "rt", encoding="utf-8") as f:
        catalogue = json.load(f)

    assert catalogue["schema_version"] == build_catalogue.SCHEMA_VERSION
    assert catalogue["generated_at"] == GENERATED_AT
    assert len(catalogue["plans"]) == len(RESIDENTIAL_FIXTURES)
    assert manifest["plan_count"] == len(catalogue["plans"])


def test_manifest_counts_correct(fixture_cache, tmp_path):
    out_dir = os.path.join(str(tmp_path), "dist")
    manifest = build_catalogue.build_catalogue(fixture_cache, out_dir, GENERATED_AT)

    with open(os.path.join(out_dir, "manifest.json"), encoding="utf-8") as f:
        on_disk = json.load(f)

    assert on_disk == manifest
    assert manifest["schema_version"] == build_catalogue.SCHEMA_VERSION
    assert manifest["generated_at"] == GENERATED_AT
    assert manifest["plan_count"] == len(RESIDENTIAL_FIXTURES)
    # globird (x2 plans) + covau = 2 distinct brands.
    assert manifest["retailer_count"] == 2

    gz_bytes = os.path.getsize(os.path.join(out_dir, "catalogue.json.gz"))
    assert manifest["bytes_gzipped"] == gz_bytes


def test_duplicate_plan_ids_deduplicated(fixture_cache):
    """A plan cached under two slugs (shared base URI) appears once."""
    src = _envelope(os.path.join(FIXTURE_SRC, "globird_single_rate.json"))
    # Same planId, different retailer slug — must not double-count. The reseller
    # slug lists the plan as current so it survives the current-list filter.
    _write_cached_plan_listed(fixture_cache, "globird-reseller", src)

    plans = build_catalogue.build_plans(fixture_cache)
    ids = [p["planId"] for p in plans]
    assert len(ids) == len(set(ids))
    assert len(plans) == len(RESIDENTIAL_FIXTURES)


def test_stale_cached_detail_excluded_when_not_in_current_list(fixture_cache):
    """A cached detail absent from the current plan list must not be published.

    Simulates a cache-warm run where a retailer delisted a plan: the detail file
    lingers in the cache but the refreshed plan list no longer references it.
    """
    src = _envelope(os.path.join(FIXTURE_SRC, "globird_single_rate.json"))
    stale = copy.deepcopy(src)
    stale_id = "GLO-DELISTED-999@EME"
    stale["data"]["planId"] = stale_id
    # Cache the detail WITHOUT adding it to the current plan list.
    _write_cached_plan(fixture_cache, "globird", stale)

    plans = build_catalogue.build_plans(fixture_cache)
    plan_ids = {p["planId"] for p in plans}
    assert stale_id not in plan_ids
    assert len(plans) == len(RESIDENTIAL_FIXTURES)


def test_slug_without_plan_list_contributes_nothing(fixture_cache):
    """A slug whose plan list is missing/unreadable must contribute no plans.

    Without a trustworthy current list the detail cache for that slug cannot be
    proven current, so the builder drops the whole slug rather than risk leaking
    stale entries.
    """
    orphan = _envelope(os.path.join(FIXTURE_SRC, "covau_tou.json"))
    orphan = copy.deepcopy(orphan)
    orphan["data"]["planId"] = "ORPHAN-NO-LIST-001@EME"
    # Detail cached, but no _planlist*.json written for this slug.
    _write_cached_plan(fixture_cache, "orphanco", orphan)

    plans = build_catalogue.build_plans(fixture_cache)
    plan_ids = {p["planId"] for p in plans}
    assert "ORPHAN-NO-LIST-001@EME" not in plan_ids
    assert len(plans) == len(RESIDENTIAL_FIXTURES)


def test_empty_cache_exits_nonzero_without_writing(tmp_path):
    """A 0-plan cache must raise (non-zero exit) and never write outputs."""
    empty_cache = os.path.join(str(tmp_path), "empty-cache")
    os.makedirs(empty_cache, exist_ok=True)
    out_dir = os.path.join(str(tmp_path), "dist")

    with pytest.raises(SystemExit):
        build_catalogue.build_catalogue(empty_cache, out_dir, GENERATED_AT)

    assert not os.path.exists(os.path.join(out_dir, "catalogue.json.gz"))
    assert not os.path.exists(os.path.join(out_dir, "manifest.json"))


def test_cache_with_only_excluded_plans_exits_nonzero(tmp_path):
    """A cache holding only gas/business plans yields 0 entries -> non-zero."""
    cache_dir = os.path.join(str(tmp_path), "excluded-only")
    base = _envelope(os.path.join(FIXTURE_SRC, "globird_single_rate.json"))

    gas = copy.deepcopy(base)
    gas["data"]["planId"] = "GAS-ONLY-001@EME"
    gas["data"]["fuelType"] = "GAS"
    _write_cached_plan(cache_dir, "gasco", gas)

    out_dir = os.path.join(str(tmp_path), "dist")
    with pytest.raises(SystemExit):
        build_catalogue.build_catalogue(cache_dir, out_dir, GENERATED_AT)
    assert not os.path.exists(os.path.join(out_dir, "manifest.json"))


def test_main_runs_against_fixture_cache(fixture_cache, tmp_path):
    out_dir = os.path.join(str(tmp_path), "dist")
    rc = build_catalogue.main(
        [
            "--cache-dir",
            fixture_cache,
            "--out-dir",
            out_dir,
            "--generated-at",
            GENERATED_AT,
        ]
    )
    assert rc == 0
    with gzip.open(os.path.join(out_dir, "catalogue.json.gz"), "rt", encoding="utf-8") as f:
        catalogue = json.load(f)
    assert len(catalogue["plans"]) == len(RESIDENTIAL_FIXTURES)
