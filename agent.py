"""
agent.py

The agent's ONLY tool is `propose_upsell`. It cannot call
`create_order` directly — that's a deliberate design choice, not a
limitation: the agent proposes, a human (or a downstream automated
policy) decides, and only then does razorpay_client actually move
money. This keeps every money action bounded (agent can only pick
from the real catalog) and gated (nothing happens without an
explicit accept step).

Swap ANTHROPIC_MODEL for whichever Claude model your API key has
access to.
"""

import json
import os
from typing import Optional

import anthropic

from catalog import catalog_as_agent_context, get_product

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

SYSTEM_PROMPT = """You are CartMate, an upsell & cross-sell assistant for an outdoor \
gear merchant on Razorpay. A customer has items in their cart. Your job is to \
suggest AT MOST ONE additional product from the catalog that genuinely complements \
their cart — not the most expensive item, the most RELEVANT one.

Rules you must follow:
- You may only reference products that appear in the catalog context below.
- You must call the propose_upsell tool to make your suggestion — never state a \
price or product recommendation in plain text only.
- If nothing in the catalog is a good fit, call propose_upsell with product_id \
set to null and explain why in the reason field.
- Keep the reason field to one short, honest sentence a customer would find useful, \
not persuasive/pushy copy.
"""

PROPOSE_UPSELL_TOOL = {
    "name": "propose_upsell",
    "description": "Propose a single upsell/cross-sell product for the customer's cart, or propose nothing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "product_id": {
                "type": ["string", "null"],
                "description": "Catalog product ID to propose, or null if nothing fits well.",
            },
            "reason": {
                "type": "string",
                "description": "One short, honest sentence explaining why this fits the cart.",
            },
        },
        "required": ["product_id", "reason"],
    },
}


class UpsellProposal:
    def __init__(self, product_id: Optional[str], reason: str):
        self.product_id = product_id
        self.reason = reason
        self.product = get_product(product_id) if product_id else None
        # True only when the model returned a non-null product_id that
        # does NOT exist in the catalog — a hallucination, distinct from
        # the model deliberately proposing nothing.
        self.is_hallucinated = bool(product_id) and self.product is None

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "reason": self.reason,
            "product": self.product,
            "is_hallucinated": self.is_hallucinated,
        }


def get_upsell_proposal(cart_product_ids: list[str], client: Optional["anthropic.Anthropic"] = None) -> UpsellProposal:
    """
    Calls Claude with the propose_upsell tool forced, so the model MUST
    respond through the structured tool call rather than free text.
    Returns a parsed UpsellProposal. Raises on malformed tool output —
    callers should treat that as a recoverable failure (see app.py).
    """
    client = client or anthropic.Anthropic()

    catalog_context = catalog_as_agent_context()
    cart_desc = ", ".join(cart_product_ids) if cart_product_ids else "(empty cart)"

    message = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=SYSTEM_PROMPT + "\n\nCatalog:\n" + catalog_context,
        tools=[PROPOSE_UPSELL_TOOL],
        tool_choice={"type": "tool", "name": "propose_upsell"},
        messages=[
            {"role": "user", "content": f"Customer's current cart contains: {cart_desc}. What's your one upsell suggestion?"}
        ],
    )

    for block in message.content:
        if block.type == "tool_use" and block.name == "propose_upsell":
            data = block.input
            return UpsellProposal(product_id=data.get("product_id"), reason=data.get("reason", ""))

    raise ValueError("Model did not return a propose_upsell tool call.")
