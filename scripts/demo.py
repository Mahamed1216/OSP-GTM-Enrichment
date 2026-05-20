"""End-to-end demo with Rich CLI output.

Runs the full pipeline (always dry-run for safety) on every lead in the DB
and renders enrichment, scoring, generated content, and delivery decision
in a way that's pleasant to watch.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from src.db import init_db, session_scope
from src.logging_setup import setup_logging
from src.models import Lead
from src.pipeline import run_pipeline_for_lead


def _tier_color(tier: str) -> str:
    return {"A": "bold green", "B": "yellow", "C": "red"}.get(tier, "white")


async def amain() -> None:
    setup_logging(console=False)
    init_db()
    console = Console()

    with session_scope() as session:
        leads = [
            (l.id, f"{l.first_name} {l.last_name}", l.title, l.company, l.industry)
            for l in session.query(Lead).all()
        ]

    if not leads:
        console.print("[red]No leads in DB. Run:[/red]")
        console.print("  python scripts/ingest_leads.py data/sample_leads.csv")
        return

    console.print()
    console.print(Panel.fit(
        Text.from_markup(
            "[bold]SDR Enablement Pipeline — Demo[/bold]\n"
            f"{len(leads)} leads loaded\n"
            "[dim]Mode: DRY-RUN (no Instantly POST will be made)[/dim]"
        ),
        border_style="cyan",
    ))

    summary_table = Table(title="Result summary", show_lines=False)
    summary_table.add_column("ID", justify="right")
    summary_table.add_column("Lead")
    summary_table.add_column("Title")
    summary_table.add_column("Enrich")
    summary_table.add_column("Score")
    summary_table.add_column("Tier")
    summary_table.add_column("Delivery")

    for lid, name, title, company, _industry in leads:
        console.print()
        console.print(Rule(f"Lead {lid}: {name} — {title} @ {company}"))
        result = await run_pipeline_for_lead(lid, dry_run=True)

        if result.error:
            console.print(f"[red]ERROR: {result.error}[/red]")
            summary_table.add_row(str(lid), name, title or "", "-", "-", "-", "[red]error[/red]")
            continue

        # Enrichment
        ok = sum(1 for v in result.enrichment_status.values() if v["success"])
        total = len(result.enrichment_status)
        enrich_str = f"{ok}/{total}"
        for src_name, info in result.enrichment_status.items():
            mark = "[green]+[/green]" if info["success"] else "[red]x[/red]"
            console.print(f"  {mark} {src_name} ({info['duration_ms']}ms)")

        # Score
        score_str = "-"
        tier_str = "-"
        if result.score:
            color = _tier_color(result.score.tier)
            console.print(
                f"\n  [bold]Score:[/bold] [{color}]{result.score.score} ({result.score.tier})[/{color}]"
            )
            console.print(f"  [bold]Rationale:[/bold] {result.score.rationale}")
            if result.score.signals_used:
                console.print(f"  [bold]Signals:[/bold] {', '.join(result.score.signals_used)}")
            score_str = str(result.score.score)
            tier_str = f"[{color}]{result.score.tier}[/{color}]"

        # Email
        if result.email:
            console.print(Panel(
                Text.from_markup(
                    f"[italic]Subject:[/italic] [bold]{result.email.subject}[/bold]\n\n"
                    f"{result.email.body}\n\n"
                    f"[dim]Signals cited: {', '.join(result.email.signals_cited)}[/dim]"
                ),
                title="Generated email",
                border_style="green",
            ))

        # Call script
        if result.call_script:
            cs = result.call_script
            console.print(Panel(
                Text.from_markup(
                    f"[bold]Opener:[/bold] {cs.opener}\n"
                    f"[bold]Value prop:[/bold] {cs.value_prop}\n"
                    f"[bold]Objections:[/bold]\n" +
                    "\n".join(f"  - [yellow]{o.objection}[/yellow]\n    -> {o.response}"
                              for o in cs.objections) +
                    f"\n[bold]Close:[/bold] {cs.close}"
                ),
                title="Cold call script",
                border_style="blue",
            ))

        # LinkedIn DM
        if result.linkedin_msg:
            console.print(Panel(
                Text.from_markup(
                    result.linkedin_msg.body +
                    f"\n\n[dim]Signals cited: {', '.join(result.linkedin_msg.signals_cited)}[/dim]"
                ),
                title="LinkedIn DM",
                border_style="magenta",
            ))

        # Delivery
        delivery_str = "-"
        if result.content_skipped:
            console.print("[dim]Content generation skipped (tier below threshold).[/dim]")
            delivery_str = "[dim]skipped (tier)[/dim]"
        elif result.delivery:
            if result.delivery.dry_run:
                console.print("[yellow]Delivery: DRY RUN — payload built, not POSTed[/yellow]")
                delivery_str = "[yellow]dry-run[/yellow]"
            elif result.delivery.delivered:
                console.print(f"[green]Delivered: id={result.delivery.delivery_id}[/green]")
                delivery_str = "[green]ok[/green]"
            else:
                reason = result.delivery.skip_reason
                console.print(f"[red]Skipped: {reason}[/red]")
                delivery_str = f"[red]{reason}[/red]"

        summary_table.add_row(
            str(lid), name, title or "", enrich_str, score_str, tier_str, delivery_str
        )

    console.print()
    console.print(Rule("Summary"))
    console.print(summary_table)
    console.print(
        "\n[dim]Structured JSON logs were written to logs/sdr_run_<date>.log.[/dim]\n"
    )


if __name__ == "__main__":
    asyncio.run(amain())
