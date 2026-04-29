import type {
  DashboardOpportunity,
  DashboardTeaserResponse,
  ExperienceMode,
  FreshnessInfo,
  PipelineRunSummary,
  UsageSummary
} from "./types";

export const FREE_PREDICTION_LIMIT = 3;
const STALE_THRESHOLD_MS = 36 * 60 * 60 * 1000;

export type BillingCustomerMap = {
  userId: string;
  stripeCustomerId: string;
  email: string | null;
};

export type SubscriptionEntitlement = {
  userId: string;
  stripeCustomerId: string | null;
  stripeSubscriptionId: string | null;
  planCode: string | null;
  status: string;
  currentPeriodEnd: string | null;
  cancelAtPeriodEnd: number;
};

type DatabaseEnv = {
  DB?: D1Database;
};

type PipelineRunRow = {
  runId: string;
  runType: string;
  status: string;
  startedAt: string;
  finishedAt: string;
  modelVersionBefore: string | null;
  modelVersionAfter: string | null;
  summaryJson: string | null;
  errorJson: string | null;
};

export function databaseConfigured(env: DatabaseEnv): boolean {
  return Boolean(env.DB);
}

function requireDatabase(env: DatabaseEnv): D1Database {
  if (!env.DB) {
    throw new Error("D1 database binding is not configured.");
  }
  return env.DB;
}

function isPremiumStatus(status: string | null | undefined): boolean {
  return status === "active" || status === "trialing";
}

