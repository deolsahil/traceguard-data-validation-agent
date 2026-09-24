from __future__ import annotations

import argparse
import json
import difflib
import os
import re
import time
import traceback
from datetime import date
from pathlib import Path
from typing import Any

from agents.intent_parser import find_sp_destination, parse_intent
from agents.query_generator import (
    BrokenObject,
    bq_message,
    TruncatedResponse,
    _is_query_error,
    fix_query,
    generate_query,
)
from attachment_parser import collect_data_model, is_parseable
from bigquery_executor import (
    dry_run_query,
    get_routine_definition,
    latest_partition_date,
    get_table_metadata,
    suggest_column,
    suggest_object,
    run_query,
    save_result as save_execution_result,
)
from jira_reader import JiraReader
from traceability_report import (
    DASHBOARD_PATH,
    generate_dashboard,
    load_report,
    mark_scan_complete,
    save_report,
    update_traceability_report,
    utc_now,
)


OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

# A bulk scan must not be hostage to one slow ticket. Past this, the ticket is
# abandoned and the scan moves on; nothing is posted, so the next run retries it.
TICKET_BUDGET_SECONDS = float(os.getenv("TICKET_BUDGET_SECONDS", "180"))

# The label says "this ticket is for the agent", not which environment to touch — the
# target comes from the ticket itself. Configurable so a team can adopt its own
# convention without a code change.
VALIDATION_LABEL = os.getenv("VALIDATION_LABEL", "validation-agent")
AGENT_QUERY_MARKER = "[VALIDATION AGENT]"
AGENT_RESULT_MARKER = "[VALIDATION AGENT RESULT]"
# Some comments report a blocker rather than post a query: the object is unreadable, or
# the answer did not fit. They carry no SQL, so treating them as "a query was posted"
# parks the ticket in AWAITING_APPROVAL with nothing to approve — and overwrites the
# real reason on the next scan. They still must not repeat every four hours, which is
# what BLOCKED already means.
AGENT_NOTICE_MARKER = "[VALIDATION AGENT NOTICE]"
# Hard cycle boundary. The state machine reads Jira comments, so discarding a stale
# approved query means marking a point that everything before is history — otherwise
# the old APPROVED still matches and the same query runs again.
AGENT_RESET_MARKER = "[VALIDATION AGENT RESET]"
# Everything the agent acts on must be addressed to it. That is the whole rule:
# tag the agent, then say what you want. Without it, a manager typing a bare
# "APPROVED" for an unrelated sign-off would execute a pending query, and no amount
# of anchoring fixes that — only explicit addressing does.
AGENT_MENTION_RE = re.compile(
    r"@(?:validation[-_]?)?agent\b", re.IGNORECASE
)
APPROVED_RE = re.compile(r"^APPROVED\b(.*)", re.IGNORECASE | re.DOTALL)
REJECT_RE = re.compile(r"^REJECTE?D?\b(.*)", re.IGNORECASE | re.DOTALL)


def _addressed(body: str) -> bool:
    """True when this comment is talking to the agent rather than to the team."""
    return bool(AGENT_MENTION_RE.search(body or ""))


def _instruction(body: str) -> str:
    """The comment with the @mention and surrounding punctuation removed."""
    return AGENT_MENTION_RE.sub("", body or "").strip().lstrip("-:,–—*_ ").strip()


def _says_approved(body: str) -> bool:
    """
    True only when the instruction *begins* with APPROVED.

    Searching anywhere for the word meant "@agent - not approved yet, hold off"
    executed the query. Requiring it to lead keeps "APPROVED - looks good" working
    while refusing anything that merely mentions approval in passing.
    """
    return _addressed(body) and bool(APPROVED_RE.match(_instruction(body)))


def _says_reject(body: str) -> str | None:
    """The rejection reason, or None when this is not a rejection."""
    if not _addressed(body):
        return None
    match = REJECT_RE.match(_instruction(body))
    if not match:
        return None
    return match.group(1).strip(" -:,.*_\n") or ""


# Inline safety check — SELECT/WITH only, no DML
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


def _detect_state(ticket: dict[str, Any]) -> tuple[str, str | None]:
    """
    Scan ticket comments to determine pipeline state.

    Returns (state, generated_query):
      NOT_GENERATED     — nothing posted yet, or a new instruction started a cycle
      AWAITING_APPROVAL — query posted in this cycle, waiting for approval
      REJECTED          — the reviewer rejected it; the reason drives the retry
      APPROVED          — approval found after this cycle's query
      EXECUTED          — successful result posted, nothing asked of us since
      BLOCKED           — execution failed and nobody has responded

    Only comments that tag the agent count. Everything else is team conversation.
    """
    comments = ticket.get("comments", [])

    # A reset discards the whole prior cycle — the query it approved was written
    # against a schema that no longer exists. Take the last one.
    reset_idx = None
    for i, comment in enumerate(comments):
        if AGENT_RESET_MARKER in comment.get("body", ""):
            reset_idx = i
    if reset_idx is not None:
        comments = comments[reset_idx + 1:]

    # An execution failure leaves the ticket APPROVED, so the pipeline would retry
    # it on every scan and post an identical failure comment each time — burying the
    # ticket. Hold at BLOCKED until someone actually responds to the failure.
    last_result_idx = None
    for i, comment in enumerate(comments):
        if AGENT_RESULT_MARKER in comment.get("body", ""):
            last_result_idx = i

    if (
        last_result_idx is not None
        and "EXECUTION_FAILED" in comments[last_result_idx].get("body", "")
    ):
        restart_idx = None
        responded = False
        for i, comment in enumerate(comments[last_result_idx + 1:], last_result_idx + 1):
            body = comment.get("body", "")
            if _addressed(body) and not _says_approved(body):
                restart_idx = i  # a new instruction — regenerate from scratch
                break
            if _says_approved(body) or "{code" in body or "```sql" in body:
                responded = True  # corrected SQL or a re-approval — retry as normal
                break
        if restart_idx is not None:
            comments = comments[restart_idx + 1:]
        elif responded:
            # Retrying. If someone posted corrected SQL after the failure, use that —
            # the pre-failure APPROVED would otherwise win and re-run the same query.
            override = None
            for comment in comments[last_result_idx + 1:]:
                body = comment.get("body", "")
                match = re.search(r"\{code(?::sql)?\}(.*?)\{code\}", body, re.DOTALL) \
                    or re.search(r"```sql\n(.*?)```", body, re.DOTALL)
                if match and AGENT_QUERY_MARKER not in body:
                    override = match.group(1).strip()
            if override:
                return "APPROVED", override
        else:
            return "BLOCKED", None

    # A blocker we already reported. Nothing to approve and nothing to retry until a
    # person acts, so hold — the same rule an execution failure follows above.
    notice_idx = None
    for i, comment in enumerate(comments):
        body = comment.get("body", "")
        # Either the explicit marker, or — for comments posted before that marker
        # existed — an agent comment carrying no SQL at all. A query comment without a
        # query is a notice whatever it is labelled, and there is nothing to approve.
        if AGENT_NOTICE_MARKER in body or (
            AGENT_QUERY_MARKER in body
            and "{code:sql}" not in body
            and "```sql" not in body
        ):
            notice_idx = i
    if notice_idx is not None:
        if not any(_addressed(c.get("body", ""))
                   for c in comments[notice_idx + 1:]):
            return "BLOCKED", None

    # Find the last successful result comment
    result_idx = None
    for i, comment in enumerate(comments):
        body = comment.get("body", "")
        if AGENT_RESULT_MARKER in body and "EXECUTION_FAILED" not in body:
            result_idx = i

    # Determine the start of the current cycle
    cycle_start = 0
    if result_idx is not None:
        # Anyone addressing the agent starts a new cycle
        trigger_idx = None
        for i, comment in enumerate(comments[result_idx + 1:], result_idx + 1):
            if _addressed(comment.get("body", "")):
                trigger_idx = i
                break
        if trigger_idx is None:
            return "EXECUTED", None
        print(f"  New instruction to the agent — starting a fresh validation cycle")
        cycle_start = trigger_idx + 1

    # Find agent query within the current cycle
    agent_query_idx = None
    generated_query = None
    for i, comment in enumerate(comments[cycle_start:], cycle_start):
        body = comment.get("body", "")
        if AGENT_QUERY_MARKER in body:
            agent_query_idx = i
            m = re.search(r"\{code(?::sql)?\}(.*?)\{code\}", body, re.DOTALL) \
                or re.search(r"```sql\n(.*?)```", body, re.DOTALL)
            if m:
                generated_query = m.group(1).strip()

    if agent_query_idx is None:
        return "NOT_GENERATED", None

    # What the reviewer said after the query was posted. Only comments addressed to
    # the agent are considered; anything else on the ticket is team conversation.
    user_override_query = None
    for comment in comments[agent_query_idx + 1:]:
        body = comment.get("body", "")

        # Someone's own SQL is an override whoever posts it, and it needs approval
        # like any other query — so pick it up before deciding what the comment means.
        match = re.search(r"\{code(?::sql)?\}(.*?)\{code\}", body, re.DOTALL) \
            or re.search(r"```sql\n(.*?)```", body, re.DOTALL)
        has_sql = bool(match) and AGENT_QUERY_MARKER not in body
        if has_sql:
            user_override_query = match.group(1).strip()

        if not _addressed(body):
            continue  # not talking to us

        # Checked before approval so "@agent REJECT - not approved" reads correctly
        reason = _says_reject(body)
        if reason is not None:
            print(f"  Query rejected — regenerating{' with feedback' if reason else ''}")
            return "REJECTED", reason or None

        if _says_approved(body):
            return "APPROVED", user_override_query or generated_query

        if not has_sql:
            print(f"  New instruction after pending query — regenerating")
            return "NOT_GENERATED", None

    return "AWAITING_APPROVAL", generated_query


