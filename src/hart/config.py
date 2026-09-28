"""Settings from environment variables (``HART_*``, see docs/configuration.md).

Values that make sense to change at runtime are also exposed on the Settings
page; those overrides live in the database (``hart.server.settings``).
"""

from __future__ import annotations

import dataclasses
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclasses.dataclass(frozen=True)
class GarminSettings:
    email: str
    password: str
    token_store_path: Path


@dataclasses.dataclass(frozen=True)
class AthleteSettings:
    name: str


DEFAULT_DISCORD_AVATAR = (
    "https://raw.githubusercontent.com/louqash/hart/main/src/hart/server/web/static/discord-avatar.png"
)


@dataclasses.dataclass(frozen=True)
class DiscordSettings:
    bot_token: str
    channel_id: int
    alert_channel_id: int
    webhook_url: str = ""  # simplest setup: a channel webhook (HART_DISCORD_WEBHOOK_URL)
    # Webhook messages only (a bot posts with its own profile). Discord fetches the avatar itself, so it must
    # be a public URL — the default is the icon in the GitHub repository.
    username: str = "hart"
    avatar_url: str = DEFAULT_DISCORD_AVATAR


@dataclasses.dataclass(frozen=True)
class RecoveryWeights:
    hrv: float = 0.30
    sleep: float = 0.25
    body_battery: float = 0.20
    readiness: float = 0.15
    stress: float = 0.05
    fatigue: float = 0.05


@dataclasses.dataclass(frozen=True)
class AnalyticsSettings:
    ctl_time_constant: int = 42
    atl_time_constant: int = 7
    anomaly_z_threshold: float = 2.0
    recovery_weights: RecoveryWeights = dataclasses.field(default_factory=RecoveryWeights)


@dataclasses.dataclass(frozen=True)
class ServerSettings:
    env: str = "production"  # "dev" disables the identity-header check (127.0.0.1 only)
    tz: str = "UTC"
    port: int = 8765
    server_url: str = ""  # used by the CLI to reach a running hart server
    seed_dir: Path = _PROJECT_ROOT / "data" / "seed"
    public_host: str = ""  # e.g. hart.<tailnet>.ts.net — accepted as same-origin
    backup_dir: Path | None = None  # directory for nightly exports (e.g. a NAS mount)
    auth_header: str = "Tailscale-User-Login"  # identity header set by the reverse proxy
    allowed_users: tuple[str, ...] = ()  # empty: anyone the proxy lets through


@dataclasses.dataclass(frozen=True)
class ClaudeSettings:
    model_chat: str = "claude-opus-5-5"
    model_grade: str = "claude-sonnet-5"
    model_suggest: str = "claude-opus-5-5"
    model_parse: str = "claude-haiku-4-5"
    max_concurrency: int = 2
    token_issued_at: str = ""  # ISO date `claude setup-token` was run (1-year validity)
    workspace: Path = _PROJECT_ROOT / "deploy" / "claude-workspace"
    mcp_url: str = ""  # defaults to http://127.0.0.1:<port>/mcp


@dataclasses.dataclass(frozen=True)
class Settings:
    db_path: Path
    garmin_token_path: Path
    garmin: GarminSettings
    athlete: AthleteSettings
    discord: DiscordSettings
    analytics: AnalyticsSettings = dataclasses.field(default_factory=AnalyticsSettings)
    server: ServerSettings = dataclasses.field(default_factory=ServerSettings)
    claude: ClaudeSettings = dataclasses.field(default_factory=ClaudeSettings)


def _env(name: str, default: str = "") -> str:
    """The HART_<name> environment variable, or *default* when unset or empty."""
    value = os.environ.get(f"HART_{name}")
    return default if value in (None, "") else value


def _path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def _load() -> Settings:
    load_dotenv(_PROJECT_ROOT / ".env")

    data_dir = _path(_env("DATA_DIR", "data"), _PROJECT_ROOT)
    # Explicit paths are relative to the project folder; defaults live in the data folder.
    explicit_db = _env("DB_PATH")
    db_path = _path(explicit_db, _PROJECT_ROOT) if explicit_db else data_dir / "hart.duckdb"
    explicit_tokens = os.environ.get("GARMIN_TOKEN_PATH", "")
    token_path = _path(explicit_tokens, _PROJECT_ROOT) if explicit_tokens else data_dir / ".garmin_tokens"
    backup = _env("BACKUP_DIR")

    return Settings(
        db_path=db_path,
        garmin_token_path=token_path,
        garmin=GarminSettings(
            email=os.environ.get("GARMIN_EMAIL", ""),
            password=os.environ.get("GARMIN_PASSWORD", ""),
            token_store_path=token_path,
        ),
        athlete=AthleteSettings(
            name=_env("ATHLETE_NAME") or os.environ.get("ATHLETE_NAME", "") or "Athlete",
        ),
        discord=DiscordSettings(
            bot_token=os.environ.get("DISCORD_BOT_TOKEN", ""),
            channel_id=int(os.environ.get("DISCORD_CHANNEL_ID", "0") or 0),
            alert_channel_id=int(os.environ.get("DISCORD_ALERT_CHANNEL_ID", "0") or 0),
            webhook_url=_env("DISCORD_WEBHOOK_URL"),
            username=_env("DISCORD_USERNAME", "hart"),
            avatar_url=_env("DISCORD_AVATAR_URL", DEFAULT_DISCORD_AVATAR),
        ),
        server=ServerSettings(
            env=_env("ENV", "production"),
            tz=_env("TZ", os.environ.get("TZ", "UTC") if "/" in os.environ.get("TZ", "") else "UTC"),
            port=int(_env("PORT", "8765")),
            server_url=_env("SERVER_URL").rstrip("/"),
            seed_dir=_path(_env("SEED_DIR"), _PROJECT_ROOT) if _env("SEED_DIR") else data_dir / "seed",
            public_host=_env("PUBLIC_HOST").lower(),
            backup_dir=Path(backup) if backup else None,
            auth_header=_env("AUTH_HEADER", "Tailscale-User-Login"),
            allowed_users=tuple(u.strip().lower() for u in _env("ALLOWED_USERS").split(",") if u.strip()),
        ),
        claude=ClaudeSettings(
            model_chat=_env("MODEL_CHAT", "claude-opus-5-5"),
            model_grade=_env("MODEL_GRADE", "claude-sonnet-5"),
            model_suggest=_env("MODEL_SUGGEST", "claude-opus-5-5"),
            model_parse=_env("MODEL_PARSE", "claude-haiku-4-5"),
            max_concurrency=int(_env("CLAUDE_MAX_CONCURRENCY", "2")),
            token_issued_at=_env("CLAUDE_TOKEN_ISSUED_AT") or os.environ.get("CLAUDE_TOKEN_ISSUED_AT", ""),
            workspace=_path(_env("CLAUDE_WORKSPACE"), _PROJECT_ROOT)
            if _env("CLAUDE_WORKSPACE")
            else _PROJECT_ROOT / "deploy" / "claude-workspace",
            mcp_url=_env("CLAUDE_MCP_URL"),
        ),
    )


_lock = threading.Lock()
_settings: Settings | None = None


def get_config(*, reload: bool = False) -> Settings:
    global _settings  # noqa: PLW0603
    if _settings is None or reload:
        with _lock:
            if _settings is None or reload:
                _settings = _load()
    return _settings


# Alias used across the code base as a type hint
HartSettings = Settings
