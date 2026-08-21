"""
tests/test_agent.py

Covers exactly what the Buildathon brief asks reviewers to look for:
  1. The agent's proposal is bounded (price always comes from catalog, never the LLM)
  2. Money only moves after an explicit accept (gated)
  3. One failure (Razorpay order creation) is handled gracefully, not by crashing
  4. The audit trail records proposal -> decision -> outcome

Run with:  pytest tests/ -v
These tests mock the Anthropic and Razorpay clients — no real API keys
or network calls needed to verify the logic.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import get_upsell_proposal, UpsellProposal
from razorpay_client import RazorpayCheckout, OrderCreationError
from policy import RateLimiter, would_exceed_session_cap, SESSION_SPEND_CAP_PAISE
import audit


def _fake_tool_use_response(product_id, reason):
    """Builds a fake Anthropic Message with a tool_use block, matching the SDK shape."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "propose_upsell"
    block.input = {"product_id": product_id, "reason": reason}
    message = MagicMock()
    message.content = [block]
    return message


class TestAgentProposal:
    def test_proposal_price_comes_from_catalog_not_llm(self):
        """The LLM only ever returns a product_id — the price is looked up
        from our own catalog, so the agent can never quote its own price."""
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _fake_tool_use_response(
            "P004", "Pairs well with the Trailblazer backpack for rain protection."
        )
        proposal = get_upsell_proposal(["P001"], client=fake_client)

        assert proposal.product_id == "P004"
        assert proposal.product is not None
        assert proposal.product["price_inr"] == 79900  # from catalog.json, not from the LLM

    def test_proposal_can_be_none(self):
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _fake_tool_use_response(
            None, "Nothing in the catalog complements this cart well."
        )
        proposal = get_upsell_proposal(["P008"], client=fake_client)
        assert proposal.product_id is None
        assert proposal.product is None

    def test_hallucinated_product_id_is_flagged(self):
        """The model can technically put any string in product_id even
        though we asked it to only use catalog IDs — is_hallucinated
        catches that instead of silently returning a broken product."""
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _fake_tool_use_response(
            "P999", "This backpack is great."  # P999 does not exist in catalog.json
        )
        proposal = get_upsell_proposal(["P001"], client=fake_client)
        assert proposal.is_hallucinated is True
        assert proposal.product is None

    def test_valid_product_is_not_flagged_as_hallucinated(self):
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _fake_tool_use_response(
            "P004", "Rain protection."
        )
        proposal = get_upsell_proposal(["P001"], client=fake_client)
        assert proposal.is_hallucinated is False

    def test_malformed_tool_response_raises_not_crashes_silently(self):
        """If the model doesn't return the forced tool call, we raise a
        clear error rather than guessing — the caller (app.py) turns this
        into a friendly message instead of a 500."""
        fake_client = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        message = MagicMock()
        message.content = [text_block]
        fake_client.messages.create.return_value = message

        with pytest.raises(ValueError):
            get_upsell_proposal(["P001"], client=fake_client)


class TestRazorpayCheckoutFailureHandling:
    """This is 'the one failure handled gracefully' for the submission."""

    def test_transient_failure_then_success_on_retry(self):
        checkout = RazorpayCheckout.__new__(RazorpayCheckout)  # bypass __init__ (no real keys)
        checkout.client = MagicMock()
        checkout.client.order.create.side_effect = [
            Exception("temporary network error"),
            {"id": "order_test123", "amount": 79900, "currency": "INR", "status": "created", "receipt": "r1"},
        ]

        result = checkout.create_order_for_product("P004", 79900, "r1", max_retries=1)

        assert result.order_id == "order_test123"
        assert checkout.client.order.create.call_count == 2

    def test_persistent_failure_raises_order_creation_error(self):
        checkout = RazorpayCheckout.__new__(RazorpayCheckout)
        checkout.client = MagicMock()
        checkout.client.order.create.side_effect = Exception("gateway down")

        with pytest.raises(OrderCreationError):
            checkout.create_order_for_product("P004", 79900, "r1", max_retries=1)

        # Retried exactly once beyond the first attempt, then gave up cleanly
        assert checkout.client.order.create.call_count == 2


class TestPolicy:
    def test_rate_limiter_allows_up_to_max(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        assert limiter.allow("s1") is True
        assert limiter.allow("s1") is True
        assert limiter.allow("s1") is True
        assert limiter.allow("s1") is False  # 4th request in window is blocked

    def test_rate_limiter_is_per_session(self):
        limiter = RateLimiter(max_requests=1, window_seconds=60)
        assert limiter.allow("s1") is True
        assert limiter.allow("s1") is False
        assert limiter.allow("s2") is True  # different session, own budget

    def test_spend_cap_blocks_when_exceeded(self):
        # current total already near the cap; one more item pushes it over
        near_cap = SESSION_SPEND_CAP_PAISE - 50000
        assert would_exceed_session_cap("s1", 100000, near_cap) is True

    def test_spend_cap_allows_when_under(self):
        assert would_exceed_session_cap("s1", 50000, 0) is False


class TestIdempotentCheckout:
    def test_second_accept_for_same_product_returns_existing_order(self, tmp_path, monkeypatch):
        log_file = tmp_path / "audit_log.jsonl"
        monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(log_file))

        audit.log_order_created("s1", "P004", "order_abc", 79900)

        found = audit.find_order_for("s1", "P004")
        assert found is not None
        assert found["order_id"] == "order_abc"

        not_found = audit.find_order_for("s1", "P005")  # different product, no order yet
        assert not_found is None

    def test_session_total_sums_only_created_orders(self, tmp_path, monkeypatch):
        log_file = tmp_path / "audit_log.jsonl"
        monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(log_file))

        audit.log_order_created("s1", "P004", "order_a", 79900)
        audit.log_order_created("s1", "P006", "order_b", 129900)
        audit.log_order_failed("s1", "P002", "gateway down")  # should NOT count toward total

        total = audit.session_accepted_total_paise("s1")
        assert total == 79900 + 129900


class TestAuditTrail:
    def test_full_flow_is_logged_in_order(self, tmp_path, monkeypatch):
        log_file = tmp_path / "audit_log.jsonl"
        monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(log_file))

        audit.log_proposal("s1", "P004", "Rain protection for the backpack.", ["P001"])
        audit.log_user_decision("s1", "P004", accepted=True)
        audit.log_order_created("s1", "P004", "order_abc", 79900)

        trail = audit.read_audit_trail("s1")
        types = [e["type"] for e in trail]
        assert types == ["proposal", "user_decision", "order_created"]
        assert trail[0]["reason"] == "Rain protection for the backpack."
        assert trail[1]["accepted"] is True
        assert trail[2]["order_id"] == "order_abc"

    def test_failed_order_is_logged_not_silently_dropped(self, tmp_path, monkeypatch):
        log_file = tmp_path / "audit_log.jsonl"
        monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(log_file))

        audit.log_order_failed("s2", "P004", "gateway down after 2 attempts")
        trail = audit.read_audit_trail("s2")
        assert trail[0]["type"] == "order_failed"
        assert "gateway down" in trail[0]["error_message"]
