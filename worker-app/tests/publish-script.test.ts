import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { execFileSync } from "node:child_process";
import { afterEach, describe, expect, it } from "vitest";

const root = resolve(import.meta.dirname, "..", "..");
const reportsDir = resolve(root, "data", "reports");

const cleanupPaths: string[] = [];

function runScript(script: string, env: Record<string, string>) {
  execFileSync("bash", [resolve(root, script)], {
    cwd: root,
    env: { ...process.env, ...env },
    stdio: "pipe"
  });
}

afterEach(() => {
  while (cleanupPaths.length) {
    const path = cleanupPaths.pop();
    if (path) {
      rmSync(path, { recursive: true, force: true });
    }
  }
});

describe("publish-cloudflare script", () => {
  it("builds transactional SQL and seeds pipeline run registry in dry-run mode", () => {
    runScript("ops/write-pipeline-run-report.sh", {
      PIPELINE_RUN_ID: "run-test-scrape",
      PIPELINE_RUN_TYPE: "scrape",
      PIPELINE_RUN_STATUS: "success",
      PIPELINE_RUN_STARTED_AT: "2026-04-18T00:00:00Z",
      PIPELINE_RUN_FINISHED_AT: "2026-04-18T00:10:00Z",
      PIPELINE_SUMMARY_JSON: JSON.stringify({ degradedSources: [] }),
      PIPELINE_ERROR_JSON: "null"
    });
    cleanupPaths.push(resolve(reportsDir, "pipeline-scrape-latest.json"));
    cleanupPaths.push(resolve(reportsDir, "pipeline-scrape-run-test-scrape.json"));

    const dryRunDir = mkdtempSync(resolve(tmpdir(), "valuo-publish-"));
    cleanupPaths.push(dryRunDir);

    runScript("ops/publish-cloudflare.sh", {
      PIPELINE_RUN_ID: "run-test-scrape",
      PUBLISH_DRY_RUN_DIR: dryRunDir
    });

    const sql = readFileSync(resolve(dryRunDir, "d1-seed.sql"), "utf-8");
    expect(sql).toContain("BEGIN IMMEDIATE;");
    expect(sql).toContain("COMMIT;");
    expect(sql).toContain("DELETE FROM market_listing_score;");
    expect(sql).toContain("INSERT OR REPLACE INTO pipeline_run_registry");
    expect(sql).toContain("run-test-scrape");
  });
});
