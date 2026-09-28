"""Analysis sub-commands for training data inspection.

Provides CLI commands for examining training load (CTL/ATL/TSB), recovery
scores, zone distributions, and performance efficiency trends.  All output
uses rich tables, panels, and colour coding for at-a-glance assessment.
"""

from __future__ import annotations

import datetime
import logging
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from hart.config import get_config
from hart.storage.database import get_database
from hart.storage.queries import (
    get_activity_metrics,
    get_recovery_scores,
    get_training_load,
)

logger = logging.getLogger(__name__)
console = Console()

analyze_app = typer.Typer(
    name="analyze",
    help="Training analysis commands.",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SPARKLINE_CHARS = " ▁▂▃▄▅▆▇█"


def _sparkline(values: list[float], width: int = 30) -> str:
    """Generate a Unicode sparkline from a list of numeric values."""
    if not values:
        return ""
    mn = min(values)
    mx = max(values)
    rng = mx - mn if mx != mn else 1.0

    # Sample values if more than width
    if len(values) > width:
        step = len(values) / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = values

    chars = []
    for v in sampled:
        idx = int((v - mn) / rng * (len(_SPARKLINE_CHARS) - 1))
        idx = max(0, min(idx, len(_SPARKLINE_CHARS) - 1))
        chars.append(_SPARKLINE_CHARS[idx])
    return "".join(chars)


def _color_tsb(tsb: float) -> str:
    if tsb > 10:
        return f"[green]{tsb:+.1f}[/green]"
    elif tsb < -10:
        return f"[red]{tsb:+.1f}[/red]"
    return f"[yellow]{tsb:+.1f}[/yellow]"


def _color_recovery(score: float) -> str:
    if score > 70:
        return f"[green]{score:.0f}[/green]"
    elif score >= 40:
        return f"[yellow]{score:.0f}[/yellow]"
    return f"[red]{score:.0f}[/red]"


def _color_decoupling(pct: float) -> str:
    """Color code decoupling: <5% green, 5-10% yellow, >10% red."""
    if pct < 5.0:
        return f"[green]{pct:.1f}%[/green]"
    elif pct < 10.0:
        return f"[yellow]{pct:.1f}%[/yellow]"
    return f"[red]{pct:.1f}%[/red]"


def _color_monotony(monotony: float) -> str:
    """Color code monotony: <1.5 green, 1.5-2.0 yellow, >2.0 red."""
    if monotony < 1.5:
        return f"[green]{monotony:.2f}[/green]"
    elif monotony < 2.0:
        return f"[yellow]{monotony:.2f}[/yellow]"
    return f"[red]{monotony:.2f}[/red]"


def _format_duration(seconds: int | float) -> str:
    s = int(seconds)
    h, remainder = divmod(s, 3600)
    m, sec = divmod(remainder, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m {sec:02d}s"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@analyze_app.command("load")
def analyze_load(
    days: Annotated[int, typer.Option("--days", "-d", help="Number of days to analyze.")] = 30,
    sport: Annotated[str, typer.Option("--sport", "-s", help="Sport type filter (swim/bike/run/all).")] = "all",
) -> None:
    """Show CTL/ATL/TSB training load with sparkline visualization."""
    config = get_config()
    db = get_database(config.db_path)

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=days)

    sport_type = "combined" if sport == "all" else sport

    data = get_training_load(db, start_date, end_date, sport_type)

    if not data:
        console.print("[yellow]No training load data available for the selected period.[/yellow]")
        raise typer.Exit(0)

    # Current values (last row)
    current = data[-1]
    current_ctl = current.get("ctl", 0.0)
    current_atl = current.get("atl", 0.0)
    current_tsb = current.get("tsb", 0.0)

    # Header panel
    sport_label = sport_type.capitalize()
    console.print()
    console.print(
        Panel(
            f"[bold]{sport_label} Training Load[/bold] -- last {days} days\n\n"
            f"  CTL (Fitness): [bold cyan]{current_ctl:.1f}[/bold cyan]\n"
            f"  ATL (Fatigue): [bold magenta]{current_atl:.1f}[/bold magenta]\n"
            f"  TSB (Form):    {_color_tsb(current_tsb)}",
            border_style="cyan",
        )
    )

    # Sparklines
    ctl_values = [float(d.get("ctl", 0)) for d in data]
    atl_values = [float(d.get("atl", 0)) for d in data]
    tsb_values = [float(d.get("tsb", 0)) for d in data]

    console.print(f"\n  CTL: [cyan]{_sparkline(ctl_values)}[/cyan]")
    console.print(f"  ATL: [magenta]{_sparkline(atl_values)}[/magenta]")
    console.print(
        f"  TSB: [{'green' if current_tsb > 0 else 'red'}]{_sparkline(tsb_values)}[/{'green' if current_tsb > 0 else 'red'}]"
    )

    # Daily detail table (last 14 days or less)
    display_data = data[-min(14, len(data)) :]

    table = Table(title=f"\nDaily Training Load ({sport_label})", show_header=True, header_style="bold")
    table.add_column("Date", style="dim")
    table.add_column("TSS", justify="right")
    table.add_column("CTL", justify="right", style="cyan")
    table.add_column("ATL", justify="right", style="magenta")
    table.add_column("TSB", justify="right")
    table.add_column("Monotony", justify="right")
    table.add_column("Strain", justify="right")

    for row in display_data:
        date_str = str(row.get("date", ""))
        tss = row.get("daily_tss", 0.0)
        ctl = row.get("ctl", 0.0)
        atl = row.get("atl", 0.0)
        tsb = row.get("tsb", 0.0)
        monotony = row.get("monotony", 0.0) or 0.0
        strain = row.get("strain", 0.0) or 0.0

        tss_str = f"{tss:.0f}" if tss > 0 else "[dim]--[/dim]"
        table.add_row(
            date_str,
            tss_str,
            f"{ctl:.1f}",
            f"{atl:.1f}",
            _color_tsb(tsb),
            _color_monotony(monotony) if monotony > 0 else "[dim]--[/dim]",
            f"{strain:.0f}" if strain > 0 else "[dim]--[/dim]",
        )

    console.print(table)

    # Ramp rate
    if len(data) >= 7:
        ctl_7d_ago = data[-7].get("ctl", 0.0)
        ramp = (current_ctl - ctl_7d_ago) / 7.0
        ramp_color = "green" if 3 <= ramp <= 7 else "yellow" if 0 <= ramp <= 10 else "red"
        console.print(
            f"\n  7-day ramp rate: [{ramp_color}]{ramp:+.1f} CTL/day[/{ramp_color}]  [dim](target: 3-7 CTL/day)[/dim]"
        )

    console.print()


@analyze_app.command("recovery")
def analyze_recovery(
    days: Annotated[int, typer.Option("--days", "-d", help="Number of days to show.")] = 7,
) -> None:
    """Show daily recovery scores and component breakdown."""
    config = get_config()
    db = get_database(config.db_path)

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=days)

    recovery_data = get_recovery_scores(db, start_date, end_date)

    if not recovery_data:
        console.print("[yellow]No recovery data available for the selected period.[/yellow]")
        raise typer.Exit(0)

    # Current recovery
    current = recovery_data[-1]
    score = current.get("recovery_score", 0.0)

    console.print()
    console.print(
        Panel(
            f"[bold]Recovery Status[/bold]\n\n"
            f"  Composite Score: {_color_recovery(score)}/100\n"
            f"  Date: {current.get('date', 'N/A')}",
            border_style="green" if score > 70 else "yellow" if score >= 40 else "red",
        )
    )

    # Component breakdown for current day
    components = [
        ("HRV", current.get("hrv_component")),
        ("Sleep", current.get("sleep_component")),
        ("Body Battery", current.get("body_battery_component")),
        ("Readiness", current.get("readiness_component")),
        ("Stress", current.get("stress_component")),
        ("Fatigue", current.get("fatigue_component")),
    ]

    comp_table = Table(title="Component Breakdown", show_header=True, header_style="bold")
    comp_table.add_column("Component", style="cyan")
    comp_table.add_column("Score", justify="right")
    comp_table.add_column("Visual", width=20)

    for name, value in components:
        if value is not None:
            bar_len = int(value / 100 * 20)
            bar_char = "█" * bar_len + "░" * (20 - bar_len)
            color = "green" if value > 70 else "yellow" if value >= 40 else "red"
            comp_table.add_row(name, f"[{color}]{value:.0f}[/{color}]", f"[{color}]{bar_char}[/{color}]")
        else:
            comp_table.add_row(name, "[dim]N/A[/dim]", "[dim]░░░░░░░░░░░░░░░░░░░░[/dim]")

    console.print(comp_table)

    # Daily trend table
    trend_table = Table(title=f"\nRecovery Trend (last {days} days)", show_header=True, header_style="bold")
    trend_table.add_column("Date", style="dim")
    trend_table.add_column("Score", justify="right")
    trend_table.add_column("Trend", width=15)
    trend_table.add_column("Notes")

    for i, row in enumerate(recovery_data):
        date_str = str(row.get("date", ""))
        s = row.get("recovery_score", 0.0)

        # Simple arrow indicator
        if i > 0:
            prev = recovery_data[i - 1].get("recovery_score", 0.0)
            if s > prev + 5:
                arrow = "[green]  ↑[/green]"
            elif s < prev - 5:
                arrow = "[red]  ↓[/red]"
            else:
                arrow = "[dim]  →[/dim]"
        else:
            arrow = ""

        bar_len = int(s / 100 * 15)
        bar = "█" * bar_len + "░" * (15 - bar_len)
        color = "green" if s > 70 else "yellow" if s >= 40 else "red"
        notes = row.get("notes") or ""

        trend_table.add_row(
            date_str,
            f"{_color_recovery(s)}{arrow}",
            f"[{color}]{bar}[/{color}]",
            notes[:40] if notes else "[dim]--[/dim]",
        )

    console.print(trend_table)
    console.print()


