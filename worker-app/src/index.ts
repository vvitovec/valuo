import {
  __resetAuthCacheForTests,
  accountDeletionConfigured,
  authenticateRequest,
  authConfigured,
  deleteSupabaseUser,
  getAuthConfigStatus
} from "./auth";
import {
  billingConfigured,
  createBillingPortalSession,
  createCheckoutSession,
  handleStripeWebhookEvent,
  verifyStripeWebhook
} from "./billing";
import {
  FREE_PREDICTION_LIMIT,
  databaseConfigured,
  deleteUserAccountData,
  getDashboardTeaser,
  getPipelineRuntimeStatus,
  getUsageSummary,
  listDashboardOpportunities,
  recordPredictionUsage,
  upsertUserProfile
} from "./db";
import { resolveAddress } from "./geocode";
import {
  applyPredictionInterval,
  confidenceScoreComponents,
  enrichPayloadForModel,
  featureEffectsFromScore,
  makeFeaturePayload,
  scoreModel
} from "./model";
import { prefillListingFromUrl } from "./prefill";
import type {
  DashboardTeaserResponse,
  ExportedModel,
  HealthResponse,
  MeResponse,
  ModelSource,
  ModelRegistryManifest,
  PredictionResponse
} from "./types";
import { deleteAccountRequestSchema, listingPrefillRequestSchema, predictionRequestSchema } from "./types";

type RuntimeEnv = {
  ASSETS: Fetcher;
  DB?: D1Database;
  MODEL_BUCKET?: R2Bucket;
  SUPABASE_URL?: string;
  SUPABASE_ANON_KEY?: string;
  SUPABASE_SERVICE_ROLE_KEY?: string;
  STRIPE_SECRET_KEY?: string;
  STRIPE_WEBHOOK_SECRET?: string;
  STRIPE_PRICE_ID?: string;
  PREMIUM_PLAN_CODE?: string;
  PREMIUM_PRICE_LABEL?: string;
  APP_BASE_URL?: string;
};

const MODEL_CACHE_TTL_MS = 5 * 60 * 1000;

let cachedModelPromise: Promise<ExportedModel> | null = null;
let cachedRegistryPromise: Promise<ModelRegistryManifest | null> | null = null;
let cachedModelLoadedAt = 0;
let cachedRegistryLoadedAt = 0;
let cachedModelSource: ModelSource = "bundle";

function cacheExpired(lastLoadedAt: number): boolean {
  return !lastLoadedAt || Date.now() - lastLoadedAt > MODEL_CACHE_TTL_MS;
}

function jsonResponse(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: {
      "content-type": "application/json; charset=utf-8",
      ...(init?.headers ?? {})
    }
  });
}

async function readBundledJson<T>(env: RuntimeEnv, pathname: string): Promise<T> {
  const request = new Request(`https://assets.local${pathname}`);
  const response = await env.ASSETS.fetch(request);
  if (!response.ok) {
    throw new Error(`Bundled asset is missing: ${pathname}`);
  }
  return (await response.json()) as T;
}

async function loadModel(env: RuntimeEnv): Promise<ExportedModel> {
  if (!cachedModelPromise || cacheExpired(cachedModelLoadedAt)) {
    const previousPromise = cachedModelPromise;
    cachedModelPromise = (async () => {
      try {
        if (env.MODEL_BUCKET) {
          const object = await env.MODEL_BUCKET.get("active-model.json");
          if (object) {
            cachedModelSource = "r2";
            return (await object.json()) as ExportedModel;
          }
        }
      } catch (error) {
        if (previousPromise) {
          return previousPromise;
        }
        cachedModelSource = "bundle";
        const bundledModel = await readBundledJson<ExportedModel>(env, "/models/active-model.json");
        return bundledModel;
      }
      cachedModelSource = "bundle";
      return await readBundledJson<ExportedModel>(env, "/models/active-model.json");
    })().catch(async (error) => {
      if (previousPromise) {
        return previousPromise;
      }
      throw error;
    });
    cachedModelLoadedAt = Date.now();
  }
  return cachedModelPromise;
}

