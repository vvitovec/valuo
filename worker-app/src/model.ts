import { latLngToCell } from "h3-js";

import transitNodes from "../../shared/transit-nodes.json";
import { haversineKm, inferMarketSegment } from "./market";
import type {
  BaselineContribution,
  BlendedEBMModel,
  ComparableLookupPayload,
  EBMContribution,
  EBMPpmModel,
  EBMTermsModel,
  ExportedModel,
  PredictionRequest,
  SegmentedEBMModel
} from "./types";

const PRAGUE_CENTER = { lat: 50.0755, lng: 14.4378 };

type TransitNode = {
  lat: number;
  lng: number;
  name: string;
};

type TransitNodesPayload = {
  metroNodes: TransitNode[];
  railNodes: TransitNode[];
};

type FeatureValue = string | number | null;

export type FeaturePayload = {
  district_prague: string;
  property_type: string;
  disposition: string;
  floor_area_m2: number;
  land_area_m2: number;
  land_area_missing?: string;
  floor_no: number;
  floor_no_missing?: string;
  total_floors: number;
  total_floors_missing?: string;
  ownership: string;
  condition: string;
  construction: string;
  energy_label: string;
  has_elevator: string;
  has_parking: string;
  has_cellar: string;
  has_balcony_or_loggia: string;
  distance_to_center_km?: number | null;
  distance_to_metro_km?: number | null;
  distance_to_rail_km?: number | null;
  center_ring?: string;
  market_segment: string;
  location_cluster: string;
  h3_cell?: string;
  room_count: number;
  area_per_room_m2: number;
  floor_position_ratio: number;
  has_geocode_coordinates?: string;
  geocode_resolution?: string;
  missing_core_feature_count?: number;
  listing_input_quality_score?: number;
  local_ppm_h3_property?: number;
  local_ppm_location_cluster_property?: number;
  local_ppm_district_property?: number;
  local_ppm_shrunk?: number;
  comparables_count_h3?: number;
  model_segment: string;
  [key: string]: FeatureValue | undefined;
};

type ComparableSignalPayload = {
  comparablesCount: number;
  warningFlags: string[];
  confidenceScore: number;
  confidenceLabel: "high" | "medium" | "low";
};

function normalizeTextKey(value: unknown): string {
  if (value == null) {
    return "unknown";
  }
  const text = String(value)
    .normalize("NFKD")
    .replace(/\p{Diacritic}/gu, "")
    .replace(/\u00a0/g, " ")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, " ");
  return text || "unknown";
}

function normalizeOwnership(value: unknown): string {
  const normalized = normalizeTextKey(value);
  if (normalized.includes("osob")) {
    return "osobni";
  }
  if (normalized.includes("druz")) {
    return "druzstevni";
  }
  if (normalized === "jine" || normalized === "ostatni") {
    return "other";
  }
  return normalized;
}

function normalizeEnergyLabel(value: unknown): string {
  const normalized = normalizeTextKey(value);
  const match = normalized.match(/[a-g]/);
  return match?.[0] ?? normalized;
}

function normalizeBooleanFlag(value: unknown): string {
  if (value == null) {
    return "unknown";
  }
  if (typeof value === "string") {
    const normalized = normalizeTextKey(value);
    if (normalized === "unknown") {
      return "unknown";
    }
    if (["ano", "yes", "true", "1"].includes(normalized)) {
      return "yes";
    }
    if (["ne", "no", "false", "0"].includes(normalized)) {
      return "no";
    }
  }
  return value ? "yes" : "no";
}

function parseRoomCount(disposition: string | null | undefined): number {
  if (!disposition) {
    return 0;
  }
  const match = disposition.trim().toLowerCase().match(/^(\d+)\+/);
  return match ? Number(match[1]) : 0;
}

