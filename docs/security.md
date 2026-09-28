# Security & privacy

hart holds sensitive data — training, health, sleep, lab results — so it's designed to keep it on your server
and to be safe to expose to your own devices through a proxy. It is a single-user app: everyone who can sign
in sees everything.

## Sign-in

- hart has **no passwords or accounts of its own**. It trusts a reverse proxy that has already authenticated
  you and passes your identity in a header (`HART_AUTH_HEADER`, by default Tailscale Serve's
  `Tailscale-User-Login`). Requests without it are rejected (`403`).
- `HART_ALLOWED_USERS` limits which identities get in.
- **Never expose the port directly** — the container binds to 127.0.0.1. With a non-Tailscale proxy, make sure
  it strips the identity header from incoming requests.
- `HART_ENV=dev` turns the check off and is refused unless hart listens on 127.0.0.1.
- `/healthz` is open (no data). `/mcp` also accepts an internal bearer token, generated per start, used only by
  the server's own Claude runs.

## Cross-site requests

Your browser attaches your proxy identity to any request, including one a malicious page tries to make. So:

- a request with a foreign `Origin` is rejected everywhere, `/mcp` included;
- every state-changing API call needs `X-Requested-With: hart` and a JSON body — a cross-site page can't send
  that without a CORS preflight, and hart answers no CORS;
- `GET` requests never change anything.

## What Claude can do

Ember, grading and suggestions run Claude Code with a tight policy (`server/claude/policy.py`):

- only hart's MCP tools — **no shell, no file access, no other MCP servers** (`strict_mcp_config`);
- grading and suggestions: read-only tools; chat: read tools plus *proposals* (notes, plan, season, health
  checks) that change nothing until you apply them, and starting a Garmin sync;
- `run_sql_query` accepts a single read-only `SELECT` (parsed by DuckDB, table functions and file paths refused);
- a working directory with no project files.

Numbers Claude reports in grades and suggestions are checked against the data it was given; prompts forbid
inventing numbers.

## What leaves your server

- **Garmin Connect** — sync requests with your credentials; workouts you choose to send.
- **Anthropic** — prompts and tool results of Claude runs, i.e. the data Ember reads to answer you, under your
  Claude plan's terms.
- **The web**, only when you enable web research in a chat: queries go through a guard that remembers the numbers
  and IDs the hart tools returned in that run and blocks queries or URLs containing them (and long query
  parameters); it learns numbers of three or more digits (years excepted). The prompt forbids putting personal
  data in searches. It's a tripwire, not a guarantee.
- **Discord**, if you configure the evening message.
- Nothing else — no analytics, telemetry or CDN requests (fonts, charts and icons are served locally).

## Secrets

`.env` (Garmin password, Claude token, Discord webhook) and the Garmin OAuth tokens in the data folder are the
secrets. Keep `.env` out of version control (it's in `.gitignore`), readable only by the service user. The
Settings page shows whether a secret is set, never its value.

## Backups

Nightly exports contain your full database and Garmin tokens. Store them somewhere as private as the server.

## Reporting a problem

Open an issue without details, or contact the maintainer privately, if you find a vulnerability.
