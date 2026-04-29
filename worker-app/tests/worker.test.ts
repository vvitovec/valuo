import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import worker, { __resetWorkerCachesForTests } from "../src/index";

const root = resolve(import.meta.dirname, "..");
const modelJson = readFileSync(
  resolve(root, "public", "models", "active-model.json"),
  "utf-8"
);
const registryPath = resolve(root, "public", "manifests", "model-registry.json");
const registryJson = existsSync(registryPath)
  ? readFileSync(registryPath, "utf-8")
  : JSON.stringify({
      activeModelVersion: JSON.parse(modelJson).version,
      lastPromotedAt: JSON.parse(modelJson).trainedAt,
      entries: []
    });
const indexHtml = readFileSync(resolve(root, "public", "index.html"), "utf-8");
const activeModelVersion = JSON.parse(modelJson).version as string;

function inputUrl(input: string | Request | URL): string {
  if (typeof input === "string") {
    return input;
  }
  if (input instanceof URL) {
    return input.toString();
  }
  return input.url;
}

type MockOpportunity = {
  source: string;
  source_listing_id: string;
  discovered_at: string;
  observed_at: string;
  listing_url: string;
  address_text: string;
  district_prague: string;
  property_type: string;
  asking_price_czk: number;
  predicted_price_czk: number;
  typical_range_low_czk: number;
  typical_range_high_czk: number;
  deviation_czk: number;
  deviation_pct: number;
  market_position: "under_market" | "within_range" | "over_market";
  opportunity_score: number;
  listing_quality_score?: number;
  quality_flags?: string[];
  comparables_count?: number;
  confidence_score?: number;
  is_filtered_default?: boolean;
  filter_reasons?: string[];
  warning_flags?: string[];
};

class MockPreparedStatement {
  values: unknown[] = [];

  constructor(
    private readonly db: MockDb,
    private readonly query: string
  ) {}

  bind(...values: unknown[]) {
    this.values = values;
    return this;
  }

  async first<T>() {
    return this.db.first(this.query, this.values) as T | null;
  }

  async all<T>() {
    return { results: this.db.all(this.query, this.values) as T[] };
  }

  async run() {
    this.db.run(this.query, this.values);
    return { success: true };
  }
}

class MockDb {
  geocodeCache = new Map<string, { lat: number; lng: number; district_prague: string }>();
  usageSuccessCount = new Map<string, number>();
  entitlements = new Map<
    string,
    {
      status: string;
      stripeCustomerId: string | null;
      stripeSubscriptionId: string | null;
      planCode: string | null;
      currentPeriodEnd: string | null;
      cancelAtPeriodEnd: number;
    }
  >();
  billingByUser = new Map<string, { stripeCustomerId: string; email: string | null }>();
  billingByCustomer = new Map<string, { userId: string; email: string | null }>();
  pipelineRuns: Array<{
    runId: string;
    runType: string;
    status: string;
    startedAt: string;
    finishedAt: string;
    modelVersionBefore: string | null;
    modelVersionAfter: string | null;
    summaryJson: string | null;
    errorJson: string | null;
  }>;

  constructor(
    freeUsed: number,
    premium: boolean,
    readonly opportunities: MockOpportunity[],
    pipelineRuns?: Array<{
      runId: string;
      runType: string;
      status: string;
      startedAt: string;
      finishedAt: string;
      modelVersionBefore?: string | null;
      modelVersionAfter?: string | null;
      summary?: Record<string, unknown>;
      error?: Record<string, unknown> | null;
    }>
  ) {
    this.usageSuccessCount.set("user-123", freeUsed);
    if (premium) {
      this.entitlements.set("user-123", {
        status: "active",
        stripeCustomerId: "cus_existing",
        stripeSubscriptionId: "sub_existing",
        planCode: "premium_monthly",
        currentPeriodEnd: new Date(Date.now() + 10 * 24 * 60 * 60 * 1000).toISOString(),
        cancelAtPeriodEnd: 0
      });
    }
    const now = new Date().toISOString();
    this.pipelineRuns = (pipelineRuns ?? [
      {
        runId: "run-publish",
        runType: "publish",
        status: "success",
        startedAt: now,
        finishedAt: now,
        modelVersionBefore: activeModelVersion,
        modelVersionAfter: activeModelVersion,
        summaryJson: JSON.stringify({ activeModelVersion, marketOpportunitiesRows: this.opportunities.length }),
        errorJson: null
      },
      {
        runId: "run-train",
        runType: "train",
        status: "success",
        startedAt: now,
        finishedAt: now,
        modelVersionBefore: activeModelVersion,
        modelVersionAfter: activeModelVersion,
        summaryJson: JSON.stringify({ activeModelVersion }),
        errorJson: null
      },
      {
        runId: "run-scrape",
        runType: "scrape",
        status: "success",
        startedAt: now,
        finishedAt: now,
        modelVersionBefore: activeModelVersion,
        modelVersionAfter: activeModelVersion,
        summaryJson: JSON.stringify({ degradedSources: [] }),
        errorJson: null
      }
    ]).map((run) => ({
      runId: run.runId,
      runType: run.runType,
      status: run.status,
      startedAt: run.startedAt,
      finishedAt: run.finishedAt,
      modelVersionBefore: run.modelVersionBefore ?? null,
      modelVersionAfter: run.modelVersionAfter ?? null,
      summaryJson: "summaryJson" in run ? run.summaryJson : JSON.stringify(run.summary ?? {}),
      errorJson: "errorJson" in run ? run.errorJson : (run.error ? JSON.stringify(run.error) : null)
    }));
  }

