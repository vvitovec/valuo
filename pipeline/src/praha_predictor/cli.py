from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
from statistics import median
from typing import Any

import pandas as pd

from praha_predictor.config import (
    ARTIFACTS_DIR,
    INDEX_DIR,
    NORMALIZED_DIR,
    REPORTS_DIR,
    PipelineConfig,
    TRAINING_REGISTRY_PATH,
)
from praha_predictor.http import HttpFetchError
from praha_predictor.schemas import RejectReason, RunContext, SourceProbeReport
from praha_predictor.sources.base import ListingSourceAdapter
from praha_predictor.sources.bezrealitky import BezrealitkyAdapter
from praha_predictor.sources.realitymix import RealityMixAdapter
from praha_predictor.sources.remax import RemaxAdapter
from praha_predictor.storage import (
    finish_source_run,
    get_frontier_candidate_urls,
    get_frontier_stats,
    rebuild_current_views,
    save_report,
    save_rows_to_parquet,
    start_source_run,
    upsert_frontier_urls,
    update_listing_current_index,
    mark_frontier_result,
    write_raw_snapshot,
)

BENIGN_REJECTION_REASONS = {
    "missing_coordinates",
    "missing_price_or_area",
    "non_czk_currency",
    "outside_target_region",
    "unsupported_property_type",
    "wrong_offer_type",
}
BENIGN_FETCH_FAILURE_CLASSES = {"http_404", "http_410"}


def _current_curated_count() -> int:
    curated_path = NORMALIZED_DIR / "curated-current.parquet"
    if not curated_path.exists():
        return 0
    frame = pd.read_parquet(curated_path, columns=["source_listing_id"])
    return int(len(frame))


def _latest_quality_report() -> dict[str, Any] | None:
    quality_path = REPORTS_DIR / "quality-report-latest.json"
    if not quality_path.exists():
        return None
    return json.loads(quality_path.read_text(encoding="utf-8"))


def _make_run_context(max_listings: int | None, source: str) -> RunContext:
    timestamp = datetime.now(timezone.utc)
    return RunContext(
        run_id=timestamp.strftime("run-%Y%m%dT%H%M%SZ"),
        observed_at=timestamp.isoformat(),
        max_listings=max_listings,
        source=source,
    )


def _source_adapters(config: PipelineConfig) -> list[ListingSourceAdapter]:
    primary = BezrealitkyAdapter(config)
    secondary_probe = RemaxAdapter(config).probe_source(20)
    secondary_adapter: ListingSourceAdapter | None = None
    if secondary_probe.coverage_score >= 0.8:
        secondary_adapter = RemaxAdapter(config)
    tertiary_probe = RealityMixAdapter(config).probe_source(20)
    adapters: list[ListingSourceAdapter] = [primary]
    if secondary_adapter is not None:
        adapters.append(secondary_adapter)
    if tertiary_probe.coverage_score >= 0.8:
        adapters.append(RealityMixAdapter(config))
    return adapters


def _choose_urls_for_run(
    adapter: ListingSourceAdapter,
    run_context: RunContext,
    config: PipelineConfig,
) -> list[str]:
    target = run_context.max_listings or config.max_listings_default
    frontier_urls = get_frontier_candidate_urls(adapter.source_name, target)
    frontier_stats = get_frontier_stats(adapter.source_name)
    if len(frontier_urls) >= target and frontier_stats["unfetched_count"] >= target:
        return frontier_urls[:target]

    top_up_target = max(
        target,
        int(target * config.frontier_top_up_factor),
        frontier_stats["active_count"] + int(target * config.frontier_top_up_factor),
    )
    discovered_urls = adapter.discover_listing_urls(
        RunContext(
            run_id=run_context.run_id,
            observed_at=run_context.observed_at,
            max_listings=top_up_target,
            source=adapter.source_name,
        )
    )
    upsert_frontier_urls(
        adapter.source_name,
        discovered_urls,
        discovered_at=run_context.observed_at,
        discovery_method="sitemap",
    )
    return get_frontier_candidate_urls(adapter.source_name, target)