def _query_comment(intent: dict[str, Any], query: str, partition_info: dict[str, Any] | None = None) -> str:
    target = intent.get("full_table_name") or intent.get("view_name") or "unknown"

    if partition_info:
        m = re.search(r"INTERVAL\s+(\d+)\s+DAY", query, re.IGNORECASE)
        days = m.group(1) if m else "7"
        partition_line = (
            f"**Partition:** `{partition_info['field']}` "
            f"({partition_info.get('granularity') or partition_info['type']}) "
            f"— query filters last {days} day(s)\n"
        )
    else:
        partition_line = (
            "**Partition:** No partition column found — "
            "this query will scan the full table. Approve only if table size is acceptable.\n"
        )

    model_columns = intent.get("_data_model_columns") or []
    model_line = (
        f"*Data model:* {len(model_columns)} column(s) read from "
        f"`{intent.get('_data_model_file')}`\n"
        if model_columns else ""
    )

    # Anything the ticket asked for that SQL can't answer. Stating it here is the
    # whole point — the alternative is quietly answering less than was asked and
    # letting the reader assume otherwise.
    # Warn only when the window this query actually uses cannot reach the data.
    # A 200-day window on 149-day-old data is fine and needs no warning.
    stale_days = intent.get("_data_age_days")
    latest_data = (partition_info or {}).get("latest_data")
    window_days = int(days) if partition_info and str(days).isdigit() else None
    misses_data = (
        stale_days is not None
        and latest_data
        and window_days is not None
        and window_days < stale_days
    )
    stale_line = (
        f"\n*Heads-up:* newest data in this table is *{latest_data}* ({stale_days} days "
        f"old), so this {window_days}-day window will match no rows. Comment *@agent* "
        f"with a wider window — e.g. _@agent check the last {stale_days + 7} days_ — "
        f"if you need it to reach the data.\n"
        if misses_data else ""
    )

    unanswerable = intent.get("unanswerable") or []
    unanswerable_line = (
        "\n*I could not check:*\n"
        + "\n".join(f"* {u}" for u in unanswerable)
        + "\n"
        if unanswerable else ""
    )

    return (
        f"{AGENT_QUERY_MARKER}\n"
        f"*Validation Agent — Generated Query*\n\n"
        f"*Change detected:* {intent.get('change_summary')}\n"
        f"*Target:* {target}\n"
        f"{model_line}"
        f"*{partition_line.strip()}*\n"
        f"{stale_line}"
        f"{unanswerable_line}\n"
        f"{{code:sql}}\n{query}\n{{code}}\n\n"
        f"_Tag me to reply — I only act on comments that mention *@agent*._\n"
        f"*Approve:* *@agent APPROVED*\n"
        f"*Reject:* *@agent REJECT* followed by what's wrong — "
        f"e.g. _@agent REJECT - should be 30 days, not 7_. I'll rewrite it using your reason.\n"
        f"*Change what's checked:* *@agent* followed by what you want instead\n"
        f"*Use your own SQL:* paste it in a {{code:sql}} block, then *@agent APPROVED*"
    )


def _result_comment(issue_key: str, result: dict[str, Any], auto_run: bool = False,
                    empty_window: str = "") -> str:
    status = result.get("status", "UNKNOWN")
    details = result.get("validation_details") or ""
    bytes_display = result.get("total_bytes_processed_display") or ""
    errors = result.get("errors") or []
    passed = status == "VALIDATION_PASSED"

    if passed:
        status_line = f"*Status:* (/) VALIDATION PASSED"
    elif errors:
        status_line = f"*Status:* (x) {status}"
    else:
        status_line = f"*Status:* (!) {status}"

    error_block = ""
    if errors:
        error_items = "\n".join(f"** {e}" for e in errors)
        error_block = f"\n*Errors:*\n{error_items}"

    if passed:
        revalidate_hint = (
            "\n----\n_Description changed? Comment *@agent* with what to check for a "
            "fresh validation cycle._"
        )
    elif status == "EXECUTION_FAILED":
        # The agent stops here until someone responds, so spell out how to restart it.
        revalidate_hint = (
            "\n----\n_I won't retry this on the next scan — that would just repeat the "
            "same error. To restart, comment one of:_\n"
            "* *@agent* followed by what to check — regenerates from scratch\n"
            "* *@agent REJECT* followed by what was wrong — regenerates using your feedback\n"
            "* your own corrected SQL in a {code:sql} block, then *@agent APPROVED*"
        )
    else:
        revalidate_hint = (
            "\n----\n_Comment *@agent* with what to check to run a fresh validation cycle._"
        )
    auto_note = (
        "_Answered from table metadata only — no table rows were read, so there was "
        "nothing to approve and this ran straight away. (The bytes below are "
        "BigQuery's fixed minimum charge for an INFORMATION_SCHEMA lookup, not a "
        "scan of your data.)_\n\n"
        if auto_run else ""
    )

    return (
        f"{AGENT_RESULT_MARKER}\n"
        f"*Validation Agent — Execution Result*\n\n"
        f"{auto_note}{empty_window}"
        f"*Ticket:* {issue_key}\n"
        f"{status_line}\n"
        f"{f'*Details:* {details}' + chr(10) if details else ''}"
        f"{f'*Bytes scanned:* {bytes_display}' + chr(10) if bytes_display else ''}"
        f"{error_block}"
        f"{revalidate_hint}"
    )


