# Cloud deployment preparation

Status: deployed on Render Free at https://insightpilot-demo.onrender.com.
Cloud health, guest login, independent visitor histories and a real SQL analysis
passed. Report continuation testing found a missing single-query evidence catalog;
the fix has offline coverage and is awaiting cloud verification.

The container packages the Next.js standalone server, Python API, and public
HomeNova snapshots. Next.js listens on `PORT` (default 10000) and proxies to the
API at loopback port 8001. Both processes stop if either process exits. Existing
local development and its default build output are unchanged.

Build from the repository root with `docker build -f deploy/Dockerfile .`.
Supply model secrets only as runtime environment variables. No local credentials,
session databases, trace logs or private evaluation files enter the build context.

## Isolation and trial controls

- Container deployments enable `INSIGHTPILOT_PUBLIC_DEMO=1`. Local development
  leaves it unset, preserving local accounts, records and task budgets.
- The image copies application code and public HomeNova snapshots only. The
  developer's databases, history, model traces, credentials, interview notes and
  private evaluation files are excluded from the build context.
- Each browser receives a signed visitor identity. Existing default passwords
  are disabled in trial mode. Guests can access only `homenova_20260831`; existing
  owner checks separate their conversations, results and artifacts.
- Trial usage lives in the private `insightpilot_trial` Postgres schema. It holds
  counters, run leases and monetary reservations, not prompts or business data.
  Local model calls never touch this ledger. Test fixtures use temporary stores.
- Each visitor may start two analyses per Shanghai calendar day. Answering an
  existing clarification or resuming an authorized task does not consume another
  start. Only one public run can be active globally. A crashed run's lease expires
  after 15 minutes; daily counters remain intact.
- Before **each** chat, embedding and rerank attempt, including retries, Postgres
  must commit a conservative reservation within the CNY 10 daily limit. Reported
  usage settles the reservation once; unknown outcomes retain it. Missing or
  unavailable quota storage rejects the request before provider dispatch.
- Public chat requests disable thinking and cap output at 16,384 tokens. Only the
  configured DashScope mainland endpoints and four explicitly priced model IDs
  are allowed. Local generation behavior is unchanged.
- Rates conservatively exceed the published regional prices for those models;
  promotional discounts are ignored. Review them when changing providers/models.
  Source checked 2026-11-03: [Alibaba Cloud model pricing](https://help.aliyun.com/zh/model-studio/model-pricing).

Visitor cookies identify browsers, not unique humans. Clearing cookies may obtain
a new visitor identity; the global monetary limit and concurrency gate still
apply. App accounting applies only to this deployment, not other uses of the same
provider key. A separate provider key is preferable for independent billing.

## Runtime configuration

Set server-side environment variables (never put values in Git or browser code):

- `INSIGHTPILOT_PUBLIC_ORIGIN`: exact public HTTPS origin, no path. When omitted
  on Render, use its automatically assigned `RENDER_EXTERNAL_URL`.
- `INSIGHTPILOT_VISITOR_SECRET`: randomly generated secret, at least 32 characters;
  keep stable across redeploys.
- `INSIGHTPILOT_QUOTA_DATABASE_URL`: authorized Postgres connection with access to
  the private quota schema. Apply `deploy/quota_schema.sql` once before opening.
- `LLM_API_KEY`: model credential; `LLM_MODEL=qwen3.7-plus`,
  `ROUTE_MODEL=qwen3.7-flash`, `EMBEDDING_MODEL=text-embedding-v4`,
  `RERANK_MODEL=qwen3-rerank`.
- `MODEL_TRACE_DIR`: a private backend directory if full model traces are needed.
  It must not be served as frontend assets.

On Render select Web Service, Docker, repository root as context,
`deploy/Dockerfile`, and **Free** instance. No paid compute plan is authorized.

## Required before public release

- For a fresh deployment, initialize its independent Supabase quota schema with
  `deploy/quota_admin.py initialize --secrets-file <private-file> --project-ref <ref>`.
  The original project's database is not reused.
- For a fresh deployment, configure secrets in Render's server environment after authorization.
- Test actual container peak memory under a query, frontend/API readiness and
  long-task behavior. Render's free instance has only 512 MB RAM.

Render Free sleeps when idle and loses locally written files on restart. Saved
visitor histories therefore need durable storage if retention is required. Never
upload the developer's existing task history as the deployment database.

## Preparation checks (2026-11-03)

- Clean `npm ci` and `INSIGHTPILOT_STANDALONE=1 npm run build` passed.
- The generated standalone server returned HTTP 200 for the login page and three
  referenced static assets, using an isolated localhost port and no real API.
- Python supervisor syntax and whitespace checks passed.
- Public authentication, caller isolation, known-account rejection, failed quota
  dispatch, retry reservation and unchanged local mode have offline coverage.
- Native Python API imports and all 15 HomeNova tables loaded/read successfully
  with DuckDB's trial memory limit of 128 MB and one thread. Observed macOS peak
  RSS was approximately 264 MiB. This does not establish a Linux 512 MB container
  peak, especially with Next.js and a complete analysis running together.
- Render successfully built and started the Docker image on Free compute.
  A live two-month metric query completed; peak memory for a full eight-round
  investigation is not yet established.
- The newly created, independent demo Supabase project is reachable. Live checks
  use a randomly named disposable schema, separate from public counters. They
  cover competing starts, parallel monetary reservations, daily reset, two-start
  limits, clarification continuation, idempotent settlement, unknown outcomes
  across new connections, and persistent halt on unexpected provider overage.
- 30 offline tests passed, including Render-assigned origin handling and visitor
  record isolation. Live checks do not call a model or consume model budget.
- All 7 live Postgres checks passed; their disposable schema was cleaned up.
  The independent demo project's 4 production quota tables were initialized.
- Real cloud model calls are recorded by the independent quota ledger. The first
  live test produced a saved table and analysis. A follow-up report exposed that
  ordinary query results lacked the stable R1 catalog already expected by the
  evidence reader. The fix publishes the same ID to the router and requires empty
  result selection when no catalog exists; owner/source checks remain intact.
- 75 focused offline regressions passed after that fix, including saved-result
  continuation and rejection of another visitor's evidence.