  prepare(query: string) {
    return new MockPreparedStatement(this, query);
  }

  private normalize(query: string) {
    return query.replace(/\s+/g, " ").trim().toLowerCase();
  }

  private matchesSince(dateIso: string, since: string) {
    return dateIso >= since;
  }

  first(query: string, values: unknown[]) {
    const normalized = this.normalize(query);
    if (normalized === "select 1") {
      return { 1: 1 };
    }
    if (normalized.includes("from geocode_cache")) {
      return this.geocodeCache.get(String(values[0])) ?? null;
    }
    if (normalized.includes("from subscription_entitlement")) {
      const entitlement = this.entitlements.get(String(values[0]));
      return (
        entitlement && {
          userId: String(values[0]),
          stripeCustomerId: entitlement.stripeCustomerId,
          stripeSubscriptionId: entitlement.stripeSubscriptionId,
          planCode: entitlement.planCode,
          status: entitlement.status,
          currentPeriodEnd: entitlement.currentPeriodEnd,
          cancelAtPeriodEnd: entitlement.cancelAtPeriodEnd
        }
      );
    }
    if (normalized.includes("count(*) as total") && normalized.includes("from prediction_usage_event")) {
      return { total: this.usageSuccessCount.get(String(values[0])) ?? 0 };
    }
    if (normalized.includes("from billing_customer_map") && normalized.includes("where user_id")) {
      const item = this.billingByUser.get(String(values[0]));
      return item
        ? {
            userId: String(values[0]),
            stripeCustomerId: item.stripeCustomerId,
            email: item.email
          }
        : null;
    }
    if (normalized.includes("from billing_customer_map") && normalized.includes("where stripe_customer_id")) {
      const item = this.billingByCustomer.get(String(values[0]));
      return item
        ? {
            userId: item.userId,
            stripeCustomerId: String(values[0]),
            email: item.email
          }
        : null;
    }
    if (normalized.includes("from market_listing_score") && normalized.includes("count(*) as total")) {
      const total = this.opportunities.filter(
        (row) =>
          this.matchesSince(row.discovered_at, String(values[0])) &&
          row.market_position === values[1]
      ).length;
      return { total };
    }
    return null;
  }

