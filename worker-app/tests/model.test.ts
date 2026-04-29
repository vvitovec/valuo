import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { featureEffectsFromScore, scoreModel } from "../src/model";
import type { FeaturePayload } from "../src/model";
import type { ExportedModel } from "../src/types";

const root = resolve(import.meta.dirname, "..", "..");
const activeModel = JSON.parse(
  readFileSync(resolve(root, "artifacts", "active-model.json"), "utf-8")
) as ExportedModel;
const parityFixtures = JSON.parse(
  readFileSync(resolve(root, "artifacts", "scorer-parity-fixtures.json"), "utf-8")
) as {
  fixtures: Array<{
    input: FeaturePayload;
    expectedPriceCzk: number;
    expectedContributionScores: Array<{ featureName: string; score: number }>;
  }>;
};

describe("worker scorer parity", () => {
  it("matches exported Python fixtures", () => {
    for (const fixture of parityFixtures.fixtures) {
      const scored = scoreModel(activeModel, fixture.input);
      expect(scored.estimatedPriceCzk).toBeCloseTo(fixture.expectedPriceCzk, 6);
      if ("contributions" in scored) {
        expect(scored.contributions).toHaveLength(fixture.expectedContributionScores.length);
        scored.contributions.forEach((entry, index) => {
          const expectedEntry = fixture.expectedContributionScores[index];
          expect(entry.featureName).toBe(expectedEntry?.featureName);
          expect(entry.score).toBeCloseTo(expectedEntry?.score ?? 0, 6);
        });
      }
    }
  });
});

describe("worker scorer model kinds", () => {
  it("scores blended EBM models and exposes direct impact contributions", () => {
    const payload: FeaturePayload = {
      district_prague: "Praha 9",
      property_type: "flat",
      disposition: "2+kk",
      floor_area_m2: 50,
      land_area_m2: 0,
      floor_no: 2,
      total_floors: 5,
      ownership: "osobni",
      condition: "good",
      construction: "brick",
      energy_label: "c",
      has_elevator: "yes",
      has_parking: "no",
      has_cellar: "no",
      has_balcony_or_loggia: "yes",
      distance_to_center_km: 6,
      market_segment: "prague",
      location_cluster: "Praha 9",
      room_count: 2,
      area_per_room_m2: 25,
      floor_position_ratio: 0.4,
      model_segment: "prague_flat"
    };
    const blendedModel: ExportedModel = {
      kind: "blended_ebm_regressor",
      version: "test",
      trainedAt: "2026-03-30T00:00:00Z",
      target: "asking_price",
      metrics: {},
      curatedRowCount: 2,
      baselineLookup: { "fallback|all": 100000 },
      residualQuantiles: { low: -0.1, high: 0.1 },
      featureOrder: ["floor_area_m2"],
      notes: [],
      blendWeights: { price: 0.4, pricePerM2: 0.6 },
      priceModel: {
        kind: "ebm_regressor",
        targetScale: "price",
        intercept: Math.log(5_000_000),
        featureOrder: ["floor_area_m2"],
        terms: [
          {
            featureName: "floor_area_m2",
            featureType: "continuous",
            missingScore: 0,
            unknownScore: 0,
            bins: [{ upperBound: null, score: 0 }]
          }
        ]
      },
      ppmModel: {
        kind: "ebm_regressor",
        targetScale: "price_per_m2",
        intercept: Math.log(120_000),
        featureOrder: ["floor_area_m2"],
        terms: [
          {
            featureName: "floor_area_m2",
            featureType: "continuous",
            missingScore: 0,
            unknownScore: 0,
            bins: [{ upperBound: null, score: 0 }]
          }
        ]
      }
    };

    const scored = scoreModel(blendedModel, payload);
    expect(scored.estimatedPriceCzk).toBeCloseTo(5_600_000, 6);
    const effects = featureEffectsFromScore(blendedModel, scored);
    expect(effects[0]?.featureName).toBe("floor_area_m2");
    expect(Math.abs(effects[0]?.impactCzk ?? 0)).toBe(0);
  });
});
