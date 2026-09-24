"""
What the agent does when the things it depends on are broken.

The other two suites test a working world. This one breaks BigQuery and Jira on
purpose, because the dangerous failure is not an error — it is an error reported as
a pass. Every case here asserts the same thing in a different way: when the agent
could not look, it must say so and must never claim a validation succeeded.

    python tests/failure_cases.py

Needs no BigQuery credentials — that is the point. Failures are injected, so this
runs in seconds and is the only suite that can gate a commit. Case 1 additionally
uses the REAL client and is only meaningful while credentials are broken; it skips
itself when they work.
"""
from __future__ import annotations
import atexit, io, json, contextlib, pathlib, shutil, sys, tempfile, traceback

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import main as agent_main
import traceability_report as tr
import bigquery_executor as bq

SB = pathlib.Path(tempfile.mkdtemp(prefix="failsuite-"))
agent_main.OUTPUT_DIR = SB
tr.OUTPUT_DIR = SB; tr.REPORT_PATH = SB / "r.json"; tr.DASHBOARD_PATH = SB / "d.html"
bq.OUTPUT_DIR = SB
agent_main.DASHBOARD_PATH = tr.DASHBOARD_PATH
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))

T = "traceguard-demo.validation_demo.e2e_validation_fixture"
Q, RES = agent_main.AGENT_QUERY_MARKER, agent_main.AGENT_RESULT_MARKER

# A syntactically fine query, so nothing fails for the wrong reason.
GOOD_SQL = (f"SELECT COUNTIF(category IS NOT NULL) > 0 AS validation_passed, "
            f"'category checked' AS validation_details FROM `{T}`")


class Fake:
    """Records comments. Any real Jira read is a hard failure."""
    def __init__(self): self.posted = []
    def add_comment(self, k, b): self.posted.append(b); return {}
    def download_attachment(self, *a, **k): return None
    def read_issue(self, *a, **k): raise AssertionError("SAFETY: read_issue called")
    def search_issues(self, *a, **k): raise AssertionError("SAFETY: search_issues called")


class DeadJira(Fake):
    """Jira is up enough to read but refuses writes — a a real outage shape."""
    def add_comment(self, k, b):
        raise ConnectionError("503 Service Unavailable: Jira is down")


def tk(key, desc, comments=None):
    return {"jira_key": key, "summary": "[Dev] ticket", "description": desc,
            "labels": [agent_main.VALIDATION_LABEL],
            "comments": [{"author": "S", "id": str(i), "body": b}
                         for i, b in enumerate(comments or [])],
            "attachments": [], "status": "Open", "project": "DEMO",
            "issue_type": "Story", "components": [], "fix_versions": [],
            "updated": "2026-08-10T10:00:00.000+0000"}


CASES, RESULTS = [], []


def case(n, title, needs_broken_creds=False):
    def d(fn): CASES.append((n, title, fn, needs_broken_creds)); return fn
    return d


def _state(key):
    path = SB / f"{key.upper()}-validation-state.json"
    return json.loads(path.read_text()) if path.exists() else None


def _result(key):
    path = SB / f"{key.upper()}-execution-result.json"
    return json.loads(path.read_text()) if path.exists() else None


def _no_false_success(entry, key, posted):
    """
    The one rule this whole suite exists for.

    Applied to every case regardless of what it was checking, because "it broke and
    said so" and "it broke and claimed success" are the only two outcomes that
    matter, and only one of them is acceptable.
    """
    result = _result(key)
    if result and result.get("status") == "VALIDATION_PASSED":
        entry["problems"].append(
            "HARD FAIL: recorded VALIDATION_PASSED while the dependency was broken")
    if result and result.get("validation_passed"):
        entry["problems"].append("HARD FAIL: validation_passed is true despite the failure")
    for phrase in ("VALIDATION PASSED", "validation succeeded"):
        if phrase.lower() in posted.lower():
            entry["problems"].append(f"HARD FAIL: told the reader {phrase!r}")


def run_case(n, title, fn):
    entry = {"n": n, "title": title, "problems": []}
    buf = io.StringIO()
    try:
        ticket, reader, check = fn()
        key = ticket["jira_key"]
        try:
            with contextlib.redirect_stdout(buf):
                entry["exit"] = agent_main.run_agent_workflow(
                    issue_key=key, ticket_data=ticket, reader=reader, batch=True)
            entry["raised"] = None
        except Exception as error:
            # A crash is acceptable for some failures and not others — the case
            # decides. What is never acceptable is a silent success.
            entry["raised"] = f"{type(error).__name__}: {error}"
        entry["log"] = buf.getvalue()
        posted = "\n\n".join(reader.posted)
        entry["posted"] = posted
        _no_false_success(entry, key, posted)
        check(entry, key, posted)
    except AssertionError as error:
        entry["problems"].append(f"ASSERT: {error}")
    except Exception:
        entry["problems"].append(
            "EXCEPTION in harness: " + traceback.format_exc(limit=2).strip().splitlines()[-1])
    RESULTS.append(entry)
    return entry


