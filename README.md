# Bolthouse Agriculture SQL MCP — Railway

This project deploys a read-only Model Context Protocol server for the
`Bolthouse_Ag_AI` SQL Server database using the `sqlprd_ag_ai` login.

It is based on the existing MCP4 behavior, adapted for Railway:

- Streamable HTTP MCP endpoint at `/mcp`
- FastMCP 3 with tested, pinned top-level dependencies
- Bearer-token authentication
- Read-only inline and paged SQL tools
- Background CSV or CSV.GZ exports
- Agent-accessible export-status tool
- Temporary signed download URLs
- Railway dynamic `PORT` support
- Optional Railway Volume persistence
- No credentials stored in source code

## MCP tools

| Tool | Purpose |
|---|---|
| `execute_sql_query` | Runs a read-only query with capped inline results |
| `execute_sql_query_paged` | Returns a requested page; requires `ORDER BY` |
| `submit_sql_export` | Starts a background CSV/CSV.GZ export |
| `get_sql_export_status` | Polls an export job and returns its download URL |

The expected agent flow for a large download is:

1. Call `submit_sql_export`.
2. Save the returned `job_id`.
3. Call `get_sql_export_status` until `status` is `completed`.
4. Download the returned `result.download_url`.

## Before deployment

### 1. Confirm SQL Server connectivity

Railway must be able to reach the SQL Server host on TCP port 1433. An
internal-only IP such as `10.x.x.x` is not normally reachable from Railway
unless your organization provides a VPN, tunnel, or other private-network
path. If SQL Server is exposed through a public endpoint, restrict its
firewall as tightly as your network design permits.

The `sqlprd_ag_ai` login should have read access only to `Bolthouse_Ag_AI`.
SQL Server permissions are the primary security boundary; the application
also rejects non-read-only SQL.

### 2. Optional audit table

By default, query metadata is written to Railway logs without recording the
SQL text. To write full audit records into SQL Server:

1. Run `sql/create_audit_table.sql`.
2. Set `AUDIT_TABLE=dbo.MCP_Query_Audit_AG_Railway`.

## GitHub deployment

1. Create a new private GitHub repository.
2. Copy all files in this folder into the repository root.
3. Commit and push:

   ```bash
   git init
   git add .
   git commit -m "Add Bolthouse Agriculture Railway MCP"
   git branch -M main
   git remote add origin https://github.com/YOUR-ORG/YOUR-REPO.git
   git push -u origin main
   ```

Never commit `.env`, SQL passwords, or API tokens.

## Railway deployment

1. In Railway, choose **New Project → Deploy from GitHub repo**.
2. Select the private repository.
3. Railway detects the root `Dockerfile`.
4. Add these service variables:

   ```text
   API_TOKEN=<long-random-secret>
   DB_SERVER=<SQL Server hostname or reachable IP>
   DB_PORT=1433
   DB_NAME=Bolthouse_Ag_AI
   DB_USER=sqlprd_ag_ai
   DB_PASS=<SQL password>
   DB_DRIVER=ODBC Driver 18 for SQL Server
   DB_ENCRYPT=yes
   DB_TRUST_SERVER_CERTIFICATE=yes
   SERVICE_NAME=Bolthouse Agriculture AI Read Only MCP
   ```

5. Deploy the service.
6. Open **Settings → Networking → Generate Domain**.
7. Copy the generated HTTPS domain and add:

   ```text
   PUBLIC_BASE_URL=https://YOUR-SERVICE.up.railway.app
   ```

8. Redeploy after adding `PUBLIC_BASE_URL`.

Your MCP URL is:

```text
https://YOUR-SERVICE.up.railway.app/mcp
```

Use the value of `API_TOKEN` as the bearer token in the MCP registration.

## Persistent CSV downloads

Railway container storage is ephemeral. Exports can disappear during a
redeploy or restart unless a Railway Volume is attached.

Recommended setup:

1. Add a Railway Volume to this service.
2. Set its mount path to `/data`.
3. Do not set `EXPORT_DIR`; the application automatically detects
   `RAILWAY_VOLUME_MOUNT_PATH` and stores exports under `/data/exports`.
4. Keep the service at one replica because export job state is held in the
   running process.

Completed export files and the export registry persist on the volume. Active
background jobs do not survive a restart and must be submitted again.

## Verification

Health check:

```bash
curl https://YOUR-SERVICE.up.railway.app/health
```

Inline query:

```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"SELECT DB_NAME() AS database_name"}' \
  https://YOUR-SERVICE.up.railway.app/query
```

Submit an export:

```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"SELECT TOP (1000) * FROM dbo.YourTable","gzip_output":true}' \
  https://YOUR-SERVICE.up.railway.app/exports
```

Check the job:

```bash
curl \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  https://YOUR-SERVICE.up.railway.app/jobs/JOB_ID
```

## Operational notes

- Use exactly one Railway replica for this implementation.
- Signed download links expire after `SIGNED_URL_TTL_SECONDS`.
- A new signed URL is generated whenever job status is requested.
- `MAX_INLINE_ROWS` protects MCP responses from oversized JSON payloads.
- CSV.GZ is recommended for large exports.
- The `/health` route is public but reveals no credentials.
- Download routes use signed URLs and do not require the bearer header.
