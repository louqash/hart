"""Main Typer CLI application for hart.

Entry point registered as ``hart`` in ``pyproject.toml``.  Provides command
groups for sync, analysis, reports, and free-form agent queries, all rendered
with rich for beautiful terminal output.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from hart.config import HartSettings, get_config
from hart.interfaces.cli.analyze_cmd import analyze_app
from hart.interfaces.cli.sync_cmd import sync_app
from hart.storage.database import Database
from hart.storage.queries import get_athlete_profile

console = Console()

app = typer.Typer(
    name="hart",
    help="hart — self-hosted endurance training companion (Garmin sync, analytics, Ember).",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

# Register sub-command groups
app.add_typer(sync_app, name="sync", help="Data synchronisation commands.")
app.add_typer(analyze_app, name="analyze", help="Training analysis commands.")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    """Configure root logger with rich handler."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def _get_db(config: HartSettings) -> Database:
    """Open and return the project database."""
    from hart.interfaces.cli.server_client import open_local_db

    return open_local_db(config, console)


def _format_duration(seconds: float | int | None) -> str:
    """Format seconds into H:MM:SS."""
    if seconds is None or seconds <= 0:
        return "--"
    s = int(seconds)
    h, remainder = divmod(s, 3600)
    m, sec = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _format_distance(meters: float | None) -> str:
    """Format meters into km with one decimal."""
    if meters is None or meters <= 0:
        return "--"
    return f"{meters / 1000:.1f} km"


def _color_tsb(tsb: float) -> str:
    """Return a rich-markup colored TSB value."""
    if tsb > 10:
        return f"[green]{tsb:+.1f}[/green]"
    elif tsb < -10:
        return f"[red]{tsb:+.1f}[/red]"
    return f"[yellow]{tsb:+.1f}[/yellow]"


def _color_recovery(score: float) -> str:
    """Return a rich-markup colored recovery score."""
    if score > 70:
        return f"[green]{score:.0f}[/green]"
    elif score >= 40:
        return f"[yellow]{score:.0f}[/yellow]"
    return f"[red]{score:.0f}[/red]"


# ---------------------------------------------------------------------------
# Callback for global options
# ---------------------------------------------------------------------------


@app.callback()
def main(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Enable debug logging.")
    ] = False,
) -> None:
    """hart CLI."""
    _setup_logging(verbose)


# ---------------------------------------------------------------------------
# Direct commands
# ---------------------------------------------------------------------------


@app.command()
def auth() -> None:
    """Authenticate with Garmin Connect via browser (one-time setup).

    Opens a browser window for you to log in to Garmin. Captures the OAuth
    tokens and saves them locally so ``hart sync garmin`` works without
    re-authenticating.

    Run as ``uv run --extra auth hart auth``
    (first time: ``uv run --extra auth playwright install chromium``).
    """
    from urllib.parse import urlencode

    config = get_config()
    token_path = config.garmin_token_path
    token_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401 — availability check
    except ImportError:
        console.print(
            "[red]Error:[/red] Playwright not installed.\n\n"
            "Run with the auth extra:\n"
            "  [cyan]uv run --extra auth hart auth[/cyan]\n"
            "(first time: [cyan]uv run --extra auth playwright install chromium[/cyan])"
        )
        raise typer.Exit(1)

    sso = "https://sso.garmin.com/sso"
    service_url = "https://connect.garmin.com/app"
    signin_url = f"{sso}/signin?" + urlencode({
        "id": "gauth-widget",
        "embedWidget": "true",
        "gauthHost": sso,
        "service": service_url,
        "source": service_url,
        "redirectAfterAccountLoginUrl": service_url,
        "redirectAfterAccountCreationUrl": service_url,
    })

    console.print(
        "\n[bold cyan]Garmin Connect Authentication[/bold cyan]\n"
        "A browser window will open. Log in with your Garmin credentials.\n"
        "If you have MFA enabled, complete that too.\n"
    )

    try:
        from garminconnect import Garmin  # type: ignore[import-untyped]

        # Use Playwright to complete the browser login and capture the ticket
        ticket = _playwright_garmin_login(config, signin_url)

        # Exchange CAS ticket for OAuth tokens via garminconnect's client
        console.print("[dim]Exchanging ticket for session tokens...[/dim]")
        garmin = Garmin(config.garmin.email, config.garmin.password)
        garmin.client._exchange_service_ticket(ticket, service_url=service_url)
        garmin.client.dump(str(token_path))

        console.print(
            f"\n[bold green]Authenticated successfully![/bold green]\n"
            f"Tokens saved to [cyan]{token_path}[/cyan]\n\n"
            "You can now run [cyan]hart sync garmin[/cyan] — it will reuse these tokens."
        )
        if config.server.server_url:
            _upload_garmin_tokens(config, token_path)

    except KeyboardInterrupt:
        console.print("\n[yellow]Authentication cancelled.[/yellow]")
        raise typer.Exit(1)
    except Exception as exc:
        console.print(f"\n[red]Authentication failed:[/red] {exc}")
        raise typer.Exit(1)


