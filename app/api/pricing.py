"""Suggestions (accept / reject), the summary report and "run now"."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, client_ip, require
from app.api.market import _branch_for, _rows, stocked_branches
from app.api.schemas import SuggestionOut, SummaryOut
from app.core.db import get_db, get_sessionmaker
from app.models import Branch, Competitor, Product, ProductMatch, Suggestion
from app.services import apply, audit, jobs
from app.services.online_client import OnlineClient

router = APIRouter(tags=["pricing"])
_running: set[asyncio.Task] = set()


def _out(s: Suggestion, branch: str, product: Product, competitor: str) -> SuggestionOut:
    return SuggestionOut(
        suggestion_id=s.suggestion_id, branch_id=s.branch_id, branch=branch,
        product_id=s.product_id, product_name=product.name, sku=product.sku,
        current_price=s.current_price, suggested_price=s.suggested_price,
        cheapest_price=s.cheapest_price, cheapest_competitor=competitor,
        cost_price=s.cost_price, floor_price=s.floor_price, reason=s.reason, status=s.status,
        created_at=s.created_at, decided_at=s.decided_at, applied_at=s.applied_at,
        apply_error=s.apply_error,
    )


@router.get("/suggestions", response_model=list[SuggestionOut])
async def list_suggestions(
    branch_id: uuid.UUID | None = None,
    status: str = Query(default="pending",
                        pattern="^(pending|accepted|rejected|superseded|failed|applied)$"),
    limit: int = Query(default=100, le=500),
    p: Principal = Depends(require("suggestions.read")),
    db: AsyncSession = Depends(get_db),
):
    stmt = (select(Suggestion, Branch.name, Product, Competitor.name)
            .join(Branch, Branch.branch_id == Suggestion.branch_id)
            .join(Product, Product.product_id == Suggestion.product_id)
            .join(Competitor, Competitor.competitor_id == Suggestion.cheapest_competitor_id)
            .where(Suggestion.status == status)
            .order_by((Suggestion.current_price - Suggestion.suggested_price).desc())
            .limit(limit))
    if branch_id is not None:
        if not p.can_see_branch(branch_id):
            raise HTTPException(status_code=403, detail="you do not have access to this branch")
        stmt = stmt.where(Suggestion.branch_id == branch_id)
    elif (scope := p.branch_scope()) is not None:
        stmt = stmt.where(Suggestion.branch_id.in_(scope))
    return [_out(*row) for row in (await db.execute(stmt)).all()]


async def _check_branch(db: AsyncSession, p: Principal, suggestion_id: uuid.UUID) -> None:
    branch_id = await db.scalar(select(Suggestion.branch_id).where(
        Suggestion.suggestion_id == suggestion_id))
    if branch_id is None:
        raise HTTPException(status_code=404, detail="suggestion not found")
    if not p.can_see_branch(branch_id):
        raise HTTPException(status_code=403, detail="you do not have access to this branch")


async def _one(db: AsyncSession, suggestion_id: uuid.UUID) -> SuggestionOut:
    row = (await db.execute(
        select(Suggestion, Branch.name, Product, Competitor.name)
        .join(Branch, Branch.branch_id == Suggestion.branch_id)
        .join(Product, Product.product_id == Suggestion.product_id)
        .join(Competitor, Competitor.competitor_id == Suggestion.cheapest_competitor_id)
        .where(Suggestion.suggestion_id == suggestion_id))).one()
    return _out(*row)


@router.post("/suggestions/{suggestion_id}/accept", response_model=SuggestionOut)
async def accept(suggestion_id: uuid.UUID, request: Request,
                 p: Principal = Depends(require("suggestions.decide")),
                 db: AsyncSession = Depends(get_db)):
    await _check_branch(db, p, suggestion_id)
    async with OnlineClient() as online:
        await apply.accept(db, online, suggestion_id, p.user_id, client_ip(request))
    await db.flush()
    return await _one(db, suggestion_id)


@router.post("/suggestions/{suggestion_id}/reject", response_model=SuggestionOut)
async def reject(suggestion_id: uuid.UUID, request: Request,
                 p: Principal = Depends(require("suggestions.decide")),
                 db: AsyncSession = Depends(get_db)):
    await _check_branch(db, p, suggestion_id)
    await apply.reject(db, suggestion_id, p.user_id, client_ip(request))
    await db.flush()
    return await _one(db, suggestion_id)


@router.get("/reports/summary", response_model=SummaryOut)
async def summary(branch_id: uuid.UUID | None = None,
                  p: Principal = Depends(require("reports.read")),
                  db: AsyncSession = Depends(get_db)):
    branches = await stocked_branches(db)
    branch = _branch_for(p, branch_id, branches)
    rows = await _rows(db, branch, None)
    count = {k: sum(1 for r in rows if r.position == k)
             for k in ("cheaper", "equal", "dearer", "unknown")}
    return SummaryOut(
        products=len(rows),
        tracked=sum(1 for r in rows if r.prices),
        cost_missing=sum(1 for r in rows if r.cost_price is None and r.our_price is not None),
        pending_suggestions=await db.scalar(select(func.count()).select_from(Suggestion).where(
            Suggestion.branch_id == branch, Suggestion.status == "pending")),
        match_queue=await db.scalar(select(func.count()).select_from(
            select(ProductMatch.product_id, ProductMatch.competitor_id)
            .where(ProductMatch.status == "candidate")
            .group_by(ProductMatch.product_id, ProductMatch.competitor_id).subquery())),
        sources_down=list(await db.scalars(select(Competitor.name).where(
            Competitor.is_enabled.is_(True), Competitor.status.in_(("failing", "blocked"))))),
        **count,
    )


@router.post("/jobs/daily", status_code=202)
async def run_now(request: Request, sync: bool = True, competitor_id: int | None = None,
                  p: Principal = Depends(require("scrape.run")),
                  db: AsyncSession = Depends(get_db)):
    """Starts the daily job in the background; the runs list shows how it went.

    With competitor_id only that shop is read (the Sources table's per-row Run button).
    """
    if competitor_id is not None:
        c = await db.get(Competitor, competitor_id)
        if c is None or c.kind != "scraped":
            raise HTTPException(status_code=404, detail="not an automatic source")
        if not c.is_enabled:
            raise HTTPException(status_code=409, detail="this source is switched off")
        if await jobs.busy(db):
            raise HTTPException(status_code=409,
                                detail="a reading is already running; try again when it ends")
        await audit.record(db, user_id=p.user_id, action="competitor.run", entity="competitor",
                           entity_id=competitor_id, ip_address=client_ip(request))
        await db.commit()
    task = asyncio.create_task(jobs.daily(
        get_sessionmaker(), sync=sync,
        competitor_ids=[competitor_id] if competitor_id else None))
    _running.add(task)
    task.add_done_callback(_running.discard)
    return {"status": "started"}
