# Sample Jira Ticket — Validation Agent Guide

## What the agent needs to work

| Field | Required? | Notes |
|---|---|---|
| Jira label: `validation-agent` | **Must have** | Without this the agent skips the ticket entirely |
| Table / view / SP name | **Must have** | Full path preferred: `project.dataset.table` |
| Type of change | **Must have** | Column added, type changed, table created, view modified, SP changed |
| Column name | Only for column changes | The exact column name as it appears in BigQuery |
| Data type | Only for type changes | e.g. `STRING`, `INT64`, `TIMESTAMP`, `DATE`, `BOOL` |
| Dataset / project | Optional if full path given | Helps if only partial name is provided |

The agent reads the **ticket summary, description, and all comments**. You can write in plain English — it understands natural language. But being specific about names avoids ambiguity.

---

## Change type reference

| What happened | Use this phrase |
|---|---|
| New column added to existing table | "added a column", "new column", "column added" |
| Column data type changed | "type changed", "changed from X to Y", "column_type_changed" |
| Column value or schema modified | "modified column", "updated column" |
| New table created | "new table", "created table", "table added" |
| Existing table schema/data changed | "modified table", "updated table" |
| New view created or existing view changed | "new view", "created view", "view updated" |
| Stored procedure changed | "SP changed", "stored procedure updated", "procedure modified" |

---

## Sample tickets by change type

---

### 1. Column Added

**Summary:**
```
[Analytics] Add session_id column to user_events table
```

**Labels:** `validation-agent`

**Description:**
```
As part of the session tracking initiative, we added a new column session_id
to the user events table.

table_name: gcp-project-prod.analytics_dataset.user_events
column_name: session_id
```

---

### 2. Column Type Changed

**Summary:**
```
[DAP] Change event_timestamp column type from STRING to TIMESTAMP
```

**Labels:** `validation-agent`

**Description:**
```
The event_timestamp column in the tag_events table was migrated from STRING
to TIMESTAMP to align with the new pipeline schema.

table_name: gcp-project-prod.analytics_dataset.tag_events
column_name: event_timestamp
data_type: TIMESTAMP
```

> The agent will use `INFORMATION_SCHEMA.COLUMNS` (0 bytes scanned) to verify the column type matches the expected value.

---

### 3. New Table Created

**Summary:**
```
[DAP] Create new daily_active_users aggregation table
```

**Labels:** `validation-agent`

**Description:**
```
Created a new aggregation table for daily active users as part of the
reporting pipeline refresh.

table_name: gcp-project-prod.reporting_dataset.daily_active_users
```

---

### 4. View Created or Modified

**Summary:**
```
[TraceGuard] Update v_tag_events view to include new event types
```

**Labels:** `validation-agent`

**Description:**
```
The v_tag_events view was updated to include two new event types:
page_load and scroll_depth.

view_name: gcp-project-prod.analytics_dataset.v_tag_events
```

---

### 5. Stored Procedure Changed

**Summary:**
```
[Pipeline] Update sp_refresh_product_catalog to handle null categories
```

**Labels:** `validation-agent`

**Description:**
```
Modified the product catalog refresh stored procedure to handle null
category values that were causing downstream failures.

sp_name: sp_refresh_product_catalog
dataset: catalog_dataset
project: gcp-project-prod
```

> **SP note:** Stored procedures are not directly queryable. The agent validates the SP's **output table** instead.
> If the SP body contains placeholder variables like `<<GCP_PROJECT>>`, the agent cannot resolve them automatically.
> In that case, include `table_name:` explicitly with the real output table path.

```
sp_name: sp_refresh_product_catalog
table_name: gcp-project-prod.catalog_dataset.product_catalog
```

---

### 6. Table with an attached data model (CSV / Excel)

**Summary:**
```
[DAP] Create customer_master table
```

**Labels:** `validation-agent`

**Attachment:** `customer_master_model.xlsx`

**Description:**
```
Create this table as per the attached data model.

table_name: gcp-project-prod.reporting_dataset.customer_master
```

The agent downloads the attachment, reads the schema, and validates **every**
column in one query — no need to list 50 columns in the description.

The spreadsheet needs a header row naming the columns. These are all recognised:

| Purpose | Accepted header names |
|---|---|
| Column name | `column_name`, `column name`, `field_name`, `attribute_name`, `column`, `field`, `name` |
| Data type | `data_type`, `data type`, `datatype`, `column_type`, `field_type`, `type` |
| Description | `description`, `comment`, `definition`, `notes` |

Example sheet:

| Column Name | Data Type | Description |
|---|---|---|
| customer_id | STRING | Unique customer id |
| signup_date | DATE | Date of signup |
| is_active | BOOL | Active flag |

Notes:
- `.csv`, `.tsv`, `.txt`, `.xlsx`, `.xlsm` are supported. PDFs and images are skipped.
- Title/blank rows above the header are fine — the agent scans the first 20 rows for it.
- If a **Data Type** column is present the agent checks name **and** type; otherwise existence only.
- Validation runs against `INFORMATION_SCHEMA.COLUMNS`, so it scans **0 bytes** no matter how many columns.
- If several files are attached, the first one that parses as a data model is used.

