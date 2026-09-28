"""Free-form agent query command.

Routes natural-language questions to Claude Code agents via the CLI and
renders the response as rich markdown in the terminal.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner

logger = logging.getLogger(__name__)
console = Console()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent


def run_query(question: str, agent: str = "") -> None:
    """Execute a free-form query against a Claude Code agent.

    Parameters
    ----------
    question:
        Natural-language question about training, fitness, recovery, or
        race preparation.
    agent:
        Optional agent name (without .md extension). Defaults to
        training-load-analyst.
    """
    console.print()
    console.print(
        Panel(
            f"[bold cyan]Question:[/bold cyan] {question}",
            border_style="cyan",
        )
    )

    agent_name = agent or "training-load-analyst"

    cmd = [
        "claude",
        "-p",
        question,
        "--allowedTools",
        "mcp__hart__*",
        "--agent",
        agent_name,
    ]

    with Live(Spinner("dots", text="Thinking..."), console=console, transient=True):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
            )
            if result.returncode == 0 and result.stdout.strip():
                answer = result.stdout.strip()
            elif result.stderr.strip():
                answer = f"Agent error: {result.stderr.strip()}"
            else:
                answer = "No response from agent."
        except FileNotFoundError:
            answer = "Error: 'claude' CLI not found. Install Claude Code to enable agent-powered queries."

    # Render the answer
    console.print()
    try:
        md = Markdown(answer)
        console.print(
            Panel(
                md,
                title="[bold green]Answer[/bold green]",
                border_style="green",
                padding=(1, 2),
            )
        )
    except Exception:
        console.print(
            Panel(
                answer,
                title="[bold green]Answer[/bold green]",
                border_style="green",
                padding=(1, 2),
            )
        )

    console.print()