# ─────────────────────────── cases ───────────────────────────
@case(1, "REAL expired credentials: refuses to generate, states it could not look",
      needs_broken_creds=True)
def c1():
    def check(entry, key, posted):
        state = _state(key)
        # Both the model and BigQuery authenticate with ADC now, so one expired login
        # can surface at either call. Whichever trips first, the requirement is the
        # same: refuse to generate and say it could not look.
        acceptable = {"METADATA_UNAVAILABLE", "CREDENTIALS_UNAVAILABLE"}
        if not state or state.get("status") not in acceptable:
            entry["problems"].append(
                f"expected one of {sorted(acceptable)}, got {state and state.get('status')}")
        if "{code:sql}" in posted:
            entry["problems"].append("posted a query built on a schema it never read")
        if entry.get("exit") == 0:
            entry["problems"].append("exit 0 — reported success to the caller")
    # No patching: this uses the real client, which is exactly the point.
    return tk("F1", f"Check category exists.\n\ntable_name: {T}"), Fake(), check


@case(2, "Metadata permission denied: no query, no guessed schema")
def c2():
    original = agent_main.get_table_metadata
    original_parse = agent_main.parse_intent
    agent_main.parse_intent = lambda *a, **k: {
        "change_type": "column_added",
        "full_table_name": T,
        "column_name": "category",
        "validation_requirements": "Check category exists.",
        "_human_text": "Check category exists.",
    }
    agent_main.get_table_metadata = lambda name: {
        "exists": False, "columns": [], "partition": None,
        "error": "403 Access Denied: User does not have bigquery.tables.get permission"}
    atexit.register(lambda: setattr(agent_main, "get_table_metadata", original))

    def check(entry, key, posted):
        agent_main.get_table_metadata = original
        agent_main.parse_intent = original_parse
        state = _state(key)
        if not state or state.get("status") != "METADATA_UNAVAILABLE":
            entry["problems"].append(
                f"expected METADATA_UNAVAILABLE, got {state and state.get('status')}")
        if "{code:sql}" in posted:
            entry["problems"].append("generated a query without a schema")
    return tk("F2", f"Check category exists.\n\ntable_name: {T}"), Fake(), check


@case(3, "Execution denied after approval: reports the failure, never a pass")
def c3():
    orig_run, orig_fix = agent_main.run_query, agent_main.fix_query
    agent_main.run_query = lambda issue_key, query: {
        "stage": "BIGQUERY_EXECUTION", "jira_key": issue_key, "status": "EXECUTION_FAILED",
        "errors": ["403 Access Denied: User does not have bigquery.jobs.create permission"],
        "validation_passed": False, "total_bytes_processed": 0}
    agent_main.fix_query = lambda q, e, c: q   # a permission error is not fixable by SQL

    def check(entry, key, posted):
        agent_main.run_query, agent_main.fix_query = orig_run, orig_fix
        result = _result(key)
        if not result or result.get("status") != "EXECUTION_FAILED":
            entry["problems"].append(
                f"expected EXECUTION_FAILED, got {result and result.get('status')}")
        if "access denied" not in posted.lower() and "403" not in posted:
            entry["problems"].append("the reader is not told why it failed")
    return tk("F3", f"Check category.\n\ntable_name: {T}",
              [f"{Q}\n{{code:sql}}{GOOD_SQL}{{code}}", "@agent APPROVED"]), Fake(), check


@case(4, "Query timeout: reports the failure, never a pass")
def c4():
    orig_run, orig_fix = agent_main.run_query, agent_main.fix_query
    def timeout(issue_key, query):
        raise TimeoutError("Query exceeded timeout of 300s and was cancelled")
    agent_main.run_query = timeout
    agent_main.fix_query = lambda q, e, c: q

    def check(entry, key, posted):
        agent_main.run_query, agent_main.fix_query = orig_run, orig_fix
        # Crashing is acceptable here; silently recording a pass is not. The
        # base invariant already checked that, so only confirm nothing was stored.
        result = _result(key)
        if result and result.get("status") == "VALIDATION_PASSED":
            entry["problems"].append("HARD FAIL: pass recorded despite a timeout")
        if not entry.get("raised") and not result:
            entry["problems"].append(
                "swallowed the timeout without recording or raising anything")
    return tk("F4", f"Check category.\n\ntable_name: {T}",
              [f"{Q}\n{{code:sql}}{GOOD_SQL}{{code}}", "@agent APPROVED"]), Fake(), check


