# InsightPilot native workbench

This app reuses the DataFoundry React/Next.js workbench from `apps/web`. The layout,
styles, chat renderer, resource panels, composer popovers and result console are the
upstream implementations. `datafoundry-source-manifest.json` records their original
SHA-256 hashes so later updates can be reviewed against the source. Apache-2.0
licensing remains in the repository root.

The business analysis engine remains in `references/askdata_studio/backend`.
`app/foundry_adapter` converts its authorized resources, saved analyses and background
runs to the REST and AG-UI contracts expected by this UI. No DataFoundry account,
data store, inference engine or session cookie is shared. UI metadata lives in
`backend/data/foundry_ui.sqlite3`; query results remain in the original stores.

## Run

From the repository root, install the existing workspace dependencies normally.
Build this app with `npm run build --workspace @insightpilot/web`. Then use:

```sh
references/askdata_studio/backend/.venv/bin/python scripts/homenova_phase_e/start.py restart --restart-frontend --record-dir /tmp/insightpilot-workbench-runtime
```

Open `http://127.0.0.1:5174/data-tasks`. The backend remains on port 8001 and the
original DataFoundry remains on port 3000. Login uses the existing business analysis
accounts. Its normal process-local login sessions expire when the backend restarts.

## Adapter boundaries

- Each conversation uses one authorized datasource; the native popover selects it
  and locks it after the first turn. Models, tools and optional Skills reach the real
  background execution context.
- Resource definitions are owned by the business backend. Adding/editing connections
  and credentials in this copied UI is disabled; configuration selection still works.
- Knowledge/file uploads and artifact promotion are disabled by backend capabilities.
- A branch starts a new analysis conversation with the selected earlier evidence.
  It does not clone a LangGraph execution checkpoint or replay SQL automatically.
- Clarification replies use one of the displayed option names. “继续” resumes a partial
  investigation with the existing backend continuation mechanism.
- Stop is cooperative: an in-flight model/tool request returns before later work stops.
- UI transport reconnections and history restoration never start another model run.

The Vue frontend under `references/askdata_studio/frontend` is retained for rollback.

## Live execution and history

The adapter records actual stage transitions and MCP start/arguments/result events
in an owner-scoped execution journal. SSE drains that journal while work runs;
reconnects replay the same event IDs, and completed history restores the same stages
and tools. Model phases remain activities, so they do not inflate tool-call counts.
The trace graph includes live phases and final checkpoints, with Chinese UI labels.

Sidebar reads use task metadata rather than restoring all reports. Conversation
reads restrict restoration to the requested session and inherited evidence. The
composer polls only its own active-run endpoint and prevents overlapping requests.
AG-UI connection handshakes do not count as analysis execution or block history.
Cancellation acknowledges the stop immediately in the stream; the worker rejects
subsequent external calls at its next boundary. It is not suspend/resume of an
in-flight provider request. Successful results with `error: null` remain successful.
