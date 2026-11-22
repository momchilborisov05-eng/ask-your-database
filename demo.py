"""
Offline mode (no ANTHROPIC_API_KEY, or DEMO_MODE=1): a stand-in for the Claude client,
so the app works with no key and no network.

1. The eight example questions have SQL written in advance for the sample shop.
2. Any other question goes to the offline NL->SQL engine (sqlkit.nl2sql), which
   answers common metric-by-dimension questions from the semantic layer and declines
   the rest with a reason.

The query still runs for real through the static check and safe_sql, and the result,
chart, history and audit log work as with the real API.
"""

import json
import re
import uuid

from anthropic.types.beta import BetaMessage

from dashboard import EXAMPLE_QUESTIONS
from sqlkit.nl2sql import Declined, describe_rows

VALID = "o.status NOT IN ('cancelled', 'returned')"
PLANS = {
    EXAMPLE_QUESTIONS[0]: ("bar", "product", "units_sold", f"""
WITH sold AS (
    SELECT p.name AS product, SUM(i.quantity) AS units_sold,
           RANK() OVER (ORDER BY SUM(i.quantity) DESC) AS rnk
    FROM orders o JOIN order_items i ON i.order_id = o.id JOIN products p ON p.id = i.product_id
    WHERE {VALID} AND strftime('%Y-%m', o.ordered_at) = '2026-09'
    GROUP BY p.id
)
SELECT product, units_sold FROM sold WHERE rnk <= 5 ORDER BY units_sold DESC, product"""),
    EXAMPLE_QUESTIONS[1]: ("bar", "period", "revenue_eur", f"""
SELECT CASE WHEN date(o.ordered_at) >= '2025-11-24' THEN 'Black Friday week' ELSE 'Week before' END AS period,
       COUNT(DISTINCT o.id) AS orders,
       ROUND(SUM(i.quantity * i.unit_price_eur), 2) AS revenue_eur
FROM orders o JOIN order_items i ON i.order_id = o.id
WHERE {VALID} AND date(o.ordered_at) BETWEEN '2025-11-17' AND '2025-11-30'
GROUP BY period ORDER BY MIN(o.ordered_at)"""),
    EXAMPLE_QUESTIONS[2]: ("bar", "customer", "total_spent_eur", f"""
SELECT c.first_name || ' ' || c.last_name AS customer, COUNT(DISTINCT o.id) AS orders,
       ROUND(SUM(i.quantity * i.unit_price_eur), 2) AS total_spent_eur
FROM customers c JOIN orders o ON o.customer_id = c.id JOIN order_items i ON i.order_id = o.id
WHERE {VALID}
GROUP BY c.id ORDER BY total_spent_eur DESC LIMIT 10"""),
    EXAMPLE_QUESTIONS[3]: ("line", "month", "mobile_share_pct", """
SELECT strftime('%Y-%m', ordered_at) AS month,
       ROUND(100.0 * SUM(channel = 'mobile_app') / COUNT(*), 1) AS mobile_share_pct
FROM orders GROUP BY month ORDER BY month"""),
    EXAMPLE_QUESTIONS[4]: ("bar", "product", "units_sold_30d", f"""
SELECT p.name AS product, p.stock, SUM(i.quantity) AS units_sold_30d
FROM products p JOIN order_items i ON i.product_id = p.id JOIN orders o ON o.id = i.order_id
WHERE {VALID} AND o.ordered_at >= date((SELECT MAX(ordered_at) FROM orders), '-30 days')
GROUP BY p.id HAVING p.stock < 20 ORDER BY units_sold_30d DESC LIMIT 10"""),
    EXAMPLE_QUESTIONS[5]: ("bar", "category", "return_rate_pct", """
SELECT c.name AS category,
       ROUND(100.0 * COUNT(DISTINCT CASE WHEN o.status = 'returned' THEN o.id END)
             / COUNT(DISTINCT o.id), 2) AS return_rate_pct
FROM orders o JOIN order_items i ON i.order_id = o.id
JOIN products p ON p.id = i.product_id JOIN categories c ON c.id = p.category_id
WHERE o.status <> 'cancelled'
GROUP BY c.name ORDER BY return_rate_pct DESC"""),
    EXAMPLE_QUESTIONS[6]: ("bar", "city", "avg_order_eur", f"""
WITH totals AS (
    SELECT o.id, cu.city, SUM(i.quantity * i.unit_price_eur) AS total
    FROM orders o JOIN customers cu ON cu.id = o.customer_id JOIN order_items i ON i.order_id = o.id
    WHERE {VALID} GROUP BY o.id
)
SELECT city, ROUND(AVG(total), 2) AS avg_order_eur, COUNT(*) AS orders
FROM totals GROUP BY city ORDER BY avg_order_eur DESC"""),
    EXAMPLE_QUESTIONS[7]: ("line", "month", "new_customers", """
SELECT strftime('%Y-%m', signed_up) AS month, COUNT(*) AS new_customers
FROM customers WHERE signed_up >= '2026-01-01' GROUP BY month ORDER BY month"""),
}


class DemoClient:
    """engine: an sqlkit OfflineEngine for questions beyond the examples (None: examples only)."""

    def __init__(self, engine=None):
        self.engine = engine
        self.beta = self
        self.messages = self

    def create(self, *, messages, output_config, **_):
        properties = output_config["format"]["schema"]["properties"]
        prompt = messages[0]["content"]
        if "answer" in properties:
            return _message({"answer": _summary(prompt)})
        question = prompt.split("Question:", 1)[-1].strip()
        plan = PLANS.get(question)
        if plan:
            chart, x, y, sql = plan
            return _message({"can_answer": True, "sql": sql.strip(), "chart": {"type": chart, "x": x, "y": y},
                             "explanation": "Offline mode: this query was written in advance for this example."})
        if self.engine is None:
            return _decline("Offline mode answers only the example questions here. "
                            "Set ANTHROPIC_API_KEY to ask anything.")
        try:
            offline = self.engine.plan(question)
        except Declined as e:
            return _decline(str(e))
        return _message({"can_answer": True, "sql": offline.sql, "chart": offline.chart,
                         "explanation": offline.explanation})


def _decline(reason: str) -> BetaMessage:
    return _message({"can_answer": False, "sql": "", "chart": {"type": "none", "x": "", "y": ""},
                     "explanation": reason})


def _summary(prompt: str) -> str:
    rows = json.loads(re.search(r"result rows:\n(.*)\n\n", prompt, re.S)[1])
    columns = list(rows[0]) if rows else []
    return "Offline mode (no AI): " + describe_rows(columns, [list(r.values()) for r in rows])


def _message(data: dict) -> BetaMessage:
    return BetaMessage.model_validate({
        "id": f"msg_demo_{uuid.uuid4().hex[:8]}", "type": "message", "role": "assistant", "model": "demo",
        "content": [{"type": "text", "text": json.dumps(data)}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    })
