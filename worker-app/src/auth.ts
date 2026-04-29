export type AuthenticatedUser = {
  id: string;
  email: string;
};

export type AuthConfigStatus = {
  available: boolean;
  reason: "ready" | "missing" | "invalid_url" | "unreachable";
  message: string | null;
};

type SupabaseAuthEnv = {
  SUPABASE_URL?: string;
  SUPABASE_ANON_KEY?: string;
  SUPABASE_SERVICE_ROLE_KEY?: string;
};

const AUTH_STATUS_CACHE_TTL_MS = 60_000;

let cachedAuthStatusEnvKey: string | null = null;
let cachedAuthStatus: AuthConfigStatus | null = null;
let cachedAuthStatusAt = 0;

function authEnvCacheKey(env: SupabaseAuthEnv): string {
  return `${env.SUPABASE_URL ?? ""}::${env.SUPABASE_ANON_KEY ?? ""}`;
}

function isCachedAuthStatusFresh(envKey: string): boolean {
  return cachedAuthStatusEnvKey === envKey
    && Boolean(cachedAuthStatus)
    && Date.now() - cachedAuthStatusAt < AUTH_STATUS_CACHE_TTL_MS;
}

function authorizationToken(request: Request): string | null {
  const header = request.headers.get("authorization");
  if (!header) {
    return null;
  }
  const [scheme, token] = header.split(" ");
  if (scheme?.toLowerCase() !== "bearer" || !token) {
    return null;
  }
  return token;
}

export function authConfigured(env: SupabaseAuthEnv): boolean {
  return Boolean(env.SUPABASE_URL && env.SUPABASE_ANON_KEY);
}

export function accountDeletionConfigured(env: SupabaseAuthEnv): boolean {
  return Boolean(env.SUPABASE_URL && env.SUPABASE_SERVICE_ROLE_KEY);
}

export async function getAuthConfigStatus(env: SupabaseAuthEnv): Promise<AuthConfigStatus> {
  const envKey = authEnvCacheKey(env);
  if (isCachedAuthStatusFresh(envKey)) {
    return cachedAuthStatus as AuthConfigStatus;
  }

  let status: AuthConfigStatus;

  if (!env.SUPABASE_URL || !env.SUPABASE_ANON_KEY) {
    status = {
      available: false,
      reason: "missing",
      message: "Přihlášení není na serveru nakonfigurované."
    };
  } else {
    try {
      const supabaseUrl = new URL(env.SUPABASE_URL);
      if (supabaseUrl.protocol !== "https:") {
        status = {
          available: false,
          reason: "invalid_url",
          message: "Přihlašovací služba má neplatnou adresu."
        };
      } else {
        const response = await fetch(new URL("/auth/v1/settings", supabaseUrl), {
          headers: {
            apikey: env.SUPABASE_ANON_KEY
          }
        });
        status = response.ok
          ? {
              available: true,
              reason: "ready",
              message: null
            }
          : {
              available: false,
              reason: "unreachable",
              message: "Přihlašovací služba teď není dostupná. Zkontroluj SUPABASE_URL a stav projektu v Supabase."
            };
      }
    } catch {
      status = {
        available: false,
        reason: "unreachable",
        message: "Přihlašovací služba teď není dostupná. Zkontroluj SUPABASE_URL a stav projektu v Supabase."
      };
    }
  }

  cachedAuthStatusEnvKey = envKey;
  cachedAuthStatus = status;
  cachedAuthStatusAt = Date.now();
  return status;
}

export async function authenticateRequest(
  request: Request,
  env: SupabaseAuthEnv
): Promise<AuthenticatedUser | null> {
  const token = authorizationToken(request);
  if (!token || !env.SUPABASE_URL || !env.SUPABASE_ANON_KEY) {
    return null;
  }

  let response: Response;
  try {
    response = await fetch(`${env.SUPABASE_URL}/auth/v1/user`, {
      headers: {
        apikey: env.SUPABASE_ANON_KEY,
        authorization: `Bearer ${token}`
      }
    });
  } catch {
    return null;
  }
  if (!response.ok) {
    return null;
  }

  const json = (await response.json()) as {
    id?: string;
    email?: string;
  };
  if (!json.id || !json.email) {
    return null;
  }

  return {
    id: json.id,
    email: json.email
  };
}

export async function deleteSupabaseUser(
  userId: string,
  env: SupabaseAuthEnv
): Promise<void> {
  if (!env.SUPABASE_URL || !env.SUPABASE_SERVICE_ROLE_KEY) {
    throw new Error("Supabase account deletion is not configured.");
  }

  const response = await fetch(`${env.SUPABASE_URL}/auth/v1/admin/users/${userId}`, {
    method: "DELETE",
    headers: {
      apikey: env.SUPABASE_SERVICE_ROLE_KEY,
      authorization: `Bearer ${env.SUPABASE_SERVICE_ROLE_KEY}`
    }
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Supabase user deletion failed with ${response.status}.`);
  }
}

export function __resetAuthCacheForTests() {
  cachedAuthStatusEnvKey = null;
  cachedAuthStatus = null;
  cachedAuthStatusAt = 0;
}
