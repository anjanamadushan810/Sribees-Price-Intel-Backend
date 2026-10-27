"""One collection run for one competitor: read prices, store them, propose matches.

Catalogue sources are read whole; search sources look up only the products whose
price_review_level says they are due. Every run leaves a scrape_runs row, and a
blocked or broken source flips the competitor's status so the admin sees it.
"""

import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters import build
from app.adapters.base import ListedItem, PoliteClient, SourceBlocked, SourceFailed
from app.models import (
    Competitor,
    CompetitorItem,
    PriceObservation,
    Product,
    ProductMatch,
    ScrapeRun,
    SearchCheck,
)
from app.services import matcher, schedule
from app.services.normalize import Parsed, barcode_of, parse

log = logging.getLogger("intel.collect")
LIVE = ("auto", "confirmed")
BATCH = 200


async def _store(db: AsyncSession, competitor_id: int, items: list[ListedItem],
                 now: datetime) -> dict[str, uuid.UUID]:
    """Upsert the items' latest prices; append history for items that are matched."""
    ids: dict[str, uuid.UUID] = {}
    for it in items:
        parsed = parse(it.name, it.pack)
        values = {
            "name": it.name[:300], "brand": (it.brand or None), "url": it.url,
            "barcode": barcode_of(it.barcode), "size_value": parsed.size_value,
            "size_unit": parsed.size_unit, "last_price": it.price, "last_mrp": it.mrp,
            "in_stock": it.in_stock, "last_seen_at": now,
        }
        item_id = await db.scalar(
            insert(CompetitorItem)
            .values(competitor_id=competitor_id, external_id=it.external_id[:120], **values)
            .on_conflict_do_update(index_elements=["competitor_id", "external_id"], set_=values)
            .returning(CompetitorItem.item_id)
        )
        ids[it.external_id] = item_id
    if ids:
        matched = set(await db.scalars(
            select(ProductMatch.item_id).where(
                ProductMatch.item_id.in_(ids.values()), ProductMatch.status.in_(LIVE))
        ))
        by_id = {ids[it.external_id]: it for it in items}
        for item_id in matched:
            it = by_id[item_id]
            db.add(PriceObservation(item_id=item_id, price=it.price, mrp=it.mrp,
                                    in_stock=it.in_stock, source="scrape", seen_at=now))
    return ids


async def propose_matches(db: AsyncSession, competitor_id: int,
                          product_ids: list[uuid.UUID] | None = None) -> int:
    """Barcode-equal pairs become 'auto'; good name matches become 'candidate' for the
    Price Checker. A pair once rejected is never proposed again (unique pair row)."""
    products_q = select(Product).where(Product.is_active.is_(True))
    if product_ids is not None:
        products_q = products_q.where(Product.product_id.in_(product_ids))
    products = list(await db.scalars(products_q))
    already = set(await db.scalars(select(ProductMatch.product_id).where(
        ProductMatch.competitor_id == competitor_id, ProductMatch.status.in_(LIVE))))
    pending = set(await db.scalars(select(ProductMatch.product_id).where(
        ProductMatch.competitor_id == competitor_id, ProductMatch.status == "candidate")))
    todo = [p for p in products if p.product_id not in already and p.product_id not in pending]
    if not todo:
        return 0

    items = list(await db.scalars(
        select(CompetitorItem).where(CompetitorItem.competitor_id == competitor_id)))
    by_barcode = {i.barcode: i for i in items if i.barcode}
    parsed_items = {i.item_id: Parsed(parse(i.name).words, i.size_value, i.size_unit)
                    for i in items}
    index: dict[str, list[uuid.UUID]] = defaultdict(list)
    for item_id, parsed in parsed_items.items():
        for word in parsed.words:
            index[word].append(item_id)

    created = 0
    for p in todo:
        twin = by_barcode.get(p.barcode) if p.barcode else None
        if twin is not None:
            result = await db.execute(insert(ProductMatch).values(
                product_id=p.product_id, item_id=twin.item_id, competitor_id=competitor_id,
                status="auto", method="barcode", score=1,
            ).on_conflict_do_nothing())
            created += result.rowcount or 0
            continue
        ours = Parsed(parse(p.name).words, p.size_value, p.size_unit)
        pool = {item_id for w in ours.words for item_id in index.get(w, ())}
        for c in matcher.best_candidates(ours, [(i, parsed_items[i]) for i in pool]):
            result = await db.execute(insert(ProductMatch).values(
                product_id=p.product_id, item_id=c.key, competitor_id=competitor_id,
                status="candidate", method="name", score=c.score,
            ).on_conflict_do_nothing())
            created += result.rowcount or 0
    return created


