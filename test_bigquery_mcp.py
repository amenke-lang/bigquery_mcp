"""Tests for bigquery_mcp.py — all BigQuery calls are mocked."""

import json
from datetime import datetime, date
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from pydantic import ValidationError

from bigquery_mcp import (
    ExecuteSqlInput,
    ListDatasetsInput,
    ListTablesInput,
    GetTableSchemaInput,
    PreviewTableInput,
    GetTableInfoInput,
    ResponseFormat,
    _format_rows_as_markdown,
    _handle_error,
    _get_client,
    bq_execute_sql,
    bq_list_datasets,
    bq_list_tables,
    bq_get_table_schema,
    bq_preview_table,
    bq_get_table_info,
)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestExecuteSqlInputValidation:
    def test_rejects_insert(self):
        with pytest.raises(ValidationError):
            ExecuteSqlInput(query="INSERT INTO t VALUES (1)")

    def test_rejects_update(self):
        with pytest.raises(ValidationError):
            ExecuteSqlInput(query="UPDATE t SET x=1")

    def test_rejects_delete(self):
        with pytest.raises(ValidationError):
            ExecuteSqlInput(query="DELETE FROM t")

    def test_rejects_drop(self):
        with pytest.raises(ValidationError):
            ExecuteSqlInput(query="DROP TABLE t")

    def test_rejects_create(self):
        with pytest.raises(ValidationError):
            ExecuteSqlInput(query="CREATE TABLE t (id INT)")

    def test_accepts_select(self):
        inp = ExecuteSqlInput(query="SELECT 1")
        assert inp.query == "SELECT 1"

    def test_accepts_select_lowercase(self):
        inp = ExecuteSqlInput(query="select * from t")
        assert inp.query == "select * from t"

    def test_accepts_with(self):
        inp = ExecuteSqlInput(query="WITH cte AS (SELECT 1) SELECT * FROM cte")
        assert "WITH" in inp.query

    def test_accepts_leading_whitespace(self):
        inp = ExecuteSqlInput(query="  SELECT 1")
        assert inp.query.strip() == "SELECT 1"

    def test_accepts_leading_paren(self):
        inp = ExecuteSqlInput(query="(SELECT 1)")
        assert inp.query == "(SELECT 1)"

    def test_rejects_empty_query(self):
        with pytest.raises(ValidationError):
            ExecuteSqlInput(query="")

    def test_max_rows_default(self):
        inp = ExecuteSqlInput(query="SELECT 1")
        assert inp.max_rows == 100

    def test_max_rows_upper_bound(self):
        with pytest.raises(ValidationError):
            ExecuteSqlInput(query="SELECT 1", max_rows=10_001)

    def test_max_rows_lower_bound(self):
        with pytest.raises(ValidationError):
            ExecuteSqlInput(query="SELECT 1", max_rows=0)


class TestPreviewTableInputValidation:
    def test_max_rows_default(self):
        inp = PreviewTableInput(dataset_id="ds", table_id="tbl")
        assert inp.max_rows == 10

    def test_max_rows_upper_bound(self):
        with pytest.raises(ValidationError):
            PreviewTableInput(dataset_id="ds", table_id="tbl", max_rows=101)

    def test_rejects_empty_dataset_id(self):
        with pytest.raises(ValidationError):
            PreviewTableInput(dataset_id="", table_id="tbl")

    def test_rejects_empty_table_id(self):
        with pytest.raises(ValidationError):
            PreviewTableInput(dataset_id="ds", table_id="")


class TestListTablesInputValidation:
    def test_rejects_empty_dataset_id(self):
        with pytest.raises(ValidationError):
            ListTablesInput(dataset_id="")


class TestGetTableSchemaInputValidation:
    def test_rejects_empty_fields(self):
        with pytest.raises(ValidationError):
            GetTableSchemaInput(dataset_id="", table_id="tbl")
        with pytest.raises(ValidationError):
            GetTableSchemaInput(dataset_id="ds", table_id="")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _make_schema_field(name):
    """Create a mock schema field with a .name attribute."""
    field = MagicMock()
    field.name = name
    return field


