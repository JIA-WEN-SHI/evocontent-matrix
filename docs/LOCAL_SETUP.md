# EvoContent Matrix

Monorepo implementation for the EvoContent Matrix MVP:

- `apps/web`: Next.js + Tailwind + shadcn-style UI dashboard
- `services/api`: FastAPI REST API (approval, audit, webhook, strategy)
- `services/agent`: LangGraph worker (drafting, publishing, reflection)
- `infra/supabase/migrations`: SQL migrations for Supabase/PostgreSQL

## 1. Repository Layout

```text
apps/
  web/
services/
  api/
  agent/
infra/
  supabase/
    migrations/
tests/
```

## 2. Quick Start

1. Copy `.env.example` to `.env` and fill values.
   - Generate a valid Fernet key for `PII_ENCRYPTION_KEY`:
   - `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. Run SQL migrations in Supabase SQL editor:
   - `infra/supabase/migrations/001_init.sql`
   - `infra/supabase/migrations/002_seed.sql`
   - `infra/supabase/migrations/003_matrix_pipeline.sql`
   - `infra/supabase/migrations/004_daily_ops_reports.sql`
   - `infra/supabase/migrations/005_repair_japan_domain_text.sql` (repair garbled Chinese text in existing data)
   - `infra/supabase/migrations/006_channel_accounts.sql` (multi-account login/profile management)
   - `infra/supabase/migrations/007_channel_accounts_credentials.sql`
   - `infra/supabase/migrations/008_runs_decisions_memory.sql`
   - `infra/supabase/migrations/010_intelligence_items_account_scope.sql`
   - `infra/supabase/migrations/011_account_runtime_prompt_versions.sql`
   - `infra/supabase/migrations/012_kb_core_entities.sql`
   - `infra/supabase/migrations/013_kb_tags_links.sql`
   - `infra/supabase/migrations/014_kb_ingest_jobs.sql`
   - `infra/supabase/migrations/015_kb_compat_views.sql`
   - `infra/supabase/migrations/016_kb_rls_baseline.sql`
   - `infra/supabase/migrations/017_rpa_orchestration.sql`
   - `infra/supabase/migrations/018_execution_governance.sql`
   - `infra/supabase/migrations/019_kb_rule_playbook_io.sql`
   - Tip: `powershell -ExecutionPolicy Bypass -File scripts/copy_migration_sql.ps1 -Migration 003` to copy SQL content (use `004/005` the same way).
3. Start API service:
   - `cd services/api`
   - `pip install -e .`
   - `uvicorn app.main:app --reload --port 8000`
4. Start Agent service:
   - `cd services/agent`
   - `pip install -e .`
   - `uvicorn app.main:app --reload --port 8100`
5. Start Web:
   - `cd apps/web`
   - `npm install`
   - `npm run dev`

### Recommended Local Start Order

Always run these commands from the repository root: `D:\自己新项目`

1. Start API (`8000`)
   - `cd services/api`
   - `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`
2. Start Agent (`8100`)
   - `cd services/agent`
   - `uvicorn app.main:app --reload --host 0.0.0.0 --port 8100`
3. Start Web (`3001`)
   - `cd D:\自己新项目`
   - `npm install`
   - `npm run dev --workspace @evocontent/web`

If the web page shows `无法连接到 API 服务（http://127.0.0.1:8000）`:
- confirm API is really running on `8000`
- confirm the page is opened with either `http://127.0.0.1:3001` or `http://localhost:3001`
- confirm `.env` contains `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000` or leave it blank for loopback auto-resolution
- do not run `npm --prefix apps/web ...` from `C:\Users\Administrator`; run from repository root instead

### One-Click Start (Windows)

