from __future__ import annotations

import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_MODEL = os.getenv("LLM_MODEL", "google/gemini-3.7-flash")

# Same client for every provider — see the note in intent_parser.py. Derived from
# the project so no environment-specific value is hardcoded here.
_BASE_URL = os.getenv("LLM_BASE_URL") or (
    f"https://aiplatform.googleapis.com/v1/projects/{os.getenv('GOOGLE_CLOUD_PROJECT', '')}"
    f"/locations/{os.getenv('VERTEX_LOCATION', 'global')}/endpoints/openapi"
)

# No timeout means a hung API call blocks the whole scan indefinitely — one ticket
# once sat for 17 minutes. Fail fast and retry once; a stuck call is far more likely
# to be a bad connection than a slow answer.
_LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
_LLM_RETRIES = 1

_creds = None


# Gemini draws its thinking tokens from the same budget as the answer, so a wide table
# and a multi-part question can exhaust the cap mid-statement.
_MAX_OUTPUT = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "8192"))


class TruncatedResponse(Exception):
    """The model ran out of output budget mid-answer."""


def _content(response: Any) -> str:
    """
    The reply text, or an error if the model never finished writing it.

    A truncated completion is not a wrong answer, it is half an answer — and passed
    through unchecked it becomes SQL with an unclosed string, which BigQuery reports as
    a syntax error. That sends the reader looking for a mistake in a query that was
    simply cut off, so it is worth naming precisely at the point it happens.
    """
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise TruncatedResponse(
            f"the model hit its {_MAX_OUTPUT}-token output limit mid-answer — "
            f"the question likely needs more room, or a narrower check"
        )
    return (choice.message.content or "").strip()


def _api_key() -> str:
    """
    Vertex authenticates with a short-lived ADC token, other providers with a static
    LLM_API_KEY. Kept next to the calls rather than in a shared module so a failing
    request can be read top to bottom in one file. Same helper lives in
    intent_parser.py.
    """
    if "aiplatform.googleapis.com" not in _BASE_URL:
        key = os.getenv("LLM_API_KEY", "").strip()
        if not key:
            raise ValueError("LLM_API_KEY is missing from .env")
        return key

    global _creds
    if _creds is None:
        import google.auth
        _creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not _creds.valid:
        import google.auth.transport.requests
        _creds.refresh(google.auth.transport.requests.Request())
    return _creds.token