class ListingProcessingTimeout(RuntimeError):
    pass


class _ListingTimeout:
    def __init__(self, seconds: int) -> None:
        self.seconds = max(int(seconds), 1)
        self._previous_handler: Any = None

    def _handle_timeout(self, signum: int, frame: Any) -> None:
        raise ListingProcessingTimeout(f"listing processing exceeded {self.seconds}s")

    def __enter__(self) -> None:
        self._previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, self._handle_timeout)
        signal.setitimer(signal.ITIMER_REAL, float(self.seconds))

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, self._previous_handler)


def _build_source_health_report(
    *,
    source: str,
    run_id: str,
    discovered_count: int,
    processed_count: int,
    normalized_rows: list[dict[str, Any]],
    reject_rows: list[dict[str, Any]],
    fetch_latencies_ms: list[float],
    failure_classes: list[str],
    probe_report: SourceProbeReport,
    config: PipelineConfig,
) -> dict[str, Any]:
    failure_counter = Counter(failure_classes)
    normalized_count = len(normalized_rows)
    rejected_count = len(reject_rows)
    fetch_failure_count = sum(
        count
        for failure_class, count in failure_counter.items()
        if failure_class == "fetch_error"
        or failure_class.startswith(("http_", "network_", "timeout", "unknown_"))
    )
    benign_fetch_failure_count = sum(
        failure_counter.get(reason, 0)
        for reason in BENIGN_FETCH_FAILURE_CLASSES
    )
    hard_fetch_failure_count = max(fetch_failure_count - benign_fetch_failure_count, 0)
    fetch_success_count = processed_count - fetch_failure_count
    fetch_candidate_count = processed_count - benign_fetch_failure_count
    ignored_reject_count = sum(
        failure_counter.get(reason, 0)
        for reason in BENIGN_REJECTION_REASONS
    )
    parse_failure_count = sum(
        count
        for failure_class, count in failure_counter.items()
        if failure_class != "fetch_error"
    )
    guardrail_parse_failure_count = sum(
        count
        for failure_class, count in failure_counter.items()
        if failure_class not in BENIGN_REJECTION_REASONS
        and failure_class not in BENIGN_FETCH_FAILURE_CLASSES
        and failure_class != "fetch_error"
        and not failure_class.startswith(("http_", "network_", "timeout", "unknown_"))
    )
    parse_candidate_count = normalized_count + guardrail_parse_failure_count
    fetch_success_rate = (
        1.0
        if fetch_candidate_count == 0
        else round(fetch_success_count / fetch_candidate_count, 4)
    )
    parse_success_rate = (
        1.0
        if parse_candidate_count == 0
        else round(normalized_count / parse_candidate_count, 4)
    )
    guardrails = {
        "fetch_success_rate": {
            "actual": fetch_success_rate,
            "minimum": config.source_min_fetch_success_rate,
            "passed": fetch_success_rate >= config.source_min_fetch_success_rate,
        },
        "parse_success_rate": {
            "actual": parse_success_rate,
            "minimum": config.source_min_parse_success_rate,
            "passed": parse_success_rate >= config.source_min_parse_success_rate,
        },
    }
    degraded_reasons = [
        name
        for name, details in guardrails.items()
        if not details["passed"]
    ]
    status = "degraded" if degraded_reasons else "success"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "source": source,
        "discovered_count": discovered_count,
        "processed_count": processed_count,
        "normalized_count": normalized_count,
        "rejected_count": rejected_count,
        "fetch_success_count": fetch_success_count,
        "fetch_failure_count": fetch_failure_count,
        "hard_fetch_failure_count": hard_fetch_failure_count,
        "benign_fetch_failure_count": benign_fetch_failure_count,
        "fetch_candidate_count": fetch_candidate_count,
        "parse_failure_count": parse_failure_count,
        "guardrail_parse_failure_count": guardrail_parse_failure_count,
        "ignored_reject_count": ignored_reject_count,
        "parse_candidate_count": parse_candidate_count,
        "fetch_success_rate": fetch_success_rate,
        "parse_success_rate": parse_success_rate,
        "median_latency_ms": round(median(fetch_latencies_ms), 2) if fetch_latencies_ms else None,
        "failure_classes": dict(failure_counter),
        "status": status,
        "degraded": bool(degraded_reasons),
        "degraded_reasons": degraded_reasons,
        "guardrails": guardrails,
        "probe": probe_report.to_dict(),
    }


