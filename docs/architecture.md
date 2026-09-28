# Architecture

```
Garmin Connect ──┐                       ┌── Web UI (Jinja + vanilla JS + ECharts, PWA)
.fit files ──────┤                       ├── JSON API (/api, CSRF-protected)
                 ▼                       │
            sync pipeline ──► DuckDB ◄───┼── MCP server (/mcp) ◄── Claude Code (Ember, grading,
                 ▲              ▲        │                          suggestions; your laptop)
          job runner + scheduler │       └── CLI (hart …)
                 │               │
          Claude runner (Agent SDK, your subscription) ──► Discord (evening message)
```

One Python process (`hart serve`, FastAPI + uvicorn) does everything. DuckDB allows a single process to open
the database, so the server owns it: the CLI and your laptop's Claude Code talk to the server (`/api`,
`/mcp`) instead of opening the file.

## Layers

| Package | Role |
|---|---|
| `hart.ingestion` | Garmin Connect client (`SyncManager`), FIT parsing, bulk import. |
| `hart.storage` | Schema and migrations, writers, read queries, the read-only SQL guard. |
| `hart.analytics` | Pure computation: training load (CTL/ATL/TSB), recovery, efficiency and decoupling, power curve, anomalies, phases and observed state, readiness, grading features, guardrails, health-check rules, strength progression. |
| `hart.server` | The web service: routes and templates, jobs and scheduler, Claude runner, chat, grading, suggestions, plan, health, settings. |
| `hart.mcp_server` | 42 MCP tools over the same database (stdio on its own, or mounted at `/mcp` in the server). |
| `hart.interfaces.cli` | `hart` command line. |

Code computes facts; Claude interprets. Anything that must be right — zones, comparisons, readiness,
guardrails, health rules, grade arithmetic — is deterministic Python. Claude writes judgement and prose on
top, and its numbers are checked against the facts it was given.

## Sync pipeline

`hart.server.jobs.pipeline.run_sync_pipeline` is the only sync implementation (UI, CLI, MCP and the scheduler
all enqueue it):

1. Garmin health: daily summary, sleep, HRV (merge-upsert — a missing value never erases a stored one).
2. Garmin activities: new ones with FIT streams, laps, strength sets and metrics; renamed ones get their new
   name; recent gym sessions get their edited sets; recent RPE/feel edits are picked up.
3. Training load, recovery scores, materialised views, anomaly detection.
4. Plan matching, then follow-up jobs: grading for new sessions (and regrading for edited ones), today's
   suggestion once last night's sleep has arrived, a refresh of tomorrow's suggestion after a late session.

Garmin auth failures pause scheduled syncs until you run `hart auth`; rate limits back off exponentially.

## Jobs and schedule

`JobRunner` keeps jobs in the `jobs` table and runs them in two lanes: **io** (syncs, backups, messages) and
**claude** (grading, suggestions, Garmin workouts), so a slow Claude run never delays a sync. Duplicates are
refused, not queued twice. The `Scheduler` ticks every 30 seconds; its times come from Settings → Schedule:

- morning watch: light syncs until last night's sleep arrives, then a full sync;
- hourly syncs; an hourly sweep that grades anything missed;
- today's suggestion by the end of the morning watch at the latest; tomorrow's at the preliminary time;
- daily health-check evaluation; nightly backup; the evening message.

The web UI shows running jobs in a bar under the header and refreshes once when they finish.

## Ember and other Claude runs

`ClaudeRunner` starts Claude Code through the Claude Agent SDK with your `CLAUDE_CODE_OAUTH_TOKEN`:

- **Tools:** only hart's MCP tools (over loopback `/mcp` with an internal token); no shell, no file access.
  Read-only runs (grading, suggestions) get read tools only. Chat additionally gets proposal tools and — when
  you enable it for a conversation — web search/fetch behind a filter.
- **Proposals, not writes:** Ember can propose notes, plan changes, season/race changes and health checks;
  nothing changes until you apply them.
- **Structured output:** grading, suggestions, paste parsing and Garmin workout conversion return JSON
  validated by Pydantic, with one repair retry.
- **Runs are recorded** in `claude_runs` (tool calls, tokens, duration); usage limits and auth failures pause
  background work instead of retrying.

Nothing about the athlete is in the prompts. Their name and setup come from Settings; everything else from
their notes, which every prompt tells Claude to read first (`get_athlete_context`).

### Session grading

`grading_features.build_features` computes zones, stream-based decoupling, lap variability, a comparison with
similar sessions (same sport and indoor/outdoor, ±25% duration; 90 days, falling back to a year), strength top
sets, and the context of the day (readiness, phase, constraints, feedback). Claude scores execution, response
and context fit 1–5 with a justification each; code computes the overall score and letter and checks every
cited number against the facts and tool results — too many unverifiable numbers make the grade low-confidence.

### Daily suggestions

`suggestions.build_context` gives Claude readiness, the coach's plan, the phase, 14 days of sessions, 4 weeks
of totals, weekly targets from the notes, the notes themselves, the hard limits, and — when regenerating — the
previous suggestion and what changed since. `analytics.guardrails` then checks the answer: red readiness →
no hard work; the coach's plan can be shortened or eased, not extended; note rules; weekly minimums that can't
wait; comeback duration caps. A violating answer gets one repair with the violations listed, then is stored as
failed and not shown.

## Other features

- **Season:** phases are generated backwards from the next A-race (taper, peak, build, base blocks, a comeback
  phase after a detected layoff) and are yours to edit; the observed state (building, maintaining, absorbing,
  detraining, overreaching risk) comes from the load curve.
- **Plan:** pasted coach text → preview → sessions; activities are matched by date, sport and duration;
  accepted suggestions replace coach sessions without deleting them; runs and rides go to Garmin as structured
  workouts (`garmin_workouts`).
- **Health:** lab results with a marker catalogue (Polish and English names); reminder rules in
  `analytics.health_checks` (regular panels timed to the season, follow-ups on flagged or near-limit results,
  stale markers, planned re-checks from notes, pre-race medical, physio check-ins), reconciled daily.
- **Strength:** `analytics.strength_progress` — top sets, estimated 1-rep max, weekly sessions. Lifts are
  merged at read time by `exercise_key` (the name without a `BARBELL_` prefix that matches the category, then
  the athlete's aliases from the `exercise_aliases` app setting); `strength_sets` keeps Garmin's names. See
  [configuration](configuration.md#strength-exercise-names).
- **Settings:** `hart.server.settings` registry — default ← env variable ← Settings page.

## Data model

Raw: `activities`, `activity_streams`, `activity_laps`, `strength_sets`, `hrv_samples`, `daily_health`,
`sleep_records`, `hrv_daily`. Computed: `activity_metrics`, `daily_training_load`, `daily_recovery`,
`weekly_summary`, `anomaly_log`. App: `training_phases`, `races`, `annotations`, `athlete_notes`,
`planned_sessions`, `session_feedback`, `session_grades`, `daily_suggestions`, `season_proposals`,
`lab_results`, `health_checks`, `chat_conversations`, `chat_messages`, `claude_runs`, `jobs`, `app_settings`.
`describe_schema` (MCP) lists every column. Migrations run on start (`storage/schema.py`).
