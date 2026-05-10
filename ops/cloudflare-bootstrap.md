# Cloudflare Bootstrap

This repository is prepared for a separate Python runner plus a Cloudflare Worker serving layer.

## 1. Authenticate Wrangler

```bash
cd /Users/viktorvitovec/Documents/Projekty/HousesPredict-v2/worker-app
npx wrangler whoami
npx wrangler login
```

## 2. Create D1 and R2

```bash
npx wrangler d1 create praha-price-predictor
npx wrangler r2 bucket create praha-price-models
```

Copy the returned `database_id` into [wrangler.jsonc](/Users/viktorvitovec/Documents/Projekty/HousesPredict-v2/worker-app/wrangler.jsonc) and uncomment the `d1_databases` and `r2_buckets` bindings.

## 3. Apply migrations

```bash
npx wrangler d1 migrations apply praha-price-predictor --remote
```

The latest migration also creates service tables for:

- user profiles
- free prediction usage tracking
- premium entitlements
- Stripe customer mapping
- scored market opportunities for the dashboard

## 4. Configure Worker secrets / vars

Set the product env on the Worker:

```bash
npx wrangler secret put SUPABASE_URL
npx wrangler secret put SUPABASE_ANON_KEY
npx wrangler secret put STRIPE_SECRET_KEY
npx wrangler secret put STRIPE_WEBHOOK_SECRET
npx wrangler secret put APP_BASE_URL
npx wrangler secret put STRIPE_PRICE_ID
```

Optional non-secret vars can be added in `wrangler.jsonc` or via dashboard:

- `PREMIUM_PLAN_CODE`
- `PREMIUM_PRICE_LABEL`

## 5. Validate and deploy

```bash
npx wrangler check
npx wrangler deploy
```

## 6. Pipeline runner

You have two supported scheduling options.

Option A: separate Linux host

Run the Python pipeline on a separate Linux host with:

```bash
./ops/run-scrape-publish.sh
./ops/run-train-publish.sh
```

Install the example cron from [pipeline.crontab.example](/Users/viktorvitovec/Documents/Projekty/HousesPredict-v2/ops/pipeline.crontab.example).

Option B: GitHub Actions

Enable these workflows and set these repository secrets:

- [.github/workflows/market-scrape-publish.yml](/Users/viktorvitovec/Documents/Local/Projekty/HousesPredict-v2/.github/workflows/market-scrape-publish.yml)
- [.github/workflows/market-train-publish.yml](/Users/viktorvitovec/Documents/Local/Projekty/HousesPredict-v2/.github/workflows/market-train-publish.yml)
- [.github/workflows/market-backfill.yml](/Users/viktorvitovec/Documents/Local/Projekty/HousesPredict-v2/.github/workflows/market-backfill.yml)
- [.github/workflows/market-housekeeping.yml](/Users/viktorvitovec/Documents/Local/Projekty/HousesPredict-v2/.github/workflows/market-housekeeping.yml)

- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ACCOUNT_ID`
- `ALERT_WEBHOOK_URL` optional

The GitHub Actions setup provisions Python and Node, runs scrape/publish every 6 hours, train/publish nightly, and uploads generated reports as workflow artifacts.
Without the Cloudflare secrets, scheduled runs skip remote publish/housekeeping and exit successfully so notification email stays quiet until publishing is configured.
