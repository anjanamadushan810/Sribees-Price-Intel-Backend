"""Request and response bodies. Money travels as strings in JSON so no float ever touches it."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, PlainSerializer

Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]
Role = Literal["super_admin", "branch_manager", "marketing_manager", "price_checker"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- auth
class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class TicketIn(BaseModel):
    ticket: str


class TicketCodeIn(BaseModel):
    ticket: str
    code: str = Field(min_length=6, max_length=8)


class RefreshIn(BaseModel):
    refresh_token: str


class TokensOut(BaseModel):
    status: Literal["ok"] = "ok"
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int


class StepOut(BaseModel):
    status: Literal["totp_required", "totp_setup_required"]
    ticket: str


class TotpSetupOut(BaseModel):
    secret: str
    otpauth_uri: str


class SessionOut(ORM):
    session_id: uuid.UUID
    user_agent: str | None
    ip_address: str | None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    current: bool = False


# ---------------------------------------------------------------- people
class BranchOut(ORM):
    branch_id: uuid.UUID
    name: str
    is_active: bool
    products: int = 0  # products this branch stocks on Online (only on /branches)


class UserOut(ORM):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: str
    all_branches: bool
    is_active: bool
    totp_enabled: bool
    last_login_at: datetime | None
    branches: list[BranchOut]


class MeOut(UserOut):
    permissions: list[str]
    two_factor: bool = True


class UserIn(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=150)
    role: Role
    password: str = Field(min_length=12, max_length=200)
    branch_ids: list[uuid.UUID] = []
    all_branches: bool = False


class UserPatch(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=150)
    role: Role | None = None
    is_active: bool | None = None
    branch_ids: list[uuid.UUID] | None = None
    all_branches: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=200)
    reset_totp: bool = False


class AuditOut(ORM):
    audit_id: int
    occurred_at: datetime
    user_id: uuid.UUID | None
    action: str
    entity: str
    entity_id: str | None
    before: dict | None
    after: dict | None


class PricingSettings(BaseModel):
    min_margin_pct: Decimal = Field(ge=0, le=100)
    stale_days: dict[Literal["daily", "weekly", "monthly", "long"], int]


# ---------------------------------------------------------------- market
class CompetitorOut(ORM):
    competitor_id: int
    code: str
    name: str
    kind: str
    adapter: str | None
    base_url: str | None
    is_enabled: bool
    status: str
    notes: str | None
    last_run_at: datetime | None
    last_ok_at: datetime | None
    running: bool = False


class CompetitorPatch(BaseModel):
    is_enabled: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)


class RunOut(ORM):
    run_id: int
    started_at: datetime
    finished_at: datetime | None
    status: str
    requests: int
    items_seen: int
    error: str | None


class ItemOut(ORM):
    item_id: uuid.UUID
    competitor_id: int
    name: str
    brand: str | None
    size_value: Decimal | None
    size_unit: str | None
    url: str | None
    image_url: str | None = None
    last_price: Money | None
    last_mrp: Money | None
    in_stock: bool
    last_seen_at: datetime


class CompetitorPrice(BaseModel):
    competitor_id: int
    competitor: str
    match_id: uuid.UUID
    status: str
    item: ItemOut
    fresh: bool


class ProductRow(BaseModel):
    product_id: uuid.UUID
    name: str
    name_si: str | None
    sku: str | None
    category: str | None
    image_url: str | None = None
    review_level: str
    mrp: Money | None
    our_price: Money | None
    cost_price: Money | None
    cheapest_price: Money | None
    cheapest_competitor: str | None
    cheapest_image: str | None = None
    position: Literal["cheaper", "equal", "dearer", "unknown"]
    prices: list[CompetitorPrice]
    pending_suggestion_id: uuid.UUID | None = None


class ProductPage(BaseModel):
    items: list[ProductRow]
    total: int
    page: int
    pages: int


class HistoryPoint(BaseModel):
    competitor: str
    price: Money
    seen_at: datetime
    source: str


class MatchQueueItem(BaseModel):
    match_id: uuid.UUID
    product_id: uuid.UUID
    product_name: str
    product_size: str | None
    competitor_id: int
    competitor: str
    score: Decimal
    item: ItemOut
    our_price: Money | None = None  # what we sell it for, to see a price that cannot be it
    product_image: str | None = None  # our photo, beside theirs


class MatchQueuePage(BaseModel):
    items: list[MatchQueueItem]
    total: int  # (product, shop) pairs waiting
    no_price_yet: int = 0  # products in the queue matched at no shop at all
    page: int
    pages: int


class MatchHealthItem(BaseModel):
    match_id: uuid.UUID
    product_id: uuid.UUID
    product_name: str
    product_size: str | None
    product_image: str | None
    our_price: Money | None
    competitor_id: int
    competitor: str
    status: Literal["changed", "gone"]
    was: str | None  # auto / confirmed before the shop moved
    method: str
    note: str | None
    since: datetime | None
    seen_name: str | None  # the title when it was matched
    item: ItemOut  # what the shop shows now


class MatchHealthPage(BaseModel):
    items: list[MatchHealthItem]
    total: int
    page: int
    pages: int


class ManualLinkIn(BaseModel):
    product_id: uuid.UUID
    item_id: uuid.UUID


class ManualPriceIn(BaseModel):
    competitor_id: int
    product_id: uuid.UUID
    price: Decimal = Field(gt=0, le=10_000_000, decimal_places=2)
    mrp: Decimal | None = Field(default=None, gt=0, le=10_000_000, decimal_places=2)
    in_stock: bool = True
    note: str | None = Field(default=None, max_length=300)


class CostIn(BaseModel):
    cost_price: Decimal | None = Field(default=None, ge=0, le=10_000_000, decimal_places=2)


# ---------------------------------------------------------------- suggestions
class SuggestionOut(BaseModel):
    suggestion_id: uuid.UUID
    branch_id: uuid.UUID
    branch: str
    product_id: uuid.UUID
    product_name: str
    sku: str | None
    current_price: Money
    suggested_price: Money
    cheapest_price: Money
    cheapest_competitor: str
    cost_price: Money
    floor_price: Money
    reason: str
    status: str
    created_at: datetime
    decided_at: datetime | None
    applied_at: datetime | None
    apply_error: str | None


class SummaryOut(BaseModel):
    products: int
    tracked: int
    cheaper: int
    equal: int
    dearer: int
    unknown: int
    cost_missing: int
    pending_suggestions: int
    match_queue: int
    sources_down: list[str]
