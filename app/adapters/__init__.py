"""Source adapters by name (competitors.adapter). Keells and Cargills are Phase 2."""

from app.adapters.base import Adapter, PoliteClient
from app.adapters.nexmart import NexmartAdapter
from app.adapters.opencart import OpenCartAdapter
from app.adapters.shopify import ShopifyAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    "nexmart": NexmartAdapter,
    "shopify": ShopifyAdapter,
    "opencart": OpenCartAdapter,
}


def build(adapter: str, client: PoliteClient) -> Adapter | None:
    cls = ADAPTERS.get(adapter)
    return cls(client) if cls else None
