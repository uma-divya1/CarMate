"""
policy.py

Two independent safety layers, deliberately kept separate from the
agent and from the Razorpay client so each is easy to reason about
and test on its own:

1. RateLimiter — cheap abuse protection on /api/propose. Doesn't
   touch money at all; just stops one session from hammering the LLM.

2. SESSION_SPEND_CAP_PAISE — a policy the checkout endpoint enforces
   BEFORE calling Razorpay. This is intentionally redundant with
   "price always comes from the catalog" (razorpay_client.py) — the
   catalog bound stops the agent from inventing a price, this bound
   stops a session from accepting an unbounded NUMBER of upsells even
   at correct catalog prices. Two different failure modes, two
   independent gates.
"""

import time
from collections import defaultdict
from typing import Dict

# ₹10,000 in paise — a merchant-configurable ceiling on how much a
# single session can spend via agent-proposed upsells, regardless of
# how many individual proposals get accepted.
SESSION_SPEND_CAP_PAISE = 1_000_000


class RateLimiter:
    """Very small in-memory limiter: N requests per session per window.
    Good enough for a single-instance demo; a real deployment would move
    this to Redis so it works across multiple gunicorn workers."""

    def __init__(self, max_requests: int = 5, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: Dict[str, list] = defaultdict(list)

    def allow(self, session_id: str) -> bool:
        now = time.time()
        hits = self._hits[session_id]
        # drop hits outside the window
        while hits and hits[0] < now - self.window_seconds:
            hits.pop(0)
        if len(hits) >= self.max_requests:
            return False
        hits.append(now)
        return True


propose_rate_limiter = RateLimiter(max_requests=5, window_seconds=60)


def would_exceed_session_cap(session_id: str, additional_amount_paise: int, current_total_paise: int) -> bool:
    return (current_total_paise + additional_amount_paise) > SESSION_SPEND_CAP_PAISE
