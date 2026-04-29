import { describe, expect, it, vi } from "vitest";

import { resolveAddress } from "../src/geocode";

describe("geocode canonicalization", () => {
  it("falls back to canonical manual district when geocoder fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 500 })));
    const resolved = await resolveAddress("Poděbradská 777/9", "Vysočany", {});
    expect(resolved.districtPrague).toBe("Praha 9");
    expect(resolved.geocodeResolution).toBe("fallback_manual");
    expect(resolved.distanceToCenterKm).toBeNull();
    expect(resolved.notes[0]).toContain("Geokódování adresy selhalo");
  });

  it("maps nearby municipalities to Praha okolí", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify([
            {
              lat: "49.9912",
              lon: "14.6543",
              address: { city: "Říčany" }
            }
          ]),
          { headers: { "content-type": "application/json" } }
        )
      )
    );
    const resolved = await resolveAddress("Čechova 12, Říčany", "Říčany", {});
    expect(resolved.districtPrague).toBe("Praha okolí");
    expect(resolved.locationCluster).toBe("Praha-východ");
    expect(resolved.marketSegment).toBe("metro");
  });
});
