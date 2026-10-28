"""Competitors, our products against their prices, the match queue, manual prices, costs."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, client_ip, require
from app.api.schemas import (
    CompetitorOut,
    CompetitorPatch,
    CompetitorPrice,
    CostIn,
    HistoryPoint,
    ItemOut,
    ManualLinkIn,
    ManualPriceIn,
    MatchQueueItem,
    ProductPage,
    ProductRow,
    RunOut,
)
from app.core.db import get_db
from app.models import (
    Branch,
    BranchProduct,
    Competitor,
    CompetitorItem,
    PriceObservation,
    Product,
    ProductMatch,
    ScrapeRun,
    Suggestion,
)
from app.services import audit, matches, schedule
from app.services.suggest import pricing_settings, supersede_for

router = APIRouter(tags=["market"])
LIVE = ("auto", "confirmed")


# ---------------------------------------------------------------- competitors
@router.get("/competitors", response_model=list[CompetitorOut])
async def list_competitors(_: Principal = Depends(require("competitors.read")),
                           db: AsyncSession = Depends(get_db)):
    return list(await db.scalars(select(Competitor).order_by(Competitor.competitor_id)))


@router.patch("/competitors/{competitor_id}", response_model=CompetitorOut)
async def update_competitor(competitor_id: int, body: CompetitorPatch, request: Request,
                            p: Principal = Depends(require("competitors.write")),
                            db: AsyncSession = Depends(get_db)):
    c = await db.get(Competitor, competitor_id, with_for_update=True)
    if c is None:
        raise HTTPException(status_code=404, detail="competitor not found")
    changes = body.model_dump(exclude_unset=True)
    if changes.get("is_enabled") and c.status == "pending":
        raise HTTPException(status_code=422, detail="this source's adapter is not written yet")
    before = {"is_enabled": c.is_enabled, "notes": c.notes}
    for field, value in changes.items():
        setattr(c, field, value)
    await audit.record(db, user_id=p.user_id, action="competitor.update", entity="competitor",
                       entity_id=competitor_id, before=before, after=changes,
                       ip_address=client_ip(request))
    return c


@router.get("/competitors/{competitor_id}/runs", response_model=list[RunOut])
async def list_runs(competitor_id: int, limit: int = Query(default=30, le=200),
                    _: Principal = Depends(require("competitors.read")),
                    db: AsyncSession = Depends(get_db)):
    return list(await db.scalars(
        select(ScrapeRun).where(ScrapeRun.competitor_id == competitor_id)
        .order_by(ScrapeRun.run_id.desc()).limit(limit)))


@router.get("/items", response_model=list[ItemOut])
async def search_items(q: str = Query(min_length=2, max_length=80),
                       competitor_id: int | None = None,
                       _: Principal = Depends(require("matches.read")),
                       db: AsyncSession = Depends(get_db)):
    """Free search in competitors' lists, for linking a product by hand."""
    stmt = select(CompetitorItem).order_by(CompetitorItem.name).limit(50)
    for word in q.split()[:5]:
        stmt = stmt.where(CompetitorItem.name.ilike(f"%{word}%"))
    if competitor_id:
        stmt = stmt.where(CompetitorItem.competitor_id == competitor_id)
    return list(await db.scalars(stmt))


# ---------------------------------------------------------------- products vs market
def _branch_for(p: Principal, branch_id: uuid.UUID | None, branches: list[Branch]) -> uuid.UUID:
    if branch_id is not None:
        if not p.can_see_branch(branch_id):
            raise HTTPException(status_code=403, detail="you do not have access to this branch")
        return branch_id
    scope = p.branch_scope()
    for b in branches:
        if scope is None or b.branch_id in scope:
            return b.branch_id
    raise HTTPException(status_code=422, detail="no branch available; sync the catalogue first")