class TestFormatRowsAsMarkdown:
    def test_empty_rows(self):
        assert _format_rows_as_markdown([], []) == "_No rows returned._"

    def test_single_row(self):
        schema = [_make_schema_field("id"), _make_schema_field("name")]
        rows = [{"id": "1", "name": "Alice"}]
        result = _format_rows_as_markdown(rows, schema)
        assert "| id" in result
        assert "| name" in result
        assert "Alice" in result
        lines = result.strip().split("\n")
        assert len(lines) == 3  # header, separator, 1 data row

    def test_multiple_rows(self):
        schema = [_make_schema_field("x")]
        rows = [{"x": "a"}, {"x": "bb"}, {"x": "ccc"}]
        result = _format_rows_as_markdown(rows, schema)
        lines = result.strip().split("\n")
        assert len(lines) == 5  # header, separator, 3 data rows

    def test_column_width_adapts(self):
        schema = [_make_schema_field("c")]
        rows = [{"c": "longvalue"}]
        result = _format_rows_as_markdown(rows, schema)
        # separator dashes should be at least as wide as "longvalue"
        sep_line = result.strip().split("\n")[1]
        dashes = sep_line.replace("|", "").strip()
        assert len(dashes) >= len("longvalue")


class TestHandleError:
    def test_not_found(self):
        from google.api_core.exceptions import NotFound
        result = _handle_error(NotFound("table missing"))
        assert "not found" in result.lower()

    def test_forbidden(self):
        from google.api_core.exceptions import Forbidden
        result = _handle_error(Forbidden("no access"))
        assert "Permission denied" in result

    def test_bad_request(self):
        from google.api_core.exceptions import BadRequest
        result = _handle_error(BadRequest("syntax error"))
        assert "Bad request" in result

    def test_value_error(self):
        result = _handle_error(ValueError("no project"))
        assert "no project" in result

    def test_generic_exception(self):
        result = _handle_error(RuntimeError("boom"))
        assert "RuntimeError" in result
        assert "boom" in result


# ---------------------------------------------------------------------------
# Client initialization
# ---------------------------------------------------------------------------


class TestGetClient:
    @patch.dict("os.environ", {}, clear=True)
    @patch("bigquery_mcp.SERVICE_ACCOUNT_KEY_PATH", None)
    @patch("bigquery_mcp.DEFAULT_PROJECT_ID", None)
    def test_no_project_raises(self):
        with pytest.raises(ValueError, match="No project ID"):
            _get_client()

    @patch("bigquery_mcp.SERVICE_ACCOUNT_KEY_PATH", "/fake/key.json")
    @patch("bigquery_mcp.service_account.Credentials.from_service_account_file")
    @patch("bigquery_mcp.bigquery.Client")
    def test_uses_service_account(self, mock_client_cls, mock_creds):
        mock_creds.return_value = MagicMock()
        _get_client("my-project")
        mock_creds.assert_called_once()
        mock_client_cls.assert_called_once()
        assert mock_client_cls.call_args[1]["project"] == "my-project"

    @patch("bigquery_mcp.SERVICE_ACCOUNT_KEY_PATH", None)
    @patch("bigquery_mcp.bigquery.Client")
    def test_falls_back_to_adc(self, mock_client_cls):
        _get_client("my-project")
        mock_client_cls.assert_called_once_with(project="my-project")


# ---------------------------------------------------------------------------
# Tool functions (mocked BigQuery client)
# ---------------------------------------------------------------------------


def _mock_schema():
    """Return mock schema fields for testing."""
    return [_make_schema_field("id"), _make_schema_field("name")]