_SYSTEM = """You are a BigQuery SQL expert generating data validation queries.

== OUTPUT CONTRACT (an interface — not negotiable) ==

Return ONE statement starting with SELECT or WITH, returning exactly two columns:
  - `validation_passed` (BOOL)    — TRUE only if what the ticket asked for is actually true
  - `validation_details` (STRING) — short human-readable summary of what was found

The query MUST always return exactly ONE row. Never filter the final row away with a
trailing WHERE — put the condition inside validation_passed instead.

NEVER write `TRUE AS validation_passed` or `FALSE AS validation_passed`. A hardcoded verdict
reports the same answer regardless of the data and makes the whole validation worthless.
validation_passed must always be an expression computed from the check.

Read-only: no INSERT, UPDATE, DELETE, CREATE, DROP, MERGE, TRUNCATE.
Backtick-quote full table paths: `project.dataset.table`. Use SAFE_DIVIDE, never /.

== PRINCIPLES ==

1. Check ONLY what the ticket asks. Don't add distinct counts or fill rates nobody requested.
2. Prefer INFORMATION_SCHEMA for structure questions (does a column/table exist, what type,
   how many columns) — it scans 0 bytes. Query the data table only for questions about data.
   COUNT(*) on a data table counts ROWS; a COLUMN count comes from INFORMATION_SCHEMA.COLUMNS.
3. A missing column or table is a RESULT to report (validation_passed = false), not an error.
   Never reference a column you've been told doesn't exist, and never silently drop it or
   swap in a similar name.
4. Apply the partition filter only when scanning the data table. INFORMATION_SCHEMA has no
   partition column — filtering it there always fails.
5. If several time windows are mentioned, the most recent instruction wins. Never emit one
   branch per window.
6. Where a real schema is supplied below, it is the truth — trust it over the ticket text.

7. If anything the ticket asks for cannot be answered in SQL, it is listed under
   CANNOT BE ANSWERED below and has already been excluded from the requirements.
   Do not try to express it anyway. Never invent a placeholder column for it — no
   SAFE_CAST(NULL AS ...) AS when_added, no literal standing in for a measurement.
   Answer only what is actually checkable; the reader is told separately what was not.

8. Keep the query compact. Never restate the same aggregate more than once — compute it
   once and reference it. Do not write one CTE column per table column.

9. validation_details reports EXCEPTIONS, not an inventory. Say what failed, not the
   status of every item. For many columns, name the ones that are empty or missing and
   summarise the rest as a count:
     'empty_columns=none (16/16 populated)'
     'empty_columns=region,sku (14/16 populated)'
   Never emit one 'col: has_data | col: has_data | ...' entry per column.

10. Only conditions the ticket actually states may decide validation_passed. "Check the
    null count", "report the row count", "how many distinct values" ask for a NUMBER, not
    a threshold — compute it, put it in validation_details, and leave it out of
    validation_passed. Inventing a bar it must clear ("and null_count = 0") reports a
    failure the ticket never asked for, and buries the answer that was asked for.
    A condition counts as stated only if the ticket gives one: must exist, must be
    populated, at least N%, no older than N days. When a ticket both states a condition
    and asks for a number, the condition alone decides the verdict.

== PATTERNS ==

column exists + has data:
SELECT
  COUNTIF(MY_COLUMN IS NOT NULL) > 0 AS validation_passed,
  CONCAT('MY_COLUMN: non_null_rows=', CAST(COUNTIF(MY_COLUMN IS NOT NULL) AS STRING)) AS validation_details
FROM `project.dataset.table`

column exists + distinct count over a time window:
SELECT
  COUNT(DISTINCT MY_COLUMN) > 0 AS validation_passed,
  CONCAT('MY_COLUMN: distinct_last_N_days=', CAST(COUNT(DISTINCT MY_COLUMN) AS STRING)) AS validation_details
FROM `project.dataset.table`
WHERE partition_col >= DATE_SUB(CURRENT_DATE(), INTERVAL N DAY)

column existence only (0 bytes) — note COUNTIF, not a hardcoded literal:
SELECT
  COUNTIF(column_name = 'MY_COLUMN') > 0 AS validation_passed,
  CONCAT('MY_COLUMN: exists=', CAST(COUNTIF(column_name = 'MY_COLUMN') > 0 AS STRING)) AS validation_details
FROM `project.dataset.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'my_table'

does the table exist / how many COLUMNS (0 bytes):
SELECT
  COUNT(*) > 0 AS validation_passed,
  CONCAT('table_exists=', CAST(COUNT(*) > 0 AS STRING),
         ' column_count=', CAST(COUNT(*) AS STRING)) AS validation_details
FROM `project.dataset.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'my_table'

column count AND whether there is data — two different sources, so CTEs are correct here.
Note validation_passed combines both checks; it is never a literal:
WITH
  cols AS (
    SELECT COUNT(*) AS column_count
    FROM `project.dataset.INFORMATION_SCHEMA.COLUMNS`
    WHERE table_name = 'my_table'
  ),
  data AS (
    SELECT COUNT(*) AS row_count
    FROM `project.dataset.table`
    WHERE partition_col >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
  )
SELECT
  (SELECT column_count FROM cols) > 0 AND (SELECT row_count FROM data) > 0 AS validation_passed,
  CONCAT('column_count=', CAST((SELECT column_count FROM cols) AS STRING),
         ' row_count=', CAST((SELECT row_count FROM data) AS STRING)) AS validation_details

MANY columns — do they contain data? One pass, one aggregate per column, and the details
name only the empty ones:
SELECT
  COUNTIF(col_a IS NOT NULL) > 0 AND COUNTIF(col_b IS NOT NULL) > 0 AS validation_passed,
  CONCAT('empty_columns=', IFNULL(ARRAY_TO_STRING(ARRAY(
      SELECT c FROM UNNEST([
        IF(COUNTIF(col_a IS NOT NULL) = 0, 'col_a', NULL),
        IF(COUNTIF(col_b IS NOT NULL) = 0, 'col_b', NULL)
      ]) c WHERE c IS NOT NULL), ','), 'none')) AS validation_details
FROM `project.dataset.table`
WHERE partition_col >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)

does a table exist (0 bytes) — INFORMATION_SCHEMA.TABLES, not COLUMNS:
SELECT
  COUNTIF(table_name = 'my_table') > 0 AS validation_passed,
  CONCAT('my_table: ', IF(COUNTIF(table_name = 'my_table') > 0, 'exists', 'MISSING')) AS validation_details
FROM `project.dataset.INFORMATION_SCHEMA.TABLES`

MANY columns (3+) — existence. Always this shape, never one MAX(col) per column. 0 bytes,
scales to any count, and names what is missing:
SELECT
  COUNT(*) = 3 AS validation_passed,
  CONCAT('found=', CAST(COUNT(*) AS STRING), '/3 present: ',
         IFNULL(STRING_AGG(column_name, ',' ORDER BY column_name), 'none')) AS validation_details
FROM `project.dataset.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'my_table'
  AND column_name IN ('col_a','col_b','col_c')

MANY columns with data types (data model validation) — 0 bytes, checks name AND type:
SELECT
  COUNTIF(actual_type = expected_type) = COUNT(*) AS validation_passed,
  CONCAT('matched=', CAST(COUNTIF(actual_type = expected_type) AS STRING), '/', CAST(COUNT(*) AS STRING),
         ' mismatches=', IFNULL(STRING_AGG(IF(actual_type = expected_type, NULL,
           CONCAT(name, '(want ', expected_type, ' got ', IFNULL(actual_type, 'MISSING'), ')')), '; '), 'none')) AS validation_details
FROM UNNEST([STRUCT('col_a' AS name, 'STRING' AS expected_type),
             STRUCT('col_b' AS name, 'INT64' AS expected_type)]) e
LEFT JOIN (
  SELECT column_name, data_type FROM `project.dataset.INFORMATION_SCHEMA.COLUMNS` WHERE table_name = 'my_table'
) c ON c.column_name = e.name,
UNNEST([c.data_type]) AS actual_type

data type check for one column (0 bytes):
SELECT
  MAX(data_type) = 'EXPECTED_TYPE' AS validation_passed,
  CONCAT('col type=', IFNULL(MAX(data_type), 'MISSING'), ' expected=EXPECTED_TYPE') AS validation_details
FROM `project.dataset.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'my_table' AND column_name = 'col'

value threshold:
SELECT
  COUNTIF(col > 1000) > 0 AS validation_passed,
  CONCAT('rows_above_threshold=', CAST(COUNTIF(col > 1000) AS STRING), ' total=', CAST(COUNT(*) AS STRING)) AS validation_details
FROM `project.dataset.table`

row count:
SELECT
  COUNT(*) > 0 AS validation_passed,
  CONCAT('row_count=', CAST(COUNT(*) AS STRING)) AS validation_details
FROM `project.dataset.table`

multiple DIFFERENT tables — CTEs, one per table:
WITH
  a AS (SELECT COUNTIF(col IS NOT NULL) > 0 AS passed,
               CONCAT('table_a: non_null=', CAST(COUNTIF(col IS NOT NULL) AS STRING)) AS detail
        FROM `project.dataset.table_a`),
  b AS (SELECT COUNTIF(col IS NOT NULL) > 0 AS passed,
               CONCAT('table_b: non_null=', CAST(COUNTIF(col IS NOT NULL) AS STRING)) AS detail
        FROM `project.dataset.table_b`)
SELECT
  (SELECT passed FROM a) AND (SELECT passed FROM b) AS validation_passed,
  CONCAT((SELECT detail FROM a), ' | ', (SELECT detail FROM b)) AS validation_details

These patterns are starting points — adapt them to the ticket. If the ticket asks something
none of them covers, write the query that answers it while honouring the output contract.

Return ONLY the SQL — no explanation, no markdown fences, no backtick blocks."""


