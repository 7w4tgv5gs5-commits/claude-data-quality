"""
generate_data.py

Creates (or recreates) the 'testdb' PostgreSQL database and populates it with
synthetic data using Faker. Data quality issues are deliberately seeded at
configurable rates to support data quality testing demonstrations.

Usage:
    python generate_data.py [--rows N] [--seed S] [--dbname NAME] [--reset]

Defaults: 200 customers, 80 products, 500 orders; seed=42; dbname=testdb
"""

import argparse
import random
import re
import subprocess
import sys
from datetime import date, datetime, timedelta

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from faker import Faker

# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rows",   type=int, default=200,    help="Number of customer rows (default 200)")
    p.add_argument("--seed",   type=int, default=42,     help="Random seed (default 42)")
    p.add_argument("--dbname", default="testdb",         help="Target database name (default testdb)")
    p.add_argument("--reset",  action="store_true",      help="Drop and recreate the database first")
    return p.parse_args()

# ── Database bootstrap ────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS customers (
    id          SERIAL PRIMARY KEY,
    first_name  VARCHAR(50),
    last_name   VARCHAR(50),
    email       VARCHAR(100),
    phone       VARCHAR(30),
    birthdate   DATE,
    signup_date DATE,
    country     VARCHAR(50)
);

CREATE TABLE IF NOT EXISTS products (
    id         SERIAL PRIMARY KEY,
    sku        VARCHAR(20),
    name       VARCHAR(100),
    category   VARCHAR(50),
    price      NUMERIC(10,2),
    stock      INT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
    id          SERIAL PRIMARY KEY,
    customer_id INT,
    product_id  INT,
    quantity    INT,
    unit_price  NUMERIC(10,2),
    ordered_at  TIMESTAMP
);
"""


def ensure_database(dbname: str, reset: bool) -> None:
    """Create the target database if it doesn't exist (or drop-recreate if --reset)."""
    conn = psycopg2.connect(dbname="postgres")
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()

    exists = cur.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
    ) or cur.fetchone()

    if reset and exists:
        print(f"Terminating connections to '{dbname}' and dropping ...")
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (dbname,)
        )
        cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(dbname)))
        exists = None

    if not exists:
        print(f"Creating database '{dbname}' ...")
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))

    cur.close()
    conn.close()


def apply_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS orders, products, customers CASCADE")
        cur.execute(DDL)
    conn.commit()
    print("Schema applied.")

# ── Data quality issue injectors ─────────────────────────────────────────────

def maybe_null(value, rate: float):
    """Return None instead of value with probability `rate`."""
    return None if random.random() < rate else value


def corrupt_email(email: str) -> str:
    """Introduce a format error into a valid email."""
    choice = random.choice(["no_at", "no_domain", "double_at", "spaces"])
    if choice == "no_at":
        return email.replace("@", "-at-")
    if choice == "no_domain":
        return email.split("@")[0] + "@"
    if choice == "double_at":
        return email.replace("@", "@@")
    return email.replace("@", " @ ")


def outlier_price() -> float:
    return round(random.uniform(50_000, 999_999), 2)


def outlier_quantity() -> int:
    return random.randint(10_000, 99_999)

# ── Row generators ────────────────────────────────────────────────────────────

CATEGORIES = ["Electronics", "Furniture", "Kitchen", "Stationery", "Clothing", "Sports", "Toys", "Misc"]

TODAY = date.today()
FAR_FUTURE = date(2099, 12, 31)
FAR_PAST   = date(1880, 1, 1)


def make_customers(fake: Faker, n: int) -> list[tuple]:
    """
    Inject these issue types (roughly):
      ~8%  NULL email            → completeness
      ~4%  bad email format      → format validity
      ~3%  duplicate email       → uniqueness
      ~3%  NULL first_name       → completeness
      ~3%  NULL last_name        → completeness
      ~2%  future birthdate      → range error
      ~2%  implausibly old bdate → range error
      ~2%  future signup_date    → range error
      ~3%  NULL country          → completeness
      ~2%  NULL phone            → completeness
    """
    rows = []
    clean_emails = []  # pool for duplicating

    for _ in range(n):
        first  = maybe_null(fake.first_name(), 0.03)
        last   = maybe_null(fake.last_name(),  0.03)
        phone  = maybe_null(fake.phone_number(), 0.02)
        country = maybe_null(fake.country_code(), 0.03)

        # birthdate
        r = random.random()
        if r < 0.02:
            bdate = FAR_FUTURE                              # future — impossible
        elif r < 0.04:
            bdate = FAR_PAST + timedelta(days=random.randint(0, 3650))  # ~1880s
        else:
            bdate = fake.date_of_birth(minimum_age=18, maximum_age=80)

        # signup_date
        r = random.random()
        if r < 0.02:
            sdate = FAR_FUTURE                             # future — impossible
        else:
            sdate = fake.date_between(start_date=date(2020, 1, 1), end_date=TODAY)

        # email
        r = random.random()
        if r < 0.08:
            email = None                                   # missing
        elif r < 0.12 and clean_emails:
            email = random.choice(clean_emails)            # duplicate
        elif r < 0.16:
            email = corrupt_email(fake.email())            # bad format
        else:
            email = fake.unique.email()
            clean_emails.append(email)

        rows.append((first, last, email, phone, bdate, sdate, country))

    return rows


