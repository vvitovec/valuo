import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { prefillListingFromUrl } from "../src/prefill";
import worker from "../src/index";

const root = resolve(import.meta.dirname, "..");
const workspaceRoot = resolve(root, "..");
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

function loadRawFixture(relativePath: string) {
  return JSON.parse(
    readFileSync(resolve(workspaceRoot, relativePath), "utf-8")
  ) as {
    listing_url: string;
    html: string;
  };
}

const bezrealitkyFixture = loadRawFixture(
  "data/raw/bezrealitky/1001327/2026-03-30T19-09-50.730806+00-00--ef1f661eae57e868.json"
);
const realityMixFixture = loadRawFixture(
  "data/raw/realitymix/8548461/2026-03-30T18-13-50.873705+00-00--2f96bc4922134ead.json"
);
const remaxFixture = loadRawFixture(
  "data/raw/remax/433510/2026-03-30T18-22-07.455728+00-00--495e95641631acb0.json"
);

function stubListingFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input instanceof Request ? input.url : input);
      if (url === bezrealitkyFixture.listing_url) {
        return new Response(bezrealitkyFixture.html, {
          headers: { "content-type": "text/html; charset=utf-8" }
        });
      }
      if (url === realityMixFixture.listing_url) {
        return new Response(realityMixFixture.html, {
          headers: { "content-type": "text/html; charset=utf-8" }
        });
      }
      if (url === remaxFixture.listing_url) {
        return new Response(remaxFixture.html, {
          headers: { "content-type": "text/html; charset=utf-8" }
        });
      }
      return new Response("not found", { status: 404 });
    })
  );
}

function makeEnv() {
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
    }
  };
}

function makeRequest(input: string, init?: RequestInit) {
  return new Request(input, init) as unknown as Request<
    unknown,
    IncomingRequestCfProperties<unknown>
  >;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("listing prefill parser", () => {
  it("parses Bezrealitky detail into form fields", async () => {
    stubListingFetch();
    const result = await prefillListingFromUrl(bezrealitkyFixture.listing_url);
    expect(result.source).toBe("bezrealitky");
    expect(result.fields.address).toContain("Heřmanova");
    expect(result.fields.districtPrague).toBe("Praha 7");
    expect(result.fields.propertyType).toBe("flat");
    expect(result.fields.disposition).toBe("2+kk");
    expect(result.fields.floorAreaM2).toBe(42);
    expect(result.fields.askingPriceCzk).toBe(8750000);
    expect(result.fields.hasElevator).toBe(true);
  });

  it("parses RealityMix detail into form fields", async () => {
    stubListingFetch();
    const result = await prefillListingFromUrl(realityMixFixture.listing_url);
    expect(result.source).toBe("realitymix");
    expect(result.fields.address).toContain("Běhounkova");
    expect(["Praha 13", "Stodůlky"]).toContain(result.fields.districtPrague);
    expect(result.fields.propertyType).toBe("flat");
    expect(result.fields.disposition).toBe("2+kk");
    expect(result.fields.floorAreaM2).toBe(36);
    expect(result.fields.askingPriceCzk).toBe(5990000);
    expect(result.fields.condition).toBe("very_good");
  });

  it("parses RE/MAX detail into form fields", async () => {
    stubListingFetch();
    const result = await prefillListingFromUrl(remaxFixture.listing_url);
    expect(result.source).toBe("remax");
    expect(result.fields.address).toContain("Za Zelenou liškou");
    expect(result.fields.districtPrague).toBe("Praha 4");
    expect(result.fields.propertyType).toBe("flat");
    expect(result.fields.floorAreaM2).toBe(48);
    expect(result.fields.askingPriceCzk).toBe(6300000);
    expect(result.fields.ownership).toBe("osobni");
  });
});

describe("listing prefill endpoint", () => {
  it("returns parsed fields from the worker API", async () => {
    stubListingFetch();
    const response = await worker.fetch(
      makeRequest("https://example.com/api/prefill-listing", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url: remaxFixture.listing_url })
      }),
      makeEnv(),
      {} as ExecutionContext
    );
    expect(response.status).toBe(200);
    const body = (await response.json()) as {
      source: string;
      fields: { address: string; askingPriceCzk: number };
    };
    expect(body.source).toBe("remax");
    expect(body.fields.address).toContain("Za Zelenou liškou");
    expect(body.fields.askingPriceCzk).toBe(6300000);
  });
});
