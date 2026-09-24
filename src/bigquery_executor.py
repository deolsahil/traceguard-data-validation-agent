from __future__ import annotations

import difflib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.api_core.exceptions import NotFound
from google.cloud import bigquery


OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

# Every BigQuery call must be bounded. Without this a stalled metadata request blocks
# forever: TICKET_BUDGET_SECONDS is only checked between stages, never during a call, so
# nothing rescues it. Fine when someone is watching a terminal; on a scheduled run it is
# a job that never returns and never reports. Both the request that creates the job and
# the wait for its results need a bound — either can hang alone.
_BQ_TIMEOUT = float(os.getenv("BQ_TIMEOUT_SECONDS", "60"))

_FORBIDDEN = re.compile(
    r"\b(ALTER|CALL|CREATE|DELETE|DROP|EXECUTE|EXPORT|GRANT|INSERT|MERGE|REVOKE|TRUNCATE|UPDATE)\b",
    re.IGNORECASE,
)


def _safety_errors(query: str) -> list[str]:
    clean = re.sub(r"/\*.*?\*/", " ", query, flags=re.DOTALL)
    clean = re.sub(r"--[^\n]*", " ", clean)
    errors = []
    first = re.match(r"^\s*(\w+)", clean.strip())
    if not first or first.group(1).upper() not in {"SELECT", "WITH"}:
        errors.append("Only SELECT or WITH…SELECT queries are allowed.")
    for m in _FORBIDDEN.finditer(clean):
        errors.append(f"Forbidden SQL operation: {m.group(1).upper()}")
    return errors


_CLIENT: bigquery.Client | None = None


def _client() -> bigquery.Client:
    """
    One reused client. Constructing it re-resolves credentials each time (~0.5s),
    and a single ticket makes several metadata/dry-run calls.
    """
    global _CLIENT
    if _CLIENT is None:
        billing_project, _ = load_bigquery_settings()
        _CLIENT = bigquery.Client(project=billing_project)
    return _CLIENT


def load_bigquery_settings() -> tuple[str, str]:
    load_dotenv(dotenv_path=".env")
    billing_project = os.getenv("BQ_BILLING_PROJECT", "").strip()
    location = os.getenv("BQ_LOCATION", "").strip()
    if not billing_project:
        raise ValueError("BQ_BILLING_PROJECT is missing from .env")
    if not location:
        raise ValueError("BQ_LOCATION is missing from .env")
    return billing_project, location


def format_bytes(n: int | None) -> str:
    if n is None:
        return "Unknown"
    v = float(n)
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if v < 1024:
            return f"{v:.2f} {unit}"
        v /= 1024
    return f"{v:.2f} TiB"


def _split_queries(sql: str) -> list[str]:
    """
    Split on statement separators only — semicolons inside strings or comments
    are not separators.

    A naive split() tore queries apart at things like STRING_AGG(x, '; '), which
    the dry run never caught because it validates the whole query while execution
    ran the pieces.
    """
    parts, buf = [], []
    quote = None          # "'" or '"' when inside a string literal
    line_comment = False
    block_comment = False
    i = 0
    while i < len(sql):
        ch, nxt = sql[i], sql[i + 1:i + 2]
        if line_comment:
            if ch == "\n":
                line_comment = False
            buf.append(ch)
        elif block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                buf.append("*/"); i += 2; continue
            buf.append(ch)
        elif quote:
            buf.append(ch)
            if ch == "\\":                      # escaped char inside the literal
                if nxt:
                    buf.append(nxt); i += 2; continue
            elif ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch; buf.append(ch)
        elif ch == "-" and nxt == "-":
            line_comment = True; buf.append("--"); i += 2; continue
        elif ch == "/" and nxt == "*":
            block_comment = True; buf.append("/*"); i += 2; continue
        elif ch == ";":
            parts.append("".join(buf)); buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))

    result = []
    for part in parts:
        has_sql = any(
            line.strip() and not line.strip().startswith("--")
            for line in part.splitlines()
        )
        if has_sql:
            result.append(part.strip())
    return result