@pytest.mark.asyncio
class TestBqExecuteSql:
    @patch("bigquery_mcp._get_client")
    async def test_markdown_output(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        mock_job = MagicMock()
        mock_job.total_bytes_processed = 1024
        mock_client.query.return_value = mock_job

        mock_results = MagicMock()
        mock_results.schema = _mock_schema()
        mock_results.total_rows = 1
        mock_results.__iter__ = lambda self: iter([{"id": "1", "name": "Alice"}])
        mock_job.result.return_value = mock_results

        params = ExecuteSqlInput(query="SELECT 1")
        result = await bq_execute_sql(params)
        assert "Alice" in result
        assert "rows returned" in result

    @patch("bigquery_mcp._get_client")
    async def test_json_output(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        mock_job = MagicMock()
        mock_job.total_bytes_processed = 2048
        mock_client.query.return_value = mock_job

        mock_results = MagicMock()
        mock_results.schema = _mock_schema()
        mock_results.total_rows = 1
        mock_results.__iter__ = lambda self: iter([{"id": "1", "name": "Bob"}])
        mock_job.result.return_value = mock_results

        params = ExecuteSqlInput(query="SELECT 1", response_format="json")
        result = await bq_execute_sql(params)
        parsed = json.loads(result)
        assert parsed["row_count"] == 1
        assert parsed["rows"][0]["name"] == "Bob"

    @patch("bigquery_mcp._get_client")
    async def test_truncation_message(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        mock_job = MagicMock()
        mock_job.total_bytes_processed = 0
        mock_client.query.return_value = mock_job

        mock_results = MagicMock()
        mock_results.schema = _mock_schema()
        mock_results.total_rows = 500
        mock_results.__iter__ = lambda self: iter([{"id": "1", "name": "A"}])
        mock_job.result.return_value = mock_results

        params = ExecuteSqlInput(query="SELECT 1", max_rows=1)
        result = await bq_execute_sql(params)
        assert "truncated" in result.lower()

    @patch("bigquery_mcp._get_client")
    async def test_serializes_dates(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        mock_job = MagicMock()
        mock_job.total_bytes_processed = 0
        mock_client.query.return_value = mock_job

        dt = datetime(2025, 1, 15, 12, 0, 0)
        mock_results = MagicMock()
        mock_results.schema = [_make_schema_field("ts")]
        mock_results.total_rows = 1
        mock_results.__iter__ = lambda self: iter([{"ts": dt}])
        mock_job.result.return_value = mock_results

        params = ExecuteSqlInput(query="SELECT 1", response_format="json")
        result = await bq_execute_sql(params)
        parsed = json.loads(result)
        assert parsed["rows"][0]["ts"] == "2025-01-15T12:00:00"

    @patch("bigquery_mcp._get_client", side_effect=ValueError("No project"))
    async def test_error_handling(self, mock_gc):
        params = ExecuteSqlInput(query="SELECT 1")
        result = await bq_execute_sql(params)
        assert "Error" in result
        assert "No project" in result


@pytest.mark.asyncio
class TestBqListDatasets:
    @patch("bigquery_mcp.DEFAULT_PROJECT_ID", "test-proj")
    @patch("bigquery_mcp._get_client")
    async def test_markdown_output(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        ds = MagicMock()
        ds.dataset_id = "my_dataset"
        ds.project = "test-proj"
        mock_client.list_datasets.return_value = [ds]

        params = ListDatasetsInput()
        result = await bq_list_datasets(params)
        assert "my_dataset" in result

    @patch("bigquery_mcp.DEFAULT_PROJECT_ID", "test-proj")
    @patch("bigquery_mcp._get_client")
    async def test_json_output(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        ds = MagicMock()
        ds.dataset_id = "ds1"
        ds.project = "test-proj"
        mock_client.list_datasets.return_value = [ds]

        params = ListDatasetsInput(response_format="json")
        result = await bq_list_datasets(params)
        parsed = json.loads(result)
        assert parsed["count"] == 1
        assert parsed["datasets"][0]["dataset_id"] == "ds1"

    @patch("bigquery_mcp.DEFAULT_PROJECT_ID", "test-proj")
    @patch("bigquery_mcp._get_client")
    async def test_empty_datasets(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client
        mock_client.list_datasets.return_value = []

        params = ListDatasetsInput()
        result = await bq_list_datasets(params)
        assert "No datasets found" in result


@pytest.mark.asyncio
class TestBqListTables:
    @patch("bigquery_mcp._get_client")
    async def test_markdown_output(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        tbl = MagicMock()
        tbl.table_id = "users"
        tbl.table_type = "TABLE"
        tbl.project = "proj"
        tbl.dataset_id = "ds"
        mock_client.list_tables.return_value = [tbl]

        params = ListTablesInput(dataset_id="ds")
        result = await bq_list_tables(params)
        assert "users" in result
        assert "TABLE" in result

    @patch("bigquery_mcp._get_client")
    async def test_json_output(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        tbl = MagicMock()
        tbl.table_id = "orders"
        tbl.table_type = "VIEW"
        tbl.project = "proj"
        tbl.dataset_id = "ds"
        mock_client.list_tables.return_value = [tbl]

        params = ListTablesInput(dataset_id="ds", response_format="json")
        result = await bq_list_tables(params)
        parsed = json.loads(result)
        assert parsed["tables"][0]["type"] == "VIEW"

    @patch("bigquery_mcp._get_client")
    async def test_empty_tables(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client
        mock_client.list_tables.return_value = []

        params = ListTablesInput(dataset_id="ds")
        result = await bq_list_tables(params)
        assert "No tables found" in result


@pytest.mark.asyncio
class TestBqGetTableSchema:
    @patch("bigquery_mcp._get_client")
    async def test_markdown_output(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        field = MagicMock()
        field.name = "user_id"
        field.field_type = "INTEGER"
        field.mode = "REQUIRED"
        field.description = "Primary key"
        field.fields = []

        mock_table = MagicMock()
        mock_table.schema = [field]
        mock_client.get_table.return_value = mock_table

        params = GetTableSchemaInput(dataset_id="ds", table_id="tbl")
        result = await bq_get_table_schema(params)
        assert "user_id" in result
        assert "INTEGER" in result
        assert "REQUIRED" in result

    @patch("bigquery_mcp._get_client")
    async def test_json_output(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        field = MagicMock()
        field.name = "email"
        field.field_type = "STRING"
        field.mode = "NULLABLE"
        field.description = ""
        field.fields = []

        mock_table = MagicMock()
        mock_table.schema = [field]
        mock_client.get_table.return_value = mock_table

        params = GetTableSchemaInput(dataset_id="ds", table_id="tbl", response_format="json")
        result = await bq_get_table_schema(params)
        parsed = json.loads(result)
        assert parsed["num_columns"] == 1
        assert parsed["schema"][0]["name"] == "email"

    @patch("bigquery_mcp._get_client")
    async def test_nested_fields(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        inner = MagicMock()
        inner.name = "street"
        inner.field_type = "STRING"
        inner.mode = "NULLABLE"
        inner.description = ""
        inner.fields = []

        outer = MagicMock()
        outer.name = "address"
        outer.field_type = "RECORD"
        outer.mode = "NULLABLE"
        outer.description = "Address record"
        outer.fields = [inner]

        mock_table = MagicMock()
        mock_table.schema = [outer]
        mock_client.get_table.return_value = mock_table

        params = GetTableSchemaInput(dataset_id="ds", table_id="tbl", response_format="json")
        result = await bq_get_table_schema(params)
        parsed = json.loads(result)
        assert parsed["schema"][0]["fields"][0]["name"] == "street"


@pytest.mark.asyncio
class TestBqPreviewTable:
    @patch("bigquery_mcp._get_client")
    async def test_markdown_output(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        mock_table = MagicMock()
        mock_table.num_rows = 100
        mock_table.schema = _mock_schema()
        mock_client.get_table.return_value = mock_table

        mock_rows = MagicMock()
        mock_rows.__iter__ = lambda self: iter([{"id": "1", "name": "Alice"}])
        mock_client.list_rows.return_value = mock_rows

        params = PreviewTableInput(dataset_id="ds", table_id="tbl")
        result = await bq_preview_table(params)
        assert "Preview" in result
        assert "Alice" in result

    @patch("bigquery_mcp._get_client")
    async def test_json_output(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        mock_table = MagicMock()
        mock_table.num_rows = 50
        mock_table.schema = _mock_schema()
        mock_client.get_table.return_value = mock_table

        mock_rows = MagicMock()
        mock_rows.__iter__ = lambda self: iter([{"id": "2", "name": "Bob"}])
        mock_client.list_rows.return_value = mock_rows

        params = PreviewTableInput(dataset_id="ds", table_id="tbl", response_format="json")
        result = await bq_preview_table(params)
        parsed = json.loads(result)
        assert parsed["total_rows"] == 50
        assert parsed["rows"][0]["name"] == "Bob"

    @patch("bigquery_mcp._get_client")
    async def test_serializes_dates_and_bytes(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        mock_table = MagicMock()
        mock_table.num_rows = 1
        mock_table.schema = [_make_schema_field("dt"), _make_schema_field("bin")]
        mock_client.get_table.return_value = mock_table

        dt = date(2025, 6, 15)
        mock_rows = MagicMock()
        mock_rows.__iter__ = lambda self: iter([{"dt": dt, "bin": b"\xde\xad"}])
        mock_client.list_rows.return_value = mock_rows

        params = PreviewTableInput(dataset_id="ds", table_id="tbl", response_format="json")
        result = await bq_preview_table(params)
        parsed = json.loads(result)
        assert parsed["rows"][0]["dt"] == "2025-06-15"
        assert parsed["rows"][0]["bin"] == "dead"


@pytest.mark.asyncio
class TestBqGetTableInfo:
    @patch("bigquery_mcp._get_client")
    async def test_markdown_output(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        mock_table = MagicMock()
        mock_table.project = "proj"
        mock_table.dataset_id = "ds"
        mock_table.table_id = "tbl"
        mock_table.table_type = "TABLE"
        mock_table.num_rows = 1000
        mock_table.num_bytes = 5 * 1024 * 1024
        mock_table.created = datetime(2025, 1, 1)
        mock_table.modified = datetime(2025, 6, 1)
        mock_table.description = "Test table"
        mock_table.time_partitioning = None
        mock_table.clustering_fields = None
        mock_table.schema = [MagicMock(), MagicMock()]
        mock_client.get_table.return_value = mock_table

        params = GetTableInfoInput(dataset_id="ds", table_id="tbl")
        result = await bq_get_table_info(params)
        assert "proj.ds.tbl" in result
        assert "1,000" in result
        assert "5.0 MB" in result
        assert "Test table" in result

    @patch("bigquery_mcp._get_client")
    async def test_json_output(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        mock_table = MagicMock()
        mock_table.project = "proj"
        mock_table.dataset_id = "ds"
        mock_table.table_id = "tbl"
        mock_table.table_type = "TABLE"
        mock_table.num_rows = 42
        mock_table.num_bytes = 1024
        mock_table.created = datetime(2025, 1, 1)
        mock_table.modified = datetime(2025, 2, 1)
        mock_table.description = ""
        mock_table.time_partitioning = None
        mock_table.clustering_fields = ["col_a"]
        mock_table.schema = [MagicMock()]
        mock_client.get_table.return_value = mock_table

        params = GetTableInfoInput(dataset_id="ds", table_id="tbl", response_format="json")
        result = await bq_get_table_info(params)
        parsed = json.loads(result)
        assert parsed["num_rows"] == 42
        assert parsed["clustering_fields"] == ["col_a"]

    @patch("bigquery_mcp._get_client")
    async def test_with_partitioning(self, mock_gc):
        mock_client = MagicMock()
        mock_gc.return_value = mock_client

        mock_table = MagicMock()
        mock_table.project = "proj"
        mock_table.dataset_id = "ds"
        mock_table.table_id = "tbl"
        mock_table.table_type = "TABLE"
        mock_table.num_rows = 0
        mock_table.num_bytes = 0
        mock_table.created = None
        mock_table.modified = None
        mock_table.description = ""
        mock_table.time_partitioning = MagicMock()
        mock_table.time_partitioning.__str__ = lambda self: "DAY(created_at)"
        mock_table.clustering_fields = None
        mock_table.schema = []
        mock_client.get_table.return_value = mock_table

        params = GetTableInfoInput(dataset_id="ds", table_id="tbl")
        result = await bq_get_table_info(params)
        assert "Partitioning" in result
        assert "DAY(created_at)" in result

    @patch("bigquery_mcp._get_client", side_effect=Exception("connection failed"))
    async def test_error_handling(self, mock_gc):
        params = GetTableInfoInput(dataset_id="ds", table_id="tbl")
        result = await bq_get_table_info(params)
        assert "Error" in result
        assert "connection failed" in result
