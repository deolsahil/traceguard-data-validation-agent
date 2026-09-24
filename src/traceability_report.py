import argparse
import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
VALIDATION_LABEL = os.getenv("VALIDATION_LABEL", "validation-agent")
REPORT_PATH = OUTPUT_DIR / "traceability-report.json"
DASHBOARD_PATH = OUTPUT_DIR / "traceability-dashboard.html"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, required: bool = False) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Required file not found: {path}")
        return {}

    return json.loads(path.read_text(encoding="utf-8"))


def derive_prod_status(ticket: dict[str, Any]) -> str:
    """
    Derive production status from Jira fix-version release metadata.
    """

    versions = ticket.get("fix_version_details", [])

    if not versions:
        return "unknown"

    released_versions = [
        version
        for version in versions
        if version.get("released") is True
        and version.get("archived") is not True
    ]

    if released_versions:
        return "in_prod"

    return "not_in_prod"


def validation_query_found(validation_state: dict[str, Any]) -> bool:
    return validation_state.get("status") in {"VALIDATION_EXECUTED", "CONTRACT_VALID"}


def collect_environment_details(
    execution_result: dict[str, Any],
    query_review: dict[str, Any],
) -> list[dict[str, Any]]:
    if not execution_result and not query_review:
        return []

    status = (
        execution_result.get("status")
        or query_review.get("status")
        or "PENDING"
    )
    errors = execution_result.get("errors") or query_review.get("errors") or []

    return [
        {
            "validation": "DEV Validation",
            "environment": "DEV",
            "status": status,
            "validation_passed": execution_result.get("validation_passed"),
            "validation_details": execution_result.get("validation_details"),
            "job_id": execution_result.get("job_id"),
            "bytes_processed": execution_result.get("total_bytes_processed"),
            "bytes_processed_display": execution_result.get(
                "total_bytes_processed_display"
            ),
            "errors": errors,
        }
    ]


def derive_validation_status(
    query_found: bool,
    execution_result: dict[str, Any],
) -> str:
    if not query_found:
        return "not_run"

    if execution_result.get("mode") != "EXECUTE":
        if execution_result.get("status") in {
            "BLOCKED_UNSAFE_SQL",
            "DRY_RUN_FAILED",
            "INVALID_RESULT_SCHEMA",
            "EXECUTION_FAILED",
        }:
            return "query_error"
        return "not_run"

    exec_status = execution_result.get("status")
    if exec_status == "VALIDATION_PASSED":
        return "succeeded"
    if exec_status == "VALIDATION_FAILED":
        return "failed"
    return "query_error"


# What the details column should say when a ticket never got as far as running a query.
# Infrastructure problems get an instruction, because the reader can act on those.
DETAIL_FALLBACKS = {
    "credentials_unavailable": "Login expired before the ticket could be read — "
                               "run: gcloud auth application-default login",
    "object_broken": "The table or view cannot be read at all — no query against it can run",
    "metadata_unavailable": "The table's schema could not be read, so no query was written",
    "needs_clarification": "Waiting on a reply saying which table or view to check",
    "query_invalid": "A query was written but BigQuery rejected it",
    "missing_label": f"Skipped: the ticket is not labelled {VALIDATION_LABEL}",
}


def build_validation_details(
    environment_details: list[dict[str, Any]],
    fallback_status: str,
) -> str:
    if not environment_details:
        # Without an execution there is nothing to summarise, so the status itself is
        # the detail — but it must read as a sentence, not as the constant the code
        # stores. Anything without a specific line falls back to its display label.
        return DETAIL_FALLBACKS.get(fallback_status, humanize(fallback_status))

    parts = []

    for environment in environment_details:
        environment_name = (
            environment.get("environment") or "UNKNOWN"
        )
        status = environment.get("status") or "UNKNOWN"

        detail = f"{environment_name}: {status}"

        validation_message = environment.get(
            "validation_details"
        )

        if validation_message:
            detail += f" - {validation_message}"

        errors = environment.get("errors", [])

        if errors:
            detail += f" - {errors[0]}"

        parts.append(detail)

    return "; ".join(parts)


def build_ticket_record(issue_key: str) -> dict[str, Any]:
    issue_key = issue_key.upper()

    ticket = load_json(OUTPUT_DIR / f"{issue_key}.json", required=True)
    validation_state = load_json(OUTPUT_DIR / f"{issue_key}-validation-state.json")
    query_review = load_json(OUTPUT_DIR / f"{issue_key}-query-review.json")
    execution_result = load_json(OUTPUT_DIR / f"{issue_key}-execution-result.json")

    query_found = validation_query_found(validation_state)
    environment_details = collect_environment_details(execution_result, query_review)

    if validation_state.get("status") == "AWAITING_APPROVAL":
        validation_status = "awaiting_approval"
    elif validation_state.get("status") == "NEEDS_CLARIFICATION":
        validation_status = "needs_clarification"
    elif validation_state.get("status") == "METADATA_UNAVAILABLE":
        validation_status = "metadata_unavailable"
    elif validation_state.get("status") == "OBJECT_BROKEN":
        validation_status = "object_broken"
    elif validation_state.get("status") == "CREDENTIALS_UNAVAILABLE":
        # An infrastructure problem, not a verdict on the ticket. It belongs here and
        # on the run log, never as a comment on somebody's ticket.
        validation_status = "credentials_unavailable"
    elif validation_state.get("status") == "QUERY_INVALID":
        validation_status = "query_invalid"
    elif validation_state.get("status") == "MISSING_VALIDATION_LABEL":
        # Only reachable via single-ticket runs; bulk scans filter on the label, so
        # this never appears from a normal scan and gets no filter entry.
        validation_status = "missing_label"
    else:
        validation_status = derive_validation_status(
            query_found=query_found,
            execution_result=execution_result,
        )
    run_timestamp = execution_result.get("executed_at") or utc_now()

    jira_released = derive_prod_status(ticket) == "in_prod"
    bq_passed = validation_status == "succeeded"
    if jira_released and bq_passed:
        prod_status = "in_prod"
    elif jira_released:
        prod_status = "not_validated"
    else:
        prod_status = "not_in_prod"

    return {
        "jira_key": ticket.get("jira_key") or issue_key,
        "summary": ticket.get("summary"),
        "jira_status": ticket.get("status"),
        "prod_status": prod_status,
        "fix_versions": ticket.get("fix_versions", []),
        "validation_query_found": query_found,
        "validation_status": validation_status,
        "validation_details": build_validation_details(
            environment_details=environment_details,
            fallback_status=validation_status,
        ),
        "run_timestamp": run_timestamp,
        "labels": ticket.get("labels", []),
        "environment_details": environment_details,
    }


def load_report() -> dict[str, Any]:
    if not REPORT_PATH.exists():
        return {
            "generated_at": None,
            "tickets": [],
        }

    report = load_json(REPORT_PATH)

    if not isinstance(report.get("tickets"), list):
        report["tickets"] = []

    return report


# A run is worth keeping as an object, not just a timestamp: it has a number, a length,
# an outcome, and — the useful part — a previous one to be compared against. Twenty is
# enough to see a trend and small enough that the report stays a file you can open.
_RUN_HISTORY = 20

# Verdicts that mean the agent reached an answer. A move between any two of these is a
# change worth reporting; anything involving a pending or infrastructure state is noise,
# because "not run yet" becoming "passed" is the system working, not news.
_SETTLED = {"succeeded", "failed", "query_error"}


def _verdicts(report: dict[str, Any]) -> dict[str, str]:
    return {
        t["jira_key"]: t.get("validation_status")
        for t in report.get("tickets", [])
        if t.get("jira_key")
    }


def _flips(before: dict[str, str], after: dict[str, str]) -> list[dict[str, str]]:
    """
    Verdicts that reversed between two snapshots.

    Only moves between settled verdicts count. "Not run yet" becoming "passed" is the
    system working, not news, and reporting it would bury the one line that matters.
    """
    changes = []
    for key, now in after.items():
        was = before.get(key)
        if was and was != now and was in _SETTLED and now in _SETTLED:
            changes.append({"jira_key": key, "from": was, "to": now})

    # A ticket that stopped matching the scan is a change too, and a consequential one:
    # pruning deletes its files, so its history is gone. Comparing only tickets present
    # in both snapshots missed it entirely and reported "no changes" for a run that had
    # dropped one. Arrivals are deliberately not reported — tickets appear on every
    # normal run, so that would be noise rather than news.
    for key, was in before.items():
        if key not in after and was in _SETTLED:
            changes.append({"jira_key": key, "from": was, "to": "removed"})

    return sorted(changes, key=lambda c: c["jira_key"])


def changed_since_previous(report: dict[str, Any]) -> list[dict[str, str]]:
    """What moved in the most recent run. Empty on a first run, which is honest."""
    runs = report.get("runs") or []
    return list(runs[-1].get("changes") or []) if runs else []


def mark_scan_complete(started_at: str | None = None,
                       duration_seconds: float | None = None,
                       scanned: int | None = None) -> dict[str, Any]:
    """
    Record that a scan finished, and return the report to render.

    generated_at was only ever set by upsert_ticket, i.e. once per ticket written — so
    the footer reading "Report refreshed at" actually meant "when a ticket last changed".
    A scan that matched nothing, or whose tickets were all already done, left a stale
    date on the dashboard and looked like the agent had not run.

    The run record appended here is what lets the dashboard say "run #128, 21 seconds,
    2 verdicts changed" instead of printing a timestamp and leaving the reader to work
    out whether anything happened.
    """
    report = load_report()
    report["generated_at"] = utc_now()

    tickets = report.get("tickets", [])
    counts: dict[str, int] = {}
    for ticket in tickets:
        status = ticket.get("validation_status") or "unknown"
        counts[status] = counts.get(status, 0) + 1

    runs = report.get("runs") or []
    verdicts = _verdicts(report)
    previous = runs[-1].get("verdicts") if runs else None

    runs.append({
        "number": (runs[-1]["number"] + 1) if runs else 1,
        "started_at": started_at or report["generated_at"],
        "finished_at": report["generated_at"],
        "duration_seconds": round(duration_seconds, 1) if duration_seconds is not None else None,
        "scanned": scanned if scanned is not None else len(tickets),
        "counts": counts,
        # What moved. This is what history is for — a run is remembered by what it
        # changed, not by a copy of everything that was true at the time.
        "changes": _flips(previous, verdicts) if previous else [],
        "verdicts": verdicts,
    })

    # Only the newest snapshot earns its keep: it is the baseline the next run diffs
    # against. Keeping one per run stored the same ticket list twenty times over and
    # made the file grow with ticket count instead of with history.
    for old_run in runs[:-1]:
        old_run.pop("verdicts", None)

    report["runs"] = runs[-_RUN_HISTORY:]

    save_report(report)
    return report


