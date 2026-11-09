"""Competitor assortment gap analysis and product discovery endpoints.

Shows products sold by competitors that Sribees does not stock or match yet.
"""

import math
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require
from app.api.schemas import (
    AssortmentItemOut,
    AssortmentPage,
    AssortmentStatsOut,
    CrawlIn,
    CrawlStatusOut,
)
from app.core.db import get_db, get_sessionmaker
from app.models import Competitor, CompetitorItem, ProductMatch
from app.services import discovery

router = APIRouter(prefix="/assortment", tags=["assortment"])

LIVE_STATUSES = ("auto", "confirmed")


def _unmatched_condition():
    """Items in competitor_items that are not in a live product match."""
    matched_exists = (
        select(1)
        .where(
            ProductMatch.item_id == CompetitorItem.item_id,
            ProductMatch.status.in_(LIVE_STATUSES),
        )
        .exists()
    )
    return ~matched_exists


@router.get("/gaps", response_model=AssortmentPage)
async def list_gaps(
    competitor_id: int | None = None,
    q: str | None = None,
    in_stock_only: bool = False,
    sort: str = "newest",
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    _: Principal = Depends(require("products.read")),
    db: AsyncSession = Depends(get_db),
):
    """List competitor items that Sribees does not stock or match."""
    base_filter = _unmatched_condition()

    stmt = (
        select(CompetitorItem, Competitor.name.label("competitor_name"))
        .join(Competitor, Competitor.competitor_id == CompetitorItem.competitor_id)
        .where(base_filter)
    )

    if competitor_id is not None:
        stmt = stmt.where(CompetitorItem.competitor_id == competitor_id)

    if in_stock_only:
        stmt = stmt.where(CompetitorItem.in_stock.is_(True))

    if q and q.strip():
        for term in q.strip().split():
            pattern = f"%{term}%"
            stmt = stmt.where(
                or_(
                    CompetitorItem.name.ilike(pattern, escape="\\"),
                    CompetitorItem.brand.ilike(pattern, escape="\\"),
                    CompetitorItem.barcode.ilike(pattern, escape="\\"),
                )
            )

    # Count total matching rows
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await db.scalar(count_stmt)) or 0

    # Sorting
    if sort == "newest":
        stmt = stmt.order_by(CompetitorItem.first_seen_at.desc(), CompetitorItem.name.asc())
    elif sort == "oldest":
        stmt = stmt.order_by(CompetitorItem.first_seen_at.asc(), CompetitorItem.name.asc())
    elif sort == "price_desc":
        stmt = stmt.order_by(
            CompetitorItem.last_price.desc().nulls_last(), CompetitorItem.name.asc()
        )
    elif sort == "price_asc":
        stmt = stmt.order_by(
            CompetitorItem.last_price.asc().nulls_last(), CompetitorItem.name.asc()
        )
    elif sort == "name":
        stmt = stmt.order_by(CompetitorItem.name.asc())
    else:
        stmt = stmt.order_by(CompetitorItem.first_seen_at.desc(), CompetitorItem.name.asc())

    # Pagination
    offset = (page - 1) * limit
    stmt = stmt.offset(offset).limit(limit)

    rows = (await db.execute(stmt)).all()

    items = [
        AssortmentItemOut(
            item_id=item.item_id,
            competitor_id=item.competitor_id,
            competitor_name=comp_name,
            name=item.name,
            brand=item.brand,
            barcode=item.barcode,
            size_value=item.size_value,
            size_unit=item.size_unit,
            url=item.url,
            image_url=item.image_url,
            last_price=item.last_price,
            last_mrp=item.last_mrp,
            in_stock=item.in_stock,
            first_seen_at=item.first_seen_at,
            last_seen_at=item.last_seen_at,
        )
        for item, comp_name in rows
    ]

    pages = max(1, math.ceil(total / limit))
    return AssortmentPage(items=items, total=total, page=page, pages=pages)


@router.get("/stats", response_model=AssortmentStatsOut)
async def get_stats(
    competitor_id: int | None = None,
    _: Principal = Depends(require("products.read")),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate statistics for competitor assortment gaps."""
    base_filter = _unmatched_condition()
    stmt = select(CompetitorItem).where(base_filter)

    if competitor_id is not None:
        stmt = stmt.where(CompetitorItem.competitor_id == competitor_id)

    week_ago = datetime.now(UTC) - timedelta(days=7)
    stats_query = select(
        func.count(CompetitorItem.item_id).label("total_gaps"),
        func.count(case((CompetitorItem.in_stock.is_(True), 1))).label("in_stock_gaps"),
        func.count(case((CompetitorItem.first_seen_at >= week_ago, 1))).label("new_this_week"),
    ).select_from(stmt.subquery())

    res = (await db.execute(stats_query)).one()
    return AssortmentStatsOut(
        total_gaps=res.total_gaps or 0,
        in_stock_gaps=res.in_stock_gaps or 0,
        new_this_week=res.new_this_week or 0,
    )


@router.post("/crawl")
async def trigger_crawl_all_or_one(
    background_tasks: BackgroundTasks,
    body: CrawlIn | None = None,
    _: Principal = Depends(require("scrape.run")),
    db: AsyncSession = Depends(get_db),
):
    """Trigger an on-demand discovery crawl for all or a specific competitor."""
    target_id = body.competitor_id if body else None
    comp_name = "all competitors"
    if target_id is not None:
        competitor = await db.get(Competitor, target_id)
        if competitor is None:
            raise HTTPException(status_code=404, detail="competitor not found")
        if competitor.kind != "scraped":
            raise HTTPException(status_code=400, detail="only scraped competitors can be crawled")
        comp_name = competitor.name

    if discovery.is_running():
        raise HTTPException(status_code=409, detail="A discovery crawl is already in progress")

    background_tasks.add_task(discovery.run_discovery, get_sessionmaker(), target_id)
    return {"status": "started", "competitor_id": target_id, "competitor": comp_name}


@router.post("/crawl/{competitor_id}")
async def trigger_crawl(
    competitor_id: int,
    background_tasks: BackgroundTasks,
    _: Principal = Depends(require("scrape.run")),
    db: AsyncSession = Depends(get_db),
):
    """Trigger an on-demand discovery crawl for a specific competitor in the background."""
    competitor = await db.get(Competitor, competitor_id)
    if competitor is None:
        raise HTTPException(status_code=404, detail="competitor not found")
    if competitor.kind != "scraped":
        raise HTTPException(status_code=400, detail="only scraped competitors can be crawled")

    if discovery.is_running():
        raise HTTPException(status_code=409, detail="A discovery crawl is already in progress")

    background_tasks.add_task(discovery.run_discovery, get_sessionmaker(), competitor_id)
    return {"status": "started", "competitor_id": competitor_id, "competitor": competitor.name}


@router.get("/crawl/status", response_model=CrawlStatusOut)
async def crawl_status(
    _: Principal = Depends(require("products.read")),
):
    """Get the current state of any active discovery crawl."""
    status_info = discovery.get_status()
    return CrawlStatusOut(**status_info)
