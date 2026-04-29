from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from praha_predictor.config import NORMALIZED_DIR, REPORTS_DIR
from praha_predictor.storage import latest_source_health_reports


def disposition_bucket(value: str | None) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "unknown"
    value = str(value)
    if not value:
        return "unknown"
    prefix = value.split("+", 1)[0]
    return prefix if prefix.isdigit() else value


def normalized_address_fingerprint(value: str) -> str:
    value = (value or "").lower()
    return " ".join(value.replace("\xa0", " ").split())


def _group_outlier_indexes(frame: pd.DataFrame, group_index: pd.Index) -> list[int]:
    if len(group_index) < 5:
        return []
    series = frame.loc[group_index, "log_price_per_m2"].astype(float)
    median = float(series.median())
    abs_dev = (series - median).abs()
    mad = float(abs_dev.median())
    q1 = float(series.quantile(0.25))
    q3 = float(series.quantile(0.75))
    iqr = q3 - q1
    flagged: list[int] = []
    if mad > 0:
        robust_z = abs_dev / (1.4826 * mad)
        flagged.extend(series.index[robust_z > 3.5].tolist())
    elif iqr > 0:
        lower = q1 - 2.5 * iqr
        upper = q3 + 2.5 * iqr
        flagged.extend(series.index[(series < lower) | (series > upper)].tolist())
    return flagged


def mark_outliers(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["outlier_flag"] = False
    frame["outlier_segment"] = None
    frame["disposition_bucket"] = frame["disposition"].map(disposition_bucket)
    group_levels = [
        ["property_type", "district_prague", "disposition_bucket"],
        ["property_type", "district_prague"],
        ["property_type"],
    ]
    for group_cols in group_levels:
        grouped = frame.groupby(group_cols, dropna=False)
        for group_key, group in grouped:
            min_size = 8 if group_cols != ["property_type"] else 10
            if len(group) < min_size:
                continue
            flagged_indexes = _group_outlier_indexes(frame, group.index)
            if not flagged_indexes:
                continue
            frame.loc[flagged_indexes, "outlier_flag"] = True
            frame.loc[flagged_indexes, "outlier_segment"] = "|".join(map(str, group_key if isinstance(group_key, tuple) else (group_key,)))
    return frame


def _load_previous_curated_ids() -> set[tuple[str, str]]:
    previous_curated_path = NORMALIZED_DIR / "curated-current.parquet"
    if not previous_curated_path.exists():
        return set()
    previous = pd.read_parquet(previous_curated_path, columns=["source", "source_listing_id"])
    return {
        (str(row.source), str(row.source_listing_id))
        for row in previous.itertuples(index=False)
    }


def _build_source_coverage(curated_frame: pd.DataFrame) -> dict[str, int]:
    if curated_frame.empty or "source" not in curated_frame.columns:
        return {}
    return (
        curated_frame.groupby("source").size().sort_values(ascending=False).astype(int).to_dict()
    )


def build_quality_report(
    current_frame: pd.DataFrame,
    curated_frame: pd.DataFrame,
    previous_report: dict[str, Any] | None,
    current_rejects_frame: pd.DataFrame | None,
    new_curated_rows: int,
) -> dict[str, Any]:
    district_distribution = (
        curated_frame.groupby("district_prague").size().sort_values(ascending=False).astype(int).to_dict()
        if not curated_frame.empty
        else {}
    )
    missingness = (
        current_frame.isna().mean().sort_values(ascending=False).round(4).to_dict()
        if not current_frame.empty
        else {}
    )
    reject_reason_counts = (
        current_rejects_frame.groupby("reason").size().sort_values(ascending=False).astype(int).to_dict()
        if current_rejects_frame is not None and not current_rejects_frame.empty
        else {}
    )
    reject_reason_by_source = (
        current_rejects_frame.groupby(["source", "reason"]).size().sort_values(ascending=False).astype(int).to_dict()
        if current_rejects_frame is not None
        and not current_rejects_frame.empty
        and {"source", "reason"}.issubset(current_rejects_frame.columns)
        else {}
    )
    curation_reject_counts = (
        current_frame.loc[current_frame["curation_reject_reason"].notna()]
        .groupby("curation_reject_reason")
        .size()
        .sort_values(ascending=False)
        .astype(int)
        .to_dict()
        if not current_frame.empty
        else {}
    )
    duplicate_rate = round(float(current_frame["duplicate_flag"].mean()) if not current_frame.empty else 0.0, 4)
    outlier_rate = round(float(current_frame["outlier_flag"].mean()) if not current_frame.empty else 0.0, 4)
    outlier_summary = (
        current_frame.loc[current_frame["outlier_flag"]]
        .groupby(["district_prague", "property_type"])
        .size()
        .sort_values(ascending=False)
        .astype(int)
        .to_dict()
        if not current_frame.empty
        else {}
    )
    per_source_coverage = _build_source_coverage(curated_frame)
    source_health = latest_source_health_reports()
    degraded_sources = sorted(
        source
        for source, report in source_health.items()
        if report.get("degraded")
    )
    overall_status = "degraded" if degraded_sources else "success"

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": overall_status,
        "degraded_sources": degraded_sources,
        "current_records": int(len(current_frame)),
        "curated_records": int(len(curated_frame)),
        "rejected_records": int(len(current_frame) - len(curated_frame)),
        "new_curated_rows_since_previous_run": int(new_curated_rows),
        "dedup_rate": duplicate_rate,
        "outlier_rate": outlier_rate,
        "district_coverage": district_distribution,
        "market_coverage": district_distribution,
        "district_outlier_summary": {str(key): value for key, value in outlier_summary.items()},
        "missingness": missingness,
        "per_source_coverage": per_source_coverage,
        "fetch_parse_failure_classes": reject_reason_counts,
        "reject_reason_breakdown": reject_reason_counts,
        "reject_reason_breakdown_by_source": {str(key): value for key, value in reject_reason_by_source.items()},
        "curation_reject_breakdown": curation_reject_counts,
        "source_health": source_health,
        "drift": {},
    }
    if previous_report:
        previous_coverage = previous_report.get("district_coverage", {})
        drift = {}
        for district in sorted(set(previous_coverage) | set(district_distribution)):
            previous_count = previous_coverage.get(district, 0)
            current_count = district_distribution.get(district, 0)
            if previous_count != current_count:
                drift[district] = {"previous": previous_count, "current": current_count}
        report["drift"] = drift
    return report