def upsert_ticket(
    report: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    records_by_key = {
        str(ticket.get("jira_key")).upper(): ticket
        for ticket in report.get("tickets", [])
        if ticket.get("jira_key")
    }

    records_by_key[record["jira_key"].upper()] = record

    report["tickets"] = sorted(
        records_by_key.values(),
        key=lambda ticket: ticket.get("jira_key", ""),
    )

    report["generated_at"] = utc_now()

    return report


def save_report(report: dict[str, Any]) -> None:
    REPORT_PATH.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def format_utc(value: Any) -> str:
    """
    Render an ISO timestamp as readable UTC.

    The stored value is already UTC, but '2026-08-06T10:54:47.797831+00:00' reads
    like a machine field and leaves people guessing at the timezone.
    """
    if not value:
        return "—"
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc)
    return stamp.strftime("%b %d, %Y at %H:%M UTC")


def _run_line(report: dict[str, Any]) -> str:
    """
    The summary line, as a link only when there is something behind it.

    Before the first run there is no drawer to open, so rendering it as a link — dotted
    underline, pointer cursor — promises a panel that does not exist.
    """
    text = _run_summary(report)
    if not (report.get("runs") or []):
        return f'<span id="st-run" class="idle">{text}</span>'
    return f'<a id="st-run" href="#" role="button">{text}</a>'


def _run_summary(report: dict[str, Any]) -> str:
    """
    One line, chosen by what is true — never by a timer.

    Text that rotates on its own cannot be re-read, cannot be predicted, and is
    announced afresh by a screen reader every time it swaps. So the line changes only
    when the underlying fact changes, and it leads with the flips, because a verdict
    that reversed is the one thing here worth interrupting somebody for.
    """
    runs = report.get("runs") or []
    if not runs:
        # Two lines rather than one short phrase: it balances under the button, and an
        # empty dashboard is the one moment where saying what to do next is worth more
        # than saying what happened.
        return "No runs yet · tap the bot to start"
    last = runs[-1]
    changes = changed_since_previous(report)
    if changes:
        n = len(changes)
        gone = sum(1 for c in changes if c.get("to") == "removed")
        what = "change" if n != gone else ("ticket left scope" if gone == 1 else "tickets left scope")
        if n != gone:
            what = "change" if n == 1 else "changes"
        return f"&#8593; {n} {what} since run #{runs[-2]['number']}"
    if len(runs) > 1:
        return f"No changes since run #{runs[-2]['number']}"
    # "1 tickets" is a bug, and "1 ticket" alone does not say what happened to it. The
    # count is what the scan looked at, which is not always what reached the table — a
    # ticket that errors is scanned and never lands.
    scanned = last.get("scanned", 0)
    if not scanned:
        return f"Run #{last['number']} · no tickets matched"
    return f"Run #{last['number']} · {scanned} ticket{'' if scanned == 1 else 's'} scanned"


_RUN_LIST = 8   # how many runs the drawer offers; older ones stay in the file


def _run_panel(run: dict[str, Any], latest: bool) -> str:
    """One run's detail. Older runs kept their changes, so they still read fully."""
    counts = run.get("counts") or {}
    duration = run.get("duration_seconds")
    rows = [
        ("Started", format_utc(run.get("started_at"))),
        ("Duration", f"{duration:g}s" if duration is not None else "—"),
        ("Tickets scanned", str(run.get("scanned", 0))),
        ("Succeeded", str(counts.get("succeeded", 0))),
        ("Failed", str(counts.get("failed", 0))),
        ("Query errors", str(counts.get("query_error", 0))),
        ("Awaiting approval", str(counts.get("awaiting_approval", 0))),
    ]
    body = "".join(
        f'<div class="rd-row"><span>{escape(k)}</span><b>{escape(v)}</b></div>' for k, v in rows
    )
    changes = run.get("changes") or []
    if changes:
        items = "".join(
            f'<li><code>{escape(c["jira_key"])}</code> '
            f'{escape(humanize(c["from"]))} &#8594; <b>{escape(humanize(c["to"]))}</b></li>'
            for c in changes[:6]
        )
        more = f'<li class="rd-more">and {len(changes) - 6} more</li>' if len(changes) > 6 else ""
        # A removed ticket has no row to show, so the link only appears when at least
        # one of the changes is still in the table.
        still_here = any(c.get("to") != "removed" for c in changes)
        action = ('<a href="#" class="rd-show-changed">Show these tickets &#8594;</a>'
                  if latest and still_here else "")
        body += (f'<div class="rd-changes"><div class="rd-changes-hd">Changed this run</div>'
                 f'<ul>{items}{more}</ul>{action}</div>')
    else:
        body += '<div class="rd-changes"><div class="rd-none">No verdicts changed</div></div>'
    return (f'<div class="rd-panel" data-run="{run["number"]}"{"" if latest else " hidden"}>'
            f'<div class="rd-hd">Run #{run["number"]}</div>{body}</div>')


def _run_drawer(report: dict[str, Any]) -> str:
    """
    The run as an object, plus the runs before it.

    Without the list, run #2 is unreachable the moment you are on #20 — the record is
    in the file and nothing can read it. Each entry carries the one fact worth scanning
    a history for: whether anything moved.
    """
    runs = report.get("runs") or []
    if not runs:
        return ""   # no drawer at all, rather than an empty box waiting to be styled

    recent = runs[-_RUN_LIST:][::-1]
    panels = "".join(_run_panel(r, latest=(r is recent[0])) for r in recent)
    items = "".join(
        f'<button class="rd-item{" active" if r is recent[0] else ""}" data-run="{r["number"]}">'
        f'<span>#{r["number"]}</span>'
        f'<span class="rd-item-note">'
        f'{(str(len(r.get("changes") or [])) + " changed") if r.get("changes") else "no changes"}'
        f'</span></button>'
        for r in recent
    )
    history = (f'<div class="rd-history"><div class="rd-history-hd">Recent runs</div>'
               f'<div class="rd-list">{items}</div>'
               f'<button id="rd-clear" type="button">Clear run history</button>'
               f'</div>') if len(recent) > 1 else ""
    return f'<div id="run-drawer" hidden>{panels}{history}</div>'


def escape(value: Any) -> str:
    if value is None or value == "":
        return "—"

    return html.escape(str(value))


def status_class(status: str) -> str:
    return {
        "succeeded": "success",
        "failed": "danger",
        "query_error": "danger",
        "not_run": "neutral",
        "in_prod": "success",
        "not_in_prod": "warning",
        "not_validated": "warning",
        "awaiting_approval": "warning",
        "needs_clarification": "warning",
        "metadata_unavailable": "danger",
        "credentials_unavailable": "danger",
        "object_broken": "danger",
        "query_invalid": "danger",
        "missing_label": "neutral",
        "unknown": "neutral",
    }.get(str(status).lower(), "neutral")


# Internal values are snake_case constants; these are what people should read.
# Display only — filtering and the stored data still use the raw values.
STATUS_LABELS = {
    "succeeded": "Passed",
    "failed": "Failed",
    "query_error": "Query error",
    "not_run": "Not run",
    "awaiting_approval": "Awaiting approval",
    "needs_clarification": "Needs clarification",
    "metadata_unavailable": "Cannot read table",
    "credentials_unavailable": "Login expired",
    "object_broken": "Object is broken",
    "removed": "no longer in scope",
    "query_invalid": "Could not build query",
    "missing_label": "Missing label",
    "in_prod": "In prod",
    "not_in_prod": "Not in prod",
    "not_validated": "Not validated",
    "unknown": "Unknown",
}


def humanize(value: Any) -> str:
    """Turn an internal status constant into something readable."""
    raw = str(value or "unknown")
    return STATUS_LABELS.get(raw, raw.replace("_", " ").capitalize())


def status_badge(status: Any) -> str:
    normalized = str(status or "unknown")

    return (
        f'<span class="badge {status_class(normalized)}">'
        f"{escape(humanize(normalized))}"
        "</span>"
    )


def render_environment_details(
    ticket: dict[str, Any],
) -> str:
    details = ticket.get("environment_details", [])

    panel_header = (
        "<div class='panel-hd'>"
        "<span class='panel-title'>Environment Evidence</span>"
        "<button class='panel-close' "
        "onclick=\"this.closest('details').removeAttribute('open')\">&#x2715;</button>"
        "</div>"
    )

    if not details:
        return (
            panel_header
            + "<p class='empty-evidence'>No environment evidence available.</p>"
        )

    cards = []

    for detail in details:
        errors = detail.get("errors", [])
        errors_html = ""

        if errors:
            items = "".join(
                f"<li>{escape(e)}</li>" for e in errors
            )
            errors_html = f"<ul class='errors'>{items}</ul>"

        cards.append(
            f"""<div class="evidence-card">
  <div class="evidence-heading">
    <strong>{escape(detail.get("validation"))}</strong>
    {status_badge(detail.get("status"))}
  </div>
  <dl>
    <dt>Environment</dt><dd>{escape(detail.get("environment"))}</dd>
    <dt>Details</dt><dd>{escape(detail.get("validation_details"))}</dd>
    <dt>Job ID</dt><dd class="monospace">{escape(detail.get("job_id"))}</dd>
    <dt>Bytes</dt><dd>{escape(detail.get("bytes_processed_display"))}</dd>
  </dl>
  {errors_html}
</div>"""
        )

    return (
        panel_header
        + "<div class='evidence-grid'>"
        + "".join(cards)
        + "</div>"
    )


