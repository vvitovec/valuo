import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { execFileSync } from "node:child_process";
import { afterEach, describe, expect, it } from "vitest";

const root = resolve(import.meta.dirname, "..", "..");

const cleanupPaths: string[] = [];

function runScript(script: string, env: Record<string, string>) {
  return execFileSync("bash", [resolve(root, script)], {
    cwd: root,
    env: { ...process.env, ...env },
    stdio: "pipe"
  }).toString("utf-8");
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
  it("documents production cron entries with publish wrappers", () => {
    const crontab = readFileSync(resolve(root, "ops", "pipeline.crontab.example"), "utf-8");

    expect(crontab).toContain("./ops/run-scrape-publish.sh");
    expect(crontab).toContain("./ops/run-train-publish.sh");
    expect(crontab).not.toContain("./ops/run-scrape.sh >>");
    expect(crontab).not.toContain("./ops/run-refresh.sh >>");
  });

  it("builds transactional SQL and seeds pipeline run registry in dry-run mode", () => {
    const runtimeDir = mkdtempSync(resolve(tmpdir(), "valuo-runtime-"));
    const reportsDir = resolve(runtimeDir, "data", "reports");
    const artifactsDir = resolve(runtimeDir, "artifacts");
    mkdirSync(reportsDir, { recursive: true });
    mkdirSync(artifactsDir, { recursive: true });
    writeFileSync(resolve(reportsDir, "market-opportunities-latest.json"), "[]\n");
    cleanupPaths.push(runtimeDir);

    runScript("ops/write-pipeline-run-report.sh", {
      HOUSESPREDICT_RUNTIME_DIR: runtimeDir,
      HOUSESPREDICT_DATA_DIR: resolve(runtimeDir, "data"),
      HOUSESPREDICT_ARTIFACTS_DIR: artifactsDir,
      PIPELINE_RUN_ID: "run-test-scrape",
      PIPELINE_RUN_TYPE: "scrape",
      PIPELINE_RUN_STATUS: "success",
      PIPELINE_RUN_STARTED_AT: "2026-04-18T00:00:00Z",
      PIPELINE_RUN_FINISHED_AT: "2026-04-18T00:10:00Z",
      PIPELINE_SUMMARY_JSON: JSON.stringify({ degradedSources: [] }),
      PIPELINE_ERROR_JSON: "null"
    });

    const dryRunDir = mkdtempSync(resolve(tmpdir(), "valuo-publish-"));
    cleanupPaths.push(dryRunDir);

    runScript("ops/publish-cloudflare.sh", {
      HOUSESPREDICT_RUNTIME_DIR: runtimeDir,
      HOUSESPREDICT_DATA_DIR: resolve(runtimeDir, "data"),
      HOUSESPREDICT_ARTIFACTS_DIR: artifactsDir,
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

  it("bootstraps publish artifacts and tolerates a missing market feed", () => {
    const runtimeDir = mkdtempSync(resolve(tmpdir(), "valuo-runtime-"));
    const reportsDir = resolve(runtimeDir, "data", "reports");
    const artifactsDir = resolve(runtimeDir, "artifacts");
    mkdirSync(reportsDir, { recursive: true });
    cleanupPaths.push(runtimeDir);

    runScript("ops/write-pipeline-run-report.sh", {
      HOUSESPREDICT_RUNTIME_DIR: runtimeDir,
      HOUSESPREDICT_DATA_DIR: resolve(runtimeDir, "data"),
      HOUSESPREDICT_ARTIFACTS_DIR: artifactsDir,
      PIPELINE_RUN_ID: "run-test-clean",
      PIPELINE_RUN_TYPE: "scrape",
      PIPELINE_RUN_STATUS: "success",
      PIPELINE_RUN_STARTED_AT: "2026-05-04T04:00:00Z",
      PIPELINE_RUN_FINISHED_AT: "2026-05-04T04:10:00Z",
      PIPELINE_SUMMARY_JSON: JSON.stringify({ marketOpportunitiesRows: 0 }),
      PIPELINE_ERROR_JSON: "null"
    });

    const dryRunDir = mkdtempSync(resolve(tmpdir(), "valuo-publish-"));
    cleanupPaths.push(dryRunDir);

    runScript("ops/publish-cloudflare.sh", {
      HOUSESPREDICT_RUNTIME_DIR: runtimeDir,
      HOUSESPREDICT_DATA_DIR: resolve(runtimeDir, "data"),
      HOUSESPREDICT_ARTIFACTS_DIR: artifactsDir,
      PIPELINE_RUN_ID: "run-test-clean",
      PUBLISH_DRY_RUN_DIR: dryRunDir
    });

    const manifest = readFileSync(resolve(dryRunDir, "r2-upload-manifest.tsv"), "utf-8");
    const sql = readFileSync(resolve(dryRunDir, "d1-seed.sql"), "utf-8");
    expect(manifest).toContain("active-model.json");
    expect(manifest).toContain("model-registry.json");
    expect(sql).toContain("INSERT OR REPLACE INTO pipeline_run_registry");
    expect(sql).toContain("run-test-clean");
    expect(sql).not.toContain("DELETE FROM market_listing_score;");
  });

  it("skips remote publish when Cloudflare credentials are missing", () => {
    const runtimeDir = mkdtempSync(resolve(tmpdir(), "valuo-runtime-"));
    const reportsDir = resolve(runtimeDir, "data", "reports");
    const artifactsDir = resolve(runtimeDir, "artifacts");
    mkdirSync(reportsDir, { recursive: true });
    cleanupPaths.push(runtimeDir);

    const output = runScript("ops/publish-cloudflare.sh", {
      HOUSESPREDICT_RUNTIME_DIR: runtimeDir,
      HOUSESPREDICT_DATA_DIR: resolve(runtimeDir, "data"),
      HOUSESPREDICT_ARTIFACTS_DIR: artifactsDir,
      CLOUDFLARE_API_TOKEN: "",
      CLOUDFLARE_ACCOUNT_ID: "",
      PUBLISH_DRY_RUN_DIR: ""
    });

    const report = JSON.parse(readFileSync(resolve(reportsDir, "pipeline-publish-latest.json"), "utf-8"));
    expect(output).toContain("Cloudflare publish skipped");
    expect(report.status).toBe("skipped");
    expect(report.error.message).toContain("credentials are not configured");
  });

  it("skips D1 housekeeping when Cloudflare credentials are missing", () => {
    const runtimeDir = mkdtempSync(resolve(tmpdir(), "valuo-runtime-"));
    cleanupPaths.push(runtimeDir);

    const output = runScript("ops/run-housekeeping.sh", {
      HOUSESPREDICT_RUNTIME_DIR: runtimeDir,
      CLOUDFLARE_API_TOKEN: "",
      CLOUDFLARE_ACCOUNT_ID: ""
    });

    expect(output).toContain("Housekeeping skipped");
  });

  it("writes housekeeping SQL without explicit transactions", () => {
    const runtimeDir = mkdtempSync(resolve(tmpdir(), "valuo-runtime-"));
    const dryRunDir = mkdtempSync(resolve(tmpdir(), "valuo-housekeeping-"));
    cleanupPaths.push(runtimeDir, dryRunDir);

    const output = runScript("ops/run-housekeeping.sh", {
      HOUSESPREDICT_RUNTIME_DIR: runtimeDir,
      HOUSEKEEPING_DRY_RUN_DIR: dryRunDir,
      CLOUDFLARE_API_TOKEN: "",
      CLOUDFLARE_ACCOUNT_ID: ""
    });

    const sql = readFileSync(resolve(dryRunDir, "housekeeping.sql"), "utf-8");
    expect(output).toContain("Housekeeping dry run prepared");
    expect(sql).toContain("DELETE FROM prediction_audit");
    expect(sql).not.toContain("BEGIN");
    expect(sql).not.toContain("COMMIT");
  });
});
