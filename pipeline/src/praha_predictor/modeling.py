from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from interpret.glassbox._ebm._ebm import ExplainableBoostingRegressor
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import GroupKFold, KFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OrdinalEncoder

from praha_predictor.config import ARTIFACTS_DIR, PipelineConfig, WORKER_MANIFEST_DIR, WORKER_MODEL_DIR
from praha_predictor.features import (
    CORE_FEATURE_COLUMNS,
    EXTENDED_FEATURE_COLUMNS,
    FEATURE_COLUMNS,
    FEATURE_TYPES,
    SEGMENTED_FEATURE_COLUMNS,
    build_baseline_lookup,
    build_model_frame,
)
from praha_predictor.signals import ComparableLookup, build_comparable_lookup, comparable_lookup_from_dict, derive_comparable_features
from praha_predictor.signals import apply_comparable_features


MODEL_REGISTRY_PATH = ARTIFACTS_DIR / "model-registry.json"
ACTIVE_MODEL_PATH = ARTIFACTS_DIR / "active-model.json"
ACTIVE_PARITY_PATH = ARTIFACTS_DIR / "scorer-parity-fixtures.json"
WORKER_ACTIVE_MODEL_PATH = WORKER_MODEL_DIR / "active-model.json"
WORKER_REGISTRY_PATH = WORKER_MANIFEST_DIR / "model-registry.json"
SEGMENT_MIN_ROWS = 45
TIME_HOLDOUT_FRACTION = 0.2
FINAL_SCORE_TIME_WEIGHT = 0.6
FINAL_SCORE_BALANCED_WEIGHT = 0.4
FINAL_SCORE_PROMOTION_THRESHOLD = 0.0025
SEGMENT_REGRESSION_THRESHOLD = 0.0075
HOUSE_REGRESSION_THRESHOLD = 0.01
CHALLENGER_READINESS_THRESHOLD = 0.005
PRICE_EBM_PARAMS = {
    "outer_bags": 8,
    "learning_rate": 0.02,
    "max_rounds": 256,
    "min_samples_leaf": 4,
    "max_leaves": 3,
    "smoothing_rounds": 300,
}
CORE_EBM_PARAMS = PRICE_EBM_PARAMS
PPM_EBM_PARAMS = PRICE_EBM_PARAMS
INTERACTION_EBM_PARAMS = {
    **PRICE_EBM_PARAMS,
    "interactions": 12,
}
BLEND_PPM_WEIGHT = 0.65
BLEND_PRICE_WEIGHT = 0.35
EXPORTABLE_CANDIDATES = (
    "baseline_ppm",
    "ebm_core",
    "ebm_regressor",
    "ebm_ppm_regressor",
    "blended_ebm_regressor",
    "segmented_ebm_regressor",
)
CHALLENGER_CANDIDATES = (
    "ebm_interactions_challenger",
    "hist_gradient_boosting_challenger",
)


def _mape(actual: np.ndarray, predicted: np.ndarray) -> float:
    safe_actual = np.maximum(np.abs(actual), 1.0)
    return float(np.mean(np.abs(actual - predicted) / safe_actual))


def _lookup_baseline_ppm(
    lookup: dict[str, float],
    location_cluster: str,
    property_type: str,
) -> float:
    return (
        lookup.get(f"{location_cluster}|{property_type}")
        or lookup.get(f"fallback|{property_type}")
        or lookup["fallback|all"]
    )


def _predict_baseline_from_features(features: pd.DataFrame, lookup: dict[str, float]) -> np.ndarray:
    prices = []
    for record in features.to_dict(orient="records"):
        ppm = _lookup_baseline_ppm(
            lookup,
            str(record["location_cluster"]),
            str(record["property_type"]),
        )
        prices.append(float(record["floor_area_m2"]) * ppm)
    return np.asarray(prices, dtype=float)


def _make_splitter(groups: pd.Series) -> Any:
    unique_groups = groups.nunique()
    if unique_groups >= 3:
        return GroupKFold(n_splits=min(5, unique_groups))
    return KFold(n_splits=3, shuffle=True, random_state=42)


def _feature_types_for(feature_columns: list[str]) -> list[str]:
    return [FEATURE_TYPES[column] for column in feature_columns]


def _fit_ebm(
    x_frame: pd.DataFrame,
    y_series: pd.Series,
    feature_columns: list[str],
    *,
    model_params: dict[str, Any] | None = None,
) -> ExplainableBoostingRegressor:
    params = {
        "interactions": 0,
        "outer_bags": 6,
        "inner_bags": 0,
        "learning_rate": 0.025,
        "max_rounds": 196,
        "min_samples_leaf": 3,
        "max_leaves": 3,
        "random_state": 42,
    }
    if model_params:
        params.update(model_params)
    model = ExplainableBoostingRegressor(
        feature_names=feature_columns,
        feature_types=_feature_types_for(feature_columns),
        **params,
    )
    model.fit(x_frame[feature_columns], np.log(y_series.to_numpy(dtype=float)))
    return model


def _fit_hist_gradient_boosting(
    x_frame: pd.DataFrame,
    y_series: pd.Series,
    feature_columns: list[str],
) -> Pipeline:
    categorical_columns = [column for column in feature_columns if FEATURE_TYPES[column] == "nominal"]
    continuous_columns = [column for column in feature_columns if FEATURE_TYPES[column] == "continuous"]
    preprocessor = ColumnTransformer(
        [
            (
                "categorical",
                Pipeline(
                    [
                        ("impute", SimpleImputer(strategy="most_frequent")),
                        (
                            "encode",
                            OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1),
                        ),
                    ]
                ),
                categorical_columns,
            ),
            (
                "continuous",
                Pipeline([("impute", SimpleImputer(strategy="median"))]),
                continuous_columns,
            ),
        ],
        sparse_threshold=0,
    )
    model = Pipeline(
        [
            ("preprocessor", preprocessor),
            (
                "regressor",
                HistGradientBoostingRegressor(
                    loss="squared_error",
                    learning_rate=0.04,
                    max_iter=500,
                    max_leaf_nodes=31,
                    min_samples_leaf=10,
                    l2_regularization=0.1,
                    random_state=42,
                ),
            ),
        ]
    )
    model.fit(x_frame[feature_columns], np.log(y_series.to_numpy(dtype=float)))
    return model