  all(query: string, values: unknown[]) {
    const normalized = this.normalize(query);
    if (normalized.includes("from pipeline_run_registry")) {
      return [...this.pipelineRuns]
        .sort((left, right) => right.finishedAt.localeCompare(left.finishedAt));
    }
    if (normalized.includes("group by district_prague")) {
      const since = String(values[0]);
      const counts = new Map<string, number>();
      for (const row of this.opportunities) {
        if (!this.matchesSince(row.discovered_at, since) || row.market_position === "within_range") {
          continue;
        }
        counts.set(row.district_prague, (counts.get(row.district_prague) ?? 0) + 1);
      }
      return [...counts.entries()]
        .map(([districtPrague, total]) => ({ districtPrague, total }))
        .sort((left, right) => right.total - left.total || left.districtPrague.localeCompare(right.districtPrague))
        .slice(0, 5);
    }
    if (normalized.includes("from market_listing_score")) {
      const since = String(values[0]);
      const marketPosition = String(values[1]);
      let index = 2;
      let district = "";
      let propertyType = "";
      let source = "";
      const excludeFiltered = normalized.includes("coalesce(is_filtered_default, 0) = 0");

      if (normalized.includes("district_prague =")) {
        district = String(values[index++]);
      }
      if (normalized.includes("property_type =")) {
        propertyType = String(values[index++]);
      }
      if (normalized.includes("source =")) {
        source = String(values[index++]);
      }
      const limit = Number(values[index] ?? 50);

      return this.opportunities
        .filter((row) => this.matchesSince(row.discovered_at, since))
        .filter((row) => row.market_position === marketPosition)
        .filter((row) => !district || row.district_prague === district)
        .filter((row) => !propertyType || row.property_type === propertyType)
        .filter((row) => !source || row.source === source)
        .filter((row) => !excludeFiltered || !row.is_filtered_default)
        .sort((left, right) => right.opportunity_score - left.opportunity_score || Math.abs(right.deviation_czk) - Math.abs(left.deviation_czk))
        .slice(0, limit)
        .map((row) => ({
          source: row.source,
          sourceListingId: row.source_listing_id,
          discoveredAt: row.discovered_at,
          observedAt: row.observed_at,
          listingUrl: row.listing_url,
          addressText: row.address_text,
          districtPrague: row.district_prague,
          propertyType: row.property_type,
          askingPriceCzk: row.asking_price_czk,
          predictedPriceCzk: row.predicted_price_czk,
          typicalRangeLowCzk: row.typical_range_low_czk,
          typicalRangeHighCzk: row.typical_range_high_czk,
          deviationCzk: row.deviation_czk,
          deviationPct: row.deviation_pct,
          marketPosition: row.market_position,
          opportunityScore: row.opportunity_score,
          listingQualityScore: row.listing_quality_score ?? 1,
          qualityFlags: row.quality_flags ?? [],
          comparablesCount: row.comparables_count ?? 0,
          confidenceScore: row.confidence_score ?? 1,
          isFilteredDefault: Boolean(row.is_filtered_default),
          filterReasons: row.filter_reasons ?? [],
          warningFlags: row.warning_flags ?? []
        }));
    }
    return [];
  }

  run(query: string, values: unknown[]) {
    const normalized = this.normalize(query);
    if (normalized.includes("insert into geocode_cache")) {
      this.geocodeCache.set(String(values[0]), {
        lat: Number(values[1]),
        lng: Number(values[2]),
        district_prague: String(values[3])
      });
      return;
    }
    if (normalized.includes("insert into prediction_usage_event")) {
      const userId = String(values[0]);
      const eventKind = String(values[2]);
      if (eventKind === "prediction_success") {
        this.usageSuccessCount.set(userId, (this.usageSuccessCount.get(userId) ?? 0) + 1);
      }
      return;
    }
    if (normalized.includes("insert into billing_customer_map")) {
      const userId = String(values[0]);
      const stripeCustomerId = String(values[1]);
      const email = values[2] == null ? null : String(values[2]);
      this.billingByUser.set(userId, { stripeCustomerId, email });
      this.billingByCustomer.set(stripeCustomerId, { userId, email });
      return;
    }
    if (normalized.includes("insert into subscription_entitlement")) {
      const userId = String(values[0]);
      this.entitlements.set(userId, {
        stripeCustomerId: values[1] == null ? null : String(values[1]),
        stripeSubscriptionId: values[2] == null ? null : String(values[2]),
        planCode: values[3] == null ? null : String(values[3]),
        status: String(values[4]),
        currentPeriodEnd: values[5] == null ? null : String(values[5]),
        cancelAtPeriodEnd: Number(values[6] ?? 0)
      });
      return;
    }
    if (normalized.includes("delete from prediction_usage_event")) {
      this.usageSuccessCount.delete(String(values[0]));
      return;
    }
    if (normalized.includes("delete from subscription_entitlement")) {
      this.entitlements.delete(String(values[0]));
      return;
    }
    if (normalized.includes("delete from billing_customer_map")) {
      const userId = String(values[0]);
      const item = this.billingByUser.get(userId);
      if (item) {
        this.billingByCustomer.delete(item.stripeCustomerId);
      }
      this.billingByUser.delete(userId);
      return;
    }
    if (normalized.includes("delete from user_profile")) {
      return;
    }
  }
}