@analyze_app.command("zones")
def analyze_zones(
    days: Annotated[int, typer.Option("--days", "-d", help="Number of days to analyze.")] = 30,
    sport: Annotated[str, typer.Option("--sport", "-s", help="Sport type filter.")] = "all",
) -> None:
    """Show training zone distribution for the selected period."""
    config = get_config()
    db = get_database(config.db_path)

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=days)

    sport_filter = None if sport == "all" else sport
    metrics = get_activity_metrics(db, sport_type=sport_filter, start_date=start_date, end_date=end_date)

    if not metrics:
        console.print("[yellow]No activity metrics available for the selected period.[/yellow]")
        raise typer.Exit(0)

    # Aggregate HR zone seconds
    hr_zones: dict[str, int] = {}

    for m in metrics:
        hr_z = m.get("hr_zone_seconds")
        if hr_z:
            if isinstance(hr_z, str):
                import json

                hr_z = json.loads(hr_z)
            if isinstance(hr_z, dict):
                for zone, secs in hr_z.items():
                    hr_zones[zone] = hr_zones.get(zone, 0) + int(secs)

    sport_label = sport.capitalize() if sport != "all" else "All Sports"
    console.print()
    console.print(
        Panel(
            f"[bold]Zone Distribution[/bold] -- {sport_label}, last {days} days",
            border_style="cyan",
        )
    )

    # Zone colors for visual bars (5 zones typical)
    zone_colors = ["green", "cyan", "yellow", "red", "bright_red"]

    def _print_zone_table(title: str, zones: dict[str, int]) -> None:
        if not zones:
            return

        total_sec = sum(zones.values())
        if total_sec == 0:
            return

        zt = Table(title=title, show_header=True, header_style="bold")
        zt.add_column("Zone", style="cyan", width=15)
        zt.add_column("Time", justify="right", width=10)
        zt.add_column("%", justify="right", width=6)
        zt.add_column("Distribution", width=30)

        sorted_zones = sorted(zones.items())

        for i, (zone_name, secs) in enumerate(sorted_zones):
            pct = (secs / total_sec) * 100
            bar_len = int(pct / 100 * 30)
            color = zone_colors[i % len(zone_colors)]
            bar = f"[{color}]{'█' * bar_len}{'░' * (30 - bar_len)}[/{color}]"

            zt.add_row(
                zone_name,
                _format_duration(secs),
                f"{pct:.1f}%",
                bar,
            )

        zt.add_row(
            "[bold]Total[/bold]",
            f"[bold]{_format_duration(total_sec)}[/bold]",
            "[bold]100%[/bold]",
            "",
        )

        console.print(zt)

    _print_zone_table("Heart Rate Zones", hr_zones)

    # Polarization index
    if hr_zones:
        sorted_z = sorted(hr_zones.items())
        total = sum(hr_zones.values())
        if len(sorted_z) >= 3 and total > 0:
            low_pct = sum(s for _, s in sorted_z[:2]) / total * 100
            high_pct = sum(s for _, s in sorted_z[-2:]) / total * 100
            mid_pct = 100 - low_pct - high_pct

            if mid_pct < 0:
                mid_pct = sum(s for _, s in sorted_z[2:-2]) / total * 100 if len(sorted_z) > 4 else 0

            console.print(
                f"\n  Training distribution: "
                f"[green]Low {low_pct:.0f}%[/green] | "
                f"[yellow]Mid {mid_pct:.0f}%[/yellow] | "
                f"[red]High {high_pct:.0f}%[/red]"
            )
            if low_pct >= 75:
                console.print("  [green]Polarized training -- good zone discipline.[/green]")
            elif low_pct >= 60:
                console.print("  [yellow]Mostly aerobic, consider more polarization.[/yellow]")
            else:
                console.print("  [red]Too much moderate intensity -- risk of grey zone training.[/red]")

    console.print()


