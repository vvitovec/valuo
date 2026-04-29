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
