"""Branches, users, pricing settings and the audit trail."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, client_ip, require
from app.api.schemas import AuditOut, BranchOut, PricingSettings, UserIn, UserOut, UserPatch
from app.core.db import get_db
from app.core.security import hash_password
from app.models import AuditLog, Branch, BranchProduct, Setting, User, UserSession
from app.services import audit
from app.services.suggest import pricing_settings

router = APIRouter(tags=["admin"])


# ---------------------------------------------------------------- branches (synced from Online)
@router.get("/branches", response_model=list[BranchOut])
async def list_branches(
    p: Principal = Depends(require("branches.read")), db: AsyncSession = Depends(get_db)
):
    stocked = (select(func.count()).where(BranchProduct.branch_id == Branch.branch_id,
                                          BranchProduct.is_active.is_(True))
               .correlate(Branch).scalar_subquery())
    q = select(Branch, stocked).order_by(stocked.desc(), Branch.name)
    scope = p.branch_scope()
    if scope is not None:
        q = q.where(Branch.branch_id.in_(scope))
    return [BranchOut(branch_id=b.branch_id, name=b.name, is_active=b.is_active, products=n)
            for b, n in (await db.execute(q)).all()]


# ---------------------------------------------------------------- pricing rule
@router.get("/settings/pricing", response_model=PricingSettings)
async def get_pricing(_: Principal = Depends(require("settings.read")),
                      db: AsyncSession = Depends(get_db)):
    return await pricing_settings(db)


@router.put("/settings/pricing", response_model=PricingSettings)
async def put_pricing(
    body: PricingSettings,
    request: Request,
    p: Principal = Depends(require("settings.write")),
    db: AsyncSession = Depends(get_db),
):
    before = await pricing_settings(db)
    value = body.model_dump(mode="json")
    value["min_margin_pct"] = float(body.min_margin_pct)
    row = await db.get(Setting, "pricing", with_for_update=True)
    if row is None:
        db.add(Setting(key="pricing", value=value, updated_by=p.user_id))
    else:
        row.value, row.updated_by, row.updated_at = value, p.user_id, datetime.now(UTC)
    await audit.record(db, user_id=p.user_id, action="settings.pricing", entity="setting",
                       entity_id="pricing", before=before, after=value,
                       ip_address=client_ip(request))
    return value


# ---------------------------------------------------------------- users
async def _branches(db: AsyncSession, ids: list[uuid.UUID]) -> list[Branch]:
    if not ids:
        return []
    found = list(await db.scalars(select(Branch).where(Branch.branch_id.in_(ids))))
    if len(found) != len(set(ids)):
        raise HTTPException(status_code=422, detail="unknown branch in branch_ids")
    return found


async def _revoke_all(db: AsyncSession, user_id: uuid.UUID) -> None:
    await db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


def _snapshot(user: User) -> dict:
    data = UserOut.model_validate(user).model_dump(mode="json")
    data["branches"] = [b["name"] for b in data["branches"]]
    return data


@router.get("/users", response_model=list[UserOut])
async def list_users(_: Principal = Depends(require("users.read")),
                     db: AsyncSession = Depends(get_db)):
    return list(await db.scalars(select(User).order_by(User.full_name)))


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    body: UserIn,
    request: Request,
    p: Principal = Depends(require("users.write")),
    db: AsyncSession = Depends(get_db),
):
    if await db.scalar(select(User).where(func.lower(User.email) == body.email.lower())):
        raise HTTPException(status_code=409, detail="a user with this email exists")
    if body.role == "branch_manager" and not body.all_branches and not body.branch_ids:
        raise HTTPException(
            status_code=422,
            detail="a branch manager must have at least one branch or all_branches enabled",
        )
    user = User(
        email=body.email.lower(),
        full_name=body.full_name,
        role=body.role,
        password_hash=hash_password(body.password),
        all_branches=body.all_branches,
        branches=[] if body.all_branches else await _branches(db, body.branch_ids),
    )
    db.add(user)
    await db.flush()
    await audit.record(db, user_id=p.user_id, action="user.create", entity="user",
                       entity_id=user.user_id, after=_snapshot(user), ip_address=client_ip(request))
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    body: UserPatch,
    request: Request,
    p: Principal = Depends(require("users.write")),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id, with_for_update=True)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    before = _snapshot(user)
    changes = body.model_dump(exclude_unset=True)

    losing_owner = user.role == "super_admin" and (
        changes.get("role", "super_admin") != "super_admin" or changes.get("is_active") is False
    )
    if losing_owner:
        owners = await db.scalar(
            select(func.count()).select_from(User).where(User.role == "super_admin", User.is_active)
        )
        if owners <= 1:
            raise HTTPException(status_code=422,
                                detail="there must always be an active super admin")

    target_role = changes.get("role", user.role)
    target_all_branches = changes.get("all_branches", user.all_branches)
    target_branch_ids = (
        body.branch_ids
        if body.branch_ids is not None
        else [b.branch_id for b in user.branches]
    )

    if target_role == "branch_manager" and not target_all_branches and not target_branch_ids:
        raise HTTPException(
            status_code=422,
            detail="a branch manager must have at least one branch or all_branches enabled",
        )

    for field in ("full_name", "role", "is_active", "all_branches"):
        if field in changes:
            setattr(user, field, changes[field])
    if body.branch_ids is not None or user.all_branches:
        user.branches = [] if user.all_branches else await _branches(db, body.branch_ids or [])
    must_sign_in_again = False
    if body.password:
        user.password_hash = hash_password(body.password)
        must_sign_in_again = True
    if body.reset_totp:
        user.totp_enabled = False
        user.totp_secret_enc = None
        must_sign_in_again = True
    if changes.get("is_active") is False or "role" in changes:
        must_sign_in_again = True
    if must_sign_in_again:
        await _revoke_all(db, user.user_id)
    user.updated_at = datetime.now(UTC)
    await db.flush()
    after = _snapshot(user)
    if body.password:
        after["password"] = "changed"  # noqa: S105 - audit marker, not a secret
    await audit.record(db, user_id=p.user_id, action="user.update", entity="user",
                       entity_id=user_id, before=before, after=after,
                       ip_address=client_ip(request))
    return user


# ---------------------------------------------------------------- audit
@router.get("/audit", response_model=list[AuditOut])
async def list_audit(
    entity: str | None = None,
    entity_id: str | None = None,
    user_id: uuid.UUID | None = None,
    before_id: int | None = Query(default=None, description="page backwards from this id"),
    limit: int = Query(default=100, le=500),
    _: Principal = Depends(require("audit.read")),
    db: AsyncSession = Depends(get_db),
):
    q = select(AuditLog).order_by(AuditLog.audit_id.desc()).limit(limit)
    if entity:
        q = q.where(AuditLog.entity == entity)
    if entity_id:
        q = q.where(AuditLog.entity_id == entity_id)
    if user_id:
        q = q.where(AuditLog.user_id == user_id)
    if before_id:
        q = q.where(AuditLog.audit_id < before_id)
    return list(await db.scalars(q))