def generate_query(intent: dict[str, Any], partition_info: dict[str, Any] | None = None) -> str:
    api_key = _api_key()

    target = (
        intent.get("full_table_name")
        or intent.get("view_name")
        or intent.get("sp_name")
        or "unknown"
    )

    if partition_info:
        col_type = partition_info.get("col_type", "DATE")
        if col_type == "TIMESTAMP":
            date_expr = "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)"
        elif col_type == "DATETIME":
            date_expr = "DATETIME_SUB(CURRENT_DATETIME(), INTERVAL 7 DAY)"
        else:
            date_expr = "DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)"
        partition_note = (
            f"{partition_info['field']} ({col_type}, "
            f"{partition_info.get('granularity') or partition_info['type']}). "
            f"Filter on it when scanning the data table so the scan stays cheap — "
            f"e.g. {partition_info['field']} >= {date_expr}. Default to 7 days unless the "
            f"ticket asks for a different window. This column exists only on the data "
            f"table, not in INFORMATION_SCHEMA."
        )
    else:
        partition_note = "no partition — generate query without partition filter"

    column_name = intent.get("column_name") or "N/A"
    data_type = intent.get("data_type") or "N/A"
    ticket_text = intent.get("_ticket_text") or ""

    # SP output tables resolved from INFORMATION_SCHEMA.ROUTINES — authoritative,
    # so the model doesn't have to guess which of many tables the SP writes to.
    sp_targets = intent.get("_sp_write_targets") or []
    if sp_targets:
        sp_note = (
            f"\nRESOLVED SP OUTPUT TABLES (read from the procedure body — authoritative):\n"
            + "\n".join(f"  - {t}" for t in sp_targets)
            + f"\nValidate `{sp_targets[0]}` unless the ticket names a different one. "
            f"Do NOT query the procedure itself and do NOT guess other table names.\n"
        )
    else:
        sp_note = ""

    # Data model parsed from a spreadsheet attached to the ticket. This is the
    # authoritative column list — the ticket text usually just says "see attached".
    model_columns = intent.get("_data_model_columns") or []
    if model_columns:
        typed = [c for c in model_columns if c.get("data_type")]
        listing = "\n".join(
            f"  - {c['column_name']}" + (f" : {c['data_type']}" if c.get("data_type") else "")
            for c in model_columns
        )
        model_note = (
            f"\nDATA MODEL FROM ATTACHED FILE `{intent.get('_data_model_file')}` "
            f"({len(model_columns)} columns — authoritative, validate all of them):\n{listing}\n"
            + ("Check each column's data type as well as its presence.\n" if typed else "")
        )
    else:
        model_note = ""

    # The table's real schema, read from BigQuery. Without this the model guesses
    # column names from the ticket prose and invents ones that don't exist.
    actual = intent.get("_actual_columns") or []

    # State which requested columns don't exist as a fact. The model decides how to
    # express that in SQL — it just can't be expected to reliably diff two lists.
    missing_note = ""
    if actual:
        have = {c["name"].lower() for c in actual}
        requested = [c["column_name"] for c in (intent.get("_data_model_columns") or [])]
        if intent.get("column_name"):
            # One ticket can ask for several columns, and they arrive as one string.
            # Diffed whole against the schema it never matches, so the note below
            # reports every column as missing, including the ones that are there.
            requested.extend(c.strip() for c in str(intent["column_name"]).split(","))
        absent = sorted({c for c in requested if c and c.lower() not in have})
        if absent:
            suggestion = intent.get("_column_suggestion")
            hint = (
                f"The closest real column is '{suggestion}' — say so in validation_details "
                f"(e.g. \"...MISSING (did you mean {suggestion}?)\") so a typo is obvious, "
                f"but validate what was actually asked for, not the guess.\n"
                if suggestion else ""
            )
            missing_note = (
                f"\nFACT: the ticket refers to {len(absent)} column(s) that do not exist in "
                f"this table: {', '.join(absent)}\n{hint}"
                f"This validation therefore FAILS. Check only that these columns exist, via "
                f"INFORMATION_SCHEMA, and report which are missing. Any further check the "
                f"ticket asks for (has data, distinct count, value range) is impossible on a "
                f"column that isn't there — drop it rather than faking it with NULL, LIMIT 0 "
                f"or a placeholder branch, and do not scan the data table at all.\n"
                f"Emit ONLY the existence check — do not express the dropped condition in any "
                f"other form, and never use metadata columns like is_nullable as a stand-in "
                f"for whether rows contain data (is_nullable describes the schema, not the "
                f"data). Say what happened in validation_details so the reader understands "
                f"why the rest was skipped — e.g. "
                f"\"updated_ts: MISSING from table (data check skipped)\".\n"
            )

    if actual:
        schema_note = (
            f"\nACTUAL TABLE SCHEMA ({len(actual)} columns, read from BigQuery — these are "
            f"the only columns that exist):\n"
            + "\n".join(f"  - {c['name']} : {c['type']}" for c in actual)
            + "\n"
        )
    elif intent.get("_object_missing"):
        missing = intent["_object_missing"]
        name = missing.split(".")[-1]
        dataset_ref = ".".join(missing.split(".")[:-1]) or "the dataset"
        schema_note = (
            f"\nFACT: '{missing}' does not exist as a table, view or stored procedure — "
            f"it was looked up and is not there. There is no schema, so there is nothing "
            f"to check inside it.\n"
            f"This validation therefore FAILS. Check only that '{name}' EXISTS, by "
            f"querying `{dataset_ref}.INFORMATION_SCHEMA.ROUTINES` (and/or `.TABLES`) for "
            f"routine_name/table_name = '{name}' — validation_passed is that existence "
            f"check, so it comes out FALSE. Do NOT write a check that returns TRUE when "
            f"the object is absent: the ticket asked about something that isn't there, "
            f"which is a failed validation, not a successful absence check.\n"
            f"Do NOT reference any data table. In particular do not invent a destination "
            f"table from the name — a procedure called 'calculate_order_metrics' does not "
            f"imply a table called 'order_metrics'. Report the absence in "
            f"validation_details, e.g. \"{name}: MISSING from {dataset_ref}\".\n"
            + (
                f"A near-miss exists: '{intent['_object_suggestion']}'. Say so as a "
                f"question — \"{name}: MISSING from {dataset_ref} (did you mean "
                f"{intent['_object_suggestion'].split('.')[-1]}?)\" — and still validate "
                f"'{name}', not the suggestion. The reviewer decides whether it was a typo.\n"
                if intent.get("_object_suggestion") else ""
            )
        )
    elif intent.get("_table_missing"):
        near = intent.get("_object_suggestion")
        asked = (intent.get("full_table_name") or intent.get("view_name") or "").split(".")[-1]
        schema_note = (
            "\nThe target table could not be read (it may not exist yet). Validate via "
            "INFORMATION_SCHEMA only — do NOT reference any column of the table "
            "directly, since none are known to exist.\n"
            # Spelled out rather than shown as a template: given a placeholder to fill in,
            # the model kept the plain "MISSING" wording and dropped the suggestion.
            + (f"'{asked}' does not exist, but '{near.split('.')[-1]}' does and is nearly the "
               f"same name. Still check '{asked}' — never substitute the close one. Write "
               f"validation_details as exactly: "
               f"'{asked}: MISSING (did you mean {near.split('.')[-1]}?)'\n"
               if near and asked else "")
        )
    else:
        schema_note = ""

    # A reviewer looked at the previous attempt and said no. Whatever they objected
    # to is the single most important thing to get right this time.
    # A column name in the ticket was corrected against the real schema. Both the
    # requirements text and the ticket body still say the wrong one, so this has to
    # be unmissable or the model follows the stale name.
    corrected = intent.get("_column_corrected")
    corrected_note = (
        f"\nCORRECTION: the ticket says '{corrected[0]}', which does not exist in this "
        f"table. The real column is '{corrected[1]}'. Use '{corrected[1]}' everywhere — "
        f"in the SQL and in validation_details — and ignore '{corrected[0]}' wherever it "
        f"appears in the ticket text below.\n"
        if corrected else ""
    )

    # A time filter that reaches no rows proves nothing about the column, but the
    # obvious query reports zero non-null rows — which reads as "the column is empty".
    # Only sent when the table is known to be behind, so the usual call pays nothing.
    age = intent.get("_data_age_days")
    stale_note = (
        f"\nFACT: the newest row in this table is {age} days old. If the window you filter "
        f"on is shorter than that, the query scans zero rows, and zero rows is NOT evidence "
        f"about the column — nobody looked where the data is. Count the rows you actually "
        f"scanned and branch on it, so the two outcomes cannot be confused:\n"
        f"  IF(COUNT(*) = 0,\n"
        f"     'INCONCLUSIVE: nothing was scanned in this window, so this proves nothing',\n"
        f"     CONCAT(...the normal summary..., ' of ', CAST(COUNT(*) AS STRING), ' scanned'))\n"
        f"validation_passed stays a computed expression either way — an empty window is not "
        f"a pass.\n"
        if isinstance(age, int) and age > 7 else ""
    )

    # Several objects in one ticket. Their real state is already read, so the model
    # answers about all of them from fact rather than picking one and dropping the rest.
    targets = intent.get("_target_schemas") or {}
    targets_note = ""
    if targets:
        lines = []
        for name, meta in targets.items():
            if meta.get("error"):
                lines.append(f"  - {name} : COULD NOT BE READ ({meta['error'].splitlines()[0][:80]})")
            elif meta.get("exists"):
                cols = ", ".join(c["name"] for c in meta["columns"][:40]) or "no columns"
                lines.append(f"  - {name} : EXISTS ({len(meta['columns'])} columns: {cols})")
            elif meta.get("suggestion"):
                lines.append(f"  - {name} : DOES NOT EXIST — closest real object is "
                             f"{meta['suggestion']}, which may be what was meant")
            else:
                lines.append(f"  - {name} : DOES NOT EXIST")
        present = [n for n, m in targets.items() if m.get("exists")]
        targets_note = (
            f"\nTHE TICKET NAMES {len(targets)} OBJECTS. This is their real state, read from "
            f"BigQuery:\n" + "\n".join(lines) + "\n"
            f"Answer about every one of them in a single query — never drop one, and never "
            f"answer about only the first. Use the fully qualified names exactly as written "
            f"above. validation_passed is true only if every object satisfies what the ticket "
            f"asks; validation_details must name each object and its own outcome, e.g. "
            f"'A: exists, B: exists, C: MISSING'. Objects listed as DOES NOT EXIST are a "
            f"reportable result, not an error — do not query them directly, and do not leave "
            f"them out.\n"
            f"Where a closest real object is given, say so in validation_details as a "
            f"question, e.g. \"C: MISSING (did you mean C_REAL?)\". Never validate the "
            f"suggestion instead: the answer is still that the object asked about is not "
            f"there, and the reviewer decides whether it was a typo.\n"
            + (f"Existence questions are answerable from INFORMATION_SCHEMA alone and scan 0 "
               f"bytes; prefer that over touching {', '.join(present[:3])}.\n" if present else "")
            + "Shape for an existence check across several objects in one dataset — one row, "
              "one pass over INFORMATION_SCHEMA, every name accounted for:\n"
              "SELECT\n"
              "  COUNTIF(table_name = 'OBJ_A') > 0 AND COUNTIF(table_name = 'OBJ_B') > 0 AS validation_passed,\n"
              "  CONCAT('OBJ_A: ', IF(COUNTIF(table_name = 'OBJ_A') > 0, 'exists', 'MISSING'),\n"
              "         ', OBJ_B: ', IF(COUNTIF(table_name = 'OBJ_B') > 0, 'exists', 'MISSING')) AS validation_details\n"
              "FROM `project.dataset.INFORMATION_SCHEMA.TABLES`\n"
              "Objects in different datasets need one such scan per dataset, combined in CTEs.\n"
        )

    unanswerable = intent.get("unanswerable") or []
    unanswerable_note = (
        "\nCANNOT BE ANSWERED IN SQL (already excluded from the requirements — do not "
        "attempt these, and do not fabricate a column for them):\n"
        + "\n".join(f"  - {u}" for u in unanswerable) + "\n"
        if unanswerable else ""
    )

    feedback = intent.get("_rejection_feedback")
    feedback_note = (
        f"\nA REVIEWER REJECTED YOUR PREVIOUS QUERY for this ticket. Their reason:\n"
        f"  {feedback}\n"
        f"Address it directly — do not repeat the same mistake.\n"
        if feedback else ""
    )

    # A comment addressed to the agent (@agent ...) is the most recent, most
    # specific statement of what to check — it outranks the description.
    directive = intent.get("_agent_directive")
    directive_note = (
        f"\nDIRECT INSTRUCTION TO THE AGENT (highest priority — the query MUST "
        f"answer exactly this):\n{directive}\n"
        if directive else ""
    )

    validation_requirements = intent.get("validation_requirements") or intent.get("change_summary") or "Validate the data change landed correctly."

    user_message = f"""Generate a BigQuery validation query.

--- ORIGINAL TICKET (primary source of truth) ---
{ticket_text}
--- END TICKET ---
{corrected_note}{unanswerable_note}{feedback_note}{targets_note}{schema_note}{missing_note}{stale_note}{directive_note}{sp_note}{model_note}
Extracted hints (use to confirm, not override what the ticket says):
  change_type: {intent.get('change_type')}
  target table: {target or ('see the object list above' if targets else 'unknown')}
  column_name: {column_name}
  data_type: {data_type}
  dataset: {intent.get('dataset') or 'N/A'}
  project: {intent.get('project') or 'N/A'}
  validation_requirements: {validation_requirements}
  partition: {partition_note}

Write the query that answers this ticket. Where the schema above is given, it is the truth
about what exists — prefer it over anything the ticket text implies."""

    client = OpenAI(base_url=_BASE_URL, api_key=api_key,
                    timeout=_LLM_TIMEOUT, max_retries=_LLM_RETRIES)
    response = client.chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_message},
        ],
        temperature=0,
        top_p=0.7,
        max_tokens=_MAX_OUTPUT,
        stream=False,
    )
    query = _content(response)

    return _validate_with_bigquery(query, api_key, user_message)


