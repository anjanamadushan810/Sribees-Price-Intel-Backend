"""SRIBEESonline's admin API, used with the dedicated price-intel admin account.

Read: branches, products, branch inventory. Write: one thing only, a branch
inventory row's price (PUT /admin/inventory/{id}) after a person accepts it.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.errors import IntelError

PAGE = 100


class OnlineError(IntelError):
    status_code = 502


class OnlineClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        settings = get_settings()
        if not settings.online_api_url or not settings.online_admin_email:
            raise OnlineError("ONLINE_API_URL / ONLINE_ADMIN_EMAIL are not set")
        self._settings = settings
        self._token: str | None = None
        self._client = httpx.AsyncClient(
            base_url=settings.online_api_url.rstrip("/"), timeout=30, transport=transport
        )

    async def __aenter__(self) -> "OnlineClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _login(self) -> None:
        resp = await self._client.post("/api/v1/admin/auth/login", json={
            "email": self._settings.online_admin_email,
            "password": self._settings.online_admin_password,
        })
        if resp.status_code != 200:
            raise OnlineError(f"Online sign-in failed (HTTP {resp.status_code})")
        self._token = resp.json()["data"]["token"]

    async def _call(self, method: str, path: str, **kwargs) -> Any:
        for attempt in range(2):
            if self._token is None:
                await self._login()
            resp = await self._client.request(
                method, path, headers={"Authorization": f"Bearer {self._token}"}, **kwargs
            )
            if resp.status_code == 401 and attempt == 0:
                self._token = None  # expired: sign in once more
                continue
            if resp.status_code >= 400:
                detail = ""
                try:
                    detail = str(resp.json().get("detail", ""))[:200]
                except ValueError:
                    pass
                raise OnlineError(f"Online {method} {path}: HTTP {resp.status_code} {detail}")
            return resp.json()
        raise OnlineError("Online sign-in keeps expiring")  # pragma: no cover

    async def branches(self) -> list[dict]:
        data = await self._call("GET", "/api/v1/admin/branches")
        return data["data"]["branches"]

    async def _pages(self, path: str, key: str, params: dict | None = None) -> AsyncIterator[dict]:
        page = 1
        while True:
            data = await self._call("GET", path, params={**(params or {}), "page": page,
                                                        "limit": PAGE})
            body = data["data"]
            for row in body[key]:
                yield row
            if page >= int(body["pagination"]["total_pages"] or 1):
                return
            page += 1

    def products(self) -> AsyncIterator[dict]:
        return self._pages("/api/v1/admin/products", "products")

    def inventory(self, branch_id: str | None = None) -> AsyncIterator[dict]:
        params = {"branch_id": branch_id} if branch_id else {}
        return self._pages("/api/v1/admin/inventory", "items", params)

    async def inventory_row(self, branch_id: str, inventory_id: str, search: str) -> dict | None:
        """Today's row, read fresh just before a price change (Online has no GET by id)."""
        async for row in self._pages("/api/v1/admin/inventory", "items",
                                     {"branch_id": branch_id, "search": search[:100]}):
            if row["inventory_id"] == inventory_id:
                return row
        return None

    async def price_floors(self, branch_id: str, product_ids: list[str]) -> dict[str, list[dict]]:
        """Sribees Finance's minimum prices for these products in one branch (at most 200)."""
        data = await self._call("GET", "/api/v1/admin/inventory/price-floors", params={
            "branch_id": branch_id, "product_ids": ",".join(product_ids)})
        return data["data"]["floors"]

    async def set_price(self, inventory_id: str, body: dict) -> dict:
        data = await self._call("PUT", f"/api/v1/admin/inventory/{inventory_id}", json=body)
        return data["data"]