async function loadRegistry(env: RuntimeEnv): Promise<ModelRegistryManifest | null> {
  if (!cachedRegistryPromise || cacheExpired(cachedRegistryLoadedAt)) {
    const previousPromise = cachedRegistryPromise;
    cachedRegistryPromise = (async () => {
      try {
        if (env.MODEL_BUCKET) {
          const object = await env.MODEL_BUCKET.get("model-registry.json");
          if (object) {
            return (await object.json()) as ModelRegistryManifest;
          }
        }
      } catch (error) {
        if (previousPromise) {
          return previousPromise;
        }
        return await readBundledJson<ModelRegistryManifest>(env, "/manifests/model-registry.json");
      }
      return await readBundledJson<ModelRegistryManifest>(env, "/manifests/model-registry.json");
    })().catch(async (error) => {
      if (previousPromise) {
        return previousPromise;
      }
      throw error;
    });
    cachedRegistryLoadedAt = Date.now();
  }
  return cachedRegistryPromise;
}

async function recordPredictionAudit(
  env: RuntimeEnv,
  requestBody: Record<string, unknown>,
  responseBody: PredictionResponse
) {
  if (!env.DB) {
    return;
  }
  await env.DB.prepare(
    `INSERT INTO prediction_audit (
      created_at, request_json, response_json, model_version
    ) VALUES (datetime('now'), ?1, ?2, ?3)`
  )
    .bind(
      JSON.stringify(requestBody),
      JSON.stringify(responseBody),
      responseBody.modelVersion
    )
    .run();
}

function inferMarketPosition(
  askingPriceCzk: number | undefined,
  low: number,
  high: number
): PredictionResponse["marketPosition"] {
  if (!askingPriceCzk) {
    return "unknown";
  }
  if (askingPriceCzk < low) {
    return "under_market";
  }
  if (askingPriceCzk > high) {
    return "over_market";
  }
  return "within_range";
}

async function requireUser(request: Request, env: RuntimeEnv) {
  if (!authConfigured(env)) {
    return {
      error: jsonResponse(
        {
          error: "Auth is not configured.",
          code: "AUTH_NOT_CONFIGURED"
        },
        { status: 503 }
      )
    };
  }
  if (!databaseConfigured(env)) {
    return {
      error: jsonResponse(
        {
          error: "Database is not configured.",
          code: "DB_NOT_CONFIGURED"
        },
        { status: 503 }
      )
    };
  }
  const user = await authenticateRequest(request, env);
  if (!user) {
    return {
      error: jsonResponse(
        {
          error: "Authentication required.",
          code: "AUTH_REQUIRED"
        },
        { status: 401 }
      )
    };
  }
  await upsertUserProfile(env, user);
  return { user };
}

function appBaseUrl(request: Request, env: RuntimeEnv): string {
  return env.APP_BASE_URL ?? new URL(request.url).origin;
}

async function handleConfig(request: Request, env: RuntimeEnv): Promise<Response> {
  const authStatus = await getAuthConfigStatus(env);
  return jsonResponse({
    appBaseUrl: appBaseUrl(request, env),
    auth: {
      configured: authStatus.available,
      reason: authStatus.reason,
      message: authStatus.message,
      supabaseUrl: env.SUPABASE_URL ?? null,
      supabaseAnonKey: env.SUPABASE_ANON_KEY ?? null
    },
    billing: {
      configured: billingConfigured(env),
      premiumPriceLabel: env.PREMIUM_PRICE_LABEL ?? "Měsíční předplatné"
    },
    account: {
      deletionAvailable: accountDeletionConfigured(env)
    },
    limits: {
      freePredictions: FREE_PREDICTION_LIMIT
    }
  });
}

async function handleMe(request: Request, env: RuntimeEnv): Promise<Response> {
  const auth = await requireUser(request, env);
  if (auth.error) {
    return auth.error;
  }
  const usage = await getUsageSummary(env, auth.user.id);
  const response: MeResponse = {
    user: auth.user,
    usage
  };
  return jsonResponse(response);
}

