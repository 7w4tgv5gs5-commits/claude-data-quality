"""
docker/seed.py — Seeder for the Docker environment.

Runs as the 'seeder' service before the web app starts. It:
  1. Calls generate_data.py to populate testdb with synthetic dirty data.
  2. Builds testdb_kimball (Kimball star schema) by reading from testdb
     directly via psycopg2 — no postgres_fdw required.

Connection is driven by PGHOST / PGUSER / PGPASSWORD env vars, which
psycopg2 picks up automatically for all connections.
"""

import os
import sys
import subprocess
from datetime import date, timedelta
from decimal import Decimal

import psycopg2
import psycopg2.extras
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

PGHOST = os.environ["PGHOST"]
PGUSER = os.environ["PGUSER"]
PGPASS = os.environ["PGPASSWORD"]


def connect(dbname: str):
    return psycopg2.connect(dbname=dbname, host=PGHOST, user=PGUSER, password=PGPASS)


def to_float(v):
    return float(v) if isinstance(v, Decimal) else v


# ── 1. Generate testdb source data ────────────────────────────────────────────

print("==> Step 1: generating testdb source data …")
result = subprocess.run(
    [sys.executable, "/app/generate_data.py", "--reset"],
    env=os.environ.copy(),
)
if result.returncode != 0:
    sys.exit(f"generate_data.py failed with code {result.returncode}")
print("==> testdb ready.\n")


# ── 2. (Re)create testdb_kimball ──────────────────────────────────────────────

print("==> Step 2: creating testdb_kimball …")
admin = connect("postgres")
admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
with admin.cursor() as cur:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = 'testdb_kimball'")
    if cur.fetchone():
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = 'testdb_kimball' AND pid <> pg_backend_pid()"
        )
        cur.execute("DROP DATABASE testdb_kimball")
    cur.execute("CREATE DATABASE testdb_kimball")
admin.close()
print("==> testdb_kimball created.\n")


# ── 3. Create star schema ─────────────────────────────────────────────────────

print("==> Step 3: creating star schema …")
dst = connect("testdb_kimball")

with dst.cursor() as cur:
    cur.execute("""
        CREATE TABLE dim_date (
            date_sk      INT PRIMARY KEY,
            full_date    DATE,
            day_of_week  SMALLINT,
            day_name     VARCHAR(12),
            day_of_month SMALLINT,
            day_of_year  SMALLINT,
            week_of_year SMALLINT,
            month_number SMALLINT,
            month_name   VARCHAR(12),
            quarter      SMALLINT,
            year         SMALLINT,
            is_weekend   BOOLEAN
        );

        CREATE TABLE dim_customer (
            customer_sk SERIAL PRIMARY KEY,
            customer_id INT,
            first_name  VARCHAR(50),
            last_name   VARCHAR(50),
            full_name   VARCHAR(101),
            email       VARCHAR(100),
            phone       VARCHAR(30),
            birthdate   DATE,
            signup_date DATE,
            country     VARCHAR(50)
        );

        CREATE TABLE dim_product (
            product_sk SERIAL PRIMARY KEY,
            product_id INT,
            sku        VARCHAR(20),
            name       VARCHAR(100),
            category   VARCHAR(50),
            list_price NUMERIC(10,2),
            stock      INT
        );

        CREATE TABLE fact_orders (
            order_sk    SERIAL PRIMARY KEY,
            order_id    INT  NOT NULL,
            customer_sk INT  NOT NULL REFERENCES dim_customer(customer_sk),
            product_sk  INT  NOT NULL REFERENCES dim_product(product_sk),
            date_sk     INT  NOT NULL REFERENCES dim_date(date_sk),
            quantity    INT,
            unit_price  NUMERIC(10,2),
            line_total  NUMERIC(12,2)
        );
    """)
dst.commit()
print("==> Schema created.\n")


# ── 4. Populate dim_date ──────────────────────────────────────────────────────

print("==> Step 4: populating dim_date …")

DAY_NAMES   = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
MONTH_NAMES = ["January","February","March","April","May","June",
               "July","August","September","October","November","December"]

# date_sk = -1 is the "No Date" sentinel for NULL ordered_at values
date_rows = [(-1, None, None, "No Date", None, None, None, None, "No Date", None, None, None)]

d = date(2020, 1, 1)
while d <= date(2031, 12, 31):
    wk = d.weekday()                       # Mon=0 … Sun=6
    dow = 1 + ((wk + 1) % 7)              # Sun=1, Mon=2 … Sat=7
    date_rows.append((
        int(d.strftime("%Y%m%d")),
        d,
        dow,
        DAY_NAMES[wk],
        d.day,
        d.timetuple().tm_yday,
        d.isocalendar()[1],
        d.month,
        MONTH_NAMES[d.month - 1],
        (d.month - 1) // 3 + 1,
        d.year,
        wk >= 5,
    ))
    d += timedelta(days=1)

