import base64
import csv
import datetime as dt
import decimal
import gzip
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import pyodbc
import uvicorn
from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("bolthouse-ag-mcp")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


API_TOKEN = required_env("API_TOKEN")
DB_SERVER = required_env("DB_SERVER")
DB_NAME = os.getenv("DB_NAME", "Bolthouse_Ag_AI").strip()
DB_USER = os.getenv("DB_USER", "sqlprd_ag_ai").strip()
DB_PASS = required_env("DB_PASS")
DB_PORT = int(os.getenv("DB_PORT", "1433"))
DB_DRIVER = os.getenv("DB_DRIVER", "ODBC Driver 18 for SQL Server").strip()
DB_ENCRYPT = os.getenv("DB_ENCRYPT", "yes").strip()
DB_TRUST_SERVER_CERTIFICATE = os.getenv(
    "DB_TRUST_SERVER_CERTIFICATE", "yes"
).strip()

SERVICE_NAME = os.getenv(
    "SERVICE_NAME", "Bolthouse Agriculture AI Read Only MCP"
).strip()
MCP_PATH = os.getenv("MCP_PATH", "/mcp").strip() or "/mcp"
if not MCP_PATH.startswith("/"):
    MCP_PATH = f"/{MCP_PATH}"

MAX_INLINE_ROWS = int(os.getenv("MAX_INLINE_ROWS", "5000"))
MAX_PAGE_SIZE = int(os.getenv("MAX_PAGE_SIZE", "5000"))
FETCH_BATCH_SIZE = int(os.getenv("FETCH_BATCH_SIZE", "5000"))
QUERY_TIMEOUT_SECONDS = int(os.getenv("QUERY_TIMEOUT_SECONDS", "180"))
EXPORT_TIMEOUT_SECONDS = int(os.getenv("EXPORT_TIMEOUT_SECONDS", "3600"))
MAX_QUERY_LENGTH = int(os.getenv("MAX_QUERY_LENGTH", "100000"))
MAX_CONCURRENT_EXPORTS = int(os.getenv("MAX_CONCURRENT_EXPORTS", "1"))
SIGNED_URL_TTL_SECONDS = int(os.getenv("SIGNED_URL_TTL_SECONDS", "3600"))

volume_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
default_export_dir = Path(volume_path) / "exports" if volume_path else Path("./data/exports")
EXPORT_DIR = Path(os.getenv("EXPORT_DIR", str(default_export_dir))).resolve()
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
REGISTRY_FILE = EXPORT_DIR / "export_registry.json"

configured_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
if configured_base_url:
    PUBLIC_BASE_URL = configured_base_url
elif railway_domain:
    PUBLIC_BASE_URL = f"https://{railway_domain}"
else:
    PUBLIC_BASE_URL = ""

CORS_ORIGINS = [
    item.strip()
    for item in os.getenv("CORS_ORIGINS", "").split(",")
    if item.strip()
]

AUDIT_TABLE = os.getenv("AUDIT_TABLE", "").strip()
if AUDIT_TABLE and not re.fullmatch(r"[A-Za-z_][\w]*\.[A-Za-z_][\w]*", AUDIT_TABLE):
    raise RuntimeError("AUDIT_TABLE must have the form schema.table")


CONNECTION_STRING = (
    f"Driver={{{DB_DRIVER}}};"
    f"Server={DB_SERVER},{DB_PORT};"
    f"Database={DB_NAME};"
    f"UID={DB_USER};"
    f"PWD={DB_PASS};"
    f"Encrypt={DB_ENCRYPT};"
    f"TrustServerCertificate={DB_TRUST_SERVER_CERTIFICATE};"
    "Application Name=Bolthouse Agriculture MCP;"
)


def get_connection(timeout_seconds: int) -> pyodbc.Connection:
    connection = pyodbc.connect(
        CONNECTION_STRING,
        timeout=max(1, min(timeout_seconds, 60)),
        autocommit=True,
    )
    # pyodbc exposes query timeout on the Connection object.
    # Cursor.timeout is not supported by pyodbc on Linux.
    connection.timeout = timeout_seconds
    return connection


