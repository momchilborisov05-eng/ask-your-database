"""
Creates shop.db: a sample database for a fictional online electronics and home
goods shop in Bulgaria, with customers, products, orders, order items and payments.

The data is random but repeatable (fixed seed), so everyone gets the same numbers.

Run:
    python seed_db.py
"""

import random
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).with_name("shop.db")

SCHEMA = """
CREATE TABLE categories (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE
);

CREATE TABLE products (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    category_id  INTEGER NOT NULL REFERENCES categories(id),
    price_eur    REAL    NOT NULL,     -- current selling price
    cost_eur     REAL    NOT NULL,     -- what the shop pays the supplier
    stock        INTEGER NOT NULL      -- units in the warehouse today
);

CREATE TABLE customers (
    id          INTEGER PRIMARY KEY,
    first_name  TEXT NOT NULL,
    last_name   TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    city        TEXT NOT NULL,
    signed_up   TEXT NOT NULL          -- ISO date
);

CREATE TABLE orders (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    ordered_at   TEXT    NOT NULL,     -- ISO date and time
    status       TEXT    NOT NULL CHECK (status IN ('delivered', 'shipped', 'cancelled', 'returned')),
    channel      TEXT    NOT NULL CHECK (channel IN ('web', 'mobile_app'))
);

CREATE TABLE order_items (
    id              INTEGER PRIMARY KEY,
    order_id        INTEGER NOT NULL REFERENCES orders(id),
    product_id      INTEGER NOT NULL REFERENCES products(id),
    quantity        INTEGER NOT NULL,
    unit_price_eur  REAL    NOT NULL   -- price the customer paid per unit
);

CREATE TABLE payments (
    id        INTEGER PRIMARY KEY,
    order_id  INTEGER NOT NULL REFERENCES orders(id),
    method    TEXT    NOT NULL CHECK (method IN ('card', 'cash_on_delivery', 'bank_transfer')),
    amount_eur REAL   NOT NULL,
    paid_at   TEXT    NOT NULL
);

CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_date ON orders(ordered_at);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE INDEX idx_items_product ON order_items(product_id);
"""

CATALOG = {
    "Laptops": [("UltraBook 14", 899), ("WorkPro 15", 1199), ("Student Lite 13", 549), ("Gamer X 16", 1599), ("Creator 15 OLED", 1849)],
    "Phones": [("Nova 8", 449), ("Nova 8 Pro", 699), ("Pixel-like S", 379), ("Budget Phone A2", 159), ("Fold Z", 1399)],
    "Audio": [("Wireless Earbuds", 79), ("Noise-Cancel Headphones", 229), ("Bluetooth Speaker", 59), ("Soundbar 2.1", 189), ("Studio Monitor Pair", 349)],
    "Monitors": [("24\" Office Monitor", 139), ("27\" 4K Monitor", 329), ("34\" Ultrawide", 479), ("Portable 15\" Monitor", 169)],
    "Accessories": [("USB-C Hub", 39), ("Wireless Mouse", 25), ("Mechanical Keyboard", 89), ("Laptop Backpack", 49), ("Webcam HD", 59), ("Phone Case", 15), ("Charger 65W", 35)],
    "Smart Home": [("Smart Bulb 4-pack", 45), ("Robot Vacuum", 299), ("Smart Thermostat", 159), ("Video Doorbell", 129), ("Smart Plug", 19)],
    "Kitchen": [("Espresso Machine", 249), ("Air Fryer", 99), ("Blender Pro", 79), ("Electric Kettle", 35), ("Food Processor", 139)],
}

CITIES = [("Sofia", 40), ("Plovdiv", 14), ("Varna", 12), ("Burgas", 8), ("Ruse", 5), ("Stara Zagora", 5),
          ("Pleven", 4), ("Sliven", 3), ("Dobrich", 3), ("Shumen", 3), ("Veliko Tarnovo", 3)]
FIRST = ["Ivan", "Georgi", "Dimitar", "Nikolay", "Petar", "Hristo", "Stefan", "Martin", "Aleksandar", "Todor",
         "Maria", "Elena", "Ivanka", "Yordanka", "Petya", "Desislava", "Gergana", "Viktoria", "Nikol", "Kalina"]