def _save_validation_state(
    issue_key: str,
    status: str,
    query: str | None = None,
    schema: dict[str, Any] | None = None,
) -> None:
    path = OUTPUT_DIR / f"{issue_key.upper()}-validation-state.json"
    state: dict[str, Any] = {"jira_key": issue_key, "status": status, "query": query}
    if schema:
        state["schema"] = schema
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


class TicketTimeout(Exception):
    """This ticket has used its share of the scan."""


class CredentialsUnavailable(Exception):
    """The login expired. Every remaining ticket would fail the same way."""


def _is_credentials_error(error: Exception) -> bool:
    """
    Tell "the login expired" apart from "this ticket is broken".

    Since the move to Gemini, the model and BigQuery both authenticate with ADC, so
    one expired token fails every ticket in the scan — and it fails inside parse_intent,
    before the metadata guard that used to report this. Left unhandled it is invisible:
    the ticket saves no state, never reaches the report, and the dashboard shows a
    healthy agent that simply found nothing to do.

    Matched on the message as well as the type because either google.auth or the LLM
    client can surface it first, wrapped differently.
    """
    text = f"{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in (
        "refresherror",
        "defaultcredentialserror",
        "reauthentication is needed",
        "could not automatically determine credentials",
        "invalid_grant",
    ))


def _stage(issue_key: str, label: str, started: float) -> float:
    """
    Print how long a stage took and return a fresh start marker.

    Without this a slow run is unexplainable after the fact — the stream is not
    persisted, so there is nothing left to look at once it finishes.
    """
    print(f"  [{issue_key}] {label} ({time.monotonic() - started:.1f}s)")
    return time.monotonic()


def _check_budget(issue_key: str, ticket_started: float) -> None:
    spent = time.monotonic() - ticket_started
    if spent > TICKET_BUDGET_SECONDS:
        raise TicketTimeout(
            f"gave up after {spent:.0f}s (budget {TICKET_BUDGET_SECONDS:.0f}s)")


def _qualify(name: str, intent: dict[str, Any]) -> str:
    """Fill in project/dataset from the ticket when only a bare name was given."""
    parts = name.replace("`", "").split(".")
    if len(parts) >= 3:
        return ".".join(parts[-3:])
    dataset = intent.get("dataset") or ""
    project = intent.get("project") or ""
    if len(parts) == 2 and project:
        return f"{project}.{parts[0]}.{parts[1]}"
    if len(parts) == 1 and project and dataset:
        return f"{project}.{dataset}.{parts[0]}"
    return name


# A ticket listing objects is listing a handful, not a warehouse. Each one costs a
# metadata call, so stop at a sane number rather than let a pasted inventory fan out.
# ponytail: flat cap, make it a config value if anyone genuinely lists more.
_MAX_TARGETS = 12


def _already_suggested(suggestion: str, ticket_text_lower: str) -> bool:
    """True when the agent has already offered this name in an earlier comment."""
    return f"did you mean {suggestion.split('.')[-1].lower()}" in ticket_text_lower


