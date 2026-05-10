# HousesPredict v2

Prague residential asking-price prediction service. The repository contains a Python data pipeline for scraping, curation, model training, and export, plus a Cloudflare Worker frontend/API for predictions, auth, billing, and the premium market-opportunity dashboard.

## Repository Layout

- `pipeline/` - Python package for source adapters, quality checks, feature engineering, model training, and dashboard feed generation.
- `worker-app/` - Cloudflare Worker API, static frontend, D1 migrations, and Worker tests.
- `shared/` - JSON configuration shared by the pipeline and Worker.
- `ops/` - Operational scripts for local runners, scheduled pipelines, Cloudflare publishing, and housekeeping.
- `.github/workflows/` - Scheduled and manual pipeline workflows.

Runtime data and model artifacts are intentionally ignored. On macOS the pipeline defaults to `~/Library/Application Support/HousesPredict-v2`; override it with `HOUSESPREDICT_RUNTIME_DIR`, or set `HOUSESPREDICT_USE_REPO_RUNTIME=1` to use repo-local `data/` and `artifacts/`.

## Setup

Requirements:

- Python 3.14
- Node.js 22
- npm
- Cloudflare Wrangler for Worker development and publishing

Install dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./pipeline[dev]
npm install
```

Create local Worker environment variables from the example:

```bash
cp worker-app/.dev.vars.example worker-app/.dev.vars
```

Fill in the private values in `worker-app/.dev.vars`. Never commit that file.

## Development

Run the Worker locally:

```bash
npm run dev --workspace worker-app
```

Run checks:

```bash
npm run typecheck:worker
npm run test:worker
npm run test:python
npm test
```

Useful pipeline commands:

```bash
npm run probe:sources
npm run scrape
npm run refresh
npm run train
npm run backfill
npm run status
npm run run-all
```

## Operations

Cloudflare bootstrap notes live in [`ops/cloudflare-bootstrap.md`](ops/cloudflare-bootstrap.md).

Publish refreshed artifacts and dashboard rows:

```bash
./ops/publish-cloudflare.sh
```

Scheduled production jobs are split by responsibility:

- `./ops/run-scrape-publish.sh` every 6 hours for fresh listings and dashboard feed publishing.
- `./ops/run-train-publish.sh` nightly for training, promotion gating, and publishing.
- `./ops/run-backfill.sh` manually for dataset growth.
- `./ops/run-housekeeping.sh` weekly for D1 retention cleanup.

GitHub Actions require these repository secrets:

- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ACCOUNT_ID`
- `ALERT_WEBHOOK_URL` optional

If the Cloudflare secrets are missing, the scheduled jobs still produce local reports and artifacts but skip remote publishing/housekeeping instead of failing the workflow.

## Worker API

- `GET /api/config`
- `GET /api/me`
- `POST /api/predict`
- `POST /api/prefill-listing`
- `GET /api/dashboard/teaser`
- `GET /api/dashboard/opportunities`
- `POST /api/billing/create-checkout-session`
- `POST /api/billing/create-portal-session`
- `POST /api/account/delete`
- `POST /api/billing/webhook`
- `GET /api/health`

## Worker Environment

Required:

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`
- `STRIPE_SECRET_KEY`
- `STRIPE_PRICE_ID`
- `STRIPE_WEBHOOK_SECRET`
- `APP_BASE_URL`

Optional:

- `PREMIUM_PLAN_CODE` defaults to `premium_monthly`
- `PREMIUM_PRICE_LABEL` defaults to `Měsíční předplatné`

## Notes

- Predictions estimate typical Prague asking prices, not final transaction prices.
- The Worker is inference-only; scraping, quality checks, and training run outside Cloudflare.
- Candidate models are versioned, and the active model changes only after promotion gates pass.