def run_smoke_test(sample_size: int) -> int:
    config = PipelineConfig()
    adapter = BezrealitkyAdapter(config)
    run_context = _make_run_context(sample_size, adapter.source_name)
    urls = adapter.discover_listing_urls(run_context)
    print(f"Discovered {len(urls)} candidate URLs.")
    for url in urls[:sample_size]:
        snapshot = adapter.fetch_listing(url, run_context)
        normalized = adapter.normalize(snapshot)
        print(f"- {url}")
        if isinstance(normalized, RejectReason):
            print(f"  rejected: {normalized.reason}")
        else:
            print(
                "  normalized:",
                normalized.district_prague,
                normalized.property_type,
                int(normalized.floor_area_m2),
                int(normalized.price_czk),
            )
    return 0


def run_source_probe(sample_size: int) -> int:
    config = PipelineConfig()
    remax_probe = RemaxAdapter(config).probe_source(sample_size)
    save_report(remax_probe.to_dict(), REPORTS_DIR / "source-probe-remax-latest.json")
    print(f"RE/MAX coverage score: {remax_probe.coverage_score}")
    print(remax_probe.to_dict())
    realitymix_probe = RealityMixAdapter(config).probe_source(sample_size)
    save_report(
        realitymix_probe.to_dict(),
        REPORTS_DIR / "source-probe-realitymix-latest.json",
    )
    print(f"RealityMix coverage score: {realitymix_probe.coverage_score}")
    print(realitymix_probe.to_dict())
    return 0