FORBIDDEN_SQL = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|CREATE|"
    r"GRANT|REVOKE|DENY|EXEC|EXECUTE|BACKUP|RESTORE|DBCC|"
    r"USE|KILL|BULK|OPENROWSET|OPENDATASOURCE|OPENQUERY"
    r")\b",
    re.IGNORECASE,
)
SELECT_INTO = re.compile(r"\bSELECT\b[\s\S]*?\bINTO\b", re.IGNORECASE)
LEADING_COMMENTS = re.compile(
    r"^\s*(?:(?:--[^\r\n]*(?:\r?\n|$))|(?:/\*[\s\S]*?\*/))*\s*",
    re.IGNORECASE,
)


def validate_read_only_sql(sql: str) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("Query cannot be empty.")
    if len(sql) > MAX_QUERY_LENGTH:
        raise ValueError(f"Query exceeds MAX_QUERY_LENGTH={MAX_QUERY_LENGTH}.")
    if "\x00" in sql:
        raise ValueError("Query contains an invalid null character.")

    cleaned = LEADING_COMMENTS.sub("", sql).strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    if ";" in cleaned:
        raise ValueError("Multiple SQL statements are not allowed.")
    if not re.match(r"^(SELECT|WITH)\b", cleaned, re.IGNORECASE):
        raise ValueError("Only SELECT or WITH queries are allowed.")
    if FORBIDDEN_SQL.search(cleaned):
        raise ValueError("The query contains a forbidden SQL operation.")
    if SELECT_INTO.search(cleaned):
        raise ValueError("SELECT INTO is not allowed.")
    return cleaned


def json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def execute_inline(sql: str) -> dict[str, Any]:
    query = validate_read_only_sql(sql)
    started = time.monotonic()
    connection = None
    cursor = None
    try:
        connection = get_connection(QUERY_TIMEOUT_SECONDS)
        cursor = connection.cursor()
        cursor.execute(query)
        if not cursor.description:
            raise ValueError("The query did not return a result set.")

        columns = [item[0] for item in cursor.description]
        rows = cursor.fetchmany(MAX_INLINE_ROWS + 1)
        truncated = len(rows) > MAX_INLINE_ROWS
        rows = rows[:MAX_INLINE_ROWS]
        result_rows = [
            {columns[index]: json_value(value) for index, value in enumerate(row)}
            for row in rows
        ]
        result = {
            "status": "success",
            "database": DB_NAME,
            "columns": columns,
            "rows": result_rows,
            "row_count": len(result_rows),
            "truncated": truncated,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
        audit_query("inline", query, "SUCCESS", len(result_rows), None)
        return result
    except Exception as exc:
        audit_query("inline", query, "FAILED", 0, str(exc))
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()


def execute_paged(sql: str, offset: int, limit: int) -> dict[str, Any]:
    query = validate_read_only_sql(sql)
    if offset < 0:
        raise ValueError("offset must be zero or greater.")
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}.")
    if not re.search(r"\bORDER\s+BY\b", query, re.IGNORECASE):
        raise ValueError("Paged queries must include ORDER BY.")
    paged_query = f"{query}\nOFFSET ? ROWS FETCH NEXT ? ROWS ONLY"

    connection = None
    cursor = None
    started = time.monotonic()
    try:
        connection = get_connection(QUERY_TIMEOUT_SECONDS)
        cursor = connection.cursor()
        cursor.execute(paged_query, offset, limit)
        columns = [item[0] for item in cursor.description]
        rows = cursor.fetchall()
        result_rows = [
            {columns[index]: json_value(value) for index, value in enumerate(row)}
            for row in rows
        ]
        audit_query("paged", query, "SUCCESS", len(result_rows), None)
        return {
            "status": "success",
            "database": DB_NAME,
            "columns": columns,
            "rows": result_rows,
            "row_count": len(result_rows),
            "offset": offset,
            "limit": limit,
            "has_more": len(result_rows) == limit,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        audit_query("paged", query, "FAILED", 0, str(exc))
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()


registry_lock = threading.RLock()
jobs_lock = threading.Lock()
export_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_EXPORTS)
export_registry: dict[str, dict[str, Any]] = {}
jobs: dict[str, dict[str, Any]] = {}


