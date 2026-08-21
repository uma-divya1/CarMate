"""
audit.py

Every agent decision and every money action gets written here as one
JSON line — append-only, never edited or deleted. This is the audit
trail the track brief explicitly asks for: "Every money action
explainable, bounded and gated. Show the audit trail."

Log entry types:
  - "proposal"      agent suggested an upsell, with its stated reason
  - "user_decision"  user accepted or declined the proposal
  - "order_created"  a real (test-mode) Razorpay order was created
  - "order_failed"   order creation failed (and how it was handled)
"""

import json
import os
import time
import uuid
from typing import Optional

AUDIT_LOG_PATH = os.path.join(os.path.dirname(__file__), "audit_log.jsonl")


def _write(entry: dict) -> dict:
    entry["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry["event_id"] = str(uuid.uuid4())
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def log_proposal(session_id: str, product_id: str, reason: str, cart_product_ids: list[str]) -> dict:
    return _write({
        "type": "proposal",
        "session_id": session_id,
        "product_id": product_id,
        "reason": reason,
        "cart_at_time": cart_product_ids,
    })


def log_user_decision(session_id: str, product_id: str, accepted: bool) -> dict:
    return _write({
        "type": "user_decision",
        "session_id": session_id,
        "product_id": product_id,
        "accepted": accepted,
    })


def log_order_created(session_id: str, product_id: str, order_id: str, amount_inr_paise: int) -> dict:
    return _write({
        "type": "order_created",
        "session_id": session_id,
        "product_id": product_id,
        "order_id": order_id,
        "amount_inr_paise": amount_inr_paise,
    })


def log_order_failed(session_id: str, product_id: str, error_message: str) -> dict:
    return _write({
        "type": "order_failed",
        "session_id": session_id,
        "product_id": product_id,
        "error_message": error_message,
    })


def log_invalid_proposal(session_id: str, hallucinated_product_id: str, reason: str) -> dict:
    """The LLM returned a product_id that doesn't exist in our catalog.
    This is distinct from a Razorpay failure — it's a model reliability
    failure, and we want it visible in the audit trail as its own
    category rather than lumped in with payment errors."""
    return _write({
        "type": "invalid_proposal",
        "session_id": session_id,
        "hallucinated_product_id": hallucinated_product_id,
        "reason": reason,
    })


def log_blocked_by_policy(session_id: str, product_id: str, policy: str, detail: str) -> dict:
    """A proposal was accepted by the user but blocked by a safety policy
    (e.g. session spend cap) before any money moved. This is a THIRD
    kind of 'gate' — independent of the tool schema and independent of
    catalog-price-only enforcement."""
    return _write({
        "type": "blocked_by_policy",
        "session_id": session_id,
        "product_id": product_id,
        "policy": policy,
        "detail": detail,
    })


def find_order_for(session_id: str, product_id: str) -> Optional[dict]:
    """Look for an already-created order for this exact session+product.
    Used to make /api/checkout/accept idempotent: a double-click,
    browser retry, or page refresh should never create a second real
    order for the same accepted proposal."""
    for entry in read_audit_trail(session_id=session_id):
        if entry.get("type") == "order_created" and entry.get("product_id") == product_id:
            return entry
    return None


def session_accepted_total_paise(session_id: str) -> int:
    """Sum of all successfully created order amounts in this session so
    far — used to enforce a per-session spend cap independent of the
    per-item catalog-price bound."""
    total = 0
    for entry in read_audit_trail(session_id=session_id):
        if entry.get("type") == "order_created":
            total += entry.get("amount_inr_paise", 0)
    return total


def read_audit_trail(session_id: str = None) -> list[dict]:
    if not os.path.exists(AUDIT_LOG_PATH):
        return []
    entries = []
    with open(AUDIT_LOG_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if session_id is None or entry.get("session_id") == session_id:
                entries.append(entry)
    return entries
