-- =============================================================================
-- build_kimball.sql
-- Builds a Kimball-style star schema in testdb_kimball from testdb source data.
-- Run with: psql -f build_kimball.sql
-- =============================================================================

-- ── 0. Bootstrap ─────────────────────────────────────────────────────────────

\c postgres
DROP DATABASE IF EXISTS testdb_kimball;
CREATE DATABASE testdb_kimball;
\c testdb_kimball

-- ── 1. Dimension: dim_date ───────────────────────────────────────────────────
-- date_sk is YYYYMMDD integer (fast range predicates, human-readable).
-- Surrogate key -1 is the "No Date" member for NULL ordered_at values.

CREATE TABLE dim_date (
    date_sk       INT          PRIMARY KEY,   -- YYYYMMDD or -1
    full_date     DATE,
    day_of_week   SMALLINT,                   -- 1=Sun … 7=Sat (ISO: 1=Mon)
    day_name      VARCHAR(9),
    day_of_month  SMALLINT,
    day_of_year   SMALLINT,
    week_of_year  SMALLINT,
    month_number  SMALLINT,
    month_name    VARCHAR(9),
    quarter       SMALLINT,
    year          SMALLINT,
    is_weekend    BOOLEAN
);

-- "No Date" sentinel row
INSERT INTO dim_date VALUES (-1, NULL, NULL, 'No Date', NULL, NULL, NULL, NULL, 'No Date', NULL, NULL, NULL);

-- Generate calendar 2020-01-01 → 2031-12-31
INSERT INTO dim_date
SELECT
    TO_CHAR(d, 'YYYYMMDD')::INT        AS date_sk,
    d::DATE                             AS full_date,
    EXTRACT(DOW FROM d)::SMALLINT + 1  AS day_of_week,   -- 1=Sun,7=Sat
    TO_CHAR(d, 'Day')                  AS day_name,
    EXTRACT(DAY  FROM d)::SMALLINT     AS day_of_month,
    EXTRACT(DOY  FROM d)::SMALLINT     AS day_of_year,
    EXTRACT(WEEK FROM d)::SMALLINT     AS week_of_year,
    EXTRACT(MONTH FROM d)::SMALLINT    AS month_number,
    TO_CHAR(d, 'Month')                AS month_name,
    EXTRACT(QUARTER FROM d)::SMALLINT  AS quarter,
    EXTRACT(YEAR FROM d)::SMALLINT     AS year,
    EXTRACT(DOW FROM d) IN (0, 6)      AS is_weekend
FROM generate_series('2020-01-01'::DATE, '2031-12-31'::DATE, '1 day') AS g(d);

-- ── 2. Dimension: dim_customer ───────────────────────────────────────────────
-- Surrogate key 0 is the "Unknown" member (null or orphaned source FK).

CREATE TABLE dim_customer (
    customer_sk   SERIAL       PRIMARY KEY,
    customer_id   INT,                        -- source natural key (NULL = unknown)
    first_name    VARCHAR(50),
    last_name     VARCHAR(50),
    full_name     VARCHAR(101),
    email         VARCHAR(100),
    phone         VARCHAR(30),
    birthdate     DATE,
    signup_date   DATE,
    country       VARCHAR(50)
);

-- Unknown member (sk=0; override serial so we can FK to it)
INSERT INTO dim_customer (customer_sk, customer_id, first_name, last_name, full_name,
                          email, phone, birthdate, signup_date, country)
OVERRIDING SYSTEM VALUE
VALUES (0, NULL, 'Unknown', 'Unknown', 'Unknown', NULL, NULL, NULL, NULL, NULL);

-- Load from source (dblink-free: use postgres_fdw approach via psql \copy,
-- but simplest here is a dblink or cross-db copy. We use a temp staging trick.)

-- Pull data via postgres_fdw
CREATE EXTENSION IF NOT EXISTS postgres_fdw;

CREATE SERVER testdb_src
    FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (dbname 'testdb', host 'localhost', port '5432');

CREATE USER MAPPING FOR CURRENT_USER
    SERVER testdb_src
    OPTIONS (user 'benmoretti');

CREATE FOREIGN TABLE src_customers (
    id          INT,
    first_name  VARCHAR(50),
    last_name   VARCHAR(50),
    email       VARCHAR(100),
    phone       VARCHAR(30),
    birthdate   DATE,
    signup_date DATE,
    country     VARCHAR(50)
) SERVER testdb_src OPTIONS (table_name 'customers');