def _run_single_statement(
    client: bigquery.Client,
    query: str,
    location: str,
    max_bytes_billed: int,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    job_config = bigquery.QueryJobConfig(
        use_query_cache=False,
        maximum_bytes_billed=max_bytes_billed,
    )

    try:
        job = client.query(query, job_config=job_config, location=location,
                           timeout=timeout_seconds)
        rows = job.result(timeout=timeout_seconds)
        schema = {f.name: f.field_type.upper() for f in (rows.schema or [])}
        has_contract_cols = (
            "validation_passed" in schema and "validation_details" in schema
        )

        if has_contract_cols:
            errors = []
            if schema.get("validation_passed") not in {"BOOL", "BOOLEAN"}:
                errors.append("validation_passed must be BOOL.")
            if schema.get("validation_details") != "STRING":
                errors.append("validation_details must be STRING.")
            if errors:
                return {"status": "INVALID_RESULT_SCHEMA", "job_id": job.job_id, "errors": errors}

            row = next(iter(rows), None)
            if row is None:
                return {"status": "NO_RESULT_ROWS", "job_id": job.job_id, "errors": ["Query returned no rows."]}

            passed = row["validation_passed"]
            if not isinstance(passed, bool):
                return {"status": "INVALID_RESULT_VALUE", "job_id": job.job_id, "errors": ["validation_passed must return a boolean."]}

            return {
                "status": "VALIDATION_PASSED" if passed else "VALIDATION_FAILED",
                "job_id": job.job_id,
                "validation_passed": passed,
                "validation_details": row["validation_details"],
                "total_bytes_processed": int(job.total_bytes_processed or 0),
                "total_bytes_processed_display": format_bytes(int(job.total_bytes_processed or 0)),
                "errors": [],
            }

        else:
            first_row = next(iter(rows), None)
            has_rows = first_row is not None
            col_names = list(schema.keys())
            details = f"Returned data ({', '.join(col_names)})" if has_rows else "No rows returned"
            return {
                "status": "VALIDATION_PASSED" if has_rows else "VALIDATION_FAILED",
                "job_id": job.job_id,
                "validation_passed": has_rows,
                "validation_details": details,
                "total_bytes_processed": int(job.total_bytes_processed or 0),
                "total_bytes_processed_display": format_bytes(int(job.total_bytes_processed or 0)),
                "errors": [],
            }

    except Exception as error:
        return {"status": "EXECUTION_FAILED", "errors": [str(error)]}


def execute_query(
    client: bigquery.Client,
    query: str,
    location: str,
    max_bytes_billed: int,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    safety_errors = _safety_errors(query)
    if safety_errors:
        return {"status": "BLOCKED_UNSAFE_SQL", "errors": safety_errors}

    statements = _split_queries(query)
    if not statements:
        return {"status": "EXECUTION_FAILED", "errors": ["No SQL statements found."]}

    if len(statements) == 1:
        return _run_single_statement(client, statements[0], location, max_bytes_billed, timeout_seconds)

    all_passed = True
    details_parts: list[str] = []
    first_job_id = None
    total_bytes = 0

    for i, stmt in enumerate(statements, 1):
        r = _run_single_statement(client, stmt, location, max_bytes_billed, timeout_seconds)
        if i == 1:
            first_job_id = r.get("job_id")
        total_bytes += r.get("total_bytes_processed", 0)
        passed = r.get("validation_passed", False)
        failed_status = r.get("status") in {
            "EXECUTION_FAILED", "BLOCKED_UNSAFE_SQL", "INVALID_RESULT_SCHEMA",
            "NO_RESULT_ROWS", "VALIDATION_FAILED",
        }
        if not passed or failed_status:
            all_passed = False
        detail = r.get("validation_details") or ("; ".join(r.get("errors", [])) or r.get("status", ""))
        details_parts.append(f"Query {i}: {detail}")

    return {
        "status": "VALIDATION_PASSED" if all_passed else "VALIDATION_FAILED",
        "job_id": first_job_id,
        "validation_passed": all_passed,
        "validation_details": " | ".join(details_parts),
        "total_bytes_processed": total_bytes,
        "total_bytes_processed_display": format_bytes(total_bytes),
        "errors": [],
    }


def dry_run_query(query: str) -> tuple[bool, str, int]:
    """
    Validate a query against BigQuery without running it — zero bytes, no cost.

    Catches syntax errors, unknown columns, type mismatches and bad table refs
    before a human is ever asked to approve the query.

    Returns (ok, error_message, estimated_bytes).
    """
    try:
        _, location = load_bigquery_settings()
        job = _client().query(
            query,
            job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
            location=location,
            timeout=_BQ_TIMEOUT,
        )
        return True, "", job.total_bytes_processed or 0
    except Exception as error:
        return False, str(error), 0


_DID_YOU_MEAN = re.compile(r"Did you mean ([A-Za-z_][A-Za-z0-9_]*)\?", re.IGNORECASE)


def suggest_column(full_table_name: str, column: str) -> str | None:
    """
    Ask BigQuery what it thinks a mistyped column should be — zero bytes, no execution.

    Dry-running a reference to the column makes BigQuery emit its own
    "Did you mean ...?" against the actual schema, which beats guessing at string
    similarity. Returns None when BigQuery offers no suggestion, which is itself
    useful: the name isn't close to anything real.
    """
    ok, error, _ = dry_run_query(f"SELECT `{column}` FROM `{full_table_name}` LIMIT 0")
    if ok:
        return None  # column resolves fine, nothing to suggest
    match = _DID_YOU_MEAN.search(error)
    return match.group(1) if match else None


def suggest_object(full_name: str) -> str | None:
    """
    Closest real table or view name in the same dataset — zero bytes, no execution.

    BigQuery's own "Did you mean ...?" only covers columns, so this reads the dataset's
    object list from INFORMATION_SCHEMA and compares names. Returns None when nothing is
    close, which is the useful answer: the name is not a typo of anything that exists.

    Only ever a suggestion. The caller still reports the object the ticket asked for as
    missing — swapping in the near match would answer a question nobody asked.
    """
    parts = full_name.replace("`", "").split(".")
    if len(parts) < 3:
        return None
    project, dataset, name = parts[-3], parts[-2], parts[-1]
    try:
        rows = _client().query(
            f"SELECT table_name FROM `{project}.{dataset}.INFORMATION_SCHEMA.TABLES`",
            job_config=bigquery.QueryJobConfig(use_query_cache=True),
        ).result()
        names = [r["table_name"] for r in rows]
    except Exception:
        return None  # can't read the dataset — no suggestion is better than a wrong one
    close = difflib.get_close_matches(name, names, n=1, cutoff=0.7)
    if close and close[0] != name:
        return f"{project}.{dataset}.{close[0]}"
    return None


def latest_partition_date(full_table_name: str, partition_field: str) -> str | None:
    """
    The newest partition that actually contains rows, as 'YYYY-MM-DD'.

    A fixed "last 7 days" filter matches nothing on a table that stopped loading
    months ago — the query scans 0 bytes, finds 0 rows and reports the column
    empty, when really nobody looked where the data is. Reading PARTITIONS costs
    nothing and tells us where to look.
    """
    try:
        parts = full_table_name.replace("`", "").split(".")
        if len(parts) < 3:
            return None
        project, dataset, table = parts[-3], parts[-2], parts[-1]
        _, location = load_bigquery_settings()
        query = f"""
            SELECT MAX(partition_id) AS latest
            FROM `{project}.{dataset}.INFORMATION_SCHEMA.PARTITIONS`
            WHERE table_name = @table
              AND total_rows > 0
              AND partition_id NOT IN ('__NULL__', '__UNPARTITIONED__')
        """
        config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("table", "STRING", table)]
        )
        rows = list(_client().query(query, job_config=config, location=location,
                                    timeout=_BQ_TIMEOUT).result(timeout=_BQ_TIMEOUT))
        latest = rows[0]["latest"] if rows else None
        if not latest or not re.fullmatch(r"\d{8}", latest):
            return None  # hourly/monthly granularity or integer range — leave it alone
        return f"{latest[:4]}-{latest[4:6]}-{latest[6:]}"
    except Exception:
        return None  # not fatal — fall back to the default window