def _resolve_targets(issue_key: str, intent: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """
    Read the real schema of every object a multi-object ticket named.

    Bare names inherit project.dataset from the first fully-qualified name in the same
    list — "check a.b.CLICKS, IMPRESSIONS and SCORES exist" means all three live in a.b,
    which is how people write it. Falls back to the ticket's own project/dataset fields.

    The resolved names are handed to the generator and echoed in the Jira comment on
    purpose: inheriting a path is a guess about intent, and a guess that stays invisible
    is how you validate the wrong object and report a pass for it.

    Returns {} when fewer than two objects were named, leaving the single-target path
    below exactly as it was.
    """
    raw = [str(t).replace("`", "").strip() for t in (intent.get("targets") or [])]
    raw = [t for t in raw if t]
    if len(raw) < 2:
        return {}

    prefix = next((".".join(n.split(".")[-3:-1]) for n in raw if len(n.split(".")) >= 3), None)

    seen: set[str] = set()
    ordered: list[str] = []
    for name in raw:
        qualified = name if name.count(".") >= 2 else (
            f"{prefix}.{name.split('.')[-1]}" if prefix else _qualify(name, intent)
        )
        if qualified.lower() not in seen:
            seen.add(qualified.lower())
            ordered.append(qualified)

    if len(ordered) > _MAX_TARGETS:
        print(f"  [{issue_key}] {len(ordered)} objects named — checking the first {_MAX_TARGETS}")
        ordered = ordered[:_MAX_TARGETS]

    # Say a near-miss once. Match on the phrase the agent actually posts, not on the bare
    # name: a suggestion is often a substring of the typo that produced it
    # (FOO_METRICS inside FOO_METRICSWWW), so a plain containment test makes the typo
    # suppress its own correction.
    already_said = (intent.get("_ticket_text") or "").lower()

    resolved: dict[str, dict[str, Any]] = {}
    for qualified in ordered:
        meta = get_table_metadata(qualified)
        exists = bool(meta.get("exists")) and not meta.get("error")
        suggestion = None
        if not exists and not meta.get("error"):
            suggestion = suggest_object(qualified)
            if suggestion and _already_suggested(suggestion, already_said):
                suggestion = None
        resolved[qualified] = {
            "exists": exists,
            "columns": meta.get("columns") or [],
            "error": meta.get("error"),
            "suggestion": suggestion,
        }
    intent["_target_schemas"] = resolved
    return resolved


def _is_real_table(name: str, intent: dict[str, Any]) -> bool:
    """Does this name resolve to an actual table or view?"""
    return get_table_metadata(_qualify(name, intent))["exists"]


def _resolve_sp_target(issue_key: str, sp_name: str, intent: dict[str, Any]) -> str | None:
    """
    Find the table a stored procedure writes to, and confirm it is really a table.

    The model reads the procedure body because procedures differ too much to match
    on — one builds its target with FORMAT and %s, the next concatenates a string
    for EXECUTE IMMEDIATE. BigQuery then verifies the answer, so a confident but
    wrong reading is caught rather than validated against.
    """
    qualified = _qualify(sp_name, intent)
    routine = get_routine_definition(qualified)
    if not routine:
        return None

    print(f"  [{issue_key}] '{sp_name.split('.')[-1]}' is a stored procedure — reading it")
    found = find_sp_destination(qualified, routine["body"])

    verified = []
    for table in found["tables"]:
        full = _qualify(table, intent)
        if get_table_metadata(full)["exists"]:
            verified.append(full)
        else:
            print(f"  [{issue_key}] discarded '{full}' — not an existing table")

    if not verified:
        print(f"  [{issue_key}] Could not determine the SP's destination table "
              f"— add table_name: to the ticket")
        return None

    intent["full_table_name"] = verified[0]
    intent["_sp_write_targets"] = verified
    intent["_sp_name"] = qualified
    if found["reasoning"]:
        print(f"  [{issue_key}] {found['reasoning']}")
    print(f"  [{issue_key}] SP writes to: {', '.join(verified)}")
    return verified[0]


def _reject_unearned_pass(result: dict[str, Any], query: str) -> None:
    """
    A pass has to be earned by reading data — 0 bytes scanned means nobody looked.

    An empty result makes some checks trivially true: on a table whose partitions
    stopped loading, `COUNTIF(col IS NOT NULL) = COUNT(*)` is `0 = 0`, so the agent
    reports a fully populated column having read nothing at all. Which spelling the
    model picks decides the verdict — `> 0` fails honestly on the same empty input —
    so this cannot be left to the prompt.

    Downgraded to VALIDATION_FAILED rather than a new status on purpose: that is what
    _explain_empty_window keys on, so the reader gets the reason (where the data
    actually is, and the window to ask for) instead of a bare failure.
    """
    if result.get("status") != "VALIDATION_PASSED":
        return
    if result.get("total_bytes_processed", 0) > 0:
        return
    if _is_metadata_only(query):
        return  # metadata reads scan no data by nature — nothing to earn
    if _counts_rows_only(query):
        return  # BigQuery answers a bare row count from metadata — see the helper

    result["status"] = "VALIDATION_FAILED"
    result["validation_passed"] = False
    result["errors"] = list(result.get("errors") or []) + [
        "0 bytes scanned — the query matched no rows, so this is not evidence of a pass"
    ]
    # Lead with the verdict. Appending it to the raw detail produced sentences that
    # argued with themselves — "row_count=1000 — INCONCLUSIVE: nothing was scanned".
    detail = (result.get("validation_details") or "").strip()
    result["validation_details"] = (
        "INCONCLUSIVE: nothing was scanned, so this result proves nothing"
        + (f" (query reported: {detail})" if detail else "")
    )


def _explain_empty_window(result: dict[str, Any], query: str,
                          partition_info: dict[str, Any] | None) -> str:
    """
    Distinguish "the check failed" from "the window I looked in was empty".

    A partitioned table that stopped loading matches no partitions for the default
    last-7-days filter: 0 bytes scanned, 0 rows, reported as a failure. That reads
    as "the column has no data", which is a different and much worse claim.
    """
    if result.get("status") != "VALIDATION_FAILED":
        return ""
    if result.get("total_bytes_processed", 0) > 0:
        return ""  # it really did look at data
    if not partition_info or not partition_info.get("latest_data"):
        return ""
    latest = partition_info["latest_data"]
    try:
        age = (date.today() - date.fromisoformat(latest)).days
    except ValueError:
        return ""
    match = re.search(r"INTERVAL\s+(\d+)\s+DAY", query or "", re.IGNORECASE)
    window = int(match.group(1)) if match else 7
    if window >= age:
        return ""  # the window did reach the data, so the failure is real
    return (
        f"_Nothing was scanned: this table's newest data is *{latest}* ({age} days old), "
        f"so the {window}-day window matched no rows. This is not evidence the column is "
        f"empty — nobody looked where the data is. Comment *@agent* with a wider window, "
        f"e.g. _@agent check the last {age + 7} days_._\n\n"
    )


def _reconcile_column(issue_key: str, intent: dict[str, Any], columns: list[dict[str, str]]) -> None:
    """
    Settle disagreements about which column the ticket means, using the real schema.

    column_name is scraped from `column_added:`-style fields in the description, and
    that scrape overrides whatever the model concluded. So when someone corrects a
    typo in a comment — "the column name could be product_name" — the stale value in
    the description wins and the correction is ignored. Here we can actually check:
    if the scraped name isn't in the table but a name from the ticket text is, the
    ticket text was right.
    """
    requested = intent.get("column_name")
    if not requested:
        return
    names = [c["name"] for c in columns]
    if requested in names:
        return

    # A request naming several columns is not a misspelling of one. The correction
    # below would match whichever of them happens to exist, rewrite the intent to
    # that one, and tell the generator to ignore the rest — answering, and passing,
    # a multi-column ticket on the subset that was already there.
    if "," in requested:
        return

    # Did a HUMAN name a column that really exists? That, and only that, is a
    # correction. Everything searched here must be text a person typed:
    #
    #   _human_text       ticket + comments, with the agent's own comments stripped
    #   _agent_directive  the last "@agent ..." instruction, already human-only
    #
    # Deliberately excluded: _ticket_text, and the model's own paraphrases
    # (validation_requirements, change_summary). A previous scan's comment says
    # "MISSING (did you mean customer_id?)", and any text derived from it carries that
    # name — read back, the agent treats its own guess as a human correction, swaps in
    # the column it suggested and passes a validation that should fail. The paraphrases
    # are summaries of the full ticket, so they are a second-hand route to the same
    # thing. No fallback to _ticket_text either: finding no correction is the safe
    # outcome, making up the wrong one is not.
    text = " ".join(filter(None, [
        intent.get("_human_text"),
        intent.get("_agent_directive"),
    ]))
    mentioned = [n for n in names if re.search(rf"\b{re.escape(n)}\b", text)]
    if len(mentioned) == 1:
        print(f"  [{issue_key}] '{requested}' is not in the table — the ticket text "
              f"says '{mentioned[0]}', which is. Using that.")
        intent["column_name"] = mentioned[0]
        # The requirements text and ticket body still carry the old name, and the
        # generator follows those about half the time. State the correction outright.
        intent["_column_corrected"] = (requested, mentioned[0])
        return

    # Otherwise offer the closest real column so the result is actionable, but don't
    # silently swap it — the reviewer decides whether it was a typo. BigQuery's own
    # "Did you mean ...?" is the better source than string similarity, and a dry run
    # gets it for nothing; fall back to difflib when it offers nothing.
    target = intent.get("full_table_name")
    suggestion = suggest_column(target, requested) if target else None
    if not suggestion:
        close = difflib.get_close_matches(requested, names, n=1, cutoff=0.7)
        suggestion = close[0] if close else None
    if suggestion:
        intent["_column_suggestion"] = suggestion
        print(f"  [{issue_key}] '{requested}' is not in the table — closest match is '{suggestion}'")


_OTHER_AGGREGATE = re.compile(
    r"\b(?:COUNTIF|SUM|AVG|MIN|MAX|ARRAY_AGG|STRING_AGG|LOGICAL_AND|LOGICAL_OR|APPROX_\w+)\s*\("
    r"|\bCOUNT\s*\(\s*(?!\*\s*\))",   # COUNT(col) and COUNT(DISTINCT col), but not COUNT(*)
    re.IGNORECASE,
)


def _counts_rows_only(query: str) -> bool:
    """
    True if the only thing this query measures is COUNT(*) over an unfiltered table.

    BigQuery serves that from table metadata and bills 0 bytes, so the usual
    "0 bytes means nobody looked" rule reads a correct answer as an empty one — a
    real ticket asking for a record count came back FAILED with
    "row_count=1000 — INCONCLUSIVE", which is both wrong and self-contradictory.

    Kept deliberately narrow. A WHERE clause means rows really were filtered, so 0
    bytes there is the empty-window case the caller must still catch. Any aggregate
    over a column disqualifies it too: `COUNTIF(c IS NOT NULL) = COUNT(*)` is the
    0 = 0 unearned pass, and on an empty table it also scans 0 bytes. What is left —
    COUNT(*) alone, no filter — is honest either way, because an empty table makes
    `COUNT(*) > 0` false rather than true.
    """
    if not re.search(r"\bCOUNT\s*\(\s*\*\s*\)", query, re.IGNORECASE):
        return False
    if re.search(r"\bWHERE\b", query, re.IGNORECASE):
        return False
    return not _OTHER_AGGREGATE.search(query)


def _is_metadata_only(query: str) -> bool:
    """
    True if the query reads nothing but INFORMATION_SCHEMA.

    Such a query costs nothing and exposes no row data — it answers questions like
    "does this table exist" that anyone could look up in the BigQuery console. There
    is nothing for an approver to weigh, so these run without the approval gate.

    Deliberately conservative: anything it cannot positively verify as metadata-only
    returns False and goes through approval as normal.
    """
    refs = re.findall(r"`([^`]+)`", query)
    if not refs:
        return False  # no explicit table reference — can't verify, so don't skip review
    if any("INFORMATION_SCHEMA" not in ref.upper() for ref in refs):
        return False

    # An unquoted dotted target (FROM project.dataset.table) would slip past the
    # backtick check, so reject those too. Bare names are CTE references and fine.
    for target in re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z0-9_.\-]+)", query, re.IGNORECASE):
        if "." in target:
            return False
    return True