def search_query(name: str) -> str:
    """Brand and kind, without the pack size: shops spell sizes differently."""
    words = [w for w in parse(name).words if not w.isdigit()]
    ordered = [w for w in name.lower().replace("-", " ").split() if w in words]
    return " ".join(ordered[:4]) or name[:40]


async def run(sessionmaker: async_sessionmaker, competitor_id: int,
              transport=None) -> ScrapeRun:
    async with sessionmaker() as db, db.begin():
        competitor = await db.get(Competitor, competitor_id)
        if competitor is None or competitor.kind != "scraped" or not competitor.base_url:
            raise ValueError("not a scraped competitor")
        scrape_run = ScrapeRun(competitor_id=competitor_id)
        db.add(scrape_run)
        await db.flush()
        run_id, adapter_name, base_url = scrape_run.run_id, competitor.adapter, competitor.base_url

    status, error, seen, requests = "ok", None, 0, 0
    try:
        async with PoliteClient(base_url, transport=transport) as client:
            adapter = build(adapter_name or "", client)
            if adapter is None:
                raise SourceFailed(f"adapter '{adapter_name}' is not written yet")
            try:
                if adapter.mode == "catalogue":
                    seen = await _run_catalogue(sessionmaker, competitor_id, adapter)
                else:
                    seen = await _run_search(sessionmaker, competitor_id, adapter)
            finally:
                requests = client.requests
    except SourceBlocked as exc:
        status, error = "blocked", str(exc)
    except SourceFailed as exc:
        status, error = "failed", str(exc)
    except Exception as exc:  # noqa: BLE001 -- recorded on the run, then re-raised
        status, error = "failed", f"{exc.__class__.__name__}: {exc}"
        log.exception("collection run %s crashed", run_id)

    now = datetime.now(UTC)
    async with sessionmaker() as db, db.begin():
        scrape_run = await db.get(ScrapeRun, run_id)
        scrape_run.status, scrape_run.error = status, error
        scrape_run.items_seen, scrape_run.requests, scrape_run.finished_at = seen, requests, now
        values = {"last_run_at": now,
                  "status": {"ok": "ok", "blocked": "blocked"}.get(status, "failing")}
        if status == "ok":
            values["last_ok_at"] = now
        await db.execute(update(Competitor).where(Competitor.competitor_id == competitor_id)
                         .values(**values))
    return scrape_run


async def _run_catalogue(sessionmaker, competitor_id: int, adapter) -> int:
    now = datetime.now(UTC)
    seen, batch = 0, []
    async for item in adapter.catalogue():
        batch.append(item)
        if len(batch) >= BATCH:
            async with sessionmaker() as db, db.begin():
                await _store(db, competitor_id, batch, now)
            seen += len(batch)
            batch = []
    async with sessionmaker() as db, db.begin():
        await _store(db, competitor_id, batch, now)
        seen += len(batch)
        await db.execute(update(CompetitorItem).where(
            CompetitorItem.competitor_id == competitor_id,
            CompetitorItem.last_seen_at < now,
        ).values(in_stock=False))  # no longer listed: treat as not on sale there
        await propose_matches(db, competitor_id)
    return seen


async def _run_search(sessionmaker, competitor_id: int, adapter) -> int:
    now = datetime.now(UTC)
    async with sessionmaker() as db:
        products = list(await db.scalars(select(Product).where(Product.is_active.is_(True))))
        checked = dict((await db.execute(select(SearchCheck.product_id, SearchCheck.checked_at)
                        .where(SearchCheck.competitor_id == competitor_id))).all())
    due = [p for p in products if schedule.is_due(p.review_level, checked.get(p.product_id), now)]
    seen = 0
    for p in due:
        items = await adapter.search(search_query(p.name))
        async with sessionmaker() as db, db.begin():
            await _store(db, competitor_id, items, now)
            await db.execute(insert(SearchCheck).values(
                product_id=p.product_id, competitor_id=competitor_id, checked_at=now,
            ).on_conflict_do_update(index_elements=["product_id", "competitor_id"],
                                    set_={"checked_at": now}))
            await propose_matches(db, competitor_id, [p.product_id])
        seen += len(items)
    return seen
