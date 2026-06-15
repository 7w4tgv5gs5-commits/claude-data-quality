# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

This project demonstrates using Claude Code as a data quality auditing tool against a local PostgreSQL database. It is not a traditional software application — there is no build step, no test runner, and no source code to compile. The primary artefact is the `/data-quality` slash command.

## Database

- **Engine:** PostgreSQL 18 (Homebrew), running locally on port 5432
- **Database:** `testdb`
- **Connect:** `psql testdb`
- **Tables:** `customers`, `products`, `orders`

The `testdb` dataset is intentionally dirty and is used for demonstrating data quality checks. It contains deliberate issues: NULL required fields, duplicate emails/SKUs, negative prices, impossible dates, orphaned foreign keys, format violations, and statistical outliers.

## Slash command

`/data-quality [dbname]` — defined in `.claude/commands/data-quality.md`

Runs a six-step data quality audit (completeness → uniqueness → range validity → format validity → referential integrity → cross-table consistency) and produces a structured markdown report with an executive summary table. Defaults to `testdb` if no database name is passed.

## How the slash command works

`.claude/commands/*.md` files define project-level slash commands. When invoked, Claude Code reads the markdown file and executes the instructions inside it using its available tools (primarily `Bash` for `psql` calls). No code is executed at install time — the file is pure instruction prose.

## Extending the skill

To add a new check, edit `.claude/commands/data-quality.md` and add a new numbered step following the existing pattern. Each step should:
1. State what it checks and why
2. Provide the SQL to run (parametrised by table/column names discovered in Step 0)
3. Describe how to classify findings (ERROR / WARNING / INFO)