def _upload_garmin_tokens(config: HartSettings, token_path: Path) -> None:
    """Send fresh Garmin tokens to hart server; it resumes syncing."""
    from hart.interfaces.cli import server_client

    files = {p.name: p.read_text(encoding="utf-8") for p in sorted(token_path.glob("*.json"))}
    if not files:
        console.print("[yellow]No token files found to upload.[/yellow]")
        return
    try:
        out = server_client.request(config, "POST", "/api/garmin/tokens", {"files": files})
    except RuntimeError as exc:
        console.print(f"[red]Token upload to hart server failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(
        f"[green]Uploaded {len(out['saved'])} token file(s) to hart server[/green]; "
        f"sync job #{out['sync']['job_id']} {out['sync']['status']}."
    )


def _playwright_garmin_login(config: HartSettings, signin_url: str) -> str:
    """Open a browser for Garmin SSO login, return the CAS ticket."""
    import re

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ))
        page = context.new_page()
        page.goto(signin_url)
        page.wait_for_load_state("networkidle")

        # Pre-fill credentials if configured
        if config.garmin.email:
            try:
                page.wait_for_selector('input[name="username"]', timeout=10_000)
                page.fill('input[name="username"]', config.garmin.email)
                pw_field = page.query_selector('input[name="password"]')
                if pw_field and pw_field.is_visible() and config.garmin.password:
                    page.fill('input[name="password"]', config.garmin.password)
                    page.click("#login-btn-signin")
                else:
                    # Two-step: submit email, wait for password field
                    page.click("#login-btn-signin")
                    if config.garmin.password:
                        page.wait_for_selector(
                            'input[name="password"]', state="visible", timeout=10_000
                        )
                        page.fill('input[name="password"]', config.garmin.password)
                        page.click("#login-btn-signin")
            except Exception:
                pass  # Let user complete manually

        console.print("[dim]Waiting for login to complete (complete MFA if prompted)...[/dim]")

        # Capture the ticket from the redirect chain — the URL with the ticket
        # is transient (Garmin redirects away from it immediately)
        captured_ticket: list[str] = []

        def _on_request(request: Any) -> None:
            url = request.url
            m = re.search(r'[?&]ticket=(ST-[^&\s]+)', url)
            if m and not captured_ticket:
                captured_ticket.append(m.group(1))

        page.on("request", _on_request)

        # Wait until we capture a ticket or the page lands on Connect
        for _ in range(240):  # 2 minutes max
            if captured_ticket:
                break
            page.wait_for_timeout(500)

        browser.close()

    if not captured_ticket:
        raise RuntimeError("No CAS ticket captured during login redirect.")
    return captured_ticket[0]


@app.command()
def status() -> None:
    """Show current fitness status at a glance."""
    import datetime

    config = get_config()
    if config.server.server_url:
        _status_via_server(config)
        return
    db = _get_db(config)

    from hart.server import settings

    today = datetime.date.today()
    race = db.fetchone(
        "SELECT name, race_date FROM races WHERE priority = 'A' AND race_date >= ? ORDER BY race_date LIMIT 1",
        [today],
    )
    profile = get_athlete_profile(db)

    # Build header
    headline = (f"{race[0]} in [bold]{(race[1] - today).days}[/bold] days" if race
                else "No A-race set — add one on the Season page")
    console.print()
    console.print(
        Panel(
            f"[bold cyan]{settings.get(db, 'athlete_name')}[/bold cyan] -- {headline}",
            title="hart",
            border_style="cyan",
        )
    )

    # Training load table
    load_data = profile.get("training_load", [])
    if load_data:
        load_table = Table(
            title="Current Training Load",
            show_header=True,
            header_style="bold magenta",
        )
        load_table.add_column("Sport", style="cyan")
        load_table.add_column("CTL (Fitness)", justify="right")
        load_table.add_column("ATL (Fatigue)", justify="right")
        load_table.add_column("TSB (Form)", justify="right")

        for row in load_data:
            sport = str(row.get("sport_type", "unknown")).capitalize()
            ctl = row.get("ctl", 0.0)
            atl = row.get("atl", 0.0)
            tsb = row.get("tsb", 0.0)
            load_table.add_row(
                sport,
                f"{ctl:.1f}",
                f"{atl:.1f}",
                _color_tsb(tsb),
            )
        console.print(load_table)
    else:
        console.print("[dim]No training load data available yet.[/dim]")

    # Recovery score
    recovery_score = profile.get("latest_recovery_score")
    recovery_date = profile.get("latest_recovery_date")
    if recovery_score is not None:
        console.print(
            f"\n  Recovery Score: {_color_recovery(recovery_score)}/100"
            f"  [dim]({recovery_date})[/dim]"
        )

    # VO2max
    vo2_run = profile.get("vo2max_run")
    vo2_cycle = profile.get("vo2max_cycle")
    resting_hr = profile.get("resting_hr")

    metrics_parts: list[str] = []
    if vo2_run is not None:
        metrics_parts.append(f"VO2max Run: [bold]{vo2_run:.1f}[/bold]")
    if vo2_cycle is not None:
        metrics_parts.append(f"VO2max Cycle: [bold]{vo2_cycle:.1f}[/bold]")
    if resting_hr is not None:
        metrics_parts.append(f"RHR: [bold]{resting_hr}[/bold] bpm")

    if metrics_parts:
        console.print(f"\n  {' | '.join(metrics_parts)}")

    console.print()



