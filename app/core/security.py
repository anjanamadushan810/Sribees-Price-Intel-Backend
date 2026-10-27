"""Passwords (argon2), TOTP (secrets encrypted at rest), access tokens (JWT), refresh tokens."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet

from app.core.config import get_settings

_hasher = PasswordHasher()
# Verified against when the email is unknown, so a miss costs the same time as a hit.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def _fernet() -> Fernet:
    return Fernet(get_settings().totp_encryption_key.encode())


def new_totp_secret() -> str:
    return pyotp.random_base32()


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=get_settings().totp_issuer)


def verify_totp(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    return code.isdigit() and pyotp.TOTP(secret).verify(code, valid_window=1)


# Token types. Only "access" reaches the API; "mfa" and "setup" are short-lived
# tickets for the second login step and for first-time 2FA enrolment.
ACCESS, MFA, SETUP = "access", "mfa", "setup"


def make_token(
    user_id: uuid.UUID, typ: str, minutes: int, session_id: uuid.UUID | None = None
) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "typ": typ,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
        "jti": secrets.token_hex(8),
    }
    if session_id:
        claims["sid"] = str(session_id)
    return jwt.encode(claims, get_settings().jwt_secret, algorithm="HS256")


def read_token(token: str, typ: str) -> dict:
    """Decode and check type. Raises jwt.InvalidTokenError on anything wrong."""
    claims = jwt.decode(
        token,
        get_settings().jwt_secret,
        algorithms=["HS256"],
        options={"require": ["sub", "typ", "exp"]},
    )
    if claims["typ"] != typ:
        raise jwt.InvalidTokenError("wrong token type")
    return claims


def new_refresh_token() -> tuple[str, str]:
    """(token for the client, sha256 hex stored in the database)."""
    token = secrets.token_urlsafe(32)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