def _schema_snapshot(target: str, columns: list[dict[str, str]]) -> dict[str, Any]:
    """What the table looked like when the query was written, so drift is detectable."""
    return {
        "target": target,
        "columns": sorted(f"{c['name']}:{c['type']}" for c in columns),
    }


def _schema_drift(issue_key: str) -> str | None:
    """
    Compare the table now against the snapshot taken when the query was generated.

    Approval is a human step, so hours or days can pass in between. A query written
    for a column that has since been added — or removed, or retyped — can report a
    pass for a check it never actually performed. Returns a description of the
    change, or None if nothing relevant moved.
    """
    path = OUTPUT_DIR / f"{issue_key.upper()}-validation-state.json"
    if not path.exists():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8")).get("schema")
    except (json.JSONDecodeError, OSError):
        return None
    if not saved or not saved.get("target"):
        return None

    now = get_table_metadata(saved["target"])
    if not now["exists"]:
        # Can't read it — could be a permissions blip. Don't block execution on that.
        return None

    before = dict(c.split(":", 1) for c in (saved.get("columns") or []))
    after = {c["name"]: c["type"] for c in now["columns"]}
    if before == after:
        return None

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    retyped = sorted(
        f"{name} ({before[name]} -> {after[name]})"
        for name in set(before) & set(after)
        if before[name] != after[name]
    )
    parts = []
    if added:
        parts.append(f"added: {', '.join(added)}")
    if removed:
        parts.append(f"removed: {', '.join(removed)}")
    if retyped:
        parts.append(f"type changed: {', '.join(retyped)}")
    return "; ".join(parts)


def _save_query_review(issue_key: str, query: str) -> None:
    path = OUTPUT_DIR / f"{issue_key.upper()}-query-review.json"
    path.write_text(
        json.dumps({
            "stage": "QUERY_REVIEW",
            "jira_key": issue_key,
            "status": "READY_FOR_BIGQUERY_DRY_RUN",
            "query": query,
            "errors": [],
        }, indent=2),
        encoding="utf-8",
    )


def _save_ticket_snapshot(issue_key: str, ticket: dict[str, Any]) -> None:
    path = OUTPUT_DIR / f"{issue_key.upper()}.json"
    path.write_text(json.dumps(ticket, indent=2, ensure_ascii=False), encoding="utf-8")



