"""Sync sub-commands for data ingestion from Garmin Connect.

``hart sync garmin`` / ``hart sync all`` run the canonical pipeline
(:mod:`hart.server.jobs.pipeline`) — through hart server when
``HART_SERVER_URL`` is set.  The other commands are thin wrappers around
:class:`~hart.ingestion.sync_manager.SyncManager`.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from hart.config import HartSettings, get_config
from hart.storage.database import Database

logger = logging.getLogger(__name__)
console = Console()

sync_app = typer.Typer(
    name="sync",
    help="Data synchronisation commands.",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_db(config: HartSettings) -> Database:
    from hart.interfaces.cli.server_client import open_local_db

    return open_local_db(config, console)


def _sync_via_server(config: HartSettings) -> None:
    """Enqueue a manual sync on hart server and wait for it."""
    from hart.interfaces.cli import server_client

    try:
        job = server_client.request(config, "POST", "/api/sync", {"full": True})
        job_id = job["job_id"]
        if job["status"].startswith("already_"):
            console.print(f"[yellow]{job['message']}[/yellow] — waiting for it instead.")
        with console.status(f"Syncing on hart server (job #{job_id})..."):
            final = server_client.wait_for_job(config, job_id)
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    _print_pipeline_result(final.get("result") or {}, final.get("status", "?"), final.get("error"))


def _print_pipeline_result(result: dict[str, Any], status: str, error: str | None) -> None:
    table = Table(title="Sync Summary", show_header=False)
    table.add_column("Metric", style="cyan", width=25)
    table.add_column("Value", justify="right")
    steps = result.get("steps", {})
    health = (steps.get("garmin_health") or {}).get("out") or {}
    acts = (steps.get("garmin_activities") or {}).get("out") or {}
    table.add_row("Status", f"[green]{status}[/green]" if status == "ok" else f"[red]{status}[/red]")
    if isinstance(health, dict) and "days" in health:
        table.add_row("Health days synced", str(health["days"]))
    if isinstance(acts, dict) and "new" in acts:
        table.add_row("New activities", str(acts["new"]))
    if result.get("partial"):
        table.add_row("Partial", "[yellow]yes[/yellow]")
    for err in (result.get("errors") or [])[:5]:
        table.add_row("Error", f"[red]{err}[/red]")
    if error:
        table.add_row("Job error", f"[red]{error}[/red]")
    console.print()
    console.print(table)
    console.print()


def _run_sync(days: int) -> None:
    """Run the canonical pipeline: through hart server if configured, else locally."""
    config = get_config()
    if config.server.server_url:
        _sync_via_server(config)
        return

    if not config.garmin.email or not config.garmin.password:
        console.print(
            "[red]Error:[/red] Garmin credentials not configured. "
            "Set GARMIN_EMAIL and GARMIN_PASSWORD environment variables."
        )
        raise typer.Exit(1)

    from hart.server.jobs.pipeline import run_sync_pipeline

    db = _get_db(config)
    with console.status("Syncing Garmin and updating analytics..."):
        result = run_sync_pipeline(db, config, health_days=days, activity_days=days)
    status = "error" if result["error_code"] in ("garmin_auth", "garmin_rate_limited") else "ok"
    _print_pipeline_result(result, status, result["error_code"])


def _print_sync_summary(
    source: str,
    activities_synced: int = 0,
    new_health_days: int = 0,
    errors: int = 0,
    elapsed_sec: float = 0.0,
) -> None:
    table = Table(title=f"Sync Summary: {source}", show_header=False)
    table.add_column("Metric", style="cyan", width=25)
    table.add_column("Value", justify="right")

    if activities_synced > 0:
        table.add_row("Activities synced", f"[green]{activities_synced}[/green]")
    if new_health_days > 0:
        table.add_row("Health days synced", f"[green]{new_health_days}[/green]")
    if activities_synced == 0 and new_health_days == 0:
        table.add_row("New data", "[dim]Already up to date[/dim]")
    if errors > 0:
        table.add_row("Errors", f"[red]{errors}[/red]")
    table.add_row("Duration", f"{elapsed_sec:.1f}s")

    console.print()
    console.print(table)
    console.print()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@sync_app.command("garmin")
def sync_garmin() -> None:
    """Sync Garmin Connect and update all analytics (training load, recovery, anomalies).

    Uses hart server when HART_SERVER_URL is set.
    """
    _run_sync(days=14)


@sync_app.command("fit")
def sync_fit(
    path: Annotated[Path, typer.Argument(help="Path to a .fit file.")],
) -> None:
    """Import a single .fit file."""
    if not path.exists():
        console.print(f"[red]Error:[/red] File not found: {path}")
        raise typer.Exit(1)

    if path.suffix.lower() != ".fit":
        console.print("[red]Error:[/red] Expected a .fit file.")
        raise typer.Exit(1)

    config = get_config()
    db = _get_db(config)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"Parsing {path.name}...", total=None)

        from hart.ingestion.fit_parser import FitParser
        from hart.storage.writers import (
            replace_strength_sets,
            upsert_activity,
            upsert_hrv_samples,
            upsert_laps,
            upsert_stream_points,
        )

        result = FitParser().parse_file(path)

        progress.update(task, description="Writing to database...")
        upsert_activity(db, result.activity)
        upsert_stream_points(db, result.activity.activity_id, result.stream_points)
        upsert_laps(db, result.activity.activity_id, result.laps)
        upsert_hrv_samples(db, result.activity.activity_id, result.hrv_rr_intervals)
        replace_strength_sets(db, result.activity.activity_id, result.strength_sets)

    activity = result.activity
    table = Table(title="Imported Activity", show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("Name", activity.name or "--")
    table.add_row("Sport", activity.sport_type.value.capitalize())
    table.add_row("Date", str(activity.start_time.date()))
    table.add_row("Duration", f"{activity.elapsed_seconds // 60} min")
    if activity.distance_meters:
        table.add_row("Distance", f"{activity.distance_meters / 1000:.2f} km")
    if activity.avg_hr:
        table.add_row("Avg HR", f"{activity.avg_hr} bpm")
    if activity.avg_power:
        table.add_row("Avg Power", f"{activity.avg_power} W")
    table.add_row("Streams", f"{len(result.stream_points)} data points")
    table.add_row("Laps", str(len(result.laps)))
    if result.strength_sets:
        active = sum(1 for s in result.strength_sets if s.set_type == "active")
        table.add_row("Sets", f"{active} active")
    console.print()
    console.print(table)
    console.print()


@sync_app.command("backfill-metrics")
def backfill_metrics() -> None:
    """Fetch metrics from Garmin API for activities that are missing them.

    Covers activities synced before metrics were part of the sync pipeline.
    New syncs populate metrics automatically — only run this once to backfill
    historical data.
    """
    config = get_config()
    db = _get_db(config)

    rows = db.fetchall(
        "SELECT external_id, activity_id, sport_type, start_time "
        "FROM activities "
        "WHERE source = 'garmin' AND external_id IS NOT NULL "
        "ORDER BY start_time"
    )
    if not rows:
        console.print("[yellow]No Garmin activities found.[/yellow]")
        return

    existing = {r[0] for r in db.fetchall("SELECT activity_id FROM activity_metrics")}
    to_process = [r for r in rows if r[1] not in existing]
    if not to_process:
        console.print("[green]All activities already have metrics.[/green]")
        return

    console.print(f"Found [cyan]{len(to_process)}[/cyan] activities to backfill.")

    from hart.ingestion.decoupling import activity_decoupling
    from hart.ingestion.sync_manager import SyncManager

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Authenticating...", total=len(to_process))

        manager = SyncManager(db, config)
        garmin = manager.get_garmin_client()

        metrics_inserted = 0
        activities_updated = 0
        errors = 0

        for i, (ext_id, act_id, sport_type, start_time) in enumerate(to_process, 1):
            progress.update(task, completed=i, description=f"Fetching {i}/{len(to_process)}...")
            try:
                detail = garmin.get_activity(str(ext_id))
                if not detail:
                    continue
                summary = detail.get("summaryDTO", {})

                # Update activities table with summaryDTO fields
                moving = summary.get("movingDuration")
                db.execute(
                    "UPDATE activities SET "
                    "rpe = COALESCE(rpe, ?), feel = COALESCE(feel, ?), "
                    "garmin_training_load = COALESCE(garmin_training_load, ?), "
                    "training_effect_label = COALESCE(training_effect_label, ?), "
                    "body_battery_delta = COALESCE(body_battery_delta, ?), "
                    "begin_stamina = COALESCE(begin_stamina, ?), "
                    "end_stamina = COALESCE(end_stamina, ?), "
                    "moving_seconds = COALESCE(?, moving_seconds), "
                    "normalized_power = COALESCE(?, normalized_power) "
                    "WHERE activity_id = ?",
                    [
                        summary.get("directWorkoutRpe"),
                        summary.get("directWorkoutFeel"),
                        summary.get("activityTrainingLoad"),
                        summary.get("trainingEffectLabel"),
                        summary.get("differenceBodyBattery"),
                        summary.get("beginPotentialStamina"),
                        summary.get("endPotentialStamina"),
                        int(moving) if moving is not None else None,
                        summary.get("normalizedPower"),
                        act_id,
                    ],
                )
                activities_updated += 1

                # HR zones
                hr_zone_seconds = None
                try:
                    hr_zones = garmin.get_activity_hr_in_timezones(str(ext_id))
                    if hr_zones:
                        hr_zone_seconds = {
                            f"Z{z['zoneNumber']}": round(z["secsInZone"], 1)
                            for z in hr_zones
                            if z.get("secsInZone") is not None
                        }
                except Exception:
                    pass

                # EF — NP/avg_HR (bike) or speed/avg_HR (run), no FTP needed.
                np_val = summary.get("normalizedPower")
                avg_hr = summary.get("averageHR")
                avg_speed = summary.get("averageMovingSpeed")

                ef: float | None = None
                if sport_type == "bike" and np_val and avg_hr and avg_hr > 0:
                    ef = round(np_val / avg_hr, 4)
                elif sport_type == "run" and avg_speed and avg_hr and avg_hr > 0:
                    ef = round(avg_speed / (avg_hr / 60.0), 4)

                act_date = start_time.date() if hasattr(start_time, "date") else str(start_time)[:10]

                # Running dynamics (Garmin strideLength is in cm → convert to m)
                sl = summary.get("strideLength")
                db.execute(
                    "INSERT OR REPLACE INTO activity_metrics "
                    "(activity_id, sport_type, date, tss, tss_method, "
                    "hr_zone_seconds, efficiency_factor, aerobic_decoupling_pct, "
                    "avg_ground_contact_ms, avg_vertical_osc_mm, avg_vertical_ratio_pct, "
                    "avg_stride_length_m, swolf, estimated_calories, estimated_kj, "
                    "carb_calories, fat_calories) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        act_id, sport_type, act_date,
                        summary.get("activityTrainingLoad"), "garmin",
                        json.dumps(hr_zone_seconds) if hr_zone_seconds else None,
                        ef, activity_decoupling(db, act_id, sport_type),
                        summary.get("groundContactTime"),
                        summary.get("verticalOscillation"),
                        summary.get("verticalRatio"),
                        round(sl / 100.0, 3) if sl is not None else None,
                        None,        # swolf
                        summary.get("calories"),
                        round(summary.get("totalWork", 0), 1) if summary.get("totalWork") else None,
                        None, None,  # carb/fat calories
                    ],
                )
                metrics_inserted += 1

            except Exception as exc:
                logger.debug("Failed to backfill metrics for %s: %s", ext_id, exc)
                errors += 1

    table = Table(title="Metrics Backfill Summary", show_header=False)
    table.add_column("Metric", style="cyan", width=25)
    table.add_column("Value", justify="right")
    table.add_row("Metrics created", f"[green]{metrics_inserted}[/green]")
    table.add_row("Activities updated", f"[green]{activities_updated}[/green]")
    if errors:
        table.add_row("Errors", f"[red]{errors}[/red]")
    console.print()
    console.print(table)
    console.print()


@sync_app.command("backfill-gear")
def backfill_gear() -> None:
    """Fetch gear assignments from Garmin for all activities missing gear data.

    Calls the Garmin API once per activity (~0.3 s apart). With ~177 activities
    this takes roughly 60 seconds. Safe to re-run — skips activities that
    already have a gear_id set.
    """
    config = get_config()
    db = _get_db(config)

    total = db.fetchall(
        "SELECT COUNT(*) FROM activities WHERE gear_id IS NULL AND external_id IS NOT NULL"
    )[0][0]

    if total == 0:
        console.print("[green]All activities already have gear data.[/green]")
        return

    console.print(f"Found [cyan]{total}[/cyan] activities without gear — fetching from Garmin...")

    from hart.ingestion.sync_manager import SyncManager
    import datetime

    start_time = datetime.datetime.now()
    manager = SyncManager(db, config)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Fetching gear...", total=total)

        import time
        garmin = manager.get_garmin_client()

        rows = db.fetchall(
            "SELECT activity_id, external_id FROM activities "
            "WHERE gear_id IS NULL AND external_id IS NOT NULL "
            "ORDER BY start_time DESC"
        )

        updated = errors = 0
        for i, (activity_id, external_id) in enumerate(rows, 1):
            progress.update(task, completed=i, description=f"Fetching {i}/{total}...")
            try:
                from hart.ingestion.sync_manager import _fetch_gear_id
                gear_uuid = _fetch_gear_id(garmin, str(external_id))
                db.execute(
                    "UPDATE activities SET gear_id = ? WHERE activity_id = ?",
                    [gear_uuid, activity_id],
                )
                updated += 1
                time.sleep(0.3)
            except Exception as exc:
                logger.debug("Gear fetch failed for %s: %s", external_id, exc)
                errors += 1

    elapsed = (datetime.datetime.now() - start_time).total_seconds()
    table = Table(title="Gear Backfill Summary", show_header=False)
    table.add_column("Metric", style="cyan", width=25)
    table.add_column("Value", justify="right")
    table.add_row("Activities updated", f"[green]{updated}[/green]")
    if errors:
        table.add_row("Errors", f"[red]{errors}[/red]")
    table.add_row("Duration", f"{elapsed:.1f}s")
    console.print()
    console.print(table)
    console.print()


@sync_app.command("backfill-strength")
def backfill_strength(
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Re-fetch all strength sessions, not just ones missing sets."),
    ] = False,
) -> None:
    """Fetch exercise sets (reps, weight, exercise) for strength sessions from Garmin.

    New syncs store sets automatically — run this once for older sessions, or
    with --refresh after editing workouts in Garmin Connect.
    """
    config = get_config()
    db = _get_db(config)

    from hart.ingestion.sync_manager import SyncManager

    start_time = datetime.datetime.now()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        progress.add_task("Fetching strength sets from Garmin...", total=None)
        result = SyncManager(db, config).backfill_strength_sets(refresh=refresh)

    elapsed = (datetime.datetime.now() - start_time).total_seconds()
    table = Table(title="Strength Backfill Summary", show_header=False)
    table.add_column("Metric", style="cyan", width=25)
    table.add_column("Value", justify="right")
    table.add_row("Sessions updated", f"[green]{result.updated_activities}[/green]")
    if result.errors:
        table.add_row("Errors", f"[red]{result.errors}[/red]")
    table.add_row("Duration", f"{elapsed:.1f}s")
    console.print()
    console.print(table)
    console.print()


@sync_app.command("backfill-vo2max")
def backfill_vo2max(
    since: Annotated[
        str | None,
        typer.Option("--since", help="First date (YYYY-MM-DD). Default: first day with health data."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", help="Last date (YYYY-MM-DD). Default: today."),
    ] = None,
) -> None:
    """Fetch past VO2max estimates (run + bike) from Garmin into daily health.

    New syncs store VO2max automatically — run this once for older days.
    Uses hart server when HART_SERVER_URL is set; otherwise the server must be stopped.
    """
    config = get_config()
    try:
        start = datetime.date.fromisoformat(since) if since else None
        end = datetime.date.fromisoformat(until) if until else None
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    if config.server.server_url:
        from hart.interfaces.cli import server_client

        body = {k: v.isoformat() for k, v in (("start", start), ("end", end)) if v}
        try:
            job = server_client.request(config, "POST", "/api/backfill/vo2max", body)
            if job["status"].startswith("already_"):
                console.print(f"[yellow]{job['message']}[/yellow] — waiting for it instead.")
            with console.status(f"Backfilling VO2max on hart server (job #{job['job_id']})..."):
                final = server_client.wait_for_job(config, job["job_id"])
        except RuntimeError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1) from exc
        if final.get("status") != "ok":
            console.print(f"[red]Job {final.get('status')}:[/red] {final.get('error')}")
            raise typer.Exit(1)
        result = final.get("result") or {}
    else:
        if not config.garmin.email:
            console.print("[red]Error:[/red] Garmin credentials not configured (GARMIN_EMAIL).")
            raise typer.Exit(1)
        from hart.ingestion.sync_manager import SyncManager

        db = _get_db(config)
        with console.status("Fetching VO2max history from Garmin..."):
            result = SyncManager(db, config).backfill_vo2max(start=start, end=end)

    table = Table(title="VO2max Backfill Summary", show_header=False)
    table.add_column("Metric", style="cyan", width=25)
    table.add_column("Value", justify="right")
    table.add_row("Range", f"{result.get('from')} → {result.get('to')}")
    table.add_row("Garmin estimates", str(result.get("estimates", 0)))
    table.add_row("Days updated", f"[green]{result.get('updated', 0)}[/green]")
    if result.get("no_health_row"):
        table.add_row("Skipped (no health row)", f"[yellow]{result['no_health_row']}[/yellow]")
    console.print()
    console.print(table)
    console.print()


@sync_app.command("backfill-decoupling")
def backfill_decoupling_cmd(
    only_missing: Annotated[
        bool, typer.Option("--only-missing", help="Skip sessions that already have a value."),
    ] = False,
) -> None:
    """Compute aerobic decoupling from stored streams for bike and run sessions.

    Only steady sessions with 45+ moving minutes get a value. New syncs compute
    it automatically — run this once for older activities or after changing the
    gates. Uses hart server when HART_SERVER_URL is set; otherwise the server must
    be stopped.
    """
    config = get_config()
    if config.server.server_url:
        from hart.interfaces.cli import server_client

        try:
            job = server_client.request(
                config, "POST", "/api/backfill/decoupling", {"only_missing": only_missing}
            )
            if job["status"].startswith("already_"):
                console.print(f"[yellow]{job['message']}[/yellow] — waiting for it instead.")
            with console.status(f"Backfilling decoupling on hart server (job #{job['job_id']})..."):
                final = server_client.wait_for_job(config, job["job_id"])
        except RuntimeError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1) from exc
        if final.get("status") != "ok":
            console.print(f"[red]Job {final.get('status')}:[/red] {final.get('error')}")
            raise typer.Exit(1)
        result = final.get("result") or {}
    else:
        from hart.ingestion.decoupling import backfill_decoupling
        from hart.storage.views import refresh_all_views

        db = _get_db(config)
        with console.status("Computing aerobic decoupling from streams..."):
            result = backfill_decoupling(db, only_missing=only_missing)
            refresh_all_views(db)

    table = Table(title="Decoupling Backfill Summary", show_header=False)
    table.add_column("Metric", style="cyan", width=25)
    table.add_column("Value", justify="right")
    table.add_row("Bike/run sessions checked", str(result.get("checked", 0)))
    table.add_row("Decoupling computed", f"[green]{result.get('computed', 0)}[/green]")
    table.add_row("Not eligible", str(result.get("not_eligible", 0)))
    if result.get("errors"):
        table.add_row("Errors", f"[red]{result['errors']}[/red]")
    console.print()
    console.print(table)
    console.print()


@sync_app.command("all")
def sync_all() -> None:
    """Sync Garmin data and update the analytics pipeline (same as `hart sync garmin`)."""
    console.print(Panel("[bold cyan]Running full data sync[/bold cyan]", border_style="cyan"))
    _run_sync(days=14)