def _continuous_term_from_model(cuts: list[float], scores: list[float]) -> dict[str, Any]:
    return {
        "missingScore": float(scores[0]),
        "unknownScore": float(scores[-1]),
        "bins": [
            {"upperBound": float(upper_bound), "score": float(score)}
            for upper_bound, score in zip(cuts, scores[1:-2], strict=False)
        ]
        + [{"upperBound": None, "score": float(scores[-2])}],
    }


def _categorical_term_from_model(bin_mapping: dict[str, int], scores: list[float]) -> dict[str, Any]:
    category_scores = {str(category): float(scores[index]) for category, index in bin_mapping.items()}
    return {
        "missingScore": float(scores[0]),
        "unknownScore": float(scores[-1]),
        "categories": category_scores,
    }


def export_ebm_terms_model(
    model: ExplainableBoostingRegressor,
    feature_columns: list[str],
    *,
    target_scale: str = "price",
) -> dict[str, Any]:
    terms = []
    for feature_index, feature_name in enumerate(feature_columns):
        term_index = next(
            index
            for index, term_features in enumerate(model.term_features_)
            if term_features == (feature_index,)
        )
        scores = model.term_scores_[term_index].tolist()
        bins = model.bins_[feature_index][0]
        feature_type = FEATURE_TYPES[feature_name]
        if feature_type == "continuous":
            term_payload = _continuous_term_from_model(list(bins), scores)
        else:
            term_payload = _categorical_term_from_model(dict(bins), scores)
        terms.append(
            {
                "featureName": feature_name,
                "featureType": feature_type,
                **term_payload,
            }
        )
    return {
        "kind": "ebm_regressor",
        "targetScale": target_scale,
        "intercept": float(model.intercept_),
        "terms": terms,
        "featureOrder": feature_columns,
    }


def export_ebm_model(
    model: ExplainableBoostingRegressor,
    baseline_lookup: dict[str, float],
    residual_quantiles: dict[str, float],
    metadata: dict[str, Any],
    feature_columns: list[str],
) -> dict[str, Any]:
    return {
        **export_ebm_terms_model(model, feature_columns),
        "baselineLookup": baseline_lookup,
        "baselineLocationFeature": "location_cluster",
        "residualQuantiles": residual_quantiles,
        **metadata,
    }


def export_baseline_model(
    baseline_lookup: dict[str, float],
    residual_quantiles: dict[str, float],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": "baseline_ppm",
        "baselineLookup": baseline_lookup,
        "baselineLocationFeature": "location_cluster",
        "residualQuantiles": residual_quantiles,
        "featureOrder": FEATURE_COLUMNS,
        **metadata,
    }


def export_segmented_ebm_model(
    *,
    fallback_model: ExplainableBoostingRegressor,
    segment_models: dict[str, ExplainableBoostingRegressor],
    baseline_lookup: dict[str, float],
    residual_quantiles: dict[str, float],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": "segmented_ebm_regressor",
        "segmentKeyFeature": "model_segment",
        "baselineLookup": baseline_lookup,
        "baselineLocationFeature": "location_cluster",
        "residualQuantiles": residual_quantiles,
        "featureOrder": EXTENDED_FEATURE_COLUMNS,
        "fallbackModel": export_ebm_terms_model(fallback_model, SEGMENTED_FEATURE_COLUMNS),
        "segmentModels": {
            segment_key: export_ebm_terms_model(model, SEGMENTED_FEATURE_COLUMNS)
            for segment_key, model in segment_models.items()
        },
        "segmentCoverage": {segment_key: int(1) for segment_key in segment_models},
        **metadata,
    }


def export_ppm_ebm_model(
    model: ExplainableBoostingRegressor,
    baseline_lookup: dict[str, float],
    residual_quantiles: dict[str, float],
    metadata: dict[str, Any],
    feature_columns: list[str],
) -> dict[str, Any]:
    return {
        **export_ebm_terms_model(model, feature_columns, target_scale="price_per_m2"),
        "baselineLookup": baseline_lookup,
        "baselineLocationFeature": "location_cluster",
        "residualQuantiles": residual_quantiles,
        **metadata,
    }


def export_blended_ebm_model(
    *,
    price_model: ExplainableBoostingRegressor,
    ppm_model: ExplainableBoostingRegressor,
    baseline_lookup: dict[str, float],
    residual_quantiles: dict[str, float],
    metadata: dict[str, Any],
    feature_columns: list[str],
) -> dict[str, Any]:
    return {
        "kind": "blended_ebm_regressor",
        "blendWeights": {
            "price": BLEND_PRICE_WEIGHT,
            "pricePerM2": BLEND_PPM_WEIGHT,
        },
        "priceModel": export_ebm_terms_model(price_model, feature_columns, target_scale="price"),
        "ppmModel": export_ebm_terms_model(ppm_model, feature_columns, target_scale="price_per_m2"),
        "baselineLookup": baseline_lookup,
        "baselineLocationFeature": "location_cluster",
        "residualQuantiles": residual_quantiles,
        "featureOrder": feature_columns,
        **metadata,
    }


