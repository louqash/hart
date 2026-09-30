# Deployment

hart is one process: web UI, API, background jobs (Garmin syncs, grading, suggestions, backups) and the MCP
endpoint. It owns the DuckDB file — only one process can open it — so run exactly one instance per database.

The recommended setup is a small always-on machine (home server, NAS, mini PC) running the Docker image,
reachable from your devices through **Tailscale Serve**, which also signs you in.

**You need:**
- **The server:** a 64-bit Linux machine (x86-64 or ARM64, e.g. a Raspberry Pi 4/5 with 4 GB), or a Mac, with
  Docker and Docker Compose. A couple of GB of RAM and a few GB of disk are plenty (a database with two years
  of training, streams included, is around 150 MB).
- **Tailscale** on the server and your devices, or another reverse proxy that signs you in.
- **Your laptop:** Python 3.12+ and [uv](https://docs.astral.sh/uv/) for the one-time Garmin login (step 5), and
  Claude Code with a Pro or Max subscription for Ember's token (step 2).
- A Garmin Connect account; optionally a Discord server for the evening message and chat.

## 1. Prepare

On the server:

```bash
git clone https://github.com/louqash/hart.git && cd hart
cp .env.example .env
cp deploy/docker-compose.example.yml docker-compose.yml
mkdir -p data/seed claude-home
sudo chown -R 1000:1000 data claude-home   # the container runs as uid 1000 (not needed with Docker Desktop)
```

Fill in `.env` — at least `GARMIN_EMAIL`, `HART_TZ`, `HART_ATHLETE_NAME` and `HART_PUBLIC_HOST` (the name you'll
open hart at; you'll know it after step 4 — fill it in then). See [configuration.md](configuration.md) for
everything else.

`GARMIN_PASSWORD` can stay empty on the server: hart syncs with the tokens from `hart auth` (step 5), and a
password login from a server is what usually triggers Garmin's MFA prompts and Cloudflare blocks.

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
HART_COMMIT=$(git describe --always --dirty) docker compose up -d --build
docker compose logs -f hart
curl -s http://127.0.0.1:8765/healthz   # {"ok": true, ...}
```

The container publishes port 8765 on **127.0.0.1 only**. Never expose it directly: hart relies on the proxy for
sign-in.

Until you've done the Garmin login (step 5) the server has no Garmin tokens: the first automatic sync fails with
"Garmin login expired" and scheduled syncs pause. That's expected — they resume as soon as `hart auth` uploads
the tokens.

## 4. Tailscale Serve

On the server (with Tailscale installed and logged in):

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8765
```

hart is now at `https://<machine>.<tailnet>.ts.net` for devices in your tailnet. Tailscale adds the
`Tailscale-User-Login` header to every request from a user's device, which hart requires. Set
`HART_PUBLIC_HOST` to that host name and restart. Also set `HART_ALLOWED_USERS` to your Tailscale login (as the
admin console's Users page shows it — `you@example.com`, or `you@github` for a GitHub sign-in), so sharing the
node or inviting someone to the tailnet doesn't let them in.

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

## 6. Your history

The **Sync** button fetches the last 7 days and scheduled syncs the last 2, so a new install knows almost
nothing about you. Training load (fitness/fatigue) needs about six weeks of sessions to settle, and readiness
needs a few weeks of sleep and HRV for its baselines. Load your history once:

```bash
HART_SERVER_URL=https://hart.<tailnet>.ts.net uv run hart sync all --days 90   # on your laptop
```

It fetches activities and daily health (sleep, HRV, resting HR, Body Battery) for every day in the range and
recomputes the analytics; only sessions from the last week are graded. Each day is several Garmin requests, so
a long range takes a while, and Garmin rate-limits long runs — go back further in steps (e.g. `--days 180`
another day) rather than all at once. The maximum is 365.

Optional extras: `hart sync backfill-vo2max` (VO2max history) and `hart sync backfill-intervals --days 60`
(lap kinds and structured-workout targets for the interval breakdown; one Garmin download per session) run
through the server like the sync above.
`backfill-strength` (sets for older strength sessions) and `backfill-metrics` (Garmin metrics missing on older
activities) open the database themselves, so run them with the server stopped, like the import below:
`docker compose run --rm hart /app/.venv/bin/hart sync backfill-strength`.

**Years of activities:** request your data from Garmin (Garmin account → Account Management → *Export Your
Data*; the e-mail with the zip can take a day). Then, with the server stopped:

```bash
cp ~/Downloads/<export>.zip data/garmin-export.zip
docker compose stop hart
docker compose run --rm hart /app/.venv/bin/hart import /app/data/garmin-export.zip
docker compose start hart
```

The export has activities (with streams and laps) but no daily health history — use `--days` for that.

## 7. Your phone

On your phone, open hart in the browser and **Add to Home Screen** — it installs as an app.

## 8. Tell it about yourself

Ember and the suggestions only know what you tell hart. On the **Season** page add your races (the next A-race
drives the season phases); on the **Notes** page add goals, injuries, constraints and baselines ("swim only on
Thursdays", "previous half: 5:30"); on **Settings** set your name, power meter and whether you have a coach.
The files in [`examples/seed`](../examples/seed) show the same things as seed files you can drop into
`data/seed` before the first start.

## Discord (optional)

A **bot** does everything: it posts the evening message and a message for each graded session (letter,
scores, summary; Settings → Notifications) — re-grades of a session go into a thread under its first grade —
and lets you chat with Ember in the channel. If you only
want the evening message, a channel **webhook** is enough — Edit channel → Integrations → Webhooks → Copy URL, set
`HART_DISCORD_WEBHOOK_URL` — and it posts as Ember with Ember's avatar. With both set, the bot is used.

Setting up the bot:

1. In the [Discord developer portal](https://discord.com/developers/applications) create an application, name
   it Ember and give it an avatar (`src/hart/server/web/static/discord-avatar.png`).
2. **Bot** tab: reset and copy the token, and turn on **Message Content Intent**.
3. **OAuth2 → URL Generator**: scope `bot`; permissions *View Channels*, *Send Messages*, *Create Public
   Threads*, *Send Messages in Threads*, *Read Message History*. Open the URL and add the bot to your server.
4. Set `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` (Discord settings → Advanced → Developer Mode, then
   right-click the channel → Copy Channel ID) and restart. The evening message now comes from the bot, so you
   can reply to it; `HART_DISCORD_WEBHOOK_URL` can be removed.

Tag **@Ember** in the channel, or reply to the evening message (with the reply's mention left on): Ember opens
a thread and answers there; keep writing in the thread to continue — no tag needed there. Untagged messages in
the channel are left alone.

You can also **message Ember directly**: click the bot in your server's member list → *Message*. In a DM no tag
is needed; it continues one conversation until it has been quiet for six hours, or until you write `new`. (If
Discord refuses, allow direct messages from members of that server: server name → Privacy Settings.) The conversations also appear on the Ember page. Paste your coach's training there
("today's session from my coach: …") and Ember runs it through the Plan page's plan reader; the sessions wait
on the Plan page until you apply them, exactly like a paste in the paste box. Only you are answered — the owner of
the bot application, or the user IDs in `HART_DISCORD_ALLOWED_USERS` — so other members of the server can't
read your data through it. The System page shows whether the bot is connected.

## Backups

Set `HART_BACKUP_DIR` (e.g. a NAS mount, mapped into the container as `/backups`) and create an empty marker
file `.hart-backups` in it once. Every night (Settings → Schedule) hart exports the database as Parquet files
plus the Garmin tokens into `<backup dir>/<date>/`, keeping 14 daily and 8 weekly copies. The System page has
a **Back up now** button and shows the last result.

Restore:

```bash
docker compose stop hart
mv data/hart.duckdb data/hart.duckdb.old
cp -r /path/to/backups/2027-01-31 data/restore
docker compose run --rm hart /app/.venv/bin/python -c \
  "import duckdb; duckdb.connect('/app/data/hart.duckdb').execute(\"IMPORT DATABASE '/app/data/restore/db'\")"
cp -r data/restore/garmin_tokens data/.garmin_tokens && rm -rf data/restore
docker compose start hart
```

(Without Docker: the same `IMPORT DATABASE` with `uv run python -c …` in the project folder.)

## Updating

```bash
git pull && HART_COMMIT=$(git describe --always --dirty) HART_BUILT_AT=$(date -Iseconds) docker compose up -d --build
```

The System page shows the running version and commit (also in `/healthz`), so you can check a deploy took.
Without `HART_COMMIT` it says "commit unknown".

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
| `403 no_identity` | The request didn't come through your proxy, or the proxy doesn't set `HART_AUTH_HEADER`. With Tailscale: the device is a *tagged* node (servers, shared nodes) — Tailscale only sends identity headers for devices owned by a user. |
| `PermissionError` / "unable to open database file" at start | The container (uid 1000) can't write `data/` or `claude-home/` — `sudo chown -R 1000:1000 data claude-home`. |
| `403 cross_origin` | The Origin doesn't match the host — set `HART_PUBLIC_HOST`. |
| "Could not set lock on file" | Another process (a local `hart-mcp`, a second server) has the database open. Stop it. |
| "Garmin login expired" | Run `hart auth` with `HART_SERVER_URL` (step 5). |
| Ember says the usage limit is reached | Your Claude plan's limit; background work pauses until it resets. Settings → Ember lets you pick lighter models. |
| Backups fail with "Backup target not available" | The marker file `.hart-backups` is missing — the share isn't mounted, or you haven't created it. |
