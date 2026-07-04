"""Seed the DuckDB warehouse with a deterministic e-commerce dataset.

Run:  python data/seed.py
Creates data/warehouse.duckdb with 5 related tables (~3k orders).
"""
import os
import random
from datetime import date, timedelta

import duckdb

DB_PATH = os.path.join(os.path.dirname(__file__), "warehouse.duckdb")

FIRST = ["Aarav", "Diya", "Vihaan", "Ananya", "Ishaan", "Meera", "Kabir", "Sara",
         "Liam", "Emma", "Noah", "Olivia", "Mateo", "Sofia", "Yuki", "Chen",
         "Fatima", "Omar", "Elena", "Lucas"]
LAST = ["Sharma", "Patel", "Iyer", "Khan", "Reddy", "Gupta", "Smith", "Garcia",
        "Kim", "Tanaka", "Muller", "Rossi", "Silva", "Nair", "Das", "Bose"]
COUNTRIES = ["India", "United States", "Germany", "Japan", "Brazil", "United Kingdom", "Australia"]
SEGMENTS = ["consumer", "small_business", "enterprise"]
CHANNELS = ["web", "mobile_app", "marketplace"]
ORDER_STATUS = ["completed", "shipped", "processing", "cancelled", "returned"]
PAY_METHODS = ["credit_card", "upi", "paypal", "bank_transfer", "wallet"]
PAY_STATUS = ["captured", "pending", "failed", "refunded"]

CATALOG = {
    "Electronics": [("Wireless Earbuds", 79.99), ("Mechanical Keyboard", 129.0), ("4K Monitor", 349.0),
                    ("USB-C Hub", 45.0), ("Smart Speaker", 99.0), ("Webcam Pro", 89.0),
                    ("Portable SSD 1TB", 119.0), ("Noise-Cancel Headphones", 249.0)],
    "Home & Kitchen": [("Espresso Machine", 199.0), ("Air Fryer", 149.0), ("Chef Knife Set", 89.0),
                       ("Robot Vacuum", 299.0), ("Cast Iron Pan", 39.0), ("Blender Max", 79.0)],
    "Sports": [("Yoga Mat", 29.0), ("Running Shoes", 119.0), ("Dumbbell Set", 149.0),
               ("Cycling Helmet", 59.0), ("Resistance Bands", 19.0)],
    "Books": [("Data Engineering 101", 42.0), ("The Startup Playbook", 24.0),
              ("Deep Learning Illustrated", 55.0), ("Mystery of the Nile", 14.0)],
    "Beauty": [("Vitamin C Serum", 25.0), ("Sunscreen SPF50", 18.0), ("Hair Dryer Ionic", 69.0)],
}

START = date(2024, 1, 1)
END = date(2026, 6, 30)
DAYS = (END - START).days


def main(db_path: str = DB_PATH) -> None:
    rng = random.Random(42)
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    con = duckdb.connect(db_path)

    con.execute("""
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            name        VARCHAR NOT NULL,
            email       VARCHAR NOT NULL,
            country     VARCHAR NOT NULL,
            segment     VARCHAR NOT NULL,
            signup_date DATE NOT NULL
        );
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY,
            name       VARCHAR NOT NULL,
            category   VARCHAR NOT NULL,
            price      DECIMAL(10,2) NOT NULL,
            cost       DECIMAL(10,2) NOT NULL
        );
        CREATE TABLE orders (
            order_id    INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
            order_date  DATE NOT NULL,
            status      VARCHAR NOT NULL,
            channel     VARCHAR NOT NULL
        );
        CREATE TABLE order_items (
            order_item_id INTEGER PRIMARY KEY,
            order_id      INTEGER NOT NULL REFERENCES orders(order_id),
            product_id    INTEGER NOT NULL REFERENCES products(product_id),
            quantity      INTEGER NOT NULL,
            unit_price    DECIMAL(10,2) NOT NULL,
            discount      DECIMAL(4,2) NOT NULL
        );
        CREATE TABLE payments (
            payment_id INTEGER PRIMARY KEY,
            order_id   INTEGER NOT NULL REFERENCES orders(order_id),
            amount     DECIMAL(12,2) NOT NULL,
            method     VARCHAR NOT NULL,
            status     VARCHAR NOT NULL,
            paid_at    TIMESTAMP NOT NULL
        );
    """)

    customers = []
    for cid in range(1, 501):
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        email = f"{name.lower().replace(' ', '.')}{cid}@example.com"
        signup = START + timedelta(days=rng.randint(0, DAYS - 90))
        customers.append((cid, name, email, rng.choice(COUNTRIES),
                          rng.choices(SEGMENTS, weights=[70, 20, 10])[0], signup))
    con.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", customers)

    products = []
    pid = 1
    for category, items in CATALOG.items():
        for name, price in items:
            cost = round(price * rng.uniform(0.45, 0.7), 2)
            products.append((pid, name, category, price, cost))
            pid += 1
    con.executemany("INSERT INTO products VALUES (?,?,?,?,?)", products)

    orders, items, payments = [], [], []
    oiid, payid = 1, 1
    for oid in range(1, 3001):
        cust = rng.choice(customers)
        earliest = max(0, (cust[5] - START).days)
        odate = START + timedelta(days=rng.randint(earliest, DAYS))
        status = rng.choices(ORDER_STATUS, weights=[55, 15, 10, 12, 8])[0]
        orders.append((oid, cust[0], odate, status, rng.choice(CHANNELS)))

        total = 0.0
        for _ in range(rng.randint(1, 4)):
            prod = rng.choice(products)
            qty = rng.randint(1, 3)
            disc = rng.choices([0.0, 0.05, 0.10, 0.20], weights=[60, 20, 15, 5])[0]
            items.append((oiid, oid, prod[0], qty, prod[3], disc))
            total += qty * float(prod[3]) * (1 - disc)
            oiid += 1

        if status != "cancelled":
            pstatus = "refunded" if status == "returned" else \
                rng.choices(PAY_STATUS[:3], weights=[85, 10, 5])[0]
            paid_at = odate + timedelta(hours=rng.randint(0, 48))
            payments.append((payid, oid, round(total, 2), rng.choice(PAY_METHODS),
                             pstatus, paid_at))
            payid += 1

    con.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)
    con.executemany("INSERT INTO order_items VALUES (?,?,?,?,?,?)", items)
    con.executemany("INSERT INTO payments VALUES (?,?,?,?,?,?)", payments)
    con.close()

    print(f"Seeded {db_path}")
    print(f"  customers: {len(customers)}, products: {len(products)}, "
          f"orders: {len(orders)}, order_items: {len(items)}, payments: {len(payments)}")


if __name__ == "__main__":
    main()