def _contract_violations(query: str) -> list[str]:
    """
    Check the query actually decides something, rather than just running.

    A dry run proves the SQL is valid, not that it means anything: `TRUE AS
    validation_passed` is perfectly good SQL that reports success for every ticket
    forever. These are assertions about the output contract, not style rules.
    """
    problems: list[str] = []

    literal = re.search(
        r"\b(TRUE|FALSE)\s+AS\s+validation_passed\b", query, re.IGNORECASE
    )
    if literal:
        problems.append(
            f"`{literal.group(1).upper()} AS validation_passed` is a hardcoded result — "
            f"it reports the same verdict no matter what the data says. "
            f"validation_passed must be an expression computed from the check itself."
        )

    fake = re.search(r"(?:SAFE_)?CAST\s*\(\s*NULL\s+AS\s+\w+\s*\)\s+AS\s+(\w+)", query, re.IGNORECASE)
    if fake:
        problems.append(
            f"`{fake.group(0)}` invents a placeholder column that is always NULL. "
            f"If a value cannot be determined, leave it out entirely rather than "
            f"reporting a fabricated one."
        )

    if not re.search(r"\bAS\s+validation_passed\b", query, re.IGNORECASE):
        problems.append("Query must return a column aliased `validation_passed`.")
    if not re.search(r"\bAS\s+validation_details\b", query, re.IGNORECASE):
        problems.append("Query must return a column aliased `validation_details`.")

    # A trailing WHERE over the final SELECT can filter the only row away, leaving
    # the executor with no verdict at all.
    if re.search(r"\bWHERE\s+\w+\s*(LIMIT\s+\d+\s*)?$", query.strip(), re.IGNORECASE):
        problems.append(
            "The outer SELECT is filtered by a WHERE that can return zero rows. "
            "A validation query must always return exactly one row — put the "
            "condition inside validation_passed instead of filtering the row out."
        )

    return problems


