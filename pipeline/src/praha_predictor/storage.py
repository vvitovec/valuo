from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pandas as pd

from praha_predictor.config import (
    ARTIFACTS_DIR,
    INDEX_DIR,
    NORMALIZED_DIR,
    RAW_DIR,
    REPORTS_DIR,
    WAREHOUSE_PATH,
)
from praha_predictor.schemas import RawSnapshot


TABLE_EXPORTS = {
    "source_runs": INDEX_DIR / "source-runs.parquet",
    "listing_frontier": INDEX_DIR / "listing-frontier.parquet",
    "listing_current_index": INDEX_DIR / "listing-current-index.parquet",
    "source_health_reports": INDEX_DIR / "source-health-reports.parquet",
}


def ensure_directories() -> None:
    for directory in (RAW_DIR, NORMALIZED_DIR, REPORTS_DIR, ARTIFACTS_DIR, INDEX_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def get_connection() -> duckdb.DuckDBPyConnection:
    ensure_directories()
    connection = duckdb.connect(str(WAREHOUSE_PATH))
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS source_runs (
          run_id TEXT,
          source TEXT,
          started_at TEXT,
          finished_at TEXT,
          discovered_count INTEGER DEFAULT 0,
          processed_count INTEGER DEFAULT 0,
          normalized_count INTEGER DEFAULT 0,
          rejected_count INTEGER DEFAULT 0,
          fetch_success_count INTEGER DEFAULT 0,
          fetch_failure_count INTEGER DEFAULT 0,
          parse_failure_count INTEGER DEFAULT 0,
          median_latency_ms DOUBLE,
          health_json TEXT,
          PRIMARY KEY (run_id, source)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS listing_frontier (
          source TEXT,
          listing_url TEXT,
          source_listing_id TEXT,
          discovered_at TEXT,
          last_seen_at TEXT,
          discovery_method TEXT,
          status TEXT,
          attempts INTEGER DEFAULT 0,
          last_fetched_at TEXT,
          last_error TEXT,
          active BOOLEAN DEFAULT TRUE,
          PRIMARY KEY (source, listing_url)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS listing_current_index (
          source TEXT,
          source_listing_id TEXT,
          listing_url TEXT,
          observed_at TEXT,
          content_hash TEXT,
          current_state TEXT,
          current_kind TEXT,
          PRIMARY KEY (source, source_listing_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS source_health_reports (
          run_id TEXT,
          source TEXT,
          created_at TEXT,
          report_json TEXT
        )
        """
    )
    return connection


def export_operational_tables() -> None:
    connection = get_connection()
    try:
        for table_name, output_path in TABLE_EXPORTS.items():
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_sql = str(output_path).replace("'", "''")
            connection.execute(
                f"COPY (SELECT * FROM {table_name}) TO '{output_sql}' (FORMAT PARQUET)"
            )
    finally:
        connection.close()


def write_raw_snapshot(raw_snapshot: RawSnapshot) -> Path:
    ensure_directories()
    observed_slug = raw_snapshot.observed_at.replace(":", "-")
    output_dir = RAW_DIR / raw_snapshot.source / raw_snapshot.source_listing_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{observed_slug}--{raw_snapshot.content_hash}.json"
    if not output_path.exists():
        output_path.write_text(
            json.dumps(raw_snapshot.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return output_path


def save_rows_to_parquet(rows: list[dict[str, Any]], output_path: Path) -> Path | None:
    ensure_directories()
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    for column in frame.columns:
        if frame[column].map(lambda value: isinstance(value, dict)).any():
            frame[column] = frame[column].map(
                lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, dict)
                else value
            )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(output_path, index=False)
    return output_path


def save_report(report: dict[str, Any], output_path: Path) -> Path:
    ensure_directories()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def start_source_run(run_id: str, source: str, started_at: str) -> None:
    connection = get_connection()
    try:
        connection.execute(
            """
            INSERT OR REPLACE INTO source_runs (
              run_id, source, started_at, finished_at, discovered_count, processed_count,
              normalized_count, rejected_count, fetch_success_count, fetch_failure_count,
              parse_failure_count, median_latency_ms, health_json
            ) VALUES (?, ?, ?, NULL, 0, 0, 0, 0, 0, 0, 0, NULL, NULL)
            """,
            [run_id, source, started_at],
        )
    finally:
        connection.close()


def finish_source_run(run_id: str, source: str, health_report: dict[str, Any]) -> None:
    connection = get_connection()
    try:
        connection.execute(
            """
            INSERT INTO source_health_reports (run_id, source, created_at, report_json)
            VALUES (?, ?, ?, ?)
            """,
            [
                run_id,
                source,
                health_report["generated_at"],
                json.dumps(health_report, ensure_ascii=False),
            ],
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO source_runs (
              run_id, source, started_at, finished_at, discovered_count, processed_count,
              normalized_count, rejected_count, fetch_success_count, fetch_failure_count,
              parse_failure_count, median_latency_ms, health_json
            )
            SELECT
              existing.run_id,
              existing.source,
              existing.started_at,
              ?,
              ?,
              ?,
              ?,
              ?,
              ?,
              ?,
              ?,
              ?,
              ?
            FROM source_runs existing
            WHERE existing.run_id = ? AND existing.source = ?
            """,
            [
                health_report["generated_at"],
                health_report.get("discovered_count", 0),
                health_report.get("processed_count", 0),
                health_report.get("normalized_count", 0),
                health_report.get("rejected_count", 0),
                health_report.get("fetch_success_count", 0),
                health_report.get("fetch_failure_count", 0),
                health_report.get("parse_failure_count", 0),
                health_report.get("median_latency_ms"),
                json.dumps(health_report, ensure_ascii=False),
                run_id,
                source,
            ],
        )
        export_operational_tables()
    finally:
        connection.close()


def upsert_frontier_urls(
    source: str,
    urls: Iterable[str],
    *,
    discovered_at: str,
    discovery_method: str,
) -> None:
    unique_urls = list(dict.fromkeys(urls))
    if not unique_urls:
        return
    connection = get_connection()
    try:
        for url in unique_urls:
            connection.execute(
                """
                INSERT OR REPLACE INTO listing_frontier (
                  source, listing_url, source_listing_id, discovered_at, last_seen_at,
                  discovery_method, status, attempts, last_fetched_at, last_error, active
                )
                VALUES (
                  ?, ?,
                  COALESCE(
                    (SELECT source_listing_id FROM listing_frontier WHERE source = ? AND listing_url = ?),
                    NULL
                  ),
                  COALESCE(
                    (SELECT discovered_at FROM listing_frontier WHERE source = ? AND listing_url = ?),
                    ?
                  ),
                  ?,
                  ?,
                  COALESCE(
                    (SELECT status FROM listing_frontier WHERE source = ? AND listing_url = ?),
                    'discovered'
                  ),
                  COALESCE(
                    (SELECT attempts FROM listing_frontier WHERE source = ? AND listing_url = ?),
                    0
                  ),
                  COALESCE(
                    (SELECT last_fetched_at FROM listing_frontier WHERE source = ? AND listing_url = ?),
                    NULL
                  ),
                  COALESCE(
                    (SELECT last_error FROM listing_frontier WHERE source = ? AND listing_url = ?),
                    NULL
                  ),
                  TRUE
                )
                """,
                [
                    source,
                    url,
                    source,
                    url,
                    source,
                    url,
                    discovered_at,
                    discovered_at,
                    discovery_method,
                    source,
                    url,
                    source,
                    url,
                    source,
                    url,
                    source,
                    url,
                ],
            )
        export_operational_tables()
    finally:
        connection.close()


def get_frontier_candidate_urls(source: str, limit: int) -> list[str]:
    connection = get_connection()
    try:
        rows = connection.execute(
            """
            SELECT listing_url
            FROM listing_frontier
            WHERE source = ? AND active = TRUE
            ORDER BY
              CASE status
                WHEN 'discovered' THEN 0
                WHEN 'fetch_failed' THEN 1
                WHEN 'rejected' THEN 2
                ELSE 3
              END,
              COALESCE(last_fetched_at, '1970-01-01T00:00:00+00:00') ASC,
              attempts ASC,
              last_seen_at DESC
            LIMIT ?
            """,
            [source, limit],
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        connection.close()


def get_frontier_stats(source: str) -> dict[str, int]:
    connection = get_connection()
    try:
        active_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM listing_frontier
            WHERE source = ? AND active = TRUE
            """,
            [source],
        ).fetchone()[0]
        unfetched_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM listing_frontier
            WHERE source = ? AND active = TRUE AND attempts = 0
            """,
            [source],
        ).fetchone()[0]
        return {
            "active_count": int(active_count or 0),
            "unfetched_count": int(unfetched_count or 0),
        }
    finally:
        connection.close()


def mark_frontier_result(
    source: str,
    listing_url: str,
    *,
    source_listing_id: str,
    observed_at: str,
    success: bool,
    state: str,
    error: str | None = None,
) -> None:
    connection = get_connection()
    try:
        connection.execute(
            """
            INSERT OR REPLACE INTO listing_frontier (
              source, listing_url, source_listing_id, discovered_at, last_seen_at,
              discovery_method, status, attempts, last_fetched_at, last_error, active
            )
            VALUES (
              ?, ?,
              ?,
              COALESCE(
                (SELECT discovered_at FROM listing_frontier WHERE source = ? AND listing_url = ?),
                ?
              ),
              ?,
              COALESCE(
                (SELECT discovery_method FROM listing_frontier WHERE source = ? AND listing_url = ?),
                'frontier'
              ),
              ?,
              COALESCE(
                (SELECT attempts FROM listing_frontier WHERE source = ? AND listing_url = ?),
                0
              ) + 1,
              ?,
              ?,
              ?
            )
            """,
            [
                source,
                listing_url,
                source_listing_id,
                source,
                listing_url,
                observed_at,
                observed_at,
                source,
                listing_url,
                state,
                source,
                listing_url,
                observed_at,
                error,
                True if success else True,
            ],
        )
        export_operational_tables()
    finally:
        connection.close()


def update_listing_current_index(
    source: str,
    source_listing_id: str,
    *,
    listing_url: str,
    observed_at: str,
    content_hash: str,
    current_state: str,
    current_kind: str,
) -> None:
    connection = get_connection()
    try:
        connection.execute(
            """
            INSERT OR REPLACE INTO listing_current_index (
              source, source_listing_id, listing_url, observed_at, content_hash, current_state, current_kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                source,
                source_listing_id,
                listing_url,
                observed_at,
                content_hash,
                current_state,
                current_kind,
            ],
        )
        export_operational_tables()
    finally:
        connection.close()


def rebuild_current_views() -> dict[str, Path | None]:
    ensure_directories()
    current_normalized = NORMALIZED_DIR / "current-normalized.parquet"
    current_rejects = NORMALIZED_DIR / "current-rejects.parquet"
    outputs: dict[str, Path | None] = {"normalized": None, "rejects": None}

    normalized_paths = sorted(NORMALIZED_DIR.glob("normalized-run-*.parquet"))
    if normalized_paths:
        normalized_frames = [pd.read_parquet(path) for path in normalized_paths]
        normalized_frame = pd.concat(normalized_frames, ignore_index=True, sort=False)
        normalized_frame = normalized_frame.sort_values(
            ["source", "source_listing_id", "observed_at", "content_hash"],
            ascending=[True, True, False, False],
        )
        normalized_frame = normalized_frame.drop_duplicates(
            subset=["source", "source_listing_id"],
            keep="first",
        )
        normalized_frame.to_parquet(current_normalized, index=False)
        outputs["normalized"] = current_normalized

    reject_paths = sorted(NORMALIZED_DIR.glob("rejects-run-*.parquet"))
    if reject_paths:
        reject_frames = [pd.read_parquet(path) for path in reject_paths]
        reject_frame = pd.concat(reject_frames, ignore_index=True, sort=False)
        for column in reject_frame.columns:
            if reject_frame[column].map(lambda value: isinstance(value, dict)).any():
                reject_frame[column] = reject_frame[column].map(
                    lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if isinstance(value, dict)
                    else value
                )
        reject_frame = reject_frame.sort_values(
            ["source", "source_listing_id", "observed_at", "content_hash"],
            ascending=[True, True, False, False],
        )
        reject_frame = reject_frame.drop_duplicates(
            subset=["source", "source_listing_id"],
            keep="first",
        )
        reject_frame.to_parquet(current_rejects, index=False)
        outputs["rejects"] = current_rejects
    return outputs


def latest_source_health_reports() -> dict[str, dict[str, Any]]:
    connection = get_connection()
    try:
        rows = connection.execute(
            """
            WITH ranked AS (
              SELECT *,
                     ROW_NUMBER() OVER (
                       PARTITION BY source
                       ORDER BY created_at DESC
                     ) AS row_num
              FROM source_health_reports
            )
            SELECT source, report_json
            FROM ranked
            WHERE row_num = 1
            """
        ).fetchall()
        return {row[0]: json.loads(row[1]) for row in rows}
    finally:
        connection.close()