function parseJsonObject(value: string | null | undefined): Record<string, unknown> {
  if (!value) {
    return {};
  }
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function parseJsonError(value: string | null | undefined): Record<string, unknown> | null {
  if (!value) {
    return null;
  }
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function toPipelineRunSummary(row: PipelineRunRow | null): PipelineRunSummary | null {
  if (!row) {
    return null;
  }
  return {
    runId: row.runId,
    runType: row.runType,
    status: row.status,
    startedAt: row.startedAt,
    finishedAt: row.finishedAt,
    modelVersionBefore: row.modelVersionBefore,
    modelVersionAfter: row.modelVersionAfter,
    summary: parseJsonObject(row.summaryJson),
    error: parseJsonError(row.errorJson)
  };
}

async function getLatestPipelineRun(
  db: D1Database,
  runType?: "scrape" | "train" | "publish",
  acceptedStatuses?: string[]
): Promise<PipelineRunSummary | null> {
  try {
    const result = await db
      .prepare(
        `SELECT
           run_id as runId,
           run_type as runType,
           status,
           started_at as startedAt,
           finished_at as finishedAt,
           model_version_before as modelVersionBefore,
           model_version_after as modelVersionAfter,
           summary_json as summaryJson,
           error_json as errorJson
         FROM pipeline_run_registry
         ORDER BY finished_at DESC
         LIMIT 25`
      )
      .all<PipelineRunRow>();
    const row = (result.results ?? []).find((entry) => {
      if (runType && entry.runType !== runType) {
        return false;
      }
      if (acceptedStatuses?.length && !acceptedStatuses.includes(entry.status)) {
        return false;
      }
      return true;
    });
    return toPipelineRunSummary(row ?? null);
  } catch (error) {
    if (error instanceof Error && /pipeline_run_registry|no such table/i.test(error.message)) {
      return null;
    }
    throw error;
  }
}

function derivedDegradedSources(run: PipelineRunSummary | null): string[] {
  const degraded = run?.summary?.degradedSources;
  return Array.isArray(degraded) ? degraded.filter((item): item is string => typeof item === "string") : [];
}

function buildFreshnessInfo(params: {
  publishRun: PipelineRunSummary | null;
  scrapeRun: PipelineRunSummary | null;
  latestRun: PipelineRunSummary | null;
}): FreshnessInfo {
  const { publishRun, scrapeRun, latestRun } = params;
  const generatedAt = publishRun?.finishedAt ?? null;
  const degradedSources = derivedDegradedSources(scrapeRun);
  const scrapeDegraded = scrapeRun?.status === "degraded" || degradedSources.length > 0;
  const ageMs = generatedAt ? Date.now() - Date.parse(generatedAt) : Number.POSITIVE_INFINITY;

  let staleReason: string | null = null;
  let isStale = false;
  if (!generatedAt) {
    staleReason = "missing_publish_run";
    isStale = true;
  } else if (ageMs > STALE_THRESHOLD_MS) {
    staleReason = "publish_older_than_36h";
    isStale = true;
  } else if (scrapeDegraded) {
    staleReason = "source_guardrail_failed";
    isStale = true;
  }

  return {
    generatedAt,
    isStale,
    isDegraded: scrapeDegraded,
    staleReason,
    latestRunStatus: latestRun?.status ?? null,
    degradedSources
  };
}

export async function getPipelineRuntimeStatus(
  env: DatabaseEnv
): Promise<{
  freshness: FreshnessInfo;
  latestRunStatus: string | null;
  lastSuccessfulScrapeAt: string | null;
  lastSuccessfulTrainAt: string | null;
  lastSuccessfulPublishAt: string | null;
  dataStale: boolean;
  modelStale: boolean;
}> {
  const db = requireDatabase(env);
  const [latestRun, scrapeRun, trainRun, publishRun] = await Promise.all([
    getLatestPipelineRun(db),
    getLatestPipelineRun(db, "scrape", ["success", "degraded"]),
    getLatestPipelineRun(db, "train", ["success", "degraded", "skipped"]),
    getLatestPipelineRun(db, "publish", ["success", "degraded"])
  ]);
  const freshness = buildFreshnessInfo({ publishRun, scrapeRun, latestRun });
  const trainFinishedAt = trainRun?.finishedAt ?? null;
  const modelStale = !trainFinishedAt || Date.now() - Date.parse(trainFinishedAt) > STALE_THRESHOLD_MS;
  return {
    freshness,
    latestRunStatus: latestRun?.status ?? null,
    lastSuccessfulScrapeAt: scrapeRun?.finishedAt ?? null,
    lastSuccessfulTrainAt: trainFinishedAt,
    lastSuccessfulPublishAt: publishRun?.finishedAt ?? null,
    dataStale: freshness.isStale,
    modelStale
  };
}

export async function upsertUserProfile(
  env: DatabaseEnv,
  user: { id: string; email: string }
): Promise<void> {
  const db = requireDatabase(env);
  await db
    .prepare(
      `INSERT INTO user_profile (user_id, email, created_at, last_seen_at)
       VALUES (?1, ?2, datetime('now'), datetime('now'))
       ON CONFLICT(user_id) DO UPDATE SET
         email = excluded.email,
         last_seen_at = datetime('now')`
    )
    .bind(user.id, user.email)
    .run();
}

export async function getSubscriptionEntitlement(
  env: DatabaseEnv,
  userId: string
): Promise<SubscriptionEntitlement | null> {
  const db = requireDatabase(env);
  const result = await db
    .prepare(
      `SELECT
         user_id as userId,
         stripe_customer_id as stripeCustomerId,
         stripe_subscription_id as stripeSubscriptionId,
         plan_code as planCode,
         status,
         current_period_end as currentPeriodEnd,
         cancel_at_period_end as cancelAtPeriodEnd
       FROM subscription_entitlement
       WHERE user_id = ?1`
    )
    .bind(userId)
    .first<SubscriptionEntitlement>();
  return result ?? null;
}

export async function getUsageSummary(
  env: DatabaseEnv,
  userId: string
): Promise<UsageSummary> {
  const db = requireDatabase(env);
  const entitlement = await getSubscriptionEntitlement(env, userId);
  const usageResult = await db
    .prepare(
      `SELECT COUNT(*) as total
       FROM prediction_usage_event
       WHERE user_id = ?1
         AND event_kind = 'prediction_success'`
    )
    .bind(userId)
    .first<{ total: number }>();
  const used = Number(usageResult?.total ?? 0);
  const premium = isPremiumStatus(entitlement?.status);
  return {
    freeLimit: FREE_PREDICTION_LIMIT,
    freeUsed: used,
    freeRemaining: premium ? null : Math.max(FREE_PREDICTION_LIMIT - used, 0),
    premium,
    premiumStatus: entitlement?.status ?? "free",
    currentPeriodEnd: entitlement?.currentPeriodEnd ?? null,
    cancelAtPeriodEnd: Boolean(entitlement?.cancelAtPeriodEnd)
  };
}

export async function recordPredictionUsage(
  env: DatabaseEnv,
  payload: {
    userId: string;
    experienceMode: ExperienceMode;
    eventKind: "prediction_success" | "prediction_blocked";
    requestJson: string | null;
    responseJson: string | null;
  }
): Promise<void> {
  const db = requireDatabase(env);
  await db
    .prepare(
      `INSERT INTO prediction_usage_event (
         user_id, created_at, experience_mode, event_kind, request_json, response_json
       ) VALUES (?1, datetime('now'), ?2, ?3, ?4, ?5)`
    )
    .bind(
      payload.userId,
      payload.experienceMode,
      payload.eventKind,
      payload.requestJson,
      payload.responseJson
    )
    .run();
}

export async function getBillingCustomerMapByUserId(
  env: DatabaseEnv,
  userId: string
): Promise<BillingCustomerMap | null> {
  const db = requireDatabase(env);
  const result = await db
    .prepare(
      `SELECT
         user_id as userId,
         stripe_customer_id as stripeCustomerId,
         email
       FROM billing_customer_map
       WHERE user_id = ?1`
    )
    .bind(userId)
    .first<BillingCustomerMap>();
  return result ?? null;
}

export async function getBillingCustomerMapByStripeCustomerId(
  env: DatabaseEnv,
  stripeCustomerId: string
): Promise<BillingCustomerMap | null> {
  const db = requireDatabase(env);
  const result = await db
    .prepare(
      `SELECT
         user_id as userId,
         stripe_customer_id as stripeCustomerId,
         email
       FROM billing_customer_map
       WHERE stripe_customer_id = ?1`
    )
    .bind(stripeCustomerId)
    .first<BillingCustomerMap>();
  return result ?? null;
}

export async function upsertBillingCustomerMap(
  env: DatabaseEnv,
  payload: BillingCustomerMap
): Promise<void> {
  const db = requireDatabase(env);
  await db
    .prepare(
      `INSERT INTO billing_customer_map (user_id, stripe_customer_id, email, updated_at)
       VALUES (?1, ?2, ?3, datetime('now'))
       ON CONFLICT(user_id) DO UPDATE SET
         stripe_customer_id = excluded.stripe_customer_id,
         email = excluded.email,
         updated_at = datetime('now')`
    )
    .bind(payload.userId, payload.stripeCustomerId, payload.email)
    .run();
}

export async function upsertSubscriptionEntitlement(
  env: DatabaseEnv,
  payload: SubscriptionEntitlement
): Promise<void> {
  const db = requireDatabase(env);
  await db
    .prepare(
      `INSERT INTO subscription_entitlement (
         user_id, stripe_customer_id, stripe_subscription_id, plan_code, status,
         current_period_end, cancel_at_period_end, updated_at
       )
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, datetime('now'))
       ON CONFLICT(user_id) DO UPDATE SET
         stripe_customer_id = excluded.stripe_customer_id,
         stripe_subscription_id = excluded.stripe_subscription_id,
         plan_code = excluded.plan_code,
         status = excluded.status,
         current_period_end = excluded.current_period_end,
         cancel_at_period_end = excluded.cancel_at_period_end,
         updated_at = datetime('now')`
    )
    .bind(
      payload.userId,
      payload.stripeCustomerId,
      payload.stripeSubscriptionId,
      payload.planCode,
      payload.status,
      payload.currentPeriodEnd,
      payload.cancelAtPeriodEnd
    )
    .run();
}

export async function deleteUserAccountData(
  env: DatabaseEnv,
  userId: string
): Promise<void> {
  const db = requireDatabase(env);
  await db.prepare("DELETE FROM prediction_usage_event WHERE user_id = ?1").bind(userId).run();
  await db.prepare("DELETE FROM subscription_entitlement WHERE user_id = ?1").bind(userId).run();
  await db.prepare("DELETE FROM billing_customer_map WHERE user_id = ?1").bind(userId).run();
  await db.prepare("DELETE FROM user_profile WHERE user_id = ?1").bind(userId).run();
}

export async function listDashboardOpportunities(
  env: DatabaseEnv,
  filters: {
    window: "1d" | "7d" | "30d";
    direction: "under" | "over";
    district?: string | null;
    propertyType?: string | null;
    source?: string | null;
    includeFiltered?: boolean;
    limit?: number;
  }
): Promise<DashboardOpportunity[]> {
  const db = requireDatabase(env);
  const since = new Date(
    Date.now() -
      (filters.window === "1d" ? 1 : filters.window === "7d" ? 7 : 30) * 24 * 60 * 60 * 1000
  ).toISOString();
  const marketPosition = filters.direction === "under" ? "under_market" : "over_market";

  const clauses = ["discovered_at >= ?1", "market_position = ?2"];
  const values: Array<string | number> = [since, marketPosition];
  if (!filters.includeFiltered) {
    clauses.push(`COALESCE(is_filtered_default, 0) = 0`);
  }

  if (filters.district) {
    clauses.push(`district_prague = ?${values.length + 1}`);
    values.push(filters.district);
  }
  if (filters.propertyType) {
    clauses.push(`property_type = ?${values.length + 1}`);
    values.push(filters.propertyType);
  }
  if (filters.source) {
    clauses.push(`source = ?${values.length + 1}`);
    values.push(filters.source);
  }

  values.push(filters.limit ?? 50);

  const query = `
    SELECT
      source,
      source_listing_id as sourceListingId,
      discovered_at as discoveredAt,
      observed_at as observedAt,
      listing_url as listingUrl,
      address_text as addressText,
      district_prague as districtPrague,
      property_type as propertyType,
      asking_price_czk as askingPriceCzk,
      predicted_price_czk as predictedPriceCzk,
      typical_range_low_czk as typicalRangeLowCzk,
      typical_range_high_czk as typicalRangeHighCzk,
      deviation_czk as deviationCzk,
      deviation_pct as deviationPct,
      market_position as marketPosition,
      opportunity_score as opportunityScore,
      COALESCE(listing_quality_score, 1.0) as listingQualityScore,
      COALESCE(quality_flags, '[]') as qualityFlags,
      COALESCE(comparables_count, 0) as comparablesCount,
      COALESCE(confidence_score, 1.0) as confidenceScore,
      CAST(COALESCE(is_filtered_default, 0) AS INTEGER) as isFilteredDefault,
      COALESCE(filter_reasons, '[]') as filterReasons,
      COALESCE(warning_flags, '[]') as warningFlags
    FROM market_listing_score
    WHERE ${clauses.join(" AND ")}
    ORDER BY opportunity_score DESC, ABS(deviation_czk) DESC
    LIMIT ?${values.length}
  `;

  const results = await db.prepare(query).bind(...values).all<
    DashboardOpportunity & {
      qualityFlags: string | string[];
      filterReasons: string | string[];
      warningFlags: string | string[];
      isFilteredDefault: number | boolean;
    }
  >();
  return (results.results ?? []).map((row) => ({
    ...row,
    qualityFlags:
      typeof row.qualityFlags === "string" ? JSON.parse(row.qualityFlags || "[]") : row.qualityFlags,
    filterReasons:
      typeof row.filterReasons === "string" ? JSON.parse(row.filterReasons || "[]") : row.filterReasons,
    warningFlags:
      typeof row.warningFlags === "string" ? JSON.parse(row.warningFlags || "[]") : row.warningFlags,
    isFilteredDefault: Boolean(row.isFilteredDefault)
  }));
}

async function aggregateCounts(
  db: D1Database,
  since: string,
  marketPosition: "under_market" | "over_market"
): Promise<number> {
  const result = await db
    .prepare(
      `SELECT COUNT(*) as total
       FROM market_listing_score
       WHERE discovered_at >= ?1
         AND market_position = ?2`
    )
    .bind(since, marketPosition)
    .first<{ total: number }>();
  return Number(result?.total ?? 0);
}

async function aggregateTopDistricts(
  db: D1Database,
  since: string
): Promise<Array<{ districtPrague: string; total: number }>> {
  const results = await db
    .prepare(
      `SELECT district_prague as districtPrague, COUNT(*) as total
       FROM market_listing_score
       WHERE discovered_at >= ?1
         AND market_position != 'within_range'
       GROUP BY district_prague
       ORDER BY total DESC, district_prague ASC
       LIMIT 5`
    )
    .bind(since)
    .all<{ districtPrague: string; total: number }>();
  return results.results ?? [];
}

export async function getDashboardTeaser(
  env: DatabaseEnv
): Promise<DashboardTeaserResponse> {
  const db = requireDatabase(env);
  const windows = {
    "1d": new Date(Date.now() - 1 * 24 * 60 * 60 * 1000).toISOString(),
    "7d": new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString(),
    "30d": new Date(Date.now() - 30 * 24 * 60 * 60 * 1000).toISOString()
  } as const;

  const summary = await Promise.all(
    Object.entries(windows).map(async ([window, since]) => ({
      window: window as "1d" | "7d" | "30d",
      underCount: await aggregateCounts(db, since, "under_market"),
      overCount: await aggregateCounts(db, since, "over_market"),
      topDistricts: await aggregateTopDistricts(db, since)
    }))
  );
  const runtimeStatus = await getPipelineRuntimeStatus(env);

  return {
    summary,
    freshness: runtimeStatus.freshness
  };
}