@case(5, "BigQuery returns no rows: not evidence of a pass")
def c5():
    orig_run, orig_fix = agent_main.run_query, agent_main.fix_query
    agent_main.run_query = lambda issue_key, query: {
        "stage": "BIGQUERY_EXECUTION", "jira_key": issue_key, "status": "NO_RESULT_ROWS",
        "errors": ["query returned no rows"], "validation_passed": False,
        "total_bytes_processed": 0}
    agent_main.fix_query = lambda q, e, c: q

    def check(entry, key, posted):
        agent_main.run_query, agent_main.fix_query = orig_run, orig_fix
        result = _result(key)
        if result and result.get("validation_passed"):
            entry["problems"].append("HARD FAIL: empty result read as a pass")
    return tk("F5", f"Check category.\n\ntable_name: {T}",
              [f"{Q}\n{{code:sql}}{GOOD_SQL}{{code}}", "@agent APPROVED"]), Fake(), check


@case(6, "Jira write fails: does not record a success it could not report")
def c6():
    orig_run, orig_fix = agent_main.run_query, agent_main.fix_query
    agent_main.run_query = lambda issue_key, query: {
        "stage": "BIGQUERY_EXECUTION", "jira_key": issue_key, "status": "VALIDATION_PASSED",
        "errors": [], "validation_passed": True, "validation_details": "category checked",
        "total_bytes_processed": 4096}
    agent_main.fix_query = lambda q, e, c: q

    def check(entry, key, posted):
        agent_main.run_query, agent_main.fix_query = orig_run, orig_fix
        # Here the validation genuinely passed — the base invariant would misread
        # that, so this case owns its own rule: the failure must not be silent.
        entry["problems"] = [p for p in entry["problems"] if "HARD FAIL" not in p]
        if not entry.get("raised"):
            entry["problems"].append(
                "Jira rejected the comment and the agent carried on silently — "
                "the ticket shows nothing while state says it ran")
    return tk("F6", f"Check category.\n\ntable_name: {T}",
              [f"{Q}\n{{code:sql}}{GOOD_SQL}{{code}}", "@agent APPROVED"]), DeadJira(), check


def batch_isolation() -> list[str]:
    """
    One exploding ticket must not strand the queue.

    Not a run_case — it drives run_bulk rather than a single ticket, because the
    thing under test is the loop. Before the fix, run_bulk caught only
    TicketTimeout, so a BigQuery timeout on ticket 2 of 50 ended the scan: 48
    tickets silently unvalidated and no dashboard written.
    """
    problems, attempted = [], []
    keys = ["B1", "B2", "B3"]

    def workflow(issue_key, ticket_data, reader, batch=False):
        attempted.append(issue_key)
        if issue_key == "B2":
            raise TimeoutError("BigQuery timed out")
        return 0

    class Reader:
        def search_issues(self, *a, **k): return [tk(k_, "desc") for k_ in keys]
        def add_comment(self, *a, **k): return {}

    originals = (agent_main.run_agent_workflow, agent_main.JiraReader,
                 agent_main.generate_dashboard, agent_main.load_report,
                 agent_main.save_report)
    agent_main.run_agent_workflow = workflow
    agent_main.JiraReader = Reader
    agent_main.generate_dashboard = lambda *a, **k: None
    agent_main.load_report = lambda *a, **k: {"tickets": []}
    agent_main.save_report = lambda *a, **k: None
    try:
        # stderr too: the fix prints a traceback for the failing ticket, which is
        # wanted in production and noise here.
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            agent_main.run_bulk("dummy jql")
    except Exception as error:
        problems.append(f"run_bulk aborted on one bad ticket: {type(error).__name__}: {error}")
    finally:
        (agent_main.run_agent_workflow, agent_main.JiraReader,
         agent_main.generate_dashboard, agent_main.load_report,
         agent_main.save_report) = originals

    if attempted != keys:
        problems.append(f"attempted {attempted}, expected all of {keys} — "
                        f"tickets after the failure were skipped")
    return problems


