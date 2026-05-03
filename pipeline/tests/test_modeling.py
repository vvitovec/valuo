import json
from pathlib import Path

from praha_predictor import modeling
from praha_predictor.config import PipelineConfig
from praha_predictor.modeling import _decide_promotion


def test_bootstrap_model_is_replaced_once_candidate_dataset_is_production_ready() -> None:
    promoted, reason = _decide_promotion(
        active_version="model-bootstrap",
        active_entry={"promotionReason": "bootstrap_existing_active_model"},
        active_row_count=1,
        curated_row_count=149,
        new_curated_rows=148,
        candidate_selected_mae=1_500_000.0,
        mae_improvement=-2.35,
        config=PipelineConfig(),
    )

    assert promoted is True
    assert reason == "bootstrap_replacement_after_dataset_growth"


def test_regular_gate_still_holds_for_non_bootstrap_active_model() -> None:
    promoted, reason = _decide_promotion(
        active_version="model-stable",
        active_entry={"promotionReason": "promoted_after_growth_and_mae_gate"},
        active_row_count=120,
        curated_row_count=149,
        new_curated_rows=29,
        candidate_selected_mae=1_500_000.0,
        mae_improvement=0.02,
        config=PipelineConfig(),
    )

    assert promoted is False
    assert reason == "candidate_held_back:new_rows=29,mae_improvement=0.0200"


def test_large_growth_with_positive_mae_delta_promotes_even_below_strict_threshold() -> None:
    promoted, reason = _decide_promotion(
        active_version="model-stable",
        active_entry={"promotionReason": "promoted_after_growth_and_mae_gate"},
        active_row_count=1011,
        curated_row_count=1293,
        new_curated_rows=282,
        candidate_selected_mae=1_995_288.0,
        mae_improvement=0.0143,
        config=PipelineConfig(),
    )

    assert promoted is True
    assert reason == "promoted_after_large_growth_and_positive_mae_delta"


def test_runtime_model_artifacts_bootstrap_from_worker_assets(tmp_path: Path, monkeypatch) -> None:
    artifacts_dir = tmp_path / "artifacts"
    worker_model_dir = tmp_path / "worker-models"
    worker_manifest_dir = tmp_path / "worker-manifests"
    worker_model_dir.mkdir()
    worker_manifest_dir.mkdir()

    worker_active_model = worker_model_dir / "active-model.json"
    worker_registry = worker_manifest_dir / "model-registry.json"
    worker_active_model.write_text(json.dumps({"version": "model-fixture"}), encoding="utf-8")
    worker_registry.write_text(json.dumps({"activeModelVersion": "model-fixture"}), encoding="utf-8")

    monkeypatch.setattr(modeling, "ARTIFACTS_DIR", artifacts_dir)
    monkeypatch.setattr(modeling, "ACTIVE_MODEL_PATH", artifacts_dir / "active-model.json")
    monkeypatch.setattr(modeling, "MODEL_REGISTRY_PATH", artifacts_dir / "model-registry.json")
    monkeypatch.setattr(modeling, "WORKER_ACTIVE_MODEL_PATH", worker_active_model)
    monkeypatch.setattr(modeling, "WORKER_REGISTRY_PATH", worker_registry)

    modeling.ensure_runtime_model_artifacts()

    assert json.loads((artifacts_dir / "active-model.json").read_text(encoding="utf-8")) == {
        "version": "model-fixture"
    }
    assert json.loads((artifacts_dir / "model-registry.json").read_text(encoding="utf-8")) == {
        "activeModelVersion": "model-fixture"
    }
