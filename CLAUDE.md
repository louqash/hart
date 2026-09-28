# hart

Self-hosted endurance training companion: Garmin sync → DuckDB → analytics, a FastAPI web UI, background jobs,
an MCP server, and Ember (Claude Code via the Agent SDK). Start with `docs/architecture.md`.

## Working on the code

- Tests: `uv run --extra dev pytest` (no network or Claude needed — fakes in `tests/test_chat.py` and
  `tests/test_server.py`).
- Dev server: `HART_ENV=dev uv run hart serve` (127.0.0.1, no sign-in).
- **Code computes facts, Claude interprets.** Anything that must be correct lives in `src/hart/analytics/` with
  tests; prompts receive the results as JSON.
- **No personal data in the repository** — not in code, prompts, tests or docs. Athlete details belong in notes,
  seed files under `data/` (gitignored) or Settings. Tests use fictional data.
- New runtime setting → `hart.server.settings.REGISTRY` (+ regenerate the table in `docs/configuration.md`).
  Schema change → bump `CURRENT_SCHEMA_VERSION` with an idempotent migration. Prompt change → bump its version.
- Conventions and layout: `docs/development.md`.

## Analysing training data

When answering questions about the athlete's training, health or recovery:

- Access data **only through the `hart` MCP tools** — never open the DuckDB file directly (the server owns it;
  a second process gets a lock error).
- **Call `get_athlete_context` first.** The athlete's notes (goals, injuries, constraints, baselines) are the
  source of truth. To remember something new, call `propose_athlete_note` (the athlete approves it in the UI)
  rather than writing memory files. `get_training_phase` and `get_readiness` give the phase and today's readiness.
- **Never fabricate data.** Every number you present must come from a tool call in this conversation. If a tool
  returns nothing, say so. Use `run_sql_query` (after `describe_schema`) when the other tools don't cover it.
- Do not spawn sub-agents for data analysis; call the tools yourself.
- `sync_garmin` / `sync_all` enqueue a background job under the server — check it with `get_job_status`.
- Changes go through proposals the athlete approves: `propose_plan_change`, `propose_season_change`,
  `propose_health_check`, `propose_athlete_note`.
