"""Source adapters by name (competitors.adapter)."""

from app.adapters.base import Adapter, PoliteClient
from app.adapters.cargills import CargillsAdapter
from app.adapters.glomark import GlomarkAdapter
from app.adapters.keells import KeellsAdapter
from app.adapters.nexmart import NexmartAdapter
from app.adapters.opencart import OpenCartAdapter
from app.adapters.ranjan import RanjanAdapter
from app.adapters.shopify import ShopifyAdapter
from app.adapters.woocommerce import WooCommerceAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    "nexmart": NexmartAdapter,
    "shopify": ShopifyAdapter,
    "opencart": OpenCartAdapter,
    "keells": KeellsAdapter,
    "cargills": CargillsAdapter,
    "glomark": GlomarkAdapter,
    "woocommerce": WooCommerceAdapter,
    "ranjan": RanjanAdapter,
}


def build(adapter: str, client: PoliteClient) -> Adapter | None:
    cls = ADAPTERS.get(adapter)
    return cls(client) if cls else None