# "400 POST https://bigquery.googleapis.com/.../jobs?prettyPrint=false: " in front of
# every message pushes the part a person needs off the first line — and BigQuery's own
# hint ("Did you mean app_version_txt?") is usually the fix.
_BQ_NOISE = re.compile(r"^\s*\d{3}\s+\w+\s+https?://\S+?:\s*", re.IGNORECASE)


def bq_message(error: str) -> str:
    """BigQuery's error with the HTTP envelope stripped, so it leads with the cause."""
    first = (error or "").strip().splitlines()[0] if (error or "").strip() else ""
    return _BQ_NOISE.sub("", first).strip()


class BrokenObject(Exception):
    """The table or view itself does not work — no query against it can succeed."""


# BigQuery names this precisely: a view whose definition references something that is
# no longer there fails to parse, and says so.
_BROKEN_OBJECT = re.compile(
    r"failed to parse view|invalid view definition|view .* references .* which does not exist",
    re.IGNORECASE,
)


def _broken_object(error: str) -> bool:
    """
    True when the object under test is itself unusable, rather than the SQL being wrong.

    Worth separating because the fix loop cannot help: every rewrite fails the same way,
    and the model — handed the parse error three times — eventually writes a query
    *about* the breakage instead of the check that was asked for. That reads as an
    answer while quietly abandoning the question.
    """
    return bool(_BROKEN_OBJECT.search(error or ""))


