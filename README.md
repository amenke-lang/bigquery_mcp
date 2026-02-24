# BigQuery MCP Server

A custom MCP server for Google BigQuery with service account authentication.

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Set environment variables:**
   ```bash
   export BQ_SERVICE_ACCOUNT_KEY="/path/to/your-service-account-key.json"
   export BQ_PROJECT_ID="your-default-project-id"
   ```

3. **Run the server:**
   ```bash
   python bigquery_mcp.py
   ```

## Configuration for Claude Desktop / Cowork

Add this to your MCP settings (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "bigquery": {
      "command": "python",
      "args": ["/path/to/bigquery_mcp.py"],
      "env": {
        "BQ_SERVICE_ACCOUNT_KEY": "/path/to/your-service-account-key.json",
        "BQ_PROJECT_ID": "looplink-drt"
      }
    }
  }
}
```

## Available Tools

| Tool | Description |
|------|-------------|
| `bq_execute_sql` | Run a read-only SQL query |
| `bq_list_datasets` | List all datasets in a project |
| `bq_list_tables` | List tables in a dataset |
| `bq_get_table_schema` | Get column definitions for a table |
| `bq_preview_table` | Preview first N rows (free, no query cost) |
| `bq_get_table_info` | Get table metadata (rows, size, partitioning) |

## Notes

- Only SELECT queries are allowed (safety enforced)
- 10 GB billing cap per query
- Supports both `looplink-drt` and `dandelion-tf-production` projects via `project_id` parameter