async def _rows(db: AsyncSession, branch_id: uuid.UUID, product_ids: list[uuid.UUID] | None,
                ) -> list[ProductRow]:
    now = datetime.now(UTC)
    stale = (await pricing_settings(db))["stale_days"]
    pq = select(Product).where(Product.is_active.is_(True)).order_by(Product.name)
    if product_ids is not None:
        pq = pq.where(Product.product_id.in_(product_ids))
    products = list(await db.scalars(pq))
    names = dict((await db.execute(select(Competitor.competitor_id, Competitor.name))).all())
    bps = {bp.product_id: bp for bp in await db.scalars(
        select(BranchProduct).where(BranchProduct.branch_id == branch_id))}
    matches: dict[uuid.UUID, list[ProductMatch]] = {}
    for m in await db.scalars(select(ProductMatch).where(ProductMatch.status.in_(LIVE))):
        matches.setdefault(m.product_id, []).append(m)
    pending = dict((await db.execute(
        select(Suggestion.product_id, Suggestion.suggestion_id).where(
            Suggestion.branch_id == branch_id, Suggestion.status == "pending"))).all())

    rows = []
    for prod in products:
        bp = bps.get(prod.product_id)
        prices = []
        for m in matches.get(prod.product_id, []):
            fresh = m.item.last_price is not None and m.item.in_stock and schedule.is_fresh(
                prod.review_level, m.item.last_seen_at, now, stale)
            prices.append(CompetitorPrice(
                competitor_id=m.competitor_id, competitor=names.get(m.competitor_id, "?"),
                match_id=m.match_id, status=m.status, item=ItemOut.model_validate(m.item),
                fresh=fresh))
        live = [cp for cp in prices if cp.fresh]
        cheapest = min(live, key=lambda cp: cp.item.last_price) if live else None
        ours = bp.selling_price if bp else None
        if cheapest is None or ours is None:
            position = "unknown"
        elif ours < cheapest.item.last_price:
            position = "cheaper"
        elif ours == cheapest.item.last_price:
            position = "equal"
        else:
            position = "dearer"
        rows.append(ProductRow(
            product_id=prod.product_id, name=prod.name, name_si=prod.name_si, sku=prod.sku,
            category=prod.category, review_level=prod.review_level, mrp=prod.mrp,
            our_price=ours, cost_price=bp.cost_price if bp else None,
            cheapest_price=cheapest.item.last_price if cheapest else None,
            cheapest_competitor=cheapest.competitor if cheapest else None,
            position=position, prices=sorted(prices, key=lambda cp: cp.competitor_id),
            pending_suggestion_id=pending.get(prod.product_id),
        ))
    return rows


