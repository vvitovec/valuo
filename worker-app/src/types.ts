import { z } from "zod";

export const predictionRequestSchema = z.object({
  address: z.string().min(3),
  districtPrague: z.string().min(2),
  propertyType: z.enum(["flat", "house"]),
  experienceMode: z.enum(["pricing", "insight"]).default("pricing"),
  disposition: z.string().min(1).default("unknown"),
  floorAreaM2: z.number().positive(),
  landAreaM2: z.number().nonnegative().optional(),
  condition: z.string().default("unknown"),
  ownership: z.string().default("unknown"),
  construction: z.string().default("unknown"),
  floorNo: z.number().optional(),
  totalFloors: z.number().optional(),
  hasElevator: z.boolean().optional(),
  hasParking: z.boolean().optional(),
  hasCellar: z.boolean().optional(),
  hasBalconyOrLoggia: z.boolean().optional(),
  energyLabel: z.string().optional(),
  askingPriceCzk: z.number().positive().optional()
});

export type PredictionRequest = z.infer<typeof predictionRequestSchema>;

export const listingPrefillRequestSchema = z.object({
  url: z.string().trim().url()
});

export type ListingPrefillRequest = z.infer<typeof listingPrefillRequestSchema>;

export const deleteAccountRequestSchema = z.object({
  confirmation: z.string().trim().min(1)
});

export type DeleteAccountRequest = z.infer<typeof deleteAccountRequestSchema>;

export const listingPrefillFieldsSchema = predictionRequestSchema.omit({
  experienceMode: true
}).extend({
  districtPrague: z.string().min(2),
  address: z.string().min(3)
});

export type ListingPrefillFields = z.infer<typeof listingPrefillFieldsSchema>;

export type ListingPrefillResponse = {
  source: "bezrealitky" | "realitymix" | "remax";
  listingUrl: string;
  fields: ListingPrefillFields;
  notes: string[];
};

export type ExperienceMode = "pricing" | "insight";

export type UsageSummary = {
  freeLimit: number;
  freeUsed: number;
  freeRemaining: number | null;
  premium: boolean;
  premiumStatus: string;
  currentPeriodEnd: string | null;
  cancelAtPeriodEnd: boolean;
};

export type BaselineContribution = {
  featureName: string;
  score: number;
  mode: "price_per_m2" | "area_multiplier";
};

export type EBMContribution = {
  featureName: string;
  score: number;
};

export type ExportedModelBase = {
  kind: "baseline_ppm" | "ebm_regressor" | "ebm_ppm_regressor" | "blended_ebm_regressor" | "segmented_ebm_regressor";
  version: string;
  trainedAt: string;
  target: string;
  metrics: Record<string, number | string>;
  curatedRowCount: number;
  trainingWindow?: {
    from: string;
    to: string;
  };
  sourceMix?: Record<string, number>;
  validationSummary?: {
    grouping: string;
    selectedModel: string;
    selectedMae: number;
    finalScore?: number;
    timeHoldoutMape?: number;
    balancedSegmentMape?: number;
    metrics: Record<string, number | string>;
    segmentMapes?: Record<string, number>;
    houseMape?: number | null;
  };
  promotionReason?: string;
  baselineLookup: Record<string, number>;
  baselineLocationFeature?: string;
  residualQuantiles: {
    low: number;
    high: number;
  };
  featureOrder: string[];
  comparableLookup?: ComparableLookupPayload;
  notes: string[];
};

export type ComparableLookupPayload = {
  h3Resolution: number;
  h3PropertyPpm: Record<string, number>;
  h3PropertyCount: Record<string, number>;
  locationPropertyPpm: Record<string, number>;
  districtPropertyPpm: Record<string, number>;
  propertyFallbackPpm: Record<string, number>;
  globalFallbackPpm: number;
};

export type BaselineModel = ExportedModelBase & {
  kind: "baseline_ppm";
};

export type ContinuousTerm = {
  featureName: string;
  featureType: "continuous";
  missingScore: number;
  unknownScore: number;
  bins: Array<{
    upperBound: number | null;
    score: number;
  }>;
};

export type CategoricalTerm = {
  featureName: string;
  featureType: "nominal";
  missingScore: number;
  unknownScore: number;
  categories: Record<string, number>;
};

export type EBMModel = ExportedModelBase & {
  kind: "ebm_regressor";
  intercept: number;
  terms: Array<ContinuousTerm | CategoricalTerm>;
};

export type EBMTermsModel = {
  kind: "ebm_regressor";
  targetScale?: "price" | "price_per_m2";
  intercept: number;
  terms: Array<ContinuousTerm | CategoricalTerm>;
  featureOrder: string[];
};

export type EBMPpmModel = ExportedModelBase & {
  kind: "ebm_ppm_regressor";
  intercept: number;
  terms: Array<ContinuousTerm | CategoricalTerm>;
  targetScale?: "price_per_m2";
};