def load_registry() -> None:
    global export_registry
    if not REGISTRY_FILE.exists():
        return
    try:
        with REGISTRY_FILE.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            export_registry = loaded
    except Exception:
        logger.exception("Could not load export registry")


def save_registry() -> None:
    temporary = REGISTRY_FILE.with_suffix(".tmp")
    with registry_lock:
        temporary.write_text(
            json.dumps(export_registry, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(REGISTRY_FILE)


load_registry()


def sign_download(export_id: str, expires_at: int) -> str:
    payload = f"{export_id}:{expires_at}".encode("utf-8")
    signature = hmac.new(API_TOKEN.encode("utf-8"), payload, hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(payload + signature)
    return encoded.decode("ascii").rstrip("=")


def verify_download(export_id: str, token: str) -> bool:
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        if len(decoded) <= hashlib.sha256().digest_size:
            return False
        payload = decoded[: -hashlib.sha256().digest_size]
        supplied_signature = decoded[-hashlib.sha256().digest_size :]
        token_export_id, expires_text = payload.decode("utf-8").split(":", 1)
        if token_export_id != export_id or int(expires_text) < int(time.time()):
            return False
        expected = hmac.new(
            API_TOKEN.encode("utf-8"), payload, hashlib.sha256
        ).digest()
        return hmac.compare_digest(supplied_signature, expected)
    except Exception:
        return False


def make_download_url(export_id: str) -> str:
    expires_at = int(time.time()) + SIGNED_URL_TTL_SECONDS
    token = sign_download(export_id, expires_at)
    path = f"/exports/{export_id}?token={quote(token)}"
    return f"{PUBLIC_BASE_URL}{path}" if PUBLIC_BASE_URL else path


def export_query(sql: str, gzip_output: bool) -> dict[str, Any]:
    query = validate_read_only_sql(sql)
    export_id = uuid.uuid4().hex
    suffix = ".csv.gz" if gzip_output else ".csv"
    filename = f"bolthouse_ag_{export_id}{suffix}"
    output_path = EXPORT_DIR / filename
    connection = None
    cursor = None
    row_count = 0
    started = time.monotonic()

    try:
        connection = get_connection(EXPORT_TIMEOUT_SECONDS)
        cursor = connection.cursor()
        cursor.arraysize = FETCH_BATCH_SIZE
        cursor.execute(query)
        if not cursor.description:
            raise ValueError("The query did not return a result set.")

        columns = [item[0] for item in cursor.description]
        opener = gzip.open if gzip_output else open
        with opener(output_path, "wt", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            while True:
                batch = cursor.fetchmany(FETCH_BATCH_SIZE)
                if not batch:
                    break
                writer.writerows(
                    [[json_value(value) for value in row] for row in batch]
                )
                row_count += len(batch)

        metadata = {
            "export_id": export_id,
            "filename": filename,
            "path": str(output_path),
            "gzip": gzip_output,
            "row_count": row_count,
            "size_bytes": output_path.stat().st_size,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        with registry_lock:
            export_registry[export_id] = metadata
        save_registry()
        audit_query("export", query, "SUCCESS", row_count, None)
        public_metadata = {
            key: value for key, value in metadata.items() if key != "path"
        }
        return {
            **public_metadata,
            "status": "completed",
            "download_url": make_download_url(export_id),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        audit_query("export", query, "FAILED", row_count, str(exc))
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()


def submit_export(sql: str, gzip_output: bool) -> str:
    query = validate_read_only_sql(sql)
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    def run() -> None:
        with jobs_lock:
            jobs[job_id]["status"] = "running"
            jobs[job_id]["started_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        try:
            result = export_query(query, gzip_output)
            with jobs_lock:
                jobs[job_id].update(
                    {
                        "status": "completed",
                        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "result": result,
                    }
                )
        except Exception as exc:
            logger.exception("Export job %s failed", job_id)
            with jobs_lock:
                jobs[job_id].update(
                    {
                        "status": "failed",
                        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "error": str(exc),
                    }
                )

    export_executor.submit(run)
    return job_id


def job_status(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return {
                "status": "not_found",
                "job_id": job_id,
                "message": "Job not found. Railway restarts clear in-memory job state.",
            }
        result = json.loads(json.dumps(job))
    if result.get("status") == "completed":
        export_id = result["result"]["export_id"]
        result["result"]["download_url"] = make_download_url(export_id)
    return result


def audit_query(
    operation: str,
    query: str,
    status: str,
    row_count: int,
    error_message: str | None,
) -> None:
    logger.info(
        "sql_audit operation=%s status=%s rows=%s query_sha256=%s",
        operation,
        status,
        row_count,
        hashlib.sha256(query.encode("utf-8")).hexdigest(),
    )
    if not AUDIT_TABLE:
        return
    connection = None
    cursor = None
    try:
        connection = get_connection(QUERY_TIMEOUT_SECONDS)
        cursor = connection.cursor()
        cursor.execute(
            f"""
            INSERT INTO {AUDIT_TABLE}
                (RequestID, Operation, QueryText, Status, RowsReturned, ErrorMessage, CreatedUTC)
            VALUES (?, ?, ?, ?, ?, ?, SYSUTCDATETIME())
            """,
            str(uuid.uuid4()),
            operation,
            query,
            status,
            row_count,
            error_message,
        )
    except Exception:
        logger.exception("Audit insert failed; request execution is unaffected")
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()


mcp = FastMCP(SERVICE_NAME)


@mcp.tool(
    description=(
        "Run a read-only SELECT/WITH query against Bolthouse_Ag_AI. "
        "Results are capped; use submit_sql_export for large datasets."
    ),
    tags={"sql", "database", "read-only", "agriculture"},
)
def execute_sql_query(
    query: Annotated[str, "A single read-only SQL SELECT or WITH query."],
) -> dict[str, Any]:
    try:
        return execute_inline(query)
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@mcp.tool(
    description=(
        "Run one page of a read-only query. The query must contain ORDER BY."
    ),
    tags={"sql", "database", "read-only", "paged", "agriculture"},
)
def execute_sql_query_paged(
    query: Annotated[str, "A SELECT/WITH query containing ORDER BY."],
    offset: Annotated[int, "Zero-based row offset."] = 0,
    limit: Annotated[int, "Rows to return, up to MAX_PAGE_SIZE."] = 1000,
) -> dict[str, Any]:
    try:
        return execute_paged(query, offset, limit)
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@mcp.tool(
    description=(
        "Start a background CSV export for a large read-only query. "
        "Returns a job_id; call get_sql_export_status until it completes."
    ),
    tags={"sql", "database", "read-only", "csv", "export", "agriculture"},
)
def submit_sql_export(
    query: Annotated[str, "A single read-only SQL SELECT or WITH query."],
    gzip_output: Annotated[
        bool, "Create a smaller .csv.gz file when true."
    ] = True,
) -> dict[str, Any]:
    try:
        job_id = submit_export(query, gzip_output)
        return {
            "status": "queued",
            "job_id": job_id,
            "next_step": "Call get_sql_export_status with this job_id.",
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@mcp.tool(
    description=(
        "Check a CSV export job. When completed, the response contains a "
        "temporary signed download_url that the agent or user can download."
    ),
    tags={"sql", "csv", "export", "status", "agriculture"},
)
def get_sql_export_status(
    job_id: Annotated[str, "The job_id returned by submit_sql_export."],
) -> dict[str, Any]:
    return job_status(job_id)


class BearerAuthMiddleware:
    """Pure ASGI authentication middleware that preserves streaming responses."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "").rstrip("/") or "/"
        method = scope.get("method", "GET").upper()
        if method == "OPTIONS" or path == "/health" or path.startswith("/exports/"):
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        authorization = headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, API_TOKEN):
            response = JSONResponse(
                {"status": "error", "message": "Invalid or missing bearer token."},
                status_code=401,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def health(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": SERVICE_NAME,
            "database": DB_NAME,
            "mcp_path": MCP_PATH,
            "exports": "enabled",
            "persistent_volume": bool(volume_path),
        }
    )


async def rest_query(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        return JSONResponse(execute_inline(body.get("query", "")))
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)


async def rest_query_paged(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        result = execute_paged(
            body.get("query", ""),
            int(body.get("offset", 0)),
            int(body.get("limit", 1000)),
        )
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)


async def rest_submit_export(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        job_id = submit_export(
            body.get("query", ""),
            parse_bool(body.get("gzip_output"), default=True),
        )
        return JSONResponse(
            {
                "status": "queued",
                "job_id": job_id,
                "status_url": f"/jobs/{job_id}",
            },
            status_code=202,
        )
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)


async def rest_job_status(request: Request) -> JSONResponse:
    result = job_status(request.path_params["job_id"])
    status_code = 404 if result["status"] == "not_found" else 200
    return JSONResponse(result, status_code=status_code)


async def rest_job_download(request: Request) -> Response:
    result = job_status(request.path_params["job_id"])
    if result.get("status") != "completed":
        return JSONResponse(
            {"status": "error", "message": "Export is not completed."},
            status_code=409,
        )
    return RedirectResponse(result["result"]["download_url"])


async def download_export(request: Request) -> Response:
    export_id = request.path_params["export_id"]
    token = request.query_params.get("token", "")
    if not verify_download(export_id, token):
        return JSONResponse(
            {"status": "error", "message": "Invalid or expired download token."},
            status_code=401,
        )
    with registry_lock:
        metadata = export_registry.get(export_id)
    if not metadata:
        return JSONResponse(
            {"status": "error", "message": "Export not found."}, status_code=404
        )
    path = Path(metadata["path"]).resolve()
    if EXPORT_DIR not in path.parents or not path.is_file():
        return JSONResponse(
            {"status": "error", "message": "Export file is unavailable."},
            status_code=404,
        )
    return FileResponse(
        path,
        filename=metadata["filename"],
        media_type="application/gzip" if metadata["gzip"] else "text/csv",
    )


mcp_app = mcp.http_app(path=MCP_PATH, stateless_http=True)

middleware = []
if CORS_ORIGINS:
    middleware.append(
        Middleware(
            CORSMiddleware,
            allow_origins=CORS_ORIGINS,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "mcp-protocol-version",
                "mcp-session-id",
            ],
            expose_headers=["mcp-session-id", "Content-Disposition"],
        )
    )
middleware.append(Middleware(BearerAuthMiddleware))

routes = [
    Route("/health", health, methods=["GET"]),
    Route("/query", rest_query, methods=["POST"]),
    Route("/query/paged", rest_query_paged, methods=["POST"]),
    Route("/exports", rest_submit_export, methods=["POST"]),
    Route("/exports/{export_id}", download_export, methods=["GET"]),
    Route("/jobs/{job_id}", rest_job_status, methods=["GET"]),
    Route("/jobs/{job_id}/download", rest_job_download, methods=["GET"]),
    Mount("/", app=mcp_app),
]

app = Starlette(
    routes=routes,
    middleware=middleware,
    lifespan=mcp_app.lifespan,
)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        workers=1,
        timeout_keep_alive=120,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
