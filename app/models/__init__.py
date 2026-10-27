"""ORM mappings of the tables created in alembic/versions. The migrations are the source of
truth; tests/test_models_match_schema.py fails if a mapping drifts from the database."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


# ---------------------------------------------------------------- people
class Branch(Base):
    """A SRIBEESonline branch; branch_id is Online's own id."""

    __tablename__ = "branches"

    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(150))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    all_branches: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    branches: Mapped[list[Branch]] = relationship(secondary="user_branches", lazy="selectin")


class UserBranch(Base):
    __tablename__ = "user_branches"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), primary_key=True
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.branch_id"), primary_key=True
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.user_id"))
    refresh_hash: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"

    audit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(String(64))


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------- our catalogue (copy of Online)
class Product(Base):
    """A SRIBEESonline product; product_id is Online's id. Refreshed by catalog sync."""

    __tablename__ = "products"

    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    name_si: Mapped[str | None] = mapped_column(String(255))
    sku: Mapped[str | None] = mapped_column(String(100))
    barcode: Mapped[str | None] = mapped_column(String(20))
    category: Mapped[str | None] = mapped_column(String(120))
    size_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    size_unit: Mapped[str | None] = mapped_column(String(4))
    review_level: Mapped[str] = mapped_column(String(10), default="long")
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BranchProduct(Base):
    """What a branch charges today (from Online) and what it paid (typed by its manager)."""

    __tablename__ = "branch_products"

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.branch_id"), primary_key=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.product_id"), primary_key=True
    )
    inventory_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    branch_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    global_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    discount_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    is_on_sale: Mapped[bool] = mapped_column(Boolean, default=False)
    selling_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    cost_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    cost_updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    cost_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------- the market
class Competitor(Base):
    __tablename__ = "competitors"

    competitor_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(80))
    # scraped: an adapter reads the site; manual: the Price Checker types prices.
    kind: Mapped[str] = mapped_column(String(10))
    adapter: Mapped[str | None] = mapped_column(String(20))
    base_url: Mapped[str | None] = mapped_column(String(200))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(12), default="ok")
    notes: Mapped[str | None] = mapped_column(Text)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CompetitorItem(Base):
    """One product as a competitor lists it, with its latest price."""

    __tablename__ = "competitor_items"

    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    competitor_id: Mapped[int] = mapped_column(Integer, ForeignKey("competitors.competitor_id"))
    external_id: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(300))
    brand: Mapped[str | None] = mapped_column(String(120))
    barcode: Mapped[str | None] = mapped_column(String(20))
    size_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    size_unit: Mapped[str | None] = mapped_column(String(4))
    url: Mapped[str | None] = mapped_column(String(500))
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    last_mrp: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PriceObservation(Base):
    """Append-only price history of matched items (and every manual entry)."""

    __tablename__ = "price_observations"

    observation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("competitor_items.item_id")
    )
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(10))
    entered_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    note: Mapped[str | None] = mapped_column(String(300))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProductMatch(Base):
    """"Our product X is their item Y". candidate -> confirmed / rejected by a person;
    auto only on an equal barcode."""

    __tablename__ = "product_matches"

    match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.product_id")
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("competitor_items.item_id")
    )
    competitor_id: Mapped[int] = mapped_column(Integer, ForeignKey("competitors.competitor_id"))
    status: Mapped[str] = mapped_column(String(10))
    method: Mapped[str] = mapped_column(String(10))
    score: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Inner join: FOR UPDATE refuses the nullable side of an outer join.
    item: Mapped[CompetitorItem] = relationship(lazy="joined", innerjoin=True)


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    competitor_id: Mapped[int] = mapped_column(Integer, ForeignKey("competitors.competitor_id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10), default="running")
    requests: Mapped[int] = mapped_column(Integer, default=0)
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class SearchCheck(Base):
    __tablename__ = "search_checks"

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.product_id"), primary_key=True
    )
    competitor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("competitors.competitor_id"), primary_key=True
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Suggestion(Base):
    __tablename__ = "suggestions"

    suggestion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.branch_id")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.product_id")
    )
    current_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    suggested_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    cheapest_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    cheapest_competitor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("competitors.competitor_id")
    )
    cost_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    floor_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    reason: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(12), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    apply_error: Mapped[str | None] = mapped_column(Text)
