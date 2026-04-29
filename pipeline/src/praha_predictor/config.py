from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]


def _env_flag(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _path_from_env(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    return Path(value).expanduser()


def _default_runtime_root() -> Path:
    explicit = _path_from_env("HOUSESPREDICT_RUNTIME_DIR")
    if explicit is not None:
        return explicit
    if _env_flag("HOUSESPREDICT_USE_REPO_RUNTIME"):
        return REPO_ROOT
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "HousesPredict-v2"
    return REPO_ROOT


RUNTIME_ROOT = _default_runtime_root()
DATA_DIR = _path_from_env("HOUSESPREDICT_DATA_DIR") or (RUNTIME_ROOT / "data")
RAW_DIR = DATA_DIR / "raw"
NORMALIZED_DIR = DATA_DIR / "normalized"
REPORTS_DIR = DATA_DIR / "reports"
INDEX_DIR = DATA_DIR / "index"
TRAINING_REGISTRY_PATH = INDEX_DIR / "training-listing-registry.parquet"
WAREHOUSE_PATH = DATA_DIR / "warehouse.duckdb"
ARTIFACTS_DIR = _path_from_env("HOUSESPREDICT_ARTIFACTS_DIR") or (RUNTIME_ROOT / "artifacts")
WORKER_MODEL_DIR = REPO_ROOT / "worker-app" / "public" / "models"
WORKER_MANIFEST_DIR = REPO_ROOT / "worker-app" / "public" / "manifests"

PRAGUE_CENTER = (50.0755, 14.4378)


@dataclass(frozen=True)
class PipelineConfig:
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/134.0.0.0 Safari/537.36 HousesPredict-v2/0.1"
    )
    request_timeout_seconds: int = 10
    max_listings_default: int = 100
    live_delay_seconds: float = 0.1
    source_name: str = "bezrealitky"
    request_retries: int = 3
    retry_backoff_seconds: float = 0.75
    listing_processing_timeout_seconds: int = 30
    frontier_top_up_factor: float = 2.0
    promotion_min_new_rows: int = 25
    promotion_min_mae_improvement: float = 0.05
    promotion_large_growth_rows: int = 150
    bootstrap_replacement_min_active_rows: int = 25
    bootstrap_replacement_min_candidate_rows: int = 100
    source_min_fetch_success_rate: float = 0.9
    source_min_parse_success_rate: float = 0.5
    metro_region_radius_km: float = 40.0
    backfill_target_curated_rows: int = 1000
    backfill_max_rounds: int = 10
