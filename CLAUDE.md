# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A single-file MCP (Model Context Protocol) server for Google BigQuery. It exposes read-only BigQuery tools to Claude Desktop / Claude Code via the FastMCP framework. The entire server is in `bigquery_mcp.py`.

## Running

```bash
# Install dependencies
pip install -r requirements.txt

# Set required environment variables
export BQ_SERVICE_ACCOUNT_KEY="/path/to/service-account-key.json"
export BQ_PROJECT_ID="looplink-drt"

# Run the MCP server
python bigquery_mcp.py
```

## Architecture

Single-module design in `bigquery_mcp.py`:

- **Configuration** (top): env vars `BQ_SERVICE_ACCOUNT_KEY` and `BQ_PROJECT_ID`, constants for row limits
- **`_get_client()`**: Creates a `bigquery.Client` per request using service account credentials (or falls back to ADC)
- **Pydantic input models**: One `BaseModel` per tool (`ExecuteSqlInput`, `ListDatasetsInput`, etc.) with field validation. `ExecuteSqlInput.must_be_select` validator enforces read-only queries.
- **Tool functions**: Six `@mcp.tool()` async handlers, each accepting a Pydantic model and returning formatted strings (markdown table or JSON)
- **Safety**: 10 GB `maximum_bytes_billed` cap on queries; only SELECT/WITH statements allowed

## Key Constraints

- All tools are read-only; no INSERT/UPDATE/DELETE/DDL allowed
- Two GCP projects are used: `looplink-drt` (default) and `dandelion-tf-production`
- Query results default to 100 rows, max 10,000
- Table preview (`bq_preview_table`) uses `list_rows` (free, no query cost)
- All tools support `response_format` parameter: `"markdown"` (default) or `"json"`

## Dependencies

- `mcp[cli]` (FastMCP framework)
- `google-cloud-bigquery`
- `google-auth`
- `pydantic`