async function handlePredict(request: Request, env: RuntimeEnv): Promise<Response> {
  const auth = await requireUser(request, env);
  if (auth.error) {
    return auth.error;
  }

  const json = await request.json();
  const parsed = predictionRequestSchema.safeParse(json);
  if (!parsed.success) {
    return jsonResponse(
      { error: "Neplatný vstup", details: parsed.error.flatten() },
      { status: 400 }
    );
  }

  const usageBefore = await getUsageSummary(env, auth.user.id);
  if (!usageBefore.premium && (usageBefore.freeRemaining ?? 0) <= 0) {
    await recordPredictionUsage(env, {
      userId: auth.user.id,
      experienceMode: parsed.data.experienceMode,
      eventKind: "prediction_blocked",
      requestJson: JSON.stringify(parsed.data),
      responseJson: null
    });
    return jsonResponse(
      {
        error: "Bez prémiového účtu už není k dispozici další predikce.",
        code: "UPGRADE_REQUIRED",
        usage: usageBefore
      },
      { status: 402 }
    );
  }

  const geocode = await resolveAddress(parsed.data.address, parsed.data.districtPrague, env);
  const model = await loadModel(env);
  const payload = makeFeaturePayload({
    ...parsed.data,
    distanceToCenterKm: geocode.distanceToCenterKm,
    locationCluster: geocode.locationCluster,
    marketSegment: geocode.marketSegment,
    lat: geocode.lat,
    lng: geocode.lng,
    geocodeResolution: geocode.geocodeResolution
  });
  payload.district_prague = geocode.districtPrague;
  const enrichedPayload = enrichPayloadForModel(model, payload);

  const scoreResult = scoreModel(model, enrichedPayload);
  const estimatedPriceCzk = Math.round(scoreResult.estimatedPriceCzk);
  const interval = applyPredictionInterval(model, estimatedPriceCzk);
  const confidence = confidenceScoreComponents(
    estimatedPriceCzk,
    interval.low,
    interval.high,
    enrichedPayload
  );
  const deltaVsInputPriceCzk = parsed.data.askingPriceCzk
    ? Math.round(parsed.data.askingPriceCzk - estimatedPriceCzk)
    : null;

  await recordPredictionUsage(env, {
    userId: auth.user.id,
    experienceMode: parsed.data.experienceMode,
    eventKind: "prediction_success",
    requestJson: JSON.stringify(parsed.data),
    responseJson: null
  });
  const usageAfter = await getUsageSummary(env, auth.user.id);

  const responseBody: PredictionResponse = {
    estimatedPriceCzk,
    typicalRangeLowCzk: interval.low,
    typicalRangeHighCzk: interval.high,
    marketPosition: inferMarketPosition(parsed.data.askingPriceCzk, interval.low, interval.high),
    deltaVsInputPriceCzk,
    featureEffects: featureEffectsFromScore(model, scoreResult).slice(0, 6),
    modelVersion: model.version,
    notes: [...model.notes, ...geocode.notes],
    resolvedDistrictPrague: geocode.districtPrague,
    resolvedLocationCluster: geocode.locationCluster,
    resolvedLat: geocode.lat,
    resolvedLng: geocode.lng,
    confidenceScore: confidence.confidenceScore,
    confidenceLabel: confidence.confidenceLabel,
    inputQualityScore: Number(enrichedPayload.listing_input_quality_score ?? 0),
    comparablesCount: confidence.comparablesCount,
    warningFlags: confidence.warningFlags,
    modelFamily: model.kind,
    usage: usageAfter,
    experienceMode: parsed.data.experienceMode
  };

  await recordPredictionAudit(env, parsed.data, responseBody);
  return jsonResponse(responseBody);
}

async function handleHealth(env: RuntimeEnv): Promise<Response> {
  const model = await loadModel(env);
  const registry = await loadRegistry(env);
  const authStatus = await getAuthConfigStatus(env);
  const activeEntry = registry?.entries.find((entry) => entry.version === registry.activeModelVersion) ?? null;
  let d1Ready = false;
  if (env.DB) {
    try {
      await env.DB.prepare("SELECT 1").first();
      d1Ready = true;
    } catch {
      d1Ready = false;
    }
  }
  const r2Ready = Boolean(env.MODEL_BUCKET);
  const runtimeStatus = env.DB
    ? await getPipelineRuntimeStatus(env)
    : {
        freshness: {
          generatedAt: null,
          isStale: true,
          isDegraded: false,
          staleReason: "db_unavailable",
          latestRunStatus: null,
          degradedSources: []
        },
        latestRunStatus: null,
        lastSuccessfulScrapeAt: null,
        lastSuccessfulTrainAt: null,
        lastSuccessfulPublishAt: null,
        dataStale: true,
        modelStale: true
      };
  const response: HealthResponse = {
    status: "ok",
    activeModelVersion: model.version,
    lastModelPromotionTime: registry?.lastPromotedAt ?? model.trainedAt,
    bindingStatus: {
      d1: d1Ready,
      r2: r2Ready
    },
    validationSummary: model.validationSummary ?? activeEntry?.validationSummary ?? null,
    curatedRowCount: model.curatedRowCount ?? activeEntry?.curatedRowCount ?? null,
    authConfigured: authStatus.available,
    billingConfigured: billingConfigured(env),
    modelSource: cachedModelSource,
    lastSuccessfulScrapeAt: runtimeStatus.lastSuccessfulScrapeAt,
    lastSuccessfulTrainAt: runtimeStatus.lastSuccessfulTrainAt,
    lastSuccessfulPublishAt: runtimeStatus.lastSuccessfulPublishAt,
    dataStale: runtimeStatus.dataStale,
    modelStale: runtimeStatus.modelStale,
    latestRunStatus: runtimeStatus.latestRunStatus
  };
  return jsonResponse(response);
}

