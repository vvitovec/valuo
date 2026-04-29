import metroConfig from "../../shared/metro-subregions.json";
import { canonicalizePragueDistrict } from "./districts";
import { stripAccents } from "./text";

const PRAGUE_CENTER = { lat: 50.0755, lng: 14.4378 };
const METRO_REGION_RADIUS_KM = 40;

export const METRO_REGION_LABEL = "Praha okolí";

type MetroRecord = {
  canonical: string;
  aliases: string[];
};

export function normalizeLocationKey(value: string | null | undefined): string {
  return stripAccents(
    (value ?? "")
      .replace(/\u00a0/g, " ")
      .replace(/[–—]/g, "-")
      .trim()
      .toLowerCase()
      .replace(/\s+/g, " ")
  ).replace(/^[,\s-]+|[,\s-]+$/g, "");
}

const metroAliasMap = new Map<string, string>();
for (const region of metroConfig.subregions as MetroRecord[]) {
  for (const alias of [region.canonical, ...region.aliases]) {
    const normalized = normalizeLocationKey(alias);
    if (normalized && !metroAliasMap.has(normalized)) {
      metroAliasMap.set(normalized, region.canonical);
    }
  }
}

export function canonicalizeMetroSubregion(
  value: string | null | undefined
): string | null {
  if (!value) {
    return null;
  }
  return metroAliasMap.get(normalizeLocationKey(value)) ?? null;
}

export function haversineKm(
  lat1: number,
  lng1: number,
  lat2: number,
  lng2: number
): number {
  const radiusKm = 6371;
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLng = ((lng2 - lng1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((lat1 * Math.PI) / 180) *
      Math.cos((lat2 * Math.PI) / 180) *
      Math.sin(dLng / 2) ** 2;
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return radiusKm * c;
}

function bearingFromPrague(lat: number, lng: number): number {
  const lat1 = (PRAGUE_CENTER.lat * Math.PI) / 180;
  const lat2 = (lat * Math.PI) / 180;
  const diffLng = ((lng - PRAGUE_CENTER.lng) * Math.PI) / 180;
  const y = Math.sin(diffLng) * Math.cos(lat2);
  const x =
    Math.cos(lat1) * Math.sin(lat2) -
    Math.sin(lat1) * Math.cos(lat2) * Math.cos(diffLng);
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}

export function directionalMetroCluster(
  lat: number | null | undefined,
  lng: number | null | undefined
): string {
  if (lat == null || lng == null) {
    return METRO_REGION_LABEL;
  }
  const distance = haversineKm(lat, lng, PRAGUE_CENTER.lat, PRAGUE_CENTER.lng);
  if (distance <= 12) {
    return "Praha okraj";
  }
  const bearing = bearingFromPrague(lat, lng);
  if (bearing >= 45 && bearing < 135) {
    return "Praha okolí východ";
  }
  if (bearing >= 135 && bearing < 225) {
    return "Praha okolí jih";
  }
  if (bearing >= 225 && bearing < 315) {
    return "Praha okolí západ";
  }
  return "Praha okolí sever";
}

export function isWithinPragueMetroRegion(
  lat: number | null | undefined,
  lng: number | null | undefined
): boolean {
  if (lat == null || lng == null) {
    return false;
  }
  return haversineKm(lat, lng, PRAGUE_CENTER.lat, PRAGUE_CENTER.lng) <= METRO_REGION_RADIUS_KM;
}

export function normalizeMarketArea(
  listingValue: string | null | undefined,
  externalValue: string | null | undefined,
  manualValue: string | null | undefined,
  lat: number | null | undefined,
  lng: number | null | undefined
): string {
  return (
    canonicalizePragueDistrict(listingValue) ??
    canonicalizePragueDistrict(externalValue) ??
    canonicalizePragueDistrict(manualValue) ??
    (isWithinPragueMetroRegion(lat, lng) ? METRO_REGION_LABEL : manualValue?.trim()) ??
    METRO_REGION_LABEL
  );
}

export function deriveLocationCluster(
  values: Array<string | null | undefined>,
  districtPrague: string | null | undefined,
  lat: number | null | undefined,
  lng: number | null | undefined
): string {
  for (const value of [districtPrague, ...values]) {
    if (!value) {
      continue;
    }
    const parts = [value, ...value.split(/[,/|]/g)].map((part) => part.trim());
    for (const part of parts) {
      const district = canonicalizePragueDistrict(part);
      if (district) {
        return district;
      }
      const metroRegion = canonicalizeMetroSubregion(part);
      if (metroRegion) {
        return metroRegion;
      }
    }
  }
  const marketArea = normalizeMarketArea(values[0], values[1], districtPrague, lat, lng);
  if (marketArea !== METRO_REGION_LABEL) {
    return marketArea;
  }
  return directionalMetroCluster(lat, lng);
}

export function inferMarketSegment(
  locationCluster: string | null | undefined,
  districtPrague: string | null | undefined
): "prague" | "metro" {
  return canonicalizePragueDistrict(locationCluster) || canonicalizePragueDistrict(districtPrague)
    ? "prague"
    : "metro";
}
