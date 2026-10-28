"""End to end on a real database, with Online and the shops faked at the HTTP layer:
sync -> collect -> candidate -> confirm -> cost -> suggestion -> accept -> Online PUT."""

import json
import uuid
from decimal import Decimal as D

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select, text

from app.core.db import Base, get_engine, get_sessionmaker
from app.core.security import hash_password
from app.models import (
    BranchProduct,
    Competitor,
    CompetitorItem,
    PriceObservation,
    ProductMatch,
    Suggestion,
    User,
)
from app.services import collect, suggest
from app.services.catalog_sync import sync
from app.services.online_client import OnlineClient

pytestmark = pytest.mark.usefixtures("database")

MATUGAMA = "b2ab53fb-6419-4705-8a49-0c1369942959"
OTHER = "11111111-2222-3333-4444-555555555555"
BISCUIT = "aaaaaaaa-0000-0000-0000-000000000001"
SOAP = "aaaaaaaa-0000-0000-0000-000000000002"
INV = {MATUGAMA: "cccccccc-0000-0000-0000-000000000001",
       OTHER: "cccccccc-0000-0000-0000-000000000002"}


class FakeOnline:
    """Just enough of SRIBEESonline's admin API."""

    def __init__(self):
        self.puts: list[tuple[str, dict]] = []
        self.rows = {
            (MATUGAMA, BISCUIT): {"branch_price": None, "global_price": 520.0,
                                  "discount_percentage": None, "is_on_sale": False},
            (OTHER, BISCUIT): {"branch_price": None, "global_price": 520.0,
                               "discount_percentage": None, "is_on_sale": False},
        }

    def _inv(self, branch, product):
        r = self.rows[(branch, product)]
        base = r["branch_price"] if r["branch_price"] is not None else r["global_price"]
        return {"inventory_id": INV[branch], "product_id": product, "branch_id": branch,
                "product_name": "Maliban Chocolate Cream 400g", "sku": "MAL-CC-400",
                "branch_price": r["branch_price"], "global_price": r["global_price"],
                "effective_price": base, "effective_discount": r["discount_percentage"],
                "discount_percentage": r["discount_percentage"], "is_on_sale": r["is_on_sale"],
                "is_active": True, "price_review_level": "daily"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        page = {"total": 0, "page": 1, "limit": 100, "total_pages": 1}
        if path.endswith("/admin/auth/login"):
            return httpx.Response(200, json={"data": {"token": "t", "refresh_token": "r"}})
        assert request.headers["authorization"] == "Bearer t"
        if path.endswith("/admin/branches"):
            return httpx.Response(200, json={"data": {"branches": [
                {"branch_id": MATUGAMA, "name": "Matugama", "is_active": True},
                {"branch_id": OTHER, "name": "Other", "is_active": True}]}})
        if path.endswith("/admin/products"):
            return httpx.Response(200, json={"data": {"pagination": page, "products": [
                {"product_id": BISCUIT, "name": "Maliban Chocolate Cream 400g",
                 "sku": "MAL-CC-400", "compare_at_price": 540, "price_review_level": "daily",
                 "is_active": True},
                {"product_id": SOAP, "name": "Sunlight Soap", "sku": "4006381333931",
                 "weight": "0.11", "weight_unit": "kg", "is_active": True}]}})
        if path.endswith("/admin/inventory") and request.method == "GET":
            branch = request.url.params.get("branch_id")
            items = [self._inv(b, p) for (b, p) in self.rows if branch in (None, b)]
            return httpx.Response(200, json={"data": {"pagination": page, "items": items}})
        if request.method == "PUT":
            inv_id = path.rsplit("/", 1)[1]
            branch = next(b for b, i in INV.items() if i == inv_id)
            body = json.loads(request.content)
            self.puts.append((inv_id, body))
            row = self.rows[(branch, BISCUIT)]
            if "branch_price" in body:
                row["branch_price"] = float(body["branch_price"])
            if "discount_percentage" in body:
                row["discount_percentage"] = body["discount_percentage"]
            return httpx.Response(200, json={"data": self._inv(branch, BISCUIT)})
        return httpx.Response(404)


def nexmart_shop(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    return httpx.Response(200, json={"pages": 1, "items": [
        {"sku": "BISC-004", "name": "MALIBAN BISCUIT CHOCOLATE CREAM - 400 g", "pack": "400 g",
         "brand": "Maliban", "price": 480.5, "mrp": 540, "inStock": True, "enabled": True},
        {"sku": "BISC-005", "name": "MALIBAN BISCUIT CHOCOLATE CREAM - 200 g", "pack": "200 g",
         "brand": "Maliban", "price": 250, "mrp": 270, "inStock": True, "enabled": True},
        {"sku": "SOAP-2", "name": "SUNLIGHT SOAP - 110 g", "price": 95, "inStock": True,
         "enabled": True},
        {"sku": "SOAP-1", "name": "Lux Soap 100g", "price": 150, "inStock": True,
         "enabled": True},
    ]})


@pytest.fixture(scope="module")
def online():
    return FakeOnline()


@pytest.fixture(scope="module")
async def api(online, monkeypatch_module):
    from app.api import pricing as pricing_api
    from app.main import app

    monkeypatch_module.setattr(
        pricing_api, "OnlineClient", lambda: OnlineClient(transport=httpx.MockTransport(online)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


async def _user(role: str, branches: list[str] | None = None) -> tuple[str, str]:
    email = f"{role}-{uuid.uuid4().hex[:6]}@test.lk"
    async with get_sessionmaker()() as db, db.begin():
        from app.models import Branch

        found = [await db.get(Branch, uuid.UUID(b)) for b in branches or []]
        db.add(User(email=email, full_name=role, role=role,
                    password_hash=hash_password("correct horse battery"), branches=found))
    return email, "correct horse battery"


async def _login(api: AsyncClient, role: str, branches: list[str] | None = None) -> dict:
    email, password = await _user(role, branches)
    resp = await api.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ---------------------------------------------------------------- schema
async def test_models_match_schema():
    async with get_engine().connect() as conn:
        def columns(sync_conn):
            insp = inspect(sync_conn)
            return {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names()}
        db_cols = await conn.run_sync(columns)
    for table in Base.metadata.sorted_tables:
        assert table.name in db_cols, table.name
        assert {c.name for c in table.columns} == db_cols[table.name], table.name


# ---------------------------------------------------------------- the whole loop
async def test_sync_then_collect_proposes_only_same_size(online):
    async with OnlineClient(transport=httpx.MockTransport(online)) as client, \
            get_sessionmaker()() as db, db.begin():
        counts = await sync(db, client)
    assert counts == {"branches": 2, "products": 2, "branch_products": 2}

    async with get_sessionmaker()() as db:
        nexmart = await db.scalar(select(Competitor).where(Competitor.code == "nexmart"))
    run = await collect.run(get_sessionmaker(), nexmart.competitor_id,
                            transport=httpx.MockTransport(nexmart_shop))
    assert run.status == "ok" and run.items_seen == 4, run.error

    async with get_sessionmaker()() as db:
        matches = list(await db.scalars(select(ProductMatch).where(
            ProductMatch.product_id == uuid.UUID(BISCUIT))))
        assert [m.item.name for m in matches] == ["MALIBAN BISCUIT CHOCOLATE CREAM - 400 g"]
        assert matches[0].status == "candidate"
        # "Sunlight Soap" (110 g from Online's weight) is a sure match: automatic, and its
        # history starts with the price it was seen at.
        (soap,) = list(await db.scalars(select(ProductMatch).where(
            ProductMatch.product_id == uuid.UUID(SOAP))))
        assert soap.status == "auto" and soap.method == "name"
        (obs,) = list(await db.scalars(select(PriceObservation)))
        assert obs.item_id == soap.item_id and obs.price == D("95.00")


async def test_checker_confirms_manager_prices_and_online_changes(api, online):
    checker = await _login(api, "price_checker")
    page = (await api.get("/matches/queue", headers=checker)).json()
    assert page["total"] >= 1
    count = (await api.get("/matches/queue/count", headers=checker)).json()
    assert count["waiting"] == page["total"]
    queue = page["items"]
    biscuit = next(q for q in queue if q["product_id"] == BISCUIT)
    assert (await api.post(f"/matches/{biscuit['match_id']}/confirm",
                           headers=checker)).status_code == 204

    manager = await _login(api, "branch_manager", [MATUGAMA])
    # No cost yet: no suggestion, and the product shows up as cost-missing.
    async with get_sessionmaker()() as db, db.begin():
        assert (await suggest.refresh(db))["cost_missing"] == 2
    page = (await api.get("/products", params={"cost_missing": True}, headers=manager)).json()
    assert [r["product_id"] for r in page["items"]] == [BISCUIT]
    assert page["items"][0]["position"] == "dearer"
    assert page["items"][0]["cheapest_price"] == "480.50"

    # The checker may not type costs; the manager may, but not for another branch.
    url = f"/branches/{MATUGAMA}/products/{BISCUIT}/cost"
    assert (await api.put(url, json={"cost_price": "400"}, headers=checker)).status_code == 403
    assert (await api.put(f"/branches/{OTHER}/products/{BISCUIT}/cost",
                          json={"cost_price": "400"}, headers=manager)).status_code == 403
    assert (await api.put(url, json={"cost_price": "400"}, headers=manager)).status_code == 204

    async with get_sessionmaker()() as db, db.begin():
        assert (await suggest.refresh(db))["new"] == 1
        again = await suggest.refresh(db)
        assert again["new"] == 0  # unchanged inputs: the pending one stays

    (s,) = (await api.get("/suggestions", headers=manager)).json()
    assert s["suggested_price"] == "480.00" and s["floor_price"] == "420.00"
    assert s["cheapest_competitor"] == "Nexmart"

    marketing = await _login(api, "marketing_manager")
    assert (await api.post(f"/suggestions/{s['suggestion_id']}/accept",
                           headers=marketing)).status_code == 403

    resp = await api.post(f"/suggestions/{s['suggestion_id']}/accept", headers=manager)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied" and resp.json()["apply_error"] is None
    assert online.puts == [(INV[MATUGAMA], {"branch_price": "480.00"})]
    assert online.rows[(OTHER, BISCUIT)]["branch_price"] is None  # other branch untouched

    async with get_sessionmaker()() as db:
        bp = await db.get(BranchProduct, (uuid.UUID(MATUGAMA), uuid.UUID(BISCUIT)))
        assert bp.selling_price == D("480.00")
        actions = list(await db.scalars(text(
            "SELECT action FROM audit_log ORDER BY audit_id")))
        assert {"match.confirm", "cost.set", "price.apply"} <= set(actions)


async def test_price_changed_on_online_after_suggestion_is_not_overwritten(api, online):
    async with get_sessionmaker()() as db, db.begin():
        bp = await db.get(BranchProduct, (uuid.UUID(OTHER), uuid.UUID(BISCUIT)))
        bp.cost_price = D("400")
        await suggest.refresh(db)
    online.rows[(OTHER, BISCUIT)]["branch_price"] = 499.0  # someone edits Online meanwhile
    admin = await _login(api, "super_admin")
    (s,) = (await api.get("/suggestions", params={"branch_id": OTHER}, headers=admin)).json()
    resp = await api.post(f"/suggestions/{s['suggestion_id']}/accept", headers=admin)
    assert resp.json()["status"] == "failed"
    assert "changed" in resp.json()["apply_error"]
    assert len(online.puts) == 1  # no second PUT


async def test_manual_shelf_price(api):
    checker = await _login(api, "price_checker", [MATUGAMA])
    unassigned = await _login(api, "price_checker")
    assert (await api.get(f"/products/{BISCUIT}", headers=unassigned)).status_code == 422
    async with get_sessionmaker()() as db:
        local = await db.scalar(select(Competitor).where(Competitor.code == "local"))
        nexmart = await db.scalar(select(Competitor).where(Competitor.code == "nexmart"))
    body = {"competitor_id": local.competitor_id, "product_id": BISCUIT, "price": "470",
            "note": "Shop near the bus stand"}
    assert (await api.post("/observations/manual", json=body, headers=checker)).status_code == 201
    body["competitor_id"] = nexmart.competitor_id
    assert (await api.post("/observations/manual", json=body, headers=checker)).status_code == 422

    page = (await api.get(f"/products/{BISCUIT}", headers=checker)).json()
    assert page["cheapest_price"] == "470.00"
    assert page["cheapest_competitor"] == "Local shops (manual)"


# ---------------------------------------------------------------- database rules
async def test_history_and_audit_are_append_only():
    from sqlalchemy.exc import DBAPIError

    for sql in ("UPDATE price_observations SET price = 1", "DELETE FROM audit_log"):
        async with get_sessionmaker()() as db:
            with pytest.raises(DBAPIError):
                await db.execute(text(sql))


async def test_one_live_match_per_product_and_shop():
    from sqlalchemy.exc import IntegrityError

    async with get_sessionmaker()() as db:
        live = await db.scalar(select(ProductMatch).where(ProductMatch.status == "confirmed",
                                                          ProductMatch.method == "name"))
        other = CompetitorItem(competitor_id=live.competitor_id, external_id="dupe",
                               name="dupe")
        db.add(other)
        await db.flush()
        db.add(ProductMatch(product_id=live.product_id, item_id=other.item_id,
                            competitor_id=live.competitor_id, status="auto",
                            method="barcode", score=1))
        with pytest.raises(IntegrityError):
            await db.flush()


async def test_suggestion_can_never_raise_a_price():
    from sqlalchemy.exc import IntegrityError

    async with get_sessionmaker()() as db:
        nexmart = await db.scalar(select(Competitor.competitor_id).where(
            Competitor.code == "nexmart"))
        db.add(Suggestion(branch_id=uuid.UUID(OTHER), product_id=uuid.UUID(SOAP),
                          current_price=D("100"), suggested_price=D("120"),
                          cheapest_price=D("120"), cheapest_competitor_id=nexmart,
                          cost_price=D("50"), floor_price=D("53"), reason="match_cheapest"))
        with pytest.raises(IntegrityError):
            await db.flush()


# ---------------------------------------------------------------- the Price app
def _online_profiles(request: httpx.Request) -> httpx.Response:
    """Online's /admin/auth/profile for three tokens: a manager, support staff, garbage."""
    who = {"Bearer mgr-token": ("mgr@sribees.lk", "branch_manager"),
           "Bearer staff-token": ("staff@sribees.lk", "customer_support")}
    found = who.get(request.headers.get("authorization", ""))
    if found is None:
        return httpx.Response(401, json={"detail": "Invalid token"})
    email, role = found
    return httpx.Response(200, json={"success": True, "admin": {
        "adminId": str(uuid.uuid4()), "email": email, "fullName": "Floor Manager",
        "role": role, "isActive": True}})


async def test_price_app_signs_in_with_online_token_and_decides(api, monkeypatch_module):
    from app.api import app_api

    monkeypatch_module.setattr(app_api, "_transport", httpx.MockTransport(_online_profiles))
    url = f"/app/products/{BISCUIT}/shops"
    assert (await api.get(url)).status_code == 401
    assert (await api.get(url, headers={"Authorization": "Bearer junk"})).status_code == 401
    assert (await api.get(url, headers={"Authorization": "Bearer staff-token"})).status_code == 403

    mgr = {"Authorization": "Bearer mgr-token"}
    view = (await api.get(url, headers=mgr)).json()
    nexmart = next(s for s in view["shops"] if s["competitor"] == "Nexmart")
    assert nexmart["matched_by"] == "confirmed" and nexmart["candidates"] == []
    assert nexmart["matched"]["item"]["last_price"] == "480.50"

    # The manager says the match is wrong: it is gone, and the decision is theirs.
    after = (await api.post(f"/app/matches/{nexmart['matched']['match_id']}/reject",
                            headers=mgr)).json()
    assert next(s for s in after["shops"] if s["competitor"] == "Nexmart")["matched"] is None
    async with get_sessionmaker()() as db:
        user = await db.scalar(select(User).where(User.email == "mgr@sribees.lk"))
        assert user.role == "price_checker"
        m = await db.get(ProductMatch, uuid.UUID(nexmart["matched"]["match_id"]))
        assert m.status == "rejected" and m.decided_by == user.user_id


async def test_branch_list_puts_the_stocking_branch_first(api):
    admin = await _login(api, "super_admin")
    branches = (await api.get("/branches", headers=admin)).json()
    assert [b["products"] for b in branches] == sorted((b["products"] for b in branches),
                                                       reverse=True)
    assert branches[0]["products"] >= 1


async def test_one_failing_search_page_does_not_end_the_run():
    from app.adapters.base import SourceFailed
    from app.services.collect import _run_search

    class Flaky:
        mode = "search"
        calls = 0

        async def search(self, query):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise SourceFailed("could not render one page")
            return []

    async with get_sessionmaker()() as db:
        arpico = await db.scalar(
            select(Competitor.competitor_id).where(Competitor.code == "arpico"))
    await _run_search(get_sessionmaker(), arpico, Flaky())
    assert Flaky.calls >= 2  # carried on after the failure