def _is_query_error(error: str) -> bool:
    """
    True if BigQuery rejected the SQL itself, rather than failing to run the check.

    Auth failures, missing credentials and network errors all surface through the
    same exception path as a bad query, but rewriting the SQL cannot fix them.
    """
    lowered = error.lower()
    infrastructure = (
        "could not automatically determine credentials",
        "default credentials",
        "invalid_grant",
        "connection", "timeout", "timed out",
        "permission", "access denied", "forbidden",
        "quota", "exceeded rate limits", "billing",
        "internal error", "backend error", "service unavailable",
    )
    if re.match(r"\s*(401|403|500|502|503)\b", error):
        return False
    # Default to treating it as a query problem. A 404 ("Not found: Dataset") is a
    # malformed reference the model can fix, so only bail on clear infrastructure
    # failures — being wrong here costs a retry, being wrong the other way means
    # posting broken SQL for a human to catch.
    return not any(marker in lowered for marker in infrastructure)


# Three attempts. Each retry is an LLM round-trip plus a dry run, but the calls are
# timeout-bounded now, and a query that still fails is refused rather than posted —
# so the extra attempt buys a real fix instead of a slower failure.
def _validate_with_bigquery(query: str, api_key: str, context: str, max_attempts: int = 3) -> str:
    """
    Dry-run the query against BigQuery and let the model fix whatever it reports.

    A dry run costs nothing and scans no data, but catches syntax errors, unknown
    columns, type mismatches and bad table references — so the agent corrects its
    own mistakes before a human is asked to approve anything. This replaces
    hand-written checks for specific failure shapes: any error BigQuery can name,
    the model gets a chance to fix.
    """
    from bigquery_executor import dry_run_query  # local import — avoids a cycle

    client = OpenAI(base_url=_BASE_URL, api_key=api_key,
                    timeout=_LLM_TIMEOUT, max_retries=_LLM_RETRIES)
    for attempt in range(1, max_attempts + 1):
        # Contract first: BigQuery happily accepts `TRUE AS validation_passed`, so a
        # clean dry run alone doesn't mean the query decides anything.
        violations = _contract_violations(query)
        if violations:
            error = "\n".join(f"- {v}" for v in violations)
        else:
            ok, bq_error, _ = dry_run_query(query)
            if ok:
                if attempt > 1:
                    print(f"  [check] query valid after {attempt - 1} correction(s)")
                return query
            if _broken_object(bq_error):
                # The object under test is broken, so no rewrite can succeed. Stop here
                # rather than let the fix loop turn the question into a description of
                # the breakage.
                raise BrokenObject(bq_message(bq_error))
            if not _is_query_error(bq_error):
                # Credentials, network, permissions — nothing the model can fix by
                # rewriting SQL. Don't burn correction attempts on it.
                print(f"  [check] skipped (cannot reach BigQuery): {bq_error.splitlines()[0][:120]}")
                return query
            error = bq_error

        if attempt == max_attempts:
            print(f"  [check] still failing after {max_attempts} attempts — posting anyway for human review")
            return query

        print(f"  [check] {error.splitlines()[0][:130]} — asking model to fix")
        response = client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": context},
                {"role": "assistant", "content": query},
                {"role": "user", "content": (
                    f"That query has a problem:\n\n{error}\n\n"
                    f"Fix it. Remember a missing column or table is a validation result to "
                    f"report (validation_passed = false), not something to work around by "
                    f"removing the check or hardcoding the verdict.\n\n"
                    f"Return ONLY the corrected SQL — no explanation, no markdown fences."
                )},
            ],
            temperature=0,
            top_p=0.7,
            max_tokens=_MAX_OUTPUT,
            stream=False,
        )
        query = _content(response)
    return query


