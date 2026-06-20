"""Tests for the v2 sweep's cdrBrand contract on shared/co-hosted endpoints.

The AER registry hosts several brands behind a single base URI (one `cdrCode`),
distinguished only by `cdrBrand`. The sweep must:

1. Keep every co-hosted brand as its own retailer (dedupe by `cdrBrand`, not
   `cdrCode`, which would collapse Indigo/Cooperative/RAA/Energy Locals into one).
2. Filter shared endpoints with `?brand=<cdrBrand>`, not `<cdrCode>` (every
   co-hosted brand carries the same `cdrCode`, so `cdrCode` cannot disambiguate).

These tests pin the documented cdrBrand contract that README.md and AGENTS.md
tell users to rely on when regenerating the catalog.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

_SWEEP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "cdr_full_sweep_v2.py",
)


def _load_sweep():
    spec = importlib.util.spec_from_file_location("cdr_full_sweep_v2", _SWEEP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sweep = _load_sweep()


def _org(cdr_code: str, cdr_brand: str, org_name: str) -> dict[str, object]:
    return {
        "cdrCode": cdr_code,
        "cdrBrand": cdr_brand,
        "orgName": org_name,
        "tradingName": org_name,
        "electricityBillURL": f"https://example.test/{cdr_brand}/bill",
    }


# Mirrors the real refdata shape: Energy Locals hosts four brands on one
# cdrCode/base URI, and AGL ships three duplicate org records under one brand.
FIXTURE_REFDATA = {
    "data": {
        "organisations": {
            "org-indigo": _org("energy-locals", "indigo", "Indigo Power"),
            "org-cooperative": _org("energy-locals", "cooperative", "Cooperative Power"),
            "org-raa": _org("energy-locals", "raa", "RAA Energy"),
            "org-energy-locals": _org("energy-locals", "energy-locals", "Energy Locals Retail"),
            "org-agl-1": _org("agl", "agl", "AGL Retail Energy Limited"),
            "org-agl-2": _org("agl", "agl", "AGL Sales Pty Limited"),
            "org-agl-3": _org("agl", "agl", "AGL"),
        }
    }
}


def test_build_retailer_list_keeps_every_cohosted_brand() -> None:
    """Co-hosted brands share one cdrCode; each must survive as its own retailer."""
    retailers = sweep.build_retailer_list(FIXTURE_REFDATA)
    brands = {r["cdrBrand"] for r in retailers}
    assert {"indigo", "cooperative", "raa", "energy-locals"} <= brands
    # All four co-hosted brands present, not collapsed to one cdrCode.
    energy_locals_brands = [r for r in retailers if r["cdrCode"] == "energy-locals"]
    assert len(energy_locals_brands) == 4


def test_build_retailer_list_dedupes_duplicate_org_records_by_brand() -> None:
    """Multiple ABN org records for one brand (AGL ×3) collapse to a single retailer."""
    retailers = sweep.build_retailer_list(FIXTURE_REFDATA)
    agl = [r for r in retailers if r["cdrBrand"] == "agl"]
    assert len(agl) == 1
    # Fixture has 7 org records -> 4 distinct co-hosted brands + 1 AGL = 5 retailers.
    assert len(retailers) == 5


def test_process_retailer_filters_shared_base_by_cdrbrand(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a shared base URI the plan-list filter must be cdrBrand, not cdrCode."""
    retailers = sweep.build_retailer_list(FIXTURE_REFDATA)
    indigo = next(r for r in retailers if r["cdrBrand"] == "indigo")

    # All four energy-locals brands share one base URI -> shared endpoint.
    base_to_brands: dict[str, list[str]] = {}
    for r in retailers:
        base_to_brands.setdefault(r["baseUri"], []).append(r["cdrBrand"])

    captured: dict[str, str | None] = {}

    def fake_fetch_plan_list(base: str, slug: str, brand_filter: str | None = None):
        captured["brand_filter"] = brand_filter
        return [], None

    monkeypatch.setattr(sweep, "fetch_plan_list", fake_fetch_plan_list)
    monkeypatch.setattr(sweep, "checkpoint", lambda *a, **k: None)

    sweep.process_retailer(indigo, base_to_brands)

    # The bug: filter was the shared cdrCode "energy-locals", returning every
    # co-hosted brand's plans. The contract requires the distinguishing cdrBrand.
    assert captured["brand_filter"] == "indigo"


def test_process_retailer_unique_base_uses_no_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A brand on its own base URI must not pass a brand filter (fetch everything)."""
    retailers = sweep.build_retailer_list(FIXTURE_REFDATA)
    agl = next(r for r in retailers if r["cdrBrand"] == "agl")

    base_to_brands: dict[str, list[str]] = {}
    for r in retailers:
        base_to_brands.setdefault(r["baseUri"], []).append(r["cdrBrand"])

    captured: dict[str, str | None] = {}

    def fake_fetch_plan_list(base: str, slug: str, brand_filter: str | None = None):
        captured["brand_filter"] = brand_filter
        return [], None

    monkeypatch.setattr(sweep, "fetch_plan_list", fake_fetch_plan_list)
    monkeypatch.setattr(sweep, "checkpoint", lambda *a, **k: None)

    sweep.process_retailer(agl, base_to_brands)

    assert captured["brand_filter"] is None
