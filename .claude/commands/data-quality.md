# Data Quality Check

Run a comprehensive data quality audit on a local PostgreSQL database and produce a structured report.

## Instructions

The user may optionally pass a database name as an argument (e.g. `/data-quality mydb`). If no argument is given, default to `testdb`.

Use the Bash tool to run `psql` commands. All queries should be run as:
```
psql <dbname> -c "<SQL>"
```

Work through each section below in order. After every section, emit a formatted markdown sub-report before moving to the next. At the end, emit an **Executive Summary** table.

---

## Step 0 — Discover the schema

Run these queries to understand what you're working with:

```sql
-- List all user tables
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name;
```

For each table returned, fetch:
```sql
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = '<table>'
ORDER BY ordinal_position;
```

Also fetch row counts:
```sql
SELECT '<table>' AS table_name, COUNT(*) AS row_count FROM <table>;
```

Report the schema and row counts in a summary table before running any quality checks.

---

## Step 1 — Completeness (NULL / missing values)

For every column in every table, count NULLs:

```sql
SELECT
  '<column>' AS column_name,
  COUNT(*) FILTER (WHERE <column> IS NULL) AS null_count,
  ROUND(COUNT(*) FILTER (WHERE <column> IS NULL) * 100.0 / COUNT(*), 1) AS null_pct
FROM <table>;
```

Build a combined results table per table. Flag any column where `null_pct > 0` as a finding.

Also check for empty strings treated as values:
```sql
SELECT COUNT(*) FROM <table> WHERE TRIM(<text_column>:: text) = '';
```

---

## Step 2 — Uniqueness & Duplicates

For each table, identify columns that should likely be unique (id, email, sku, etc.) and check:

```sql
SELECT <column>, COUNT(*) AS occurrences
FROM <table>
GROUP BY <column>
HAVING COUNT(*) > 1
ORDER BY occurrences DESC;
```

Also check for fully duplicate rows:
```sql
SELECT *, COUNT(*) OVER (PARTITION BY <all_non_pk_cols>) AS dup_count
FROM <table>
HAVING COUNT(*) OVER (...) > 1;
```

---

## Step 3 — Range & Domain Validity

Run the following checks. Adapt column names to what was discovered in Step 0.

**Numeric ranges:**
```sql
-- Negative prices
SELECT id, price FROM products WHERE price < 0;
-- Zero price (may be valid for promos — flag as warning)
SELECT id, price FROM products WHERE price = 0;
-- Negative stock
SELECT id, stock FROM products WHERE stock < 0;
-- Zero or negative order quantities
SELECT id, quantity FROM orders WHERE quantity <= 0;
-- Negative unit prices in orders
SELECT id, unit_price FROM orders WHERE unit_price < 0;
-- Statistical outliers: values > mean + 3*stddev
SELECT id, price FROM products
WHERE price > (SELECT AVG(price) + 3 * STDDEV(price) FROM products WHERE price IS NOT NULL);
```

**Date ranges:**
```sql
-- Future birthdates
SELECT id, birthdate FROM customers WHERE birthdate > CURRENT_DATE;
-- Implausibly old birthdates (before 1900)
SELECT id, birthdate FROM customers WHERE birthdate < '1900-01-01';
-- Future signup dates
SELECT id, signup_date FROM customers WHERE signup_date > CURRENT_DATE;
-- Future order timestamps
SELECT id, ordered_at FROM orders WHERE ordered_at > NOW();
-- NULL timestamps on orders
SELECT id FROM orders WHERE ordered_at IS NULL;
```

---

## Step 4 — Format Validity

**Email format** (basic pattern check):
```sql
SELECT id, email FROM customers
WHERE email IS NOT NULL
  AND email !~* '^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$';
```

**Phone format** (flag suspiciously short values):
```sql
SELECT id, phone FROM customers
WHERE phone IS NOT NULL AND LENGTH(REGEXP_REPLACE(phone, '[^0-9]', '', 'g')) < 7;
```

**SKU format** (flag NULLs or non-standard patterns):
```sql
SELECT id, sku FROM products WHERE sku IS NULL OR sku !~* '^SKU-[0-9]+$';
```

---

## Step 5 — Referential Integrity

Check that foreign keys actually point to existing parent rows (even if FK constraints aren't enforced):

```sql
-- Orders referencing non-existent customers
SELECT o.id, o.customer_id
FROM orders o
LEFT JOIN customers c ON o.customer_id = c.id
WHERE o.customer_id IS NOT NULL AND c.id IS NULL;

-- Orders referencing non-existent products
SELECT o.id, o.product_id
FROM orders o
LEFT JOIN products p ON o.product_id = p.id
WHERE o.product_id IS NOT NULL AND p.id IS NULL;
```

Also check for orphaned order rows with NULL foreign keys:
```sql
SELECT COUNT(*) AS orders_with_null_customer FROM orders WHERE customer_id IS NULL;
SELECT COUNT(*) AS orders_with_null_product  FROM orders WHERE product_id IS NULL;
```

---

## Step 6 — Cross-table Consistency

Check that `unit_price` in orders matches the current product price (flag large discrepancies):

```sql
SELECT
  o.id AS order_id,
  o.product_id,
  o.unit_price AS order_price,
  p.price AS current_product_price,
  ABS(o.unit_price - p.price) AS discrepancy
FROM orders o
JOIN products p ON o.product_id = p.id
WHERE o.unit_price IS NOT NULL
  AND p.price IS NOT NULL
  AND ABS(o.unit_price - p.price) > 1.00
ORDER BY discrepancy DESC;
```

---

## Step 7 — Executive Summary

After completing all checks, produce a markdown table summarising every finding:

| # | Table | Check | Severity | Affected Rows | Description |
|---|-------|-------|----------|---------------|-------------|
| 1 | customers | Completeness | WARNING | N | N rows have NULL email |
| … | … | … | … | … | … |

Severity levels:
- **ERROR** — data is clearly wrong (negative prices, future birthdates, orphaned FKs, invalid email format)
- **WARNING** — data may be valid but warrants review (NULL optional fields, zero prices, statistical outliers)
- **INFO** — observations with no clear right/wrong (e.g. price changes over time)

End with a one-paragraph plain-English summary of the overall data quality health of the database.