CREATE FOREIGN TABLE src_products (
    id         INT,
    sku        VARCHAR(20),
    name       VARCHAR(100),
    category   VARCHAR(50),
    price      NUMERIC(10,2),
    stock      INT,
    created_at TIMESTAMP
) SERVER testdb_src OPTIONS (table_name 'products');

CREATE FOREIGN TABLE src_orders (
    id          INT,
    customer_id INT,
    product_id  INT,
    quantity    INT,
    unit_price  NUMERIC(10,2),
    ordered_at  TIMESTAMP
) SERVER testdb_src OPTIONS (table_name 'orders');

INSERT INTO dim_customer
    (customer_id, first_name, last_name, full_name, email, phone, birthdate, signup_date, country)
SELECT
    id,
    first_name,
    last_name,
    TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')),
    email,
    phone,
    birthdate,
    signup_date,
    country
FROM src_customers;

-- ── 3. Dimension: dim_product ─────────────────────────────────────────────────

CREATE TABLE dim_product (
    product_sk  SERIAL       PRIMARY KEY,
    product_id  INT,                          -- source natural key
    sku         VARCHAR(20),
    name        VARCHAR(100),
    category    VARCHAR(50),
    list_price  NUMERIC(10,2),
    stock       INT
);

-- Unknown member
INSERT INTO dim_product (product_sk, product_id, sku, name, category, list_price, stock)
OVERRIDING SYSTEM VALUE
VALUES (0, NULL, 'UNKNOWN', 'Unknown Product', 'Unknown', NULL, NULL);

INSERT INTO dim_product (product_id, sku, name, category, list_price, stock)
SELECT id, sku, name, category, price, stock
FROM src_products;

-- ── 4. Fact table: fact_orders ────────────────────────────────────────────────
-- Grain: one order row from source orders table.
-- Degenerate dimension: order_id (source PK carried as label, not FK).
-- Null / orphaned customer_id  → customer_sk = 0 (Unknown)
-- Null / orphaned product_id   → product_sk  = 0 (Unknown)
-- Null ordered_at              → date_sk     = -1 (No Date)

CREATE TABLE fact_orders (
    order_sk        SERIAL          PRIMARY KEY,
    order_id        INT             NOT NULL,   -- degenerate dimension
    customer_sk     INT             NOT NULL REFERENCES dim_customer(customer_sk),
    product_sk      INT             NOT NULL REFERENCES dim_product(product_sk),
    date_sk         INT             NOT NULL REFERENCES dim_date(date_sk),
    quantity        INT,
    unit_price      NUMERIC(10,2),
    line_total      NUMERIC(12,2)   -- quantity * unit_price (may be NULL if either is NULL)
);

INSERT INTO fact_orders (order_id, customer_sk, product_sk, date_sk, quantity, unit_price, line_total)
SELECT
    o.id,

    COALESCE(dc.customer_sk, 0),   -- NULL or orphan → Unknown

    COALESCE(dp.product_sk, 0),    -- NULL or orphan → Unknown

    CASE
        WHEN o.ordered_at IS NULL THEN -1
        ELSE TO_CHAR(o.ordered_at, 'YYYYMMDD')::INT
    END,

    o.quantity,
    o.unit_price,
    CASE
        WHEN o.quantity IS NOT NULL AND o.unit_price IS NOT NULL
        THEN o.quantity * o.unit_price
        ELSE NULL
    END

FROM src_orders o
LEFT JOIN dim_customer dc ON dc.customer_id = o.customer_id
LEFT JOIN dim_product  dp ON dp.product_id  = o.product_id;

-- ── 5. Analytic indexes ───────────────────────────────────────────────────────

CREATE INDEX idx_fact_orders_customer ON fact_orders(customer_sk);
CREATE INDEX idx_fact_orders_product  ON fact_orders(product_sk);
CREATE INDEX idx_fact_orders_date     ON fact_orders(date_sk);
CREATE INDEX idx_dim_date_year_month  ON dim_date(year, month_number);
CREATE INDEX idx_dim_customer_country ON dim_customer(country);
CREATE INDEX idx_dim_product_category ON dim_product(category);

-- ── 6. Smoke-check ───────────────────────────────────────────────────────────

SELECT 'dim_date'     AS tbl, COUNT(*) FROM dim_date     UNION ALL
SELECT 'dim_customer'          ,        COUNT(*) FROM dim_customer UNION ALL
SELECT 'dim_product'           ,        COUNT(*) FROM dim_product  UNION ALL
SELECT 'fact_orders'           ,        COUNT(*) FROM fact_orders;
