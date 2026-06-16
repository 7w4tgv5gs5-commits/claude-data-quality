# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

This project demonstrates using Claude Code as a data quality auditing tool against a local PostgreSQL database. It has grown to include a Kimball star schema transformation and a web application for exploring and visualising the data.

## Repository layout

```
dm/
├── generate_data.py        # synthetic dirty data generator (Australian locale)
├── build_kimball.sql       # one-shot SQL to build testdb_kimball from testdb (local only)
├── docker-compose.yml      # full stack: postgres + seeder + web app
├── .dockerignore
├── erd.html                # standalone ER diagram for testdb + testdb_kimball (Mermaid.js)
├── data-quality-report.html # last-run HTML data quality report (Commonwealth Digital theme)
├── docker/
│   └── seed.py             # Python ETL seeder used by the Docker seeder service
├── webapp/
│   ├── Dockerfile          # build context is project root (dm/)
│   ├── main.py             # FastAPI application
│   ├── requirements.txt
│   └── templates/          # Jinja2 HTML templates
│       ├── base.html
│       ├── dashboard.html
│       ├── customers.html
│       ├── products.html
│       ├── orders.html
│       └── data_quality.html
└── .claude/
    └── commands/
        └── data-quality.md # /data-quality slash command definition
```

## Databases

### testdb (source — intentionally dirty)
- **Engine:** PostgreSQL 18 (Homebrew), running locally on port 5432
- **Connect:** `psql testdb`
- **Tables:** `customers`, `products`, `orders`
- **Locale:** Australian (en_AU Faker) — phone numbers, names, email domains
- Data contains deliberate quality issues: NULL required fields, duplicate emails/SKUs,
  negative prices, impossible dates, orphaned foreign keys, format violations, statistical outliers.

### testdb_kimball (Kimball star schema)
- **Connect:** `psql testdb_kimball`
- **Tables:** `dim_date`, `dim_customer`, `dim_product`, `fact_orders`
- Built from `testdb` via `build_kimball.sql` (local) or `docker/seed.py` (Docker).
- Orphaned/NULL foreign keys are routed to Unknown sentinel members (sk=0 / date_sk=-1)
  so no fact rows are lost.
- Known ETL defect: `dim_date.day_name` and `month_name` have trailing whitespace from
  `TO_CHAR` padding — use `TRIM()` when filtering on those columns.

## Regenerating data

```bash
# Recreate testdb with fresh Australian synthetic data
python generate_data.py --reset

# Rebuild testdb_kimball from the new testdb (local postgres only)
psql -d postgres -f build_kimball.sql
```

## Web application

A FastAPI app (`webapp/main.py`) provides a browser UI over `testdb_kimball`.

```bash
# Run locally (requires testdb_kimball on local postgres)
cd webapp
uvicorn main:app --reload --port 8765
# → http://localhost:8765
```

**Pages:** Dashboard (KPIs + charts), Customers, Products, Orders, Data Quality  
**API:** `/api/kpis`, `/api/revenue-by-category`, `/api/orders-over-time`,
`/api/top-customers`, `/api/customers/search`, `/api/products/search`,
`/api/orders/search`, `/api/data-quality`

DB connection is driven by env vars (`PGHOST`, `PGUSER`, `PGPASSWORD`, `DB_NAME`);
falls back to local Unix socket with `testdb_kimball` when unset.

**Design:** "Commonwealth Digital" — light grey background, navy sidebar (#1c3a5e),
government blue accent (#1d6fa4), Plus Jakarta Sans + IBM Plex Mono typography.

## Docker

Runs the full stack (postgres + seeder + web app) in containers.

```bash
# First run — builds image, seeds both databases, starts app
docker compose up --build

# Subsequent runs (data already seeded in named volume)
docker compose up

# Reseed from scratch
docker compose down -v && docker compose up --build
# → http://localhost:8765
```

The `seeder` service runs `docker/seed.py` which calls `generate_data.py` then
performs the Kimball ETL in Python (no postgres_fdw required). It exits cleanly
before the `app` service starts (`service_completed_successfully` condition).

## Slash command

`/data-quality [dbname] [--html]` — defined in `.claude/commands/data-quality.md`

Runs a seven-step data quality audit (schema discovery → completeness → uniqueness →
range validity → format validity → referential integrity → cross-table consistency)
and produces a structured markdown report with an executive summary table.
Defaults to `testdb` if no database name is passed.

Pass `--html` to also write `data-quality-report.html` — a fully styled, self-contained
HTML report in the Commonwealth Digital theme (sticky sidebar, KPI strip, per-step cards
with NULL bar charts and severity badges, executive summary findings table). The markdown
report is always emitted to the conversation regardless of the flag.

To add a new check, edit `.claude/commands/data-quality.md` and add a numbered step
following the existing pattern (state what/why, provide SQL, classify ERROR/WARNING/INFO).

## Static HTML artefacts

Two standalone HTML files live in the project root (open directly in any browser):

- **`erd.html`** — Entity relationship diagrams for both databases rendered with Mermaid.js.
  Two diagrams: testdb (3 tables, implied FK relationships) and testdb_kimball (star schema,
  enforced FKs). Includes the known TO_CHAR trailing-whitespace defect note for dim_date.

- **`data-quality-report.html`** — Last-run output of `/data-quality --html`. Commonwealth
  Digital theme. Regenerate by running `/data-quality --html` (or `--html mydb`).