def refresh_stamp() -> list[str]:
    """
    A scan that matches no tickets must still refresh the dashboard timestamp.

    Not a run_case — it drives run_bulk, and the thing under test is the scan, not a
    ticket. Before the fix, generated_at was only written by upsert_ticket, so a scan
    matching nothing left the old date on the dashboard and looked like the agent had
    never run.
    """
    problems, saved = [], []

    class Reader:
        def search_issues(self, *a, **k): return []       # nothing matched
        def add_comment(self, *a, **k): return {}

    originals = (agent_main.JiraReader, agent_main.generate_dashboard,
                 tr.load_report, tr.save_report)
    agent_main.JiraReader = Reader
    agent_main.generate_dashboard = lambda *a, **k: None
    tr.load_report = lambda *a, **k: {"generated_at": "2026-08-06T17:53:00+00:00", "tickets": []}
    tr.save_report = lambda report: saved.append(report)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            agent_main.run_bulk("dummy jql")
    except Exception as error:
        problems.append(f"run_bulk raised on an empty scan: {type(error).__name__}: {error}")
    finally:
        (agent_main.JiraReader, agent_main.generate_dashboard,
         tr.load_report, tr.save_report) = originals

    if not saved:
        problems.append("no report was saved, so the timestamp stayed stale")
    elif saved[-1].get("generated_at", "").startswith("2026-08-06"):
        problems.append("generated_at was not updated on an empty scan")
    return problems


def _credentials_broken() -> bool:
    """True when BigQuery cannot be reached — case 1 is only meaningful then."""
    meta = bq.get_table_metadata(T)
    return bool(meta.get("error"))


# Since the move to Gemini both the model and BigQuery authenticate with ADC, so an
# expired login now fails inside parse_intent — before the metadata guard case 1 checks.
# Left unhandled it was invisible: no state, nothing on the dashboard, nothing in Jira,
# and every remaining ticket in the scan failing the same silent way.
@case(9, "Expired login: recorded on the dashboard, never posted to the ticket")
def c9():
    def check(entry, key, posted):
        state = _state(key)
        if not state or state.get("status") != "CREDENTIALS_UNAVAILABLE":
            entry["problems"].append(
                f"expected state CREDENTIALS_UNAVAILABLE, got {state and state.get('status')}")
        if posted.strip():
            entry["problems"].append(
                "posted to the ticket — an expired login is our problem, not the reporter's")
        if entry.get("exit") == 0:
            entry["problems"].append("exit 0 — reported success to the caller")
        # Restored here, not via atexit: every later case needs a working parse_intent.
        agent_main.parse_intent = original

    original = agent_main.parse_intent
    def expired(*a, **k):
        from google.auth.exceptions import RefreshError
        raise RefreshError("Reauthentication is needed. Please run `gcloud auth "
                           "application-default login` to reauthenticate.")
    agent_main.parse_intent = expired
    return tk("F9", f"Check category exists.\n\ntable_name: {T}"), Fake(), check


if __name__ == "__main__":
    broken = _credentials_broken()
    print(f"BigQuery credentials: {'BROKEN (case 1 is live)' if broken else 'working (case 1 skipped)'}\n")
    want = {int(a) for a in sys.argv[1:]} if len(sys.argv) > 1 else None

    skipped = 0
    for n, title, fn, needs_broken in CASES:
        if want and n not in want:
            continue
        if needs_broken and not broken:
            print(f"[SKIP] {n}. {title}")
            skipped += 1
            continue
        entry = run_case(n, title, fn)
        print(f"[{'PASS' if not entry['problems'] else 'FAIL'}] {n}. {title}")
        for problem in entry["problems"]:
            print("        !", problem)
        if entry.get("raised"):
            print("        (raised:", entry["raised"][:110] + ")")

    if not want:
        issues = refresh_stamp()
        RESULTS.append({"n": 8, "title": "Empty scan still refreshes the dashboard timestamp",
                        "problems": issues})
        print(f"[{'PASS' if not issues else 'FAIL'}] 8. "
              f"Empty scan still refreshes the dashboard timestamp")
        for problem in issues:
            print("        !", problem)

        issues = batch_isolation()
        RESULTS.append({"n": 7, "title": "One exploding ticket does not strand the batch",
                        "problems": issues})
        print(f"[{'PASS' if not issues else 'FAIL'}] 7. "
              f"One exploding ticket does not strand the batch")
        for problem in issues:
            print("        !", problem)

    bad = [r for r in RESULTS if r["problems"]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} passed"
          + (f", {skipped} skipped" if skipped else ""))
    if bad:
        print("\nFAILURES:")
        for r in bad:
            print(f"  {r['n']}. {r['title']}")
            for p in r["problems"]:
                print("     -", p)
    raise SystemExit(1 if bad else 0)

