#!/usr/bin/env python3
"""
MCP Server for Google BigQuery.

Provides tools to execute SQL queries, browse datasets/tables/schemas,
and explore data in BigQuery using service account key authentication.
"""

import json
import os
import sys
from typing import Optional, List, Dict, Any
from enum import Enum

from pydantic import BaseModel, Field, field_validator, ConfigDict
from mcp.server.fastmcp import FastMCP
from google.cloud import bigquery
from google.oauth2 import service_account

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SERVICE_ACCOUNT_KEY_PATH = os.environ.get("BQ_SERVICE_ACCOUNT_KEY")
DEFAULT_PROJECT_ID = os.environ.get("BQ_PROJECT_ID")
MAX_QUERY_ROWS = 10_000
DEFAULT_QUERY_ROWS = 100

# ---------------------------------------------------------------------------
# Client initialization
# ---------------------------------------------------------------------------


def _get_client(project_id: Optional[str] = None) -> bigquery.Client:
    """Create a BigQuery client using service account credentials."""
    target_project = project_id or DEFAULT_PROJECT_ID
    if not target_project:
        raise ValueError(
            "No project ID provided. Set BQ_PROJECT_ID env var or pass project_id."
        )

    if SERVICE_ACCOUNT_KEY_PATH:
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_KEY_PATH,
            scopes=["https://www.googleapis.com/auth/bigquery"],
        )
        return bigquery.Client(project=target_project, credentials=credentials)
    else:
        # Fall back to Application Default Credentials
        return bigquery.Client(project=target_project)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _handle_error(e: Exception) -> str:
    """Consistent error formatting."""
    from google.api_core.exceptions import (
        Forbidden,
        NotFound,
        BadRequest,
    )

    if isinstance(e, NotFound):
        return f"Error: Resource not found — {e.message}"
    if isinstance(e, Forbidden):
        return (
            f"Error: Permission denied — {e.message}. "
            "Check that the service account has BigQuery Data Viewer and Job User roles."
        )
    if isinstance(e, BadRequest):
        return f"Error: Bad request — {e.message}"
    if isinstance(e, ValueError):
        return f"Error: {e}"
    return f"Error: {type(e).__name__} — {e}"


def _format_rows_as_markdown(rows: List[Dict], schema: List[Any]) -> str:
    """Format query result rows as a markdown table."""
    if not rows:
        return "_No rows returned._"

    headers = [field.name for field in schema]
    col_widths = [len(h) for h in headers]

    str_rows = []
    for row in rows:
        str_row = [str(row.get(h, "")) for h in headers]
        for i, val in enumerate(str_row):
            col_widths[i] = max(col_widths[i], len(val))
        str_rows.append(str_row)

    # Build table
    header_line = "| " + " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)) + " |"
    separator = "| " + " | ".join("-" * col_widths[i] for i in range(len(headers))) + " |"
    data_lines = [
        "| " + " | ".join(val.ljust(col_widths[i]) for i, val in enumerate(row)) + " |"
        for row in str_rows
    ]
    return "\n".join([header_line, separator] + data_lines)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("bigquery_mcp")


# ---------------------------------------------------------------------------
# Enums & Input Models
# ---------------------------------------------------------------------------


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


class ExecuteSqlInput(BaseModel):
    """Input for executing a SQL query."""
    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(
        ...,
        description="GoogleSQL SELECT query to execute. Only SELECT statements are allowed.",
        min_length=1,
    )
    project_id: Optional[str] = Field(
        default=None,
        description="GCP project ID to bill the query to. Uses default if omitted.",
    )
    max_rows: Optional[int] = Field(
        default=DEFAULT_QUERY_ROWS,
        description="Maximum rows to return (default 100, max 10000).",
        ge=1,
        le=MAX_QUERY_ROWS,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' (table) or 'json'.",
    )

    @field_validator("query")
    @classmethod
    def must_be_select(cls, v: str) -> str:
        trimmed = v.strip().lstrip("(").strip()
        if not trimmed.upper().startswith("SELECT") and not trimmed.upper().startswith("WITH"):
            raise ValueError(
                "Only SELECT / WITH queries are allowed. "
                "INSERT, UPDATE, DELETE, and DDL statements are not permitted."
            )
        return v


