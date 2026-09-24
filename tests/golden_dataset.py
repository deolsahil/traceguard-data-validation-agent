"""
The answer key: what the golden dataset contains, written down independently.

This is hand-written on purpose. Deriving the expected state by querying BigQuery
would only prove BigQuery agrees with itself: the agent's answer comes from
BigQuery too, so both sides would move together and a wrong answer would grade as
correct. The facts below are asserted independently of the system under test.

Seeded by `tests/golden_seed.sql`. If you change one, change the other —
`verify()` fails loudly when they drift.

    python tests/golden_dataset.py      # confirm BigQuery matches what's declared here
"""
from __future__ import annotations

import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
os.chdir(REPO)

PROJECT = os.getenv("GOLDEN_PROJECT", "traceguard-demo")
DATASET = os.getenv("GOLDEN_DATASET", "e2e_agent_eval")
GOLDEN = f"{PROJECT}.{DATASET}"


def t(name: str) -> str:
    """Fully-qualified name of a golden object."""
    return f"{GOLDEN}.{name}"


# ─────────────────────────────────────────────────────────────────────────────
# Column population rates. The number is what fraction of rows are NOT NULL, so
# 0.0 means "the column exists and every row is NULL" — which is a different
# claim from "the column does not exist", and the agent must not conflate them.
# ─────────────────────────────────────────────────────────────────────────────
CUSTOMERS_POPULATION = {
    "customer_id":         1.00,
    "customer_identifier": 1.00,
    "customerid":          1.00,
    "customer_key":        1.00,
    "email":               1.00,   # fully populated
    "phone":               0.00,   # exists, entirely NULL
    "customer_status":     0.75,   # partially populated
    "status_pct_97":       0.97,   # passes a >= 95% threshold
    "status_pct_92":       0.92,   # fails a >= 95% threshold
    "signup_date":         1.00,
    "lifetime_value":      1.00,
}

TABLES = {
    "customers":             {"rows": 1000, "population": CUSTOMERS_POPULATION},
    "customers_archive":     {"rows": 500,  "population": {"phone": 1.00, "email": 1.00}},
    "customers_staging":     {"rows": 100,  "population": {"customer_id": 1.00}},
    "customers_v2":          {"rows": 100,  "population": {"customer_id": 1.00}},
    "orders":                {"rows": 3000, "population": {"order_id": 1.00, "ship_status": 0.80}},
    "orders_stale":          {"rows": 500,  "population": {"order_id": 1.00}},
    "orders_rollup":         {"rows": 30,   "population": {"order_date": 1.00}},
    "customer_summary_tbl":  {"rows": 1000, "population": {"email": 1.00}},
    "empty_table":           {"rows": 0,    "population": {}},
}

VIEWS = {
    # Exposes email, hides phone — even though the base table has a phone column.
    "customer_summary": {"exposes": ["customer_id", "email", "customer_status",
                                     "signup_date", "lifetime_value"],
                         "hides": ["phone"]},
}

ROUTINES = {
    "refresh_customer_summary":        {"writes": "customer_summary_tbl",
                                        "references": ["email"], "omits": ["phone"]},
    "refresh_customer_summary_v2":     {"writes": "customer_summary_tbl"},
    "refresh_customer_summary_backup": {"writes": "customer_summary_tbl"},
    # Builds its destination with FORMAT + EXECUTE IMMEDIATE rather than a literal.
    "rebuild_orders_rollup":           {"writes": "orders_rollup"},
}

# Things that deliberately do not exist. Asking about any of these must produce a
# reported absence, never an invented one.
# customers_v3 is a deliberate near-miss of the real customers_v2 — it exists to
# prove the agent offers the close name as a question and still reports the
# requested one as missing.
ABSENT_TABLES = ["customer_details", "customers_backup", "customers_v3"]
ABSENT_COLUMNS = {"customers": ["customer_type", "customer_tier", "customer_segment_v2"]}
ABSENT_ROUTINES = ["calculate_order_metrics"]

# Partitioned tables, and whether a default 7-day window reaches their data.
PARTITIONED = {
    "orders":       {"field": "order_date", "reaches_data": True},
    "orders_stale": {"field": "order_date", "reaches_data": False},  # ~200 days stale
}