def run_scrape(max_listings_per_source: int) -> int:
    config = PipelineConfig()
    adapters = [adapter for adapter in _source_adapters(config) if adapter is not None]
    normalized_rows: list[dict[str, Any]] = []
    reject_rows: list[dict[str, Any]] = []

    for adapter in adapters:
        run_context = _make_run_context(max_listings_per_source, adapter.source_name)
        start_source_run(run_context.run_id, adapter.source_name, run_context.observed_at)
        probe_report = adapter.probe_source(20 if adapter.source_name == "remax" else 8)
        urls = _choose_urls_for_run(adapter, run_context, config)
        print(f"[{adapter.source_name}] processing {len(urls)} URLs for {run_context.run_id}")

        source_normalized_rows: list[dict[str, Any]] = []
        source_reject_rows: list[dict[str, Any]] = []
        failure_classes: list[str] = []
        fetch_latencies_ms: list[float] = []

        for index, url in enumerate(urls, start=1):
            listing_id = url.rstrip("/").split("/")[-1]
            try:
                with _ListingTimeout(config.listing_processing_timeout_seconds):
                    snapshot = adapter.fetch_listing(url, run_context)
                write_raw_snapshot(snapshot)
                fetch_latencies_ms.append(float(snapshot.meta.get("latency_ms", 0.0)))
                related_urls = snapshot.meta.get("related_listing_urls", [])
                if related_urls:
                    upsert_frontier_urls(
                        adapter.source_name,
                        related_urls,
                        discovered_at=run_context.observed_at,
                        discovery_method="related_graph",
                    )
                normalized = adapter.normalize(snapshot)
                if isinstance(normalized, RejectReason):
                    source_reject_rows.append(normalized.to_dict())
                    reject_rows.append(normalized.to_dict())
                    failure_classes.append(normalized.reason)
                    mark_frontier_result(
                        adapter.source_name,
                        snapshot.listing_url,
                        source_listing_id=snapshot.source_listing_id,
                        observed_at=snapshot.observed_at,
                        success=False,
                        state="rejected",
                        error=normalized.reason,
                    )
                    update_listing_current_index(
                        adapter.source_name,
                        snapshot.source_listing_id,
                        listing_url=snapshot.listing_url,
                        observed_at=snapshot.observed_at,
                        content_hash=snapshot.content_hash,
                        current_state=normalized.reason,
                        current_kind="reject",
                    )
                else:
                    source_normalized_rows.append(normalized.to_dict())
                    normalized_rows.append(normalized.to_dict())
                    mark_frontier_result(
                        adapter.source_name,
                        snapshot.listing_url,
                        source_listing_id=snapshot.source_listing_id,
                        observed_at=snapshot.observed_at,
                        success=True,
                        state="normalized",
                    )
                    update_listing_current_index(
                        adapter.source_name,
                        snapshot.source_listing_id,
                        listing_url=snapshot.listing_url,
                        observed_at=snapshot.observed_at,
                        content_hash=snapshot.content_hash,
                        current_state="normalized",
                        current_kind="listing",
                )
                print(f"[{adapter.source_name} {index}/{len(urls)}] processed {snapshot.source_listing_id}")
            except Exception as error:
                failure_class = error.failure_class if isinstance(error, HttpFetchError) else "fetch_error"
                reject = RejectReason(
                    source=adapter.source_name,
                    source_listing_id=listing_id,
                    observed_at=run_context.observed_at,
                    listing_url=url,
                    content_hash="fetch-error",
                    reason="fetch_error",
                    details={"error": str(error), "failure_class": failure_class},
                )
                source_reject_rows.append(reject.to_dict())
                reject_rows.append(reject.to_dict())
                failure_classes.append(failure_class)
                mark_frontier_result(
                    adapter.source_name,
                    url,
                    source_listing_id=listing_id,
                    observed_at=run_context.observed_at,
                    success=False,
                    state="fetch_failed",
                    error=str(error),
                )
                update_listing_current_index(
                    adapter.source_name,
                    listing_id,
                    listing_url=url,
                    observed_at=run_context.observed_at,
                    content_hash="fetch-error",
                    current_state="fetch_error",
                    current_kind="reject",
                )
                print(f"[{adapter.source_name} {index}/{len(urls)}] fetch failed for {listing_id}: {error}")

        save_rows_to_parquet(
            source_normalized_rows,
            NORMALIZED_DIR / f"normalized-{run_context.run_id}.parquet",
        )
        save_rows_to_parquet(
            source_reject_rows,
            NORMALIZED_DIR / f"rejects-{run_context.run_id}.parquet",
        )
        health_report = _build_source_health_report(
            source=adapter.source_name,
            run_id=run_context.run_id,
            discovered_count=len(urls),
            processed_count=len(urls),
            normalized_rows=source_normalized_rows,
            reject_rows=source_reject_rows,
            fetch_latencies_ms=fetch_latencies_ms,
            failure_classes=failure_classes,
            probe_report=probe_report,
            config=config,
        )
        finish_source_run(run_context.run_id, adapter.source_name, health_report)
        health_report_path = REPORTS_DIR / f"source-health-{adapter.source_name}-latest.json"
        save_report(health_report, health_report_path)
    return 0


def run_train() -> int:
    _refresh_outputs(train_model=True)
    return 0


def run_refresh_outputs() -> int:
    _refresh_outputs(train_model=False)
    return 0


