"""Discovery service: crawl competitor catalogs to discover products Sribees does not have.

For Cargills (a search-based source): crawls department / category keywords to ingest products.
For catalogue sources (e.g. SPAR, Nexmart): reads the complete catalog.
All items are upserted into competitor_items.
Items that do not match our products form the Assortment Gap.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.adapters import build
from app.adapters.base import ListedItem, PoliteClient
from app.models import Competitor
from app.services.collect import _run_sitemap, _store, propose_matches
from app.services.errors import IntelError

log = logging.getLogger("intel.discovery")

_discovery_lock = asyncio.Lock()
_current_running_id: int | None = None
_last_status_message: str | None = None


def is_running() -> bool:
    return _current_running_id is not None


def get_status() -> dict:
    return {
        "is_running": is_running(),
        "competitor_id": _current_running_id,
        "message": _last_status_message,
    }


async def _run_single(
    sessionmaker: async_sessionmaker,
    competitor_id: int,
    transport=None,
    prefix: str = "",
) -> int:
    global _last_status_message
    async with sessionmaker() as db:
        competitor = await db.get(Competitor, competitor_id)
        if competitor is None or competitor.kind != "scraped" or not competitor.base_url:
            return 0
        adapter_name = competitor.adapter
        base_url = competitor.base_url
        comp_name = competitor.name

    now = datetime.now(UTC)
    items_seen = 0

    async with PoliteClient(base_url, transport=transport) as client:
        adapter = build(adapter_name or "", client)
        if adapter is None:
            return 0

        async with adapter:
            if hasattr(adapter, "discover_categories"):
                _last_status_message = f"{prefix}Crawling categories on {comp_name}..."
                items = await adapter.discover_categories()
                items_seen = len(items)
                async with sessionmaker() as db, db.begin():
                    await _store(db, competitor_id, items, now)
                    await propose_matches(db, competitor_id)
            elif adapter.mode == "catalogue":
                _last_status_message = f"{prefix}Reading full catalog from {comp_name}..."
                batch: list[ListedItem] = []
                async for item in adapter.catalogue():
                    batch.append(item)
                    if len(batch) >= 200:
                        async with sessionmaker() as db, db.begin():
                            await _store(db, competitor_id, batch, now)
                            items_seen += len(batch)
                            batch = []
                if batch:
                    async with sessionmaker() as db, db.begin():
                        await _store(db, competitor_id, batch, now)
                        items_seen += len(batch)
                async with sessionmaker() as db, db.begin():
                    await propose_matches(db, competitor_id)
            elif adapter.mode == "sitemap":
                _last_status_message = f"{prefix}Reading sitemap from {comp_name}..."
                items_seen = await _run_sitemap(sessionmaker, competitor_id, adapter)

    async with sessionmaker() as db, db.begin():
        await db.execute(
            update(Competitor)
            .where(Competitor.competitor_id == competitor_id)
            .values(last_run_at=now, last_ok_at=now, status="ok")
        )
    return items_seen


async def run_discovery(
    sessionmaker: async_sessionmaker,
    competitor_id: int | None = None,
    transport=None,
) -> dict:
    global _current_running_id, _last_status_message
    if _current_running_id is not None:
        raise IntelError("A discovery crawl is already in progress", status_code=409)

    async with _discovery_lock:
        _current_running_id = competitor_id or 0
        try:
            if competitor_id is not None:
                _last_status_message = f"Starting discovery crawl for competitor {competitor_id}..."
                total_seen = await _run_single(sessionmaker, competitor_id, transport)
                _last_status_message = f"Discovery complete: {total_seen} items processed."
                return {"status": "ok", "competitor_id": competitor_id, "items_seen": total_seen}

            # Run for all enabled scraped competitors
            async with sessionmaker() as db:
                cids = list(await db.scalars(
                    select(Competitor.competitor_id).where(
                        Competitor.kind == "scraped",
                        Competitor.is_enabled.is_(True),
                    )
                ))

            total_seen = 0
            count = len(cids)
            for idx, cid in enumerate(cids):
                prefix = f"[{idx + 1}/{count}] "
                seen = await _run_single(sessionmaker, cid, transport, prefix=prefix)
                total_seen += seen

            _last_status_message = (
                f"Discovery complete for all competitors: {total_seen} items processed."
            )
            return {"status": "ok", "competitor_ids": cids, "items_seen": total_seen}
        except Exception as exc:
            _last_status_message = f"Discovery run failed: {exc}"
            log.exception("Discovery run failed")
            raise
        finally:
            _current_running_id = None
