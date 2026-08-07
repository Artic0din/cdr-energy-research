"""Tests for the sweep's fetch/refresh behaviour and partial-failure handling.

These guard the publish gate:
  - ``--refresh`` must bypass cached plan lists and plan details so a scheduled
    run is built from live data, never a stale cache.
  - A plan-list request that fails part-way through pagination must record a
    ``list_error`` so the run is flagged incomplete, even though earlier pages
    returned plans.

All network is monkeypatched; no real HTTP and no real ``/tmp/cdr-cache`` writes.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

import cdr_full_sweep_v2 as sweep


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    """Point the sweep's cache + bookkeeping files at a tmp dir.

    Patches every module-global path the worker writes to so tests never touch
    the real ``/tmp/cdr-cache``.
    """
    cache = os.path.join(str(tmp_path), "cdr-cache")
    os.makedirs(cache, exist_ok=True)
    monkeypatch.setattr(sweep, "CACHE_DIR", cache)
    monkeypatch.setattr(sweep, "FAILED_PATH", os.path.join(cache, "_failed_v2.jsonl"))
    monkeypatch.setattr(sweep, "PROGRESS_PATH", os.path.join(cache, "_progress_v2.json"))
    return cache


def _planlist_response(plan_ids: list[str], total_pages: int = 1) -> dict:
    return {
        "data": {"plans": [{"planId": pid} for pid in plan_ids]},
        "meta": {"totalPages": total_pages},
    }


def test_fetch_plan_list_uses_cache_when_not_refreshing(isolated_cache, monkeypatch):
    slug = "globird"
    cache_file = os.path.join(sweep.cache_dir(slug), "_planlist.json")
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump([{"planId": "CACHED-001@EME"}], f)

    def _fail(*_a, **_k):  # network must not be hit
        raise AssertionError("polite_get called despite warm cache")

    monkeypatch.setattr(sweep, "polite_get", _fail)
    plans, err = sweep.fetch_plan_list("https://x", slug)

    assert err is None
    assert [p["planId"] for p in plans] == ["CACHED-001@EME"]


def test_fetch_plan_list_refresh_bypasses_cache(isolated_cache, monkeypatch):
    slug = "globird"
    cache_file = os.path.join(sweep.cache_dir(slug), "_planlist.json")
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump([{"planId": "STALE-001@EME"}], f)

    calls = {"n": 0}

    def _live(url, headers, s):
        calls["n"] += 1
        return _planlist_response(["LIVE-001@EME"]), None, 200

    monkeypatch.setattr(sweep, "polite_get", _live)
    plans, err = sweep.fetch_plan_list("https://x", slug, refresh=True)

    assert err is None
    assert calls["n"] == 1, "refresh must hit the network"
    assert [p["planId"] for p in plans] == ["LIVE-001@EME"]
    # Refreshed list is written back to cache.
    with open(cache_file, encoding="utf-8") as f:
        assert json.load(f) == [{"planId": "LIVE-001@EME"}]


def test_fetch_plan_detail_refresh_bypasses_cache(isolated_cache, monkeypatch):
    slug = "globird"
    plan_id = "GLO-001@EME"
    cache_file = os.path.join(sweep.cache_dir(slug), f"{plan_id}.json")
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump({"data": {"planId": plan_id, "stale": True}}, f)

    fresh = {"data": {"planId": plan_id, "stale": False}}
    monkeypatch.setattr(sweep, "polite_get", lambda *a, **k: (fresh, None, 200))

    detail, err = sweep.fetch_plan_detail("https://x", plan_id, slug, refresh=True)
    assert err is None
    assert detail["data"]["stale"] is False
    with open(cache_file, encoding="utf-8") as f:
        assert json.load(f)["data"]["stale"] is False


def test_partial_list_failure_records_list_error(isolated_cache, monkeypatch):
    """Page 1 succeeds, page 2 fails: list_error must be recorded as incomplete."""
    slug = "bigco"

    def _paged(url, headers, s):
        # page-size is large; pagination is driven by totalPages=2.
        if "page=1" in url:
            return _planlist_response(["P1-001@EME"], total_pages=2), None, 200
        return None, "HTTP 503", 503

    monkeypatch.setattr(sweep, "polite_get", _paged)
    r = {
        "slug": slug,
        "baseUri": "https://x",
        "cdrCode": "bigco",
        "tradingName": "Big Co",
        "orgName": "Big Co Pty Ltd",
    }
    stats = sweep.process_retailer(r, {"https://x": ["bigco"]}, refresh=True)

    assert stats["list_error"] is not None, "partial-page failure must set list_error"
    assert "503" in str(stats["list_error"])


def test_fetch_plan_list_fails_closed_when_pagination_exceeds_cap(
    isolated_cache,
):
    """A capped response must not be returned or cached as a complete list."""
    slug = "hugeco"
    calls = {"n": 0}

    def _paged(url, headers, s):
        calls["n"] += 1
        return _planlist_response([f"P{calls['n']}@EME"], total_pages=31), None, 200

    with patch.object(sweep, "polite_get", _paged):
        plans, err = sweep.fetch_plan_list("https://x", slug, refresh=True)

    assert plans == []
    assert err == "pagination:totalPages=31 exceeds maximum 30"
    assert calls["n"] == 1
    assert not os.path.exists(os.path.join(sweep.cache_dir(slug), "_planlist.json"))