with dst.cursor() as cur:
    cur.executemany(
        "INSERT INTO dim_date VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        date_rows,
    )
dst.commit()
print(f"==> dim_date: {len(date_rows):,} rows\n")


# ── 5. Read source data from testdb ──────────────────────────────────────────

print("==> Step 5: reading source data from testdb …")
src = connect("testdb")
with src.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
    cur.execute("SELECT * FROM customers ORDER BY id")
    customers = cur.fetchall()
    cur.execute("SELECT * FROM products ORDER BY id")
    products = cur.fetchall()
    cur.execute("SELECT * FROM orders ORDER BY id")
    orders = cur.fetchall()
src.close()
print(f"==> {len(customers)} customers, {len(products)} products, {len(orders)} orders\n")


# ── 6. Populate dim_customer ──────────────────────────────────────────────────

print("==> Step 6: populating dim_customer …")
with dst.cursor() as cur:
    # sk=0 — Unknown sentinel for orphaned / NULL source FK values
    cur.execute(
        "INSERT INTO dim_customer (customer_sk, customer_id, full_name) "
        "OVERRIDING SYSTEM VALUE VALUES (0, NULL, 'Unknown')"
    )
    for c in customers:
        fn   = (c["first_name"] or "").strip()
        ln   = (c["last_name"]  or "").strip()
        full = " ".join(p for p in [fn, ln] if p) or None
        cur.execute(
            "INSERT INTO dim_customer "
            "(customer_id, first_name, last_name, full_name, email, phone, birthdate, signup_date, country) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (c["id"], c["first_name"], c["last_name"], full,
             c["email"], c["phone"], c["birthdate"], c["signup_date"], c["country"]),
        )
dst.commit()

with dst.cursor() as cur:
    cur.execute("SELECT customer_id, customer_sk FROM dim_customer WHERE customer_id IS NOT NULL")
    cust_map = dict(cur.fetchall())

print(f"==> dim_customer: {len(customers) + 1:,} rows\n")


# ── 7. Populate dim_product ───────────────────────────────────────────────────

print("==> Step 7: populating dim_product …")
with dst.cursor() as cur:
    cur.execute(
        "INSERT INTO dim_product (product_sk, product_id, sku, name, category) "
        "OVERRIDING SYSTEM VALUE VALUES (0, NULL, 'UNKNOWN', 'Unknown Product', 'Unknown')"
    )
    for p in products:
        cur.execute(
            "INSERT INTO dim_product (product_id, sku, name, category, list_price, stock) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (p["id"], p["sku"], p["name"], p["category"], to_float(p["price"]), p["stock"]),
        )
dst.commit()

with dst.cursor() as cur:
    cur.execute("SELECT product_id, product_sk FROM dim_product WHERE product_id IS NOT NULL")
    prod_map = dict(cur.fetchall())

print(f"==> dim_product: {len(products) + 1:,} rows\n")


# ── 8. Populate fact_orders ───────────────────────────────────────────────────

print("==> Step 8: populating fact_orders …")
fact_rows = []
for o in orders:
    csk = cust_map.get(o["customer_id"], 0)
    psk = prod_map.get(o["product_id"],  0)

    if o["ordered_at"] is None:
        dsk = -1
    else:
        dsk = int(o["ordered_at"].strftime("%Y%m%d"))

    qty    = o["quantity"]
    uprice = to_float(o["unit_price"])
    ltotal = (qty * uprice) if (qty is not None and uprice is not None) else None

    fact_rows.append((o["id"], csk, psk, dsk, qty, uprice, ltotal))

with dst.cursor() as cur:
    cur.executemany(
        "INSERT INTO fact_orders "
        "(order_id, customer_sk, product_sk, date_sk, quantity, unit_price, line_total) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        fact_rows,
    )
dst.commit()
print(f"==> fact_orders: {len(fact_rows):,} rows\n")


# ── 9. Analytic indexes ────────────────────────────────────────────────────────

print("==> Step 9: creating indexes …")
with dst.cursor() as cur:
    cur.execute("CREATE INDEX idx_fo_customer ON fact_orders(customer_sk)")
    cur.execute("CREATE INDEX idx_fo_product  ON fact_orders(product_sk)")
    cur.execute("CREATE INDEX idx_fo_date     ON fact_orders(date_sk)")
    cur.execute("CREATE INDEX idx_dd_ym       ON dim_date(year, month_number)")
    cur.execute("CREATE INDEX idx_dc_country  ON dim_customer(country)")
    cur.execute("CREATE INDEX idx_dp_category ON dim_product(category)")
dst.commit()
dst.close()

print("==> All done. testdb_kimball is ready.")
