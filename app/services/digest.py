"""The daily match-health email: what the shops changed or dropped in today's run."""

import asyncio
import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Competitor, Product, ProductMatch
from app.services import health

log = logging.getLogger("intel.digest")


async def lines_since(db: AsyncSession, since: datetime) -> list[str]:
    rows = (await db.execute(
        select(ProductMatch, Product.name, Competitor.name)
        .join(Product, Product.product_id == ProductMatch.product_id)
        .join(Competitor, Competitor.competitor_id == ProductMatch.competitor_id)
        .where(ProductMatch.status.in_(health.HELD), ProductMatch.health_at >= since)
        .order_by(ProductMatch.status, Competitor.name, Product.name))).all()
    label = {"changed": "වෙනස් කළා", "gone": "අයින් කළා"}
    return [f"[{label[m.status]}] {product} @ {shop}: {m.health_note}"
            for m, product, shop in rows]


def _send(subject: str, body: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, s.smtp_from, s.health_email_to
    msg.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)


async def send(db: AsyncSession, since: datetime) -> int:
    """Emails today's list; returns how many lines it held (0 = nothing sent)."""
    s = get_settings()
    if not (s.smtp_host and s.smtp_from and s.health_email_to):
        return 0
    lines = await lines_since(db, since)
    if not lines:
        return 0
    body = ("Shops වෙනස් කරපු හෝ අයින් කරපු, අපි match කරපු products. මේවායේ මිල ආයෙ "
            "බලනකම් ගණනයට ගන්නේ නෑ.\n\n" + "\n".join(lines)
            + f"\n\nබලන්න: {s.admin_url}/match-health\n")
    try:
        await asyncio.to_thread(_send, f"Price Intel: shop වෙනස්කම් {len(lines)}", body)
    except (OSError, smtplib.SMTPException):
        log.exception("match-health email failed")
        return 0
    return len(lines)
