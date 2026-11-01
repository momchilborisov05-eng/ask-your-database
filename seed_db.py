"""
Creates the sample database of a fictional online electronics and home goods
shop in Bulgaria, with customers, products, orders, order items and payments.

The data is random but repeatable (fixed seed), so everyone gets the same numbers.

Run:
    python seed_db.py                                    # shop.db (SQLite)
    python seed_db.py --target "mssql://<server>.database.windows.net/<db>"
                                                         # Azure SQL, schema "shop"
                                                         # (signed in with your Azure CLI login)
"""

import argparse
import random
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

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

TABLES = ["categories", "products", "customers", "orders", "order_items", "payments"]

# The same tables in T-SQL, in their own schema so a read-only login can be given just this schema.
MSSQL_SCHEMA = [
    "IF SCHEMA_ID('shop') IS NULL EXEC('CREATE SCHEMA shop')",
    *[f"DROP TABLE IF EXISTS shop.{t}" for t in reversed(TABLES)],
    """CREATE TABLE shop.categories (
        id    INT          NOT NULL PRIMARY KEY,
        name  NVARCHAR(50) NOT NULL UNIQUE
    )""",
    """CREATE TABLE shop.products (
        id           INT           NOT NULL PRIMARY KEY,
        name         NVARCHAR(100) NOT NULL,
        category_id  INT           NOT NULL REFERENCES shop.categories(id),
        price_eur    DECIMAL(10,2) NOT NULL,
        cost_eur     DECIMAL(10,2) NOT NULL,
        stock        INT           NOT NULL
    )""",
    """CREATE TABLE shop.customers (
        id          INT           NOT NULL PRIMARY KEY,
        first_name  NVARCHAR(50)  NOT NULL,
        last_name   NVARCHAR(50)  NOT NULL,
        email       NVARCHAR(200) NOT NULL UNIQUE,
        city        NVARCHAR(50)  NOT NULL,
        signed_up   DATE          NOT NULL
    )""",
    """CREATE TABLE shop.orders (
        id           INT          NOT NULL PRIMARY KEY,
        customer_id  INT          NOT NULL REFERENCES shop.customers(id),
        ordered_at   DATETIME2(0) NOT NULL,
        status       VARCHAR(20)  NOT NULL CHECK (status IN ('delivered', 'shipped', 'cancelled', 'returned')),
        channel      VARCHAR(20)  NOT NULL CHECK (channel IN ('web', 'mobile_app'))
    )""",
    """CREATE TABLE shop.order_items (
        id              INT           NOT NULL PRIMARY KEY,
        order_id        INT           NOT NULL REFERENCES shop.orders(id),
        product_id      INT           NOT NULL REFERENCES shop.products(id),
        quantity        INT           NOT NULL,
        unit_price_eur  DECIMAL(10,2) NOT NULL
    )""",
    """CREATE TABLE shop.payments (
        id          INT           NOT NULL PRIMARY KEY,
        order_id    INT           NOT NULL REFERENCES shop.orders(id),
        method      VARCHAR(20)   NOT NULL CHECK (method IN ('card', 'cash_on_delivery', 'bank_transfer')),
        amount_eur  DECIMAL(10,2) NOT NULL,
        paid_at     DATETIME2(0)  NOT NULL
    )""",
    "CREATE INDEX ix_orders_customer ON shop.orders(customer_id)",
    "CREATE INDEX ix_orders_date ON shop.orders(ordered_at)",
    "CREATE INDEX ix_items_order ON shop.order_items(order_id)",
    "CREATE INDEX ix_items_product ON shop.order_items(product_id)",
]

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