LAST = ["Ivanov", "Georgiev", "Dimitrov", "Petrov", "Nikolov", "Hristov", "Stoyanov", "Todorov", "Iliev", "Angelov",
        "Kolev", "Marinov", "Popov", "Yordanov", "Vasilev"]

START = date(2025, 5, 1)
END = date(2026, 10, 25)


def build(path: Path = DB_PATH, seed: int = 2026) -> None:
    rng = random.Random(seed)
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA)

        # Categories and products
        products = []
        for cat_id, (category, items) in enumerate(CATALOG.items(), start=1):
            db.execute("INSERT INTO categories VALUES (?, ?)", (cat_id, category))
            for name, price in items:
                cost = round(price * rng.uniform(0.55, 0.78), 2)
                products.append((len(products) + 1, name, cat_id, float(price), cost, rng.randint(0, 120)))
        db.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?)", products)

        # Customers
        cities = [c for c, w in CITIES for _ in range(w)]
        customers = []
        for cid in range(1, 801):
            first, last = rng.choice(FIRST), rng.choice(LAST)
            signed = START - timedelta(days=200) + timedelta(days=rng.randint(0, (END - START).days + 150))
            customers.append((cid, first, last, f"{first}.{last}.{cid}@example.com".lower(), rng.choice(cities),
                              min(signed, END).isoformat()))
        db.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?)", customers)

        # Some customers are loyal and order much more often than others.
        weights = [rng.paretovariate(1.3) for _ in customers]
        # Cheap items sell more often than expensive ones.
        product_weights = [1 / (p[3] ** 0.55) for p in products]

        order_id = item_id = payment_id = 0
        day = START
        while day <= END:
            # Busier in November-December (Black Friday, Christmas) and growing over time.
            season = 1.9 if day.month in (11, 12) else 1.25 if day.month == 1 else 1.0
            growth = 1 + (day - START).days / 900
            for _ in range(int(rng.gauss(6, 2) * season * growth)):
                customer = rng.choices(customers, weights)[0]
                if customer[5] > day.isoformat():
                    continue  # customer had not signed up yet
                order_id += 1
                when = datetime.combine(day, datetime.min.time()) + timedelta(minutes=rng.randint(7 * 60, 23 * 60 + 59))
                status = rng.choices(["delivered", "shipped", "cancelled", "returned"], [88, 4, 5, 3])[0]
                if (END - day).days < 4 and status == "delivered":
                    status = "shipped"
                channel = rng.choices(["web", "mobile_app"], [60, 40] if day.year == 2025 else [48, 52])[0]
                db.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
                           (order_id, customer[0], when.isoformat(sep=" ", timespec="minutes"), status, channel))

                total = 0.0
                for product in rng.sample(rng.choices(products, product_weights, k=6), rng.choices([1, 2, 3], [70, 22, 8])[0]):
                    item_id += 1
                    qty = rng.choices([1, 2, 3], [82, 14, 4])[0]
                    discount = 0.85 if day.month == 11 and day.day >= 20 else 1.0  # Black Friday week
                    unit = round(product[3] * discount, 2)
                    total += unit * qty
                    db.execute("INSERT INTO order_items VALUES (?, ?, ?, ?, ?)", (item_id, order_id, product[0], qty, unit))

                if status != "cancelled":
                    payment_id += 1
                    method = rng.choices(["card", "cash_on_delivery", "bank_transfer"], [55, 38, 7])[0]
                    db.execute("INSERT INTO payments VALUES (?, ?, ?, ?, ?)",
                               (payment_id, order_id, method, round(total, 2), when.isoformat(sep=" ", timespec="minutes")))
            day += timedelta(days=1)
        db.commit()

        counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ["categories", "products", "customers", "orders", "order_items", "payments"]}
    print(f"Created {path.name}: " + ", ".join(f"{n} {t}" for t, n in counts.items()))


if __name__ == "__main__":
    build()
