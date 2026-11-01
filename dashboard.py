"""
Hand-written SQL for the dashboard of the sample shop database. Each query is
shown in the interface, so visitors can see the SQL behind every number and
chart. Every query exists in SQLite and in T-SQL (Azure SQL, schema "shop").
"""

from safe_sql import UnsafeQuery

# Orders that bring revenue: not cancelled and not returned.
VALID = "o.status NOT IN ('cancelled', 'returned')"

QUERIES = {
    "kpis": {
        "title": "Last 30 days",
        "sql": f"""
SELECT
    ROUND(SUM(i.quantity * i.unit_price_eur), 2)                                     AS revenue_eur,
    COUNT(DISTINCT o.id)                                                              AS orders,
    ROUND(SUM(i.quantity * i.unit_price_eur) / COUNT(DISTINCT o.id), 2)               AS avg_order_eur,
    COUNT(DISTINCT o.customer_id)                                                     AS customers,
    ROUND(SUM(i.quantity * (i.unit_price_eur - p.cost_eur)), 2)                       AS gross_profit_eur
FROM orders o
JOIN order_items i ON i.order_id = o.id
JOIN products p    ON p.id = i.product_id
WHERE {VALID}
  AND o.ordered_at >= date((SELECT MAX(ordered_at) FROM orders), '-30 days')""",
        "tsql": f"""
SELECT
    ROUND(SUM(i.quantity * i.unit_price_eur), 2)                                     AS revenue_eur,
    COUNT(DISTINCT o.id)                                                              AS orders,
    ROUND(SUM(i.quantity * i.unit_price_eur) / COUNT(DISTINCT o.id), 2)               AS avg_order_eur,
    COUNT(DISTINCT o.customer_id)                                                     AS customers,
    ROUND(SUM(i.quantity * (i.unit_price_eur - p.cost_eur)), 2)                       AS gross_profit_eur
FROM shop.orders o
JOIN shop.order_items i ON i.order_id = o.id
JOIN shop.products p    ON p.id = i.product_id
WHERE {VALID}
  AND o.ordered_at >= DATEADD(day, -30, CAST((SELECT MAX(ordered_at) FROM shop.orders) AS date))""",
    },
    "monthly": {
        "title": "Monthly revenue and growth",
        # LAG() is a window function: it reads the previous month's value to compute growth.
        "sql": f"""
WITH monthly AS (
    SELECT strftime('%Y-%m', o.ordered_at) AS month,
           ROUND(SUM(i.quantity * i.unit_price_eur), 2) AS revenue_eur
    FROM orders o
    JOIN order_items i ON i.order_id = o.id
    WHERE {VALID}
    GROUP BY month
)
SELECT month,
       revenue_eur,
       ROUND(100.0 * (revenue_eur - LAG(revenue_eur) OVER (ORDER BY month))
             / LAG(revenue_eur) OVER (ORDER BY month), 1) AS growth_pct
FROM monthly
ORDER BY month""",
        "tsql": f"""
WITH monthly AS (
    SELECT FORMAT(o.ordered_at, 'yyyy-MM') AS month,
           ROUND(SUM(i.quantity * i.unit_price_eur), 2) AS revenue_eur
    FROM shop.orders o
    JOIN shop.order_items i ON i.order_id = o.id
    WHERE {VALID}
    GROUP BY FORMAT(o.ordered_at, 'yyyy-MM')
)
SELECT month,
       revenue_eur,
       ROUND(100.0 * (revenue_eur - LAG(revenue_eur) OVER (ORDER BY month))
             / LAG(revenue_eur) OVER (ORDER BY month), 1) AS growth_pct
FROM monthly
ORDER BY month""",
    },
    "categories": {
        "title": "Revenue and margin by category",
        "sql": f"""
SELECT c.name AS category,
       ROUND(SUM(i.quantity * i.unit_price_eur), 2) AS revenue_eur,
       ROUND(100.0 * SUM(i.quantity * (i.unit_price_eur - p.cost_eur))
             / SUM(i.quantity * i.unit_price_eur), 1) AS margin_pct
FROM orders o
JOIN order_items i ON i.order_id = o.id
JOIN products p    ON p.id = i.product_id
JOIN categories c  ON c.id = p.category_id
WHERE {VALID}
GROUP BY c.name
ORDER BY revenue_eur DESC""",
        "tsql": f"""
SELECT c.name AS category,
       ROUND(SUM(i.quantity * i.unit_price_eur), 2) AS revenue_eur,
       ROUND(100.0 * SUM(i.quantity * (i.unit_price_eur - p.cost_eur))
             / SUM(i.quantity * i.unit_price_eur), 1) AS margin_pct
FROM shop.orders o
JOIN shop.order_items i ON i.order_id = o.id
JOIN shop.products p    ON p.id = i.product_id
JOIN shop.categories c  ON c.id = p.category_id
WHERE {VALID}
GROUP BY c.name
ORDER BY revenue_eur DESC""",
    },
    "top_products": {
        "title": "Top 5 products by revenue",
        "sql": f"""
SELECT p.name AS product,
       SUM(i.quantity) AS units_sold,
       ROUND(SUM(i.quantity * i.unit_price_eur), 2) AS revenue_eur,
       RANK() OVER (ORDER BY SUM(i.quantity * i.unit_price_eur) DESC) AS rank
FROM orders o
JOIN order_items i ON i.order_id = o.id
JOIN products p    ON p.id = i.product_id
WHERE {VALID}
GROUP BY p.id
ORDER BY revenue_eur DESC
LIMIT 5""",
        "tsql": f"""
SELECT TOP (5)
       p.name AS product,
       SUM(i.quantity) AS units_sold,
       ROUND(SUM(i.quantity * i.unit_price_eur), 2) AS revenue_eur,
       RANK() OVER (ORDER BY SUM(i.quantity * i.unit_price_eur) DESC) AS [rank]
FROM shop.orders o
JOIN shop.order_items i ON i.order_id = o.id
JOIN shop.products p    ON p.id = i.product_id
WHERE {VALID}
GROUP BY p.id, p.name
ORDER BY revenue_eur DESC""",
    },
    "cities": {
        "title": "Revenue by city",
        "sql": f"""
SELECT cu.city,
       COUNT(DISTINCT cu.id) AS customers,
       ROUND(SUM(i.quantity * i.unit_price_eur), 2) AS revenue_eur
FROM orders o
JOIN customers cu  ON cu.id = o.customer_id
JOIN order_items i ON i.order_id = o.id
WHERE {VALID}
GROUP BY cu.city
ORDER BY revenue_eur DESC""",
        "tsql": f"""
SELECT cu.city,
       COUNT(DISTINCT cu.id) AS customers,
       ROUND(SUM(i.quantity * i.unit_price_eur), 2) AS revenue_eur
FROM shop.orders o
JOIN shop.customers cu  ON cu.id = o.customer_id
JOIN shop.order_items i ON i.order_id = o.id
WHERE {VALID}
GROUP BY cu.city
ORDER BY revenue_eur DESC""",
    },
}

EXAMPLE_QUESTIONS = [
    "What were the 5 best-selling products in September 2026?",
    "How did revenue in Black Friday week 2025 compare with the week before?",
    "Which customers spent the most in total, and how many orders did they place?",
    "What share of orders comes from the mobile app each month?",
    "Which products are low on stock but sold well in the last 30 days?",
    "What is the return rate by category?",
    "Which city has the highest average order value?",
    "How many new customers signed up each month in 2026?",
]


def load_dashboard(source) -> dict | None:
    """The dashboard cards, or None when the database isn't the sample shop."""
    key = "tsql" if source.dialect == "mssql" else "sql"
    data = {}
    for name, query in QUERIES.items():
        try:
            result = source.run(query[key])
        except UnsafeQuery:
            return None
        data[name] = {"title": query["title"], "sql": query[key].strip(),
                      "columns": result.columns, "rows": result.rows}
    return data
