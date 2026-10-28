"""Endpoints for the Sribees Price app (VISION Q16): a manager scans a product and says
yes / no to each shop's candidate.

The app already holds a SRIBEESonline admin token, so it signs in with that: we ask
Online who the token belongs to (cached for a few minutes) and act as a Price Intel
user with the same email, created on first use with the checker role.
"""

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip
from app.api.schemas import ItemOut
from app.core.config import get_settings
from app.core.db import get_db
from app.core.security import hash_password
from app.models import Competitor, Product, ProductMatch, User
from app.services import audit, matches, schedule
from app.services.suggest import pricing_settings

router = APIRouter(prefix="/app", tags=["price app"])
_bearer = HTTPBearer(auto_error=False)

# Online roles that may confirm matches from the app.
APP_ROLES = {"super_admin", "branch_manager"}
CACHE_SECONDS = 300
_cache: dict[str, tuple[float, dict]] = {}


@dataclass
class AppUser:
    user_id: uuid.UUID
    email: str
    online_role: str


async def _online_identity(token: str) -> dict | None:
    key = hashlib.sha256(token.encode()).hexdigest()
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    settings = get_settings()
    if not settings.online_api_url:
        return None
    async with httpx.AsyncClient(base_url=settings.online_api_url.rstrip("/"), timeout=15,
                                 transport=_transport) as client:
        resp = await client.get("/api/v1/admin/auth/profile",
                                headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200:
        return None
    admin = resp.json().get("admin") or {}
    identity = {
        "email": (admin.get("email") or "").lower(),
        "name": admin.get("fullName") or admin.get("full_name") or "",
        "role": admin.get("role") or "",
        "active": admin.get("isActive", admin.get("is_active", True)),
    }
    if not identity["email"] or not identity["active"]:
        return None
    _cache[key] = (time.monotonic() + CACHE_SECONDS, identity)
    return identity


# Tests replace this with an httpx.MockTransport.
_transport: httpx.AsyncBaseTransport | None = None


async def app_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                   db: AsyncSession = Depends(get_db)) -> AppUser:
    if creds is None:
        raise HTTPException(status_code=401, detail="sign in required")
    identity = await _online_identity(creds.credentials)
    if identity is None:
        raise HTTPException(status_code=401, detail="sign in again")
    if identity["role"] not in APP_ROLES:
        raise HTTPException(status_code=403, detail="only managers can confirm matches")
    user = await db.scalar(select(User).where(func.lower(User.email) == identity["email"]))
    if user is None:
        user = User(email=identity["email"], full_name=identity["name"][:150] or identity["email"],
                    role="price_checker", password_hash=hash_password(secrets.token_urlsafe(32)))
        db.add(user)
        await db.flush()
        await audit.record(db, user_id=None, action="user.create", entity="user",
                           entity_id=user.user_id,
                           after={"email": user.email, "role": "price_checker",
                                  "via": "price app (Online sign-in)"})
    elif not user.is_active:
        raise HTTPException(status_code=403, detail="your Price Intel account is disabled")
    return AppUser(user_id=user.user_id, email=user.email, online_role=identity["role"])


class AppCandidate(BaseModel):
    match_id: uuid.UUID
    score: float
    item: ItemOut


class AppShop(BaseModel):
    competitor_id: int
    competitor: str
    matched: AppCandidate | None
    matched_by: str | None  # auto | confirmed
    fresh: bool
    candidates: list[AppCandidate]


class AppProduct(BaseModel):
    product_id: uuid.UUID
    name: str
    shops: list[AppShop]


async def _product_view(db: AsyncSession, product_id: uuid.UUID) -> AppProduct:
    prod = await db.get(Product, product_id)
    if prod is None:
        raise HTTPException(status_code=404, detail="this product is not in Price Intel yet")
    stale = (await pricing_settings(db))["stale_days"]
    now = datetime.now(UTC)
    shops = {c.competitor_id: c for c in await db.scalars(
        select(Competitor).where(Competitor.is_enabled.is_(True))
        .order_by(Competitor.competitor_id))}
    rows = list(await db.scalars(select(ProductMatch).where(
        ProductMatch.product_id == product_id,
        ProductMatch.status.in_(("auto", "confirmed", "candidate")))
        .order_by(ProductMatch.score.desc())))
    out = []
    for cid, c in shops.items():
        mine = [m for m in rows if m.competitor_id == cid]
        live = next((m for m in mine if m.status in matches.LIVE), None)
        if live is None and not mine and c.kind == "manual":
            continue  # nothing to show or decide for a hand-entered shop yet
        fresh = bool(live and live.item.last_price is not None and live.item.in_stock
                     and schedule.is_fresh(prod.review_level, live.item.last_seen_at, now, stale))
        out.append(AppShop(
            competitor_id=cid, competitor=c.name,
            matched=AppCandidate(match_id=live.match_id, score=float(live.score),
                                 item=ItemOut.model_validate(live.item)) if live else None,
            matched_by=live.status if live else None, fresh=fresh,
            candidates=[] if live else [
                AppCandidate(match_id=m.match_id, score=float(m.score),
                             item=ItemOut.model_validate(m.item))
                for m in mine if m.status == "candidate"],
        ))
    return AppProduct(product_id=prod.product_id, name=prod.name, shops=out)


@router.get("/products/{product_id}/shops", response_model=AppProduct)
async def product_shops(product_id: uuid.UUID, _: AppUser = Depends(app_user),
                        db: AsyncSession = Depends(get_db)):
    return await _product_view(db, product_id)


@router.post("/matches/{match_id}/confirm", response_model=AppProduct)
async def app_confirm(match_id: uuid.UUID, request: Request, u: AppUser = Depends(app_user),
                      db: AsyncSession = Depends(get_db)):
    m = await matches.confirm(db, match_id, u.user_id, client_ip(request))
    await db.flush()
    return await _product_view(db, m.product_id)


@router.post("/matches/{match_id}/reject", response_model=AppProduct)
async def app_reject(match_id: uuid.UUID, request: Request, u: AppUser = Depends(app_user),
                     db: AsyncSession = Depends(get_db)):
    m = await matches.reject(db, match_id, u.user_id, client_ip(request))
    await db.flush()
    return await _product_view(db, m.product_id)
