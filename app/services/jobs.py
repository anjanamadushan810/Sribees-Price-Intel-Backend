"""The daily job: sync Online -> collect every enabled source -> refresh suggestions.

Run by cron through the CLI (python -m app.cli daily) or from the admin's "Run now".
A Postgres advisory lock makes sure only one copy runs at a time.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import Competitor
from app.services import catalog_sync, collect, digest, suggest
from app.services.online_client import OnlineClient

log = logging.getLogger("intel.jobs")
LOCK_KEY = 7_331_204  # any constant; shared by every copy of this app


async def daily(sessionmaker: async_sessionmaker, *, sync: bool = True,
                competitor_ids: list[int] | None = None) -> dict:
    async with sessionmaker() as lock_db:
        got = await lock_db.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY})
        if not got:
            return {"skipped": "another run is in progress"}
        try:
            started = datetime.now(UTC)
            report: dict = {}
            if sync:
                async with OnlineClient() as online, sessionmaker() as db, db.begin():
                    report["sync"] = await catalog_sync.sync(db, online)
            async with sessionmaker() as db:
                ids = list(await db.scalars(select(Competitor.competitor_id).where(
                    Competitor.kind == "scraped", Competitor.is_enabled.is_(True))))
            for cid in competitor_ids or ids:
                run = await collect.run(sessionmaker, cid)
                report[f"source_{cid}"] = {"status": run.status, "items": run.items_seen,
                                           "error": run.error}
            async with sessionmaker() as db, db.begin():
                report["suggestions"] = await suggest.refresh(db)
            async with sessionmaker() as db:
                report["health_email"] = await digest.send(db, started)
            log.info("daily run: %s", report)
            return report
        finally:
            await lock_db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