- Double-click `start_dev.bat` (auto-opens browser to `/`)
- First run only: `start_dev.bat --bootstrap`
- Optional: run `powershell -ExecutionPolicy Bypass -File scripts/dev_env.ps1` in a new shell to load local Python/NPM path hints.
- Optional: run `powershell -ExecutionPolicy Bypass -File scripts/check_blockers.ps1` to view current blockers and required inputs.
- Optional: run `powershell -ExecutionPolicy Bypass -File scripts/check_playwright_state.ps1` to verify storage-state file.
- Or run:
  - `powershell -ExecutionPolicy Bypass -File scripts/dev_up.ps1 -OpenBrowser`
  - first run: `powershell -ExecutionPolicy Bypass -File scripts/dev_up.ps1 -Bootstrap -OpenBrowser`
- Stop:
  - `stop_dev.bat`
  - or `powershell -ExecutionPolicy Bypass -File scripts/dev_down.ps1`

### Playwright Session Requirements

- Publisher node does not perform login actions.
- Provide one of:
  - `PLAYWRIGHT_USER_DATA_DIR`
  - `PLAYWRIGHT_STORAGE_STATE_PATH`
  - `PLAYWRIGHT_SESSION_COOKIES_JSON`
- If Xiaohongshu crawler is blocked by IP risk (`461`), configure proxy:
  - `PLAYWRIGHT_PROXY_SERVER=http://host:port`
  - `PLAYWRIGHT_PROXY_USERNAME=...` (optional)
  - `PLAYWRIGHT_PROXY_PASSWORD=...` (optional)
- On DOM/risk failures, the worker saves screenshot + html under `PLAYWRIGHT_ARTIFACTS_DIR`.

#### Check Xiaohongshu Accessibility

```bash
python scripts/check_xhs_access.py --query "日本移民"
```

- Exit code `0`: page is accessible.
- Exit code `2`: blocked by risk/login wall, switch network/proxy and retry.

#### Export Xiaohongshu Storage State

```bash
python scripts/export_xhs_state.py --wait-seconds 60 --output xhs_storage_state.json
```

Then set in `.env`:

```bash
PLAYWRIGHT_STORAGE_STATE_PATH=xhs_storage_state.json
```

## 3. Demo Workflow

1. Insert a `pipeline_tasks` row in Supabase with status `queued` (or use `POST /api/pipeline/tasks`).
2. Trigger draft generation:
   - `POST /api/pipeline/tasks/{task_id}/run`
3. Review from `/approval`, then approve.
4. Approval page triggers publish run automatically.
5. Send lead webhook payload to `/api/webhooks/leads`.
6. Trigger reflection:
   - `POST /api/scheduler/reflect`

### Quick Seed For Local UI / API Smoke Test

Use management token to insert one `queued`, one `pending_review`, and one `published` task:

```bash
SUPABASE_MANAGEMENT_TOKEN=... SUPABASE_PROJECT_REF=... python scripts/seed_demo_runtime.py
```

For a full **closed-loop showcase** on landing page (public/account cards + pending review + SOP suggestions + coach brief):

```bash
python scripts/seed_closed_loop_demo.py --mode seed
```

Clean only this demo data:

```bash
python scripts/seed_closed_loop_demo.py --mode reset
```

If network to Supabase is unstable, open landing with demo fallback:

```text
http://127.0.0.1:3001/?demo=1
```

### One-Command End-to-End Smoke

This script starts `api + agent + web`, runs draft/approve/publish/webhook checks, then stops services:

```bash
python scripts/smoke_e2e.py
```

## 4. Core Routes

### API