def fix_query(query: str, error: str, columns: list[dict[str, str]] | None = None) -> str:
    """Ask LLM to fix a BigQuery SQL error. Returns corrected SQL."""
    api_key = _api_key()

    # The real schema — most SQL errors here are invented column names, and the fix
    # pass can't correct one without knowing what actually exists.
    schema_hint = ""
    if columns:
        schema_hint = (
            f"\n\nACTUAL TABLE SCHEMA ({len(columns)} columns — the ONLY columns that exist):\n"
            + "\n".join(f"  - {c['name']} : {c['type']}" for c in columns)
            + "\nIf the error is an unrecognized name, the column does not exist. Either use "
            "the correct name from this list, or check for it via INFORMATION_SCHEMA so the "
            "validation reports it as missing. Do NOT silently swap in a different column."
        )

    # Parse type mismatch hints directly from the error so the LLM doesn't have to guess
    type_hint = ""
    if "DATE, TIMESTAMP" in error or "DATE,TIMESTAMP" in error:
        type_hint = (
            "\nIMPORTANT: The partition/filter column is DATE type. "
            "Replace any TIMESTAMP_SUB(CURRENT_TIMESTAMP(), ...) with DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)."
        )
    elif "TIMESTAMP, DATE" in error or "TIMESTAMP,DATE" in error:
        type_hint = (
            "\nIMPORTANT: The partition/filter column is TIMESTAMP type. "
            "Replace any DATE_SUB(CURRENT_DATE(), ...) with TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)."
        )
    elif "DATETIME, DATE" in error or "DATETIME,DATE" in error:
        type_hint = (
            "\nIMPORTANT: The partition/filter column is DATETIME type. "
            "Replace any DATE_SUB(CURRENT_DATE(), ...) with DATETIME_SUB(CURRENT_DATETIME(), INTERVAL 7 DAY)."
        )

    client = OpenAI(base_url=_BASE_URL, api_key=api_key,
                    timeout=_LLM_TIMEOUT, max_retries=_LLM_RETRIES)
    response = client.chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": (
                f"Fix this BigQuery SQL query that failed with the error below.{type_hint}{schema_hint}\n\n"
                f"ERROR:\n{error}\n\n"
                f"ORIGINAL QUERY:\n{query}\n\n"
                f"Return ONLY the corrected SQL — no explanation, no markdown fences."
            )},
        ],
        temperature=0.1,
        top_p=0.7,
        max_tokens=_MAX_OUTPUT,
        stream=False,
    )
    return _content(response)
