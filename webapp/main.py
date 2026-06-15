"""
Kimball Analytics — FastAPI web application for testdb_kimball
Run with: uvicorn main:app --reload
"""

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from decimal import Decimal
import datetime
import os
from typing import Optional

app = FastAPI(title="Kimball Analytics")
templates = Jinja2Templates(directory="templates")


def _conn_kwargs() -> dict:
    """Build psycopg2 connect kwargs from env vars (falls back to local socket)."""
    kw: dict = {"dbname": os.getenv("DB_NAME", "testdb_kimball")}
    if host := os.getenv("PGHOST"):     kw["host"]     = host
    if user := os.getenv("PGUSER"):     kw["user"]     = user
    if pw   := os.getenv("PGPASSWORD"): kw["password"] = pw
    return kw


def _s(v):
    """Serialize a psycopg2 value to a JSON-safe Python type."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return v


@contextmanager
def _conn():
    c = psycopg2.connect(**_conn_kwargs())
    try:
        yield c
    finally:
        c.close()


def query(sql, params=None):
    with _conn() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or [])
            return [{k: _s(v) for k, v in row.items()} for row in cur.fetchall()]


def query_one(sql, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"active": "dashboard"})


@app.get("/customers", response_class=HTMLResponse)
async def customers_page(request: Request):
    countries = query(
        "SELECT DISTINCT country FROM dim_customer "
        "WHERE country IS NOT NULL AND customer_sk != 0 ORDER BY country"
    )
    return templates.TemplateResponse(request=request, name="customers.html", context={
        "active": "customers",
        "countries": [r["country"] for r in countries],
    })


@app.get("/products", response_class=HTMLResponse)
async def products_page(request: Request):
    categories = query(
        "SELECT DISTINCT category FROM dim_product "
        "WHERE category IS NOT NULL AND product_sk != 0 ORDER BY category"
    )
    return templates.TemplateResponse(request=request, name="products.html", context={
        "active": "products",
        "categories": [r["category"] for r in categories],
    })


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    return templates.TemplateResponse(request=request, name="orders.html", context={"active": "orders"})


@app.get("/data-quality", response_class=HTMLResponse)
async def data_quality_page(request: Request):
    return templates.TemplateResponse(request=request, name="data_quality.html", context={"active": "quality"})


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/kpis")
async def kpis():
    row = query_one("""
        SELECT
            COUNT(*)                                                         AS total_orders,
            SUM(CASE WHEN line_total > 0 THEN line_total ELSE 0 END)        AS total_revenue,
            COUNT(DISTINCT CASE WHEN customer_sk != 0 THEN customer_sk END)  AS active_customers
        FROM fact_orders
    """)
    products = query_one(
        "SELECT COUNT(*) AS n FROM dim_product WHERE product_sk != 0 AND list_price > 0"
    )
    return {**row, "total_products": products["n"]}


@app.get("/api/revenue-by-category")
async def revenue_by_category():
    return query("""
        SELECT
            COALESCE(NULLIF(TRIM(dp.category), ''), 'Uncategorised') AS category,
            ROUND(SUM(fo.line_total)::numeric, 2)                     AS revenue,
            COUNT(*)                                                   AS orders
        FROM fact_orders fo
        JOIN dim_product dp ON dp.product_sk = fo.product_sk
        WHERE fo.product_sk != 0 AND fo.line_total > 0
        GROUP BY dp.category
        ORDER BY revenue DESC
    """)


@app.get("/api/orders-over-time")
async def orders_over_time():
    return query("""
        SELECT
            dd.year,
            dd.month_number,
            TRIM(dd.month_name)                    AS month_name,
            COUNT(*)                               AS order_count,
            ROUND(SUM(fo.line_total)::numeric, 2)  AS revenue
        FROM fact_orders fo
        JOIN dim_date dd ON dd.date_sk = fo.date_sk
        WHERE fo.date_sk != -1
          AND dd.full_date BETWEEN '2022-01-01' AND CURRENT_DATE
          AND fo.line_total > 0
        GROUP BY dd.year, dd.month_number, dd.month_name
        ORDER BY dd.year, dd.month_number
    """)


@app.get("/api/top-customers")
async def top_customers():
    return query("""
        SELECT
            dc.full_name,
            dc.country,
            COUNT(*)                               AS orders,
            ROUND(SUM(fo.line_total)::numeric, 2)  AS total_spent
        FROM fact_orders fo
        JOIN dim_customer dc ON dc.customer_sk = fo.customer_sk
        WHERE fo.customer_sk != 0 AND fo.line_total > 0
        GROUP BY dc.customer_sk, dc.full_name, dc.country
        ORDER BY total_spent DESC
        LIMIT 10
    """)


@app.get("/api/customers/search")
async def search_customers(
    search_q: Optional[str] = Query(None, alias="q"),
    country: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
):
    conds = ["dc.customer_sk != 0"]
    params: list = []
    if search_q:
        conds.append("(dc.full_name ILIKE %s OR dc.email ILIKE %s)")
        params += [f"%{search_q}%", f"%{search_q}%"]
    if country:
        conds.append("dc.country = %s")
        params.append(country)
    params.append(limit)
    return query(f"""
        SELECT
            dc.customer_sk, dc.customer_id, dc.full_name, dc.email,
            dc.phone, dc.country, dc.signup_date,
            COUNT(fo.order_sk)                                                                      AS orders,
            ROUND(COALESCE(SUM(CASE WHEN fo.line_total > 0 THEN fo.line_total END), 0)::numeric, 2) AS total_spent
        FROM dim_customer dc
        LEFT JOIN fact_orders fo ON fo.customer_sk = dc.customer_sk
        WHERE {' AND '.join(conds)}
        GROUP BY dc.customer_sk, dc.customer_id, dc.full_name, dc.email,
                 dc.phone, dc.country, dc.signup_date
        ORDER BY total_spent DESC
        LIMIT %s
    """, params)


@app.get("/api/products/search")
async def search_products(
    search_q: Optional[str] = Query(None, alias="q"),
    category: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
):
    conds = ["dp.product_sk != 0"]
    params: list = []
    if search_q:
        conds.append("(dp.name ILIKE %s OR dp.sku ILIKE %s)")
        params += [f"%{search_q}%", f"%{search_q}%"]
    if category:
        conds.append("dp.category = %s")
        params.append(category)
    params.append(limit)
    return query(f"""
        SELECT
            dp.product_sk, dp.product_id, dp.sku, dp.name,
            dp.category, dp.list_price, dp.stock,
            COUNT(fo.order_sk) AS orders
        FROM dim_product dp
        LEFT JOIN fact_orders fo ON fo.product_sk = dp.product_sk
        WHERE {' AND '.join(conds)}
        GROUP BY dp.product_sk, dp.product_id, dp.sku, dp.name,
                 dp.category, dp.list_price, dp.stock
        ORDER BY orders DESC
        LIMIT %s
    """, params)


@app.get("/api/orders/search")
async def search_orders(
    search_q: Optional[str] = Query(None, alias="q"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
):
    conds: list[str] = []
    params: list = []
    if search_q:
        conds.append("(dc.full_name ILIKE %s OR dp.name ILIKE %s OR dp.sku ILIKE %s)")
        params += [f"%{search_q}%", f"%{search_q}%", f"%{search_q}%"]
    if date_from:
        conds.append("dd.full_date >= %s::date")
        params.append(date_from)
    if date_to:
        conds.append("dd.full_date <= %s::date")
        params.append(date_to)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    return query(f"""
        SELECT
            fo.order_id,
            dc.full_name   AS customer,
            dc.country,
            dp.name        AS product,
            dp.sku,
            dp.category,
            dd.full_date   AS order_date,
            fo.quantity,
            fo.unit_price,
            fo.line_total
        FROM fact_orders fo
        LEFT JOIN dim_customer dc ON dc.customer_sk = fo.customer_sk
        LEFT JOIN dim_product  dp ON dp.product_sk  = fo.product_sk
        LEFT JOIN dim_date     dd ON dd.date_sk      = fo.date_sk
        {where}
        ORDER BY dd.full_date DESC NULLS LAST
        LIMIT %s
    """, params)


@app.get("/api/data-quality")
async def data_quality_api():
    def chk(table, check, severity, sql):
        row = query_one(sql)
        n = list(row.values())[0] if row else 0
        return {"table": table, "check": check, "severity": severity, "affected": int(n or 0)}

    checks = [
        # dim_customer
        chk("dim_customer", "NULL email", "WARNING",
            "SELECT COUNT(*) FROM dim_customer WHERE email IS NULL AND customer_sk != 0"),
        chk("dim_customer", "Bad email format", "ERROR",
            "SELECT COUNT(*) FROM dim_customer WHERE email IS NOT NULL AND customer_sk != 0"
            " AND email !~* '^[A-Za-z0-9._%%+\\-]+@[A-Za-z0-9.\\-]+\\.[A-Za-z]{2,}$'"),
        chk("dim_customer", "Duplicate emails", "ERROR",
            "SELECT COUNT(*) FROM ("
            "  SELECT email FROM dim_customer WHERE email IS NOT NULL AND customer_sk != 0"
            "  GROUP BY email HAVING COUNT(*) > 1) x"),
        chk("dim_customer", "Future birthdate", "ERROR",
            "SELECT COUNT(*) FROM dim_customer WHERE birthdate > CURRENT_DATE AND customer_sk != 0"),
        chk("dim_customer", "Birthdate before 1900", "ERROR",
            "SELECT COUNT(*) FROM dim_customer WHERE birthdate < '1900-01-01' AND customer_sk != 0"),
        chk("dim_customer", "Future signup date", "ERROR",
            "SELECT COUNT(*) FROM dim_customer WHERE signup_date > CURRENT_DATE AND customer_sk != 0"),
        chk("dim_customer", "NULL country", "WARNING",
            "SELECT COUNT(*) FROM dim_customer WHERE country IS NULL AND customer_sk != 0"),
        chk("dim_customer", "NULL first_name", "WARNING",
            "SELECT COUNT(*) FROM dim_customer WHERE first_name IS NULL AND customer_sk != 0"),
        # dim_product
        chk("dim_product", "Negative list price", "ERROR",
            "SELECT COUNT(*) FROM dim_product WHERE list_price < 0 AND product_sk != 0"),
        chk("dim_product", "Zero list price", "WARNING",
            "SELECT COUNT(*) FROM dim_product WHERE list_price = 0 AND product_sk != 0"),
        chk("dim_product", "Negative stock", "ERROR",
            "SELECT COUNT(*) FROM dim_product WHERE stock < 0 AND product_sk != 0"),
        chk("dim_product", "Duplicate SKUs", "ERROR",
            "SELECT COUNT(*) FROM ("
            "  SELECT sku FROM dim_product WHERE sku IS NOT NULL AND product_sk != 0"
            "  GROUP BY sku HAVING COUNT(*) > 1) x"),
        chk("dim_product", "NULL SKU", "WARNING",
            "SELECT COUNT(*) FROM dim_product WHERE sku IS NULL AND product_sk != 0"),
        chk("dim_product", "NULL name", "WARNING",
            "SELECT COUNT(*) FROM dim_product WHERE name IS NULL AND product_sk != 0"),
        # fact_orders
        chk("fact_orders", "Negative quantity", "ERROR",
            "SELECT COUNT(*) FROM fact_orders WHERE quantity < 0"),
        chk("fact_orders", "Zero quantity", "WARNING",
            "SELECT COUNT(*) FROM fact_orders WHERE quantity = 0"),
        chk("fact_orders", "Negative unit price", "ERROR",
            "SELECT COUNT(*) FROM fact_orders WHERE unit_price < 0"),
        chk("fact_orders", "Unknown customer (orphaned FK)", "ERROR",
            "SELECT COUNT(*) FROM fact_orders WHERE customer_sk = 0"),
        chk("fact_orders", "Unknown product (orphaned FK)", "ERROR",
            "SELECT COUNT(*) FROM fact_orders WHERE product_sk = 0"),
        chk("fact_orders", "No date (NULL ordered_at)", "WARNING",
            "SELECT COUNT(*) FROM fact_orders WHERE date_sk = -1"),
        chk("fact_orders", "Future order date", "ERROR",
            "SELECT COUNT(*) FROM fact_orders fo "
            "JOIN dim_date dd ON dd.date_sk = fo.date_sk WHERE dd.full_date > CURRENT_DATE"),
        chk("fact_orders", "Price vs list price mismatch >$1", "ERROR",
            "SELECT COUNT(*) FROM fact_orders fo "
            "JOIN dim_product dp ON dp.product_sk = fo.product_sk "
            "WHERE fo.product_sk != 0 AND dp.list_price IS NOT NULL "
            "  AND fo.unit_price IS NOT NULL AND ABS(fo.unit_price - dp.list_price) > 1"),
        # dim_date
        chk("dim_date", "Padded day_name (ETL defect)", "WARNING",
            "SELECT COUNT(*) FROM dim_date WHERE day_name != TRIM(day_name) AND date_sk != -1"),
        chk("dim_date", "Padded month_name (ETL defect)", "WARNING",
            "SELECT COUNT(*) FROM dim_date WHERE month_name != TRIM(month_name) AND date_sk != -1"),
    ]

    errors   = [c for c in checks if c["severity"] == "ERROR"]
    warnings = [c for c in checks if c["severity"] == "WARNING"]

    return {
        "checks": checks,
        "summary": {
            "error_count":   len(errors),
            "warning_count": len(warnings),
            "clean_count":   sum(1 for c in checks if c["affected"] == 0),
            "total_checks":  len(checks),
            "error_rows":    sum(c["affected"] for c in errors),
            "warning_rows":  sum(c["affected"] for c in warnings),
        },
    }