def generate(seed: int = 2026) -> dict[str, list[tuple]]:
    """Rows for every table, the same for every run with the same seed."""
    rng = random.Random(seed)
    data: dict[str, list[tuple]] = {t: [] for t in TABLES}

    # Categories and products
    products = []
    for cat_id, (category, items) in enumerate(CATALOG.items(), start=1):
        data["categories"].append((cat_id, category))
        for name, price in items:
            cost = round(price * rng.uniform(0.55, 0.78), 2)
            products.append((len(products) + 1, name, cat_id, float(price), cost, rng.randint(0, 120)))
    data["products"] = products

    # Customers
    cities = [c for c, w in CITIES for _ in range(w)]
    customers = []
    for cid in range(1, 801):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        signed = START - timedelta(days=200) + timedelta(days=rng.randint(0, (END - START).days + 150))
        customers.append((cid, first, last, f"{first}.{last}.{cid}@example.com".lower(), rng.choice(cities),
                          min(signed, END).isoformat()))
    data["customers"] = customers

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
            data["orders"].append((order_id, customer[0], when.isoformat(sep=" ", timespec="minutes"),
                                   status, channel))

            total = 0.0
            for product in rng.sample(rng.choices(products, product_weights, k=6), rng.choices([1, 2, 3], [70, 22, 8])[0]):
                item_id += 1
                qty = rng.choices([1, 2, 3], [82, 14, 4])[0]
                discount = 0.85 if day.month == 11 and day.day >= 20 else 1.0  # Black Friday week
                unit = round(product[3] * discount, 2)
                total += unit * qty
                data["order_items"].append((item_id, order_id, product[0], qty, unit))

            if status != "cancelled":
                payment_id += 1
                method = rng.choices(["card", "cash_on_delivery", "bank_transfer"], [55, 38, 7])[0]
                data["payments"].append((payment_id, order_id, method, round(total, 2),
                                         when.isoformat(sep=" ", timespec="minutes")))
        day += timedelta(days=1)
    return data


def build(path: Path = DB_PATH, seed: int = 2026) -> None:
    data = generate(seed)
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA)
        for table, rows in data.items():
            db.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in rows[0])})", rows)
        db.commit()
    print(f"Created {path.name}: " + ", ".join(f"{len(rows)} {t}" for t, rows in data.items()))


def build_mssql(url: str, seed: int = 2026) -> None:
    """Load the same data into Azure SQL / SQL Server, schema "shop" (replacing it)."""
    from safe_sql import connect_mssql

    parsed = urlparse(url)
    database = parsed.path.lstrip("/")
    if parsed.scheme != "mssql" or not parsed.hostname or not database:
        raise SystemExit("Use mssql://<server>.database.windows.net/<database>")
    data = generate(seed)
    # Azure SQL wants real dates; SQLite keeps them as ISO text.
    data["customers"] = [r[:5] + (date.fromisoformat(r[5]),) for r in data["customers"]]
    data["orders"] = [r[:2] + (datetime.fromisoformat(r[2]),) + r[3:] for r in data["orders"]]
    data["payments"] = [r[:4] + (datetime.fromisoformat(r[4]),) for r in data["payments"]]

    conn = connect_mssql(f"Server=tcp:{parsed.hostname},{parsed.port or 1433};Database={database};"
                         "Encrypt=yes;TrustServerCertificate=no;Authentication=ActiveDirectoryDefault")
    try:
        cur = conn.cursor()
        for statement in MSSQL_SCHEMA:
            cur.execute(statement)
        for table, rows in data.items():
            marks = ", ".join("?" for _ in rows[0])
            for i in range(0, len(rows), 1000):
                cur.executemany(f"INSERT INTO shop.{table} VALUES ({marks})", rows[i:i + 1000])
        conn.commit()
    finally:
        conn.close()
    print(f"Loaded into {database}, schema shop: " + ", ".join(f"{len(rows)} {t}" for t, rows in data.items()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create the sample shop database.")
    parser.add_argument("--target", help="mssql://<server>.database.windows.net/<db> (default: shop.db)")
    args = parser.parse_args()
    if args.target:
        build_mssql(args.target)
    else:
        build()
