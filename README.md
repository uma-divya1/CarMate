# CartMate — Upsell & Cross-Sell Agent (Razorpay AI Buildathon — Track 01)

CartMate is an AI agent that looks at a customer's cart on a Razorpay
merchant's store and proposes **one** relevant upsell — then, only if
the customer accepts, creates a real Razorpay **test-mode** order for
it. Built for Track 01: AI Growth & Agentic Commerce.

## The problem it solves

Merchants lose easy incremental revenue because manual cross-sell
("customers who bought X also bought Y") is either absent on small
storefronts or applied as generic, un-targeted banners. CartMate
replaces that with a reasoning agent that looks at the *actual* cart
contents and proposes one specific, explainable add-on — the same
motion a good in-store salesperson would make, but automated.

## Why this satisfies "the bar" for Track 01

The brief requires: **"Every money action explainable, bounded and
gated. Show the audit trail and one failure handled gracefully."**
This isn't just met — it's met with *layered*, independent
mechanisms, because a single safeguard is a single point of failure:

| Requirement | Where | Why it's layered, not single-point |
|---|---|---|
| **Explainable** | Every proposal includes a one-sentence `reason` from the LLM, plus `/api/metrics` shows measured acceptance rate and revenue captured — not just a claimed rationale. | Explains both the *individual* decision and the *aggregate* impact. |
| **Bounded (price)** | The LLM only ever returns a `product_id`; `razorpay_client.py` always resolves the real amount from `catalog.json`. | Model output can never become a monetary value. |
| **Bounded (spend)** | `policy.py` enforces a per-session spend cap (`SESSION_SPEND_CAP_PAISE`) independent of per-item pricing — checked in `/api/checkout/accept` before Razorpay is ever called. | Catches a *different* failure mode: many correctly-priced items adding up past a sane ceiling. |
| **Bounded (rate)** | `policy.py`'s `RateLimiter` caps `/api/propose` calls per session. | Stops abuse of the LLM endpoint itself, unrelated to money. |
| **Gated** | The agent has no `create_order` tool. Money only moves in `/api/checkout/accept`, on an explicit user action. The agent-readable catalog (`/.well-known/agent-catalog.json`) exposes **read-only** capabilities to external agents by design — no checkout capability is published there at all. | Gates both human users *and* external AI agents from moving money without an explicit, logged step. |
| **Idempotent** | `/api/checkout/accept` checks `audit.find_order_for()` before creating anything — a double-click, refresh, or client retry returns the existing order instead of creating a duplicate. | A real bug in the first version of this project — see "What broke" below. |
| **Audit trail** | Every proposal, decision, order, failure, hallucination, and policy block is appended to `audit_log.jsonl`, viewable live at `/api/audit/<session_id>`. | Six distinct event types, not just success/failure. |
| **Failure #1 — payments** | `razorpay_client.py` retries once with backoff, then raises a structured `OrderCreationError` handled by `app.py` as a plain-language message, not a crash. | `TestRazorpayCheckoutFailureHandling` |
| **Failure #2 — model hallucination** | If the LLM returns a `product_id` not in the catalog, `agent.py` flags `is_hallucinated=True` and `app.py` logs it via `audit.log_invalid_proposal()` instead of showing a broken product card. | `test_hallucinated_product_id_is_flagged` |
| **Failure #3 — policy block** | If an accepted proposal would exceed the session spend cap, it's rejected *before* touching Razorpay and logged as `blocked_by_policy`. | `TestPolicy` |

## Agent-to-agent commerce angle (Track 01's "why now")

The track brief calls out NPCI's UAP and the ACP/AP2/x402 protocol
race as the reason agent-to-agent commerce is "the open problem of
the year." `/.well-known/agent-catalog.json` is CartMate's answer to
the **"Agent-readable catalog"** example direction: a structured,
machine-readable product manifest an external AI shopping agent could
query directly — while explicitly publishing **no** money-moving
capability, so "agent-readable" doesn't silently become
"agent-spendable."

## Architecture

```
Browser (templates/index.html)
   |
   |  1. POST /api/propose  {cart_product_ids}
   v
Flask app (app.py)
   |
   |  2. agent.get_upsell_proposal()  -> Claude, forced tool call
   v
Claude (agent.py)  --- reads catalog.py for context, returns product_id + reason only
   |
   |  3. audit.log_proposal()
   v
Browser shows proposal -> user clicks Accept or Decline
   |
   |  4. POST /api/checkout/accept {product_id}
   v
Flask app -> catalog.get_product() for the REAL price
   |
   |  5. razorpay_client.create_order_for_product()  -> Razorpay TEST MODE order.create
   v
audit.log_order_created() / audit.log_order_failed()
```

## Tech stack

