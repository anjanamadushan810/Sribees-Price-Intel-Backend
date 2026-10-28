"""Settings, read from the environment (or a .env file beside the app in development)."""

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = Field(default="development", pattern="^(development|test|production)$")
    database_url: str
    # Signs access tokens. 32+ random bytes; rotate by deploying a new value.
    jwt_secret: str = Field(min_length=32)
    # Fernet key (urlsafe base64, 32 bytes) that encrypts TOTP secrets at rest.
    totp_encryption_key: str
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    cors_origins: list[str] = []
    totp_issuer: str = "Sribees Price Intel"
    # Two-step sign-in. Off only on a development server with no real data -- or, in
    # production, when the owner chooses it on purpose (2026-10-28: QA signs in often)
    # by also setting ALLOW_PRODUCTION_WITHOUT_2FA=true.
    two_factor: bool = True
    allow_production_without_2fa: bool = False

    # SRIBEESonline admin API, reached with a dedicated admin account that the
    # owner creates for this service (never a person's login).
    online_api_url: str = ""
    online_admin_email: str = ""
    online_admin_password: str = ""

    # Polite scraping (docs/PHASE0_FINDINGS.md).
    scrape_user_agent: str = "SribeesPriceBot/0.1 (+https://sribeesonline.com)"
    scrape_delay_seconds: float = 2.0
    scrape_timeout_seconds: float = 20.0
    scrape_max_pages: int = 400
    # Web Bot Auth (app/adapters/botauth.py): base64 raw Ed25519 private key, and the
    # https origin that serves our public key directory. Both empty = unsigned.
    web_bot_auth_key: str = ""
    signature_agent: str = ""

    @model_validator(mode="after")
    def _production_keeps_two_factor(self) -> "Settings":
        if (self.environment == "production" and not self.two_factor
                and not self.allow_production_without_2fa):
            raise ValueError("TWO_FACTOR cannot be off in production unless "
                             "ALLOW_PRODUCTION_WITHOUT_2FA=true is set on purpose")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
