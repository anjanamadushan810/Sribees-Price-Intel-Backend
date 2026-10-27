"""Append-only audit trail. Written in the same transaction as the change it describes."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def record(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    action: str,
    entity: str,
    entity_id: Any = None,
    before: dict | None = None,
    after: dict | None = None,
    ip_address: str | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            before=before,
            after=after,
            ip_address=ip_address,
        )
    )
