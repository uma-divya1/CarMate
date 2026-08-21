"""
catalog.py

Loads the merchant's product catalog and provides safe lookups.
This is the single source of truth for prices and product IDs —
the agent never invents a price; it only ever references what's here.
"""

import json
import os
from typing import Optional

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "catalog.json")


def load_catalog() -> dict:
    with open(CATALOG_PATH, "r") as f:
        return json.load(f)


def get_product(product_id: str, catalog: Optional[dict] = None) -> Optional[dict]:
    catalog = catalog or load_catalog()
    for p in catalog["products"]:
        if p["id"] == product_id:
            return p
    return None


def get_products_in_cart(product_ids: list[str], catalog: Optional[dict] = None) -> list[dict]:
    catalog = catalog or load_catalog()
    return [p for p in catalog["products"] if p["id"] in product_ids]


def catalog_as_agent_context(catalog: Optional[dict] = None) -> str:
    """Render the catalog as compact text the LLM can read as context."""
    catalog = catalog or load_catalog()
    lines = [f"Merchant: {catalog['merchant_name']}", "Products:"]
    for p in catalog["products"]:
        price_rupees = p["price_inr"] / 100
        lines.append(
            f"- {p['id']}: {p['name']} (₹{price_rupees:.0f}, category: {p['category']}) "
            f"— {p['description']}"
        )
    return "\n".join(lines)