def _refresh_outputs(*, train_model: bool) -> dict[str, Any]:
    from praha_predictor.modeling import (
        ACTIVE_MODEL_PATH,
        refresh_active_model_runtime_metadata,
        train_and_export,
        write_training_artifacts,
    )
    from praha_predictor.opportunities import build_market_listing_scores
    from praha_predictor.quality import curate_current_view

    outputs = rebuild_current_views()
    if outputs["normalized"] is None:
        raise RuntimeError("Current normalized view was not produced.")

    curated_frame, report = curate_current_view(outputs["normalized"], outputs["rejects"])
    curated_path = NORMALIZED_DIR / "curated-current.parquet"
    curated_frame.to_parquet(curated_path, index=False)
    _update_training_registry(curated_frame)
    timestamp_slug = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    versioned_report_path = REPORTS_DIR / f"quality-report-{timestamp_slug}.json"
    latest_report_path = REPORTS_DIR / "quality-report-latest.json"
    save_report(report, versioned_report_path)
    save_report(report, latest_report_path)

    print(f"curated_current: {curated_path}")
    print(f"quality_report: {latest_report_path}")

    selected_model_kind = None
    promoted = False
    artifact_paths: dict[str, Path] = {}

    if train_model and len(curated_frame) < 6:
        print("Curated dataset is too small for a stable model candidate; skipping training.")
    elif train_model:
        training_artifacts = train_and_export(curated_frame)
        artifact_paths = write_training_artifacts(training_artifacts)
        selected_model_kind = training_artifacts.selected_model_kind
        promoted = training_artifacts.promoted
        print(f"selected_model: {selected_model_kind}")
        print(f"promoted: {promoted}")
        for label, path in artifact_paths.items():
            print(f"{label}: {path}")

    refresh_active_model_runtime_metadata(curated_frame)
    opportunities_path = REPORTS_DIR / "market-opportunities-latest.json"
    opportunities_rows = 0
    opportunities_summary = {"under_market": 0, "over_market": 0}
    if ACTIVE_MODEL_PATH.exists():
        opportunities_frame, opportunities_summary = build_market_listing_scores(
            outputs["normalized"],
            INDEX_DIR / "listing-frontier.parquet",
            output_path=opportunities_path,
        )
        opportunities_rows = len(opportunities_frame)
        print(f"market_opportunities: {opportunities_path}")
        print(f"market_opportunities_rows: {opportunities_rows}")
        print(f"market_opportunities_under_market: {opportunities_summary['under_market']}")
        print(f"market_opportunities_over_market: {opportunities_summary['over_market']}")
    else:
        print("market_opportunities: skipped (active model not available)")
    return {
        "curatedRows": int(len(curated_frame)),
        "newCuratedRowsSincePreviousRun": int(report.get("new_curated_rows_since_previous_run", 0)),
        "qualityReportGeneratedAt": report.get("generated_at"),
        "qualityStatus": report.get("status", "success"),
        "degradedSources": report.get("degraded_sources", []),
        "selectedModel": selected_model_kind,
        "promoted": promoted,
        "artifactPaths": {label: str(path) for label, path in artifact_paths.items()},
        "marketOpportunitiesRows": opportunities_rows,
        "marketOpportunitiesSummary": opportunities_summary,
    }


def _update_training_registry(curated_frame: pd.DataFrame) -> None:
    if curated_frame.empty:
        return
    registry_columns = [
        "source",
        "source_listing_id",
        "property_type",
        "district_prague",
        "location_cluster",
        "address_text",
        "price_czk",
        "price_per_m2",
        "floor_area_m2",
        "land_area_m2",
        "observed_at",
        "content_hash",
    ]
    registry_frame = curated_frame.copy()
    registry_frame["location_cluster"] = registry_frame.get("district_prague")
    if TRAINING_REGISTRY_PATH.exists():
        existing = pd.read_parquet(TRAINING_REGISTRY_PATH)
        registry_frame = pd.concat([existing, registry_frame[registry_columns]], ignore_index=True)
    else:
        registry_frame = registry_frame[registry_columns]
    registry_frame["observed_at"] = pd.to_datetime(registry_frame["observed_at"], utc=True)
    registry_frame = (
        registry_frame.sort_values(["source", "source_listing_id", "observed_at"])
        .drop_duplicates(subset=["source", "source_listing_id"], keep="last")
        .reset_index(drop=True)
    )
    TRAINING_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    registry_frame.to_parquet(TRAINING_REGISTRY_PATH, index=False)


