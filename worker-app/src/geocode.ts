import { chooseBestPragueDistrict } from "./districts";
import {
  deriveLocationCluster,
  haversineKm,
  inferMarketSegment,
  METRO_REGION_LABEL,
  normalizeMarketArea
} from "./market";

type RuntimeEnv = {
  DB?: D1Database;
};

async function getCachedGeocode(env: RuntimeEnv, key: string) {
  if (!env.DB) {
    return null;
  }
  return env.DB
    .prepare(
      "SELECT lat, lng, district_prague FROM geocode_cache WHERE cache_key = ?1"
    )
    .bind(key)
    .first<{ lat: number; lng: number; district_prague: string }>();
}

async function putCachedGeocode(
  env: RuntimeEnv,
  key: string,
  lat: number,
  lng: number,
  district: string
) {
  if (!env.DB) {
    return;
  }
  await env.DB.prepare(
    `INSERT OR REPLACE INTO geocode_cache (
      cache_key, lat, lng, district_prague, updated_at
    ) VALUES (?1, ?2, ?3, ?4, datetime('now'))`
  )
    .bind(key, lat, lng, district)
    .run();
}

async function recordGeocodeAudit(
  env: RuntimeEnv,
  payload: {
    cacheKey: string;
    address: string;
    manualDistrict: string;
    resolvedDistrict: string;
    lat: number | null;
    lng: number | null;
    status: "cache_hit" | "cache_miss" | "fallback_manual";
  }
) {
  if (!env.DB) {
    return;
  }
  await env.DB.prepare(
    `INSERT INTO geocode_audit (
      created_at, cache_key, address, manual_district, resolved_district, lat, lng, status
    ) VALUES (datetime('now'), ?1, ?2, ?3, ?4, ?5, ?6, ?7)`
  )
    .bind(
      payload.cacheKey,
      payload.address,
      payload.manualDistrict,
      payload.resolvedDistrict,
      payload.lat,
      payload.lng,
      payload.status
    )
    .run();
}

export async function resolveAddress(
  address: string,
  manualDistrict: string,
  env: RuntimeEnv
) {
  const notes: string[] = [];
  const cacheKey = `geocode:${address.toLowerCase()}|${manualDistrict.toLowerCase()}`;
  const manualCanonical = chooseBestPragueDistrict(null, null, manualDistrict) ?? METRO_REGION_LABEL;
  const cached = await getCachedGeocode(env, cacheKey);
  if (cached) {
    const locationCluster = deriveLocationCluster([address, manualCanonical], cached.district_prague, cached.lat, cached.lng);
    await recordGeocodeAudit(env, {
      cacheKey,
      address,
      manualDistrict: manualCanonical,
      resolvedDistrict: cached.district_prague,
      lat: cached.lat,
      lng: cached.lng,
      status: "cache_hit"
    });
    return {
      lat: cached.lat,
      lng: cached.lng,
      districtPrague: cached.district_prague,
      locationCluster,
      marketSegment: inferMarketSegment(locationCluster, cached.district_prague),
      distanceToCenterKm: haversineKm(cached.lat, cached.lng, 50.0755, 14.4378),
      geocodeResolution: "exact" as const,
      notes
    };
  }

  const url = new URL("https://nominatim.openstreetmap.org/search");
  url.searchParams.set("q", `${address}, Česká republika`);
  url.searchParams.set("format", "jsonv2");
  url.searchParams.set("limit", "1");
  url.searchParams.set("addressdetails", "1");

  try {
    const response = await fetch(url.toString(), {
      headers: {
        "User-Agent": "HousesPredict-v2/0.2"
      }
    });
    if (!response.ok) {
      throw new Error(`Geocode HTTP ${response.status}`);
    }
    const json = (await response.json()) as Array<{
      lat: string;
      lon: string;
      address?: Record<string, string>;
    }>;
    const first = json[0];
    if (!first) {
      throw new Error("No geocode result");
    }
    const lat = Number(first.lat);
    const lng = Number(first.lon);
    const district = normalizeMarketArea(
      first.address?.city_district ?? first.address?.suburb ?? first.address?.borough ?? first.address?.quarter,
      first.address?.city,
      manualCanonical,
      lat,
      lng
    );
    const locationCluster = deriveLocationCluster(
      [
        first.address?.city_district,
        first.address?.suburb,
        first.address?.borough,
        first.address?.quarter,
        first.address?.city,
        address,
        manualCanonical
      ],
      district,
      lat,
      lng
    );
    await putCachedGeocode(env, cacheKey, lat, lng, district);
    await recordGeocodeAudit(env, {
      cacheKey,
      address,
      manualDistrict: manualCanonical,
      resolvedDistrict: district,
      lat,
      lng,
      status: "cache_miss"
    });
    if (district === METRO_REGION_LABEL) {
      notes.push("Adresa spadá do pražského okolí, proto byla použita metro-region lokalita Praha okolí.");
    } else if (district === manualCanonical) {
      notes.push("Geokodér nevrátil jednoznačnou lokalitu, proto byl zachován ruční vstup.");
    }
    return {
      lat,
      lng,
      districtPrague: district,
      locationCluster,
      marketSegment: inferMarketSegment(locationCluster, district),
      distanceToCenterKm: haversineKm(lat, lng, 50.0755, 14.4378),
      geocodeResolution: "exact" as const,
      notes
    };
  } catch {
    notes.push(
      "Geokódování adresy selhalo, proto byla použita náhradní lokalita Praha nebo Praha okolí a odhad může být méně přesný."
    );
    await recordGeocodeAudit(env, {
      cacheKey,
      address,
      manualDistrict: manualCanonical,
      resolvedDistrict: manualCanonical,
      lat: null,
      lng: null,
      status: "fallback_manual"
    });
    return {
      lat: null,
      lng: null,
      districtPrague: manualCanonical,
      locationCluster: deriveLocationCluster([address, manualCanonical], manualCanonical, null, null),
      marketSegment: inferMarketSegment(manualCanonical, manualCanonical),
      distanceToCenterKm: null,
      geocodeResolution: "fallback_manual" as const,
      notes
    };
  }
}