- `GET /api/domains`
- `GET /api/domains/{slug}`
- `POST /api/domains/{slug}/strategy/versions`
- `POST /api/webhooks/leads`
- `POST /api/scheduler/reflect`
- `POST /api/scheduler/daily-run`
- `POST /api/scheduler/daily-viewpoint-run`
- `GET /api/scheduler/daily-status`
- `GET /api/scheduler/daily-reports`
- `GET /api/system/readiness`
- `GET /api/system/feature-checklist`
- `GET /api/pipeline/tasks`
- `POST /api/pipeline/tasks`
- `GET /api/pipeline/tasks/{id}`
- `POST /api/pipeline/tasks/{id}/approve`
- `POST /api/pipeline/tasks/{id}/reject`
- `POST /api/pipeline/tasks/{id}/run`
- `GET /api/pipeline/tasks/{id}/feedback`
- `GET /api/pipeline/tasks/{id}/audits`
- `POST /api/assistant/chat`
- `GET /api/ops/crawler-template`
- `PUT /api/ops/crawler-template`
- `POST /api/ops/analyze-intel`
- `GET /api/accounts`
- `POST /api/accounts`
- `PATCH /api/accounts/{id}`
- `DELETE /api/accounts/{id}`
- `POST /api/accounts/{id}/verify-login`
- `GET /api/kb/cases`
- `POST /api/kb/import/octopus`
- `POST /api/kb/topics/recommend`
- `GET /api/kb/ingestion-logs`
- `GET /api/kb/ai-jobs`
- `GET /api/kb/rpa/task-templates`
- `GET /api/kb/rpa/task-runs`
- `GET /api/kb/rpa/raw-payloads`
- `GET /api/kb/rpa/clean-results`
- `POST /api/kb/rpa/runs/{id}/replay`
- `POST /api/webhooks/kb-octopus`

### Web

- `/`
- `/dashboard`
- `/legacy-flow`
- `/accounts`
- `/tasks/[id]`
- `/settings/security`

### Daily Ops (Step 5)

- Trigger once manually:
  - `POST /api/scheduler/daily-run?domain_slug=japan_immigration`
- Trigger viewpoint-only loop:
  - `POST /api/scheduler/daily-viewpoint-run?domain_slug=japan_immigration`
- Query scheduler/runtime status:
  - `GET /api/scheduler/daily-status?domain_slug=japan_immigration&flow=full`
- Query recent run reports:
  - `GET /api/scheduler/daily-reports?domain_slug=japan_immigration&limit=20&flow=full`
- Query viewpoint run reports:
  - `GET /api/scheduler/daily-reports?domain_slug=japan_immigration&limit=20&flow=viewpoint`
- Flow:
  - collect hotspots (Xiaohongshu page search via Playwright + Google News RSS by domain queries)
  - write `intelligence_items`
  - create one `pipeline_task`
  - run draft path
  - auto approve + publish (if enabled)
  - run reflection
- Viewpoint Flow (Step 6):
  - collect external viewpoints (Google News RSS by viewpoint queries)
  - create one `pipeline_task` with `track=viewpoint_expansion`
  - force "观点拆解 -> 反驳/补充 -> 行动引导" style in drafting
  - auto approve + publish (if enabled)
- Optional background scheduler:
  - controlled by `DAILY_SCHEDULER_*` in `.env`
  - retries controlled by `DAILY_RUN_MAX_ATTEMPTS` and `DAILY_RETRY_BACKOFF_SECONDS`

### Compatibility Mode

- If `pipeline_tasks` table is missing (migration `003_matrix_pipeline.sql` not applied),
  core task flow will be degraded and key pages cannot run normally.
- Check current mode and missing tables:
  - `GET /api/system/readiness`

## 5. Security Baseline

- Field-level encryption for lead contact fields
- RBAC via request role headers (`x-user-role`)
- Append-only audit logs
- Webhook signature validation via HMAC SHA256

## 6. Docs Index

- `docs/PRD.md`
- `docs/AGENTS.md`
- `docs/implementation-issues.md`
- `docs/data-dictionary.md`
- `docs/api-spec.md`
- `docs/runbooks/kb-pipeline.md`
- `docs/runbooks/octopus-rpa-webhook.md`
- `docs/runbooks/octopus-json-contract.md`
- `docs/runbooks/rpa-edge-pipeline.md`
- `infra/supabase/functions/README.md`
- `docs/v2-cutover.md`
