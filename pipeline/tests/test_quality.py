from __future__ import annotations

import math

import pandas as pd

from praha_predictor.cli import _build_source_health_report
from praha_predictor.config import PipelineConfig
from praha_predictor.quality import build_quality_report, mark_outliers
from praha_predictor.schemas import SourceProbeReport


def test_mark_outliers_flags_price_per_m2_spike() -> None:
    frame = pd.DataFrame(
        [
            {
                "property_type": "flat",
                "district_prague": "Vysočany",
                "disposition": "2+kk",
                "price_per_m2": value,
            }
            for value in [120000, 123000, 121500, 124200, 122800, 119900, 365000, 121200]
        ]
    )
    frame["log_price_per_m2"] = frame["price_per_m2"].map(math.log)
    out = mark_outliers(frame)
    assert out["outlier_flag"].sum() == 1


def test_quality_report_contains_reject_breakdowns() -> None:
    current_frame = pd.DataFrame(
        [
            {
                "district_prague": "Praha 9",
                "property_type": "flat",
                "duplicate_flag": False,
                "outlier_flag": False,
                "curation_reject_reason": None,
            },
            {
                "district_prague": "Praha okolí",
                "property_type": "house",
                "duplicate_flag": True,
                "outlier_flag": False,
                "curation_reject_reason": "duplicate_fingerprint",
            },
        ]
    )
    curated_frame = current_frame.loc[[0]].copy()
    rejects_frame = pd.DataFrame(
        [
            {"source": "realitymix", "reason": "missing_price_or_area"},
            {"source": "realitymix", "reason": "missing_price_or_area"},
            {"source": "remax", "reason": "outside_target_region"},
        ]
    )

    report = build_quality_report(
        current_frame=current_frame,
        curated_frame=curated_frame,
        previous_report=None,
        current_rejects_frame=rejects_frame,
        new_curated_rows=1,
    )

    assert report["reject_reason_breakdown"]["missing_price_or_area"] == 2
    assert report["curation_reject_breakdown"]["duplicate_fingerprint"] == 1
    assert report["reject_reason_breakdown_by_source"]["('realitymix', 'missing_price_or_area')"] == 2


def test_source_health_guardrail_ignores_outside_target_region_rejects() -> None:
    report = _build_source_health_report(
        source="realitymix",
        run_id="run-20260419T073845Z",
        discovered_count=250,
        processed_count=250,
        normalized_rows=[{"source_listing_id": f"listing-{index}"} for index in range(109)],
        reject_rows=[{"reason": "outside_target_region"} for _ in range(124)]
        + [{"reason": "missing_price_or_area"} for _ in range(17)],
        fetch_latencies_ms=[291.98],
        failure_classes=["outside_target_region"] * 124 + ["missing_price_or_area"] * 17,
        probe_report=SourceProbeReport(
            source="realitymix",
            sampled_urls=[],
            sampled_count=0,
            field_coverage={},
            accepted_count=0,
            coverage_score=1.0,
            decision="active_tertiary",
        ),
        config=PipelineConfig(),
    )

    assert report["ignored_reject_count"] == 141
    assert report["guardrail_parse_failure_count"] == 0
    assert report["parse_candidate_count"] == 109
    assert report["parse_success_rate"] == 1.0
    assert report["status"] == "success"
    assert report["degraded_reasons"] == []


def test_source_health_guardrail_ignores_benign_rejects_and_gone_pages() -> None:
    report = _build_source_health_report(
        source="remax",
        run_id="run-20260420T042024Z",
        discovered_count=250,
        processed_count=250,
        normalized_rows=[{"source_listing_id": f"listing-{index}"} for index in range(43)],
        reject_rows=[{"reason": "missing_price_or_area"} for _ in range(147)]
        + [{"reason": "missing_coordinates"} for _ in range(6)]
        + [{"reason": "outside_target_region"} for _ in range(6)]
        + [{"reason": "fetch_error"} for _ in range(48)],
        fetch_latencies_ms=[233.0],
        failure_classes=["missing_price_or_area"] * 147
        + ["missing_coordinates"] * 6
        + ["outside_target_region"] * 6
        + ["http_410"] * 48,
        probe_report=SourceProbeReport(
            source="remax",
            sampled_urls=[],
            sampled_count=0,
            field_coverage={},
            accepted_count=0,
            coverage_score=0.9,
            decision="active_secondary",
        ),
        config=PipelineConfig(),
    )

    assert report["benign_fetch_failure_count"] == 48
    assert report["hard_fetch_failure_count"] == 0
    assert report["fetch_candidate_count"] == 202
    assert report["fetch_success_rate"] == 1.0
    assert report["ignored_reject_count"] == 159
    assert report["guardrail_parse_failure_count"] == 0
    assert report["parse_candidate_count"] == 43
    assert report["parse_success_rate"] == 1.0
    assert report["status"] == "success"
    assert report["degraded_reasons"] == []