async function handleListingPrefill(request: Request): Promise<Response> {
  const json = await request.json();
  const parsed = listingPrefillRequestSchema.safeParse(json);
  if (!parsed.success) {
    return jsonResponse(
      { error: "Neplatný odkaz na inzerát", details: parsed.error.flatten() },
      { status: 400 }
    );
  }

  try {
    const responseBody = await prefillListingFromUrl(parsed.data.url);
    return jsonResponse(responseBody);
  } catch (error) {
    return jsonResponse(
      {
        error: error instanceof Error ? error.message : "Načtení inzerátu selhalo"
      },
      { status: 422 }
    );
  }
}

async function handleDashboardTeaser(env: RuntimeEnv): Promise<Response> {
  if (!databaseConfigured(env)) {
    const empty: DashboardTeaserResponse = {
      summary: [
        { window: "1d", underCount: 0, overCount: 0, topDistricts: [] },
        { window: "7d", underCount: 0, overCount: 0, topDistricts: [] },
        { window: "30d", underCount: 0, overCount: 0, topDistricts: [] }
      ],
      freshness: {
        generatedAt: null,
        isStale: true,
        isDegraded: false,
        staleReason: "db_unavailable",
        latestRunStatus: null,
        degradedSources: []
      }
    };
    return jsonResponse(empty);
  }
  const teaser = await getDashboardTeaser(env);
  return jsonResponse(teaser);
}

async function handleDashboardOpportunities(request: Request, env: RuntimeEnv): Promise<Response> {
  const auth = await requireUser(request, env);
  if (auth.error) {
    return auth.error;
  }
  const usage = await getUsageSummary(env, auth.user.id);
  if (!usage.premium) {
    return jsonResponse(
      {
        error: "Premium dashboard je dostupný jen pro platící uživatele.",
        code: "PREMIUM_REQUIRED",
        usage
      },
      { status: 402 }
    );
  }

  const url = new URL(request.url);
  const windowValue = url.searchParams.get("window");
  const directionValue = url.searchParams.get("direction");
  if (!windowValue || !["1d", "7d", "30d"].includes(windowValue)) {
    return jsonResponse({ error: "Neplatná hodnota window." }, { status: 400 });
  }
  if (!directionValue || !["under", "over"].includes(directionValue)) {
    return jsonResponse({ error: "Neplatná hodnota direction." }, { status: 400 });
  }

  const rows = await listDashboardOpportunities(env, {
    window: windowValue as "1d" | "7d" | "30d",
    direction: directionValue as "under" | "over",
    district: url.searchParams.get("district"),
    propertyType: url.searchParams.get("propertyType"),
    source: url.searchParams.get("source"),
    includeFiltered: url.searchParams.get("includeFiltered") === "true",
    limit: 50
  });
  return jsonResponse({ opportunities: rows, usage });
}

async function handleCreateCheckoutSession(request: Request, env: RuntimeEnv): Promise<Response> {
  const auth = await requireUser(request, env);
  if (auth.error) {
    return auth.error;
  }
  const usage = await getUsageSummary(env, auth.user.id);
  if (usage.premium) {
    return jsonResponse({ error: "Účet už má aktivní premium." }, { status: 409 });
  }

  try {
    const baseUrl = appBaseUrl(request, env);
    const session = await createCheckoutSession(env, {
      user: auth.user,
      successUrl: `${baseUrl}/app/prehled?billing=success`,
      cancelUrl: `${baseUrl}/app/naceneni?billing=cancel`
    });
    return jsonResponse(session);
  } catch (error) {
    return jsonResponse(
      {
        error: error instanceof Error ? error.message : "Checkout session creation failed."
      },
      { status: 503 }
    );
  }
}

async function handleCreateBillingPortalSession(request: Request, env: RuntimeEnv): Promise<Response> {
  const auth = await requireUser(request, env);
  if (auth.error) {
    return auth.error;
  }

  try {
    const baseUrl = appBaseUrl(request, env);
    const session = await createBillingPortalSession(env, {
      user: auth.user,
      returnUrl: `${baseUrl}/app/ucet`
    });
    return jsonResponse(session);
  } catch (error) {
    return jsonResponse(
      {
        error: error instanceof Error ? error.message : "Billing portal creation failed."
      },
      { status: 503 }
    );
  }
}

