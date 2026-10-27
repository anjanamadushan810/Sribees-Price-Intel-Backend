import uuid

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, client_ip, current_principal
from app.api.schemas import (
    LoginIn,
    MeOut,
    RefreshIn,
    SessionOut,
    StepOut,
    TicketCodeIn,
    TicketIn,
    TokensOut,
    TotpSetupOut,
    UserOut,
)
from app.core.config import get_settings
from app.core.db import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.models import UserSession
from app.services import auth
from app.services.errors import IntelError

router = APIRouter(prefix="/auth", tags=["auth"])


async def _keep_failure(db: AsyncSession, call):
    """A failed sign-in still has to count towards the lockout, so commit the
    failure counter and its audit row before the error ends the request."""
    try:
        return await call
    except IntelError:
        await db.commit()
        raise


# The web app keeps the refresh token only in this httpOnly cookie, out of
# JavaScript's reach. API clients may use the refresh_token in the body instead.
REFRESH_COOKIE = "pi_refresh"


def _tokens(t: auth.Tokens, response: Response) -> TokensOut:
    settings = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        t.refresh_token,
        max_age=settings.refresh_token_days * 86400,
        path="/",
        httponly=True,
        secure=settings.environment == "production",
        samesite="strict",
    )
    return TokensOut(access_token=t.access_token, refresh_token=t.refresh_token,
                     expires_in=t.expires_in)


@router.post("/login", response_model=TokensOut | StepOut)
async def login(body: LoginIn, request: Request, response: Response,
                db: AsyncSession = Depends(get_db)):
    result = await _keep_failure(db, auth.login(
        db, email=body.email, password=body.password, ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    ))
    return StepOut(**result) if isinstance(result, dict) else _tokens(result, response)


@router.post("/totp", response_model=TokensOut)
async def totp(body: TicketCodeIn, request: Request, response: Response,
               db: AsyncSession = Depends(get_db)):
    return _tokens(await _keep_failure(db, auth.verify_totp_login(
        db, ticket=body.ticket, code=body.code, ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )), response)


@router.post("/totp/setup", response_model=TotpSetupOut)
async def totp_setup(body: TicketIn, db: AsyncSession = Depends(get_db)):
    return TotpSetupOut(**await auth.start_totp_setup(db, ticket=body.ticket))


@router.post("/totp/enable", response_model=TokensOut)
async def totp_enable(body: TicketCodeIn, request: Request, response: Response,
                      db: AsyncSession = Depends(get_db)):
    return _tokens(await _keep_failure(db, auth.enable_totp(
        db, ticket=body.ticket, code=body.code, ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )), response)


@router.post("/refresh", response_model=TokensOut)
async def refresh(
    response: Response,
    body: RefreshIn | None = None,
    pi_refresh: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
):
    token = body.refresh_token if body else pi_refresh
    if not token:
        raise HTTPException(status_code=401, detail="session expired; sign in again")
    return _tokens(await auth.refresh(db, refresh_token=token), response)


@router.post("/logout", status_code=204)
async def logout(response: Response, p: Principal = Depends(current_principal),
                 db: AsyncSession = Depends(get_db)):
    await auth.revoke_session(db, session_id=p.session_id, user_id=p.user_id)
    response.delete_cookie(REFRESH_COOKIE, path="/")


@router.get("/me", response_model=MeOut)
async def me(p: Principal = Depends(current_principal)):
    user = UserOut.model_validate(p.user).model_dump()
    return MeOut(**user, permissions=sorted(ROLE_PERMISSIONS.get(p.role, ())),
                 two_factor=get_settings().two_factor)


@router.get("/sessions", response_model=list[SessionOut])
async def sessions(p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    rows = await db.scalars(
        select(UserSession)
        .where(UserSession.user_id == p.user_id, UserSession.revoked_at.is_(None))
        .order_by(UserSession.last_used_at.desc())
    )
    out = []
    for s in rows:
        item = SessionOut.model_validate(s)
        item.current = s.session_id == p.session_id
        out.append(item)
    return out


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke(
    session_id: uuid.UUID,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(UserSession, session_id)
    if session is None or session.user_id != p.user_id:
        raise HTTPException(status_code=404, detail="session not found")
    await auth.revoke_session(db, session_id=session_id, user_id=p.user_id)
