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
    # Two-step sign-in. Off only on a development server with no real data.
    two_factor: bool = True

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

    @model_validator(mode="after")
    def _production_keeps_two_factor(self) -> "Settings":
        if self.environment == "production" and not self.two_factor:
            raise ValueError("TWO_FACTOR cannot be off in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
