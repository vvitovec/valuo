import {
  getBillingCustomerMapByStripeCustomerId,
  getBillingCustomerMapByUserId,
  upsertBillingCustomerMap,
  upsertSubscriptionEntitlement,
  type BillingCustomerMap,
  type SubscriptionEntitlement
} from "./db";

type BillingEnv = {
  DB?: D1Database;
  STRIPE_SECRET_KEY?: string;
  STRIPE_WEBHOOK_SECRET?: string;
  STRIPE_PRICE_ID?: string;
  PREMIUM_PLAN_CODE?: string;
};

type StripeRequestOptions = {
  method?: "GET" | "POST";
  path: string;
  form?: Record<string, string | number | null | undefined>;
};

function configured(env: BillingEnv): boolean {
  return Boolean(env.STRIPE_SECRET_KEY && env.STRIPE_PRICE_ID);
}

function basicAuthHeader(secretKey: string): string {
  return `Basic ${btoa(`${secretKey}:`)}`;
}

async function stripeRequest<T>(
  env: BillingEnv,
  options: StripeRequestOptions
): Promise<T> {
  if (!env.STRIPE_SECRET_KEY) {
    throw new Error("Stripe secret key is not configured.");
  }

  const headers = new Headers({
    authorization: basicAuthHeader(env.STRIPE_SECRET_KEY)
  });
  let body: string | undefined;
  if (options.form) {
    headers.set("content-type", "application/x-www-form-urlencoded");
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(options.form)) {
      if (value === null || value === undefined) {
        continue;
      }
      params.set(key, String(value));
    }
    body = params.toString();
  }

  const response = await fetch(`https://api.stripe.com${options.path}`, {
    method: options.method ?? "POST",
    headers,
    body
  });
  const json = (await response.json()) as T & { error?: { message?: string } };
  if (!response.ok) {
    throw new Error(json.error?.message ?? `Stripe request failed with ${response.status}`);
  }
  return json;
}

async function ensureStripeCustomer(
  env: BillingEnv,
  user: { id: string; email: string }
): Promise<BillingCustomerMap> {
  const existing = await getBillingCustomerMapByUserId(env, user.id);
  if (existing) {
    return existing;
  }

  const customer = await stripeRequest<{ id: string }>(env, {
    path: "/v1/customers",
    form: {
      email: user.email,
      "metadata[user_id]": user.id
    }
  });

  const payload = {
    userId: user.id,
    stripeCustomerId: customer.id,
    email: user.email
  };
  await upsertBillingCustomerMap(env, payload);
  return payload;
}

export async function createCheckoutSession(
  env: BillingEnv,
  payload: {
    user: { id: string; email: string };
    successUrl: string;
    cancelUrl: string;
  }
): Promise<{ url: string }> {
  if (!configured(env)) {
    throw new Error("Stripe billing is not configured.");
  }
  const customer = await ensureStripeCustomer(env, payload.user);
  const session = await stripeRequest<{ url: string }>(env, {
    path: "/v1/checkout/sessions",
    form: {
      "line_items[0][price]": env.STRIPE_PRICE_ID,
      "line_items[0][quantity]": 1,
      mode: "subscription",
      customer: customer.stripeCustomerId,
      client_reference_id: payload.user.id,
      "metadata[user_id]": payload.user.id,
      "metadata[user_email]": payload.user.email,
      success_url: payload.successUrl,
      cancel_url: payload.cancelUrl
    }
  });
  return { url: session.url };
}

export async function createBillingPortalSession(
  env: BillingEnv,
  payload: {
    user: { id: string; email: string };
    returnUrl: string;
  }
): Promise<{ url: string }> {
  if (!env.STRIPE_SECRET_KEY) {
    throw new Error("Stripe billing is not configured.");
  }

  const customer = await ensureStripeCustomer(env, payload.user);
  const session = await stripeRequest<{ url: string }>(env, {
    path: "/v1/billing_portal/sessions",
    form: {
      customer: customer.stripeCustomerId,
      return_url: payload.returnUrl
    }
  });
  return { url: session.url };
}

function parseSignatureHeader(value: string): { timestamp: string; signatures: string[] } | null {
  const parts = value.split(",").map((part) => part.trim());
  const timestamp = parts.find((part) => part.startsWith("t="))?.slice(2);
  const signatures = parts
    .filter((part) => part.startsWith("v1="))
    .map((part) => part.slice(3));
  if (!timestamp || signatures.length === 0) {
    return null;
  }
  return { timestamp, signatures };
}

async function computeStripeSignature(secret: string, payload: string, timestamp: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(`${timestamp}.${payload}`)
  );
  return Array.from(new Uint8Array(signature))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

