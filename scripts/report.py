"""Quick pipeline report: lead counts, tier distribution, send/reply rates."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from src.db import init_db, session_scope
from src.models import Engagement, GeneratedContent, Lead, Score


def main() -> None:
    init_db()
    console = Console()

    with session_scope() as session:
        total_leads = session.execute(select(func.count(Lead.id))).scalar() or 0
        tier_rows = session.execute(
            select(Score.tier, func.count(Score.id)).group_by(Score.tier)
        ).all()
        emails_total = session.execute(
            select(func.count(GeneratedContent.id))
            .where(GeneratedContent.kind == "email")
        ).scalar() or 0
        emails_delivered = session.execute(
            select(func.count(GeneratedContent.id))
            .where(GeneratedContent.kind == "email", GeneratedContent.delivered_at.is_not(None))
        ).scalar() or 0
        skip_rows = session.execute(
            select(GeneratedContent.skip_reason, func.count(GeneratedContent.id))
            .where(GeneratedContent.skip_reason.is_not(None))
            .group_by(GeneratedContent.skip_reason)
        ).all()

        opens = session.execute(
            select(func.count(Engagement.id)).where(Engagement.opened.is_(True))
        ).scalar() or 0
        clicks = session.execute(
            select(func.count(Engagement.id)).where(Engagement.clicked.is_(True))
        ).scalar() or 0
        replies = session.execute(
            select(func.count(Engagement.id)).where(Engagement.replied.is_(True))
        ).scalar() or 0
        bounces = session.execute(
            select(func.count(Engagement.id)).where(Engagement.bounced.is_(True))
        ).scalar() or 0

    overview = Table(title="Pipeline overview")
    overview.add_column("Metric")
    overview.add_column("Value", justify="right")
    overview.add_row("Total leads", str(total_leads))
    overview.add_row("Emails generated", str(emails_total))
    overview.add_row("Emails delivered", str(emails_delivered))
    if emails_delivered:
        overview.add_row("Open rate", f"{opens / emails_delivered:.0%}")
        overview.add_row("Click rate", f"{clicks / emails_delivered:.0%}")
        overview.add_row("Reply rate", f"{replies / emails_delivered:.0%}")
        overview.add_row("Bounce rate", f"{bounces / emails_delivered:.0%}")
    console.print(overview)

    if tier_rows:
        tier_table = Table(title="Scored leads by tier")
        tier_table.add_column("Tier")
        tier_table.add_column("Count", justify="right")
        for tier, count in tier_rows:
            tier_table.add_row(tier, str(count))
        console.print(tier_table)

    if skip_rows:
        skip_table = Table(title="Pre-send skips")
        skip_table.add_column("Reason")
        skip_table.add_column("Count", justify="right")
        for reason, count in skip_rows:
            skip_table.add_row(reason, str(count))
        console.print(skip_table)


if __name__ == "__main__":
    main()