def verify() -> list[str]:
    """
    Confirm BigQuery actually holds what this module declares.

    Returns a list of mismatches — empty means the seed is intact. Run this before
    trusting a suite result: every golden case is graded against the facts above,
    so if the dataset drifted, the grades are meaningless rather than merely wrong.
    """
    from bigquery_executor import _client, load_bigquery_settings

    _, location = load_bigquery_settings()
    problems: list[str] = []

    def q(sql: str):
        return list(_client().query(sql, location=location).result())

    # Row counts and population rates, one query per table — small tables, and a
    # single UNION would obscure which table a mismatch came from.
    for name, spec in TABLES.items():
        cols = spec["population"]
        selects = ["COUNT(*) AS row_count"] + [
            f"COUNTIF({c} IS NOT NULL) AS nn_{c}" for c in cols
        ]
        try:
            row = q(f"SELECT {', '.join(selects)} FROM `{t(name)}`")[0]
        except Exception as error:
            problems.append(f"{name}: cannot read ({str(error).splitlines()[0][:110]})")
            continue

        actual_rows = row["row_count"]
        if actual_rows != spec["rows"]:
            problems.append(f"{name}: {actual_rows} rows, expected {spec['rows']}")
        for col, expected in cols.items():
            if actual_rows == 0:
                continue
            got = row[f"nn_{col}"] / actual_rows
            if abs(got - expected) > 0.005:
                problems.append(
                    f"{name}.{col}: {got:.0%} populated, expected {expected:.0%}")

    # Object existence, including the deliberate absences.
    present_tables = {r["table_name"] for r in q(
        f"SELECT table_name FROM `{GOLDEN}.INFORMATION_SCHEMA.TABLES`")}
    for name in list(TABLES) + list(VIEWS):
        if name not in present_tables:
            problems.append(f"{name}: missing from the dataset")
    for name in ABSENT_TABLES:
        if name in present_tables:
            problems.append(f"{name}: exists, but the suite relies on it NOT existing")

    present_routines = {r["routine_name"] for r in q(
        f"SELECT routine_name FROM `{GOLDEN}.INFORMATION_SCHEMA.ROUTINES`")}
    for name in ROUTINES:
        if name not in present_routines:
            problems.append(f"{name}: procedure missing")
    for name in ABSENT_ROUTINES:
        if name in present_routines:
            problems.append(f"{name}: exists, but the suite relies on it NOT existing")

    # Columns that must not exist — the anti-hallucination probes.
    for table, columns in ABSENT_COLUMNS.items():
        real = {r["column_name"] for r in q(
            f"SELECT column_name FROM `{GOLDEN}.INFORMATION_SCHEMA.COLUMNS` "
            f"WHERE table_name = '{table}'")}
        for col in columns:
            if col in real:
                problems.append(
                    f"{table}.{col}: exists, but the suite relies on it NOT existing")

    # The view must genuinely not expose what it is supposed to hide.
    for view, spec in VIEWS.items():
        exposed = {r["column_name"] for r in q(
            f"SELECT column_name FROM `{GOLDEN}.INFORMATION_SCHEMA.COLUMNS` "
            f"WHERE table_name = '{view}'")}
        for col in spec["exposes"]:
            if col not in exposed:
                problems.append(f"{view}: does not expose {col}")
        for col in spec["hides"]:
            if col in exposed:
                problems.append(f"{view}: exposes {col}, which it must hide")

    return problems


if __name__ == "__main__":
    print(f"Verifying golden dataset `{GOLDEN}`...\n")
    issues = verify()
    if not issues:
        print(f"OK — {len(TABLES)} tables, {len(VIEWS)} view(s), "
              f"{len(ROUTINES)} procedure(s) match the declared ground truth.")
        raise SystemExit(0)
    print(f"{len(issues)} mismatch(es) between BigQuery and the declared ground truth:")
    for issue in issues:
        print("  -", issue)
    print("\nRe-run tests/golden_seed.sql to reset the dataset.")
    raise SystemExit(1)