def make_products(fake: Faker, n: int) -> list[tuple]:
    """
    Inject:
      ~5%  negative price        → range error
      ~5%  zero price            → range / domain
      ~5%  NULL price            → completeness
      ~5%  negative stock        → range error
      ~5%  NULL name             → completeness
      ~5%  duplicate SKU         → uniqueness
      ~5%  NULL SKU              → completeness
      ~2%  extreme price outlier → statistical outlier
    """
    rows = []
    used_skus = []

    for i in range(n):
        sku_base = f"SKU-{i+1:04d}"

        r = random.random()
        if r < 0.05:
            sku = None
        elif r < 0.10 and used_skus:
            sku = random.choice(used_skus)                 # duplicate
        else:
            sku = sku_base
            used_skus.append(sku)

        name = maybe_null(fake.catch_phrase()[:80], 0.05)
        category = maybe_null(random.choice(CATEGORIES), 0.03)

        r = random.random()
        if r < 0.05:
            price = round(random.uniform(-100, -0.01), 2)  # negative
        elif r < 0.10:
            price = 0.00                                    # zero
        elif r < 0.15:
            price = None                                    # missing
        elif r < 0.17:
            price = outlier_price()                         # extreme outlier
        else:
            price = round(random.uniform(0.99, 1999.99), 2)

        r = random.random()
        if r < 0.05:
            stock = random.randint(-50, -1)                 # negative
        else:
            stock = random.randint(0, 500)

        rows.append((sku, name, category, price, stock))

    return rows


def make_orders(fake: Faker, n: int, customer_ids: list[int], product_ids: list[int], product_prices: dict) -> list[tuple]:
    """
    Inject:
      ~4%  orphaned customer_id  → referential integrity
      ~4%  NULL customer_id      → completeness / RI
      ~4%  NULL product_id       → completeness / RI
      ~5%  zero quantity         → range error
      ~5%  negative quantity     → range error
      ~2%  extreme quantity      → statistical outlier
      ~4%  future ordered_at     → range error
      ~3%  NULL ordered_at       → completeness
      ~5%  negative unit_price   → range error
      ~5%  large price mismatch  → cross-table consistency
    """
    rows = []
    for _ in range(n):
        # customer
        r = random.random()
        if r < 0.04:
            cid = random.randint(90_000, 99_999)           # orphan
        elif r < 0.08:
            cid = None
        else:
            cid = random.choice(customer_ids)

        # product
        r = random.random()
        if r < 0.04:
            pid = None
            base_price = round(random.uniform(1, 200), 2)
        else:
            pid = random.choice(product_ids)
            base_price = product_prices.get(pid, round(random.uniform(1, 200), 2)) or round(random.uniform(1, 200), 2)

        # quantity
        r = random.random()
        if r < 0.05:
            qty = 0
        elif r < 0.10:
            qty = random.randint(-20, -1)
        elif r < 0.12:
            qty = outlier_quantity()
        else:
            qty = random.randint(1, 20)

        # unit_price
        r = random.random()
        if r < 0.05:
            unit_price = round(base_price * random.uniform(-2, -0.1), 2)  # negative
        elif r < 0.10:
            unit_price = round(base_price * random.uniform(5, 20), 2)     # large mismatch
        else:
            unit_price = base_price

        # ordered_at
        r = random.random()
        if r < 0.04:
            ts = fake.future_datetime(end_date="+5y")      # future
        elif r < 0.07:
            ts = None
        else:
            ts = fake.date_time_between(start_date=datetime(2022, 1, 1), end_date=datetime.now())

        rows.append((cid, pid, qty, unit_price, ts))

    return rows

# ── Insert helpers ────────────────────────────────────────────────────────────

def bulk_insert(conn, table: str, columns: list[str], rows: list[tuple]) -> list[int]:
    """Insert rows and return the generated ids."""
    col_str = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    query = f"INSERT INTO {table} ({col_str}) VALUES ({placeholders}) RETURNING id"
    ids = []
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(query, row)
            ids.append(cur.fetchone()[0])
    conn.commit()
    return ids

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    random.seed(args.seed)
    fake = Faker()
    Faker.seed(args.seed)

    n_customers = args.rows
    n_products  = max(20, args.rows // 3)
    n_orders    = args.rows * 3

    # 1. Ensure database exists
    ensure_database(args.dbname, args.reset)

    # 2. Connect and apply schema
    conn = psycopg2.connect(dbname=args.dbname)
    apply_schema(conn)

    # 3. Generate and insert customers
    print(f"Generating {n_customers} customers ...")
    customer_rows = make_customers(fake, n_customers)
    customer_ids = bulk_insert(conn, "customers",
        ["first_name", "last_name", "email", "phone", "birthdate", "signup_date", "country"],
        customer_rows)

    # 4. Generate and insert products
    print(f"Generating {n_products} products ...")
    product_rows = make_products(fake, n_products)
    product_ids = bulk_insert(conn, "products",
        ["sku", "name", "category", "price", "stock"],
        product_rows)

    # Build price lookup for cross-table consistency checks
    with conn.cursor() as cur:
        cur.execute("SELECT id, price FROM products WHERE price IS NOT NULL")
        product_prices = {row[0]: float(row[1]) for row in cur.fetchall()}

    # 5. Generate and insert orders
    print(f"Generating {n_orders} orders ...")
    order_rows = make_orders(fake, n_orders, customer_ids, product_ids, product_prices)
    bulk_insert(conn, "orders",
        ["customer_id", "product_id", "quantity", "unit_price", "ordered_at"],
        order_rows)

    # 6. Summary
    with conn.cursor() as cur:
        for table in ("customers", "products", "orders"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            print(f"  {table}: {cur.fetchone()[0]:,} rows")

    conn.close()
    print(f"\nDone. Database '{args.dbname}' is ready.")


if __name__ == "__main__":
    main()