export type BlendedEBMModel = ExportedModelBase & {
  kind: "blended_ebm_regressor";
  blendWeights: {
    price: number;
    pricePerM2: number;
  };
  priceModel: EBMTermsModel;
  ppmModel: EBMTermsModel;
};

export type SegmentedEBMModel = ExportedModelBase & {
  kind: "segmented_ebm_regressor";
  segmentKeyFeature: string;
  fallbackModel: EBMTermsModel;
  segmentModels: Record<string, EBMTermsModel>;
  segmentCoverage?: Record<string, number>;
};

export type ExportedModel = BaselineModel | EBMModel | EBMPpmModel | BlendedEBMModel | SegmentedEBMModel;

export type PredictionResponse = {
  estimatedPriceCzk: number;
  typicalRangeLowCzk: number;
  typicalRangeHighCzk: number;
  marketPosition: "under_market" | "within_range" | "over_market" | "unknown";
  deltaVsInputPriceCzk: number | null;
  featureEffects: Array<{
    featureName: string;
    impactCzk: number;
    rawScore: number;
  }>;
  modelVersion: string;
  notes: string[];
  resolvedDistrictPrague: string;
  resolvedLocationCluster: string;
  resolvedLat: number | null;
  resolvedLng: number | null;
  confidenceScore: number;
  confidenceLabel: "high" | "medium" | "low";
  inputQualityScore: number;
  comparablesCount: number;
  warningFlags: string[];
  modelFamily: string;
  usage: UsageSummary;
  experienceMode: ExperienceMode;
};

export type MeResponse = {
  user: {
    id: string;
    email: string;
  };
  usage: UsageSummary;
};

export type DashboardOpportunity = {
  source: string;
  sourceListingId: string;
  discoveredAt: string;
  observedAt: string;
  listingUrl: string;
  addressText: string;
  districtPrague: string;
  propertyType: "flat" | "house" | string;
  askingPriceCzk: number;
  predictedPriceCzk: number;
  typicalRangeLowCzk: number;
  typicalRangeHighCzk: number;
  deviationCzk: number;
  deviationPct: number;
  marketPosition: "under_market" | "within_range" | "over_market";
  opportunityScore: number;
  listingQualityScore: number;
  qualityFlags: string[];
  comparablesCount: number;
  confidenceScore: number;
  isFilteredDefault: boolean;
  filterReasons: string[];
  warningFlags: string[];
};

export type DashboardTeaserResponse = {
  summary: Array<{
    window: "1d" | "7d" | "30d";
    underCount: number;
    overCount: number;
    topDistricts: Array<{
      districtPrague: string;
      total: number;
    }>;
  }>;
  freshness: FreshnessInfo;
};

export type PipelineRunSummary = {
  runId: string;
  runType: "scrape" | "train" | "publish" | string;
  status: "success" | "degraded" | "failure" | "skipped" | string;
  startedAt: string;
  finishedAt: string;
  modelVersionBefore: string | null;
  modelVersionAfter: string | null;
  summary: Record<string, unknown>;
  error: Record<string, unknown> | null;
};

export type FreshnessInfo = {
  generatedAt: string | null;
  isStale: boolean;
  isDegraded: boolean;
  staleReason: string | null;
  latestRunStatus: string | null;
  degradedSources: string[];
};

export type ModelSource = "r2" | "bundle";

export type HealthResponse = {
  status: "ok";
  activeModelVersion: string;
  lastModelPromotionTime: string | null;
  bindingStatus: {
    d1: boolean;
    r2: boolean;
  };
  validationSummary: ExportedModelBase["validationSummary"] | Record<string, unknown> | null;
  curatedRowCount: number | null;
  authConfigured: boolean;
  billingConfigured: boolean;
  modelSource: ModelSource;
  lastSuccessfulScrapeAt: string | null;
  lastSuccessfulTrainAt: string | null;
  lastSuccessfulPublishAt: string | null;
  dataStale: boolean;
  modelStale: boolean;
  latestRunStatus: string | null;
};

export type ModelRegistryEntry = {
  version: string;
  trainedAt: string | null;
  modelKind: string;
  curatedRowCount: number;
  trainingWindow?: {
    from: string;
    to: string;
  };
  sourceMix?: Record<string, number>;
  validationSummary?: {
    grouping: string;
    selectedModel: string;
    selectedMae: number;
    metrics: Record<string, number | string>;
  };
  promotionReason?: string;
  promoted: boolean;
  promotedAt: string | null;
  newCuratedRowsSincePreviousActive: number;
  servingEligible?: boolean;
  finalScore?: number;
  timeHoldoutMape?: number;
  balancedSegmentMape?: number;
  readyForBackendServing?: boolean;
  challengers?: Array<Record<string, unknown>>;
};

export type ModelRegistryManifest = {
  activeModelVersion: string | null;
  lastPromotedAt: string | null;
  entries: ModelRegistryEntry[];
};