def _status_via_server(config: HartSettings) -> None:
    """`hart status` backed by hart server's GET /api/today."""
    from hart.interfaces.cli import server_client

    try:
        today = server_client.request(config, "GET", "/api/today")
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    race = today.get("race")
    header = f"[bold cyan]{config.athlete.name}[/bold cyan]"
    if race:
        header += f" -- {race['name']} in [bold]{max(race['days_to_race'], 0)}[/bold] days"
    console.print()
    console.print(Panel(header, title="hart", border_style="cyan"))

    load = today.get("load")
    if load:
        console.print(
            f"  CTL {load['ctl']:.1f} · ATL {load['atl']:.1f} · TSB {_color_tsb(load['tsb'])}"
            f"  [dim]({load['date']})[/dim]"
        )
    else:
        console.print("  [dim]No training load data available yet.[/dim]")
    recovery = today.get("recovery")
    if recovery:
        console.print(
            f"  Recovery Score: {_color_recovery(recovery['recovery_score'])}/100"
            f"  [dim]({recovery['date']})[/dim]"
        )
    last_sync = today.get("last_successful_sync") or "never"
    console.print(f"  [dim]Last successful sync: {last_sync}[/dim]\n")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Interface to bind. Use 0.0.0.0 only inside the container.")] = "127.0.0.1",
    port: Annotated[int | None, typer.Option(help="Port (default HART_PORT or 8765).")] = None,
) -> None:
    """Run hart server: web UI, API, MCP endpoint (/mcp), and the sync scheduler."""
    import uvicorn

    config = get_config()
    if config.server.env == "dev" and host not in ("127.0.0.1", "localhost", "::1"):
        console.print("[red]HART_ENV=dev disables authentication and is only allowed on 127.0.0.1.[/red]")
        raise typer.Exit(2)

    logging.getLogger().setLevel(logging.INFO)
    uvicorn.run(
        "hart.server.app:create_app",
        factory=True,
        host=host,
        port=port or config.server.port,
        workers=1,
        log_level="info",
    )


@app.command("import")
def import_data(
    zip_path: Annotated[
        Path,
        typer.Argument(help="Path to a .zip export file containing .fit files."),
    ],
) -> None:
    """Bulk import .fit files from a zip archive."""
    import zipfile

    from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

    if not zip_path.exists():
        console.print(f"[red]Error:[/red] File not found: {zip_path}")
        raise typer.Exit(1)

    if not zip_path.suffix.lower() == ".zip":
        console.print("[red]Error:[/red] Expected a .zip file.")
        raise typer.Exit(1)

    config = get_config()
    db = _get_db(config)

    from hart.ingestion.fit_parser import FitParser
    from hart.storage.writers import upsert_activity, upsert_laps, upsert_stream_points

    extract_dir = zip_path.parent / zip_path.stem
    console.print(f"\n[cyan]Extracting[/cyan] {zip_path.name}...")

    with zipfile.ZipFile(zip_path, "r") as zf:
        fit_files = [f for f in zf.namelist() if f.lower().endswith(".fit")]
        if not fit_files:
            console.print("[yellow]No .fit files found in archive.[/yellow]")
            raise typer.Exit(0)

        zf.extractall(extract_dir)

    parser = FitParser()
    imported = 0
    skipped = 0
    errors = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console,
    ) as progress:
        task = progress.add_task("Importing .fit files", total=len(fit_files))

        for fit_name in fit_files:
            fit_path = extract_dir / fit_name
            try:
                result = parser.parse(fit_path)
                if result is None:
                    skipped += 1
                else:
                    activity, streams, laps = result
                    upsert_activity(db, activity)
                    if streams:
                        upsert_stream_points(db, activity.activity_id, streams)
                    if laps:
                        upsert_laps(db, activity.activity_id, laps)
                    imported += 1
            except Exception as exc:
                logging.debug("Failed to import %s: %s", fit_name, exc)
                errors += 1
            progress.update(task, advance=1)

    summary_table = Table(title="Import Summary", show_header=False)
    summary_table.add_column("Metric", style="cyan")
    summary_table.add_column("Value", justify="right")
    summary_table.add_row("Total .fit files", str(len(fit_files)))
    summary_table.add_row("Imported", f"[green]{imported}[/green]")
    summary_table.add_row("Skipped", f"[yellow]{skipped}[/yellow]")
    summary_table.add_row("Errors", f"[red]{errors}[/red]" if errors else "0")

    console.print()
    console.print(summary_table)
    console.print()



