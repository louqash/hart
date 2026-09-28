"""Entry points for management scripts (init-db, backfill-metrics)."""

from __future__ import annotations


from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

console = Console()


def init_db() -> None:
    """Initialize the DuckDB database with schema.

    Creates empty tables — no data required. Sync from APIs afterwards
    with ``hart sync all``.
    """
    from hart.config import get_config
    from hart.storage.database import Database
    from hart.storage.schema import init_schema

    config = get_config()
    db_path = config.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    console.print(f"Initializing database at [cyan]{db_path}[/cyan]...")
    db = Database(str(db_path))
    db.connect()
    init_schema(db)
    db.close()

    console.print("[bold green]Database initialized.[/bold green]")
    console.print(
        "\nNext steps:\n"
        "  1. Set API credentials in .env (see .env.example)\n"
        "  2. Run [cyan]hart sync all[/cyan] to pull data from Garmin\n"
        "  3. Run [cyan]backfill-metrics[/cyan] to compute analytics\n"
        "  4. Run [cyan]hart status[/cyan] to see your fitness summary"
    )


def backfill_metrics() -> None:
    """Recompute stream-derived metrics (EF, aerobic decoupling) for every activity.

    Only needed when the analytics logic itself changes. Normal syncs populate
    HR zones and TSS automatically from Garmin. FTP-dependent fields (IF, power
    zones) are intentionally not computed — applying a current threshold to older
    activities would be misleading.
    """
    from hart.analytics.anomaly import detect_anomalies
    from hart.analytics.efficiency import (
        efficiency_factor_bike,
        efficiency_factor_run,
    )
    from hart.ingestion.decoupling import activity_decoupling
    from hart.analytics.training_load import update_training_load
    from hart.config import get_config
    from hart.storage import queries
    from hart.storage.database import Database
    from hart.storage.views import refresh_all_views
    from hart.storage.writers import insert_anomaly

    config = get_config()
    db = Database(str(config.db_path))
    db.connect()

    console.print("[bold blue]Backfilling stream-derived metrics...[/bold blue]\n")

    activities = queries.get_activities(db, limit=10000, sort_by="date")
    console.print(f"Found [bold]{len(activities)}[/bold] activities to process.\n")

    if not activities:
        console.print("[yellow]No activities found. Import or sync data first.[/yellow]")
        db.close()
        return

    errors: list[str] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Computing activity metrics...", total=len(activities))

        for activity in activities:
            try:
                activity_id = activity["activity_id"]
                sport_type = activity["sport_type"]

                # EF from the summary, aerobic decoupling from streams — no FTP dependency.
                ef = None
                if sport_type == "bike" and activity.get("normalized_power") and activity.get("avg_hr"):
                    ef = efficiency_factor_bike(activity["normalized_power"], activity["avg_hr"])
                elif sport_type == "run" and activity.get("avg_pace_sec_km") and activity.get("avg_hr"):
                    ef = efficiency_factor_run(activity["avg_pace_sec_km"], activity["avg_hr"])

                decoupling = activity_decoupling(db, activity_id, sport_type)

                act_date = (
                    activity["start_time"].date()
                    if hasattr(activity["start_time"], "date")
                    else activity["start_time"]
                )
                db.execute(
                    """INSERT INTO activity_metrics
                    (activity_id, sport_type, date, efficiency_factor, aerobic_decoupling_pct)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (activity_id) DO UPDATE SET
                        efficiency_factor = EXCLUDED.efficiency_factor,
                        aerobic_decoupling_pct = EXCLUDED.aerobic_decoupling_pct""",
                    [activity_id, sport_type, act_date, ef, decoupling],
                )
            except Exception as e:
                errors.append(f"{activity.get('activity_id', '?')}: {e}")

            progress.update(task, advance=1)

        progress.update(task, description="Computing training load curves...")
        update_training_load(db)

        progress.update(task, description="Refreshing materialized views...")
        refresh_all_views(db)

        progress.update(task, description="Running anomaly detection...")
        try:
            anomalies = detect_anomalies(db)
            for anomaly in anomalies:
                insert_anomaly(db, anomaly)
            console.print(f"\n[bold]Anomalies detected: {len(anomalies)}[/bold]")
        except Exception as e:
            errors.append(f"Anomaly detection: {e}")

    console.print("\n[bold green]Backfill complete![/bold green]")
    console.print(f"  Activities processed: {len(activities)}")
    if errors:
        console.print(f"  [bold red]Errors: {len(errors)}[/bold red]")
        for err in errors[:10]:
            console.print(f"    - {err}")
        if len(errors) > 10:
            console.print(f"    ... and {len(errors) - 10} more")

    db.close()