export async function verifyStripeWebhook(
  env: BillingEnv,
  payload: string,
  signatureHeader: string | null
): Promise<Record<string, unknown>> {
  if (!env.STRIPE_WEBHOOK_SECRET) {
    return JSON.parse(payload) as Record<string, unknown>;
  }
  if (!signatureHeader) {
    throw new Error("Missing Stripe signature header.");
  }
  const parsed = parseSignatureHeader(signatureHeader);
  if (!parsed) {
    throw new Error("Malformed Stripe signature header.");
  }
  const expected = await computeStripeSignature(env.STRIPE_WEBHOOK_SECRET, payload, parsed.timestamp);
  if (!parsed.signatures.includes(expected)) {
    throw new Error("Stripe webhook signature verification failed.");
  }
  return JSON.parse(payload) as Record<string, unknown>;
}

function normalizeSubscriptionStatus(status: string | null | undefined): string {
  return status ?? "inactive";
}

function unixToIso(value: number | null | undefined): string | null {
  if (!value) {
    return null;
  }
  return new Date(value * 1000).toISOString();
}

async function syncEntitlementFromSubscription(
  env: BillingEnv,
  payload: {
    userId: string;
    stripeCustomerId: string;
    stripeSubscriptionId: string | null;
    status: string | null | undefined;
    currentPeriodEnd: number | null | undefined;
    cancelAtPeriodEnd: boolean | null | undefined;
  }
): Promise<void> {
  const entitlement: SubscriptionEntitlement = {
    userId: payload.userId,
    stripeCustomerId: payload.stripeCustomerId,
    stripeSubscriptionId: payload.stripeSubscriptionId,
    planCode: env.PREMIUM_PLAN_CODE ?? "premium_monthly",
    status: normalizeSubscriptionStatus(payload.status),
    currentPeriodEnd: unixToIso(payload.currentPeriodEnd),
    cancelAtPeriodEnd: payload.cancelAtPeriodEnd ? 1 : 0
  };
  await upsertSubscriptionEntitlement(env, entitlement);
}

function stringMetadataValue(
  source: Record<string, unknown> | null | undefined,
  key: string
): string | null {
  const metadata = source?.metadata;
  if (!metadata || typeof metadata !== "object") {
    return null;
  }
  const value = (metadata as Record<string, unknown>)[key];
  return typeof value === "string" ? value : null;
}

export async function handleStripeWebhookEvent(
  env: BillingEnv,
  event: Record<string, unknown>
): Promise<void> {
  const type = String(event.type ?? "");
  const dataObject = (event.data as { object?: Record<string, unknown> } | undefined)?.object;
  if (!dataObject) {
    return;
  }

  if (type === "checkout.session.completed") {
    const stripeCustomerId = typeof dataObject.customer === "string" ? dataObject.customer : null;
    const userId =
      stringMetadataValue(dataObject, "user_id") ??
      (typeof dataObject.client_reference_id === "string" ? dataObject.client_reference_id : null);
    const email =
      stringMetadataValue(dataObject, "user_email") ??
      (typeof dataObject.customer_email === "string" ? dataObject.customer_email : null);
    if (stripeCustomerId && userId) {
      await upsertBillingCustomerMap(env, {
        userId,
        stripeCustomerId,
        email
      });
      await syncEntitlementFromSubscription(env, {
        userId,
        stripeCustomerId,
        stripeSubscriptionId:
          typeof dataObject.subscription === "string" ? dataObject.subscription : null,
        status: "active",
        currentPeriodEnd: null,
        cancelAtPeriodEnd: false
      });
    }
    return;
  }

  if (type === "customer.subscription.updated" || type === "customer.subscription.created") {
    const stripeCustomerId = typeof dataObject.customer === "string" ? dataObject.customer : null;
    if (!stripeCustomerId) {
      return;
    }
    const map = await getBillingCustomerMapByStripeCustomerId(env, stripeCustomerId);
    if (!map) {
      return;
    }
    await syncEntitlementFromSubscription(env, {
      userId: map.userId,
      stripeCustomerId,
      stripeSubscriptionId: typeof dataObject.id === "string" ? dataObject.id : null,
      status: typeof dataObject.status === "string" ? dataObject.status : "inactive",
      currentPeriodEnd:
        typeof dataObject.current_period_end === "number" ? dataObject.current_period_end : null,
      cancelAtPeriodEnd:
        typeof dataObject.cancel_at_period_end === "boolean"
          ? dataObject.cancel_at_period_end
          : false
    });
    return;
  }

  if (type === "customer.subscription.deleted") {
    const stripeCustomerId = typeof dataObject.customer === "string" ? dataObject.customer : null;
    if (!stripeCustomerId) {
      return;
    }
    const map = await getBillingCustomerMapByStripeCustomerId(env, stripeCustomerId);
    if (!map) {
      return;
    }
    await syncEntitlementFromSubscription(env, {
      userId: map.userId,
      stripeCustomerId,
      stripeSubscriptionId: typeof dataObject.id === "string" ? dataObject.id : null,
      status: "canceled",
      currentPeriodEnd:
        typeof dataObject.current_period_end === "number" ? dataObject.current_period_end : null,
      cancelAtPeriodEnd: true
    });
  }
}

export function billingConfigured(env: BillingEnv): boolean {
  return configured(env);
}