class ListDatasetsInput(BaseModel):
    """Input for listing datasets."""
    model_config = ConfigDict(str_strip_whitespace=True)

    project_id: Optional[str] = Field(
        default=None,
        description="GCP project ID. Uses default if omitted.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class ListTablesInput(BaseModel):
    """Input for listing tables in a dataset."""
    model_config = ConfigDict(str_strip_whitespace=True)

    dataset_id: str = Field(
        ..., description="Dataset ID to list tables from.", min_length=1
    )
    project_id: Optional[str] = Field(
        default=None,
        description="GCP project ID. Uses default if omitted.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class GetTableSchemaInput(BaseModel):
    """Input for getting table schema."""
    model_config = ConfigDict(str_strip_whitespace=True)

    dataset_id: str = Field(..., description="Dataset ID.", min_length=1)
    table_id: str = Field(..., description="Table ID.", min_length=1)
    project_id: Optional[str] = Field(
        default=None,
        description="GCP project ID. Uses default if omitted.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class PreviewTableInput(BaseModel):
    """Input for previewing table rows."""
    model_config = ConfigDict(str_strip_whitespace=True)

    dataset_id: str = Field(..., description="Dataset ID.", min_length=1)
    table_id: str = Field(..., description="Table ID.", min_length=1)
    project_id: Optional[str] = Field(
        default=None,
        description="GCP project ID. Uses default if omitted.",
    )
    max_rows: Optional[int] = Field(
        default=10,
        description="Number of preview rows (default 10, max 100).",
        ge=1,
        le=100,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class GetTableInfoInput(BaseModel):
    """Input for getting detailed table metadata."""
    model_config = ConfigDict(str_strip_whitespace=True)

    dataset_id: str = Field(..., description="Dataset ID.", min_length=1)
    table_id: str = Field(..., description="Table ID.", min_length=1)
    project_id: Optional[str] = Field(
        default=None,
        description="GCP project ID. Uses default if omitted.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="bq_execute_sql",
    annotations={
        "title": "Execute BigQuery SQL",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bq_execute_sql(params: ExecuteSqlInput) -> str:
    """Execute a read-only GoogleSQL query against BigQuery and return results.

    Only SELECT and WITH statements are permitted. The query is billed to the
    specified project (or the default project). Results are returned as either
    a markdown table or JSON array.

    Args:
        params: Validated query parameters including the SQL string, project,
                max rows, and response format.

    Returns:
        Query results formatted as markdown table or JSON, including row count
        and bytes processed metadata.
    """
    try:
        client = _get_client(params.project_id)
        job_config = bigquery.QueryJobConfig(
            maximum_bytes_billed=10 * 1024 * 1024 * 1024,  # 10 GB safety limit
        )
        query_job = client.query(params.query, job_config=job_config)
        results = query_job.result(max_results=params.max_rows)

        # Capture schema before consuming the iterator
        result_schema = results.schema
        rows = [dict(row) for row in results]
        total_rows = results.total_rows or len(rows)
        bytes_processed = query_job.total_bytes_processed or 0

        # Serialise non-JSON-safe types
        for row in rows:
            for k, v in row.items():
                if hasattr(v, "isoformat"):
                    row[k] = v.isoformat()
                elif isinstance(v, bytes):
                    row[k] = v.hex()
                elif not isinstance(v, (str, int, float, bool, type(None), list, dict)):
                    row[k] = str(v)

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(
                {
                    "rows": rows,
                    "row_count": len(rows),
                    "total_rows": total_rows,
                    "bytes_processed": bytes_processed,
                    "truncated": len(rows) < total_rows,
                },
                indent=2,
                default=str,
            )

        # Markdown
        mb = bytes_processed / (1024 * 1024)
        meta = f"**{len(rows)}** of **{total_rows}** rows returned | **{mb:.2f} MB** processed\n\n"
        table = _format_rows_as_markdown(rows, result_schema)
        if len(rows) < total_rows:
            table += f"\n\n_Results truncated. Set `max_rows` up to {MAX_QUERY_ROWS} to see more._"
        return meta + table

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="bq_list_datasets",
    annotations={
        "title": "List BigQuery Datasets",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bq_list_datasets(params: ListDatasetsInput) -> str:
    """List all datasets in a BigQuery project.

    Returns dataset IDs, descriptions, and locations. Useful for discovering
    what data is available before querying.

    Args:
        params: Project ID and response format.

    Returns:
        List of datasets in markdown or JSON format.
    """
    try:
        target_project = params.project_id or DEFAULT_PROJECT_ID
        client = _get_client(params.project_id)
        datasets = list(client.list_datasets(project=target_project))

        if not datasets:
            return f"No datasets found in project `{target_project}`."

        items = []
        for ds in datasets:
            # DatasetListItem doesn't expose .location; fetch full
            # dataset only if we need it — but to keep it lightweight,
            # we just omit location here.
            items.append(
                {
                    "dataset_id": ds.dataset_id,
                    "full_id": f"{ds.project}.{ds.dataset_id}",
                }
            )

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"datasets": items, "count": len(items)}, indent=2)

        lines = [f"## Datasets in `{params.project_id or DEFAULT_PROJECT_ID}`\n"]
        for item in items:
            lines.append(f"- **{item['dataset_id']}** (`{item['full_id']}`)")
        lines.append(f"\n_{len(items)} dataset(s) found._")
        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="bq_list_tables",
    annotations={
        "title": "List Tables in Dataset",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bq_list_tables(params: ListTablesInput) -> str:
    """List all tables and views in a BigQuery dataset.

    Returns table names, types (TABLE/VIEW/MATERIALIZED_VIEW), and row counts.

    Args:
        params: Dataset ID, project ID, and response format.

    Returns:
        List of tables with metadata in markdown or JSON format.
    """
    try:
        client = _get_client(params.project_id)
        dataset_ref = client.dataset(params.dataset_id)
        tables = list(client.list_tables(dataset_ref))

        if not tables:
            return f"No tables found in dataset `{params.dataset_id}`."

        items = []
        for t in tables:
            items.append(
                {
                    "table_id": t.table_id,
                    "type": t.table_type,
                    "full_id": f"{t.project}.{t.dataset_id}.{t.table_id}",
                }
            )

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"tables": items, "count": len(items)}, indent=2)

        lines = [f"## Tables in `{params.dataset_id}`\n"]
        for item in items:
            lines.append(f"- **{item['table_id']}** ({item['type']})")
        lines.append(f"\n_{len(items)} table(s) found._")
        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="bq_get_table_schema",
    annotations={
        "title": "Get Table Schema",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bq_get_table_schema(params: GetTableSchemaInput) -> str:
    """Get the column schema for a BigQuery table or view.

    Returns column names, types, modes (NULLABLE/REQUIRED/REPEATED),
    and descriptions. Essential for writing correct queries.

    Args:
        params: Dataset ID, table ID, project ID, and response format.

    Returns:
        Table schema in markdown or JSON format.
    """
    try:
        client = _get_client(params.project_id)
        table_ref = client.dataset(params.dataset_id).table(params.table_id)
        table = client.get_table(table_ref)

        def _schema_to_dict(fields) -> List[Dict]:
            result = []
            for f in fields:
                entry = {
                    "name": f.name,
                    "type": f.field_type,
                    "mode": f.mode,
                    "description": f.description or "",
                }
                if f.fields:
                    entry["fields"] = _schema_to_dict(f.fields)
                result.append(entry)
            return result

        schema_list = _schema_to_dict(table.schema)

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(
                {
                    "table": f"{params.dataset_id}.{params.table_id}",
                    "num_columns": len(schema_list),
                    "schema": schema_list,
                },
                indent=2,
            )

        lines = [f"## Schema: `{params.dataset_id}.{params.table_id}`\n"]
        lines.append("| Column | Type | Mode | Description |")
        lines.append("| ------ | ---- | ---- | ----------- |")
        for col in schema_list:
            lines.append(
                f"| {col['name']} | {col['type']} | {col['mode']} | {col['description']} |"
            )
        lines.append(f"\n_{len(schema_list)} column(s)._")
        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="bq_preview_table",
    annotations={
        "title": "Preview Table Data",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bq_preview_table(params: PreviewTableInput) -> str:
    """Preview the first N rows of a BigQuery table without running a query.

    Uses the BigQuery Storage Read API for fast, free preview reads.

    Args:
        params: Dataset ID, table ID, number of rows, project ID, format.

    Returns:
        Preview rows as a markdown table or JSON array.
    """
    try:
        client = _get_client(params.project_id)
        table_ref = client.dataset(params.dataset_id).table(params.table_id)
        table = client.get_table(table_ref)

        rows_iter = client.list_rows(table, max_results=params.max_rows)
        rows = [dict(row) for row in rows_iter]

        # Serialise
        for row in rows:
            for k, v in row.items():
                if hasattr(v, "isoformat"):
                    row[k] = v.isoformat()
                elif isinstance(v, bytes):
                    row[k] = v.hex()
                elif not isinstance(v, (str, int, float, bool, type(None), list, dict)):
                    row[k] = str(v)

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(
                {"rows": rows, "count": len(rows), "total_rows": table.num_rows},
                indent=2,
                default=str,
            )

        meta = f"**Preview**: {len(rows)} of {table.num_rows:,} rows in `{params.dataset_id}.{params.table_id}`\n\n"
        md_table = _format_rows_as_markdown(rows, table.schema)
        return meta + md_table

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="bq_get_table_info",
    annotations={
        "title": "Get Table Metadata",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bq_get_table_info(params: GetTableInfoInput) -> str:
    """Get detailed metadata about a BigQuery table or view.

    Returns row count, size, creation/modification dates, partitioning,
    clustering, and description.

    Args:
        params: Dataset ID, table ID, project ID, and response format.

    Returns:
        Table metadata in markdown or JSON format.
    """
    try:
        client = _get_client(params.project_id)
        table_ref = client.dataset(params.dataset_id).table(params.table_id)
        table = client.get_table(table_ref)

        size_mb = (table.num_bytes or 0) / (1024 * 1024)

        info = {
            "full_id": f"{table.project}.{table.dataset_id}.{table.table_id}",
            "type": table.table_type,
            "num_rows": table.num_rows,
            "size_mb": round(size_mb, 2),
            "created": table.created.isoformat() if table.created else None,
            "modified": table.modified.isoformat() if table.modified else None,
            "description": table.description or "",
            "partitioning": str(table.time_partitioning) if table.time_partitioning else None,
            "clustering_fields": table.clustering_fields or [],
            "num_columns": len(table.schema),
        }

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(info, indent=2)

        lines = [f"## Table Info: `{info['full_id']}`\n"]
        lines.append(f"- **Type**: {info['type']}")
        lines.append(f"- **Rows**: {info['num_rows']:,}" if info['num_rows'] else "- **Rows**: unknown")
        lines.append(f"- **Size**: {info['size_mb']} MB")
        lines.append(f"- **Columns**: {info['num_columns']}")
        if info["description"]:
            lines.append(f"- **Description**: {info['description']}")
        if info["created"]:
            lines.append(f"- **Created**: {info['created']}")
        if info["modified"]:
            lines.append(f"- **Modified**: {info['modified']}")
        if info["partitioning"]:
            lines.append(f"- **Partitioning**: {info['partitioning']}")
        if info["clustering_fields"]:
            lines.append(f"- **Clustering**: {', '.join(info['clustering_fields'])}")
        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