@router.get("/products", response_model=ProductPage)
async def list_products(
    branch_id: uuid.UUID | None = None,
    q: str | None = Query(default=None, max_length=80),
    review_level: str | None = Query(default=None, pattern="^(daily|weekly|monthly|long)$"),
    position: str | None = Query(default=None, pattern="^(cheaper|equal|dearer|unknown)$"),
    cost_missing: bool = False,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    p: Principal = Depends(require("prices.read")),
    db: AsyncSession = Depends(get_db),
):
    branches = list(await db.scalars(select(Branch).where(Branch.is_active.is_(True))
                                     .order_by(Branch.name)))
    rows = await _rows(db, _branch_for(p, branch_id, branches), None)
    if q:
        words = q.lower().split()
        rows = [r for r in rows if all(
            w in f"{r.name} {r.name_si or ''} {r.sku or ''}".lower() for w in words)]
    if review_level:
        rows = [r for r in rows if r.review_level == review_level]
    if position:
        rows = [r for r in rows if r.position == position]
    if cost_missing:
        rows = [r for r in rows if r.cost_price is None and r.our_price is not None]
    total = len(rows)
    pages = max(1, -(-total // limit))
    return ProductPage(items=rows[(page - 1) * limit: page * limit], total=total, page=page,
                       pages=pages)


@router.get("/products/{product_id}", response_model=ProductRow)
async def get_product(product_id: uuid.UUID, branch_id: uuid.UUID | None = None,
                      p: Principal = Depends(require("prices.read")),
                      db: AsyncSession = Depends(get_db)):
    branches = list(await db.scalars(select(Branch).where(Branch.is_active.is_(True))
                                     .order_by(Branch.name)))
    rows = await _rows(db, _branch_for(p, branch_id, branches), [product_id])
    if not rows:
        raise HTTPException(status_code=404, detail="product not found")
    return rows[0]


@router.get("/products/{product_id}/history", response_model=list[HistoryPoint])
async def product_history(product_id: uuid.UUID, days: int = Query(default=90, le=730),
                          _: Principal = Depends(require("prices.read")),
                          db: AsyncSession = Depends(get_db)):
    since = datetime.now(UTC).timestamp() - days * 86400
    rows = await db.execute(
        select(Competitor.name, PriceObservation.price, PriceObservation.seen_at,
               PriceObservation.source)
        .join(CompetitorItem, CompetitorItem.item_id == PriceObservation.item_id)
        .join(Competitor, Competitor.competitor_id == CompetitorItem.competitor_id)
        .join(ProductMatch, ProductMatch.item_id == CompetitorItem.item_id)
        .where(ProductMatch.product_id == product_id, ProductMatch.status.in_(LIVE),
               PriceObservation.seen_at >= datetime.fromtimestamp(since, UTC))
        .order_by(PriceObservation.seen_at))
    return [HistoryPoint(competitor=c, price=pr, seen_at=s, source=src)
            for c, pr, s, src in rows]


# ---------------------------------------------------------------- match queue
@router.get("/matches/queue", response_model=list[MatchQueueItem])
async def match_queue(competitor_id: int | None = None, limit: int = Query(default=50, le=200),
                      _: Principal = Depends(require("matches.read")),
                      db: AsyncSession = Depends(get_db)):
    """Best candidates first, so one confirmation clears the most likely pair."""
    stmt = (select(ProductMatch, Product, Competitor.name)
            .join(Product, Product.product_id == ProductMatch.product_id)
            .join(Competitor, Competitor.competitor_id == ProductMatch.competitor_id)
            .where(ProductMatch.status == "candidate", Product.is_active.is_(True))
            .order_by(ProductMatch.score.desc(), Product.name).limit(limit))
    if competitor_id:
        stmt = stmt.where(ProductMatch.competitor_id == competitor_id)
    out = []
    for m, prod, cname in (await db.execute(stmt)).all():
        size = f"{prod.size_value.normalize():f} {prod.size_unit}" if prod.size_value else None
        out.append(MatchQueueItem(
            match_id=m.match_id, product_id=prod.product_id, product_name=prod.name,
            product_size=size, competitor_id=m.competitor_id, competitor=cname,
            score=m.score, item=ItemOut.model_validate(m.item)))
    return out


@router.post("/matches/{match_id}/confirm", status_code=204)
async def confirm_match(match_id: uuid.UUID, request: Request,
                        p: Principal = Depends(require("matches.decide")),
                        db: AsyncSession = Depends(get_db)):
    await matches.confirm(db, match_id, p.user_id, client_ip(request))


@router.post("/matches/{match_id}/reject", status_code=204)
async def reject_match(match_id: uuid.UUID, request: Request,
                       p: Principal = Depends(require("matches.decide")),
                       db: AsyncSession = Depends(get_db)):
    """Also undoes a confirmed or auto match that turned out wrong."""
    await matches.reject(db, match_id, p.user_id, client_ip(request))


@router.post("/matches", status_code=201)
async def link_by_hand(body: ManualLinkIn, request: Request,
                       p: Principal = Depends(require("matches.decide")),
                       db: AsyncSession = Depends(get_db)):
    item = await db.get(CompetitorItem, body.item_id)
    if item is None or await db.get(Product, body.product_id) is None:
        raise HTTPException(status_code=404, detail="product or item not found")
    if await db.scalar(select(ProductMatch).where(
            ProductMatch.product_id == body.product_id,
            ProductMatch.competitor_id == item.competitor_id,
            ProductMatch.status.in_(LIVE))):
        raise HTTPException(status_code=409, detail="this product is already matched there; "
                                                    "reject that match first")
    now = datetime.now(UTC)
    m = await db.scalar(select(ProductMatch).where(
        ProductMatch.product_id == body.product_id, ProductMatch.item_id == body.item_id))
    if m is None:
        m = ProductMatch(product_id=body.product_id, item_id=item.item_id,
                         competitor_id=item.competitor_id, method="manual", score=1)
        db.add(m)
    m.status, m.decided_by, m.decided_at = "confirmed", p.user_id, now
    await db.flush()
    await matches.seed_history(db, item)
    await audit.record(db, user_id=p.user_id, action="match.link", entity="match",
                       entity_id=m.match_id, after={"product_id": str(body.product_id),
                                                    "item": item.name},
                       ip_address=client_ip(request))
    return {"match_id": str(m.match_id)}


# ---------------------------------------------------------------- manual prices
@router.post("/observations/manual", status_code=201)
async def manual_price(body: ManualPriceIn, request: Request,
                       p: Principal = Depends(require("observations.manual")),
                       db: AsyncSession = Depends(get_db)):
    """A shelf price seen in a shop with no website (Glomark, Laugfs, local shops).
    The shop's item for our product is created and matched on first entry."""
    c = await db.get(Competitor, body.competitor_id)
    prod = await db.get(Product, body.product_id)
    if c is None or prod is None:
        raise HTTPException(status_code=404, detail="shop or product not found")
    if c.kind != "manual":
        raise HTTPException(status_code=422, detail="prices for this shop are read automatically")
    now = datetime.now(UTC)
    external = f"product:{prod.product_id}"
    item = await db.scalar(select(CompetitorItem).where(
        CompetitorItem.competitor_id == c.competitor_id, CompetitorItem.external_id == external))
    if item is None:
        item = CompetitorItem(competitor_id=c.competitor_id, external_id=external,
                              name=prod.name[:300], size_value=prod.size_value,
                              size_unit=prod.size_unit)
        db.add(item)
        await db.flush()
    item.last_price, item.last_mrp, item.in_stock = body.price, body.mrp, body.in_stock
    item.last_seen_at = now
    live = await db.scalar(select(ProductMatch).where(
        ProductMatch.product_id == prod.product_id, ProductMatch.competitor_id == c.competitor_id,
        ProductMatch.status.in_(LIVE)))
    if live is None:
        db.add(ProductMatch(product_id=prod.product_id, item_id=item.item_id,
                            competitor_id=c.competitor_id, status="confirmed", method="manual",
                            score=1, decided_by=p.user_id, decided_at=now))
    elif live.item_id != item.item_id:
        raise HTTPException(status_code=409, detail="this product is matched to another item "
                                                    "at this shop")
    db.add(PriceObservation(item_id=item.item_id, price=body.price, mrp=body.mrp,
                            in_stock=body.in_stock, source="manual", entered_by=p.user_id,
                            note=body.note, seen_at=now))
    await audit.record(db, user_id=p.user_id, action="price.manual", entity="product",
                       entity_id=prod.product_id,
                       after={"shop": c.name, "price": str(body.price), "note": body.note},
                       ip_address=client_ip(request))
    return {"item_id": str(item.item_id)}


# ---------------------------------------------------------------- costs
@router.put("/branches/{branch_id}/products/{product_id}/cost", status_code=204)
async def set_cost(branch_id: uuid.UUID, product_id: uuid.UUID, body: CostIn,
                   request: Request, p: Principal = Depends(require("costs.write")),
                   db: AsyncSession = Depends(get_db)):
    if not p.can_see_branch(branch_id):
        raise HTTPException(status_code=403, detail="you do not have access to this branch")
    bp = await db.get(BranchProduct, (branch_id, product_id), with_for_update=True)
    if bp is None:
        raise HTTPException(status_code=404, detail="this branch does not stock this product")
    before = str(bp.cost_price) if bp.cost_price is not None else None
    bp.cost_price, bp.cost_updated_by = body.cost_price, p.user_id
    bp.cost_updated_at = datetime.now(UTC)
    await supersede_for(db, branch_id, product_id)
    await audit.record(db, user_id=p.user_id, action="cost.set", entity="branch_product",
                       entity_id=f"{branch_id}:{product_id}", before={"cost_price": before},
                       after={"cost_price": str(body.cost_price)}, ip_address=client_ip(request))


@router.get("/matches/stats")
async def match_stats(_: Principal = Depends(require("matches.read")),
                      db: AsyncSession = Depends(get_db)):
    rows = await db.execute(select(ProductMatch.competitor_id, ProductMatch.status, func.count())
                            .group_by(ProductMatch.competitor_id, ProductMatch.status))
    out: dict = {}
    for cid, status, n in rows:
        out.setdefault(cid, {})[status] = n
    return out
