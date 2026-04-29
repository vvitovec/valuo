import { chooseBestPragueDistrict } from "./districts";
import {
  canonicalizeMetroSubregion,
  METRO_REGION_LABEL,
  normalizeMarketArea
} from "./market";
import type { ListingPrefillFields, ListingPrefillResponse } from "./types";

const BEZREALITKY_NEXT_DATA_RE =
  /<script id="__NEXT_DATA__" type="application\/json">\s*(.*?)\s*<\/script>/s;
const REALITYMIX_TITLE_RE = /<title>(.*?)<\/title>/is;
const REALITYMIX_HEADING_ADDRESS_RE =
  /<p class="advert-detail-heading__address">(.*?)<\/p>/is;
const REALITYMIX_HEADING_TITLE_RE =
  /<h1 class="advert-detail-heading__title">(.*?)<\/h1>/is;
const REALITYMIX_SHORT_PROP_ROW_RE =
  /<tr[^>]*>\s*<td>\s*(.*?)\s*<\/td>\s*<td>\s*(.*?)\s*<\/td>\s*<\/tr>/gis;
const REALITYMIX_DETAIL_ITEM_RE =
  /<li class="detail-information__data-item">\s*<span>(.*?)<\/span>\s*<span>(.*?)<\/span>\s*<\/li>/gis;
const REALITYMIX_GPS_LAT_RE = /data-gps-lat="([^"]+)"/i;
const REALITYMIX_GPS_LON_RE = /data-gps-lon="([^"]+)"/i;
const REMAX_TITLE_RE = /<h1[^>]*>(.*?)<\/h1>/is;
const REMAX_ADDRESS_RE = /<h2 class="pd-header__address">(.*?)<\/h2>/is;
const REMAX_PRICE_RE = /<h2 class="pd-header__price">(.*?)<\/h2>/is;
const REMAX_GPS_RE = /data-gps="([^"]+)"/i;
const REMAX_ROW_RE =
  /<div class="pd-detail-info__row">\s*<div class="pd-detail-info__label">(.*?)<\/div>\s*<div class="pd-detail-info__value">(.*?)<\/div>\s*<\/div>/gis;

type SupportedSource = ListingPrefillResponse["source"];

function decodeHtml(value: string): string {
  return value
    .replace(/&#(\d+);/g, (_match, code) => String.fromCodePoint(Number(code)))
    .replace(/&#x([0-9a-f]+);/gi, (_match, code) =>
      String.fromCodePoint(Number.parseInt(code, 16))
    )
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/gi, "'")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&ndash;/gi, "–")
    .replace(/&mdash;/gi, "—")
    .replace(/&hellip;/gi, "…");
}

function cleanHtmlText(value: string | null | undefined): string {
  if (!value) {
    return "";
  }
  return decodeHtml(value)
    .replace(/<br\s*\/?>/gi, ", ")
    .replace(/<[^>]+>/g, " ")
    .replace(/\u00a0/g, " ")
    .replace(/\s+/g, " ")
    .replace(/\s+,/g, ",")
    .trim();
}

function normalizeUrl(rawUrl: string): string {
  const candidate = rawUrl.trim();
  return /^https?:\/\//i.test(candidate) ? candidate : `https://${candidate}`;
}

function parseJsonScript<T>(html: string, pattern: RegExp): T {
  const match = pattern.exec(html);
  if (!match) {
    throw new Error("Detail stránky neobsahuje očekávaná data.");
  }
  return JSON.parse(decodeHtml(match[1])) as T;
}