function makeEnv(options?: {
  freeUsed?: number;
  premium?: boolean;
  opportunities?: MockOpportunity[];
  pipelineRuns?: ConstructorParameters<typeof MockDb>[3];
  r2ModelJson?: string | null;
  r2RegistryJson?: string | null;
  supabaseUrl?: string;
  supabaseAnonKey?: string;
  appBaseUrl?: string;
}) {
  const db = new MockDb(
    options?.freeUsed ?? 0,
    options?.premium ?? false,
    options?.opportunities ?? [],
    options?.pipelineRuns
  );

  return {
    ASSETS: {
      fetch: vi.fn(async (request: Request | string) => {
        const url = new URL(typeof request === "string" ? request : request.url);
        if (url.pathname === "/models/active-model.json") {
          return new Response(modelJson, {
            headers: { "content-type": "application/json" }
          });
        }
        if (url.pathname === "/manifests/model-registry.json") {
          return new Response(registryJson, {
            headers: { "content-type": "application/json" }
          });
        }
        return new Response(indexHtml, {
          headers: { "content-type": "text/html; charset=utf-8" }
        });
      }),
      connect: vi.fn()
    },
    MODEL_BUCKET: options?.r2ModelJson || options?.r2RegistryJson
      ? {
          get: vi.fn(async (key: string) => {
            if (key === "active-model.json" && options?.r2ModelJson) {
              return {
                json: async () => JSON.parse(options.r2ModelJson ?? "null")
              };
            }
            if (key === "model-registry.json" && options?.r2RegistryJson) {
              return {
                json: async () => JSON.parse(options.r2RegistryJson ?? "null")
              };
            }
            return null;
          })
        } as unknown as R2Bucket
      : undefined,
    DB: db as unknown as D1Database,
    SUPABASE_URL: options?.supabaseUrl ?? "https://example.supabase.co",
    SUPABASE_ANON_KEY: options?.supabaseAnonKey ?? "sb_publishable_test",
    SUPABASE_SERVICE_ROLE_KEY: "sb_service_role_test",
    STRIPE_SECRET_KEY: "stripe_secret_test",
    STRIPE_PRICE_ID: "price_test_123",
    PREMIUM_PRICE_LABEL: "490 Kč / měsíc",
    APP_BASE_URL: options?.appBaseUrl
  };
}

function makeRequest(input: string, init?: RequestInit) {
  return new Request(input, init) as unknown as Request<
    unknown,
    IncomingRequestCfProperties<unknown>
  >;
}

function authHeaders() {
  return {
    authorization: "Bearer valid-token"
  };
}

afterEach(() => {
  __resetWorkerCachesForTests();
  vi.restoreAllMocks();
});