@analyze_app.command("performance")
def analyze_performance(
    days: Annotated[int, typer.Option("--days", "-d", help="Number of days to analyze.")] = 30,
) -> None:
    """Show efficiency factor and aerobic decoupling trends."""
    config = get_config()
    db = get_database(config.db_path)

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=days)

    metrics = get_activity_metrics(db, start_date=start_date, end_date=end_date)

    if not metrics:
        console.print("[yellow]No activity metrics available for the selected period.[/yellow]")
        raise typer.Exit(0)

    console.print()
    console.print(
        Panel(
            f"[bold]Performance Trends[/bold] -- last {days} days",
            border_style="cyan",
        )
    )

    # Group by sport
    sports: dict[str, list[dict]] = {}
    for m in metrics:
        sport_type = str(m.get("sport_type", "other"))
        sports.setdefault(sport_type, []).append(m)

    for sport_type, sport_metrics in sorted(sports.items()):
        ef_values: list[tuple[str, float]] = []
        dc_values: list[tuple[str, float]] = []

        for m in sport_metrics:
            date_str = str(m.get("date", ""))
            ef = m.get("efficiency_factor")
            dc = m.get("aerobic_decoupling_pct")

            if ef is not None and ef > 0:
                ef_values.append((date_str, float(ef)))
            if dc is not None:
                dc_values.append((date_str, float(dc)))

        if not ef_values and not dc_values:
            continue

        console.print(f"\n[bold cyan]{sport_type.capitalize()}[/bold cyan]")

        if ef_values:
            # EF trend table
            ef_table = Table(
                title=f"  Efficiency Factor ({sport_type.capitalize()})",
                show_header=True,
                header_style="bold",
            )
            ef_table.add_column("Date", style="dim")
            ef_table.add_column("EF", justify="right")
            ef_table.add_column("Trend")

            ef_nums = [v for _, v in ef_values]
            ef_spark = _sparkline(ef_nums, width=20)

            for i, (date, ef) in enumerate(ef_values[-10:]):
                if i > 0:
                    prev = ef_values[max(0, len(ef_values) - 10 + i - 1)][1]
                    if ef > prev * 1.02:
                        arrow = "[green]↑[/green]"
                    elif ef < prev * 0.98:
                        arrow = "[red]↓[/red]"
                    else:
                        arrow = "[dim]→[/dim]"
                else:
                    arrow = ""
                ef_table.add_row(date, f"{ef:.3f}", arrow)

            console.print(ef_table)
            console.print(f"  EF sparkline: [cyan]{ef_spark}[/cyan]")

            # EF trend assessment
            if len(ef_nums) >= 5:
                recent_avg = sum(ef_nums[-5:]) / 5
                older_avg = sum(ef_nums[: max(1, len(ef_nums) - 5)]) / max(1, len(ef_nums) - 5)
                if recent_avg > older_avg * 1.03:
                    console.print("  [green]EF trending upward -- aerobic fitness improving.[/green]")
                elif recent_avg < older_avg * 0.97:
                    console.print("  [red]EF declining -- possible fatigue or detraining.[/red]")
                else:
                    console.print("  [yellow]EF stable.[/yellow]")

        if dc_values:
            # Decoupling table
            dc_table = Table(
                title=f"  Aerobic Decoupling ({sport_type.capitalize()})",
                show_header=True,
                header_style="bold",
            )
            dc_table.add_column("Date", style="dim")
            dc_table.add_column("Decoupling", justify="right")
            dc_table.add_column("Assessment")

            for date, dc in dc_values[-10:]:
                if dc < 5.0:
                    assessment = "[green]Well coupled[/green]"
                elif dc < 10.0:
                    assessment = "[yellow]Moderate drift[/yellow]"
                else:
                    assessment = "[red]Significant drift[/red]"
                dc_table.add_row(date, _color_decoupling(dc), assessment)

            console.print(dc_table)

            # Average decoupling
            avg_dc = sum(v for _, v in dc_values) / len(dc_values)
            console.print(
                f"  Average decoupling: {_color_decoupling(avg_dc)} [dim](target: <5% for aerobic sessions)[/dim]"
            )

    console.print()
