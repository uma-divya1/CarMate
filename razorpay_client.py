"""
razorpay_client.py

Thin wrapper around the Razorpay Python SDK for TEST MODE order creation.
Deliberately narrow: this module can do exactly one thing — create an
order for a product that already exists in our catalog, at the exact
catalog price. It cannot create arbitrary orders or accept arbitrary
amounts from the agent. That's the "bounded" part of the design.

Failure handling: Razorpay's test API can fail transiently (network,
rate limits). We retry once with backoff, then fail loudly and return
a structured error the agent can explain to the user in plain language
instead of crashing. That's the "one failure handled gracefully" part.
"""

import os
import time
import logging
from dataclasses import dataclass
from typing import Optional

import razorpay

logger = logging.getLogger("cartmate.razorpay")


class OrderCreationError(Exception):
    """Raised when Razorpay order creation fails after retries."""
    def __init__(self, message: str, cause: Optional[Exception] = None):
        super().__init__(message)
        self.cause = cause


@dataclass
class OrderResult:
    order_id: str
    amount_inr_paise: int
    currency: str
    status: str
    receipt: str


class RazorpayCheckout:
    def __init__(self, key_id: Optional[str] = None, key_secret: Optional[str] = None):
        key_id = key_id or os.environ.get("RAZORPAY_KEY_ID")
        key_secret = key_secret or os.environ.get("RAZORPAY_KEY_SECRET")
        if not key_id or not key_secret:
            raise RuntimeError(
                "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set. "
                "Use TEST MODE keys from the Razorpay dashboard."
            )
        self.client = razorpay.Client(auth=(key_id, key_secret))

    def create_order_for_product(
        self,
        product_id: str,
        amount_inr_paise: int,
        receipt: str,
        max_retries: int = 1,
    ) -> OrderResult:
        """
        Create a Razorpay test-mode order for a single catalog product.

        amount_inr_paise MUST come from our own catalog lookup (see
        catalog.py), never from the LLM's output directly — this is
        what keeps the money action "bounded": the agent can choose
        *which* product to propose, never the price.
        """
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                payload = {
                    "amount": amount_inr_paise,
                    "currency": "INR",
                    "receipt": receipt,
                    "notes": {"product_id": product_id, "source": "cartmate-agent"},
                }
                order = self.client.order.create(data=payload)
                return OrderResult(
                    order_id=order["id"],
                    amount_inr_paise=order["amount"],
                    currency=order["currency"],
                    status=order["status"],
                    receipt=order.get("receipt", receipt),
                )
            except Exception as exc:  # razorpay SDK raises various error types
                last_error = exc
                logger.warning(
                    "Razorpay order creation failed (attempt %s/%s): %s",
                    attempt + 1, max_retries + 1, exc,
                )
                if attempt < max_retries:
                    time.sleep(0.6 * (attempt + 1))  # small backoff before retry

        # Exhausted retries — fail loudly with a clear, user-facing message.
        raise OrderCreationError(
            f"Could not create a payment order for product {product_id} "
            f"after {max_retries + 1} attempt(s). The merchant's payment "
            f"provider did not respond successfully.",
            cause=last_error,
        )