describe("worker app", () => {
  it("requires authentication for account endpoints", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));

    const response = await worker.fetch(
      makeRequest("https://example.com/api/me"),
      makeEnv(),
      {} as ExecutionContext
    );

    expect(response.status).toBe(401);
  });

  it("validates invalid prediction input for authenticated users", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input) => {
        const url = typeof input === "string" ? input : input.url;
        if (url.includes("/auth/v1/user")) {
          return new Response(JSON.stringify({ id: "user-123", email: "user@example.com" }), {
            headers: { "content-type": "application/json" }
          });
        }
        return new Response("[]", { headers: { "content-type": "application/json" } });
      })
    );

    const response = await worker.fetch(
      makeRequest("https://example.com/api/predict", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          ...authHeaders()
        },
        body: JSON.stringify({ address: "", districtPrague: "" })
      }),
      makeEnv(),
      {} as ExecutionContext
    );

    expect(response.status).toBe(400);
  });

  it("returns a prediction response and tracks free quota", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input) => {
        const url = typeof input === "string" ? input : input.url;
        if (url.includes("/auth/v1/user")) {
          return new Response(JSON.stringify({ id: "user-123", email: "user@example.com" }), {
            headers: { "content-type": "application/json" }
          });
        }
        return new Response(
          JSON.stringify([
            {
              lat: "50.1026895",
              lon: "14.508437",
              address: { city_district: "Praha - Vysočany" }
            }
          ]),
          { headers: { "content-type": "application/json" } }
        );
      })
    );

    const response = await worker.fetch(
      makeRequest("https://example.com/api/predict", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          ...authHeaders()
        },
        body: JSON.stringify({
          address: "Poděbradská 777/9",
          districtPrague: "Vysočany",
          propertyType: "flat",
          experienceMode: "pricing",
          disposition: "1+kk",
          floorAreaM2: 34,
          condition: "very_good",
          ownership: "osobni",
          construction: "mixed",
          askingPriceCzk: 6400000
        })
      }),
      makeEnv({ freeUsed: 0 }),
      {} as ExecutionContext
    );

    expect(response.status).toBe(200);
    const body = (await response.json()) as Record<string, unknown>;
    expect(body.modelVersion).toBeTypeOf("string");
    expect(body.estimatedPriceCzk).toBeTypeOf("number");
    expect(body.resolvedDistrictPrague).toBe("Praha 9");
    expect(body.resolvedLocationCluster).toBe("Praha 9");
    expect(body.confidenceScore).toBeTypeOf("number");
    expect(body.warningFlags).toBeInstanceOf(Array);
    expect((body.usage as { freeRemaining: number }).freeRemaining).toBe(2);
  });

  it("blocks prediction when free quota is exhausted", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input) => {
        const url = typeof input === "string" ? input : input.url;
        if (url.includes("/auth/v1/user")) {
          return new Response(JSON.stringify({ id: "user-123", email: "user@example.com" }), {
            headers: { "content-type": "application/json" }
          });
        }
        return new Response("[]", { headers: { "content-type": "application/json" } });
      })
    );

    const response = await worker.fetch(
      makeRequest("https://example.com/api/predict", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          ...authHeaders()
        },
        body: JSON.stringify({
          address: "Poděbradská 777/9",
          districtPrague: "Vysočany",
          propertyType: "flat",
          experienceMode: "insight",
          disposition: "1+kk",
          floorAreaM2: 34
        })
      }),
      makeEnv({ freeUsed: 3 }),
      {} as ExecutionContext
    );

    expect(response.status).toBe(402);
    const body = (await response.json()) as Record<string, unknown>;
    expect(body.code).toBe("UPGRADE_REQUIRED");
  });

  it("serves the new public landing", async () => {
    const env = makeEnv();
    const response = await worker.fetch(
      makeRequest("https://example.com/"),
      env,
      {} as ExecutionContext
    );
    expect(response.status).toBe(200);
    expect(await response.text()).toContain('<div id="app"></div>');
    expect(env.ASSETS.fetch).toHaveBeenCalled();
  });

  it("returns teaser aggregates for public dashboard preview", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}")));
    const now = new Date().toISOString();
    const env = makeEnv({
      opportunities: [
        {
          source: "realitymix",
          source_listing_id: "123",
          discovered_at: now,
          observed_at: now,
          listing_url: "https://example.com/listing-1",
          address_text: "Praha 4, Háje",
          district_prague: "Praha 4",
          property_type: "flat",
          asking_price_czk: 6000000,
          predicted_price_czk: 7000000,
          typical_range_low_czk: 6500000,
          typical_range_high_czk: 7400000,
          deviation_czk: -1000000,
          deviation_pct: -0.14,
          market_position: "under_market",
          opportunity_score: 0.14
        },
        {
          source: "remax",
          source_listing_id: "456",
          discovered_at: now,
          observed_at: now,
          listing_url: "https://example.com/listing-2",
          address_text: "Praha 6, Dejvice",
          district_prague: "Praha 6",
          property_type: "flat",
          asking_price_czk: 15000000,
          predicted_price_czk: 12000000,
          typical_range_low_czk: 11000000,
          typical_range_high_czk: 13200000,
          deviation_czk: 3000000,
          deviation_pct: 0.25,
          market_position: "over_market",
          opportunity_score: 0.25
        }
      ]
    });

    const response = await worker.fetch(
      makeRequest("https://example.com/api/dashboard/teaser"),
      env,
      {} as ExecutionContext
    );

    expect(response.status).toBe(200);
    const body = (await response.json()) as {
      summary: Array<{ underCount: number; overCount: number }>;
      freshness: { generatedAt: string | null; isStale: boolean };
    };
    expect(body.summary[0].underCount).toBe(1);
    expect(body.summary[0].overCount).toBe(1);
    expect(body.freshness.isStale).toBe(false);
    expect(body.freshness.generatedAt).toBeTruthy();
  });

  it("marks teaser freshness as stale when the latest scrape was degraded", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}")));
    const staleTimestamp = new Date().toISOString();
    const env = makeEnv({
      pipelineRuns: [
        {
          runId: "run-publish",
          runType: "publish",
          status: "success",
          startedAt: staleTimestamp,
          finishedAt: staleTimestamp,
          modelVersionBefore: activeModelVersion,
          modelVersionAfter: activeModelVersion,
          summary: {}
        },
        {
          runId: "run-scrape",
          runType: "scrape",
          status: "degraded",
          startedAt: staleTimestamp,
          finishedAt: staleTimestamp,
          summary: { degradedSources: ["remax"] }
        }
      ]
    });

    const response = await worker.fetch(
      makeRequest("https://example.com/api/dashboard/teaser"),
      env,
      {} as ExecutionContext
    );

    expect(response.status).toBe(200);
    const body = (await response.json()) as {
      freshness: { isStale: boolean; staleReason: string | null; degradedSources: string[] };
    };
    expect(body.freshness.isStale).toBe(true);
    expect(body.freshness.staleReason).toBe("source_guardrail_failed");
    expect(body.freshness.degradedSources).toContain("remax");
  });

  it("prefers the R2 model and exposes runtime freshness in health", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}")));
    const r2Model = JSON.stringify({
      ...JSON.parse(modelJson),
      version: "model-r2-test"
    });
    const r2Registry = JSON.stringify({
      activeModelVersion: "model-r2-test",
      lastPromotedAt: "2026-04-18T00:00:00Z",
      entries: []
    });
    const env = makeEnv({
      r2ModelJson: r2Model,
      r2RegistryJson: r2Registry
    });

    const response = await worker.fetch(
      makeRequest("https://example.com/api/health"),
      env,
      {} as ExecutionContext
    );

    expect(response.status).toBe(200);
    const body = (await response.json()) as {
      activeModelVersion: string;
      modelSource: string;
      lastSuccessfulScrapeAt: string | null;
      lastSuccessfulTrainAt: string | null;
      lastSuccessfulPublishAt: string | null;
      dataStale: boolean;
      modelStale: boolean;
    };
    expect(body.activeModelVersion).toBe("model-r2-test");
    expect(body.modelSource).toBe("r2");
    expect(body.lastSuccessfulScrapeAt).toBeTruthy();
    expect(body.lastSuccessfulTrainAt).toBeTruthy();
    expect(body.lastSuccessfulPublishAt).toBeTruthy();
    expect(body.dataStale).toBe(false);
    expect(body.modelStale).toBe(false);
  });

  it("returns premium dashboard opportunities for paying users", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input) => {
        const url = typeof input === "string" ? input : input.url;
        if (url.includes("/auth/v1/user")) {
          return new Response(JSON.stringify({ id: "user-123", email: "user@example.com" }), {
            headers: { "content-type": "application/json" }
          });
        }
        return new Response("{}");
      })
    );
    const now = new Date().toISOString();
    const env = makeEnv({
      premium: true,
      opportunities: [
        {
          source: "realitymix",
          source_listing_id: "123",
          discovered_at: now,
          observed_at: now,
          listing_url: "https://example.com/listing-1",
          address_text: "Praha 4, Háje",
          district_prague: "Praha 4",
          property_type: "flat",
          asking_price_czk: 6000000,
          predicted_price_czk: 7000000,
          typical_range_low_czk: 6500000,
          typical_range_high_czk: 7400000,
          deviation_czk: -1000000,
          deviation_pct: -0.14,
          market_position: "under_market",
          opportunity_score: 0.14,
          listing_quality_score: 0.82,
          quality_flags: [],
          comparables_count: 7,
          confidence_score: 0.84,
          is_filtered_default: false,
          filter_reasons: [],
          warning_flags: []
        }
      ]
    });

    const response = await worker.fetch(
      makeRequest("https://example.com/api/dashboard/opportunities?window=1d&direction=under", {
        headers: authHeaders()
      }),
      env,
      {} as ExecutionContext
    );

    expect(response.status).toBe(200);
    const body = (await response.json()) as { opportunities: Array<{ addressText: string; confidenceScore: number }> };
    expect(body.opportunities).toHaveLength(1);
    expect(body.opportunities[0].addressText).toContain("Praha 4");
    expect(body.opportunities[0].confidenceScore).toBeGreaterThan(0);
  });

  it("hides filtered opportunities by default and returns them when requested", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input) => {
        const url = typeof input === "string" ? input : input.url;
        if (url.includes("/auth/v1/user")) {
          return new Response(JSON.stringify({ id: "user-123", email: "user@example.com" }), {
            headers: { "content-type": "application/json" }
          });
        }
        return new Response("{}");
      })
    );
    const now = new Date().toISOString();
    const env = makeEnv({
      premium: true,
      opportunities: [
        {
          source: "realitymix",
          source_listing_id: "visible",
          discovered_at: now,
          observed_at: now,
          listing_url: "https://example.com/visible",
          address_text: "Praha 4, Háje",
          district_prague: "Praha 4",
          property_type: "flat",
          asking_price_czk: 6000000,
          predicted_price_czk: 7000000,
          typical_range_low_czk: 6500000,
          typical_range_high_czk: 7400000,
          deviation_czk: -1000000,
          deviation_pct: -0.14,
          market_position: "under_market",
          opportunity_score: 0.14,
          is_filtered_default: false
        },
        {
          source: "realitymix",
          source_listing_id: "filtered",
          discovered_at: now,
          observed_at: now,
          listing_url: "https://example.com/filtered",
          address_text: "Praha 9, Nejasná",
          district_prague: "Praha 9",
          property_type: "flat",
          asking_price_czk: 5000000,
          predicted_price_czk: 8000000,
          typical_range_low_czk: 6400000,
          typical_range_high_czk: 9000000,
          deviation_czk: -3000000,
          deviation_pct: -0.375,
          market_position: "under_market",
          opportunity_score: 0.375,
          is_filtered_default: true,
          filter_reasons: ["geocode_fallback"]
        }
      ]
    });

    const defaultResponse = await worker.fetch(
      makeRequest("https://example.com/api/dashboard/opportunities?window=1d&direction=under", {
        headers: authHeaders()
      }),
      env,
      {} as ExecutionContext
    );
    const defaultBody = (await defaultResponse.json()) as { opportunities: Array<{ addressText: string }> };
    expect(defaultBody.opportunities).toHaveLength(1);

    const allResponse = await worker.fetch(
      makeRequest("https://example.com/api/dashboard/opportunities?window=1d&direction=under&includeFiltered=true", {
        headers: authHeaders()
      }),
      env,
      {} as ExecutionContext
    );
    const allBody = (await allResponse.json()) as { opportunities: Array<{ addressText: string }> };
    expect(allBody.opportunities).toHaveLength(2);
  });

  it("creates a Stripe checkout session for authenticated users", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input, init) => {
        const url = typeof input === "string" ? input : input.url;
        if (url.includes("/auth/v1/user")) {
          return new Response(JSON.stringify({ id: "user-123", email: "user@example.com" }), {
            headers: { "content-type": "application/json" }
          });
        }
        if (url === "https://api.stripe.com/v1/customers") {
          return new Response(JSON.stringify({ id: "cus_test_123" }), {
            headers: { "content-type": "application/json" }
          });
        }
        if (url === "https://api.stripe.com/v1/checkout/sessions") {
          expect(init?.body).toBeTypeOf("string");
          return new Response(JSON.stringify({ url: "https://checkout.stripe.com/c/pay/cs_test_123" }), {
            headers: { "content-type": "application/json" }
          });
        }
        return new Response("{}");
      })
    );

    const response = await worker.fetch(
      makeRequest("https://example.com/api/billing/create-checkout-session", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          ...authHeaders()
        },
        body: "{}"
      }),
      makeEnv(),
      {} as ExecutionContext
    );

    expect(response.status).toBe(200);
    const body = (await response.json()) as { url: string };
    expect(body.url).toContain("checkout.stripe.com");
  });

  it("creates a Stripe billing portal session for premium users", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input) => {
        const url = typeof input === "string" ? input : input.url;
        if (url.includes("/auth/v1/user")) {
          return new Response(JSON.stringify({ id: "user-123", email: "user@example.com" }), {
            headers: { "content-type": "application/json" }
          });
        }
        if (url === "https://api.stripe.com/v1/customers") {
          return new Response(JSON.stringify({ id: "cus_test_123" }), {
            headers: { "content-type": "application/json" }
          });
        }
        if (url === "https://api.stripe.com/v1/billing_portal/sessions") {
          return new Response(JSON.stringify({ url: "https://billing.stripe.com/p/session/test_123" }), {
            headers: { "content-type": "application/json" }
          });
        }
        return new Response("{}");
      })
    );

    const response = await worker.fetch(
      makeRequest("https://example.com/api/billing/create-portal-session", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          ...authHeaders()
        },
        body: "{}"
      }),
      makeEnv({ premium: true }),
      {} as ExecutionContext
    );

    expect(response.status).toBe(200);
    const body = (await response.json()) as { url: string };
    expect(body.url).toContain("billing.stripe.com");
  });

  it("deletes the authenticated account after confirmation", async () => {
    const fetchMock = vi.fn(async (input, init) => {
      const url = typeof input === "string" ? input : input.url;
      if (url.includes("/auth/v1/user")) {
        return new Response(JSON.stringify({ id: "user-123", email: "user@example.com" }), {
          headers: { "content-type": "application/json" }
        });
      }
      if (url === "https://example.supabase.co/auth/v1/admin/users/user-123") {
        expect(init?.method).toBe("DELETE");
        return new Response(null, { status: 204 });
      }
      return new Response("{}");
    });
    vi.stubGlobal("fetch", fetchMock);

    const env = makeEnv({ freeUsed: 2, premium: true });
    const response = await worker.fetch(
      makeRequest("https://example.com/api/account/delete", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          ...authHeaders()
        },
        body: JSON.stringify({ confirmation: "user@example.com" })
      }),
      env,
      {} as ExecutionContext
    );

    expect(response.status).toBe(200);
    const db = env.DB as unknown as MockDb;
    expect(db.usageSuccessCount.has("user-123")).toBe(false);
    expect(db.entitlements.has("user-123")).toBe(false);
    expect(db.billingByUser.has("user-123")).toBe(false);
  });

  it("updates entitlements from Stripe webhooks", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input) => {
        const url = typeof input === "string" ? input : input.url;
        if (url.includes("/auth/v1/user")) {
          return new Response(JSON.stringify({ id: "user-123", email: "user@example.com" }), {
            headers: { "content-type": "application/json" }
          });
        }
        return new Response("{}");
      })
    );

    const env = makeEnv();
    const db = env.DB as unknown as MockDb;
    db.billingByUser.set("user-123", { stripeCustomerId: "cus_test_123", email: "user@example.com" });
    db.billingByCustomer.set("cus_test_123", { userId: "user-123", email: "user@example.com" });

    const webhookResponse = await worker.fetch(
      makeRequest("https://example.com/api/billing/webhook", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          type: "customer.subscription.updated",
          data: {
            object: {
              id: "sub_test_123",
              customer: "cus_test_123",
              status: "active",
              current_period_end: Math.floor(Date.now() / 1000) + 86400,
              cancel_at_period_end: false
            }
          }
        })
      }),
      env,
      {} as ExecutionContext
    );

    expect(webhookResponse.status).toBe(200);

    const meResponse = await worker.fetch(
      makeRequest("https://example.com/api/me", {
        headers: authHeaders()
      }),
      env,
      {} as ExecutionContext
    );

    const me = (await meResponse.json()) as { usage: { premium: boolean } };
    expect(me.usage.premium).toBe(true);
  });

  it("uses APP_BASE_URL in config and reports auth availability", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input) => {
        const url = inputUrl(input as string | Request | URL);
        if (url === "https://example.supabase.co/auth/v1/settings") {
          return new Response("{}", {
            headers: { "content-type": "application/json" }
          });
        }
        throw new Error(`Unexpected fetch ${url}`);
      })
    );

    const response = await worker.fetch(
      makeRequest("https://example.com/api/config"),
      makeEnv({ appBaseUrl: "https://valuo.vvitovec.com" }),
      {} as ExecutionContext
    );

    expect(response.status).toBe(200);
    const body = (await response.json()) as {
      appBaseUrl: string;
      auth: { configured: boolean; reason: string; message: string | null };
    };
    expect(body.appBaseUrl).toBe("https://valuo.vvitovec.com");
    expect(body.auth.configured).toBe(true);
    expect(body.auth.reason).toBe("ready");
    expect(body.auth.message).toBeNull();
  });

  it("reports unreachable Supabase auth in config", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new TypeError("fetch failed");
    }));

    const response = await worker.fetch(
      makeRequest("https://example.com/api/config"),
      makeEnv(),
      {} as ExecutionContext
    );

    expect(response.status).toBe(200);
    const body = (await response.json()) as {
      auth: { configured: boolean; reason: string; message: string | null };
    };
    expect(body.auth.configured).toBe(false);
    expect(body.auth.reason).toBe("unreachable");
    expect(body.auth.message).toContain("SUPABASE_URL");
  });

  it("returns health status", async () => {
    const response = await worker.fetch(
      makeRequest("https://example.com/api/health"),
      makeEnv(),
      {} as ExecutionContext
    );
    expect(response.status).toBe(200);
    const body = (await response.json()) as Record<string, unknown>;
    expect(body.status).toBe("ok");
    expect(body.activeModelVersion).toBeTypeOf("string");
    expect(body.bindingStatus).toBeInstanceOf(Object);
  });
});
