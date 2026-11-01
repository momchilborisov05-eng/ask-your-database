# Business rules: sample online shop

These rules are sent to the model with the database schema. Replace this file
(or point ASKDB_CONTEXT at your own) to teach the assistant your company's
definitions; no code changes are needed.

- **Revenue** means the sum of `order_items.quantity * order_items.unit_price_eur`
  for orders whose status is not `cancelled` or `returned`, unless the question
  says otherwise.
- **Gross profit** is revenue minus `quantity * products.cost_eur`; **margin** is
  gross profit divided by revenue.
- **Return rate** is returned orders divided by all orders that were not cancelled.
- An **active customer** placed at least one order in the period.
- `products.price_eur` is today's list price; `order_items.unit_price_eur` is what
  the customer actually paid (it includes discounts). Use the latter for revenue.
- The financial year is the calendar year. Weeks start on Monday.
- All money is in EUR.
