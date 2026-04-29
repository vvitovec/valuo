import districtConfig from "../../shared/prague-districts.json";
import { stripAccents } from "./text";

type DistrictRecord = {
  canonical: string;
  aliases: string[];
};

function normalizeDistrictKey(value: string | null | undefined): string {
  return stripAccents(
    (value ?? "")
      .replace(/\u00a0/g, " ")
      .replace(/[–—]/g, "-")
      .trim()
      .toLowerCase()
      .replace(/^hlavni mesto\s+/i, "")
      .replace(/^hlavní město\s+/i, "")
      .replace(/^mestska cast\s+/i, "")
      .replace(/^městská část\s+/i, "")
      .replace(/^obvod\s+/i, "")
      .replace(/^praha\s*-\s*/i, "")
      .replace(/\s+/g, " ")
  ).replace(/^[,\s-]+|[,\s-]+$/g, "");
}

const aliasMap = new Map<string, string>();
for (const district of districtConfig.districts as DistrictRecord[]) {
  for (const alias of [district.canonical, ...district.aliases]) {
    const normalized = normalizeDistrictKey(alias);
    if (normalized && !aliasMap.has(normalized)) {
      aliasMap.set(normalized, district.canonical);
    }
  }
}

export function canonicalizePragueDistrict(value: string | null | undefined): string | null {
  if (!value) {
    return null;
  }
  const normalized = normalizeDistrictKey(value);
  if (!normalized) {
    return null;
  }
  if (aliasMap.has(normalized)) {
    return aliasMap.get(normalized) ?? null;
  }
  const prefixedNumeric = normalized.match(/^praha\s+(\d+)\s*-\s*.+$/);
  if (prefixedNumeric) {
    return `Praha ${prefixedNumeric[1]}`;
  }
  const numeric = normalized.match(/^praha\s+(\d+)$/);
  if (numeric) {
    return `Praha ${numeric[1]}`;
  }
  return null;
}

export function chooseBestPragueDistrict(
  listingValue: string | null | undefined,
  externalValue: string | null | undefined,
  manualValue: string | null | undefined
): string | null {
  return (
    canonicalizePragueDistrict(listingValue) ??
    canonicalizePragueDistrict(externalValue) ??
    canonicalizePragueDistrict(manualValue)
  );
}