def _augment_payload_for_model(exported_model: dict[str, Any], feature_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(feature_payload)
    comparable_lookup_payload = exported_model.get("comparableLookup")
    if comparable_lookup_payload:
        lookup = comparable_lookup_from_dict(comparable_lookup_payload)
        payload.update(derive_comparable_features(payload, lookup))
    return payload


def _score_ebm_terms_value(exported_model: dict[str, Any], feature_payload: dict[str, Any]) -> dict[str, Any]:
    payload = _augment_payload_for_model(exported_model, feature_payload)
    total = float(exported_model["intercept"])
    contributions = []
    for term in exported_model["terms"]:
        feature_name = term["featureName"]
        raw_value = payload.get(feature_name)
        if raw_value in (None, "", "unknown"):
            contribution = float(term["missingScore"])
        elif term["featureType"] == "continuous":
            numeric_value = float(raw_value)
            contribution = float(term["unknownScore"])
            for bin_info in term["bins"]:
                upper_bound = bin_info["upperBound"]
                if upper_bound is None or numeric_value <= float(upper_bound):
                    contribution = float(bin_info["score"])
                    break
        else:
            contribution = float(term["categories"].get(str(raw_value), term["unknownScore"]))
        total += contribution
        contributions.append({"featureName": feature_name, "score": contribution})
    return {
        "predicted_value": float(np.exp(total)),
        "contributions": contributions,
        "raw_total_log_target": total,
    }


def _score_ebm_terms_model(exported_model: dict[str, Any], feature_payload: dict[str, Any]) -> dict[str, Any]:
    scored = _score_ebm_terms_value(exported_model, feature_payload)
    return {
        "estimated_price_czk": scored["predicted_value"],
        "contributions": scored["contributions"],
        "raw_total_log_price": scored["raw_total_log_target"],
    }


def _contribution_impacts_from_terms(
    scored_terms: dict[str, Any],
    *,
    area_multiplier: float = 1.0,
) -> list[dict[str, Any]]:
    impacts = []
    for contribution in scored_terms["contributions"]:
        without_term = float(np.exp(scored_terms["raw_total_log_target"] - contribution["score"]))
        impact = (float(scored_terms["predicted_value"]) - without_term) * area_multiplier
        impacts.append({"featureName": contribution["featureName"], "score": impact})
    return impacts


def _merge_weighted_impacts(
    price_impacts: list[dict[str, Any]],
    ppm_impacts: list[dict[str, Any]],
    *,
    price_weight: float,
    ppm_weight: float,
) -> list[dict[str, Any]]:
    merged: dict[str, float] = {}
    feature_order: list[str] = []
    for impact in price_impacts:
        feature_name = str(impact["featureName"])
        if feature_name not in merged:
            feature_order.append(feature_name)
            merged[feature_name] = 0.0
        merged[feature_name] += price_weight * float(impact["score"])
    for impact in ppm_impacts:
        feature_name = str(impact["featureName"])
        if feature_name not in merged:
            feature_order.append(feature_name)
            merged[feature_name] = 0.0
        merged[feature_name] += ppm_weight * float(impact["score"])
    return [{"featureName": feature_name, "score": merged[feature_name]} for feature_name in feature_order]


def score_exported_model(exported_model: dict[str, Any], feature_payload: dict[str, Any]) -> dict[str, Any]:
    payload = _augment_payload_for_model(exported_model, feature_payload)
    if exported_model["kind"] == "baseline_ppm":
        baseline_lookup = exported_model["baselineLookup"]
        location_field = exported_model.get("baselineLocationFeature", "district_prague")
        ppm = _lookup_baseline_ppm(
            baseline_lookup,
            str(payload[location_field]),
            str(payload["property_type"]),
        )
        estimated = float(payload["floor_area_m2"]) * ppm
        contributions = [
            {"featureName": location_field, "score": float(ppm), "mode": "price_per_m2"},
            {
                "featureName": "floor_area_m2",
                "score": float(payload["floor_area_m2"]),
                "mode": "area_multiplier",
            },
        ]
        return {"estimated_price_czk": estimated, "contributions": contributions}
    if exported_model["kind"] == "ebm_ppm_regressor":
        scored_terms = _score_ebm_terms_value(exported_model, payload)
        area_multiplier = float(payload["floor_area_m2"])
        return {
            "estimated_price_czk": float(scored_terms["predicted_value"]) * area_multiplier,
            "contributions": _contribution_impacts_from_terms(
                scored_terms,
                area_multiplier=area_multiplier,
            ),
        }
    if exported_model["kind"] == "blended_ebm_regressor":
        price_terms = _score_ebm_terms_value(exported_model["priceModel"], payload)
        ppm_terms = _score_ebm_terms_value(exported_model["ppmModel"], payload)
        area_multiplier = float(payload["floor_area_m2"])
        price_estimate = float(price_terms["predicted_value"])
        ppm_estimate = float(ppm_terms["predicted_value"]) * area_multiplier
        price_weight = float(exported_model["blendWeights"]["price"])
        ppm_weight = float(exported_model["blendWeights"]["pricePerM2"])
        return {
            "estimated_price_czk": price_weight * price_estimate + ppm_weight * ppm_estimate,
            "contributions": _merge_weighted_impacts(
                _contribution_impacts_from_terms(price_terms),
                _contribution_impacts_from_terms(ppm_terms, area_multiplier=area_multiplier),
                price_weight=price_weight,
                ppm_weight=ppm_weight,
            ),
        }
    if exported_model["kind"] == "segmented_ebm_regressor":
        segment_key = str(payload.get(exported_model["segmentKeyFeature"], ""))
        selected_model = exported_model["segmentModels"].get(segment_key) or exported_model["fallbackModel"]
        scored = _score_ebm_terms_model(selected_model, payload)
        scored["selected_segment"] = segment_key if segment_key in exported_model["segmentModels"] else "fallback"
        return scored
    return _score_ebm_terms_model(exported_model, payload)


def _residual_quantiles(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    residuals = np.log(np.maximum(actual, 1.0)) - np.log(np.maximum(predicted, 1.0))
    return {
        "low": float(np.quantile(residuals, 0.1)),
        "high": float(np.quantile(residuals, 0.9)),
    }


def _make_parity_fixtures(exported_model: dict[str, Any], x_frame: pd.DataFrame) -> list[dict[str, Any]]:
    fixtures = []
    for record in x_frame.head(12).to_dict(orient="records"):
        scored = score_exported_model(exported_model, record)
        fixtures.append(
            {
                "input": record,
                "expectedPriceCzk": scored["estimated_price_czk"],
                "expectedContributionScores": scored["contributions"],
            }
        )
    return fixtures


def _training_window(curated_frame: pd.DataFrame) -> dict[str, str]:
    observed = pd.to_datetime(curated_frame["observed_at"], utc=True)
    return {
        "from": observed.min().isoformat(),
        "to": observed.max().isoformat(),
    }


def _source_mix(curated_frame: pd.DataFrame) -> dict[str, int]:
    return curated_frame.groupby("source").size().sort_values(ascending=False).astype(int).to_dict()


def _selected_mae_from_metrics(metrics: dict[str, Any]) -> float:
    selected_model = metrics.get("selectedModel")
    return float(metrics.get(f"{selected_model}_mae", metrics.get("baseline_ppm_mae", float("inf"))))


def _selected_final_score(validation_summary: dict[str, Any] | None) -> float:
    if not validation_summary:
        return float("inf")
    return float(validation_summary.get("finalScore", float("inf")))


def _decide_promotion(
    *,
    active_version: str | None,
    active_entry: dict[str, Any] | None,
    active_row_count: int,
    curated_row_count: int,
    new_curated_rows: int,
    candidate_selected_mae: float,
    mae_improvement: float,
    config: PipelineConfig,
    final_score_improvement: float | None = None,
    segment_guardrail_passed: bool = True,
    house_guardrail_passed: bool = True,
) -> tuple[bool, str]:
    if active_version is None:
        return True, "initial_bootstrap_promotion"

    active_promotion_reason = str((active_entry or {}).get("promotionReason") or "")
    active_is_bootstrap = (
        active_row_count < config.bootstrap_replacement_min_active_rows
        or active_promotion_reason == "bootstrap_existing_active_model"
    )
    candidate_is_production_ready = (
        curated_row_count >= config.bootstrap_replacement_min_candidate_rows
        and np.isfinite(candidate_selected_mae)
    )
    if active_is_bootstrap and candidate_is_production_ready:
        return True, "bootstrap_replacement_after_dataset_growth"

    if not segment_guardrail_passed:
        return False, "candidate_held_back:segment_guardrail_failed"
    if not house_guardrail_passed:
        return False, "candidate_held_back:house_guardrail_failed"
    if final_score_improvement is not None and final_score_improvement < FINAL_SCORE_PROMOTION_THRESHOLD:
        return False, f"candidate_held_back:final_score_improvement={final_score_improvement:.4f}"
    if final_score_improvement is not None and final_score_improvement >= FINAL_SCORE_PROMOTION_THRESHOLD:
        return True, "promoted_after_dual_validation_final_score_gate"

    if new_curated_rows >= config.promotion_large_growth_rows and mae_improvement > 0:
        return True, "promoted_after_large_growth_and_positive_mae_delta"

    if (
        new_curated_rows >= config.promotion_min_new_rows
        and mae_improvement >= config.promotion_min_mae_improvement
    ):
        return True, "promoted_after_growth_and_mae_gate"

    return False, f"candidate_held_back:new_rows={new_curated_rows},mae_improvement={mae_improvement:.4f}"


def _bootstrap_registry(curated_row_count: int) -> dict[str, Any]:
    if not ACTIVE_MODEL_PATH.exists():
        return {"activeModelVersion": None, "lastPromotedAt": None, "entries": []}
    active_model = json.loads(ACTIVE_MODEL_PATH.read_text(encoding="utf-8"))
    metrics = active_model.get("metrics", {})
    entry = {
        "version": active_model["version"],
        "trainedAt": active_model.get("trainedAt"),
        "modelKind": active_model.get("kind"),
        "curatedRowCount": int(active_model.get("curatedRowCount", max(curated_row_count - 25, 0))),
        "trainingWindow": active_model.get("trainingWindow"),
        "sourceMix": active_model.get("sourceMix", {}),
        "validationSummary": active_model.get("validationSummary")
        or {
            "selectedModel": metrics.get("selectedModel", active_model.get("kind")),
            "selectedMae": _selected_mae_from_metrics(metrics) if metrics else None,
            "metrics": metrics,
            "grouping": "location_cluster_grouped_cv",
        },
        "promotionReason": active_model.get("promotionReason", "bootstrap_existing_active_model"),
        "promoted": True,
        "promotedAt": active_model.get("promotedAt", active_model.get("trainedAt")),
        "newCuratedRowsSincePreviousActive": 0,
    }
    return {
        "activeModelVersion": entry["version"],
        "lastPromotedAt": entry["promotedAt"],
        "entries": [entry],
    }


def load_model_registry(curated_row_count: int) -> dict[str, Any]:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    if MODEL_REGISTRY_PATH.exists():
        return json.loads(MODEL_REGISTRY_PATH.read_text(encoding="utf-8"))
    registry = _bootstrap_registry(curated_row_count)
    MODEL_REGISTRY_PATH.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    return registry


def _write_registry(registry: dict[str, Any]) -> None:
    MODEL_REGISTRY_PATH.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    WORKER_MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    WORKER_REGISTRY_PATH.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_validation_groups(model_frame: pd.DataFrame) -> pd.Series:
    cluster_counts = model_frame["location_cluster"].value_counts()
    return model_frame.apply(
        lambda row: row["location_cluster"]
        if int(cluster_counts.get(row["location_cluster"], 0)) >= 18
        else f"{row['market_segment']}_other",
        axis=1,
    )


def _fit_segmented_bundle(
    model_frame: pd.DataFrame,
    y_series: pd.Series,
) -> dict[str, Any]:
    fallback_model = _fit_ebm(model_frame, y_series, SEGMENTED_FEATURE_COLUMNS)
    segment_models: dict[str, ExplainableBoostingRegressor] = {}
    segment_sizes = model_frame["model_segment"].value_counts().to_dict()
    for segment_key, segment_count in segment_sizes.items():
        if int(segment_count) < SEGMENT_MIN_ROWS:
            continue
        segment_mask = model_frame["model_segment"] == segment_key
        segment_models[str(segment_key)] = _fit_ebm(
            model_frame.loc[segment_mask, SEGMENTED_FEATURE_COLUMNS],
            y_series.loc[segment_mask],
            SEGMENTED_FEATURE_COLUMNS,
        )
    return {
        "fallback_model": fallback_model,
        "segment_models": segment_models,
    }


def _predict_segmented_bundle(model_frame: pd.DataFrame, bundle: dict[str, Any]) -> np.ndarray:
    predictions = np.zeros(len(model_frame), dtype=float)
    for index, row in enumerate(model_frame.to_dict(orient="records")):
        segment_key = str(row["model_segment"])
        model = bundle["segment_models"].get(segment_key) or bundle["fallback_model"]
        predicted_log = float(model.predict(pd.DataFrame([row])[SEGMENTED_FEATURE_COLUMNS])[0])
        predictions[index] = float(np.exp(predicted_log))
    return predictions


def _candidate_metric_entry(name: str, maes: list[float], mapes: list[float]) -> dict[str, float | str]:
    return {
        "name": name,
        "mae": float(np.mean(maes)),
        "mape": float(np.mean(mapes)),
    }


@dataclass
class TrainingArtifacts:
    exported_model: dict[str, Any]
    parity_fixtures: list[dict[str, Any]]
    selected_model_kind: str
    registry_entry: dict[str, Any]
    promoted: bool
    active_registry: dict[str, Any]


def _make_time_holdout_split(model_frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    partitions: list[pd.DataFrame] = []
    ordered = model_frame.sort_values(["source", "observed_at"]).copy()
    for _, source_frame in ordered.groupby("source", sort=False):
        source_frame = source_frame.sort_values("observed_at").copy()
        if len(source_frame) <= 1:
            source_frame["__split"] = "train"
        else:
            test_size = max(1, int(np.ceil(len(source_frame) * TIME_HOLDOUT_FRACTION)))
            source_frame["__split"] = "train"
            source_frame.iloc[-test_size:, source_frame.columns.get_loc("__split")] = "test"
        partitions.append(source_frame)
    marked = pd.concat(partitions, ignore_index=False)
    train_frame = marked.loc[marked["__split"] == "train"].drop(columns="__split")
    test_frame = marked.loc[marked["__split"] == "test"].drop(columns="__split")
    return train_frame, test_frame


def _segment_mapes(
    frame: pd.DataFrame,
    actual: np.ndarray,
    predicted: np.ndarray,
    *,
    min_rows: int = 15,
) -> dict[str, float]:
    metrics_frame = frame[["source", "property_type"]].copy().reset_index(drop=True)
    metrics_frame["actual"] = actual
    metrics_frame["predicted"] = predicted
    segment_mapes: dict[str, float] = {}
    for (source, property_type), segment in metrics_frame.groupby(["source", "property_type"]):
        if len(segment) < min_rows:
            continue
        segment_mapes[f"{source}|{property_type}"] = _mape(
            segment["actual"].to_numpy(dtype=float),
            segment["predicted"].to_numpy(dtype=float),
        )
    return segment_mapes


def _house_segment_mape(frame: pd.DataFrame, actual: np.ndarray, predicted: np.ndarray) -> float | None:
    mask = frame["property_type"].astype(str) == "house"
    if int(mask.sum()) == 0:
        return None
    return _mape(actual[mask.to_numpy()], predicted[mask.to_numpy()])


def _balanced_segment_mape(segment_mapes: dict[str, float], fallback_mape: float) -> float:
    if not segment_mapes:
        return fallback_mape
    return float(np.mean(list(segment_mapes.values())))


def _final_score(time_holdout_mape: float, balanced_segment_mape: float) -> float:
    return float(
        FINAL_SCORE_TIME_WEIGHT * time_holdout_mape + FINAL_SCORE_BALANCED_WEIGHT * balanced_segment_mape
    )


def _predict_candidates_for_split(
    train_frame: pd.DataFrame,
    test_frame: pd.DataFrame,
) -> dict[str, np.ndarray]:
    y_train = train_frame["price_czk"].astype(float)
    y_train_ppm = train_frame["price_per_m2"].astype(float)
    predictions: dict[str, np.ndarray] = {}

    baseline_lookup = build_baseline_lookup(train_frame, location_feature="location_cluster")
    predictions["baseline_ppm"] = _predict_baseline_from_features(test_frame, baseline_lookup)

    core_model = _fit_ebm(
        train_frame[CORE_FEATURE_COLUMNS],
        y_train,
        CORE_FEATURE_COLUMNS,
        model_params=CORE_EBM_PARAMS,
    )
    predictions["ebm_core"] = np.exp(core_model.predict(test_frame[CORE_FEATURE_COLUMNS]))

    extended_model = _fit_ebm(
        train_frame[EXTENDED_FEATURE_COLUMNS],
        y_train,
        EXTENDED_FEATURE_COLUMNS,
        model_params=PRICE_EBM_PARAMS,
    )
    extended_predictions = np.exp(extended_model.predict(test_frame[EXTENDED_FEATURE_COLUMNS]))
    predictions["ebm_regressor"] = extended_predictions

    ppm_model = _fit_ebm(
        train_frame[EXTENDED_FEATURE_COLUMNS],
        y_train_ppm,
        EXTENDED_FEATURE_COLUMNS,
        model_params=PPM_EBM_PARAMS,
    )
    ppm_predictions = (
        np.exp(ppm_model.predict(test_frame[EXTENDED_FEATURE_COLUMNS]))
        * test_frame["floor_area_m2"].to_numpy(dtype=float)
    )
    predictions["ebm_ppm_regressor"] = ppm_predictions
    predictions["blended_ebm_regressor"] = BLEND_PRICE_WEIGHT * extended_predictions + BLEND_PPM_WEIGHT * ppm_predictions

    segmented_bundle = _fit_segmented_bundle(train_frame, y_train)
    predictions["segmented_ebm_regressor"] = _predict_segmented_bundle(test_frame, segmented_bundle)

    interaction_model = _fit_ebm(
        train_frame[EXTENDED_FEATURE_COLUMNS],
        y_train,
        EXTENDED_FEATURE_COLUMNS,
        model_params=INTERACTION_EBM_PARAMS,
    )
    predictions["ebm_interactions_challenger"] = np.exp(
        interaction_model.predict(test_frame[EXTENDED_FEATURE_COLUMNS])
    )

    hist_model = _fit_hist_gradient_boosting(
        train_frame[EXTENDED_FEATURE_COLUMNS],
        y_train,
        EXTENDED_FEATURE_COLUMNS,
    )
    predictions["hist_gradient_boosting_challenger"] = np.exp(
        hist_model.predict(test_frame[EXTENDED_FEATURE_COLUMNS])
    )
    return predictions


def _evaluate_candidates_with_cv(base_model_frame: pd.DataFrame) -> dict[str, dict[str, float | str]]:
    validation_groups = _build_validation_groups(base_model_frame)
    splitter = _make_splitter(validation_groups)
    split_inputs = splitter.split(
        base_model_frame[EXTENDED_FEATURE_COLUMNS[:3]],
        base_model_frame["price_czk"].astype(float),
        validation_groups if isinstance(splitter, GroupKFold) else None,
    )
    maes: dict[str, list[float]] = {name: [] for name in (*EXPORTABLE_CANDIDATES, *CHALLENGER_CANDIDATES)}
    mapes: dict[str, list[float]] = {name: [] for name in (*EXPORTABLE_CANDIDATES, *CHALLENGER_CANDIDATES)}

    for train_index, test_index in split_inputs:
        train_base = base_model_frame.iloc[train_index].copy()
        test_base = base_model_frame.iloc[test_index].copy()
        comparable_lookup = build_comparable_lookup(train_base)
        train_frame = apply_comparable_features(train_base, comparable_lookup)
        test_frame = apply_comparable_features(test_base, comparable_lookup)
        predictions = _predict_candidates_for_split(train_frame, test_frame)
        actual = test_frame["price_czk"].to_numpy(dtype=float)
        for name, predicted in predictions.items():
            maes[name].append(float(mean_absolute_error(actual, predicted)))
            mapes[name].append(_mape(actual, predicted))

    return {
        name: _candidate_metric_entry(name, maes[name], mapes[name])
        for name in (*EXPORTABLE_CANDIDATES, *CHALLENGER_CANDIDATES)
    }


def _evaluate_candidates_on_holdout(
    base_model_frame: pd.DataFrame,
) -> tuple[dict[str, dict[str, Any]], ComparableLookup, pd.DataFrame]:
    train_base, test_base = _make_time_holdout_split(base_model_frame)
    comparable_lookup = build_comparable_lookup(train_base)
    train_frame = apply_comparable_features(train_base, comparable_lookup)
    test_frame = apply_comparable_features(test_base, comparable_lookup)
    predictions = _predict_candidates_for_split(train_frame, test_frame)
    actual = test_frame["price_czk"].to_numpy(dtype=float)
    metrics: dict[str, dict[str, Any]] = {}
    for name, predicted in predictions.items():
        segment_mapes = _segment_mapes(test_frame, actual, predicted)
        balanced_segment = _balanced_segment_mape(segment_mapes, _mape(actual, predicted))
        time_holdout_mape = _mape(actual, predicted)
        metrics[name] = {
            "timeHoldoutMae": float(mean_absolute_error(actual, predicted)),
            "timeHoldoutMape": float(time_holdout_mape),
            "balancedSegmentMape": float(balanced_segment),
            "finalScore": _final_score(time_holdout_mape, balanced_segment),
            "segmentMapes": segment_mapes,
            "houseMape": _house_segment_mape(test_frame, actual, predicted),
        }
    return metrics, comparable_lookup, test_frame


def _select_best_exportable_candidate(
    cv_metrics: dict[str, dict[str, float | str]],
    holdout_metrics: dict[str, dict[str, Any]],
) -> str:
    ranked = sorted(
        EXPORTABLE_CANDIDATES,
        key=lambda name: (
            float(holdout_metrics[name]["finalScore"]),
            float(holdout_metrics[name]["timeHoldoutMae"]),
            float(cv_metrics[name]["mae"]),
        ),
    )
    return ranked[0]


def _evaluate_active_exported_model(
    exported_model: dict[str, Any],
    test_frame: pd.DataFrame,
) -> dict[str, Any]:
    predictions = np.asarray(
        [score_exported_model(exported_model, record)["estimated_price_czk"] for record in test_frame.to_dict(orient="records")],
        dtype=float,
    )
    actual = test_frame["price_czk"].to_numpy(dtype=float)
    segment_mapes = _segment_mapes(test_frame, actual, predictions)
    balanced_segment = _balanced_segment_mape(segment_mapes, _mape(actual, predictions))
    time_holdout_mape = _mape(actual, predictions)
    return {
        "timeHoldoutMae": float(mean_absolute_error(actual, predictions)),
        "timeHoldoutMape": float(time_holdout_mape),
        "balancedSegmentMape": float(balanced_segment),
        "finalScore": _final_score(time_holdout_mape, balanced_segment),
        "segmentMapes": segment_mapes,
        "houseMape": _house_segment_mape(test_frame, actual, predictions),
    }


def _segment_guardrails_pass(
    candidate_metrics: dict[str, Any],
    active_metrics: dict[str, Any],
) -> tuple[bool, bool]:
    segment_guardrail = True
    for segment_key, candidate_mape in candidate_metrics["segmentMapes"].items():
        active_mape = active_metrics["segmentMapes"].get(segment_key)
        if active_mape is None:
            continue
        if float(candidate_mape) - float(active_mape) > SEGMENT_REGRESSION_THRESHOLD:
            segment_guardrail = False
            break
    candidate_house = candidate_metrics.get("houseMape")
    active_house = active_metrics.get("houseMape")
    house_guardrail = True
    if candidate_house is not None and active_house is not None:
        house_guardrail = float(candidate_house) - float(active_house) <= HOUSE_REGRESSION_THRESHOLD
    return segment_guardrail, house_guardrail


def train_and_export(curated_frame: pd.DataFrame, config: PipelineConfig | None = None) -> TrainingArtifacts:
    config = config or PipelineConfig()
    base_model_frame = build_model_frame(curated_frame)
    cv_metrics = _evaluate_candidates_with_cv(base_model_frame)
    holdout_metrics, _, holdout_test_frame = _evaluate_candidates_on_holdout(base_model_frame)
    selected_model_kind = _select_best_exportable_candidate(cv_metrics, holdout_metrics)

    version = datetime.now(timezone.utc).strftime("model-%Y%m%dT%H%M%SZ")
    training_window = _training_window(curated_frame)
    source_mix = _source_mix(curated_frame)
    full_comparable_lookup = build_comparable_lookup(base_model_frame)
    model_frame = build_model_frame(curated_frame, comparable_lookup=full_comparable_lookup)
    y_series = model_frame["price_czk"].astype(float)
    core_x = model_frame[CORE_FEATURE_COLUMNS].copy()
    extended_x = model_frame[EXTENDED_FEATURE_COLUMNS].copy()

    validation_summary = {
        "grouping": "location_cluster_grouped_cv_plus_per_source_time_holdout",
        "selectedModel": selected_model_kind,
        "selectedMae": float(cv_metrics[selected_model_kind]["mae"]),
        "finalScore": float(holdout_metrics[selected_model_kind]["finalScore"]),
        "timeHoldoutMape": float(holdout_metrics[selected_model_kind]["timeHoldoutMape"]),
        "balancedSegmentMape": float(holdout_metrics[selected_model_kind]["balancedSegmentMape"]),
        "metrics": {
            **{
                f"{name}_mae": float(cv_metrics[name]["mae"])
                for name in (*EXPORTABLE_CANDIDATES, *CHALLENGER_CANDIDATES)
            },
            **{
                f"{name}_mape": float(cv_metrics[name]["mape"])
                for name in (*EXPORTABLE_CANDIDATES, *CHALLENGER_CANDIDATES)
            },
            **{
                f"{name}_time_holdout_mae": float(holdout_metrics[name]["timeHoldoutMae"])
                for name in (*EXPORTABLE_CANDIDATES, *CHALLENGER_CANDIDATES)
            },
            **{
                f"{name}_time_holdout_mape": float(holdout_metrics[name]["timeHoldoutMape"])
                for name in (*EXPORTABLE_CANDIDATES, *CHALLENGER_CANDIDATES)
            },
            **{
                f"{name}_balanced_segment_mape": float(holdout_metrics[name]["balancedSegmentMape"])
                for name in (*EXPORTABLE_CANDIDATES, *CHALLENGER_CANDIDATES)
            },
            **{
                f"{name}_final_score": float(holdout_metrics[name]["finalScore"])
                for name in (*EXPORTABLE_CANDIDATES, *CHALLENGER_CANDIDATES)
            },
            "selectedModel": selected_model_kind,
        },
        "candidates": [
            {
                **cv_metrics[name],
                **holdout_metrics[name],
                "exportable": True,
            }
            for name in EXPORTABLE_CANDIDATES
        ],
        "challengers": [
            {
                **cv_metrics[name],
                **holdout_metrics[name],
                "exportable": False,
                "readyForBackendServing": float(holdout_metrics[selected_model_kind]["finalScore"]) - float(holdout_metrics[name]["finalScore"])
                >= CHALLENGER_READINESS_THRESHOLD,
            }
            for name in CHALLENGER_CANDIDATES
        ],
    }
    metadata = {
        "version": version,
        "trainedAt": datetime.now(timezone.utc).isoformat(),
        "target": "prague_metro_asking_price_czk",
        "metrics": validation_summary["metrics"],
        "curatedRowCount": int(len(curated_frame)),
        "trainingWindow": training_window,
        "sourceMix": source_mix,
        "validationSummary": validation_summary,
        "promotionReason": "candidate_pending_gate",
        "comparableLookup": full_comparable_lookup.to_dict(),
        "notes": [
            "Estimate of typical asking price for Prague and metro-region listings.",
            "Model uses static geo context, local comparable ppm priors, and dual validation.",
            "Champion selection is based on per-source time holdout plus balanced segment MAPE.",
        ],
    }

    baseline_lookup = build_baseline_lookup(model_frame, location_feature="location_cluster")
    if selected_model_kind == "baseline_ppm":
        baseline_predictions = _predict_baseline_from_features(model_frame, baseline_lookup)
        residual_quantiles = _residual_quantiles(y_series.to_numpy(), baseline_predictions)
        exported_model = export_baseline_model(baseline_lookup, residual_quantiles, metadata)
    elif selected_model_kind == "ebm_core":
        fitted_model = _fit_ebm(
            core_x,
            y_series,
            CORE_FEATURE_COLUMNS,
            model_params=CORE_EBM_PARAMS,
        )
        fitted_predictions = np.exp(fitted_model.predict(core_x))
        residual_quantiles = _residual_quantiles(y_series.to_numpy(), fitted_predictions)
        exported_model = export_ebm_model(
            fitted_model,
            baseline_lookup,
            residual_quantiles,
            metadata,
            CORE_FEATURE_COLUMNS,
        )
    elif selected_model_kind == "ebm_ppm_regressor":
        fitted_model = _fit_ebm(
            extended_x,
            model_frame["price_per_m2"].astype(float),
            EXTENDED_FEATURE_COLUMNS,
            model_params=PPM_EBM_PARAMS,
        )
        fitted_predictions = np.exp(fitted_model.predict(extended_x)) * model_frame["floor_area_m2"].to_numpy(dtype=float)
        residual_quantiles = _residual_quantiles(y_series.to_numpy(), fitted_predictions)
        exported_model = export_ppm_ebm_model(
            fitted_model,
            baseline_lookup,
            residual_quantiles,
            metadata,
            EXTENDED_FEATURE_COLUMNS,
        )
    elif selected_model_kind == "blended_ebm_regressor":
        price_model = _fit_ebm(
            extended_x,
            y_series,
            EXTENDED_FEATURE_COLUMNS,
            model_params=PRICE_EBM_PARAMS,
        )
        ppm_model = _fit_ebm(
            extended_x,
            model_frame["price_per_m2"].astype(float),
            EXTENDED_FEATURE_COLUMNS,
            model_params=PPM_EBM_PARAMS,
        )
        price_predictions = np.exp(price_model.predict(extended_x))
        ppm_predictions = np.exp(ppm_model.predict(extended_x)) * model_frame["floor_area_m2"].to_numpy(dtype=float)
        fitted_predictions = BLEND_PRICE_WEIGHT * price_predictions + BLEND_PPM_WEIGHT * ppm_predictions
        residual_quantiles = _residual_quantiles(y_series.to_numpy(), fitted_predictions)
        exported_model = export_blended_ebm_model(
            price_model=price_model,
            ppm_model=ppm_model,
            baseline_lookup=baseline_lookup,
            residual_quantiles=residual_quantiles,
            metadata=metadata,
            feature_columns=EXTENDED_FEATURE_COLUMNS,
        )
    elif selected_model_kind == "segmented_ebm_regressor":
        segmented_bundle = _fit_segmented_bundle(model_frame, y_series)
        fitted_predictions = _predict_segmented_bundle(model_frame, segmented_bundle)
        residual_quantiles = _residual_quantiles(y_series.to_numpy(), fitted_predictions)
        exported_model = export_segmented_ebm_model(
            fallback_model=segmented_bundle["fallback_model"],
            segment_models=segmented_bundle["segment_models"],
            baseline_lookup=baseline_lookup,
            residual_quantiles=residual_quantiles,
            metadata=metadata,
        )
    else:
        fitted_model = _fit_ebm(
            extended_x,
            y_series,
            EXTENDED_FEATURE_COLUMNS,
            model_params=PRICE_EBM_PARAMS,
        )
        fitted_predictions = np.exp(fitted_model.predict(extended_x))
        residual_quantiles = _residual_quantiles(y_series.to_numpy(), fitted_predictions)
        exported_model = export_ebm_model(
            fitted_model,
            baseline_lookup,
            residual_quantiles,
            metadata,
            EXTENDED_FEATURE_COLUMNS,
        )

    parity_frame = model_frame[EXTENDED_FEATURE_COLUMNS + ["model_segment"]].copy()
    parity_fixtures = _make_parity_fixtures(exported_model, parity_frame)
    registry = load_model_registry(len(curated_frame))
    active_version = registry.get("activeModelVersion")
    active_entry = next((entry for entry in registry["entries"] if entry["version"] == active_version), None)
    active_selected_mae = (
        float(active_entry["validationSummary"]["selectedMae"])
        if active_entry and active_entry.get("validationSummary", {}).get("selectedMae") is not None
        else float("inf")
    )
    candidate_selected_mae = float(validation_summary["selectedMae"])
    mae_improvement = (
        0.0
        if not np.isfinite(active_selected_mae) or active_selected_mae <= 0
        else (active_selected_mae - candidate_selected_mae) / active_selected_mae
    )
    active_row_count = int(active_entry.get("curatedRowCount", 0)) if active_entry else 0
    new_curated_rows = int(len(curated_frame) - active_row_count)

    active_final_score = _selected_final_score(active_entry.get("validationSummary") if active_entry else None)
    if active_entry and not np.isfinite(active_final_score) and ACTIVE_MODEL_PATH.exists():
        active_model = json.loads(ACTIVE_MODEL_PATH.read_text(encoding="utf-8"))
        active_metrics = _evaluate_active_exported_model(active_model, holdout_test_frame)
    elif active_entry:
        active_metrics = {
            "finalScore": active_final_score,
            "segmentMapes": active_entry.get("validationSummary", {}).get("segmentMapes", {}),
            "houseMape": active_entry.get("validationSummary", {}).get("houseMape"),
        }
    else:
        active_metrics = {
            "finalScore": float("inf"),
            "segmentMapes": {},
            "houseMape": None,
        }

    final_score_improvement = (
        float(active_metrics["finalScore"]) - float(holdout_metrics[selected_model_kind]["finalScore"])
        if np.isfinite(float(active_metrics["finalScore"]))
        else None
    )
    segment_guardrail_passed, house_guardrail_passed = _segment_guardrails_pass(
        holdout_metrics[selected_model_kind],
        active_metrics,
    )
    promoted, promotion_reason = _decide_promotion(
        active_version=active_version,
        active_entry=active_entry,
        active_row_count=active_row_count,
        curated_row_count=int(len(curated_frame)),
        new_curated_rows=new_curated_rows,
        candidate_selected_mae=candidate_selected_mae,
        mae_improvement=mae_improvement,
        config=config,
        final_score_improvement=final_score_improvement,
        segment_guardrail_passed=segment_guardrail_passed,
        house_guardrail_passed=house_guardrail_passed,
    )
    exported_model["promotionReason"] = promotion_reason
    registry_entry = {
        "version": version,
        "trainedAt": exported_model["trainedAt"],
        "modelKind": exported_model["kind"],
        "curatedRowCount": int(len(curated_frame)),
        "trainingWindow": training_window,
        "sourceMix": source_mix,
        "validationSummary": {
            **validation_summary,
            "segmentMapes": holdout_metrics[selected_model_kind]["segmentMapes"],
            "houseMape": holdout_metrics[selected_model_kind]["houseMape"],
        },
        "promotionReason": promotion_reason,
        "promoted": promoted,
        "promotedAt": exported_model["trainedAt"] if promoted else None,
        "newCuratedRowsSincePreviousActive": new_curated_rows,
        "servingEligible": True,
        "finalScore": float(holdout_metrics[selected_model_kind]["finalScore"]),
        "timeHoldoutMape": float(holdout_metrics[selected_model_kind]["timeHoldoutMape"]),
        "balancedSegmentMape": float(holdout_metrics[selected_model_kind]["balancedSegmentMape"]),
        "readyForBackendServing": False,
        "challengers": validation_summary["challengers"],
    }
    return TrainingArtifacts(
        exported_model=exported_model,
        parity_fixtures=parity_fixtures,
        selected_model_kind=selected_model_kind,
        registry_entry=registry_entry,
        promoted=promoted,
        active_registry=registry,
    )


def write_training_artifacts(training_artifacts: TrainingArtifacts) -> dict[str, Path]:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    WORKER_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    WORKER_MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

    version = training_artifacts.exported_model["version"]
    model_path = ARTIFACTS_DIR / f"{version}.json"
    parity_versioned_path = ARTIFACTS_DIR / f"{version}-parity.json"

    model_path.write_text(
        json.dumps(training_artifacts.exported_model, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    parity_payload = {"modelVersion": version, "fixtures": training_artifacts.parity_fixtures}
    parity_versioned_path.write_text(json.dumps(parity_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    registry = training_artifacts.active_registry
    existing_entries = [entry for entry in registry.get("entries", []) if entry["version"] != version]
    existing_entries.append(training_artifacts.registry_entry)
    existing_entries.sort(key=lambda entry: entry["trainedAt"] or "", reverse=True)
    registry["entries"] = existing_entries
    if training_artifacts.promoted:
        registry["activeModelVersion"] = version
        registry["lastPromotedAt"] = training_artifacts.registry_entry["promotedAt"]
        ACTIVE_MODEL_PATH.write_text(
            json.dumps(training_artifacts.exported_model, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        ACTIVE_PARITY_PATH.write_text(
            json.dumps(parity_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        WORKER_ACTIVE_MODEL_PATH.write_text(
            json.dumps(training_artifacts.exported_model, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    _write_registry(registry)

    paths = {
        "candidate_model": model_path,
        "candidate_parity": parity_versioned_path,
        "model_registry": MODEL_REGISTRY_PATH,
    }
    if training_artifacts.promoted:
        paths.update(
            {
                "active_model": ACTIVE_MODEL_PATH,
                "active_parity": ACTIVE_PARITY_PATH,
                "worker_model": WORKER_ACTIVE_MODEL_PATH,
                "worker_registry": WORKER_REGISTRY_PATH,
            }
        )
    return paths


def refresh_active_model_runtime_metadata(curated_frame: pd.DataFrame) -> None:
    if not ACTIVE_MODEL_PATH.exists():
        return
    active_model = json.loads(ACTIVE_MODEL_PATH.read_text(encoding="utf-8"))
    comparable_lookup = build_comparable_lookup(build_model_frame(curated_frame))
    active_model["comparableLookup"] = comparable_lookup.to_dict()
    existing_notes = list(active_model.get("notes") or [])
    runtime_note = "Runtime scoring includes comparable ppm priors and confidence metadata."
    if runtime_note not in existing_notes:
        active_model["notes"] = [*existing_notes, runtime_note]
    ACTIVE_MODEL_PATH.write_text(json.dumps(active_model, ensure_ascii=False, indent=2), encoding="utf-8")
    WORKER_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    WORKER_ACTIVE_MODEL_PATH.write_text(
        json.dumps(active_model, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
