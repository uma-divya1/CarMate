"""
app.py

Routes:
  GET  /                      demo UI — pick cart items, see agent proposal
  POST /api/propose           agent proposes ONE upsell for a given cart (no money moved)
  POST /api/checkout/accept   user accepted the proposal -> create real test-mode order
  POST /api/checkout/decline  user declined -> just logs the decision
  GET  /api/audit/<session_id> returns the full audit trail for a session (for the demo)

Money only ever moves in /api/checkout/accept, and only for the exact
product + price the proposal referenced — never a value coming
straight from the LLM.
"""

import os
import uuid

from flask import Flask, jsonify, render_template, request, session

from agent import get_upsell_proposal
from catalog import get_product, load_catalog, catalog_as_agent_context
from razorpay_client import RazorpayCheckout, OrderCreationError
from policy import propose_rate_limiter, would_exceed_session_cap, SESSION_SPEND_CAP_PAISE
import audit

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")

# Lazily construct so the app can still boot / demo the UI without keys set.
_checkout_client = None


def get_checkout_client() -> RazorpayCheckout:
    global _checkout_client
    if _checkout_client is None:
        _checkout_client = RazorpayCheckout()
    return _checkout_client


@app.route("/")
def index():
    catalog = load_catalog()
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    return render_template("index.html", catalog=catalog, session_id=session["session_id"])


@app.route("/api/propose", methods=["POST"])
def propose():
    body = request.get_json(force=True)
    cart_product_ids = body.get("cart_product_ids", [])
    session_id = session.get("session_id") or str(uuid.uuid4())
    session["session_id"] = session_id

    # Abuse protection — independent of anything money-related, this
    # just stops one session from hammering the LLM endpoint.
    if not propose_rate_limiter.allow(session_id):
        return jsonify({
            "ok": False,
            "error": "Too many requests — please wait a moment before asking again.",
        }), 200

    try:
        proposal = get_upsell_proposal(cart_product_ids)
    except Exception as exc:
        # Graceful failure #1: the model call itself can fail (bad tool
        # output, API error). We don't crash the request — we tell the
        # frontend plainly and let the user retry or check out with no upsell.
        return jsonify({
            "ok": False,
            "error": "The upsell agent couldn't come up with a suggestion this time.",
            "detail": str(exc),
        }), 200

    if proposal.is_hallucinated:
        # Graceful failure #2: the model returned a product_id that
        # doesn't exist in our catalog. This is a MODEL reliability
        # failure, not a payments failure — handled and logged
        # separately so it's visible in the audit trail as its own
        # category, and the user never sees a broken product card.
        audit.log_invalid_proposal(session_id, proposal.product_id, proposal.reason)
        return jsonify({
            "ok": False,
            "error": "The agent suggested something that isn't in our catalog — skipping this suggestion.",
        }), 200

    audit.log_proposal(
        session_id=session_id,
        product_id=proposal.product_id,
        reason=proposal.reason,
        cart_product_ids=cart_product_ids,
    )

    return jsonify({"ok": True, "proposal": proposal.to_dict(), "session_id": session_id})