def run_agent_workflow(
    issue_key: str,
    ticket_data: dict[str, Any],
    reader: JiraReader,
    batch: bool = False,
) -> int:
    issue_key = issue_key.strip().upper()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    _save_ticket_snapshot(issue_key, ticket_data)

    # Label guard
    if VALIDATION_LABEL not in (ticket_data.get("labels") or []):
        _save_validation_state(issue_key, "MISSING_VALIDATION_LABEL")
        update_traceability_report(issue_key, render_html=not batch)
        print(f"  [{issue_key}] Skipped — '{VALIDATION_LABEL}' label not present.")
        return 1

    _t_ticket = time.monotonic()
    _t = _t_ticket
    comment_count = len(ticket_data.get("comments") or [])
    print(f"  [{issue_key}] Reading ticket description...")
    if comment_count:
        print(f"  [{issue_key}] Reading {comment_count} comment(s)...")

    state, generated_query = _detect_state(ticket_data)

    # A rejection carries its reason in the second slot, not a query. Regenerate as
    # if nothing had been posted, but with the reviewer's feedback in hand.
    rejection_reason = None
    if state == "REJECTED":
        rejection_reason, generated_query, state = generated_query, None, "NOT_GENERATED"

    print(f"  [{issue_key}] State: {state}")

    if state == "BLOCKED":
        # Last run failed and nobody has responded. Posting the same error again
        # helps no one, so leave the ticket alone and move to the next one.
        print(
            f"  [{issue_key}] Previous run failed and no fix posted yet — skipping. "
            f"Tag @agent with an instruction, @agent REJECT, or your own SQL to retry."
        )
        update_traceability_report(issue_key, render_html=not batch)
        return 1

    if state == "EXECUTED":
        update_traceability_report(issue_key, render_html=not batch)
        return 0

    if state == "NOT_GENERATED":
        print(f"  [{issue_key}] Parsing intent...")
        try:
            intent = parse_intent(ticket_data)
        except Exception as error:
            if not _is_credentials_error(error):
                raise
            print(f"  [{issue_key}] Login expired — cannot read the ticket. "
                  f"Run: gcloud auth application-default login")
            _save_validation_state(issue_key, "CREDENTIALS_UNAVAILABLE")
            update_traceability_report(issue_key, render_html=not batch)
            raise CredentialsUnavailable(
                "expired credentials — no ticket in this scan can be validated") from error
        _t = _stage(issue_key, "intent parsed", _t)
        _check_budget(issue_key, _t_ticket)
        change_type = intent.get("change_type", "unknown")

        # Tickets often say "create table X, see attached data model" — read the
        # column list out of the attached spreadsheet instead of the ticket text.
        spreadsheets = [
            a for a in (ticket_data.get("attachments") or [])
            if is_parseable(a.get("filename") or "")
        ]
        if spreadsheets:
            print(f"  [{issue_key}] Reading {len(spreadsheets)} attachment(s)...")
            data_model = collect_data_model(ticket_data, reader)
            if data_model:
                intent["_data_model_columns"] = data_model["columns"]
                intent["_data_model_file"] = data_model["filename"]
                columns = data_model["columns"]
                print(
                    f"  [{issue_key}] Data model: {len(columns)} column(s) "
                    f"from {data_model['filename']}"
                )
                # An attached data model IS the specification. Tickets that use one
                # are usually terse ("create this table, see attached"), so the
                # requirement parsed from the text says nothing about columns —
                # state it explicitly or the generator validates something generic.
                typed = [c for c in columns if c.get("data_type")]
                intent["validation_requirements"] = (
                    f"Verify the table matches the attached data model "
                    f"({data_model['filename']}): every one of these {len(columns)} columns "
                    f"must exist"
                    + (" with the specified data type" if typed else "")
                    + " — " + ", ".join(c["column_name"] for c in columns)
                    + ". Report which columns are missing"
                    + (" or have the wrong type." if typed else ".")
                )
            else:
                print(f"  [{issue_key}] No readable data model in attachments")

        # What blocks us is not knowing WHAT to look at. A ticket can ask a perfectly
        # clear question ("does this column exist?") that isn't a "change" at all, so
        # gate on having a target rather than on the change_type taxonomy.
        target = intent.get("full_table_name") or intent.get("view_name")

        # A ticket naming several objects has nowhere to put them: full_table_name and
        # view_name hold one each. The parser leaves both null rather than pick a
        # favourite, which used to land here as "I could not work out which table to
        # validate" on a ticket that named three. Read each one's metadata instead and
        # let the generator answer about all of them.
        multi = _resolve_targets(issue_key, intent)
        if multi:
            print(f"  [{issue_key}] {len(multi)} objects named — validating all of them")
            for name, meta in multi.items():
                print(f"  [{issue_key}]   {name}: "
                      + (f"{len(meta['columns'])} column(s)" if meta["exists"] else "NOT FOUND"))

        if not target and not multi and not intent.get("sp_name"):
            reader.add_comment(
                issue_key,
                (
                    f"{AGENT_QUERY_MARKER}\n"
                    "**Validation Agent — Needs Clarification**\n\n"
                    "I could not work out which table or view to validate.\n"
                    "Please add a comment with:\n"
                    "- `table_name`: full table name (project.dataset.table)\n"
                    "- what you want checked, in plain English\n\n"
                    "Tag *@agent* followed by the instruction — I only act on comments that mention me."
                ),
            )
            _save_validation_state(issue_key, "NEEDS_CLARIFICATION")
            if not batch:
                update_traceability_report(issue_key)
            return 1

        # A stored procedure isn't queryable, so validate the table it writes to.
        # The name can arrive as sp_name, or as full_table_name when the ticket
        # phrases it like a table ("This is the SP - X") — so treat any target that
        # turns out not to be a table as a possible procedure.
        candidate = intent.get("sp_name") or target
        if candidate and not _is_real_table(candidate, intent):
            target = _resolve_sp_target(issue_key, candidate, intent) or target

        # Nothing resolved: the name is neither a table, a view, nor a procedure we
        # could read. Say so explicitly, because otherwise the schema read below is
        # skipped and the generator is left with no target and no column list — and
        # it fills that void with a plausible table name derived from the ticket
        # ("calculate_order_metrics" -> `order_metrics`), which 404s if we are lucky
        # and answers about the wrong object if we are not. Observed as a coin flip
        # between the two, which is worse than either: it hides.
        if not target and candidate:
            missing_name = _qualify(candidate, intent)
            intent["_object_missing"] = missing_name
            # Offer the closest real object once, as a question. Repeating it after the
            # reviewer has already seen it turns a helpful nudge into nagging.
            near = suggest_object(missing_name)
            if near and not _already_suggested(near, (intent.get("_ticket_text") or "").lower()):
                intent["_object_suggestion"] = near
            print(f"  [{issue_key}] '{candidate}' is not a table, view or readable "
                  f"procedure — validating its absence only")

        print(f"  [{issue_key}] Detected: {change_type} on {target}")

        # Read the real schema + partitioning — zero bytes, metadata only. Giving
        # the generator the actual column list is what stops it inventing names.
        partition_info = None
        if target:
            print(f"  [{issue_key}] Reading table schema...")
            meta = get_table_metadata(target)
            if meta.get("error"):
                # We could not read the table, so we know neither its columns nor
                # whether it is partitioned. Generating anyway would produce a query
                # built on false assumptions — including a full table scan on a
                # table that is in fact partitioned.
                print(f"  [{issue_key}] Cannot read table metadata: {meta['error'].splitlines()[0][:160]}")
                print(f"  [{issue_key}] Skipping — fix access and re-run, no query generated.")
                _save_validation_state(issue_key, "METADATA_UNAVAILABLE")
                update_traceability_report(issue_key, render_html=not batch)
                return 1

            partition_info = meta["partition"]
            # Anchor the time window to where the data actually is. A table that
            # stopped loading months ago has nothing in "the last 7 days", and the
            # query would report the column empty rather than say it looked in an
            # empty window.
            if partition_info and partition_info.get("type") == "time":
                newest = latest_partition_date(target, partition_info["field"])
                if newest:
                    partition_info["latest_data"] = newest
                    stale = (date.today() - date.fromisoformat(newest)).days
                    intent["_data_age_days"] = stale
                    if stale > 7:
                        print(f"  [{issue_key}] Newest data is {newest} ({stale} days old) "
                              f"— a 7-day window will match nothing")
            if meta["exists"]:
                intent["_actual_columns"] = meta["columns"]
                print(f"  [{issue_key}] Table has {len(meta['columns'])} column(s)")
                _reconcile_column(issue_key, intent, meta["columns"])
            else:
                intent["_table_missing"] = True
                # Same courtesy as _object_missing above: offer the close name once.
                near = suggest_object(target)
                if near and not _already_suggested(near, (intent.get("_ticket_text") or "").lower()):
                    intent["_object_suggestion"] = near
                    print(f"  [{issue_key}] '{target}' not found — closest is {near}")
                print(f"  [{issue_key}] Table not found or unreadable — validating existence only")
            _t = _stage(issue_key, "schema read", _t)
            _check_budget(issue_key, _t_ticket)
            if partition_info:
                print(f"  [{issue_key}] Partitioned on '{partition_info['field']}' ({partition_info.get('granularity') or partition_info['type']})")
            else:
                print(f"  [{issue_key}] No partition found — full table scan warning will be shown")

        if rejection_reason:
            intent["_rejection_feedback"] = rejection_reason
            print(f"  [{issue_key}] Reviewer feedback: {rejection_reason[:100]}")

        print(f"  [{issue_key}] Generating query...")
        try:
            query = generate_query(intent, partition_info=partition_info)
        except BrokenObject as broken:
            # Not a validation result: the object cannot be read at all, so there is no
            # check to run and nothing to approve. Reported as the error it is, with
            # BigQuery's own words — which usually name the fix.
            print(f"  [{issue_key}] Object is unusable: {broken}")
            reader.add_comment(
                issue_key,
                (
                    f"{AGENT_NOTICE_MARKER}\n"
                    f"*Validation Agent — The Object Itself Is Broken*\n\n"
                    f"I could not validate this because the table or view cannot be read "
                    f"at all — any query against it fails before it runs. This is not a "
                    f"validation result; there is nothing to approve until it is fixed.\n\n"
                    f"*BigQuery said:* {broken}\n\n"
                    f"Fix the object, then tag *@agent* to run the check."
                ),
            )
            _save_validation_state(issue_key, "OBJECT_BROKEN")
            update_traceability_report(issue_key, render_html=not batch)
            _stage(issue_key, "TOTAL", _t_ticket)
            return 1
        except TruncatedResponse as cut:
            # The answer was cut off, not wrong. Sending half a statement to BigQuery
            # gets it reported back as a syntax error, which sends the reader hunting
            # for a mistake in a query that simply stopped mid-line.
            print(f"  [{issue_key}] Generation cut off: {cut}")
            reader.add_comment(
                issue_key,
                (
                    f"{AGENT_NOTICE_MARKER}\n"
                    f"*Validation Agent — Answer Was Cut Off*\n\n"
                    f"The query for this ticket did not fit in one answer, so I have not "
                    f"posted a partial one — half a statement is not something you can "
                    f"safely approve.\n\n"
                    f"This usually means the ticket asks for a per-column check across a "
                    f"wide table. Narrowing it to the columns you care about, or splitting "
                    f"it into separate asks, will fit.\n\n"
                    f"Tag *@agent* with a narrower check to try again."
                ),
            )
            _save_validation_state(issue_key, "QUERY_INVALID")
            update_traceability_report(issue_key, render_html=not batch)
            _stage(issue_key, "TOTAL", _t_ticket)
            return 1
        _t = _stage(issue_key, "query generated + validated", _t)

        errors = _safety_errors(query)
        if errors:
            print(f"  [{issue_key}] Safety check failed: {errors}")
            _save_validation_state(issue_key, "BLOCKED_UNSAFE_SQL")
            if not batch:
                update_traceability_report(issue_key)
            return 1

        # Never invite approval on SQL we already know is broken. The generator has
        # had its correction attempts by now; if BigQuery still rejects it, say so
        # instead of putting "reply APPROVED" under an invalid query.
        valid, dry_err, _ = dry_run_query(query)
        if not valid and _is_query_error(dry_err):
            first_line = dry_err.splitlines()[0][:200]
            print(f"  [{issue_key}] Could not produce valid SQL — asking for help instead")
            reader.add_comment(
                issue_key,
                (
                    f"{AGENT_QUERY_MARKER}\n"
                    f"*Validation Agent — Could Not Build a Valid Query*\n\n"
                    f"I could not produce SQL that BigQuery accepts for this ticket, so "
                    f"there is nothing safe to approve.\n\n"
                    f"*BigQuery said:* {bq_message(first_line)}\n\n"
                    f"*My last attempt:*\n{{code:sql}}\n{query}\n{{code}}\n\n"
                    f"Tag *@agent* with more detail about what to check, or paste a corrected "
                    f"query in a {{code:sql}} block and comment *@agent APPROVED*."
                ),
            )
            _save_validation_state(issue_key, "QUERY_INVALID", query)
            update_traceability_report(issue_key, render_html=not batch)
            _stage(issue_key, "TOTAL", _t_ticket)
            return 1

        # A metadata-only question ("does this table exist?") reads no data and costs
        # nothing, so there is nothing for an approver to decide. Answer it now rather
        # than making someone approve a free schema lookup.
        if _is_metadata_only(query):
            print(f"  [{issue_key}] Metadata-only query — running now, no approval needed")
            result = run_query(issue_key=issue_key, query=query)
            _t = _stage(issue_key, "executed in BigQuery", _t)
            save_execution_result(issue_key, result)
            reader.add_comment(issue_key, _result_comment(
                issue_key, result, auto_run=True,
                empty_window=_explain_empty_window(result, query, partition_info)))
            print(f"  [{issue_key}] Execution status: {result.get('status')}")
            _stage(issue_key, "TOTAL", _t_ticket)
            _save_validation_state(issue_key, "VALIDATION_EXECUTED", query)
            update_traceability_report(issue_key, render_html=not batch)
            return 0 if result.get("status") == "VALIDATION_PASSED" else 1

        reader.add_comment(issue_key, _query_comment(intent, query, partition_info))
        print(f"  [{issue_key}] Query posted to Jira. Waiting for APPROVED.")
        _stage(issue_key, "TOTAL", _t_ticket)

        # Record the schema this query was written against so drift can be caught
        # before it executes, however long approval takes.
        snapshot = (
            _schema_snapshot(target, intent["_actual_columns"])
            if target and intent.get("_actual_columns") else None
        )
        _save_validation_state(issue_key, "AWAITING_APPROVAL", query, schema=snapshot)
        _save_query_review(issue_key, query)
        update_traceability_report(issue_key, render_html=not batch)
        return 0

    if state == "AWAITING_APPROVAL":
        print(f"  [{issue_key}] Waiting for APPROVED comment in Jira.")
        _save_validation_state(issue_key, "AWAITING_APPROVAL", generated_query)
        update_traceability_report(issue_key, render_html=not batch)
        return 0

    if state == "APPROVED":
        if not generated_query:
            print(f"  [{issue_key}] ERROR: Could not extract query from agent comment.")
            return 1

        # The table may have changed while the query sat waiting for approval.
        # Executing a query written for a different schema can report a pass for a
        # check it never performed, so regenerate instead.
        drift = _schema_drift(issue_key)
        if drift:
            print(f"  [{issue_key}] Schema changed since approval ({drift}) — regenerating")
            reader.add_comment(
                issue_key,
                (
                    f"{AGENT_RESET_MARKER}\n"
                    f"*Validation Agent — Schema Changed*\n\n"
                    f"The table changed after this query was approved ({drift}), so the "
                    f"approved query no longer matches what is there. I have discarded it "
                    f"rather than run a check that could report a misleading result.\n\n"
                    f"A fresh query will be generated on the next run for you to approve."
                ),
            )
            _save_validation_state(issue_key, "NOT_GENERATED")
            update_traceability_report(issue_key, render_html=not batch)
            return 1

        MAX_RETRIES = 3
        current_query = generated_query
        result = None

        # The fix pass needs the real schema — most retryable errors are invented
        # column names. Recover the table from the approved SQL, ignoring
        # INFORMATION_SCHEMA references.
        schema_columns: list[dict[str, str]] = []
        for match in re.finditer(r"`([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)`", current_query):
            candidate = match.group(1)
            if "INFORMATION_SCHEMA" in candidate.upper():
                continue
            schema_columns = get_table_metadata(candidate)["columns"]
            if schema_columns:
                break

        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                print(f"  [{issue_key}] Retrying ({attempt - 1}/{MAX_RETRIES - 1}) — asking LLM to fix SQL...")
            else:
                print(f"  [{issue_key}] APPROVED — executing in BigQuery...")

            result = run_query(issue_key=issue_key, query=current_query)
            _reject_unearned_pass(result, current_query)
            _t = _stage(issue_key, f"executed in BigQuery (attempt {attempt})", _t)
            errors = result.get("errors") or []

            if result.get("status") != "EXECUTION_FAILED" or attempt == MAX_RETRIES:
                break

            error_msg = "; ".join(errors)
            print(f"  [{issue_key}] SQL error: {error_msg[:140]}")
            try:
                current_query = fix_query(current_query, error_msg, schema_columns)
            except Exception as e:
                print(f"  [{issue_key}] fix_query failed: {e}")
                break

        save_execution_result(issue_key, result)
        exec_partition = None
        for match in re.finditer(r"`([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)`", current_query):
            if "INFORMATION_SCHEMA" in match.group(1).upper():
                continue
            meta_now = get_table_metadata(match.group(1))
            exec_partition = meta_now.get("partition")
            if exec_partition and exec_partition.get("type") == "time":
                exec_partition["latest_data"] = latest_partition_date(
                    match.group(1), exec_partition["field"])
            break
        reader.add_comment(issue_key, _result_comment(
            issue_key, result,
            empty_window=_explain_empty_window(result, current_query, exec_partition)))
        print(f"  [{issue_key}] Execution status: {result.get('status')}")
        _stage(issue_key, "TOTAL", _t_ticket)

        # Record the query that actually ran, not the pre-retry version
        _save_validation_state(issue_key, "VALIDATION_EXECUTED", current_query)
        update_traceability_report(issue_key, render_html=not batch)
        return 0 if result.get("status") == "VALIDATION_PASSED" else 1

    return 1