def run_backfill(
    *,
    target_curated_rows: int,
    max_rounds: int,
    max_listings_per_source: int,
) -> int:
    config = PipelineConfig()
    target_curated_rows = max(target_curated_rows, config.backfill_target_curated_rows)
    max_rounds = max(max_rounds, 1)
    starting_count = _current_curated_count()
    print(f"starting_curated_rows: {starting_count}")
    print(f"target_curated_rows: {target_curated_rows}")

    last_count = starting_count
    stagnation_rounds = 0
    for round_index in range(1, max_rounds + 1):
        print(f"backfill_round: {round_index}/{max_rounds}")
        run_full_pipeline(max_listings_per_source)
        current_count = _current_curated_count()
        print(f"curated_rows_after_round: {current_count}")
        if current_count >= target_curated_rows:
            print("backfill_target_reached: true")
            return 0
        if current_count <= last_count:
            stagnation_rounds += 1
        else:
            stagnation_rounds = 0
        last_count = current_count
        if stagnation_rounds >= 2:
            print("backfill_stopped_due_to_stagnation: true")
            return 0
    print("backfill_target_reached: false")
    return 0


def run_status() -> int:
    curated_count = _current_curated_count()
    quality_report = _latest_quality_report()
    model_registry_path = ARTIFACTS_DIR / "model-registry.json"
    registry = (
        json.loads(model_registry_path.read_text(encoding="utf-8"))
        if model_registry_path.exists()
        else None
    )
    print(f"curated_rows: {curated_count}")
    if quality_report:
        print(f"new_curated_rows_since_previous_run: {quality_report.get('new_curated_rows_since_previous_run')}")
        print(f"per_source_coverage: {quality_report.get('per_source_coverage')}")
    if registry:
        print(f"active_model_version: {registry.get('activeModelVersion')}")
        print(f"last_promoted_at: {registry.get('lastPromotedAt')}")
        latest_entry = registry.get("entries", [{}])[0]
        print(f"latest_candidate_version: {latest_entry.get('version')}")
        print(f"latest_candidate_curated_rows: {latest_entry.get('curatedRowCount')}")
        print(f"latest_candidate_promotion_reason: {latest_entry.get('promotionReason')}")
    return 0


def run_full_pipeline(max_listings_per_source: int) -> int:
    run_scrape(max_listings_per_source)
    return run_train()


def main() -> int:
    parser = argparse.ArgumentParser(description="Praha residential price predictor pipeline.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    smoke_parser = subparsers.add_parser("smoke-test", help="Run a live scraper smoke test.")
    smoke_parser.add_argument("--sample-size", type=int, default=3)

    probe_parser = subparsers.add_parser("probe-sources", help="Probe source field coverage.")
    probe_parser.add_argument("--sample-size", type=int, default=20)

    scrape_parser = subparsers.add_parser("scrape", help="Run source discovery and scraping only.")
    scrape_parser.add_argument("--max-listings-per-source", type=int, default=80)

    train_parser = subparsers.add_parser("train", help="Curate current data and train/promote a model.")
    subparsers.add_parser(
        "refresh-outputs",
        help="Rebuild current reports and dashboard feed without retraining the model.",
    )

    backfill_parser = subparsers.add_parser(
        "backfill",
        help="Run repeated scrape/train rounds until the curated dataset reaches a target size.",
    )
    backfill_parser.add_argument("--target-curated-rows", type=int, default=150)
    backfill_parser.add_argument("--max-rounds", type=int, default=6)
    backfill_parser.add_argument("--max-listings-per-source", type=int, default=80)

    status_parser = subparsers.add_parser("status", help="Show current dataset and model status.")

    run_all_parser = subparsers.add_parser("run-all", help="Run scrape, curate, and train.")
    run_all_parser.add_argument("--max-listings-per-source", type=int, default=80)

    args = parser.parse_args()
    if args.command == "smoke-test":
        return run_smoke_test(args.sample_size)
    if args.command == "probe-sources":
        return run_source_probe(args.sample_size)
    if args.command == "scrape":
        return run_scrape(args.max_listings_per_source)
    if args.command == "train":
        return run_train()
    if args.command == "refresh-outputs":
        return run_refresh_outputs()
    if args.command == "backfill":
        return run_backfill(
            target_curated_rows=args.target_curated_rows,
            max_rounds=args.max_rounds,
            max_listings_per_source=args.max_listings_per_source,
        )
    if args.command == "status":
        return run_status()
    if args.command == "run-all":
        return run_full_pipeline(args.max_listings_per_source)
    raise ValueError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
