"""Request dependencies: the signed-in user, permission checks, branch scope."""

import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.db import get_db
from app.core.permissions import ALL_BRANCH_ROLES, has_permission
from app.models import User, UserSession

_bearer = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    user: User
    session_id: uuid.UUID

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.user_id

    @property
    def role(self) -> str:
        return self.user.role

    def branch_scope(self) -> set[uuid.UUID] | None:
        """None = every branch; otherwise the branches this user may see."""
        if self.user.role in ALL_BRANCH_ROLES or self.user.all_branches:
            return None
        return {b.branch_id for b in self.user.branches}

    def can_see_branch(self, branch_id: uuid.UUID) -> bool:
        scope = self.branch_scope()
        return scope is None or branch_id in scope


def client_ip(request: Request) -> str | None:
    # Caddy sets X-Forwarded-For; the app only listens on the internal network.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Caddy APPENDS the real peer; earlier entries come from the client and can be
        # forged, so take the LAST one (same fix as Sribees Supply, 2026-11-12).
        return forwarded.split(",")[-1].strip()[:64]
    return request.client.host if request.client else None


async def current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    unauthorised = HTTPException(
        status_code=401, detail="sign in required", headers={"WWW-Authenticate": "Bearer"}
    )
    if creds is None:
        raise unauthorised
    try:
        claims = security.read_token(creds.credentials, security.ACCESS)
        user_id = uuid.UUID(claims["sub"])
        session_id = uuid.UUID(claims["sid"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise unauthorised from None

    session = await db.get(UserSession, session_id)
    if session is None or session.revoked_at is not None or session.user_id != user_id:
        raise unauthorised
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorised
    return Principal(user=user, session_id=session_id)


def require(permission: str):
    async def checker(principal: Principal = Depends(current_principal)) -> Principal:
        if not has_permission(principal.role, permission):
            raise HTTPException(status_code=403, detail="you do not have access to this")
        return principal

    return checker


def scoped_branches(principal: Principal, branch_id: uuid.UUID | None) -> set[uuid.UUID] | None:
    """Branch filter for a report: the requested branch (if allowed) within the user's scope."""
    scope = principal.branch_scope()
    if branch_id is not None:
        if not principal.can_see_branch(branch_id):
            raise HTTPException(status_code=403, detail="you do not have access to this branch")
        return {branch_id}
    return scope