def generate_dashboard(report: dict[str, Any]) -> None:
    load_dotenv()
    jira_base_url = os.getenv("JIRA_BASE_URL", "").strip().rstrip("/")

    tickets = report.get("tickets", [])

    succeeded = sum(
        t.get("validation_status") == "succeeded"
        for t in tickets
    )
    failed = sum(
        t.get("validation_status") == "failed"
        for t in tickets
    )
    query_errors = sum(
        t.get("validation_status") == "query_error"
        for t in tickets
    )
    not_run = sum(
        t.get("validation_status") == "not_run"
        for t in tickets
    )

    rows = []

    for ticket in tickets:
        row_text = " ".join([
            str(ticket.get("jira_key", "")),
            str(ticket.get("summary", "")),
            str(ticket.get("prod_status", "")),
            str(ticket.get("validation_status", "")),
            humanize(ticket.get("prod_status")),
            humanize(ticket.get("validation_status")),
        ]).lower()

        qf_badge = (
            '<span class="badge success">&#x2713; Found</span>'
            if ticket.get("validation_query_found")
            else '<span class="badge neutral">—</span>'
        )

        ts = escape(ticket.get("run_timestamp") or "")

        jira_key_val = escape(ticket.get("jira_key") or "")
        jira_key_cell = (
            f'<a href="{escape(jira_base_url)}/browse/{jira_key_val}"'
            f' target="_blank" rel="noopener">{jira_key_val}</a>'
            if jira_base_url
            else jira_key_val
        )

        rows.append(
            f'<tr class="ticket-row"'
            f' data-search="{escape(row_text)}"'
            f' data-status="{escape(ticket.get("validation_status"))}">'
            f'<td class="col-key key-cell">{jira_key_cell}</td>'
            f'<td class="col-sum">{escape(ticket.get("summary"))}</td>'
            f'<td class="col-prod">{status_badge(ticket.get("prod_status"))}</td>'
            f'<td class="col-qf">{qf_badge}</td>'
            f'<td class="col-vs">{status_badge(ticket.get("validation_status"))}</td>'
            f'<td class="col-vd"><div class="details-text">{escape(ticket.get("validation_details"))}</div></td>'
            f'<td class="col-ts ts-cell" data-ts="{ts}">{ts}</td>'
            f'<td class="col-evi"><details><summary>View</summary>'
            f'<div class="evidence-panel">{render_environment_details(ticket)}</div>'
            f'</details></td>'
            f'</tr>'
        )

    empty_state = (
        '<tr><td colspan="8" class="empty-state-cell">'
        '<h3>No tickets yet</h3>'
        f'<p>Add the <strong>{VALIDATION_LABEL}</strong> label to a Jira ticket, then click Run Agent to get started.</p>'
        '<button onclick="openGuide(\'samples\')">View sample tickets &rarr;</button>'
        '</td></tr>'
        if not rows
        else ""
    )

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TraceGuard Data Validation</title>
<!-- Inline SVG so there is no file to serve: the dashboard is opened straight from
     disk as often as through server.py, and a linked icon would 404 in one of them.
     A static robot head — reads as "agent" at 16px, where anything more literal turns
     to mush. Eyes are deliberately large: at tab size they are what makes it a face. -->
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%230052cc'/><line x1='16' y1='4' x2='16' y2='9' stroke='%23fff' stroke-width='2.4' stroke-linecap='round'/><circle cx='16' cy='4' r='1.9' fill='%23fff'/><rect x='6.5' y='9' width='19' height='16' rx='4.5' fill='%23fff'/><circle cx='12' cy='15.5' r='2.3' fill='%230052cc'/><circle cx='20' cy='15.5' r='2.3' fill='%230052cc'/><line x1='12.5' y1='20.5' x2='19.5' y2='20.5' stroke='%230052cc' stroke-width='2.2' stroke-linecap='round'/></svg>">
<style>
:root {{
  --font: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --text: #172b4d;
  --muted: #6b778c;
  --border: #dfe1e6;
  --bg: #f0f2f5;
  --white: #fff;
  --blue: #0052cc;
  --r: 10px;
  --shadow: 0 1px 3px rgba(9,30,66,.1), 0 0 1px rgba(9,30,66,.07);
}}
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: var(--font); color: var(--text); background: var(--bg); font-size: 14px; line-height: 1.5; }}
main {{ width: min(1560px, calc(100% - 48px)); margin: 28px auto 80px; }}

/* ── Header ── */
header {{
  padding: 28px 32px 26px;
  border-radius: 14px;
  background: linear-gradient(135deg, #0b1e47 0%, #0052cc 65%, #2684ff 100%);
  color: #fff;
  margin-bottom: 22px;
  box-shadow: 0 6px 24px rgba(0,82,204,.28);
  display: flex; align-items: flex-start; justify-content: space-between; gap: 16px;
}}
.header-left {{ flex: 1; }}
.header-eyebrow {{ font-size: 11px; letter-spacing: .1em; text-transform: uppercase; opacity: .55; margin-bottom: 8px; }}
header h1 {{ font-size: 26px; font-weight: 700; letter-spacing: -.3px; margin-bottom: 4px; }}
header .meta {{ font-size: 12px; opacity: .6; }}
/* Fixed width on purpose. Sized to its contents, a long status line ("DEMO-123 ·
   Understanding what to validate…") stretched this column and dragged the button and
   everything under it leftwards, so the header moved while the agent worked. */
/* The column is the button's width, so its right edge lines up with the cards and the
   table below. Only the status text is allowed to be wider, and it overhangs by equal
   amounts either side so it stays centred on the button without moving anything. */
/* Wide enough for a status line, with everything anchored to its right edge — which is
   the header's padding edge, the same line the title starts from on the left. Centring
   the text on the narrower button instead pushed it past the header and out onto the
   page background, because the overhang was larger than the padding it had to live in. */
.header-actions {{
  display: flex; flex-direction: column; gap: 8px; align-items: flex-end;
  width: 236px; flex: none; padding-top: 4px;
  min-height: 40px;   /* the collapsed circle is the tallest state */
}}
/* Collapsed to a bot, expands to a pill on click. The column reserves the expanded
   width, so the morph never moves anything around it — the label still cannot resize
   the button either, which is what made the header twitch before. */
.btn-run {{
  height: 40px; width: 40px; box-sizing: border-box;
  display: inline-flex; align-items: center; justify-content: center; gap: 0;
  padding: 0; border-radius: 999px;
  transition: width .3s cubic-bezier(.2,.7,.3,1), border-radius .3s cubic-bezier(.2,.7,.3,1),
              gap .3s, padding .3s, background .18s, border-color .18s, transform .1s;
  overflow: hidden;
  font-size: 12px; white-space: nowrap; cursor: pointer;
  color: #fff; text-decoration: none;
  -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px);
  transition: background .18s, border-color .18s, transform .1s;
}}
/* Solid white, not glass: the live dot is a green pulse, and it needs an opaque
   backdrop to read. On frosted glass the blip washed out against the header. */
.btn-run {{
  background: #fff; border: 1px solid #fff; color: #0052cc;
  font-weight: 700;
  box-shadow: 0 2px 10px rgba(0,0,0,.14);
}}
.btn-run:hover:not(:disabled) {{ background: #f4f7fd; border-color: #f4f7fd; }}
.btn-run:active:not(:disabled) {{ transform: translateY(1px); }}
.btn-run:disabled {{ opacity: .55; cursor: not-allowed; }}
/* The button owns the tooltip, so it has to sit above the status lines below it —
   without this the summary text painted straight through the tooltip. */
.btn-shell {{ position: relative; z-index: 60; display: inline-flex; }}
/* Presence, not progress: green means the server behind Run Agent is answering, so
   pressing it will do something. Grey means the page is open but nothing is listening —
   which otherwise looks identical right up until you click. */
/* The label has no width until the button opens, so it cannot force the circle wider. */
#btn-label {{
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  max-width: 0; opacity: 0; transition: max-width .3s cubic-bezier(.2,.7,.3,1), opacity .2s;
}}
.btn-run.open {{ width: 132px; border-radius: 8px; gap: 7px; padding: 0 12px; }}
.btn-run.open #btn-label {{ max-width: 88px; opacity: 1; }}

/* One slow sweep while the agent is working — enough to read as alive, not enough to
   pull the eye off the table. */
.btn-run.working::after {{
  content: ""; position: absolute; inset: 0;
  background: linear-gradient(100deg, transparent 20%, rgba(0,82,204,.13) 50%, transparent 80%);
  transform: translateX(-100%); animation: _sweep 2s ease-in-out infinite;
}}
@keyframes _sweep {{ to {{ transform: translateX(100%); }} }}

@media (prefers-reduced-motion: reduce) {{
  .btn-run, #btn-label {{ transition: none; }}
  .btn-run.working::after {{ animation: none; display: none; }}
}}
#live-pip {{
  position: absolute; top: -3px; right: -3px;
  width: 9px; height: 9px; border-radius: 50%;
  border: 2px solid #fff; box-sizing: content-box;
  background: #cbd5e1; transition: background .3s;
}}
#live-pip.online {{ background: #22c55e; animation: _blip 1.8s ease-out infinite; }}
@keyframes _blip {{
  0%   {{ box-shadow: 0 0 0 0 rgba(34,197,94,.7); }}
  70%  {{ box-shadow: 0 0 0 6px rgba(34,197,94,0); }}
  100% {{ box-shadow: 0 0 0 0 rgba(34,197,94,0); }}
}}
@media (prefers-reduced-motion: reduce) {{ #live-pip.online {{ animation: none; }} }}
/* Ours, not the browser's. A native title tooltip is drawn by the OS outside the page,
   so it happily hangs off the header and no CSS can pull it back. */
#pip-tip {{
  /* Below the whole button, not over it — at 16px it covered the label it belongs to. */
  position: absolute; top: 38px; right: 0; z-index: 60;
  display: none; width: max-content; max-width: 210px;
  padding: 5px 9px; border-radius: 6px;
  background: #0f172a; color: #fff;   /* solid: at 94% the status line showed through */
  font-size: 10.5px; font-weight: 500; line-height: 1.4; text-align: left;
  white-space: normal; pointer-events: none;
  box-shadow: 0 6px 18px rgba(15,23,42,.28);
}}
/* Only the indicator opens it. Triggering from the whole button meant the tooltip
   appeared every time you went to click Run Agent. */
#live-pip:hover #pip-tip {{ display: block; }}
/* The pip is 9px across; this gives it a finger-sized hover target without drawing
   anything bigger. */
#live-pip::before {{ content: ""; position: absolute; inset: -7px; border-radius: 50%; }}
.btn-run:focus-visible {{
  outline: 2px solid rgba(255,255,255,.9); outline-offset: 2px;
}}
/* The bot is the status light. One mark reads faster than an icon plus a dot, and it
   gives the button something with a bit of character in it. */
