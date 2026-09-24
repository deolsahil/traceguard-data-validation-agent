# Test evidence

Raw output from the suite runs, kept in the repo so the result can be checked rather
than taken on trust.

## What the agent is tested against

**79 test cases** across three suites:

| Suite | Cases | What it asks |
|---|---|---|
| [Failure](../failure_cases.py) | 9 | What if its dependencies break? |
| [Edge](../edge_cases.py) | 28 | Does the pipeline behave? |
| [Golden](../golden_cases.py) | 42 | Is the answer true? |

The golden suite is scored across twelve categories — tables, views, stored procedures,
data states, identification, interpretation, ambiguity, approval, safety, hallucination,
natural-language variation and wrong-object handling — and has a deployment gate that
blocks on any hard failure, regardless of the average.

## Latest run — 2026-08-19, Gemini 3.7 Flash on Vertex

| Suite | Result |
|---|---|
| Failure | **7/7**, 1 skipped (case 1 only runs when BigQuery credentials are broken) |
| Edge | **28/28** |
| Golden | **33/35** at time of run; both failures fixed, see below |

The two golden failures were `TC-TABLE-008` (a two-column ticket answered on the one
column that existed) and `TC-DATA-005` (an empty scan window reported as an empty
column). Both were fixed the same day, and the cases that reach the changed code were
re-run and pass:

- golden `TC-TABLE-002`, `TC-TABLE-006`, `TC-TABLE-007`, `TC-TABLE-008`, `TC-VIEW-004`,
  `TC-DATA-001` through `TC-DATA-005` — 10/10
- edge `1`, `4`, `6`, `15`, `21` — 5/5

**A full-suite run has not been repeated since those fixes.** The result is inferred from
the targeted re-runs above, not measured in one sweep. Run both suites end to end before deploying.

## 2026-08-24 — full golden run

39/42 in one sweep. The three failures were duplicate `tk()` ticket keys in newly added cases,
not agent behaviour: two cases sharing a key share saved state, so one case's approved query was
still pending when the next ran and it executed without approval. Keys renumbered, and all 14
cases the collision could have touched re-run — 14/14, deployment gate clear.

Edge 28/28. Failure 7/7 with 1 skipped.

**42/42 has not been measured in a single sweep since the renumbering.** The 39 + 14 above cover
every case, but in two runs.

## 2026-08-24 — two defects found in production tickets

Both surfaced on real tickets, not in the suites, and both are now covered by new cases.

| | |
|---|---|
| A bare `COUNT(*)` returned FAILED / INCONCLUSIVE | BigQuery answers a row count from table metadata and bills 0 bytes; the unearned-pass guard read 0 bytes as "nobody looked". Guard narrowed in `main.py`, and the same assumption fixed in the harness invariant. New: `TC-DATA-006`, `TC-DATA-007` |
| A landed change reported FAILED | The ticket asserted one thing and asked for a number alongside it; the agent made the number a pass condition. `_SYSTEM` rule 10 now scopes the verdict to stated conditions. New: `TC-DATA-008` |

Re-run after the fixes: golden `TC-DATA-001` through `TC-DATA-008` — 8/8; failure suite 7/7,
1 skipped. Edge and the rest of golden were not re-run.

## Earlier run — 2026-08-13, agent commit `e28735e`

| Suite | Result | Log |
|---|---|---|
| Failure | 7/7, 1 skipped | run locally, no credentials needed |
| Edge | **28/28** | [edge-cases.log](edge-cases.log) |
| Golden | **35/35**, 100%, no hard failures | [golden-cases.log](golden-cases.log) |

## Reproducing it

```bash
gcloud auth application-default login   # token expires roughly daily
python3 tests/golden_dataset.py         # confirm BigQuery still matches the answer key
python3 tests/failure_cases.py          # no credentials needed
python3 tests/edge_cases.py
python3 tests/golden_cases.py
```

Run `golden_dataset.py` first: it checks the BigQuery dataset still matches the declared
answer key, and the grades are meaningless if it has drifted.

Both the edge and golden suites call the real LLM and real BigQuery, so timings will not
match to the second. What must not change is the pass/fail column.

Individual cases can be run on their own, which is much cheaper than a full sweep:

```bash
python3 tests/golden_cases.py TC-TABLE-008   # by case ID
python3 tests/edge_cases.py 4 21             # by number
```

## Scope

Each case runs once per sweep. Repeat-run results: [determinism.md](determinism.md).

Not covered: BigQuery or Jira actually unavailable, cost and latency budgets, and the edge
and golden suites in CI (they need real credentials).