- **Backend:** Python, Flask (same stack as my [Habit Tracker](https://github.com/uma-divya1/Habit-Tracker) project)
- **Agent:** Anthropic Claude, tool use (forced function calling) — no free-text money decisions
- **Payments:** Razorpay Python SDK, test mode only
- **Storage:** flat JSON catalog + append-only JSONL audit log (no DB needed for this scope)
- **Tests:** pytest, with mocked LLM and Razorpay clients so the whole suite runs with zero API keys or network access

## Setup

```bash
git clone <your-repo-url>
cd cartmate
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your Razorpay TEST MODE keys + Anthropic key
python app.py
# open http://127.0.0.1:5000
```

## Running the tests

```bash
pytest tests/ -v
```

All 7 tests pass without any real API keys — the Anthropic and
Razorpay clients are mocked so the logic (bounded pricing, gated
checkout, retry-then-fail behavior, audit ordering) can be verified
in isolation.

## What broke, and how I got out of it

**Problem 1 — price hallucination risk.** My first version let the
LLM return both the product *and* a suggested price in the same tool
call — it seemed convenient since the model already "knew" the
catalog prices from context. But that meant a hallucinated or
malformed price from the model could flow straight into a real
Razorpay order amount, which fails the "bounded" requirement outright.

**Fix:** I removed `price` from the tool schema entirely. The LLM's
tool call now only carries `product_id` and `reason`; `app.py` always
re-resolves the price via `catalog.get_product()` right before calling
Razorpay. `test_proposal_price_comes_from_catalog_not_llm` asserts the
exact price regardless of what the (mocked) LLM says, since the code
path physically cannot use its output as an amount.

**Problem 2 — a real double-order bug.** While writing the retry logic
for Razorpay failures, I realized the retry itself introduced a new
risk: if a request actually succeeded upstream but the response was
lost (timeout, dropped connection), retrying — or a user double-
clicking "Accept," or a page refresh resubmitting the last action —
could create a *second* real order for the same upsell.

**Fix:** `/api/checkout/accept` now checks `audit.find_order_for()`
for an existing order on that exact session+product before calling
Razorpay at all, and returns the existing order instead of creating a
new one. It's an app-level idempotency check rather than a payment-
gateway-level idempotency key, which is a real limitation I'd flag
honestly in a production system — but it directly closes the gap for
the concrete failure mode (double-click / refresh / client retry)
that a demo or an early pilot would actually hit.

## Possible extensions

- Real "frequently bought together" signal from order history instead of the static `pairs_well_with` hints in `catalog.json`
- Auto-approve proposals under a rupee threshold, escalating larger ones to a human — same gated pattern, adjustable bound (the session cap in `policy.py` is a first step toward this)
- Multi-turn negotiation ("no thanks" -> agent tries a cheaper alternative once, then stops)
- Move `RateLimiter` and idempotency checks to Redis so they work correctly across multiple gunicorn workers in production, not just single-instance
- A real payment-gateway-level idempotency key (once available for the specific Razorpay order API in use) instead of the current app-level session+product check

## API surface (for reviewers / quick reference)

| Route | Method | What it does |
|---|---|---|
| `/` | GET | Demo UI |
| `/api/propose` | POST | Agent proposes one upsell for a cart. No money moves. Rate-limited per session. |
| `/api/checkout/accept` | POST | User accepts a proposal. Idempotent, spend-capped, then creates a real Razorpay test-mode order. |
| `/api/checkout/decline` | POST | Logs a decline. No money moves. |
| `/api/audit/<session_id>` | GET | Full audit trail for a session. |
| `/.well-known/agent-catalog.json` | GET | Machine-readable catalog for external AI shopping agents. Read-only — no checkout capability exposed. |
| `/api/metrics` | GET | Computed-from-the-audit-log metrics: acceptance rate, revenue captured, failure counts. |

## Pitch video outline (5 min)

1. **0:00–0:40** — The problem: small merchants can't do targeted upsell like large e-commerce platforms, and NPCI's UAP + the ACP/AP2/x402 protocol race means agents will soon be buying on merchants' behalf too — so the same system needs to be safe for both a human and an agent to transact through.
2. **0:40–1:50** — Live demo: build a cart, get a proposal with its reason, accept it, show the real test-mode order ID.
3. **1:50–2:30** — Hit `/api/metrics` on screen: show real acceptance rate and revenue captured from the session — not a claimed number, a computed one.
4. **2:30–3:15** — Show the audit trail live, and point out it has *six* distinct event types, not just success/failure — proposal, decision, order, payment failure, model hallucination, policy block.
5. **3:15–4:00** — Trigger the failure path (kill network / bad key) and show the graceful error + retry + audit log entry, instead of a crash.
6. **4:00–4:40** — Hit `/.well-known/agent-catalog.json` and explain: this is what an external AI shopping agent would see — and point out it has zero checkout capability on purpose.
7. **4:40–5:00** — Close on the double-order bug story: "I found a real idempotency bug while building this — here's the fix, and here's the test that proves it." This is the single strongest AI-judgment/failure-recovery beat in the whole video — end on it.
