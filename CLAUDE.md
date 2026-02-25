# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Two things live in this repo:

1. **BigQuery MCP Server** (`bigquery_mcp.py`) — A single-file FastMCP server exposing read-only BigQuery tools to Claude Desktop / Claude Code. Six tools: `bq_execute_sql`, `bq_list_datasets`, `bq_list_tables`, `bq_get_table_schema`, `bq_preview_table`, `bq_get_table_info`.
2. **Drilling Incentive Reports** — Two standalone scripts that query BigQuery and generate Excel workbooks for monthly drilling performance incentive payouts:
   - `generate_incentive_report.py` — Python-computed incentives, writes summary Excel.
   - `generate_incentive_excel.py` — Writes raw data + Excel formulas for full traceability. This is the primary/active version.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt
pip install openpyxl          # additionally needed for incentive reports
pip install pytest pytest-asyncio  # for tests

# Run MCP server
export BQ_SERVICE_ACCOUNT_KEY="/path/to/service-account-key.json"
export BQ_PROJECT_ID="looplink-drt"
python bigquery_mcp.py

# Run tests (all mocked, no BigQuery access needed)
pytest test_bigquery_mcp.py -v

# Run a single test
pytest test_bigquery_mcp.py::TestBqExecuteSql::test_markdown_output -v

# Generate incentive report (requires GCP credentials)
python generate_incentive_excel.py --month 2026-01
python generate_incentive_excel.py --month 2026-01 --output custom_path.xlsx
```

## Architecture

### MCP Server (`bigquery_mcp.py`)

Single-module design. Top-to-bottom: config constants, `_get_client()` (creates BigQuery client per request via service account or ADC fallback), Pydantic input models (one per tool with field validation), `@mcp.tool()` async handlers returning formatted strings (markdown table or JSON).

Key constraints:
- All tools are read-only. `ExecuteSqlInput.must_be_select` validator rejects non-SELECT/WITH queries.
- 10 GB `maximum_bytes_billed` cap on queries.
- Query results default to 100 rows (max 10,000). Preview defaults to 10 rows (max 100).
- Every tool accepts a `response_format` parameter: `"markdown"` (default) or `"json"`.
- Two GCP projects: `looplink-drt` (default) and `dandelion-tf-production`.

### Incentive Reports

Both scripts hardcode `CREDENTIALS_PATH` and target `dandelion-tf-production`. They query two BigQuery tables:
- `reporting.drilling_master_data` — bore records with footage, status, QA status
- `dbt_dandelion_operations.fct__ops_timesheets` — daily labor hours by person/project/task

Incentive logic: footage qualifies when `status='Complete'` AND `qa_status IN ('Pass 1','Pass 2')` AND `qa_date` falls in the target month. Footage is attributed to workers proportionally by hours worked. Payout uses a two-segment rate: floor (150 ft/day) to OTE (300 ft/day) at a linear rate, then a higher per-foot rate above OTE. Helpers get a 2x crew multiplier on credited footage.

`generate_incentive_excel.py` is the preferred version — it writes raw data to sheets then builds all calculations with Excel formulas (SUMIFS, VLOOKUP, IF) referencing a Parameters sheet, so the output is fully auditable.

A `PROJECT_NAME_MAP` dict reconciles mismatched project names between the two data sources (e.g., "East Creek Farm" → "East Creek").

## Testing

Tests are in `test_bigquery_mcp.py` and cover only the MCP server. All BigQuery calls are mocked with `unittest.mock`. Tests use `pytest-asyncio` for the async tool handlers. No tests exist for the incentive report scripts.

## Dependencies

- `mcp[cli]` (FastMCP framework)
- `google-cloud-bigquery`, `google-auth`
- `pydantic`
- `openpyxl` (incentive reports only)