def curate_current_view(
    current_normalized_path: Path,
    current_rejects_path: Path | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    frame = pd.read_parquet(current_normalized_path)
    if frame.empty:
        report = build_quality_report(frame, frame, None, None, 0)
        return frame, report

    frame = frame.copy()
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
    frame["quality_flags"] = frame["quality_flags"].apply(
        lambda value: value if isinstance(value, list) else []
    )
    frame["fingerprint"] = (
        frame["address_text"].map(normalized_address_fingerprint)
        + "|"
        + frame["floor_area_m2"].round(1).astype(str)
        + "|"
        + frame["price_czk"].round(-4).astype(int).astype(str)
        + "|"
        + frame["property_type"].astype(str)
    )
    frame["geo_duplicate_bucket"] = (
        frame["lat"].round(3).astype(str)
        + "|"
        + frame["lng"].round(3).astype(str)
        + "|"
        + frame["floor_area_m2"].round(1).astype(str)
        + "|"
        + frame["price_czk"].round(-4).astype(int).astype(str)
        + "|"
        + frame["property_type"].astype(str)
    )
    frame = frame.sort_values("observed_at", ascending=False)
    frame["duplicate_flag"] = frame.duplicated(subset=["fingerprint"], keep="first")
    frame["near_duplicate_flag"] = frame.duplicated(subset=["geo_duplicate_bucket"], keep="first")
    frame["log_price_per_m2"] = frame["price_per_m2"].map(lambda value: float(np.log(value)))
    frame = mark_outliers(frame)
    frame["curation_reject_reason"] = None
    frame.loc[frame["duplicate_flag"], "curation_reject_reason"] = "duplicate_fingerprint"
    frame.loc[~frame["duplicate_flag"] & frame["near_duplicate_flag"], "curation_reject_reason"] = "duplicate_geo_price_area"
    frame.loc[~frame["duplicate_flag"] & ~frame["near_duplicate_flag"] & frame["outlier_flag"], "curation_reject_reason"] = "outlier_price_per_m2"

    curated = frame.loc[
        ~frame["duplicate_flag"] & ~frame["near_duplicate_flag"] & ~frame["outlier_flag"]
    ].copy()

    previous_report_path = REPORTS_DIR / "quality-report-latest.json"
    previous_report = None
    if previous_report_path.exists():
        previous_report = json.loads(previous_report_path.read_text(encoding="utf-8"))

    previous_curated_ids = _load_previous_curated_ids()
    current_curated_ids = {
        (str(row.source), str(row.source_listing_id))
        for row in curated[["source", "source_listing_id"]].itertuples(index=False)
    }
    new_curated_rows = len(current_curated_ids - previous_curated_ids)

    current_rejects_frame = None
    if current_rejects_path and current_rejects_path.exists():
        current_rejects_frame = pd.read_parquet(current_rejects_path)

    report = build_quality_report(
        frame,
        curated,
        previous_report,
        current_rejects_frame,
        new_curated_rows,
    )
    return curated, report