async function handleDeleteAccount(request: Request, env: RuntimeEnv): Promise<Response> {
  const auth = await requireUser(request, env);
  if (auth.error) {
    return auth.error;
  }
  if (!accountDeletionConfigured(env)) {
    return jsonResponse(
      {
        error: "Mazání účtu není na serveru nakonfigurované.",
        code: "ACCOUNT_DELETION_NOT_CONFIGURED"
      },
      { status: 503 }
    );
  }

  const json = await request.json();
  const parsed = deleteAccountRequestSchema.safeParse(json);
  if (!parsed.success) {
    return jsonResponse(
      { error: "Neplatné potvrzení smazání účtu.", details: parsed.error.flatten() },
      { status: 400 }
    );
  }
  if (parsed.data.confirmation.toLowerCase() !== auth.user.email.toLowerCase()) {
    return jsonResponse(
      {
        error: "Pro smazání účtu musí potvrzení přesně odpovídat emailu účtu.",
        code: "ACCOUNT_DELETE_CONFIRMATION_MISMATCH"
      },
      { status: 400 }
    );
  }

  try {
    await deleteSupabaseUser(auth.user.id, env);
    await deleteUserAccountData(env, auth.user.id);
    return jsonResponse({ success: true });
  } catch (error) {
    return jsonResponse(
      {
        error: error instanceof Error ? error.message : "Account deletion failed."
      },
      { status: 500 }
    );
  }
}

async function handleStripeWebhook(request: Request, env: RuntimeEnv): Promise<Response> {
  const signature = request.headers.get("stripe-signature");
  const payload = await request.text();
  try {
    const event = await verifyStripeWebhook(env, payload, signature);
    await handleStripeWebhookEvent(env, event);
    return jsonResponse({ received: true });
  } catch (error) {
    return jsonResponse(
      {
        error: error instanceof Error ? error.message : "Webhook processing failed."
      },
      { status: 400 }
    );
  }
}

async function serveSpa(env: RuntimeEnv): Promise<Response> {
  return env.ASSETS.fetch(new Request("https://assets.local/index.html"));
}

export function __resetWorkerCachesForTests() {
  cachedModelPromise = null;
  cachedRegistryPromise = null;
  cachedModelLoadedAt = 0;
  cachedRegistryLoadedAt = 0;
  cachedModelSource = "bundle";
  __resetAuthCacheForTests();
}

export default {
  async fetch(request, env, _ctx): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/api/config") {
      return handleConfig(request, env);
    }
    if (request.method === "GET" && url.pathname === "/api/me") {
      return handleMe(request, env);
    }
    if (request.method === "GET" && url.pathname === "/api/dashboard/teaser") {
      return handleDashboardTeaser(env);
    }
    if (request.method === "GET" && url.pathname === "/api/dashboard/opportunities") {
      return handleDashboardOpportunities(request, env);
    }
    if (request.method === "POST" && url.pathname === "/api/billing/create-checkout-session") {
      return handleCreateCheckoutSession(request, env);
    }
    if (request.method === "POST" && url.pathname === "/api/billing/create-portal-session") {
      return handleCreateBillingPortalSession(request, env);
    }
    if (request.method === "POST" && url.pathname === "/api/billing/webhook") {
      return handleStripeWebhook(request, env);
    }
    if (request.method === "POST" && url.pathname === "/api/account/delete") {
      return handleDeleteAccount(request, env);
    }
    if (request.method === "POST" && url.pathname === "/api/prefill-listing") {
      return handleListingPrefill(request);
    }
    if (request.method === "POST" && url.pathname === "/api/predict") {
      try {
        return await handlePredict(request, env);
      } catch (error) {
        return jsonResponse(
          {
            error: "Prediction failed",
            details: error instanceof Error ? error.message : String(error)
          },
          { status: 500 }
        );
      }
    }
    if (request.method === "GET" && url.pathname === "/api/health") {
      try {
        return await handleHealth(env);
      } catch (error) {
        return jsonResponse(
          {
            status: "error",
            error: error instanceof Error ? error.message : String(error)
          },
          { status: 500 }
        );
      }
    }
    if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/login" || url.pathname.startsWith("/app") || url.pathname === "/auth/callback")) {
      return serveSpa(env);
    }
    return env.ASSETS.fetch(request);
  }
} satisfies ExportedHandler<RuntimeEnv>;
