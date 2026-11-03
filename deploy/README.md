# Cloud deployment preparation

Status: local packaging preparation; not approved for public visitor traffic yet.

The container packages the Next.js standalone server, Python API, and public
HomeNova snapshots. Next.js listens on `PORT` (default 10000) and proxies to the
API at loopback port 8001. Both processes stop if either process exits. Existing
local development and its default build output are unchanged.

Build from the repository root with `docker build -f deploy/Dockerfile .`.
Supply model secrets only as runtime environment variables. No local credentials,
session databases, trace logs or private evaluation files enter the build context.

## Required before public release

- Replace shared demo identities with isolated visitor identities; disable known
  demo passwords and prevent visitor access to administration APIs.
- Make accepted origins and secure cookies match the actual HTTPS site.
- Enforce the agreed limit of two analyses per visitor per day, one global active
  analysis, and a CNY 10 daily model budget. Monetary accounting must include chat,
  embeddings, reranking, retries and unknown outcomes, with conservative reservation
  before dispatch. Verify provider prices; do not equate request count with cost.
- Persist quota reservations outside Render's ephemeral filesystem. Fail closed
  if quota storage is unavailable; restart must not reset the daily budget.
- Treat visitor cookies as an identification aid, not a unique-human guarantee;
  global limits must still hold if visitors clear cookies.
- Test actual container peak memory under a query, frontend/API readiness and
  long-task behavior. Render's free instance has only 512 MB RAM.

Render Free sleeps when idle and loses locally written files on restart. Saved
visitor histories therefore need durable storage if retention is required. Never
upload the developer's existing task history as the deployment database.

No paid compute subscription is authorized. Account connection, secure visitor
mode, persistent quotas and a deployment check are still required before release.

## Preparation checks (2026-11-03)

- Clean `npm ci` and `INSIGHTPILOT_STANDALONE=1 npm run build` passed.
- The generated standalone server returned HTTP 200 for the login page and three
  referenced static assets, using an isolated localhost port and no real API.
- Python supervisor syntax and whitespace checks passed.
- The local Docker daemon is not running, so the Linux image build, API integration
  and container peak-memory checks have not yet run.
- No real model calls, source database changes, GitHub push or cloud deployment
  occurred in this preparation step.