function buildFieldRecord(
  partial: Partial<ListingPrefillFields>,
  notes: string[]
): ListingPrefillFields {
  if (!partial.address || partial.address.length < 3) {
    throw new Error("Nepodařilo se určit adresu inzerátu.");
  }
  if (!partial.districtPrague || partial.districtPrague.length < 2) {
    throw new Error("Nepodařilo se určit lokalitu inzerátu.");
  }
  if (!partial.propertyType) {
    throw new Error("Nepodařilo se určit typ nemovitosti.");
  }
  if (!partial.floorAreaM2 || partial.floorAreaM2 <= 0) {
    throw new Error("Nepodařilo se určit plochu nemovitosti.");
  }

  return {
    address: partial.address,
    districtPrague: partial.districtPrague,
    propertyType: partial.propertyType,
    disposition: partial.disposition ?? "",
    floorAreaM2: partial.floorAreaM2,
    landAreaM2: partial.landAreaM2,
    condition: partial.condition ?? "unknown",
    ownership: partial.ownership ?? "unknown",
    construction: partial.construction ?? "unknown",
    floorNo: partial.floorNo,
    totalFloors: partial.totalFloors,
    hasElevator: partial.hasElevator ?? false,
    hasParking: partial.hasParking ?? false,
    hasCellar: partial.hasCellar ?? false,
    hasBalconyOrLoggia: partial.hasBalconyOrLoggia ?? false,
    energyLabel: partial.energyLabel ?? "unknown",
    askingPriceCzk: partial.askingPriceCzk
  };
}

function parseNumber(value: unknown): number | undefined {
  if (value == null || value === "") {
    return undefined;
  }
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : undefined;
  }
  const match = String(value).match(/-?\d+(?:[.,]\d+)?/);
  if (!match) {
    return undefined;
  }
  const parsed = Number(match[0].replace(",", "."));
  return Number.isFinite(parsed) ? parsed : undefined;
}

function parsePrice(value: unknown): number | undefined {
  if (value == null || value === "") {
    return undefined;
  }
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : undefined;
  }
  const digits = cleanHtmlText(String(value)).replace(/[^\d]/g, "");
  if (!digits) {
    return undefined;
  }
  const parsed = Number(digits);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function normalizeEnergyLabel(value: string | null | undefined): string | undefined {
  if (!value) {
    return undefined;
  }
  const match = cleanHtmlText(value).toLowerCase().match(/[a-g]/);
  return match?.[0];
}

