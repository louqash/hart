# Configuration

hart is configured in three layers:

1. **Environment variables** — infrastructure and secrets (paths, sign-in, credentials). Put them in `.env`
   next to the code or pass them to the container. They need a restart.
2. **Settings page** — things you'd reasonably change while using hart (your name, models, schedule times,
   notifications, thresholds). Each of these also has an environment variable that sets its *default*; a value
   saved on the Settings page wins over it. The page shows where every value comes from.
3. **Your notes and seed files** — everything about *you*: goals, races, injuries, constraints, baselines. Ember
   and the suggestion rules read them; nothing personal is built into the code.

Variables are named `HART_*`.

## Environment variables

### Required

| Variable | What it is |
|---|---|
| `GARMIN_EMAIL`, `GARMIN_PASSWORD` | Garmin Connect login. After the first `hart auth` the OAuth tokens in `<data>/.garmin_tokens` are used; the password is only needed to refresh them. |
| `HART_TZ` | Your time zone (e.g. `Europe/Berlin`). "Today", schedules and the evening message follow it. Default `UTC`. In Docker also set `TZ` to the same value. |

### Sign-in

hart has no user accounts. It trusts a reverse proxy that authenticates you and passes your identity in a
header — [Tailscale Serve](deployment.md#tailscale-serve) does this out of the box. Requests without the
header are rejected. See [security.md](security.md).

| Variable | Default | What it does |
|---|---|---|
| `HART_AUTH_HEADER` | `Tailscale-User-Login` | Header carrying the signed-in identity (e.g. `X-Forwarded-Email` for oauth2-proxy, `Remote-User` for Authelia). |
| `HART_ALLOWED_USERS` | empty | Comma-separated identities allowed in. Empty = anyone your proxy lets through. |
| `HART_PUBLIC_HOST` | empty | The host name you open hart at (e.g. `hart.example.ts.net`). Needed behind a proxy that rewrites `Host`, and for links in messages. |
| `HART_ENV` | `production` | `dev` skips the identity check — allowed only when listening on 127.0.0.1. |
| `HART_PORT` | `8765` | Port `hart serve` listens on. |

### Claude (Ember)

| Variable | What it is |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Token from `claude setup-token` (runs on your Claude subscription; valid for a year). Without it hart works, but Ember, grading and suggestions are off. |
| `HART_CLAUDE_TOKEN_ISSUED_AT` | ISO date you created the token; hart warns 30 days before it expires. |
| `HART_CLAUDE_MAX_CONCURRENCY` | Parallel Claude runs (default `2`; background work uses at most one, so chat is never starved). |
| `HART_CLAUDE_WORKSPACE` | Working directory for Claude runs (default `deploy/claude-workspace`, which has no project files on purpose). |
| `HART_CLAUDE_MCP_URL` | Where Claude reaches hart's tools (default `http://127.0.0.1:<port>/mcp`). |

### Storage

| Variable | Default | What it is |
|---|---|---|
| `HART_DATA_DIR` | `data` | Holds the database, Garmin tokens and the seed folder. Relative paths here and below are relative to the project folder. |
| `HART_DB_PATH` | `<data>/hart.duckdb` | The DuckDB file. |
| `HART_SEED_DIR` | `<data>/seed` | Seed files, see below. |
| `GARMIN_TOKEN_PATH` | `<data>/.garmin_tokens` | Garmin OAuth tokens. |
| `HART_BACKUP_DIR` | empty | Directory for nightly exports (e.g. a NAS mount). Create an empty `.hart-backups` file in it once, so hart knows the share is really mounted. Keeps 14 daily and 8 weekly exports. |

### Notifications and the CLI

| Variable | What it is |
|---|---|
| `HART_DISCORD_WEBHOOK_URL` | Discord channel webhook for the evening message (Edit channel → Integrations → Webhooks) — only needed without a bot. |
| `HART_DISCORD_USERNAME`, `HART_DISCORD_AVATAR_URL` | Name and avatar the webhook posts as (a bot posts with its own profile) (default `Ember` and Ember's mountain badge from the GitHub repository). The avatar must be a public URL — Discord fetches it, and can't reach a server on your tailnet. |
| `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` | A Discord bot: chat with Ember in that channel (see [deployment](deployment.md#discord-optional)). It also posts the evening message (instead of the webhook); the `send_discord_message` tool of the Claude Code agents uses it too (`DISCORD_ALERT_CHANNEL_ID` for its alert channel). |
| `HART_DISCORD_ALLOWED_USERS` | Discord user IDs Ember answers, comma-separated. Default: the owner of the bot application. |
| `HART_SERVER_URL` | On another machine: send `hart sync`, `hart status` and `hart auth` token uploads to a running server instead of opening the database. |

## Settings

Everything below can be changed on the **Settings** page. The environment variable sets the default.

**Athlete**

| Setting | Environment variable | Default | Notes |
|---|---|---|---|
| Your name | `HART_ATHLETE_NAME` | `Athlete` | How Ember addresses you. Everything else about you lives in your notes. |
| Single-sided power meter | `HART_POWER_SINGLE_SIDED` | `off` | Left-only pedals or cranks double one leg: Ember compares power trends, not absolute watts. |
| I have a coach | `HART_HAS_COACH` | `off` | Paste your coach's sessions on the Plan page, from wherever they arrive (an app, email, messages). On: Ember treats them as the plan and suggests adjustments. Off: the plan is your own and suggestions plan every day freely. |

**Ember (Claude)**

| Setting | Environment variable | Default | Notes |
|---|---|---|---|
| Model for chat (default) | `HART_MODEL_CHAT` | `claude-opus-5-5` | Can be switched per conversation. |
| Model for daily suggestions | `HART_MODEL_SUGGEST` | `claude-opus-5-5` |  |
| Model for session grading | `HART_MODEL_GRADE` | `claude-sonnet-5` | Also converts sessions to Garmin workouts. |
| Model for reading pasted plans and lab reports | `HART_MODEL_PARSE` | `claude-haiku-4-5` |  |

**Schedule**

| Setting | Environment variable | Default | Notes |
|---|---|---|---|
| Morning sleep check from | `HART_MORNING_FROM` | `05:30` | Light syncs every 15 min until last night's sleep arrives. |
| …until | `HART_MORNING_UNTIL` | `10:30` | Also when today's suggestion is made at the latest. |
| Hourly sync from (hour) | `HART_HOURLY_FROM` | `6` |  |
| Hourly sync until (hour) | `HART_HOURLY_UNTIL` | `23` |  |
| Tomorrow's suggestion at | `HART_PRELIMINARY_AT` | `20:00` | From this time on, the dashboard shows tomorrow instead of today. |
| Nightly backup at | `HART_BACKUP_AT` | `03:00` |  |

**Notifications & integrations**

| Setting | Environment variable | Default | Notes |
|---|---|---|---|
| Evening Discord message | `HART_EVENING_MESSAGE` | `on` | Needs the Discord bot or HART_DISCORD_WEBHOOK_URL. |
| Evening message at | `HART_EVENING_MESSAGE_AT` | `22:00` |  |
| Send accepted suggestions to Garmin | `HART_GARMIN_AUTO_SEND` | `on` | Runs and rides are scheduled on your Garmin calendar. |

### Advanced thresholds

Also on the Settings page (collapsed): the numbers behind readiness (e.g. `ready_sleep_red_h` —
less sleep than this is red), the observed training state, the phase template (block lengths, taper weeks)
and health-check reminders (e.g. `health_panel_interval_days`, `health_vitamin_d_month` — set `8` in the
southern hemisphere). There are 27 training and 10 health thresholds; each shows its default and can
be reset. Load-based thresholds are provisional: "load" is Garmin's EPOC-based score, not TSS.

## Seed files

Put these in the seed folder (`data/seed` by default) before the first start; all are optional. Fictional
examples are in [`examples/seed`](../examples/seed).

| File | Contents | When it's read |
|---|---|---|
| `races.json` | `[{"name", "race_date", "distance": "full\|half\|olympic\|sprint\|run\|other", "priority": "A\|B\|C", "notes"?}]` | Once, on first start. The next A-race drives the phase plan. |
| `annotations.json` | `[{"kind": "injury\|illness\|travel\|no_device\|event\|race\|other", "label", "start_date", "end_date"?}]` | Once. |
| `athlete_notes.json` | `[{"category", "title", "body", "valid_from"?, "valid_to"?, "rules"?}]` | On first start (while there are no notes), as *proposed* notes you approve on the Notes page. Edits to the file reach notes that are still unapproved on the next restart. |
| `lab_results.json` | `[{"date", "markers": [{"name", "value", "unit"?, "reference_range"?, "flag"?}]}]` | Every start, idempotently. Lab names in any language are matched to a catalogue (Polish and English built in). |

After the first start, manage everything in the UI: races and events on the Season page, notes on the
Notes page, lab results on the Health page (paste a lab report or add values by hand).

## Note rules

A note can carry machine-checked `rules`. The suggestion guardrails enforce them; notes without rules are
still given to Ember as text.

```json
{
  "forbid_sports": ["run"],
  "max_duration_min": {"run": 30},
  "max_intensity": {"run": "endurance"},
  "allowed_weekdays": {"swim": ["thu"]},
  "max_sessions_per_week": {"swim": 1},
  "min_sessions_per_week": {"strength": 2},
  "preferred_weekdays": {"strength": ["tue", "sat"]},
  "planned_labs": [{"markers": ["tsh"], "due": "2027-03-01", "after": "2027-01-10", "title": "Re-test TSH"}]
}
```

- **Intensities**, lowest to highest: `recovery`, `endurance`/`strength`, `tempo`, `threshold`/`mixed`, `vo2`.
- **`min_sessions_per_week`**: when the rest of the week can't fit the sessions still owed, the day's suggestion
  must include one.
- **`planned_labs`**: creates a health-check reminder until a result for those markers dated after `after` arrives.
  Marker keys are listed on the Health page and by the `get_lab_results` tool.

## Coach's plan

hart doesn't connect to coaching platforms (their APIs are paid or closed). Copy a day or a week from
wherever your coach sends it — an app, an email, a message — and paste it on the Plan page; Ember turns it
into sessions and you confirm them. Turn on **I have a coach** in Settings so Ember treats those sessions as
your coach's plan. Leave it off if you plan your own training.

## Strength exercise names

Garmin names the same lift inconsistently: a set can be recorded by exercise ("Barbell Deadlift") or, when
the watch isn't sure, only by category ("Deadlift"). hart counts lifts by a canonical key:

- **Automatic:** an exercise whose name is its category with `BARBELL_` in front is that category
  (`BARBELL_DEADLIFT` → `DEADLIFT`). Other prefixes (`DUMBBELL_`, `ROMANIAN_`…) stay separate lifts, since they
  change the load. Warm-ups, cardio and runs logged inside a strength session aren't counted as lifts.
- **Your aliases:** anything else — a custom exercise name, a variant you treat as the same lift — you merge on
  the Strength page with **Same lift as**. Aliases can chain (A → B → C); a merge that would loop back is
  refused. **Undo** removes one.

Merging happens when data is read, never on import: the sets in `strength_sets` keep the names Garmin
recorded, and aliases are stored separately (the `exercise_aliases` app setting, included in backups). So an
alias can be undone at any time, and a re-sync never overwrites it. The merged view is used everywhere lifts
are compared: the Strength page, session comparisons, grading, suggestions and Ember's `get_strength_history`
(which also returns the original name as `recorded_as`).
