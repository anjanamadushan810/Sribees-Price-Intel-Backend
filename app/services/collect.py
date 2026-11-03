"""One collection run for one competitor: read prices, store them, propose matches.

Catalogue sources are read whole; search sources look up only the products whose
price_review_level says they are due. Every run leaves a scrape_runs row, and a
blocked or broken source flips the competitor's status so the admin sees it.
"""

import contextlib
import logging
import uuid
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters import build
from app.adapters.base import ListedItem, PoliteClient, SourceBlocked, SourceFailed
from app.models import (
    BranchProduct,
    Competitor,
    CompetitorItem,
    PriceObservation,
    Product,
    ProductMatch,
    ScrapeRun,
    SearchCheck,
)
from app.services import health, matcher, schedule
from app.services.normalize import Parsed, barcode_of, parse

log = logging.getLogger("intel.collect")
LIVE = ("auto", "confirmed")
BATCH = 200
MAX_FAILURES_IN_A_ROW = 5


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
        if it.image:
            values["image_url"] = it.image
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
    """Barcode-equal pairs and sure name matches (matcher.auto_pick) become 'auto'; other
    good name matches become 'candidate' for a person. A pair once rejected is never
    proposed again (unique pair row)."""
    products_q = select(Product).where(Product.is_active.is_(True))
    if product_ids is not None:
        products_q = products_q.where(Product.product_id.in_(product_ids))
    products = list(await db.scalars(products_q))
    already = set(await db.scalars(select(ProductMatch.product_id).where(
        ProductMatch.competitor_id == competitor_id, ProductMatch.status.in_(LIVE))))
    pending = set(await db.scalars(select(ProductMatch.product_id).where(
        ProductMatch.competitor_id == competitor_id, ProductMatch.status == "candidate")))
    todo = [p for p in products if p.product_id not in already and p.product_id not in pending]
    promoted = await _promote_sure_candidates(db, competitor_id, pending)
    if not todo:
        return promoted

    items = list(await db.scalars(
        select(CompetitorItem).where(CompetitorItem.competitor_id == competitor_id)))
    by_barcode = {i.barcode: i for i in items if i.barcode}
    parsed_items = {i.item_id: _with_size(parse(i.name), i.size_value, i.size_unit)
                    for i in items}
    index: dict[str, list[uuid.UUID]] = defaultdict(list)
    for item_id, parsed in parsed_items.items():
        for word in parsed.words:
            index[word].append(item_id)

    created = 0
    for p in todo:
        twin = by_barcode.get(p.barcode) if p.barcode else None
        # Owner (2026-10-29): the pack's own barcode is trusted as it is; no other check.
        if twin is not None:
            result = await db.execute(insert(ProductMatch).values(
                product_id=p.product_id, item_id=twin.item_id, competitor_id=competitor_id,
                status="auto", method="barcode", score=1,
            ).on_conflict_do_nothing())
            created += result.rowcount or 0
            continue
        ours = _with_size(parse(p.name), p.size_value, p.size_unit)
        pool = {item_id for w in ours.words for item_id in index.get(w, ())}
        ranked = matcher.best_candidates(ours, [(i, parsed_items[i]) for i in pool])
        sure = matcher.auto_pick(ours, [(c, parsed_items[c.key]) for c in ranked])
        for c in [sure] if sure else ranked:
            result = await db.execute(insert(ProductMatch).values(
                product_id=p.product_id, item_id=c.key, competitor_id=competitor_id,
                status="auto" if sure else "candidate", method="name", score=c.score,
            ).on_conflict_do_nothing())
            created += result.rowcount or 0
            if sure and result.rowcount:
                await _seed_history(db, sure.key)
    # New candidates may already agree on price as well as name and size.
    await db.flush()
    promoted += await _promote_sure_candidates(db, competitor_id, {p.product_id for p in todo})
    await price_sanity(db, competitor_id)
    return created + promoted


# A matched price beyond half / double ours is a different product (2 pads vs 20, 75 g
# vs a tin) -- unless other shops charge the same, in which case it is our price that is
# wrong (a placeholder, a typo) and the match is right.
SANE_LOW, SANE_HIGH = Decimal("0.5"), Decimal("2")
PEER_AGREEMENT = Decimal("0.15")


