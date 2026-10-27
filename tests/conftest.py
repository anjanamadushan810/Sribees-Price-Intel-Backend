"""Database tests run against a real, throwaway PostgreSQL database, because the rules
that matter (one live match, one pending suggestion, append-only history) live in
constraints and triggers that no fake can reproduce.

Admin connection: PRICE_INTEL_TEST_ADMIN_URL (postgresql://user:pass@host:port/postgres).
Without it the database tests are skipped; the pure tests still run.
"""

import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

BACKEND = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get("PRICE_INTEL_TEST_ADMIN_URL")
DB_NAME = f"price_intel_test_{secrets.token_hex(4)}"

if ADMIN_URL:
    _base = ADMIN_URL.rsplit("/", 1)[0]
    os.environ.update(
        DATABASE_URL=f"{_base.replace('postgresql://', 'postgresql+asyncpg://')}/{DB_NAME}",
    )
os.environ.update(
    ENVIRONMENT="test",
    JWT_SECRET=secrets.token_hex(32),
    TOTP_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    TWO_FACTOR="false",
    SCRAPE_DELAY_SECONDS="0",
    ONLINE_API_URL="https://online.test",
    ONLINE_ADMIN_EMAIL="price-intel@sribeesonline.com",
    ONLINE_ADMIN_PASSWORD="not-a-real-password",
)
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unused/unused")


def _admin(sql: str) -> None:
    import psycopg

    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql)


@pytest.fixture(scope="session")
def database():
    if not ADMIN_URL:
        pytest.skip("set PRICE_INTEL_TEST_ADMIN_URL to run database tests")
    _admin(f'CREATE DATABASE "{DB_NAME}"')
    try:
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                       cwd=BACKEND, check=True, env=os.environ.copy())
        yield
    finally:
        _admin(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
