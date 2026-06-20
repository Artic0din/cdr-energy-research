"""Tests for the sweep's completeness summary (publish gate).

``cdr_full_sweep_v2.write_summary`` produces the machine-readable record the
publish workflow reads to decide whether the catalogue may be released. Its
``complete`` flag is the load-bearing gate: it must be True only when no retailer
list and no plan-detail fetch failed, so a partial catalogue is never published.
"""
from __future__ import annotations

import json
import os

import cdr_full_sweep_v2 as sweep


def _stat(slug: str, list_error: object = None, plans_failed: int = 0) -> dict:
    return {"slug": slug, "list_error": list_error, "plans_failed": plans_failed}


def _run_write_summary(monkeypatch, tmp_path, stats, plan_detail_failures):
    summary_path = os.path.join(str(tmp_path), "_summary_v2.json")
    monkeypatch.setattr(sweep, "SUMMARY_PATH", summary_path)
    result = sweep.write_summary(stats, plan_detail_failures)
    with open(summary_path, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk == result
    return result


def test_summary_complete_when_no_failures(monkeypatch, tmp_path):
    stats = [_stat("globird"), _stat("covau"), _stat("agl")]
    summary = _run_write_summary(monkeypatch, tmp_path, stats, plan_detail_failures=0)

    assert summary["complete"] is True
    assert summary["retailers_total"] == 3
    assert summary["retailers_reachable"] == 3
    assert summary["list_failures"] == 0
    assert summary["plan_detail_failures"] == 0


def test_summary_incomplete_on_list_failure(monkeypatch, tmp_path):
    stats = [_stat("globird"), _stat("dead", list_error="HTTP 503")]
    summary = _run_write_summary(monkeypatch, tmp_path, stats, plan_detail_failures=0)

    assert summary["complete"] is False
    assert summary["list_failures"] == 1
    assert summary["retailers_reachable"] == 1


def test_summary_incomplete_on_plan_detail_failure(monkeypatch, tmp_path):
    stats = [_stat("globird", plans_failed=2), _stat("covau")]
    # The caller passes this run's plan-detail failure total explicitly.
    summary = _run_write_summary(monkeypatch, tmp_path, stats, plan_detail_failures=2)

    assert summary["complete"] is False
    assert summary["list_failures"] == 0
    assert summary["plan_detail_failures"] == 2
    # A plan-detail failure does not mark the retailer's list unreachable.
    assert summary["retailers_reachable"] == 2
