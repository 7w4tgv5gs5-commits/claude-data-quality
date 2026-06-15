# Claude Data Quality Demo

A demonstration project built with [Claude Code](https://claude.ai/code) that shows how an AI coding assistant can be used to:

- Generate intentionally dirty synthetic data
- Audit data quality via a custom slash command
- Transform a normalised schema into a Kimball star schema
- Explore and visualise the results through a web application

---

## Architecture

```
generate_data.py
      │
      ▼
  testdb (dirty source data)
      │
      ├── /data-quality slash command  ──→  markdown audit report
      │
      └── ETL (build_kimball.sql / docker/seed.py)
                │
                ▼
        testdb_kimball (star schema)
                │
                ▼
         webapp (FastAPI)  ──→  http://localhost:8765
```

---

## Prerequisites

**Local**
- Python 3.11+
- PostgreSQL 18 running on port 5432
- `pip install -r webapp/requirements.txt`

**Docker**
- Docker Desktop (or Docker Engine + Compose v2)

---

## Quick start

### Docker (recommended)

```bash
docker compose up --build
```

This pulls `postgres:17-alpine`, builds the image, seeds both databases, and starts the web app. Open **http://localhost:8765** once the seeder exits.

```bash
# Subsequent runs (data persists in a named volume)
docker compose up

# Full reseed from scratch
docker compose down -v && docker compose up --build
```

### Local

```bash
# 1. Generate dirty source data in testdb
python generate_data.py --reset

# 2. Build the Kimball star schema in testdb_kimball
psql -d postgres -f build_kimball.sql

# 3. Start the web app
cd webapp
uvicorn main:app --reload --port 8765
```

---

## Databases

### `testdb` — source (intentionally dirty)

Three tables populated with synthetic Australian data via [Faker](https://faker.readthedocs.io/).

| Table | Rows (default) | Description |
|---|---|---|
| `customers` | 200 | Names, emails, phone numbers, birthdates, signup dates |
| `products` | 66 | SKUs, categories, prices, stock levels |
| `orders` | 600 | Customer × product order lines with quantities and prices |

Deliberate quality issues seeded across all tables:

| Category | Examples |
|---|---|
| Completeness | NULL emails, NULL names, NULL SKUs |
| Uniqueness | Duplicate emails, duplicate SKUs |
| Range validity | Negative prices, negative stock, future birthdates, orders dated 2099 |
| Format validity | Malformed emails (`user @`, `user@`), non-standard SKU patterns |
| Referential integrity | Orders referencing non-existent customer/product IDs |
| Cross-table consistency | `unit_price` deviating significantly from `list_price` |

Regenerate at any time:

```bash
python generate_data.py --reset            # default: 200 customers
python generate_data.py --reset --rows 500 # larger dataset
python generate_data.py --reset --seed 99  # different random seed
```

### `testdb_kimball` — Kimball star schema

| Table | Rows | Grain / description |
|---|---|---|
| `dim_date` | 4,384 | Calendar 2020–2031; `date_sk = -1` sentinel for NULL dates |
| `dim_customer` | 201 | One row per source customer; `customer_sk = 0` Unknown sentinel |
| `dim_product` | 67 | One row per source product; `product_sk = 0` Unknown sentinel |
| `fact_orders` | 600 | One row per source order line; FK constraints enforced |

All orphaned or NULL foreign keys in `fact_orders` are routed to sentinel dimension members rather than being dropped, so every source row is preserved.

---

## Web application

A FastAPI + Jinja2 application with a "Commonwealth Digital" design theme (navy sidebar, government blue accents).

| Page | URL | Description |
|---|---|---|
| Dashboard | `/` | KPI cards, revenue-by-category chart, orders-over-time chart, top customers |
| Customers | `/customers` | Live search by name/email with country filter |
| Products | `/products` | Live search by name/SKU with category filter; inline issue badges |
| Orders | `/orders` | Live search with date-range picker; flags dirty rows inline |
| Data Quality | `/data-quality` | Live audit panel — 24 checks grouped by table |

All search fields update results in real time with a 270 ms debounce. The `/api/*` endpoints return JSON and can be called independently.

---

## `/data-quality` slash command

Requires [Claude Code](https://claude.ai/code) with this repository open.

```
/data-quality [dbname]
```

Runs a seven-step audit against the target database (defaults to `testdb`):

1. **Schema discovery** — lists tables, columns, row counts
2. **Completeness** — NULL counts per column
3. **Uniqueness** — duplicate values on key columns
4. **Range validity** — negative prices, impossible dates, statistical outliers
5. **Format validity** — email regex, phone digit count, SKU pattern
6. **Referential integrity** — orphaned foreign keys
7. **Cross-table consistency** — `unit_price` vs. `list_price` divergence

Produces a structured markdown report with an executive summary table and severity classification (ERROR / WARNING / INFO).

---

## Repository layout

```
├── generate_data.py        # synthetic data generator (Australian locale, en_AU Faker)
├── build_kimball.sql       # one-shot SQL ETL for local postgres (uses postgres_fdw)
├── docker-compose.yml      # three-service stack: db · seeder · app
├── .dockerignore
├── docker/
│   └── seed.py             # Python ETL seeder used inside Docker (no postgres_fdw)
├── webapp/
│   ├── Dockerfile
│   ├── main.py             # FastAPI application and API endpoints
│   ├── requirements.txt
│   └── templates/          # Jinja2 templates (base, dashboard, customers, products, orders, data_quality)
└── .claude/
    └── commands/
        └── data-quality.md # slash command definition
```

---

## Environment variables

The web app and Docker seeder read the following variables (all optional locally):

| Variable | Default | Description |
|---|---|---|
| `PGHOST` | *(Unix socket)* | PostgreSQL host |
| `PGUSER` | *(current user)* | PostgreSQL user |
| `PGPASSWORD` | *(none)* | PostgreSQL password |
| `DB_NAME` | `testdb_kimball` | Target database for the web app |
