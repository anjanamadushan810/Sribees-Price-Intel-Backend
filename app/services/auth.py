"""Sign-in: password, then a TOTP code (mandatory for every role but clock-in staff).

Flow
  POST /auth/login         email + password
      -> tokens                      (staff without 2FA)
      -> {totp_required, ticket}     (2FA enrolled)
      -> {totp_setup_required, ticket} (2FA mandatory, not yet enrolled)
  POST /auth/totp          ticket + code         -> tokens
  POST /auth/totp/setup    setup ticket          -> secret + otpauth URI
  POST /auth/totp/enable   setup ticket + code   -> tokens
Wrong passwords and wrong codes count towards the same lockout.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import get_settings
from app.core.permissions import TOTP_REQUIRED_ROLES
from app.models import User, UserSession
from app.services import audit
from app.services.errors import IntelError

TICKET_MINUTES = 5


class AuthError(IntelError):
    status_code = 401


class Locked(IntelError):
    status_code = 423


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_in: int


def _now() -> datetime:
    return datetime.now(UTC)


async def _register_failure(db: AsyncSession, user: User, ip: str | None, what: str) -> None:
    settings = get_settings()
    user.failed_logins += 1
    if user.failed_logins >= settings.max_failed_logins:
        user.locked_until = _now() + timedelta(minutes=settings.lockout_minutes)
        user.failed_logins = 0
    await audit.record(
        db, user_id=user.user_id, action=f"auth.{what}_failed", entity="user",
        entity_id=user.user_id, ip_address=ip,
    )


def _check_usable(user: User | None) -> User:
    if user is None or not user.is_active:
        raise AuthError("email or password is wrong")
    if user.locked_until and user.locked_until > _now():
        raise Locked("too many failed attempts; try again later")
    return user


async def _issue(db: AsyncSession, user: User, ip: str | None, user_agent: str | None) -> Tokens:
    settings = get_settings()
    refresh, refresh_hash = security.new_refresh_token()
    session = UserSession(
        user_id=user.user_id,
        refresh_hash=refresh_hash,
        ip_address=ip,
        user_agent=(user_agent or "")[:300] or None,
        expires_at=_now() + timedelta(days=settings.refresh_token_days),
    )
    db.add(session)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = _now()
    await db.flush()
    await audit.record(
        db, user_id=user.user_id, action="auth.login", entity="user", entity_id=user.user_id,
        ip_address=ip,
    )
    return Tokens(
        access_token=security.make_token(
            user.user_id, security.ACCESS, settings.access_token_minutes, session.session_id
        ),
        refresh_token=refresh,
        expires_in=settings.access_token_minutes * 60,
    )


async def login(
    db: AsyncSession, *, email: str, password: str, ip: str | None, user_agent: str | None
) -> Tokens | dict:
    user = await db.scalar(
        select(User).where(func.lower(User.email) == email.strip().lower()).with_for_update()
    )
    ok = security.verify_password(user.password_hash if user else None, password)
    user = _check_usable(user)
    if not ok:
        await _register_failure(db, user, ip, "password")
        raise AuthError("email or password is wrong")

    if not get_settings().two_factor:
        return await _issue(db, user, ip, user_agent)
    if user.totp_enabled:
        return {
            "status": "totp_required",
            "ticket": security.make_token(user.user_id, security.MFA, TICKET_MINUTES),
        }
    if user.role in TOTP_REQUIRED_ROLES:
        return {
            "status": "totp_setup_required",
            "ticket": security.make_token(user.user_id, security.SETUP, TICKET_MINUTES),
        }
    return await _issue(db, user, ip, user_agent)


async def _user_from_ticket(db: AsyncSession, ticket: str, typ: str) -> User:
    try:
        claims = security.read_token(ticket, typ)
    except Exception as exc:
        raise AuthError("sign-in step expired; start again") from exc
    user = await db.get(User, uuid.UUID(claims["sub"]), with_for_update=True)
    return _check_usable(user)


async def verify_totp_login(
    db: AsyncSession, *, ticket: str, code: str, ip: str | None, user_agent: str | None
) -> Tokens:
    user = await _user_from_ticket(db, ticket, security.MFA)
    if not user.totp_enabled or not user.totp_secret_enc:
        raise AuthError("two-factor sign-in is not set up")
    if not security.verify_totp(security.decrypt_secret(user.totp_secret_enc), code):
        await _register_failure(db, user, ip, "totp")
        raise AuthError("the code is wrong or expired")
    return await _issue(db, user, ip, user_agent)


async def start_totp_setup(db: AsyncSession, *, ticket: str) -> dict:
    user = await _user_from_ticket(db, ticket, security.SETUP)
    if user.totp_enabled:
        raise AuthError("two-factor sign-in is already set up")
    secret = security.new_totp_secret()
    user.totp_secret_enc = security.encrypt_secret(secret)
    await db.flush()
    return {"secret": secret, "otpauth_uri": security.totp_uri(secret, user.email)}


async def enable_totp(
    db: AsyncSession, *, ticket: str, code: str, ip: str | None, user_agent: str | None
) -> Tokens:
    user = await _user_from_ticket(db, ticket, security.SETUP)
    if user.totp_enabled or not user.totp_secret_enc:
        raise AuthError("start two-factor set-up first")
    if not security.verify_totp(security.decrypt_secret(user.totp_secret_enc), code):
        await _register_failure(db, user, ip, "totp")
        raise AuthError("the code is wrong or expired")
    user.totp_enabled = True
    await audit.record(
        db, user_id=user.user_id, action="auth.totp_enabled", entity="user",
        entity_id=user.user_id, ip_address=ip,
    )
    return await _issue(db, user, ip, user_agent)


async def refresh(db: AsyncSession, *, refresh_token: str) -> Tokens:
    """Rotate: the old refresh token stops working the moment a new one is issued."""
    settings = get_settings()
    session = await db.scalar(
        select(UserSession)
        .where(UserSession.refresh_hash == security.hash_refresh_token(refresh_token))
        .with_for_update()
    )
    if session is None or session.revoked_at is not None or session.expires_at <= _now():
        raise AuthError("session expired; sign in again")
    user = await db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise AuthError("session expired; sign in again")
    new_token, new_hash = security.new_refresh_token()
    session.refresh_hash = new_hash
    session.last_used_at = _now()
    await db.flush()
    return Tokens(
        access_token=security.make_token(
            user.user_id, security.ACCESS, settings.access_token_minutes, session.session_id
        ),
        refresh_token=new_token,
        expires_in=settings.access_token_minutes * 60,
    )


async def revoke_session(db: AsyncSession, *, session_id: uuid.UUID, user_id: uuid.UUID) -> None:
    session = await db.get(UserSession, session_id)
    if session is not None and session.user_id == user_id and session.revoked_at is None:
        session.revoked_at = _now()
        await db.flush()
