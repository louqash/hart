# Deployment

hart is one process: web UI, API, background jobs (Garmin syncs, grading, suggestions, backups) and the MCP
endpoint. It owns the DuckDB file — only one process can open it — so run exactly one instance per database.

The recommended setup is a small always-on machine (home server, NAS, mini PC) running the Docker image,
reachable from your devices through **Tailscale Serve**, which also signs you in.

## 1. Prepare

On the server:

```bash
git clone https://github.com/louqash/hart.git && cd hart
cp .env.example .env
cp deploy/docker-compose.example.yml docker-compose.yml
mkdir -p data/seed claude-home
```

Fill in `.env` — at least `GARMIN_EMAIL`, `GARMIN_PASSWORD`, `HART_TZ` and `HART_PUBLIC_HOST` (the name you'll
open hart at). See [configuration.md](configuration.md) for everything else.

Optional: put `races.json`, `athlete_notes.json` etc. in `data/seed` (see [`examples/seed`](../examples/seed))
so your A-race and constraints are there from the first start.

## 2. Claude token (for Ember)

On a computer where you're logged in to Claude Code with your subscription:

```bash
claude setup-token
```

Put the printed token into `.env` as `CLAUDE_CODE_OAUTH_TOKEN`, and today's date as
`HART_CLAUDE_TOKEN_ISSUED_AT`. The token lasts a year; hart shows a warning 30 days before it expires. To
renew, run `claude setup-token` again, replace both values and restart. Treat the token like a password.

Without a token hart still syncs, charts and computes readiness; Ember, grading and suggestions won't work
(their runs fail with an authentication error and background Claude work pauses).

## 3. Start

```bash
docker compose up -d --build
docker compose logs -f hart
curl -s http://127.0.0.1:8765/healthz   # {"ok": true, ...}
```

The container publishes port 8765 on **127.0.0.1 only**. Never expose it directly: hart relies on the proxy for
sign-in.

## 4. Tailscale Serve

On the server (with Tailscale installed and logged in):

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8765
```

hart is now at `https://<machine>.<tailnet>.ts.net` for devices in your tailnet. Tailscale adds the
`Tailscale-User-Login` header to every request from a user's device, which hart requires. Set
`HART_PUBLIC_HOST` to that host name and restart. To limit access to certain people, use your tailnet's access
controls, or set `HART_ALLOWED_USERS=you@example.com`.

If you'd rather give hart its own name (`https://hart.<tailnet>.ts.net`), define a
[Tailscale Service](https://tailscale.com/kb/1552/tailscale-services) `svc:hart` and serve it from this machine.

### Other reverse proxies

Any proxy that authenticates you and passes your identity in a header works (oauth2-proxy, Authelia,
Authentik, Cloudflare Access…). Set `HART_AUTH_HEADER` to the header it sets (e.g. `X-Forwarded-Email` or
`Remote-User`) and make sure the proxy **strips that header from incoming requests** — otherwise anyone could
send it. `HART_ALLOWED_USERS` narrows it further. Also set `HART_PUBLIC_HOST`.

## 5. Garmin login

Garmin sometimes requires a browser login (captcha, MFA). Do it on your laptop:

```bash
uv sync --extra auth && uv run playwright install chromium
HART_SERVER_URL=https://hart.<tailnet>.ts.net uv run --extra auth hart auth
```

`hart auth` opens a browser, saves the Garmin tokens, and — with `HART_SERVER_URL` set — uploads them to the
server, which resumes syncing. If Garmin ever logs you out, the dashboard says so; run the same command again.

Garmin's login is behind Cloudflare, which temporarily blocks an IP address after too many logins in a short
time ("Error 1015 — you are being rate limited"). `hart auth` recognises that page and stops. Wait an hour or
two before trying again, since each attempt extends the block. Your server usually shares your home IP, so its
syncs may pause too; they back off and resume on their own.

## 6. First sync and your phone

Open hart, press **Sync** (or wait for the hourly sync). The first sync pulls the last 14 days; for more
history use `docker compose exec hart /app/.venv/bin/hart sync backfill-…` commands, or import a Garmin export
with `hart import <zip>` (stop the server first — it needs the database).

On your phone, open hart in the browser and **Add to Home Screen** — it installs as an app.

## Backups

Set `HART_BACKUP_DIR` (e.g. a NAS mount, mapped into the container as `/backups`) and create an empty marker
file `.hart-backups` in it once. Every night (Settings → Schedule) hart exports the database as Parquet files
plus the Garmin tokens into `<backup dir>/<date>/`, keeping 14 daily and 8 weekly copies. The System page has
a **Back up now** button and shows the last result.

Restore:

```bash
docker compose stop hart
mv data/hart.duckdb data/hart.duckdb.old
uv run python -c "import duckdb; duckdb.connect('data/hart.duckdb').execute(\"IMPORT DATABASE '/path/to/backups/2027-01-31/db'\")"
cp -r /path/to/backups/2027-01-31/garmin_tokens data/.garmin_tokens
docker compose start hart
```

## Updating

```bash
git pull && docker compose up -d --build
```

The database schema migrates itself on start. Jobs running during a restart are marked as interrupted and
picked up by the scheduler.

## Without Docker

```bash
uv sync
uv run hart serve --host 127.0.0.1 --port 8765
```

Run it as a service (systemd or similar) with the `.env` in the project folder, and put your proxy in front.

## Claude Code on your laptop

When hart runs as a service, your laptop's Claude Code should use its MCP endpoint (the database is locked by
the server):

```bash
claude mcp add --transport http hart https://hart.<tailnet>.ts.net/mcp
```

Tailscale signs these requests in like the browser does.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `403 no_identity` | The request didn't come through your proxy, or the proxy doesn't set `HART_AUTH_HEADER`. |
| `403 cross_origin` | The Origin doesn't match the host — set `HART_PUBLIC_HOST`. |
| "Could not set lock on file" | Another process (a local `hart-mcp`, a second server) has the database open. Stop it. |
| "Garmin login expired" | Run `hart auth` with `HART_SERVER_URL` (step 5). |
| Ember says the usage limit is reached | Your Claude plan's limit; background work pauses until it resets. Settings → Ember lets you pick lighter models. |
| Backups fail with "Backup target not available" | The marker file `.hart-backups` is missing — the share isn't mounted, or you haven't created it. |