.dot {{ width: 19px; height: 19px; flex-shrink: 0; display: block; color: #0052cc; transition: color .3s; }}
.dot svg {{ width: 19px; height: 19px; display: block; overflow: visible; }}
/* White shell on a white button, so the outline carries the silhouette and the status
   colour rides on the outline, eyes and antenna instead of the head fill. */
.dot svg .head {{ fill: #fff; stroke: currentColor; stroke-width: 1.5; }}
.dot svg .ear {{ fill: #e4eaf4; stroke: currentColor; stroke-width: 1.2; }}
.dot svg .eye, .dot svg .ant {{ fill: currentColor; stroke: none; }}

/* Aimed by the pointer. The offset is set on a variable so the blink animation below
   can own `transform` on a separate group without the two overwriting each other. */
#bot-look {{
  transform: translate(var(--look-x, 0px), var(--look-y, 0px));
  transition: transform .18s ease-out;
}}
.blink {{ transform-box: fill-box; transform-origin: center; animation: _blink 5.2s infinite; }}
@keyframes _blink {{
  0%, 92%, 100% {{ transform: scaleY(1); }}
  95%           {{ transform: scaleY(.12); }}
}}
/* Working: quicker blinks — the "humming" state. */
.dot.live .blink {{ animation-duration: 2.2s; }}
.dot.live, .dot.done {{ color: #16a34a; }}
.dot.error {{ color: #ef4444; }}
@media (prefers-reduced-motion: reduce) {{
  .blink {{ animation: none; }}
  #bot-look {{ transition: none; transform: none; }}
}}
#run-status {{
  position: relative;   /* anchors the run drawer */
  font-size: 11px; text-align: right; margin-top: 4px;
  display: flex; flex-direction: column; gap: 2px; align-items: flex-end;
  align-self: stretch; min-height: 30px;
}}
#st-action, #st-run, #st-prog {{ max-width: 100%; }}
#st-action {{ line-height: 1.35; overflow-wrap: anywhere; }}
/* Same treatment as the progress line it sits beside — 11px, regular weight, muted
   white. Matching the header subtitle instead made it a size larger than its own
   neighbours, so it stood out from the group it belongs to. */
#st-run {{
  font-size: 11px; font-weight: 400; color: rgba(255,255,255,.45);
  text-decoration: none; cursor: pointer; transition: color .16s;
}}
#st-run:hover {{ color: rgba(255,255,255,.8); }}
/* Nothing to open yet: plain text, no affordance it cannot honour. */
#st-run.idle {{ color: rgba(255,255,255,.4); cursor: default; }}
#run-drawer {{
  position: absolute; right: 0; top: calc(100% + 8px); z-index: 40;
  width: 260px; padding: 12px 14px; text-align: left;
  background: #fff; color: var(--text); border: 1px solid var(--border);
  border-radius: 10px; box-shadow: 0 12px 34px rgba(16,26,40,.22);
}}
#run-drawer[hidden] {{ display: none; }}
.rd-hd {{ font-weight: 700; font-size: 13px; margin-bottom: 8px; }}
.rd-row {{
  display: flex; justify-content: space-between; gap: 12px;
  font-size: 12px; padding: 3px 0; color: var(--muted);
}}
.rd-row b {{ color: var(--text); font-weight: 600; }}
.rd-changes {{ border-top: 1px solid var(--border); margin-top: 9px; padding-top: 9px; }}
.rd-changes-hd {{ font-size: 11px; font-weight: 700; color: #b45309; margin-bottom: 5px; }}
.rd-changes ul {{ margin: 0 0 7px; padding-left: 14px; font-size: 11.5px; line-height: 1.6; }}
.rd-changes code {{ font-size: 11px; }}
.rd-more {{ list-style: none; margin-left: -14px; color: var(--muted); }}
.rd-show-changed {{ font-size: 11.5px; font-weight: 600; color: var(--blue); text-decoration: none; }}
.rd-show-changed:hover {{ text-decoration: underline; }}
.rd-none {{ font-size: 11.5px; color: var(--muted); }}
.rd-history {{ border-top: 1px solid var(--border); margin-top: 10px; padding-top: 9px; }}
.rd-history-hd {{
  font-size: 10px; font-weight: 700; letter-spacing: .07em; text-transform: uppercase;
  color: var(--muted); margin-bottom: 5px;
}}
.rd-list {{ display: flex; flex-direction: column; gap: 1px; max-height: 150px; overflow-y: auto; }}
.rd-item {{
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  width: 100%; border: none; background: none; cursor: pointer; text-align: left;
  padding: 5px 7px; border-radius: 5px; font-size: 11.5px; font-weight: 600;
  color: var(--text); font-family: inherit;
}}
.rd-item:hover {{ background: #eef3fb; }}
.rd-item.active {{ background: #eaf1ff; color: var(--blue); }}
.rd-item-note {{ font-weight: 500; color: var(--muted); }}
#rd-clear {{
  margin-top: 8px; width: 100%; padding: 5px; border: 1px solid var(--border);
  background: none; border-radius: 5px; cursor: pointer;
  font-family: inherit; font-size: 11px; font-weight: 600; color: var(--muted);
}}
#rd-clear:hover {{ border-color: #f0a5a5; color: #b42318; background: #fff5f5; }}
#st-prog   {{ color: rgba(255,255,255,.45); min-height: 14px; }}
#st-action {{
  color: rgba(255,255,255,.7); min-height: 14px;
  transition: color .25s, opacity .25s; opacity: 1;
}}
#st-action.retrying {{ color: #fbbf24; }}  /* amber for retry */
/* A run that has finished is still doing something for another moment — the page is
   about to reload. A turning spinner says that; the word "reloading" only reports it. */
.spin {{
  display: inline-block; width: 9px; height: 9px; margin-right: 6px; vertical-align: -1px;
  border: 1.5px solid rgba(255,255,255,.22); border-top-color: rgba(255,255,255,.85);
  border-radius: 50%; animation: _spin .7s linear infinite;
}}
@keyframes _spin {{ to {{ transform: rotate(360deg); }} }}
@media (prefers-reduced-motion: reduce) {{ .spin {{ animation: none; }} }}
#st-action.ok {{ color: #86efac; }}

/* ── Stat cards ── */
.summary-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}}
.summary-card {{
  background: var(--white);
  border: 1px solid var(--border);
  border-top: 3px solid #b3bac5;
  border-radius: var(--r);
  padding: 18px 20px 16px;
  box-shadow: var(--shadow);
}}
.c-total   {{ border-top-color: #0052cc; }}
.c-success {{ border-top-color: #00875a; }}
.c-danger  {{ border-top-color: #de350b; }}
.c-warning {{ border-top-color: #ff8b00; }}
.c-neutral {{ border-top-color: #b3bac5; }}
/* A zero carries no news — mute it so non-zero counts stand out. */
.summary-card.is-zero {{ border-top-color: #dfe1e6; box-shadow: none; background: #fafbfc; }}
.summary-card.is-zero .summary-value {{ color: #b3bac5 !important; }}
.summary-card.is-zero .summary-label {{ color: #a5adba; }}

.summary-value {{ font-size: 32px; font-weight: 700; line-height: 1; margin-bottom: 6px; }}
.c-total   .summary-value {{ color: #0052cc; }}
.c-success .summary-value {{ color: #00875a; }}
.c-danger  .summary-value {{ color: #de350b; }}
.c-warning .summary-value {{ color: #ff8b00; }}
.c-neutral .summary-value {{ color: #42526e; }}
.summary-label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; font-weight: 500; }}

/* ── Controls ── */
.controls {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; align-items: center; }}
input[type=search], select {{
  height: 40px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--white); color: var(--text); font: inherit; font-size: 13px;
  outline: none; transition: border-color .15s, box-shadow .15s; box-shadow: var(--shadow);
}}
input[type=search]:focus, select:focus {{
  border-color: var(--blue); box-shadow: 0 0 0 3px rgba(0,82,204,.15);
}}
input[type=search] {{ flex: 1; min-width: 260px; padding: 0 14px; }}
select {{
  width: 230px; padding: 0 40px 0 14px; appearance: none; -webkit-appearance: none; cursor: pointer;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='7' viewBox='0 0 12 7'%3E%3Cpath d='M1 1l5 5 5-5' fill='none' stroke='%23172b4d' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 14px center;
}}

/* ── Table ── */
.table-wrap {{
  background: var(--white); border: 1px solid var(--border);
  border-radius: var(--r); overflow-x: auto; box-shadow: var(--shadow);
}}
table {{ width: 100%; border-collapse: collapse; table-layout: fixed; min-width: 1100px; }}
th, td {{ padding: 11px 14px; border-bottom: 1px solid #ebecf0; text-align: left; vertical-align: middle; font-size: 13px; }}
th {{
  background: #f4f5f7; color: var(--muted); font-size: 10px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .06em; position: sticky; top: 0; z-index: 1;
  border-bottom: 2px solid var(--border);
}}
tbody tr:last-child td {{ border-bottom: none; }}
tbody tr:hover td {{ background: #f0f5ff; transition: background .1s; }}
.col-key {{ width: 110px; }}
.col-sum {{ width: 200px; }}
.col-prod {{ width: 108px; }}
.col-qf  {{ width: 90px; text-align: center; }}
.col-vs  {{ width: 135px; }}
.col-vd  {{ width: 365px; }}
.col-ts  {{ width: 175px; }}
.col-evi {{ width: 88px; }}
.key-cell {{ font-weight: 700; white-space: nowrap; color: var(--blue); }}
.key-cell a {{ color: inherit; text-decoration: none; }}
.key-cell a:hover {{ text-decoration: underline; }}
.details-text {{
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden; color: var(--muted); font-size: 12px; line-height: 1.45;
}}
.ts-cell {{ font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }}

/* ── Badges ── */
.badge {{ display: inline-flex; align-items: center; padding: 3px 9px; border-radius: 999px; font-size: 11px; font-weight: 700; white-space: nowrap; }}
.success {{ color: #006644; background: #dcfce7; }}
.danger  {{ color: #9d1c1c; background: #fee2e2; }}
.warning {{ color: #7c3a00; background: #fef3c7; }}
.neutral {{ color: #42526e; background: #e8edf3; }}

/* ── Evidence floating panel ── */
details {{ min-width: 0; }}
summary {{
  cursor: pointer; color: var(--blue); font-weight: 600; font-size: 12px;
  list-style: none; user-select: none; padding: 5px 10px; border-radius: 6px;
  border: 1px solid #b3cefd; background: #eaf1ff;
  display: inline-flex; align-items: center; white-space: nowrap;
  transition: background .12s;
}}
summary:hover {{ background: #c9dcff; }}
summary::-webkit-details-marker {{ display: none; }}
.evidence-panel {{
  position: fixed; right: 24px; top: 50%; transform: translateY(-50%);
  width: 420px; max-height: 80vh; overflow-y: auto;
  background: var(--white); border: 1px solid var(--border); border-radius: 14px;
  box-shadow: 0 20px 60px rgba(9,30,66,.22), 0 4px 14px rgba(9,30,66,.1);
  z-index: 300; padding: 20px;
}}
.panel-hd {{
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 14px; padding-bottom: 12px; border-bottom: 1px solid var(--border);
}}
.panel-title {{ font-weight: 700; font-size: 14px; }}
.panel-close {{
  background: none; border: none; cursor: pointer; color: var(--muted);
  font-size: 18px; line-height: 1; padding: 2px 4px; border-radius: 4px; transition: color .1s;
}}
.panel-close:hover {{ color: var(--text); background: #f4f5f7; }}
.evidence-grid {{ display: grid; gap: 10px; }}
.evidence-card {{ padding: 14px; border: 1px solid var(--border); border-radius: 8px; background: #fafbfc; }}
.evidence-heading {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; margin-bottom: 10px; }}
.evidence-heading strong {{ font-size: 12px; }}
dl {{ display: grid; grid-template-columns: 90px 1fr; gap: 3px 10px; }}
dt {{ color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .04em; padding-top: 1px; }}
dd {{ font-size: 12px; word-break: break-word; }}
.monospace {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }}
.errors {{ color: #9d1c1c; font-size: 12px; margin-top: 8px; padding-left: 14px; }}
.empty-evidence {{ color: var(--muted); font-size: 13px; }}

/* ── Backdrop ── */
#evi-bd {{
  display: none; position: fixed; inset: 0; z-index: 299;
  background: rgba(9,30,66,.3); backdrop-filter: blur(1px);
}}

/* ── Footer ── */
footer {{ margin-top: 20px; text-align: center; color: var(--muted); font-size: 12px; }}

/* ── Info button (ⓘ next to subtitle) ── */
.btn-info {{
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border: none; background: none; padding: 0;
  cursor: pointer; vertical-align: middle; margin-left: 6px;
  opacity: .55; transition: opacity .15s, transform .15s; flex-shrink: 0;
}}
.btn-info:hover {{ opacity: 1; transform: scale(1.1); }}

/* ── Guide modal ── */
.guide-backdrop {{
  display: none; position: fixed; inset: 0; z-index: 500;
  background: rgba(9,30,66,.5); backdrop-filter: blur(3px);
}}
.guide-backdrop.open {{ display: flex; align-items: center; justify-content: center; padding: 20px; }}
.guide-modal {{
  background: var(--white); border-radius: 16px;
  width: min(860px, 100%); max-height: 90vh;
  box-shadow: 0 32px 80px rgba(9,30,66,.32), 0 0 0 1px rgba(9,30,66,.06);
  display: flex; flex-direction: column; overflow: hidden;
}}
.guide-top {{
  display: flex; align-items: flex-start; justify-content: space-between;
  padding: 22px 24px 18px; border-bottom: 1px solid var(--border); flex-shrink: 0;
  background: linear-gradient(135deg,#0b1e47 0%,#0052cc 65%,#2684ff 100%);
  border-radius: 16px 16px 0 0;
}}
.guide-top-left h2 {{ font-size: 17px; font-weight: 700; color: #fff; margin-bottom: 3px; }}
.guide-top-left p {{ font-size: 12px; color: rgba(255,255,255,.6); }}
.guide-close {{
  background: rgba(255,255,255,.12); border: 1px solid rgba(255,255,255,.2);
  cursor: pointer; color: rgba(255,255,255,.8);
  font-size: 16px; line-height: 1; padding: 5px 8px; border-radius: 7px;
  transition: background .12s; flex-shrink: 0; margin-top: 2px;
}}
.guide-close:hover {{ background: rgba(255,255,255,.22); color: #fff; }}
.guide-layout {{ display: flex; flex: 1; overflow: hidden; min-height: 0; }}
.guide-nav {{
  width: 168px; flex-shrink: 0; border-right: 1px solid var(--border);
  padding: 16px 10px; display: flex; flex-direction: column; gap: 2px;
  overflow-y: auto; background: #fafbfc;
}}
.guide-nav-item {{
  display: block; padding: 7px 12px; border-radius: 7px; font-size: 12px;
  font-weight: 500; color: var(--muted); cursor: pointer; border: none;
  background: none; text-align: left; transition: background .12s, color .12s;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.guide-nav-item {{ text-decoration: none; display: block; }}
.guide-nav-item:hover {{ background: #eaf1ff; color: var(--blue); }}
.guide-nav-item.active {{ background: #eaf1ff; color: var(--blue); font-weight: 600; }}
.guide-nav-divider {{ height: 1px; background: var(--border); margin: 6px 8px; }}
.guide-content {{ flex: 1; overflow-y: auto; padding: 24px 28px; }}
.guide-panel {{ display: none; }}
/* display can't transition, so animate the incoming panel instead. Short and
   small on purpose — enough to read as a change of section, not a slideshow. */
.guide-panel.active {{ display: block; animation: guide-panel-in .19s cubic-bezier(.16,.8,.3,1); }}
@keyframes guide-panel-in {{
  from {{ opacity: 0; transform: translateY(7px); }}
  to   {{ opacity: 1; transform: none; }}
}}
@media (prefers-reduced-motion: reduce) {{
  .guide-panel.active {{ animation: none; }}
}}
.guide-panel h3 {{
  font-size: 13px; font-weight: 700; color: var(--text);
  margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid var(--border);
}}
.guide-flow {{
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 18px;
}}
.guide-flow-step {{
  background: #eaf1ff; color: #0052cc; border-radius: 20px;
  padding: 6px 14px; font-size: 12px; font-weight: 600; white-space: nowrap;
}}
.guide-flow-arrow {{ color: #c1c7d0; font-size: 16px; font-weight: 300; }}
.guide-cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
.guide-card {{
  border: 1px solid var(--border); border-radius: 9px; padding: 13px 14px;
  background: var(--white); transition: border-color .12s, box-shadow .12s;
}}
.guide-card:hover {{ border-color: #b3cefd; box-shadow: 0 2px 8px rgba(0,82,204,.07); }}
.guide-card-label {{
  font-size: 11px; font-weight: 700; color: var(--muted);
  text-transform: uppercase; letter-spacing: .05em; margin-bottom: 8px;
}}
.guide-card code {{
  display: block; background: #f4f5f7; border-radius: 6px; padding: 9px 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px;
  color: #172b4d; line-height: 1.65; white-space: pre-wrap; word-break: break-word;
  border: 1px solid #ebecf0;
}}
.guide-note {{
  font-size: 12.5px; color: var(--muted); line-height: 1.6; margin: 0 0 12px;
}}
.guide-note strong {{ color: #172b4d; }}
.guide-list {{
  margin: 0; padding-left: 18px; font-size: 12.5px; color: var(--muted); line-height: 1.75;
}}
.guide-list li {{ margin-bottom: 3px; }}
.guide-list code {{
  background: #f4f5f7; border: 1px solid #ebecf0; border-radius: 4px;
  padding: 1px 5px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px; color: #172b4d;
}}
.guide-approval-row {{
  display: flex; gap: 10px; margin-bottom: 10px; align-items: flex-start;
}}
.guide-approval-badge {{
  font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em;
  padding: 3px 8px; border-radius: 4px; white-space: nowrap; margin-top: 2px; flex-shrink: 0;
  /* Fixed width so every row's text starts at the same x, whatever the label length */
  width: 74px; text-align: center; box-sizing: border-box;
}}
.badge-do   {{ background: #dcfce7; color: #166534; }}
.badge-or   {{ background: #fef3c7; color: #92400e; }}
.badge-re   {{ background: #ede9fe; color: #5b21b6; }}
.badge-at   {{ background: #e0f2fe; color: #075985; }}
.badge-no   {{ background: #fee2e2; color: #991b1b; }}
.guide-approval-text {{ font-size: 13px; color: var(--text); line-height: 1.55; }}
.guide-approval-text code {{
  background: #f4f5f7; border: 1px solid #ebecf0; padding: 1px 6px;
  border-radius: 4px; font-family: ui-monospace,monospace; font-size: 11px;
}}
.guide-tips-list {{ list-style: none; display: flex; flex-direction: column; gap: 10px; }}
.guide-tips-list li {{
  display: flex; gap: 10px; align-items: flex-start;
  font-size: 13px; color: var(--text); line-height: 1.55;
}}
.guide-tip-icon {{
  font-size: 15px; flex-shrink: 0; margin-top: 1px;
}}
.guide-tips-list code {{
  background: #f4f5f7; border: 1px solid #ebecf0; padding: 1px 5px;
  border-radius: 4px; font-family: ui-monospace,monospace; font-size: 11px;
}}

/* ── Empty state ── */
.empty-state-cell {{ padding: 56px 24px !important; text-align: center; background: #fafbfc; }}
.empty-state-icon {{ font-size: 36px; margin-bottom: 12px; }}
.empty-state-cell h3 {{ font-size: 15px; font-weight: 600; color: var(--text); margin-bottom: 6px; }}
.empty-state-cell p {{ font-size: 13px; color: var(--muted); margin-bottom: 20px; max-width: 380px; margin-inline: auto; }}
.empty-state-cell button {{
  background: var(--blue); color: #fff; border: none; border-radius: 7px;
  padding: 9px 20px; font-size: 13px; font-weight: 600; cursor: pointer;
  transition: opacity .15s; display: inline-flex; align-items: center; gap: 6px;
}}
.empty-state-cell button:hover {{ opacity: .88; }}
</style>
</head>
<body>
<div id="evi-bd"></div>

<!-- Guide modal -->
<div class="guide-backdrop" id="guide-backdrop" onclick="if(event.target===this)closeGuide()">
  <div class="guide-modal" role="dialog" aria-modal="true" aria-labelledby="guide-title">

    <div class="guide-top">
      <div class="guide-top-left">
        <h2 id="guide-title">Ticket Guide</h2>
        <p>Everything you need to write a ticket the agent understands</p>
      </div>
      <button class="guide-close" onclick="closeGuide()" aria-label="Close">&#x2715;</button>
    </div>

    <div class="guide-layout">
      <nav class="guide-nav">
        <button class="guide-nav-item active" onclick="showPanel('how',this)">How it works</button>
        <button class="guide-nav-item" onclick="showPanel('samples',this)">Sample tickets</button>
        <div class="guide-nav-divider"></div>
        <button class="guide-nav-item" onclick="showPanel('approval',this)">Approval flow</button>
        <button class="guide-nav-item" onclick="showPanel('tips',this)">Tips</button>
        <div class="guide-nav-divider"></div>
        <a class="guide-nav-item" href="/architecture">Architecture &nbsp;&#8599;</a>
      </nav>

      <div class="guide-content">

        <!-- How it works -->
        <div class="guide-panel active" id="panel-how">
          <h3>How it works</h3>
          <div class="guide-flow">
            <span class="guide-flow-step">1. Create Jira ticket</span>
            <span class="guide-flow-arrow">&#8594;</span>
            <span class="guide-flow-step">2. Add label <em>{VALIDATION_LABEL}</em></span>
            <span class="guide-flow-arrow">&#8594;</span>
            <span class="guide-flow-step">3. Click Run Agent</span>
            <span class="guide-flow-arrow">&#8594;</span>
            <span class="guide-flow-step">4. Agent posts SQL query on ticket</span>
            <span class="guide-flow-arrow">&#8594;</span>
            <span class="guide-flow-step">5. Reply APPROVED on ticket</span>
            <span class="guide-flow-arrow">&#8594;</span>
            <span class="guide-flow-step">6. Agent executes &amp; posts results</span>
          </div>
          <p style="font-size:13px;color:var(--muted);line-height:1.6">
            The agent reads your ticket in plain English — you don't need to write SQL.
            Describe what changed and what you want verified. The agent generates the validation query,
            posts it to your Jira ticket, and waits for your approval before touching BigQuery.
            It also reads <strong>attached data models</strong> (CSV/Excel) and, for stored procedures,
            works out which table the SP writes to on its own.
          </p>
        </div>

        <!-- Sample tickets -->
        <div class="guide-panel" id="panel-samples">
          <h3>Sample tickets by change type</h3>
          <div class="guide-cards">
            <div class="guide-card">
              <div class="guide-card-label">Column Added</div>
              <code>We added a new column session_id to
the user_events table.

table_name: project.dataset.user_events
column_name: session_id</code>
            </div>
            <div class="guide-card">
              <div class="guide-card-label">Column Type Changed</div>
              <code>event_timestamp migrated from STRING
to TIMESTAMP.

table_name: project.dataset.tag_events
column_name: event_timestamp
data_type: TIMESTAMP</code>
            </div>
            <div class="guide-card">
              <div class="guide-card-label">New Table Created</div>
              <code>Created daily_active_users aggregation
table for the reporting pipeline.

table_name: project.dataset.daily_active_users</code>
            </div>
            <div class="guide-card">
              <div class="guide-card-label">View Created / Modified</div>
              <code>Updated v_tag_events to include two
new event types: page_load, scroll_depth.

view_name: project.dataset.v_tag_events</code>
            </div>
            <div class="guide-card">
              <div class="guide-card-label">Stored Procedure Changed</div>
              <code>Modified sp_refresh_product_catalog
to handle null category values.

sp_name: sp_refresh_product_catalog
dataset: catalog_dataset</code>
            </div>
            <div class="guide-card">
              <div class="guide-card-label">Table from Data Model 📎</div>
              <code>Create this table as per the attached
data model.

[attach customer_master.xlsx]
table_name: project.dataset.customer_master</code>
            </div>
            <div class="guide-card">
              <div class="guide-card-label">Free-form (anything goes)</div>
              <code>Added attribution_source. Confirm it's
non-null and rows where status='completed'
exist.

table_name: project.dataset.conversions
column_name: attribution_source</code>
            </div>
          </div>

          <h3 style="margin-top:22px">Attaching a data model</h3>
          <p class="guide-note">
            For tables with many columns, attach the schema as a spreadsheet instead of listing
            every column. The agent reads it and validates all columns in one query that scans
            <strong>0 bytes</strong>, no matter how many there are.
          </p>
          <div class="guide-cards">
            <div class="guide-card">
              <div class="guide-card-label">Sheet layout</div>
              <code>Column Name | Data Type | Description
customer_id | STRING    | Unique id
signup_date | DATE      | Signup date
is_active   | BOOL      | Active flag</code>
            </div>
            <div class="guide-card">
              <div class="guide-card-label">Accepted headers</div>
              <code>name: column_name, field_name,
      attribute_name, column, name

type: data_type, datatype,
      column_type, field_type, type</code>
            </div>
          </div>
          <ul class="guide-list" style="margin-top:12px">
            <li><code>.csv</code> <code>.tsv</code> <code>.txt</code> <code>.xlsx</code> <code>.xlsm</code> are read — PDFs and images are skipped.</li>
            <li>Title or blank rows above the header are fine — the first 20 rows are scanned for it.</li>
            <li>With a <strong>Data Type</strong> column the agent checks name <em>and</em> type; without one, existence only.</li>
            <li>If several files are attached, the first one that parses as a data model is used.</li>
          </ul>
        </div>

        <!-- Approval flow -->
        <div class="guide-panel" id="panel-approval">
          <h3>Approval flow</h3>
          <p class="guide-note">
            Questions answerable from table metadata alone &mdash; does a table or column exist,
            what type is it, how many columns &mdash; <strong>skip approval entirely</strong> and post
            the answer straight away, because no table rows are read. Everything below applies to
            queries that touch real data.
          </p>
          <div style="display:flex;flex-direction:column;gap:14px">
            <div class="guide-approval-row">
              <span class="guide-approval-badge badge-do">Approve</span>
              <span class="guide-approval-text">Comment <code>@agent APPROVED</code>. The agent executes the query on the next run. The <code>@agent</code> tag is required &mdash; a bare <code>APPROVED</code> is treated as a human sign-off and ignored, so an unrelated approval on the ticket can't run a query.</span>
            </div>
            <div class="guide-approval-row">
              <span class="guide-approval-badge badge-no">Reject</span>
              <span class="guide-approval-text">Comment <code>@agent REJECT</code> and say what's wrong &mdash; e.g. <code>@agent REJECT - should be 30 days, not 7</code>. The agent throws the query away, rewrites it using your reason, and reposts for approval.</span>
            </div>
            <div class="guide-approval-row">
              <span class="guide-approval-badge badge-or">Modify</span>
              <span class="guide-approval-text">Paste your corrected SQL in a <code>{{code:sql}}</code> block comment, then comment <code>@agent APPROVED</code>. The agent runs your version.</span>
            </div>
            
            <div class="guide-approval-row">
              <span class="guide-approval-badge badge-at">Ask</span>
              <span class="guide-approval-text">Comment <code>@agent</code> followed by what you want &mdash; e.g. <code>@agent also check product_name</code>. Works on a finished ticket too: it starts a fresh cycle. <code>@validation-agent</code> works the same way.</span>
            </div>
          </div>
        </div>

        <!-- Tips -->
        <div class="guide-panel" id="panel-tips">
          <h3>Tips for better results</h3>
          <ul class="guide-tips-list">
            <li><span class="guide-tip-icon">&#128172;</span><span>Write what you want validated in plain English — the agent reads natural language and generates SQL from your description.</span></li>
            <li><span class="guide-tip-icon">&#128073;</span><span>Use full table paths: <code>project.dataset.table</code>. Partial names can be ambiguous.</span></li>
            <li><span class="guide-tip-icon">&#128206;</span><span>Table with lots of columns? Attach the data model as a <code>.csv</code> or <code>.xlsx</code> instead of listing them. The agent reads the sheet and checks every column — and the data types if the sheet has a type column.</span></li>
            <li><span class="guide-tip-icon">&#9881;&#65039;</span><span>For stored procedures the agent reads the SP body and works out which table it writes to on its own. Only add <code>table_name:</code> if the SP uses <code>&lt;&lt;placeholder&gt;&gt;</code> variables it can't resolve.</span></li>
            <li><span class="guide-tip-icon">&#128260;</span><span>For type changes, include <code>data_type:</code> (e.g. <code>TIMESTAMP</code>, <code>INT64</code>). The agent verifies via <code>INFORMATION_SCHEMA</code> at zero bytes cost.</span></li>
            <li><span class="guide-tip-icon">&#9888;&#65039;</span><span>If a query fails to execute, the agent <strong>stops retrying that ticket</strong> rather than posting the same error on every scan. Other tickets carry on. Tag <code>@agent</code> with an instruction, <code>@agent REJECT</code>, or paste corrected SQL to restart it.</span></li>
            <li><span class="guide-tip-icon">&#128260;</span><span>If the table changes while a query is waiting for approval, the agent discards the approved query and regenerates — rather than running a check written for a schema that no longer exists.</span></li>
          </ul>
        </div>

      </div>
    </div>

  </div>
</div>

<main>
  <header>
    <div class="header-left">
      <div class="header-eyebrow">TraceGuard</div>
      <h1>E2E Validation Traceability</h1>
      <div class="meta">One shared report &middot; one row per Jira solution ticket
        <button class="btn-info" onclick="openGuide()" aria-label="Open ticket guide" title="Ticket guide">
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg">
            <circle cx="9" cy="9" r="7.5" stroke="white" stroke-width="1.5"/>
            <circle cx="9" cy="6.2" r="1.1" fill="white"/>
            <rect x="8.1" y="8.4" width="1.8" height="4.4" rx="0.9" fill="white"/>
          </svg>
        </button>
      </div>
    </div>
    <div class="header-actions">
      <div class="btn-shell">
      <button class="btn-run" id="btn-run" onclick="runAgent()" aria-label="Run Agent">
        <span class="dot" id="run-dot" aria-hidden="true"><svg viewBox="0 0 24 24"><rect class="ant" x="11.2" y="3.4" width="1.6" height="3.8" rx=".8"/><circle class="ant" cx="12" cy="2.7" r="1.9"/><rect class="ear" x="1.5" y="11.4" width="2.7" height="4.2" rx="1.35"/><rect class="ear" x="19.8" y="11.4" width="2.7" height="4.2" rx="1.35"/><rect class="head" x="3.4" y="6.2" width="17.2" height="14.6" rx="5.8"/><g id="bot-look"><g class="blink"><rect class="eye" x="8.3" y="11.3" width="2.7" height="4.2" rx="1.35"/><rect class="eye" x="13" y="11.3" width="2.7" height="4.2" rx="1.35"/></g></g></svg></span>
        <span id="btn-label">Run Agent</span>
      </button>
      <span id="live-pip" class="offline" aria-label="Checking the agent server"><span id="pip-tip">Checking the agent server…</span></span>
      </div>
<div id="run-status">
        {_run_line(report)}\n      {_run_drawer(report)}
        <span id="st-prog"></span>
        <span id="st-action"></span>
      </div>
    </div>
  </header>
  <script>
  function openGuide(panel) {{
    document.getElementById('guide-backdrop').classList.add('open');
    document.body.style.overflow = 'hidden';
    // Always open on a known panel. Without this the modal reopens wherever it was
    // last left, so someone who read Tips once never sees the intro again.
    showPanel(panel || 'how',
      document.querySelector('.guide-nav-item[onclick*="' + (panel || 'how') + '"]'));
  }}
  function closeGuide() {{
    document.getElementById('guide-backdrop').classList.remove('open');
    document.body.style.overflow = '';
  }}
  function showPanel(id, btn) {{
    document.querySelectorAll('.guide-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.guide-nav-item').forEach(b => b.classList.remove('active'));
    document.getElementById('panel-' + id).classList.add('active');
    if (btn) btn.classList.add('active');
    // Long panels leave the pane scrolled down; start each one from the top.
    const pane = document.querySelector('.guide-content');
    if (pane) pane.scrollTop = 0;
  }}
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') closeGuide();
  }});

  // Presence check. Polled rather than assumed: this dashboard is a file on disk and
  // stays perfectly readable when the server behind it has stopped or gone stale.
  // Eyes follow the pointer. Throttled to one update per frame — a naive mousemove
  // handler runs hundreds of times a second and this is decoration, not work.
  (function botEyes() {{
    var look = document.getElementById('bot-look');
    if (!look || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    var pending = false, mx = 0, my = 0;
    document.addEventListener('mousemove', function(e) {{
      mx = e.clientX; my = e.clientY;
      if (pending) return;
      pending = true;
      requestAnimationFrame(function() {{
        pending = false;
        var b = look.getBoundingClientRect();
        if (!b.width) return;
        var dx = mx - (b.left + b.width / 2), dy = my - (b.top + b.height / 2);
        var d = Math.hypot(dx, dy) || 1;
        // Capped travel: the pupils suggest a glance, they do not roam the face.
        var reach = Math.min(d, 260) / 260 * 1.6;
        look.style.setProperty('--look-x', (dx / d * reach).toFixed(2) + 'px');
        look.style.setProperty('--look-y', (dy / d * reach).toFixed(2) + 'px');
      }});
    }}, {{ passive: true }});
  }})();

  const agentOrigin = window.location.protocol === 'file:'
    ? 'http://localhost:8765'
    : window.location.origin;

  function pingAgent() {{
    const pip = document.getElementById('live-pip');
    if (!pip) return;
    fetch(agentOrigin + '/health', {{ cache: 'no-store' }})
      .then(function(res) {{ return res.ok ? res.json() : Promise.reject(); }})
      .then(function(info) {{
        pip.className = 'online';
        // How long it has been up, not when it started. A server that has been running
        // for days is serving whatever code it imported on day one — which is exactly
        // the trap that made several fixes look like they had not worked.
        var since = info.started_at ? (Date.now() - new Date(info.started_at).getTime()) / 1000 : null;
        var age = 'running';
        if (since !== null) {{
          if (since < 90)          age = 'started just now';
          else if (since < 3600)   age = 'up ' + Math.floor(since / 60) + ' min';
          else if (since < 86400)  age = 'up ' + Math.floor(since / 3600) + 'h';
          else {{
            var days = Math.floor(since / 86400);
            age = 'up ' + days + ' day' + (days === 1 ? '' : 's')
                + ' — restart it to pick up code changes';
          }}
        }}
        var tip = document.getElementById('pip-tip');
        if (tip) tip.textContent = 'Agent server is running · ' + age;
      }})
      .catch(function() {{
        pip.className = 'offline';
        var tipOff = document.getElementById('pip-tip');
        if (tipOff) tipOff.textContent = 'Agent server is not responding — start it with: python src/server.py';
      }});
  }}
  pingAgent();
  setInterval(pingAgent, 20000);

  function runAgent() {{
    const btn    = document.getElementById('btn-run');
    const dot    = document.getElementById('run-dot');
    const label  = document.getElementById('btn-label');
    const prog   = document.getElementById('st-prog');
    const action = document.getElementById('st-action');

    if (btn.disabled) return;

    const runLine = document.getElementById('st-run');
    const drawer  = document.getElementById('run-drawer');
    // The summary describes the *previous* run; leaving it up beside live progress
    // shows two different answers to "what is happening" at once.
    if (runLine) runLine.style.display = 'none';
    if (drawer)  drawer.hidden = true;
    const startedAt = Date.now();

    btn.classList.add('open', 'working');
    btn.disabled       = true;
    dot.className      = 'dot live';
    label.textContent  = 'Scanning…';
    prog.textContent   = '';
    action.textContent = '';
    action.className   = '';

    function resetBtn() {{
      if (runLine) runLine.style.display = '';   // run over — the summary comes back
      btn.disabled      = false;
      dot.className     = 'dot';
      label.textContent = 'Run Agent';
    }}

    // A step can sit silent for a while — an LLM call, or the dry-run correction
    // loop. A frozen label reads as a hung app, so show the step's elapsed time
    // once it's slow enough to notice, and say so plainly when it drags.
    let actionBase = '', actionSince = 0;
    const tick = setInterval(renderAction, 1000);

    function renderAction() {{
      if (!actionBase) {{ action.textContent = ''; return; }}
      const secs = Math.floor((Date.now() - actionSince) / 1000);
      let suffix = '';
      if (secs >= 25)     suffix = ' · still working, ' + secs + 's';
      else if (secs >= 5) suffix = ' · ' + secs + 's';
      action.textContent = actionBase + suffix;
    }}

    // Short, plain words for the button. They change because the stage changed, not
    // on a timer — the reader can trust that a new word means new work.
    function setStage(word) {{
      if (word) label.textContent = word;
    }}

    function setAction(msg, retrying) {{
      action.className = retrying ? 'retrying' : '';
      actionBase = (currentKey ? currentKey + ' · ' : '') + msg;
      actionSince = Date.now();
      renderAction();
    }}

    let sawError = null;
    let currentKey = null;
    const es = new EventSource('http://localhost:8765/stream');

    es.onmessage = function(e) {{
      const line = JSON.parse(e.data);

      if (line === '__done__') {{
        es.close();
        clearInterval(tick);
        resetBtn();
        prog.textContent = '';
        if (sawError) {{
          // The summary describes an earlier, different run — a failed scan never
          // records one. Beside a live error it reads as this run's outcome, and
          // "no tickets matched" flatly contradicts "could not reach Jira".
          if (runLine) runLine.style.display = 'none';
          dot.className      = 'dot error';
          label.textContent  = 'Run failed';
          actionBase         = '';
          action.className   = 'retrying';
          action.textContent = sawError;
          prog.textContent   = 'Dashboard left unchanged';
          return;   // no reload: nothing was updated, and the message should stay
        }}
        dot.className      = 'dot done';
        label.textContent  = 'Done';
        actionBase         = '';
        action.className   = 'ok';
        // The receipt the reader wants the moment a run ends: what it covered and how
        // long it took. Both are known here without persisting anything.
        const secs = Math.max(1, Math.round((Date.now() - startedAt) / 1000));
        const seen = (prog.textContent.match(/^(\\d+)/) || [])[1];
        action.textContent = '\u2713 ' + (seen ? seen + ' tickets checked' : 'Scan complete')
                           + ' in ' + secs + 's';
        prog.textContent   = '';
        setTimeout(() => window.location.reload(), 2600);
        return;
      }}
      // [N/M] progress counter
      const pm = line.match(/^\\[(\\d+)\\/(\\d+)\\]/);
      if (pm) prog.textContent = pm[1] + ' / ' + pm[2] + ' tickets';

      // The ticket key belongs in the detail line, not on the button — the button
      // should say what the agent is doing, which is the part that keeps changing.
      const tm = line.match(/\\[([A-Z]+-\\d+)\\]/);
      if (tm) currentKey = tm[1];

      // Map log output to friendly action descriptions
      // A hard failure must not be reported as a finished run. Without this, a
      // blocked Jira request showed "Connecting to Jira…" and then reported a clean finish.
      if (line.indexOf('[ERROR]') !== -1) {{
        sawError = line.replace(/.*\\[ERROR\\]\\s*/, '').slice(0, 160);
        dot.className = 'dot error';
        setAction(sawError, true);
        return;
      }}
      if      (line.includes('Searching Jira'))                    (setStage('Waking up…'), setAction('Connecting to Jira…'));
      else if (/Found \\d+ ticket/.test(line))                      setAction('Found ' + (line.match(/Found (\\d+)/)||[])[1] + ' tickets to scan');
      else if (line.includes('Reading ticket description'))        (setStage('Reading…'), setAction('Reading ticket description…'));
      else if (/Reading (\\d+) comment/.test(line))                  setAction('Reading ' + (line.match(/Reading (\\d+) comment/)||[])[1] + ' comment(s)…');
      else if (line.includes('Parsing intent'))                    (setStage('Thinking…'), setAction('Understanding what to validate…'));
      else if (line.includes('Resolving SP output table'))          setAction('Resolving stored procedure output table…');
      else if (/Reading (\\d+) attachment/.test(line))                setAction('Reading ' + (line.match(/Reading (\\d+) attachment/)||[])[1] + ' attachment(s)…');
      else if (/Data model: (\\d+) column/.test(line))                setAction('Data model: ' + (line.match(/Data model: (\\d+) column/)||[])[1] + ' columns found');
      else if (line.includes('Reading table schema'))               (setStage('Inspecting…'), setAction('Reading BigQuery table schema…'));
      else if (/Table has (\\d+) column/.test(line))                  setAction('Schema: ' + (line.match(/Table has (\\d+) column/)||[])[1] + ' columns');
      else if (line.includes('Checking partition'))                 setAction('Fetching BigQuery table metadata…');
      else if (line.includes('Generating query'))                   (setStage('Generating…'), setAction('Generating validation SQL…'));
      else if (line.includes('APPROVED — executing') ||
               line.includes('APPROVED - executing') ||
               line.includes('APPROVED — executing'))              (setStage('Running…'), setAction('Running query in BigQuery…'));
      else if (/Retrying \\((\\d+)\\/(\\d+)\\)/.test(line)) {{
        const rm = line.match(/Retrying \\((\\d+)\\/(\\d+)\\)/);
        setAction('↻ SQL error — asking AI to fix (attempt ' + rm[1] + ' of ' + rm[2] + ')', true);
      }}
      else if (line.includes('VALIDATION_PASSED'))                  setAction('✓ Validation passed');
      else if (line.includes('EXECUTION_FAILED'))                   setAction('✕ Execution failed — will retry next scan');
      else if (line.includes('VALIDATION_FAILED'))                  setAction('✕ Validation check failed');
      else if (line.includes('State: BLOCKED'))                     setAction('Previous run failed — waiting for a fix, skipping');
      else if (line.includes('State: EXECUTED'))                    setAction('Already validated — skipping');
      else if (line.includes('State: AWAITING_APPROVAL'))           setAction('Waiting for APPROVED comment in Jira');
      else if (line.includes('State: NOT_GENERATED'))               setAction('New ticket — will generate query');
      else if (line.includes('Query rejected'))                      setAction('Query rejected — rewriting with your feedback…');
      else if (line.includes('Reviewer feedback'))                   setAction('Applying reviewer feedback…');
      else if (line.includes('Schema changed since approval'))       setAction('Schema changed — discarding stale query…');
      else if (line.includes('Query posted to Jira'))               setAction('Query posted — waiting for approval');
      else if (line.includes('Pruned'))                             setAction('Removing deleted tickets…');
      else if (line.includes('Generating dashboard'))               setAction('Building dashboard…');
    }};

    es.onerror = function() {{
      es.close();
      clearInterval(tick);
      actionBase = '';
      resetBtn();
      dot.className     = 'dot error';
      label.textContent = 'Run Agent';
      prog.textContent  = '';
      action.textContent = 'Server not running — python src/server.py';
      action.className  = '';
      setTimeout(() => {{ dot.className = 'dot'; action.textContent = ''; }}, 4000);
    }};
  }}
  </script>

  <section class="summary-grid">
    <div class="summary-card c-total{' is-zero' if not len(tickets) else ''}">
      <div class="summary-value">{len(tickets)}</div>
      <div class="summary-label">Total tickets</div>
    </div>
    <div class="summary-card c-success{' is-zero' if not succeeded else ''}">
      <div class="summary-value">{succeeded}</div>
      <div class="summary-label">Succeeded</div>
    </div>
    <div class="summary-card c-danger{' is-zero' if not failed else ''}">
      <div class="summary-value">{failed}</div>
      <div class="summary-label">Failed</div>
    </div>
    <div class="summary-card c-warning{' is-zero' if not query_errors else ''}">
      <div class="summary-value">{query_errors}</div>
      <div class="summary-label">Query errors</div>
    </div>
    <div class="summary-card c-neutral{' is-zero' if not not_run else ''}">
      <div class="summary-value">{not_run}</div>
      <div class="summary-label">Not run</div>
    </div>
  </section>

  <section class="controls">
    <input id="search" type="search" placeholder="Search by Jira key, summary or status">
    <select id="statusFilter">
      <option value="">All validation statuses</option>
      <option value="succeeded">Succeeded</option>
      <option value="failed">Failed</option>
      <option value="query_error">Query error</option>
      <option value="awaiting_approval">Awaiting approval</option>
      <option value="not_run">Not run</option>
      <option value="needs_clarification">Needs clarification</option>
      <option value="object_broken">Object is broken</option>
    </select>
  </section>

  <section class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="col-key">Jira key</th>
          <th class="col-sum">Summary</th>
          <th class="col-prod">Prod status</th>
          <th class="col-qf">Query found</th>
          <th class="col-vs">Validation status</th>
          <th class="col-vd">Validation details</th>
          <th class="col-ts">Run timestamp</th>
          <th class="col-evi">Evidence</th>
        </tr>
      </thead>
      <tbody id="ticketTable">
        {''.join(rows)}
        {empty_state}
      </tbody>
    </table>
  </section>

  <footer>{len(tickets)} ticket(s) · generated {escape(format_utc(report.get("generated_at")))}</footer>
</main>
<script>
const bd = document.getElementById('evi-bd');

bd.addEventListener('click', () => {{
  document.querySelectorAll('details[open]').forEach(d => d.removeAttribute('open'));
  bd.style.display = 'none';
}});

document.querySelectorAll('details').forEach(det => {{
  det.addEventListener('toggle', () => {{
    if (det.open) {{
      document.querySelectorAll('details[open]').forEach(d => {{ if (d !== det) d.removeAttribute('open'); }});
      bd.style.display = 'block';
    }} else if (!document.querySelector('details[open]')) {{
      bd.style.display = 'none';
    }}
  }});
}});

document.querySelectorAll('[data-ts]').forEach(el => {{
  const ts = el.dataset.ts;
  if (!ts) return;
  try {{
    // UTC, not local: this report is shared, so everyone must see the same time
    // for the same run rather than one shifted into their own zone.
    el.textContent = new Date(ts).toLocaleString('en-US', {{
      timeZone: 'UTC',
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', hour12: false
    }}) + ' UTC';
  }} catch(_) {{}}
}});

const si = document.getElementById('search');
const sf = document.getElementById('statusFilter');
const trows = Array.from(document.querySelectorAll('.ticket-row'));
function filterRows() {{
  const sv = si.value.trim().toLowerCase(), fv = sf.value;
  trows.forEach(r => {{
    r.style.display =
      (!sv || r.dataset.search.includes(sv)) && (!fv || r.dataset.status === fv) ? '' : 'none';
  }});
}}
si.addEventListener('input', filterRows);
sf.addEventListener('change', filterRows);

// The summary line is the handle on the run object; the drawer is the object.
const runLink   = document.getElementById('st-run');
const runDrawer = document.getElementById('run-drawer');
if (runLink && runDrawer) {{
  runLink.addEventListener('click', function(e) {{
    e.preventDefault();
    runDrawer.hidden = !runDrawer.hidden;
  }});
  document.addEventListener('click', function(e) {{
    if (!runDrawer.hidden && !runDrawer.contains(e.target) && e.target !== runLink) {{
      runDrawer.hidden = true;
    }}
  }});
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') runDrawer.hidden = true;
  }});
}}

// Older runs are in the file either way; this is what makes them reachable.
document.querySelectorAll('.rd-item').forEach(function(item) {{
  item.addEventListener('click', function() {{
    const n = item.dataset.run;
    runDrawer.querySelectorAll('.rd-panel').forEach(function(panel) {{
      panel.hidden = panel.dataset.run !== n;
    }});
    document.querySelectorAll('.rd-item').forEach(b => b.classList.toggle('active', b === item));
  }});
}});

// Destructive and unrecoverable — runs are a local log, not something Jira can rebuild.
// So the confirmation names both consequences before it happens, not after.
const clearBtn = document.getElementById('rd-clear');
if (clearBtn) {{
  clearBtn.addEventListener('click', function() {{
    const n = document.querySelectorAll('.rd-item').length;
    const ok = window.confirm(
      'Delete the history of ' + n + ' run(s)?\\n\\n' +
      '\\u2022 Run numbering restarts at #1\\n' +
      '\\u2022 The next scan reports no changes, because the verdicts it would ' +
      'compare against are being deleted\\n\\n' +
      'Ticket rows are not affected. This cannot be undone.');
    if (!ok) return;
    clearBtn.disabled = true;
    clearBtn.textContent = 'Clearing…';
    fetch(agentOrigin + '/clear-runs', {{ method: 'POST', cache: 'no-store' }})
      .then(function(res) {{
        // A 404 is a resolved fetch, so without this an old server that lacks the
        // endpoint looked like success and reloaded with the history intact.
        if (!res.ok) throw new Error('HTTP ' + res.status);
        window.location.reload();
      }})
      .catch(() => {{
        clearBtn.disabled = false;
        clearBtn.textContent = 'Clear run history';
        alert('Could not clear the history — the dashboard server did not accept the request.\\n'
          + 'If it has been running a while, restart it: python3 src/server.py');
      }});
  }});
}}

// "Show these tickets" has to actually show them, or it is just a label.
const showChanged = runDrawer ? runDrawer.querySelector('.rd-show-changed') : null;
if (showChanged) {{
  showChanged.addEventListener('click', function(e) {{
    e.preventDefault();
    const panel = showChanged.closest('.rd-panel');
    const keys = Array.from(panel.querySelectorAll('.rd-changes code'))
                      .map(c => c.textContent.trim().toLowerCase());
    si.value = ''; sf.value = '';
    trows.forEach(r => {{
      const key = (r.querySelector('.key-cell') || {{}}).textContent || '';
      r.style.display = keys.includes(key.trim().toLowerCase()) ? '' : 'none';
    }});
    runDrawer.hidden = true;
    document.querySelector('.table-wrap').scrollIntoView({{ behavior: 'smooth', block: 'start' }});
  }});
}}
</script>
</body>
</html>"""

    DASHBOARD_PATH.write_text(page, encoding="utf-8")


def update_traceability_report(
    issue_key: str,
    render_html: bool = True,
) -> tuple[Path, Path]:
    record = build_ticket_record(issue_key)
    report = load_report()
    report = upsert_ticket(report, record)

    save_report(report)
    if render_html:
        generate_dashboard(report)

    return REPORT_PATH, DASHBOARD_PATH


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Upsert one Jira ticket into the shared "
            "traceability report and dashboard."
        )
    )

    parser.add_argument(
        "issue_key",
        help="Jira issue key, for example DEMO-123",
    )

    args = parser.parse_args()

    report_path, dashboard_path = (
        update_traceability_report(args.issue_key)
    )

    print("Traceability report updated:")
    print(report_path)
    print("Shared dashboard updated:")
    print(dashboard_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