function normalizeOwnership(value: string | null | undefined): string | undefined {
  if (!value) {
    return undefined;
  }
  const normalized = cleanHtmlText(value)
    .normalize("NFKD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase();
  if (normalized.includes("osob")) {
    return "osobni";
  }
  if (normalized.includes("druz")) {
    return "druzstevni";
  }
  return normalized || undefined;
}

function normalizeCondition(value: string | null | undefined): string | undefined {
  if (!value) {
    return undefined;
  }
  const normalized = cleanHtmlText(value).toLowerCase();
  const mapping: Record<string, string> = {
    new: "new",
    novostavba: "new",
    "ve výstavbě": "new",
    "ve vystavbe": "new",
    very_good: "very_good",
    "velmi dobrý": "very_good",
    "velmi dobry": "very_good",
    "po rekonstrukci": "very_good",
    good: "good",
    dobrý: "good",
    dobry: "good",
    before_reconstruction: "before_reconstruction",
    "před rekonstrukcí": "before_reconstruction",
    "pred rekonstrukci": "before_reconstruction"
  };
  return mapping[normalized] ?? normalized;
}

function normalizeConstruction(value: string | null | undefined): string | undefined {
  if (!value) {
    return undefined;
  }
  const normalized = cleanHtmlText(value).toLowerCase();
  const mapping: Record<string, string> = {
    brick: "brick",
    cihla: "brick",
    cihlová: "brick",
    cihlova: "brick",
    panel: "panel",
    panelová: "panel",
    panelova: "panel",
    mixed: "mixed",
    smíšená: "mixed",
    smisena: "mixed",
    wood: "wood",
    dřevěná: "wood",
    drevena: "wood"
  };
  return mapping[normalized] ?? normalized;
}

function parseYesNoFlag(value: string | null | undefined): boolean | undefined {
  if (!value) {
    return undefined;
  }
  const normalized = cleanHtmlText(value).toLowerCase();
  if (normalized === "ano") {
    return true;
  }
  if (normalized === "ne") {
    return false;
  }
  const count = parseNumber(normalized);
  if (count != null) {
    return count > 0;
  }
  return true;
}

function parseDmsCoordinate(value: string): number | undefined {
  const match = cleanHtmlText(value).match(/(\d+)°(\d+)'(\d+(?:\.\d+)?)"?([NSEW])/i);
  if (!match) {
    return undefined;
  }
  const degrees = Number(match[1]);
  const minutes = Number(match[2]);
  const seconds = Number(match[3]);
  if (![degrees, minutes, seconds].every(Number.isFinite)) {
    return undefined;
  }
  let decimal = degrees + minutes / 60 + seconds / 3600;
  if (/[SW]/i.test(match[4])) {
    decimal *= -1;
  }
  return decimal;
}

function chooseMarketArea(
  primary: string | null | undefined,
  secondary: string | null | undefined,
  fallback: string | null | undefined,
  lat: number | undefined,
  lng: number | undefined
): string {
  const candidates = [primary, secondary, fallback]
    .flatMap((value) => {
      if (!value) {
        return [];
      }
      return [
        value,
        ...String(value)
          .split(/[,/|]/g)
          .map((part) => part.trim())
          .filter(Boolean)
      ];
    })
    .filter(Boolean);

  return (
    candidates
      .map((candidate) => chooseBestPragueDistrict(candidate, null, null))
      .find(Boolean) ??
    candidates
      .map((candidate) => canonicalizeMetroSubregion(candidate))
      .find(Boolean) ??
    normalizeMarketArea(primary, secondary, fallback, lat ?? null, lng ?? null) ??
    METRO_REGION_LABEL
  );
}

async function fetchListingHtml(listingUrl: string): Promise<string> {
  const response = await fetch(listingUrl, {
    redirect: "follow",
    headers: {
      "accept-language": "cs-CZ,cs;q=0.9,en;q=0.7"
    }
  });
  if (!response.ok) {
    throw new Error(`Stažení inzerátu selhalo (${response.status}).`);
  }
  return await response.text();
}

function detectSource(listingUrl: string): SupportedSource {
  const { hostname } = new URL(listingUrl);
  if (hostname.includes("bezrealitky.cz")) {
    return "bezrealitky";
  }
  if (hostname.includes("realitymix.cz")) {
    return "realitymix";
  }
  if (hostname.includes("remax-czech.cz")) {
    return "remax";
  }
  throw new Error("Podporované jsou jen odkazy z Bezrealitky, RealityMix a RE/MAX.");
}

function parseBezrealitkyListing(
  html: string,
  listingUrl: string
): ListingPrefillResponse {
  const pageData = parseJsonScript<Record<string, unknown>>(html, BEZREALITKY_NEXT_DATA_RE);
  const pageProps = ((pageData.props as { pageProps?: Record<string, unknown> } | undefined)
    ?.pageProps ?? {}) as Record<string, unknown>;
  const advert =
    (pageProps.advert as Record<string, unknown> | undefined) ??
    (pageProps.origAdvert as Record<string, unknown> | undefined);
  if (!advert) {
    throw new Error("Bezrealitky detail neobsahuje advert payload.");
  }

  const gps = (advert.gps as { lat?: number; lng?: number } | undefined) ?? {};
  const lat = typeof gps.lat === "number" ? gps.lat : undefined;
  const lng = typeof gps.lng === "number" ? gps.lng : undefined;
  const regionTree = Array.isArray(pageProps.regionTree)
    ? pageProps.regionTree
    : Array.isArray(advert.regionTree)
      ? advert.regionTree
      : [];
  const regionHints = regionTree
    .map((entry) =>
      entry && typeof entry === "object" && "name" in entry
        ? String((entry as { name?: unknown }).name ?? "")
        : ""
    )
    .filter(Boolean)
    .join(", ");
  const address = cleanHtmlText(
    String(
      advert.address ??
        [advert.street, advert.city].filter(Boolean).join(", ")
    )
  );
  const districtPrague = chooseMarketArea(
    regionHints,
    address,
    String(advert.city ?? ""),
    lat,
    lng
  );
  const floorNo =
    parseNumber(advert.floor) ??
    parseNumber(advert.etage);
  const notes: string[] = [];

  return {
    source: "bezrealitky",
    listingUrl,
    fields: buildFieldRecord(
      {
        address,
        districtPrague,
        propertyType: advert.estateType === "DUM" ? "house" : "flat",
        disposition: cleanHtmlText(String(advert.disposition ?? ""))
          .replace(/^DISP_/i, "")
          .replace(/_/g, "+")
          .toLowerCase(),
        floorAreaM2: parseNumber(advert.surface),
        landAreaM2: parseNumber(advert.landSurface ?? advert.surfaceLand),
        condition: normalizeCondition(String(advert.condition ?? advert.buildingCondition ?? "")),
        ownership: normalizeOwnership(String(advert.ownership ?? "")),
        construction: normalizeConstruction(String(advert.construction ?? "")),
        floorNo,
        totalFloors: parseNumber(advert.totalFloors ?? advert.numberOfFloors),
        hasElevator: Boolean(advert.lift),
        hasParking: Boolean(advert.parking ?? advert.garage),
        hasCellar: Boolean(advert.cellar),
        hasBalconyOrLoggia: Boolean(advert.balcony || advert.loggia || advert.terrace),
        energyLabel: normalizeEnergyLabel(String(advert.penb ?? "")),
        askingPriceCzk: parsePrice(advert.price)
      },
      notes
    ),
    notes
  };
}

function parseRealityMixRows(
  html: string,
  pattern: RegExp
): Record<string, string> {
  const output: Record<string, string> = {};
  for (const match of html.matchAll(pattern)) {
    const key = cleanHtmlText(match[1]).replace(/:$/, "");
    const value = cleanHtmlText(match[2]);
    if (key) {
      output[key] = value;
    }
  }
  return output;
}

function parseRealityMixListing(
  html: string,
  listingUrl: string
): ListingPrefillResponse {
  const titleText = cleanHtmlText(html.match(REALITYMIX_TITLE_RE)?.[1]);
  const headingTitle = cleanHtmlText(html.match(REALITYMIX_HEADING_TITLE_RE)?.[1]);
  const headingAddress = cleanHtmlText(html.match(REALITYMIX_HEADING_ADDRESS_RE)?.[1]);
  const shortProps = parseRealityMixRows(html, REALITYMIX_SHORT_PROP_ROW_RE);
  const detailItems = parseRealityMixRows(html, REALITYMIX_DETAIL_ITEM_RE);
  const lat = parseNumber(html.match(REALITYMIX_GPS_LAT_RE)?.[1]);
  const lng = parseNumber(html.match(REALITYMIX_GPS_LON_RE)?.[1]);

  const address =
    headingAddress ||
    cleanHtmlText(
      titleText.split(",").slice(1).join(",")
    ) ||
    cleanHtmlText(headingTitle);
  const districtPrague = chooseMarketArea(
    headingAddress,
    titleText,
    headingTitle,
    lat,
    lng
  );
  const disposition =
    detailItems["Dispozice bytu"] ??
    shortProps["Dispozice/podlahová plocha"]?.split("/")[0];
  const floorAreaM2 =
    parseNumber(detailItems["Užitná plocha"]) ??
    parseNumber(detailItems["Celková podlahová plocha"]) ??
    parseNumber(detailItems["Podlahová plocha"]) ??
    parseNumber(shortProps["Dispozice/podlahová plocha"]) ??
    parseNumber(titleText);
  const notes: string[] = [];

  if (!headingAddress && districtPrague === METRO_REGION_LABEL) {
    notes.push("Zdroj neposkytl přesnou adresu, proto byla použita obecnější lokalita Praha okolí.");
  }

  return {
    source: "realitymix",
    listingUrl,
    fields: buildFieldRecord(
      {
        address,
        districtPrague,
        propertyType: /prodej domu/i.test(`${titleText} ${headingTitle}`) ? "house" : "flat",
        disposition: cleanHtmlText(disposition).toLowerCase(),
        floorAreaM2,
        landAreaM2: parseNumber(detailItems["Plocha parcely"]),
        condition: normalizeCondition(detailItems["Stav objektu"]),
        ownership: normalizeOwnership(detailItems["Vlastnictví"]),
        construction: normalizeConstruction(detailItems["Konstrukce"] ?? detailItems["Druh objektu"]),
        floorNo:
          parseNumber(detailItems["Patro"]) ??
          parseNumber(detailItems["Číslo podlaží v domě"]),
        totalFloors:
          parseNumber(detailItems["Počet podlaží"]) ??
          parseNumber(detailItems["Počet podlaží objektu"]),
        hasElevator: parseYesNoFlag(detailItems["Výtah"]),
        hasParking:
          parseYesNoFlag(detailItems["Parkování"]) ??
          parseYesNoFlag(detailItems["Garáž"]) ??
          parseYesNoFlag(detailItems["Parkovací místo"]),
        hasCellar: parseYesNoFlag(detailItems["Sklep"]),
        hasBalconyOrLoggia:
          parseYesNoFlag(detailItems["Balkon"]) ??
          parseYesNoFlag(detailItems["Lodžie"]) ??
          parseYesNoFlag(detailItems["Terasa"]),
        energyLabel: normalizeEnergyLabel(detailItems["Energetická náročnost budovy"]),
        askingPriceCzk: parsePrice(shortProps.Cena)
      },
      notes
    ),
    notes
  };
}

function parseRemaxListing(
  html: string,
  listingUrl: string
): ListingPrefillResponse {
  const titleText = cleanHtmlText(html.match(REMAX_TITLE_RE)?.[1]);
  const addressText = cleanHtmlText(html.match(REMAX_ADDRESS_RE)?.[1]).replace(/\s+mapa$/i, "");
  const priceText = cleanHtmlText(html.match(REMAX_PRICE_RE)?.[1]);
  const detailRows = parseRealityMixRows(html, REMAX_ROW_RE);
  const gpsText = cleanHtmlText(html.match(REMAX_GPS_RE)?.[1]);
  const [latText, lngText] = gpsText.split(",").map((part) => part.trim());
  const lat = latText ? parseDmsCoordinate(latText) : undefined;
  const lng = lngText ? parseDmsCoordinate(lngText) : undefined;
  const districtPrague = chooseMarketArea(
    detailRows["Městská část"] ?? addressText,
    titleText,
    addressText,
    lat,
    lng
  );
  const notes: string[] = [];

  return {
    source: "remax",
    listingUrl,
    fields: buildFieldRecord(
      {
        address: addressText || titleText,
        districtPrague,
        propertyType: /prodej domu/i.test(titleText) ? "house" : "flat",
        disposition: cleanHtmlText(detailRows.Dispozice).toLowerCase(),
        floorAreaM2:
          parseNumber(detailRows["Podlahová plocha"]) ??
          parseNumber(detailRows["Užitná plocha"]) ??
          parseNumber(titleText),
        landAreaM2:
          parseNumber(detailRows["Plocha pozemku"]) ??
          parseNumber(detailRows["Zastavěná plocha"]),
        condition: normalizeCondition(detailRows["Stav objektu"]),
        ownership: normalizeOwnership(detailRows.Vlastnictví),
        construction: normalizeConstruction(detailRows["Druh objektu"]),
        floorNo: parseNumber(detailRows["Číslo podlaží"]),
        totalFloors: parseNumber(detailRows["Počet podlaží v objektu"]),
        hasElevator: parseYesNoFlag(detailRows.Výtah),
        hasParking:
          parseYesNoFlag(detailRows.Parkování) ??
          parseYesNoFlag(detailRows.Garáž) ??
          parseYesNoFlag(detailRows["Parkovací místo"]),
        hasCellar: parseYesNoFlag(detailRows.Sklep),
        hasBalconyOrLoggia:
          parseYesNoFlag(detailRows.Balkón) ??
          parseYesNoFlag(detailRows.Lodžie) ??
          parseYesNoFlag(detailRows.Terasa),
        energyLabel:
          normalizeEnergyLabel(detailRows["Energetická náročnost"] ?? detailRows["Energetická náročnost budovy"] ?? priceText),
        askingPriceCzk: parsePrice(priceText)
      },
      notes
    ),
    notes
  };
}

export async function prefillListingFromUrl(
  rawUrl: string
): Promise<ListingPrefillResponse> {
  const listingUrl = normalizeUrl(rawUrl);
  const source = detectSource(listingUrl);
  const html = await fetchListingHtml(listingUrl);

  switch (source) {
    case "bezrealitky":
      return parseBezrealitkyListing(html, listingUrl);
    case "realitymix":
      return parseRealityMixListing(html, listingUrl);
    case "remax":
      return parseRemaxListing(html, listingUrl);
  }
}