@app.route("/api/checkout/accept", methods=["POST"])
def checkout_accept():
    body = request.get_json(force=True)
    product_id = body.get("product_id")
    session_id = session.get("session_id") or str(uuid.uuid4())

    # Idempotency: if this exact session already has a created order for
    # this exact product, don't create a second one. Covers double-clicks,
    # a page refresh resubmitting the last request, or a client-side retry
    # after a slow response the user thought had failed.
    existing = audit.find_order_for(session_id, product_id)
    if existing:
        return jsonify({
            "ok": True,
            "order_id": existing["order_id"],
            "amount_inr": existing["amount_inr_paise"] / 100,
            "status": "already_created",
            "idempotent": True,
        })

    audit.log_user_decision(session_id, product_id, accepted=True)

    product = get_product(product_id)
    if not product:
        return jsonify({"ok": False, "error": f"Unknown product_id {product_id}"}), 400

    # Policy gate #2: independent of the catalog-price bound, cap total
    # accepted spend per session so an agent (or a user rapidly accepting
    # many proposals) can't run past a merchant-configured ceiling.
    current_total = audit.session_accepted_total_paise(session_id)
    if would_exceed_session_cap(session_id, product["price_inr"], current_total):
        audit.log_blocked_by_policy(
            session_id, product_id,
            policy="session_spend_cap",
            detail=f"would reach {(current_total + product['price_inr'])/100:.0f} INR, cap is {SESSION_SPEND_CAP_PAISE/100:.0f} INR",
        )
        return jsonify({
            "ok": False,
            "error": "This session has hit its upsell spending limit — no order was created.",
        }), 200

    try:
        checkout = get_checkout_client()
        order = checkout.create_order_for_product(
            product_id=product_id,
            amount_inr_paise=product["price_inr"],  # price comes from OUR catalog, not the LLM
            receipt=f"cartmate-{session_id[:8]}-{product_id}",
        )
    except OrderCreationError as exc:
        # Graceful failure #3: Razorpay order creation failed even after
        # retry. Log it, and hand the frontend a clear, non-technical
        # message instead of a stack trace.
        audit.log_order_failed(session_id, product_id, str(exc))
        return jsonify({
            "ok": False,
            "error": "We couldn't set up payment for that item right now. Please try again in a moment.",
        }), 200

    audit.log_order_created(session_id, product_id, order.order_id, order.amount_inr_paise)

    return jsonify({
        "ok": True,
        "order_id": order.order_id,
        "amount_inr": order.amount_inr_paise / 100,
        "status": order.status,
    })


@app.route("/api/checkout/decline", methods=["POST"])
def checkout_decline():
    body = request.get_json(force=True)
    product_id = body.get("product_id")
    session_id = session.get("session_id") or str(uuid.uuid4())
    audit.log_user_decision(session_id, product_id, accepted=False)
    return jsonify({"ok": True})


@app.route("/api/audit/<session_id>")
def get_audit(session_id):
    return jsonify(audit.read_audit_trail(session_id=session_id))


@app.route("/.well-known/agent-catalog.json")
def agent_readable_catalog():
    """
    A machine-readable catalog + capability manifest for external AI
    shopping agents (agent-to-agent commerce — the 'why now' this track
    is built around: NPCI's UAP and the ACP/AP2/x402 protocol race).

    Deliberately NOT a general-purpose "call anything" surface: it
    describes what data an agent can read, and states plainly that no
    money action is exposed here at all — any purchase still has to go
    through /api/checkout/accept, which is gated by session state, not
    by anything an external agent can drive directly. An agent can look,
    it cannot spend.
    """
    catalog = load_catalog()
    return jsonify({
        "schema_version": "cartmate-agent-catalog-v1",
        "merchant_name": catalog["merchant_name"],
        "currency": "INR",
        "products": [
            {
                "id": p["id"],
                "name": p["name"],
                "price_inr": p["price_inr"] / 100,
                "category": p["category"],
                "description": p["description"],
            }
            for p in catalog["products"]
        ],
        "capabilities": {
            "read": ["GET /.well-known/agent-catalog.json"],
            "propose_only": ["POST /api/propose — returns a suggestion, moves no money"],
            "money_actions": "NONE exposed to external agents. All purchases require a "
                              "human-gated session via the CartMate web UI; this manifest "
                              "intentionally does not expose a checkout capability.",
        },
    })


@app.route("/api/metrics")
def metrics():
    """Honest, computed-from-the-audit-log metrics — no invented numbers.
    Useful for the pitch: shows the agent's actual measured impact
    instead of a claimed one."""
    all_entries = audit.read_audit_trail()
    proposals = [e for e in all_entries if e["type"] == "proposal" and e.get("product_id")]
    decisions = [e for e in all_entries if e["type"] == "user_decision"]
    accepted = [e for e in decisions if e.get("accepted")]
    orders = [e for e in all_entries if e["type"] == "order_created"]
    failures = [e for e in all_entries if e["type"] in ("order_failed", "invalid_proposal", "blocked_by_policy")]

    total_revenue_paise = sum(o.get("amount_inr_paise", 0) for o in orders)
    acceptance_rate = (len(accepted) / len(decisions)) if decisions else None

    return jsonify({
        "total_proposals_shown": len(proposals),
        "total_decisions": len(decisions),
        "accepted": len(accepted),
        "declined": len(decisions) - len(accepted),
        "acceptance_rate": round(acceptance_rate, 3) if acceptance_rate is not None else None,
        "orders_created": len(orders),
        "total_upsell_revenue_inr": total_revenue_paise / 100,
        "failure_events": len(failures),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
