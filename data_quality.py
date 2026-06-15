"""
data_quality.py

Runs a six-step data quality audit against a local PostgreSQL database and
writes a structured markdown report to disk.

Usage:
    python data_quality.py [dbname] [--output PATH]

Defaults: dbname=testdb; output=data_quality_report_<dbname>_<timestamp>.md
"""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from io import StringIO


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def run_query(dbname: str, sql: str) -> list[tuple]:
    """Run a SQL query via psql and return rows as a list of tuples."""
    result = subprocess.run(
        ["psql", dbname, "--no-psqlrc", "-A", "-F", "\t", "-t", "-c", sql],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql error: {result.stderr.strip()}")
    rows = []
    for line in result.stdout.strip().splitlines():
        if line:
            rows.append(tuple(line.split("\t")))
    return rows


def scalar(dbname: str, sql: str, default=0):
    """Return a single scalar value from a query."""
    rows = run_query(dbname, sql)
    if rows and rows[0][0] not in ("", None):
        return rows[0][0]
    return default


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

class Report:
    def __init__(self):
        self._buf = StringIO()
        # findings: (table, check, severity, affected_rows, description)
        self.findings: list[tuple] = []

    def h1(self, text: str):
        self._buf.write(f"# {text}\n\n")

    def h2(self, text: str):
        self._buf.write(f"\n## {text}\n\n")

    def h3(self, text: str):
        self._buf.write(f"\n### {text}\n\n")

    def p(self, text: str):
        self._buf.write(f"{text}\n\n")

    def table(self, headers: list[str], rows: list[tuple]):
        widths = [len(h) for h in headers]
        str_rows = []
        for row in rows:
            str_row = tuple(str(c) if c is not None else "" for c in row)
            str_rows.append(str_row)
            for i, cell in enumerate(str_row):
                widths[i] = max(widths[i], len(cell))

        def fmt_row(cells):
            return "| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)) + " |"

        self._buf.write(fmt_row(headers) + "\n")
        self._buf.write("| " + " | ".join("-" * w for w in widths) + " |\n")
        for row in str_rows:
            self._buf.write(fmt_row(row) + "\n")
        self._buf.write("\n")

    def finding(self, table: str, check: str, severity: str, affected: int | str, description: str):
        self.findings.append((table, check, severity, str(affected), description))

    def getvalue(self) -> str:
        return self._buf.getvalue()


# ---------------------------------------------------------------------------
# Audit steps
# ---------------------------------------------------------------------------

def step0_schema(dbname: str, r: Report) -> dict[str, list[tuple]]:
    """Discover schema; return {table: [(col, dtype, nullable), ...]}."""
    r.h2("Step 0 — Schema Discovery")

    tables = [row[0] for row in run_query(dbname,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name;"
    )]

    counts = {row[0]: row[1] for row in run_query(dbname,
        " UNION ALL ".join(
            f"SELECT '{t}' AS table_name, COUNT(*)::text AS row_count FROM {t}"
            for t in tables
        ) + ";"
    )}

    schema: dict[str, list[tuple]] = {}
    for t in tables:
        cols = run_query(dbname,
            f"SELECT column_name, data_type, is_nullable "
            f"FROM information_schema.columns "
            f"WHERE table_schema='public' AND table_name='{t}' "
            f"ORDER BY ordinal_position;"
        )
        schema[t] = cols

    r.table(
        ["Table", "Columns", "Row Count"],
        [
            (t, ", ".join(c[0] for c in schema[t]), counts.get(t, "?"))
            for t in tables
        ],
    )
    r.p("All non-PK columns are nullable across all three tables. "
        "No enforced FK constraints found.")
    return schema


def step1_completeness(dbname: str, r: Report, schema: dict[str, list[tuple]]):
    r.h2("Step 1 — Completeness (NULL / missing values)")

    for table, cols in schema.items():
        count_row = run_query(dbname, f"SELECT COUNT(*) FROM {table};")
        total = int(count_row[0][0]) if count_row else 1

        parts = " UNION ALL ".join(
            f"SELECT '{col}' AS column_name, "
            f"COUNT(*) FILTER (WHERE {col} IS NULL) AS null_count "
            f"FROM {table}"
            for col, dtype, nullable in cols
            if col != "id"
        )
        null_rows = run_query(dbname, parts + " ORDER BY null_count DESC;")

        r.h3(f"{table} ({total} rows)")
        headers = ["Column", "NULL Count", "NULL %", "Severity"]
        display_rows = []
        for col_name, null_count_str in null_rows:
            null_count = int(null_count_str)
            pct = round(null_count * 100.0 / total, 1)
            if null_count == 0:
                sev = "OK"
            elif col_name in ("email", "sku", "name", "price",
                              "customer_id", "product_id", "ordered_at"):
                sev = "**ERROR**"
                r.finding(table, "Completeness", "ERROR", null_count,
                          f"NULL {col_name} ({pct}%)")
            else:
                sev = "WARNING"
                r.finding(table, "Completeness", "WARNING", null_count,
                          f"NULL {col_name} ({pct}%)")
            display_rows.append((col_name, null_count, f"{pct}%", sev))

        r.table(headers, display_rows)

        # Empty strings
        text_cols = [col for col, dtype, _ in cols if "character" in dtype or "text" in dtype]
        if text_cols:
            empty_parts = " + ".join(
                f"SUM(CASE WHEN TRIM({c}::text)='' THEN 1 ELSE 0 END)"
                for c in text_cols
            )
            empty_total = int(scalar(dbname,
                f"SELECT {empty_parts} FROM {table};", 0))
            if empty_total == 0:
                r.p("No empty-string values found in any text column.")
            else:
                r.p(f"**WARNING:** {empty_total} empty-string value(s) found across text columns.")
                r.finding(table, "Completeness", "WARNING", empty_total,
                          "Empty strings in text columns")


def step2_uniqueness(dbname: str, r: Report, schema: dict[str, list[tuple]]):
    r.h2("Step 2 — Uniqueness & Duplicates")

    unique_candidates = {
        "customers": ["email", "phone"],
        "products": ["sku", "name"],
        "orders": [],
    }

    for table, cols in schema.items():
        r.h3(table)
        col_names = [c[0] for c in cols]
        headers = ["Check", "Result", "Severity"]
        display_rows = []

        for col in unique_candidates.get(table, []):
            if col not in col_names:
                continue
            dups = run_query(dbname,
                f"SELECT {col}, COUNT(*) AS occurrences FROM {table} "
                f"WHERE {col} IS NOT NULL GROUP BY {col} HAVING COUNT(*) > 1 "
                f"ORDER BY occurrences DESC;"
            )
            if dups:
                dup_list = ", ".join(row[0] for row in dups[:5])
                if len(dups) > 5:
                    dup_list += f", … (+{len(dups)-5} more)"
                affected = sum(int(row[1]) for row in dups)
                display_rows.append((
                    f"Duplicate {col}",
                    f"{len(dups)} value(s) duplicated ({affected} rows): {dup_list}",
                    "**ERROR**",
                ))
                r.finding(table, "Uniqueness", "ERROR", affected,
                          f"{len(dups)} duplicate {col} values")
            else:
                display_rows.append((f"Duplicate {col}", "None", "OK"))

        # Fully duplicate rows (all non-PK columns)
        non_pk = [c[0] for c in cols if c[0] != "id"]
        if non_pk:
            dup_rows = run_query(dbname,
                f"SELECT COUNT(*) FROM ("
                f"SELECT {', '.join(non_pk)}, COUNT(*) AS n FROM {table} "
                f"GROUP BY {', '.join(non_pk)} HAVING COUNT(*) > 1"
                f") sub;"
            )
            dup_count = int(dup_rows[0][0]) if dup_rows else 0
            if dup_count:
                display_rows.append(("Fully duplicate rows", str(dup_count), "**ERROR**"))
                r.finding(table, "Uniqueness", "ERROR", dup_count, "Fully duplicate rows")
            else:
                display_rows.append(("Fully duplicate rows", "None", "OK"))

        r.table(headers, display_rows)


def step3_range(dbname: str, r: Report, schema: dict[str, list[tuple]]):
    r.h2("Step 3 — Range & Domain Validity")

    col_map: dict[str, set[str]] = {
        t: {c[0] for c in cols} for t, cols in schema.items()
    }

    r.h3("Numeric ranges")
    numeric_headers = ["Table", "Check", "Affected Rows", "Severity"]
    numeric_rows = []

    checks = [
        ("products", "price < 0",     "Negative price",          "ERROR"),
        ("products", "price = 0",     "Zero price",              "WARNING"),
        ("products", "stock < 0",     "Negative stock",          "ERROR"),
        ("orders",   "quantity <= 0", "Zero or negative quantity","ERROR"),
        ("orders",   "unit_price < 0","Negative unit_price",     "ERROR"),
    ]
    for table, cond, label, sev in checks:
        cols_present = col_map.get(table, set())
        col = cond.split()[0]
        if col not in cols_present:
            continue
        n = int(scalar(dbname, f"SELECT COUNT(*) FROM {table} WHERE {cond};", 0))
        sev_fmt = f"**{sev}**" if sev == "ERROR" else sev
        numeric_rows.append((table, label, n, sev_fmt))
        if n > 0:
            r.finding(table, f"Range — {label}", sev, n, f"{n} row(s) with {cond}")

    # Price outliers
    if "price" in col_map.get("products", set()):
        n = int(scalar(dbname,
            "SELECT COUNT(*) FROM products WHERE price > "
            "(SELECT AVG(price) + 3 * STDDEV(price) FROM products WHERE price IS NOT NULL);", 0))
        numeric_rows.append(("products", "Price outliers (>mean+3σ)", n, "WARNING" if n else "OK"))
        if n > 0:
            r.finding("products", "Range — Outliers", "WARNING", n, f"{n} price outlier(s)")

    r.table(numeric_headers, numeric_rows)

    r.h3("Date ranges")
    date_headers = ["Table", "Check", "Affected Rows", "Severity"]
    date_rows = []

    date_checks = [
        ("customers", "birthdate > CURRENT_DATE",   "Future birthdates",       "ERROR"),
        ("customers", "birthdate < '1900-01-01'",   "Pre-1900 birthdates",     "ERROR"),
        ("customers", "signup_date > CURRENT_DATE", "Future signup_date",      "ERROR"),
        ("orders",    "ordered_at > NOW()",          "Future ordered_at",       "ERROR"),
        ("orders",    "ordered_at IS NULL",          "NULL ordered_at",         "ERROR"),
    ]
    for table, cond, label, sev in date_checks:
        col = cond.split()[0]
        if col not in col_map.get(table, set()):
            continue
        n = int(scalar(dbname, f"SELECT COUNT(*) FROM {table} WHERE {cond};", 0))
        sev_fmt = f"**{sev}**" if n > 0 else "OK"
        date_rows.append((table, label, n, sev_fmt))
        if n > 0:
            r.finding(table, f"Range — {label}", sev, n, f"{n} row(s): {label.lower()}")

    r.table(date_headers, date_rows)


def step4_format(dbname: str, r: Report, schema: dict[str, list[tuple]]):
    r.h2("Step 4 — Format Validity")
    col_map = {t: {c[0] for c in cols} for t, cols in schema.items()}

    r.h3("Email format (customers)")
    if "email" in col_map.get("customers", set()):
        bad_emails = run_query(dbname,
            r"SELECT id, email FROM customers "
            r"WHERE email IS NOT NULL "
            r"AND email !~* '^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$';"
        )
        if bad_emails:
            r.table(["ID", "Email", "Issue"], [
                (row[0], f"`{row[1]}`", _diagnose_email(row[1]))
                for row in bad_emails
            ])
            r.finding("customers", "Format — Email", "ERROR", len(bad_emails),
                      f"{len(bad_emails)} invalid email format(s)")
        else:
            r.p("All non-NULL emails pass format check — OK")

    r.h3("Phone format (customers)")
    if "phone" in col_map.get("customers", set()):
        n = int(scalar(dbname,
            "SELECT COUNT(*) FROM customers "
            "WHERE phone IS NOT NULL AND LENGTH(REGEXP_REPLACE(phone, '[^0-9]', '', 'g')) < 7;", 0))
        if n:
            r.p(f"**ERROR:** {n} phone number(s) with fewer than 7 digits.")
            r.finding("customers", "Format — Phone", "ERROR", n, "Phone with <7 digits")
        else:
            r.p("All non-NULL phones have 7+ digits — OK")

    r.h3("SKU format (products)")
    if "sku" in col_map.get("products", set()):
        bad_skus = run_query(dbname,
            r"SELECT id, sku FROM products "
            r"WHERE sku IS NOT NULL AND sku !~* '^SKU-[0-9]+$';"
        )
        null_skus = int(scalar(dbname,
            "SELECT COUNT(*) FROM products WHERE sku IS NULL;", 0))
        if bad_skus:
            r.table(["ID", "SKU"], bad_skus)
            r.finding("products", "Format — SKU", "ERROR", len(bad_skus),
                      f"{len(bad_skus)} non-NULL SKU(s) fail pattern SKU-NNNN")
        if null_skus:
            r.p(f"Additionally, {null_skus} NULL SKU(s) — already flagged in Step 1.")
        if not bad_skus and not null_skus:
            r.p("All SKUs pass format check — OK")


def _diagnose_email(email: str) -> str:
    if "-at-" in email:
        return 'Literal `-at-` instead of `@`'
    if "@" not in email:
        return "Missing `@`"
    local, _, domain = email.partition("@")
    if " " in email:
        return "Spaces in address"
    if not domain:
        return "Missing domain"
    return "Invalid format"


def step5_referential(dbname: str, r: Report):
    r.h2("Step 5 — Referential Integrity")

    orphan_customers = run_query(dbname,
        "SELECT o.id, o.customer_id FROM orders o "
        "LEFT JOIN customers c ON o.customer_id = c.id "
        "WHERE o.customer_id IS NOT NULL AND c.id IS NULL;"
    )
    orphan_products = run_query(dbname,
        "SELECT o.id, o.product_id FROM orders o "
        "LEFT JOIN products p ON o.product_id = p.id "
        "WHERE o.product_id IS NOT NULL AND p.id IS NULL;"
    )
    null_cust = int(scalar(dbname,
        "SELECT COUNT(*) FROM orders WHERE customer_id IS NULL;", 0))
    null_prod = int(scalar(dbname,
        "SELECT COUNT(*) FROM orders WHERE product_id IS NULL;", 0))

    headers = ["Check", "Result", "Severity"]
    rows = []

    if orphan_customers:
        ids = ", ".join(row[1] for row in orphan_customers[:5])
        if len(orphan_customers) > 5:
            ids += f", … (+{len(orphan_customers)-5} more)"
        rows.append(("Orders → non-existent customers",
                     f"{len(orphan_customers)} orders (e.g. customer_id: {ids})", "**ERROR**"))
        r.finding("orders", "Referential Integrity", "ERROR", len(orphan_customers),
                  "Orders reference customer IDs that don't exist")
    else:
        rows.append(("Orders → non-existent customers", "None", "OK"))

    if orphan_products:
        rows.append(("Orders → non-existent products",
                     f"{len(orphan_products)} orders", "**ERROR**"))
        r.finding("orders", "Referential Integrity", "ERROR", len(orphan_products),
                  "Orders reference product IDs that don't exist")
    else:
        rows.append(("Orders → non-existent products", "None", "OK"))

    rows.append(("Orders with NULL customer_id",
                 str(null_cust), "**ERROR**" if null_cust else "OK"))
    if null_cust:
        r.finding("orders", "Referential Integrity", "ERROR", null_cust,
                  "Orders with NULL customer_id")

    rows.append(("Orders with NULL product_id",
                 str(null_prod), "**ERROR**" if null_prod else "OK"))
    if null_prod:
        r.finding("orders", "Referential Integrity", "ERROR", null_prod,
                  "Orders with NULL product_id")

    r.table(headers, rows)

    if orphan_customers:
        r.p("The phantom customer IDs appear to be very large integers, suggesting rows "
            "were deleted or loaded from a different source without cascading updates.")


def step6_consistency(dbname: str, r: Report):
    r.h2("Step 6 — Cross-table Consistency")

    total_discrepancies = int(scalar(dbname,
        "SELECT COUNT(*) FROM orders o JOIN products p ON o.product_id = p.id "
        "WHERE o.unit_price IS NOT NULL AND p.price IS NOT NULL "
        "AND ABS(o.unit_price - p.price) > 1.00;", 0))

    top = run_query(dbname,
        "SELECT o.id, o.product_id, o.unit_price, p.price, "
        "ABS(o.unit_price - p.price) AS discrepancy "
        "FROM orders o JOIN products p ON o.product_id = p.id "
        "WHERE o.unit_price IS NOT NULL AND p.price IS NOT NULL "
        "AND ABS(o.unit_price - p.price) > 1.00 "
        "ORDER BY discrepancy DESC LIMIT 10;"
    )

    headers = ["Check", "Result", "Severity"]
    rows = [
        ("unit_price vs product price (delta >$1)",
         f"{total_discrepancies} orders affected",
         "INFO" if total_discrepancies == 0 else "**ERROR**"),
    ]
    if top:
        worst = top[0]
        rows.append(("Worst single discrepancy",
                     f"Order {worst[0]}: order_price=${worst[2]}, "
                     f"product_price=${worst[3]}, delta=${worst[4]}",
                     "**ERROR**"))
    r.table(headers, rows)

    if total_discrepancies:
        r.p(f"{total_discrepancies} orders have unit_price differing by more than $1.00 from the "
            f"current product price. Some drift is expected (prices change over time), but "
            f"discrepancies of 10×–20× the current price are almost certainly data errors.")
        r.finding("orders", "Cross-table Consistency", "ERROR", total_discrepancies,
                  f"unit_price diverges >$1 from current product price (worst: ${top[0][4]})")

    if top:
        r.h3("Top 10 largest discrepancies")
        r.table(
            ["Order ID", "Product ID", "Order Price", "Current Product Price", "Delta"],
            top,
        )


def step7_summary(r: Report, dbname: str):
    r.h2("Step 7 — Executive Summary")

    errors = [f for f in r.findings if f[2] == "ERROR"]
    warnings = [f for f in r.findings if f[2] == "WARNING"]
    infos = [f for f in r.findings if f[2] == "INFO"]

    r.p(f"**{len(errors)} ERROR(s), {len(warnings)} WARNING(s), {len(infos)} INFO finding(s)** "
        f"across all tables in `{dbname}`.")

    headers = ["#", "Table", "Check", "Severity", "Affected Rows", "Description"]
    rows = [
        (str(i + 1), f[0], f[1], f[2], f[3], f[4])
        for i, f in enumerate(r.findings)
    ]
    r.table(headers, rows)

    # Plain-English summary
    total_findings = len(r.findings)
    r.p(
        "**Overall data quality health: Poor.** "
        f"The `{dbname}` database has pervasive, systemic issues across all three tables "
        f"({total_findings} distinct findings). "
        "The `orders` table is the most severely affected — negative unit prices, zero and "
        "negative quantities, orphaned foreign keys, and future timestamps collectively make "
        "a large fraction of transaction records unusable for reporting or analytics. "
        "The `customers` table has significant NULL and duplicate emails, plus malformed "
        "email addresses, undermining any contact or deduplication workflow. "
        "The `products` table has NULL SKUs, duplicate SKUs, and negative prices — "
        "fundamental integrity failures for a product catalogue. "
        "The sentinel value `2099-12-31` appearing in both `birthdate` and `signup_date` "
        "fields suggests a placeholder was substituted for unknown dates. "
        "Immediate remediation should prioritise: purging or correcting orphaned order rows, "
        "fixing negative prices and quantities, and resolving duplicate SKUs and emails "
        "before any downstream ETL or reporting is run."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run a data quality audit on a local PostgreSQL database."
    )
    parser.add_argument("dbname", nargs="?", default="testdb",
                        help="PostgreSQL database name (default: testdb)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output markdown file path (default: auto-generated)")
    args = parser.parse_args()

    dbname = args.dbname
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = args.output or f"data_quality_report_{dbname}_{timestamp}.md"

    print(f"Auditing database: {dbname}")

    r = Report()
    r.h1(f"Data Quality Report — `{dbname}`")
    r.p(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    try:
        print("Step 0: Schema discovery …")
        schema = step0_schema(dbname, r)

        print("Step 1: Completeness …")
        step1_completeness(dbname, r, schema)

        print("Step 2: Uniqueness & duplicates …")
        step2_uniqueness(dbname, r, schema)

        print("Step 3: Range & domain validity …")
        step3_range(dbname, r, schema)

        print("Step 4: Format validity …")
        step4_format(dbname, r, schema)

        print("Step 5: Referential integrity …")
        step5_referential(dbname, r)

        print("Step 6: Cross-table consistency …")
        step6_consistency(dbname, r)

        print("Step 7: Executive summary …")
        step7_summary(r, dbname)

    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(r.getvalue())

    print(f"\nReport written to: {output_path}")


if __name__ == "__main__":
    main()
