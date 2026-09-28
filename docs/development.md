# Development

## Setup

```bash
uv sync --extra dev
cp .env.example .env            # Garmin login optional for UI work
uv run hart-init-db
HART_ENV=dev uv run hart serve  # http://127.0.0.1:8765, no sign-in
```

`uv run hart demo` creates `data/demo.duckdb` with a fictional athlete — run the server with
`HART_DB_PATH=data/demo.duckdb` to work on the UI with realistic data (syncs, Claude and seed files are off for
a demo database). The README screenshots come from it.

With an empty database most pages show empty states; `uv run hart sync all` fills it from Garmin, or
`uv run hart import <garmin-export.zip>` from FIT files. Seed files from `examples/seed` (copy them to
`data/seed`) add a race, notes and lab results.

Without `CLAUDE_CODE_OAUTH_TOKEN` (or a logged-in Claude Code on the machine) Claude runs fail with an
authentication error; everything else works.

## Tests

```bash
uv run --extra dev pytest
```

The suite (about 190 tests) needs no network and no Claude: Claude runs use a scripted fake client
(`tests/test_chat.py::FakeClient`), Garmin a fake manager. Web tests go through FastAPI's `TestClient` with an
identity header and the CSRF header (`tests/test_grading.py`: `H`, `W`).

## Layout

```
src/hart/
  analytics/        pure computation (load, recovery, readiness, guardrails, health rules, strength…)
  ingestion/        Garmin client, FIT parser, bulk import
  storage/          schema + migrations, writers, queries
  server/           FastAPI app, routes, jobs, Claude runner, chat, grading, suggestions, plan, health, settings
    web/            templates (Jinja) and static assets (CSS, JS, vendored ECharts, fonts, icons)
  mcp_server.py     MCP tools
  interfaces/cli/   `hart` CLI
tests/              pytest suite
examples/seed/      fictional seed files
deploy/             Dockerfile, compose example, Claude workspace
.claude/            Claude Code subagents and the Garmin workout formatter skill
docs/               documentation
```

## Conventions

- **Code computes facts, Claude interprets.** Put anything that must be correct in `analytics/` with tests;
  prompts get the results as a JSON bundle.
- **No personal data in code.** Athlete details belong in notes, seed files (gitignored under `data/`) or
  Settings. Tests use fictional data.
- **New setting?** Add it to `hart.server.settings.REGISTRY` (it appears on the Settings page and in
  `docs/configuration.md`'s table — regenerate that table when you change the registry).
- **Schema change?** Bump `CURRENT_SCHEMA_VERSION` and add an idempotent step in `_run_migrations`.
- **Prompt change?** Bump the prompt's version constant; it's stored with every run.
- UI: server-rendered Jinja, vanilla JS (`api.*` helpers add the CSRF header), no build step. Static URLs carry
  `?v=<mtime>` so deploys never serve stale files.