def get_table_metadata(full_table_name: str) -> dict[str, Any]:
    """
    Fetch a table's real schema and partitioning in one metadata call — zero bytes
    scanned. The column list is what stops the LLM inventing column names.

    Returns {'exists', 'columns', 'partition', 'error'}.

    'error' separates "looked, and it isn't there" from "couldn't look". Without
    that distinction an expired credential reads as "table has no partition", and
    the agent then writes a full-table-scan query against a partitioned table.
    """
    try:
        table = _client().get_table(full_table_name, timeout=_BQ_TIMEOUT)
    except NotFound:
        # Genuinely absent — a legitimate thing for a ticket to be validating.
        return {"exists": False, "columns": [], "partition": None, "error": None}
    except Exception as error:
        # Auth, network, permissions: we know nothing about this table. Report that
        # rather than an absence we never confirmed.
        return {"exists": False, "columns": [], "partition": None, "error": str(error)}

    try:
        return {
            "exists": True,
            "error": None,
            "columns": [
                {"name": f.name, "type": f.field_type.upper()} for f in (table.schema or [])
            ],
            "partition": _partition_of(table),
        }
    except Exception as error:
        # Reading partitioning off a fetched table shouldn't fail, but if it ever
        # does, "no partition" would mean a full scan of a partitioned table.
        # Report the failure instead so the caller stops.
        return {"exists": False, "columns": [], "partition": None,
                "error": f"could not read table metadata: {error}"}