function safeNumber(value: unknown): number | null {
  if (value == null || value === "") {
    return null;
  }
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function centerRing(distanceToCenterKm: number | null): string {
  if (distanceToCenterKm == null || !Number.isFinite(distanceToCenterKm)) {
    return "unknown";
  }
  if (distanceToCenterKm < 3) {
    return "lt_3km";
  }
  if (distanceToCenterKm < 6) {
    return "3_6km";
  }
  if (distanceToCenterKm < 10) {
    return "6_10km";
  }
  if (distanceToCenterKm < 16) {
    return "10_16km";
  }
  return "16km_plus";
}

function inputQualityScore(
  missingCoreFeatureCount: number,
  hasGeocodeCoordinates: boolean,
  geocodeResolution: string
): number {
  let score = 1;
  if (!hasGeocodeCoordinates) {
    score -= 0.2;
  }
  if (geocodeResolution === "fallback_manual") {
    score -= 0.15;
  }
  if (missingCoreFeatureCount >= 6) {
    score -= 0.2;
  } else if (missingCoreFeatureCount >= 4) {
    score -= 0.1;
  }
  return Math.min(1, Math.max(0, score));
}

function countMissingCoreInputs(record: Record<string, unknown>): number {
  let missing = 0;
  for (const field of [
    "disposition",
    "land_area_m2",
    "floor_no",
    "total_floors",
    "ownership",
    "condition",
    "construction",
    "energy_label",
    "has_elevator",
    "has_parking",
    "has_cellar",
    "has_balcony_or_loggia"
  ]) {
    const value = record[field];
    if (["land_area_m2", "floor_no", "total_floors"].includes(field)) {
      if (safeNumber(value) == null) {
        missing += 1;
      }
      continue;
    }
    if (value == null || normalizeTextKey(value) === "unknown") {
      missing += 1;
    }
  }
  return missing;
}

function nearestTransitDistanceKm(
  lat: number | null,
  lng: number | null,
  nodes: TransitNode[]
): number | null {
  if (lat == null || lng == null) {
    return null;
  }
  return nodes.reduce<number | null>((best, node) => {
    const distance = haversineKm(lat, lng, node.lat, node.lng);
    if (best == null || distance < best) {
      return distance;
    }
    return best;
  }, null);
}

function computeH3Cell(lat: number | null, lng: number | null, resolution: number): string {
  if (lat == null || lng == null) {
    return "unknown";
  }
  return latLngToCell(lat, lng, resolution);
}

function comparableFeatures(
  payload: FeaturePayload,
  lookup: ComparableLookupPayload
): Pick<
  FeaturePayload,
  | "local_ppm_h3_property"
  | "local_ppm_location_cluster_property"
  | "local_ppm_district_property"
  | "local_ppm_shrunk"
  | "comparables_count_h3"
> {
  const propertyType = String(payload.property_type || "unknown");
  const district = String(payload.district_prague || "unknown");
  const locationCluster = String(payload.location_cluster || district);
  const h3Cell = String(payload.h3_cell || "unknown");
  const propertyFallback = lookup.propertyFallbackPpm[propertyType] ?? lookup.globalFallbackPpm;
  const districtValue = lookup.districtPropertyPpm[`${district}|${propertyType}`] ?? propertyFallback;
  const locationValue =
    lookup.locationPropertyPpm[`${locationCluster}|${propertyType}`] ?? districtValue;
  const h3Key = `${h3Cell}|${propertyType}`;
  const h3Value = lookup.h3PropertyPpm[h3Key];
  const h3Count = Number(lookup.h3PropertyCount[h3Key] ?? 0);
  if (h3Value == null) {
    return {
      local_ppm_h3_property: locationValue,
      local_ppm_location_cluster_property: locationValue,
      local_ppm_district_property: districtValue,
      local_ppm_shrunk: locationValue,
      comparables_count_h3: h3Count
    };
  }
  const shrinkage = 5;
  return {
    local_ppm_h3_property: h3Value,
    local_ppm_location_cluster_property: locationValue,
    local_ppm_district_property: districtValue,
    local_ppm_shrunk: ((h3Value * h3Count) + locationValue * shrinkage) / Math.max(h3Count + shrinkage, 1),
    comparables_count_h3: h3Count
  };
}

export function enrichPayloadForModel(model: ExportedModel, payload: FeaturePayload): FeaturePayload {
  if (!model.comparableLookup) {
    return payload;
  }
  return {
    ...payload,
    ...comparableFeatures(payload, model.comparableLookup)
  };
}

export function makeFeaturePayload(
  request: PredictionRequest & {
    distanceToCenterKm: number | null;
    locationCluster: string;
    marketSegment: string;
    lat?: number | null;
    lng?: number | null;
    geocodeResolution?: "exact" | "fallback_manual";
    distanceToMetroKm?: number | null;
    distanceToRailKm?: number | null;
  }
): FeaturePayload {
  const roomCount = parseRoomCount(request.disposition);
  const floorNo = safeNumber(request.floorNo) ?? -1;
  const totalFloors = safeNumber(request.totalFloors) ?? -1;
  const lat = safeNumber(request.lat);
  const lng = safeNumber(request.lng);
  const hasGeocodeCoordinates = lat != null && lng != null;
  const geocodeResolution =
    request.geocodeResolution ?? (hasGeocodeCoordinates ? "exact" : "fallback_manual");
  const marketSegment =
    request.marketSegment || inferMarketSegment(request.locationCluster, request.districtPrague);
  const missingCoreFeatureCount = countMissingCoreInputs({
    disposition: request.disposition,
    land_area_m2: request.landAreaM2,
    floor_no: request.floorNo,
    total_floors: request.totalFloors,
    ownership: request.ownership,
    condition: request.condition,
    construction: request.construction,
    energy_label: request.energyLabel,
    has_elevator: request.hasElevator,
    has_parking: request.hasParking,
    has_cellar: request.hasCellar,
    has_balcony_or_loggia: request.hasBalconyOrLoggia
  });
  const distanceToCenterKm =
    request.distanceToCenterKm ??
    (hasGeocodeCoordinates ? haversineKm(lat!, lng!, PRAGUE_CENTER.lat, PRAGUE_CENTER.lng) : null);
  const distanceToMetroKm =
    request.distanceToMetroKm ?? nearestTransitDistanceKm(lat, lng, (transitNodes as TransitNodesPayload).metroNodes);
  const distanceToRailKm =
    request.distanceToRailKm ?? nearestTransitDistanceKm(lat, lng, (transitNodes as TransitNodesPayload).railNodes);

  return {
    district_prague: request.districtPrague,
    property_type: request.propertyType,
    disposition: request.disposition || "unknown",
    floor_area_m2: request.floorAreaM2,
    land_area_m2: safeNumber(request.landAreaM2) ?? 0,
    land_area_missing: request.landAreaM2 == null ? "yes" : "no",
    floor_no: floorNo,
    floor_no_missing: request.floorNo == null ? "yes" : "no",
    total_floors: totalFloors,
    total_floors_missing: request.totalFloors == null ? "yes" : "no",
    ownership: normalizeOwnership(request.ownership),
    condition: request.condition || "unknown",
    construction: request.construction || "unknown",
    energy_label: normalizeEnergyLabel(request.energyLabel),
    has_elevator: normalizeBooleanFlag(request.hasElevator),
    has_parking: normalizeBooleanFlag(request.hasParking),
    has_cellar: normalizeBooleanFlag(request.hasCellar),
    has_balcony_or_loggia: normalizeBooleanFlag(request.hasBalconyOrLoggia),
    distance_to_center_km: distanceToCenterKm,
    distance_to_metro_km: distanceToMetroKm,
    distance_to_rail_km: distanceToRailKm,
    center_ring: centerRing(distanceToCenterKm),
    market_segment: marketSegment,
    location_cluster: request.locationCluster,
    h3_cell: computeH3Cell(lat, lng, 8),
    room_count: roomCount,
    area_per_room_m2: roomCount > 0 ? request.floorAreaM2 / roomCount : request.floorAreaM2,
    floor_position_ratio: floorNo >= 0 && totalFloors > 0 ? floorNo / totalFloors : -1,
    has_geocode_coordinates: hasGeocodeCoordinates ? "yes" : "no",
    geocode_resolution: geocodeResolution,
    missing_core_feature_count: missingCoreFeatureCount,
    listing_input_quality_score: inputQualityScore(
      missingCoreFeatureCount,
      hasGeocodeCoordinates,
      geocodeResolution
    ),
    model_segment: `${marketSegment}_${request.propertyType}`
  };
}

function lookupBaselinePpm(
  lookup: Record<string, number>,
  locationCluster: string,
  propertyType: string
): number {
  return (
    lookup[`${locationCluster}|${propertyType}`] ??
    lookup[`fallback|${propertyType}`] ??
    lookup["fallback|all"]
  );
}

function scoreBaseline(
  model: Extract<ExportedModel, { kind: "baseline_ppm" }>,
  payload: FeaturePayload
) {
  const locationFeature = model.baselineLocationFeature ?? "district_prague";
  const locationValue = String(payload[locationFeature as keyof FeaturePayload] ?? payload.location_cluster);
  const ppm = lookupBaselinePpm(model.baselineLookup, locationValue, payload.property_type);
  const estimatedPriceCzk = payload.floor_area_m2 * ppm;
  const contributions: BaselineContribution[] = [
    { featureName: locationFeature, score: ppm, mode: "price_per_m2" },
    { featureName: "floor_area_m2", score: payload.floor_area_m2, mode: "area_multiplier" }
  ];
  return { estimatedPriceCzk, contributions };
}

function scoreEbmTermsValue(
  model: EBMTermsModel | EBMPpmModel,
  payload: FeaturePayload
) {
  let total = model.intercept;
  const contributions: EBMContribution[] = [];
  for (const term of model.terms) {
    const rawValue = payload[term.featureName as keyof FeaturePayload];
    let score = term.missingScore;
    if (rawValue !== undefined && rawValue !== null && rawValue !== "unknown") {
      if (term.featureType === "continuous") {
        score = term.unknownScore;
        const numericValue = Number(rawValue);
        for (const bin of term.bins) {
          if (bin.upperBound === null || numericValue <= bin.upperBound) {
            score = bin.score;
            break;
          }
        }
      } else {
        score = term.categories[String(rawValue)] ?? term.unknownScore;
      }
    }
    total += score;
    contributions.push({ featureName: term.featureName, score });
  }

  return {
    predictedValue: Math.exp(total),
    rawTotalLogTarget: total,
    contributions
  };
}

function contributionImpactsFromTerms(
  scoreResult: ReturnType<typeof scoreEbmTermsValue>,
  areaMultiplier = 1
) {
  return scoreResult.contributions.map((contribution) => ({
    featureName: contribution.featureName,
    score:
      (scoreResult.predictedValue - Math.exp(scoreResult.rawTotalLogTarget - contribution.score)) *
      areaMultiplier
  }));
}

function mergeWeightedContributionImpacts(
  priceImpacts: Array<{ featureName: string; score: number }>,
  ppmImpacts: Array<{ featureName: string; score: number }>,
  weights: BlendedEBMModel["blendWeights"]
) {
  const merged = new Map<string, number>();
  const order: string[] = [];
  for (const impact of priceImpacts) {
    if (!merged.has(impact.featureName)) {
      order.push(impact.featureName);
      merged.set(impact.featureName, 0);
    }
    merged.set(impact.featureName, (merged.get(impact.featureName) ?? 0) + weights.price * impact.score);
  }
  for (const impact of ppmImpacts) {
    if (!merged.has(impact.featureName)) {
      order.push(impact.featureName);
      merged.set(impact.featureName, 0);
    }
    merged.set(
      impact.featureName,
      (merged.get(impact.featureName) ?? 0) + weights.pricePerM2 * impact.score
    );
  }
  return order.map((featureName) => ({
    featureName,
    score: merged.get(featureName) ?? 0
  }));
}

function scorePpmEbm(
  model: Extract<ExportedModel, { kind: "ebm_ppm_regressor" }>,
  payload: FeaturePayload
) {
  const ppmScore = scoreEbmTermsValue(model, payload);
  const area = payload.floor_area_m2;
  return {
    estimatedPriceCzk: ppmScore.predictedValue * area,
    contributions: contributionImpactsFromTerms(ppmScore, area)
  };
}

function scoreBlendedEbm(
  model: Extract<ExportedModel, { kind: "blended_ebm_regressor" }>,
  payload: FeaturePayload
) {
  const priceScore = scoreEbmTermsValue(model.priceModel, payload);
  const ppmScore = scoreEbmTermsValue(model.ppmModel, payload);
  const ppmEstimatedPrice = ppmScore.predictedValue * payload.floor_area_m2;
  return {
    estimatedPriceCzk:
      model.blendWeights.price * priceScore.predictedValue +
      model.blendWeights.pricePerM2 * ppmEstimatedPrice,
    contributions: mergeWeightedContributionImpacts(
      contributionImpactsFromTerms(priceScore),
      contributionImpactsFromTerms(ppmScore, payload.floor_area_m2),
      model.blendWeights
    )
  };
}

function scoreSegmentedEbm(model: SegmentedEBMModel, payload: FeaturePayload) {
  const segmentKey = String(payload[model.segmentKeyFeature as keyof FeaturePayload] ?? payload.model_segment);
  const selectedModel = model.segmentModels[segmentKey] ?? model.fallbackModel;
  const scoreResult = scoreEbmTermsValue(selectedModel, payload);
  return {
    ...scoreResult,
    estimatedPriceCzk: scoreResult.predictedValue,
    totalLogPrice: scoreResult.rawTotalLogTarget,
    selectedSegment: model.segmentModels[segmentKey] ? segmentKey : "fallback"
  };
}

export function scoreModel(model: ExportedModel, rawPayload: FeaturePayload) {
  const payload = enrichPayloadForModel(model, rawPayload);
  if (model.kind === "baseline_ppm") {
    return scoreBaseline(model, payload);
  }
  if (model.kind === "ebm_ppm_regressor") {
    return scorePpmEbm(model, payload);
  }
  if (model.kind === "blended_ebm_regressor") {
    return scoreBlendedEbm(model, payload);
  }
  if (model.kind === "segmented_ebm_regressor") {
    return scoreSegmentedEbm(model, payload);
  }
  const scoreResult = scoreEbmTermsValue(model, payload);
  return {
    ...scoreResult,
    estimatedPriceCzk: scoreResult.predictedValue,
    totalLogPrice: scoreResult.rawTotalLogTarget
  };
}

export function featureEffectsFromScore(
  model: ExportedModel,
  scoreResult: ReturnType<typeof scoreModel>
) {
  if (model.kind === "baseline_ppm") {
    const baseline = scoreResult as ReturnType<typeof scoreBaseline>;
    return baseline.contributions.map((contribution) => ({
      featureName: contribution.featureName,
      rawScore: contribution.score,
      impactCzk: Math.round(contribution.score)
    }));
  }
  if (model.kind === "ebm_ppm_regressor" || model.kind === "blended_ebm_regressor") {
    const directImpactResult = scoreResult as {
      contributions: Array<{ featureName: string; score: number }>;
    };
    return directImpactResult.contributions
      .map((contribution) => ({
        featureName: contribution.featureName,
        rawScore: contribution.score,
        impactCzk: Math.round(contribution.score)
      }))
      .sort((left, right) => Math.abs(right.impactCzk) - Math.abs(left.impactCzk));
  }

  const ebmResult = scoreResult as ReturnType<typeof scoreEbmTermsValue> & {
    estimatedPriceCzk: number;
    totalLogPrice: number;
  };
  return ebmResult.contributions
    .map((contribution) => ({
      featureName: contribution.featureName,
      rawScore: contribution.score,
      impactCzk: Math.round(
        ebmResult.estimatedPriceCzk - Math.exp(ebmResult.totalLogPrice - contribution.score)
      )
    }))
    .sort((left, right) => Math.abs(right.impactCzk) - Math.abs(left.impactCzk));
}

export function applyPredictionInterval(
  model: ExportedModel,
  estimatedPriceCzk: number
) {
  const anchor = Math.max(estimatedPriceCzk, 1);
  const low = Math.exp(Math.log(anchor) + model.residualQuantiles.low);
  const high = Math.exp(Math.log(anchor) + model.residualQuantiles.high);
  return {
    low: Math.round(low),
    high: Math.round(high)
  };
}

export function confidenceScoreComponents(
  estimatedPriceCzk: number,
  intervalLow: number,
  intervalHigh: number,
  payload: FeaturePayload
): ComparableSignalPayload {
  let score = 1;
  const warningFlags: string[] = [];
  const comparablesCount = Number(payload.comparables_count_h3 ?? 0);
  const missingCoreFeatureCount = Number(payload.missing_core_feature_count ?? 0);
  if (payload.geocode_resolution === "fallback_manual") {
    score -= 0.35;
    warningFlags.push("geocode_fallback");
  }
  if (comparablesCount < 3) {
    score -= 0.25;
    warningFlags.push("too_few_comparables");
  } else if (comparablesCount < 5) {
    score -= 0.15;
    warningFlags.push("limited_comparables");
  }
  if (missingCoreFeatureCount >= 6) {
    score -= 0.2;
    warningFlags.push("many_missing_inputs");
  } else if (missingCoreFeatureCount >= 4) {
    score -= 0.1;
    warningFlags.push("some_missing_inputs");
  }
  const halfWidthRatio =
    Math.max(intervalHigh - estimatedPriceCzk, estimatedPriceCzk - intervalLow) / Math.max(estimatedPriceCzk, 1);
  if (halfWidthRatio > 0.3) {
    score -= 0.2;
    warningFlags.push("very_wide_prediction_interval");
  } else if (halfWidthRatio > 0.2) {
    score -= 0.1;
    warningFlags.push("wide_prediction_interval");
  }
  const confidenceScore = Math.min(1, Math.max(0, score));
  const confidenceLabel =
    confidenceScore >= 0.75 ? "high" : confidenceScore >= 0.5 ? "medium" : "low";
  return { comparablesCount, warningFlags, confidenceScore, confidenceLabel };
}