---

### 7. Multiple validation checks (free-form NLP)

**Summary:**
```
[DAP] Add attribution_source column and verify existing status values
```

**Labels:** `validation-agent`

**Description:**
```
Added a new column attribution_source to the conversions table. Also want
to confirm that existing status values are non-null and that rows where
status = 'completed' make up at least some of the total.

table_name: gcp-project-prod.marketing_dataset.conversions
column_name: attribution_source
```

> The agent reads the full description and generates a query that covers **all** the checks mentioned — column existence, distinct counts, filtered counts, value thresholds, etc. You do not need to write SQL; write what you want validated.

---

## Metadata questions skip approval

If everything the ticket asks can be answered from `INFORMATION_SCHEMA` — does a table
exist, does a column exist, what type is it, how many columns — the agent runs it
immediately and posts the answer. No `APPROVED` needed, because no table rows are read.

```
@agent - does e2e_tracebility_validation_wrk exist?
→ e2e_tracebility_validation_wrk: MISSING
```

Anything that reads actual data still waits for your approval as normal.

---

## What happens after you create the ticket

1. **Add label `validation-agent`** to the ticket in Jira
2. **Run the agent** via the dashboard ("Run Agent" button) or CLI:
   ```bash
   python3 src/main.py --jql "labels = validation-agent"
   ```
3. **Agent checks partition info** on the target table (zero bytes scanned)
4. **Agent posts a comment** with the generated SQL query and partition status
5. **Review the query:**
   - Looks good → `@agent APPROVED`
   - Wrong → `@agent REJECT` and say why, e.g. `@agent REJECT - should be 30 days, not 7`.
     The agent rewrites it using your reason and reposts.
   - Needs a specific query → paste your corrected SQL in a `{code:sql}` block, then `@agent APPROVED`
6. **Run the agent again** (or wait for the next scheduled run)
7. **Agent executes** the approved query in BigQuery and posts results back
8. **Dashboard updates** at `output/traceability-dashboard.html`

---

## Partition behaviour

- **Partitioned table** → agent adds `WHERE partition_col >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)` automatically. The Jira comment shows the partition field and type.
- **No partition** → Jira comment warns of a full table scan. You decide whether to approve.
- The agent detects the partition column type (DATE / TIMESTAMP / DATETIME) and uses the correct date function — no manual adjustment needed.

---

## Talking to the agent — `@agent`

**The agent only acts on comments that tag it.** Everything else on the ticket is
team conversation. That's the whole rule, and it's what stops an unrelated approval
("manager APPROVED the change request") from running a query.

Recognised tags: `@agent` and `@validation-agent`.

There are three things you can say:

| Comment | Effect |
|---|---|
| `@agent APPROVED` | Runs the pending query |
| `@agent REJECT - <reason>` | Discards it and rewrites using your reason |
| `@agent <instruction>` | Checks something else — starts a fresh cycle |

Plus one modifier: paste SQL in a `{code:sql}` block, then `@agent APPROVED` to run
exactly your query instead of the agent's.

Notes:
- `APPROVED` and `REJECT` must come first in the instruction. `@agent APPROVED - looks
  good` works; `@agent - not approved yet, hold off` is not an approval and won't run
  anything.
- On an already-validated ticket, `@agent <instruction>` starts a fresh cycle. There is
  no separate re-run keyword.
- If several tagged comments exist, the last one wins.

---

## Re-checking after a change

Once a ticket has been validated, tag the agent with what you want checked and it
starts a fresh cycle — re-reading the current description and comments, generating a
new query, and asking for approval again.

```
@agent the description changed, please re-check with the new requirements
```

There's no separate re-run keyword: `@agent` covers it.

---

## Result comment in Jira

After execution, the agent posts back to the ticket:

| Result | What you see |
|---|---|
| Pass | `(/) VALIDATION PASSED` + row counts + bytes scanned |
| Fail | `(x) VALIDATION_FAILED` + what the query returned |
| Error | `(x) EXECUTION_FAILED` + full error message |

> **After an execution failure the agent stops touching that ticket.** It won't retry on the
> next scan, because re-running the same broken query just fills the ticket with identical
> error comments. Other tickets carry on as normal. To restart it, tag the agent:
> `@agent <instruction>`, `@agent REJECT <reason>`, or paste corrected SQL in a
> `{code:sql}` block followed by `@agent APPROVED`.

---

## Tips for writing good ticket descriptions

- **Be specific about what to check** — "confirm the column is non-null and has at least 100 distinct values" generates a better query than "check the column."
- **Use `table_name:` for the full path** — `project.dataset.table` avoids any ambiguity.
- **For SP tickets with placeholder variables**, always include `table_name:` pointing to the real output table.
- **A tagged comment can add requirements** — the agent merges the original description with anything you add in an `@agent` comment, so you can widen a validation without rewriting the ticket.