def _partition_of(table: Any) -> dict[str, Any] | None:
    """
    Partition details for a fetched table, or None if it genuinely isn't partitioned.

    Deliberately does not swallow errors: a silent None here reads as "unpartitioned"
    and produces a full table scan. Callers turn a raised error into a hard stop.
    """
    if table.time_partitioning:
        field_name = table.time_partitioning.field or "_PARTITIONTIME"
        # Look up actual column data type from schema so the query generator
        # can pick the right date function (DATE vs TIMESTAMP vs DATETIME)
        col_type = "TIMESTAMP"  # default for _PARTITIONTIME pseudo-column
        for schema_field in (table.schema or []):
            if schema_field.name == field_name:
                col_type = schema_field.field_type.upper()
                break
        return {
            "type": "time",
            "field": field_name,
            "granularity": table.time_partitioning.type_,  # DAY, HOUR, MONTH, YEAR
            "col_type": col_type,                          # DATE, TIMESTAMP, DATETIME
        }
    if table.range_partitioning:
        return {
            "type": "range",
            "field": table.range_partitioning.field,
            "granularity": None,
            "col_type": "INT64",
        }
    return None


# Statements that write to a table — these identify an SP's output tables.
_WRITE_TARGET_RE = re.compile(
    r"\b(?:INSERT\s+INTO\s+"
    r"|MERGE\s+(?:INTO\s+)?"
    r"|CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"|TRUNCATE\s+TABLE\s+"
    r"|UPDATE\s+)"
    r"`?([A-Za-z0-9_.\-<>]+)`?",
    re.IGNORECASE,
)


def get_routine_definition(sp_name: str, dataset: str | None = None,
                           project: str | None = None) -> dict[str, Any] | None:
    """
    Fetch a stored procedure's body from INFORMATION_SCHEMA.ROUTINES (zero bytes
    scanned) and extract the tables it writes to.

    Returns {'full_name', 'body', 'write_targets': [...], 'has_placeholders': bool}
    or None if the routine can't be found.
    """
    try:
        billing_project, location = load_bigquery_settings()

        # Accept either a bare name or a full project.dataset.sp path
        parts = sp_name.replace("`", "").split(".")
        if len(parts) == 3:
            project, dataset, routine = parts
        elif len(parts) == 2:
            dataset, routine = parts
        else:
            routine = parts[0]
        if not dataset:
            return None
        project = project or billing_project

        client = _client()
        query = f"""
            SELECT routine_definition
            FROM `{project}.{dataset}.INFORMATION_SCHEMA.ROUTINES`
            WHERE routine_name = @routine
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("routine", "STRING", routine)]
        )
        rows = list(client.query(query, job_config=job_config, location=location,
                                 timeout=_BQ_TIMEOUT).result(timeout=_BQ_TIMEOUT))
        if not rows or not rows[0][0]:
            return None

        body = rows[0][0]
        targets: list[str] = []
        for match in _WRITE_TARGET_RE.finditer(body):
            target = match.group(1)
            # Skip CTE/alias references that aren't real table paths
            if "." not in target:
                continue
            if target not in targets:
                targets.append(target)

        return {
            "full_name": f"{project}.{dataset}.{routine}",
            "body": body,
            "write_targets": targets,
            "has_placeholders": bool(re.search(r"<<\s*\w+\s*>>", body)),
        }
    except Exception as error:
        # Distinguish "no such routine" from "couldn't reach BigQuery": the first is
        # a ticket problem, the second is an environment problem, and telling someone
        # to add table_name: when their credentials expired sends them the wrong way.
        print(f"  [sp] could not read routine {sp_name}: {str(error).splitlines()[0][:160]}")
        return None


def run_query(issue_key: str, query: str) -> dict[str, Any]:
    """Execute a pre-approved query for a ticket and return the result."""
    billing_project, location = load_bigquery_settings()
    max_bytes = int(os.getenv("BQ_MAX_BYTES_DEFAULT", str(10 * 1024 ** 3)))

    result: dict[str, Any] = {
        "stage": "BIGQUERY_EXECUTION",
        "mode": "EXECUTE",
        "jira_key": issue_key.upper(),
        "billing_project": billing_project,
        "location": location,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "status": None,
        "errors": [],
    }

    client = _client()
    query_result = execute_query(
        client=client,
        query=query,
        location=location,
        max_bytes_billed=max_bytes,
    )
    result.update(query_result)
    return result


def save_result(issue_key: str, result: dict[str, Any]) -> Path:
    path = OUTPUT_DIR / f"{issue_key.upper()}-execution-result.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