def run_bulk(jql: str, board_id: int | None = None) -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("TRACEGUARD DATA VALIDATION — AGENT SCAN")
    print("=" * 72)
    print("JQL:", jql)
    if board_id:
        print("Board ID:", board_id)
    print()

    reader = JiraReader()
    print("Searching Jira...")
    _t_scan = time.monotonic()
    _scan_started = utc_now()
    try:
        tickets = reader.search_issues(jql, board_id=board_id, required_label=VALIDATION_LABEL)
    except Exception as e:
        print(f"\n[ERROR] Could not reach Jira: {e}")
        print("Check your VPN connection and JIRA_BASE_URL in .env.")
        print("Dashboard unchanged — cached tickets are still visible.")
        return 1
    print(f"Found {len(tickets)} ticket(s) in {time.monotonic() - _t_scan:.1f}s.\n")

    # Prune only on a successful Jira fetch — never wipe cached tickets on a connection failure.
    found_keys = {(t.get("jira_key") or "").upper() for t in tickets if t.get("jira_key")}
    report = load_report()
    kept, pruned = [], []
    for t in report.get("tickets", []):
        key = t.get("jira_key", "").upper()
        if key in found_keys:
            kept.append(t)
        else:
            pruned.append(key)
            for suffix in ["", "-validation-state", "-query-review", "-execution-result", "-approval-check"]:
                (OUTPUT_DIR / f"{key}{suffix}.json").unlink(missing_ok=True)
    if pruned:
        report["tickets"] = kept
        save_report(report)
        print(f"Pruned {len(pruned)} stale ticket(s): {', '.join(pruned)}")

    if not tickets:
        print("No tickets matched.")
        # Still a completed scan: stamp it so the dashboard shows the agent ran. It gets
        # the same timing as any other run — reporting a dash for duration made a real
        # scan look like a broken record.
        generate_dashboard(mark_scan_complete(
            started_at=_scan_started,
            duration_seconds=time.monotonic() - _t_scan,
            scanned=0,
        ))
        return 0

    exit_codes: list[int] = []

    for i, ticket in enumerate(tickets, 1):
        issue_key = (ticket.get("jira_key") or "").upper()
        if not issue_key:
            continue
        print(f"[{i}/{len(tickets)}] {issue_key}")
        try:
            code = run_agent_workflow(
                issue_key=issue_key,
                ticket_data=ticket,
                reader=reader,
                batch=True,
            )
        except TicketTimeout as timeout:
            # Nothing was posted, so the next scan picks this ticket up again.
            print(f"  [{issue_key}] {timeout} — skipping so the scan can continue")
            code = 1
        except CredentialsUnavailable as expired:
            print(f"  [{issue_key}] {expired}")
            print(f"\nSCAN ABORTED after {i} of {len(tickets)} tickets — fix the login "
                  f"and re-run:\n  gcloud auth application-default login")
            exit_codes.append(1)
            break
        except Exception as error:
            if _is_credentials_error(error):
                # Expired mid-scan, past the first LLM call. Same conclusion.
                print(f"  [{issue_key}] Login expired mid-scan: {error}")
                _save_validation_state(issue_key, "CREDENTIALS_UNAVAILABLE")
                print(f"\nSCAN ABORTED after {i} of {len(tickets)} tickets — fix the login "
                      f"and re-run:\n  gcloud auth application-default login")
                exit_codes.append(1)
                break
            # Same reasoning as the timeout above, for everything else: BigQuery
            # timing out or Jira refusing a comment is one ticket's problem, and the
            # next scan retries it. Without this, one bad ticket ends the scan —
            # every remaining ticket goes unvalidated and no dashboard is written,
            # which is a far worse outcome than a single failure.
            print(f"  [{issue_key}] FAILED: {type(error).__name__}: {error}")
            traceback.print_exc()
            code = 1
        exit_codes.append(code)

    print("\nGenerating dashboard...")
    # The run record is what the dashboard reads back: a number, how long it took, and
    # the verdicts to diff the next one against. Written even on an aborted scan, so a
    # login expiry shows up as a short run rather than a gap in the history.
    generate_dashboard(mark_scan_complete(
        started_at=_scan_started,
        duration_seconds=time.monotonic() - _t_scan,
        scanned=len(exit_codes),
    ))

    passed = exit_codes.count(0)
    failed = len(exit_codes) - passed

    print()
    print("=" * 72)
    print(f"SCAN COMPLETE: {passed} passed / {failed} pending or failed "
          f"in {time.monotonic() - _t_scan:.1f}s")
    print("Dashboard:", DASHBOARD_PATH)
    print("=" * 72)

    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "TraceGuard data validation agent. "
            "Reads Jira tickets, generates validation queries, posts them for approval, "
            "and executes once APPROVED."
        )
    )
    parser.add_argument(
        "issue_key",
        nargs="?",
        help="Single Jira ticket key, e.g. DEMO-123",
    )
    parser.add_argument(
        "--jql",
        metavar="JQL",
        help=f'JQL to scan multiple tickets, e.g. "labels = {VALIDATION_LABEL}"',
    )
    parser.add_argument(
        "--board-id",
        type=int,
        metavar="ID",
        help=(
            "Jira board ID to restrict results to that board. "
            "Find it in the board URL: ?rapidView=ID."
        ),
    )

    args = parser.parse_args()

    if args.jql and args.issue_key:
        parser.error("Provide either an issue_key or --jql, not both.")
    if not args.jql and not args.issue_key:
        parser.error("Provide an issue_key or --jql.")

    if args.jql:
        return run_bulk(jql=args.jql, board_id=args.board_id)

    # Single ticket mode
    reader = JiraReader()
    ticket = reader.read_issue(args.issue_key)
    return run_agent_workflow(
        issue_key=args.issue_key,
        ticket_data=ticket,
        reader=reader,
    )


if __name__ == "__main__":
    raise SystemExit(main())