def price_sane(ours: Decimal | None, theirs: Decimal | None, peers: list[Decimal]) -> bool:
    if ours is None or theirs is None or ours in matcher.PLACEHOLDER_PRICES:
        return True
    if SANE_LOW <= theirs / ours <= SANE_HIGH:
        return True
    if peers:
        middle = sorted(peers)[len(peers) // 2]
        return abs(theirs - middle) <= middle * PEER_AGREEMENT
    return False


async def _price_context(db: AsyncSession, product_ids) -> tuple[dict, dict]:
    """Our price per product (highest branch selling price) and every live match's
    shop price per product, as {product_id: [(match_id, price)]}."""
    ids = list(product_ids)
    if not ids:
        return {}, {}
    ours = dict((await db.execute(
        select(BranchProduct.product_id, func.max(BranchProduct.selling_price))
        .where(BranchProduct.product_id.in_(ids), BranchProduct.is_active.is_(True))
        .group_by(BranchProduct.product_id))).all())
    peers: dict = defaultdict(list)
    for pid, mid, price in (await db.execute(
            select(ProductMatch.product_id, ProductMatch.match_id, CompetitorItem.last_price)
            .join(CompetitorItem, CompetitorItem.item_id == ProductMatch.item_id)
            .where(ProductMatch.product_id.in_(ids), ProductMatch.status.in_(LIVE),
                   CompetitorItem.last_price.is_not(None)))).all():
        peers[pid].append((mid, price))
    return ours, peers


async def price_sanity(db: AsyncSession, competitor_id: int | None = None) -> dict:
    """Re-judge automatic NAME matches (never a person's, never a barcode's -- owner
    2026-10-29: barcode matches are trusted): a price out of line with ours and with the
    other shops goes back to the queue (or away, when it no longer passes brand and size)."""
    q = (select(ProductMatch, Product)
         .join(Product, Product.product_id == ProductMatch.product_id)
         .where(ProductMatch.status == "auto", ProductMatch.method == "name"))
    if competitor_id is not None:
        q = q.where(ProductMatch.competitor_id == competitor_id)
    rows = (await db.execute(q)).all()
    ours_by_product, peers = await _price_context(db, {p.product_id for _, p in rows})
    counts = {"to_queue": 0, "removed": 0}
    for m, p in rows:
        ours = _with_size(parse(p.name), p.size_value, p.size_unit)
        theirs = _with_size(parse(m.item.name), m.item.size_value, m.item.size_unit)
        others = [price for mid, price in peers.get(p.product_id, []) if mid != m.match_id]
        if price_sane(ours_by_product.get(p.product_id), m.item.last_price, others):
            continue
        if matcher.brand_ok(ours, theirs) and matcher.same_size(ours, theirs) is True:
            m.status, m.method = "candidate", "name"
            m.score = matcher.score(ours, theirs)
            counts["to_queue"] += 1
        else:
            await db.delete(m)
            counts["removed"] += 1
    await db.flush()
    return counts


async def _seed_history(db: AsyncSession, item_id: uuid.UUID) -> None:
    """A newly matched item starts its history with the price it was last seen at."""
    item = await db.get(CompetitorItem, item_id)
    if item is not None and item.last_price is not None:
        db.add(PriceObservation(item_id=item.item_id, price=item.last_price, mrp=item.last_mrp,
                                in_stock=item.in_stock, source="scrape",
                                seen_at=item.last_seen_at))


async def _promote_sure_candidates(db: AsyncSession, competitor_id: int,
                                   product_ids: set[uuid.UUID]) -> int:
    """Candidates proposed before the auto rule (or before a better title arrived) are
    re-judged: a sure one becomes 'auto' and its rivals leave the queue (the product is
    matched at this shop now, so they are never proposed again)."""
    if not product_ids:
        return 0
    rows = (await db.execute(
        select(ProductMatch, Product).join(Product, Product.product_id == ProductMatch.product_id)
        .where(ProductMatch.competitor_id == competitor_id, ProductMatch.status == "candidate",
               ProductMatch.product_id.in_(product_ids))
        .order_by(ProductMatch.product_id, ProductMatch.score.desc()))).all()
    by_product: dict[uuid.UUID, list] = defaultdict(list)
    for m, prod in rows:
        by_product[prod.product_id].append((m, prod))
    our_prices, shop_prices = await _price_context(db, by_product)
    promoted = 0
    for pairs in by_product.values():
        prod = pairs[0][1]
        ours = _with_size(parse(prod.name), prod.size_value, prod.size_unit)
        ranked = [(matcher.Candidate(m, float(m.score)),
                   _with_size(parse(m.item.name), m.item.size_value, m.item.size_unit))
                  for m, _ in pairs]
        sure = matcher.auto_pick(ours, ranked) or matcher.price_pick(
            ours, prod.name, our_prices.get(prod.product_id),
            [(c, parsed, m.item.last_price)
             for (c, parsed), (m, _) in zip(ranked, pairs, strict=True)])
        if sure is None or not price_sane(
                our_prices.get(prod.product_id), sure.key.item.last_price,
                [price for _, price in shop_prices.get(prod.product_id, [])]):
            continue
        for m, _ in pairs:
            if m is sure.key:
                m.status = "auto"
            else:
                await db.delete(m)
        await db.flush()
        await _seed_history(db, sure.key.item_id)
        promoted += 1
    return promoted


def _with_size(parsed: Parsed, size_value, size_unit) -> Parsed:
    """The title's words, brand and pack kind with the size we stored (it may come from
    a separate pack field or Online's weight, not the title)."""
    return replace(parsed, size_value=size_value, size_unit=size_unit)


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

    status, error, seen, requests, mode = "ok", None, 0, 0, None
    started = datetime.now(UTC)
    try:
        async with PoliteClient(base_url, transport=transport) as client:
            adapter = build(adapter_name or "", client)
            if adapter is None:
                raise SourceFailed(f"adapter '{adapter_name}' is not written yet")
            mode = adapter.mode
            try:
                async with adapter:
                    if adapter.mode == "catalogue":
                        seen = await _run_catalogue(sessionmaker, competitor_id, adapter)
                    elif adapter.mode == "sitemap":
                        seen = await _run_sitemap(sessionmaker, competitor_id, adapter)
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

    if status == "ok":
        # Only a good run can say an item changed or left the shop.
        async with sessionmaker() as db, db.begin():
            counts = await health.judge(db, competitor_id, started,
                                        expect_all=mode in ("catalogue", "sitemap"))
            if counts["gone"]:
                await propose_matches(db, competitor_id)  # a replacement, same rules
        log.info("match health at shop %s: %s", competitor_id, counts)

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
    seen, failures_in_a_row = 0, 0
    for p in due:
        try:
            items = await adapter.search(search_query(p.name))
        except SourceFailed as exc:
            # One slow or broken page skips that product (it stays due for the next run);
            # only a shop that keeps failing ends the run. A block still ends it at once.
            failures_in_a_row += 1
            log.warning("search for %r failed: %s", p.name, exc)
            if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                raise
            continue
        failures_in_a_row = 0
        async with sessionmaker() as db:
            live = await db.scalar(select(ProductMatch).where(
                ProductMatch.product_id == p.product_id,
                ProductMatch.competitor_id == competitor_id, ProductMatch.status.in_(LIVE)))
        missing = live is not None and live.item.external_id not in {
            it.external_id for it in items}
        if missing:
            # Our search may simply word it differently: ask for the item by its own name.
            try:
                again = await adapter.search(search_query(live.item.name))
                items = items + again
                missing = live.item.external_id not in {it.external_id for it in again}
            except SourceFailed:
                missing = False  # not knowing is not "gone"
        async with sessionmaker() as db, db.begin():
            await _store(db, competitor_id, items, now)
            if missing:
                await health.miss(db, await db.get(ProductMatch, live.match_id))
            await db.execute(insert(SearchCheck).values(
                product_id=p.product_id, competitor_id=competitor_id, checked_at=now,
            ).on_conflict_do_update(index_elements=["product_id", "competitor_id"],
                                    set_={"checked_at": now}))
            await propose_matches(db, competitor_id, [p.product_id])
        seen += len(items)
    return seen


async def _run_sitemap(sessionmaker, competitor_id: int, adapter) -> int:
    """Names from the shop's public sitemap (one request) for matching; product pages
    read (rendered, when the shop needs a browser) only for live matches that are due."""
    from app.adapters.browser import Renderer
    from app.adapters.keells import Listed

    now = datetime.now(UTC)
    listed = await adapter.listing()
    async with sessionmaker() as db, db.begin():
        for it in listed:
            parsed = parse(it.name)
            values = {"name": it.name[:300], "url": it.url, "size_value": parsed.size_value,
                      "size_unit": parsed.size_unit, "last_seen_at": now}
            await db.execute(
                insert(CompetitorItem)
                .values(competitor_id=competitor_id, external_id=it.external_id[:120],
                        in_stock=True, **values)
                .on_conflict_do_update(index_elements=["competitor_id", "external_id"],
                                       set_=values))
        await propose_matches(db, competitor_id)
        rows = (await db.execute(
            select(CompetitorItem, Product.review_level)
            .join(ProductMatch, ProductMatch.item_id == CompetitorItem.item_id)
            .join(Product, Product.product_id == ProductMatch.product_id)
            .where(ProductMatch.competitor_id == competitor_id,
                   ProductMatch.status.in_(LIVE), Product.is_active.is_(True)))).all()
    checked = {}
    async with sessionmaker() as db:
        for item_id, at in (await db.execute(
                select(PriceObservation.item_id, func.max(PriceObservation.seen_at))
                .where(PriceObservation.item_id.in_([i.item_id for i, _ in rows]))
                .group_by(PriceObservation.item_id))).all():
            checked[item_id] = at
    due = [(item, level) for item, level in rows
           if schedule.is_due(level, checked.get(item.item_id), now)]
    if not due:
        return len(listed)
    async with contextlib.AsyncExitStack() as stack:
        renderer = (await stack.enter_async_context(Renderer(adapter.min_delay))
                    if adapter.needs_browser else None)
        for item, _level in due:
            found = await adapter.price(renderer, Listed(item.external_id, item.name, item.url))
            async with sessionmaker() as db, db.begin():
                if found is None:
                    await db.execute(update(CompetitorItem).where(
                        CompetitorItem.item_id == item.item_id).values(in_stock=False))
                else:
                    await _store(db, competitor_id, [found], datetime.now(UTC))
    return len(listed)


async def refresh_queue(sessionmaker: async_sessionmaker, competitor_id: int,
                        transport=None) -> int:
    """Re-read, now, the shop items waiting in the match queue at this shop -- search
    shops look up the waiting products again, sitemap shops read the waiting items'
    pages -- so a person deciding sees today's photo and price. Same politeness rules."""
    async with sessionmaker() as db:
        competitor = await db.get(Competitor, competitor_id)
        waiting = (await db.execute(
            select(ProductMatch.product_id, CompetitorItem)
            .join(CompetitorItem, CompetitorItem.item_id == ProductMatch.item_id)
            .where(ProductMatch.competitor_id == competitor_id,
                   ProductMatch.status == "candidate"))).all()
        products = {pid: await db.get(Product, pid) for pid, _ in waiting}
    if not waiting or competitor is None or competitor.kind != "scraped":
        return 0
    now, seen = datetime.now(UTC), 0
    async with PoliteClient(competitor.base_url, transport=transport) as client:
        adapter = build(competitor.adapter or "", client)
        if adapter is None:
            return 0
        async with adapter:
            if adapter.mode == "search":
                for product in products.values():
                    try:
                        items = await adapter.search(search_query(product.name))
                    except SourceFailed:
                        continue
                    async with sessionmaker() as db, db.begin():
                        await _store(db, competitor_id, items, now)
                    seen += len(items)
            elif adapter.mode == "sitemap":
                from app.adapters.browser import Renderer
                from app.adapters.keells import Listed

                async with contextlib.AsyncExitStack() as stack:
                    renderer = (await stack.enter_async_context(Renderer(adapter.min_delay))
                                if adapter.needs_browser else None)
                    for _, item in waiting:
                        found = await adapter.price(
                            renderer, Listed(item.external_id, item.name, item.url))
                        if found is not None:
                            async with sessionmaker() as db, db.begin():
                                await _store(db, competitor_id, [found], datetime.now(UTC))
                            seen += 1
    return seen
