# Design Spec: EOD Report Processor

## Overview

A Python batch script that runs nightly on Windows, reads EOD (End of Day) report emails from Gmail, uses Claude API to extract structured data from PDF attachments, and writes results into a Microsoft Fabric Warehouse.

**Source PRD:** `docs/EOD_Processor_PRD.md`

---

## Architecture

Linear batch pipeline with 4 modules, no classes — procedural functions only.

```
main.py  →  gmail_reader.py  →  claude_parser.py  →  fabric_writer.py
              ↓                      ↓                     ↓
         Gmail API              Claude API           Fabric Warehouse
         (OAuth2)            (claude-sonnet-4-6)    (Azure AD SP auth)
```

`config.py` holds all configuration, reading secrets from environment variables via `os.environ.get()`.

### Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Secrets management | Environment variables in `config.py` | Avoids hardcoding secrets; simple for Windows Task Scheduler |
| Development location | `D:\Source Codes\ai_coding\OWNDAYS AU Sales Data Extraction\` | Dev here, deploy to `C:\Users\stkuo\eod_processor\` later |
| Claude model | `claude-sonnet-4-6` | Latest model, better performance than PRD's `claude-sonnet-4-5-20250929` |
| Retry logic | None | Unread email acts as natural retry queue; keeps code simple |
| Code style | Procedural functions | Matches PRD, appropriate for a batch script |

---

## Data Flow

For each unread email with PDF attachments:

1. **gmail_reader** fetches email and returns:
   ```python
   {
       "message_id": str,
       "sender_name": str,
       "sender_email": str,
       "subject": str,
       "body": str,  # plain text
       "attachments": [{"filename": str, "data_base64": str}]  # standard base64
   }
   ```

2. **claude_parser** builds a multimodal prompt (PDF document blocks + email body + extraction prompt) and returns:
   ```python
   {
       "report_date": "YYYY-MM-DD",
       "store_name": str,
       "banking_no": str,
       "total_inc_gst": float,
       "total_tax": float,
       "total_exc_gst": float,
       "transaction_count": int,
       "target_exc_gst": float,       # from email body
       "consultation": float,          # from email body
       "no_customers": int,            # from email body
       "daily_comment": str,           # from email body
       "customer_feedback": str,       # from email body
       "transactions": [
           {
               "receipt_no": str,       # may be empty (e.g. DDEP rows)
               "payment_method": str,   # e.g. "MASTER", "HC", "DDEP"
               "customer_name": str,
               "customer_id": str,      # number without #, may be empty
               "amount_inc_tax": float,
               "tax": float,
               "amount_exc_tax": float
           }
       ],
       "sender_email": str,
       "sender_name": str,
       "message_id": str
   }
   ```
   Returns `None` on parse failure.

3. **fabric_writer** receives parsed dict:
   - Looks up `store_id` via `sender_email` in `Dim_Store`
   - If existing data for same `report_date` + `store_id`: deletes transactions then summary first
   - Inserts summary row, retrieves `summary_id` via `SELECT @@IDENTITY`
   - Inserts each transaction row (looks up `method_id` from `Dim_PaymentMethod`, nullable)
   - Commits on success, rollback on any failure
   - Returns `"success"` or `"error"`

4. **main.py** marks email as read only if step 3 returns `"success"`.

**Note:** Gmail returns urlsafe base64 for attachments. Must convert to standard base64 for Claude API.

---

## Error Handling

| Outcome | Mark as Read? | Action |
|---|---|---|
| No PDF attachments | Skip silently | Log debug |
| Claude returns bad JSON | No | Log error, count as failed |
| Store email not in Dim_Store | No | Log warning, skip |
| Duplicate (same store+date) | Yes | Log info, delete existing transactions + summary, re-insert |
| Fabric INSERT fails | No (rollback) | Log error, count as failed |
| Gmail token expired | N/A | Auto-refresh via google-auth |
| Fabric token expired | N/A | azure-identity handles refresh |

Key principle: email stays unread until data is safely in Fabric (or confirmed duplicate). The unread inbox is the work queue. Errors never crash the process — the loop continues to the next email.

---

## Module Contracts

### `config.py`

All config via `os.environ.get()`:

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | None (required) | |
| `AZURE_TENANT_ID` | `"6935b5fa-..."` | From PRD |
| `AZURE_CLIENT_ID` | `"254aedc1-..."` | From PRD |
| `AZURE_CLIENT_SECRET` | None (required) | |
| `CLAUDE_MODEL` | `"claude-sonnet-4-6"` | |
| `FABRIC_SERVER` | From PRD | |
| `FABRIC_DATABASE` | `"gold_warehouse"` | |
| `FABRIC_SCHEMA` | `"ownd"` | |
| `GMAIL_CREDENTIALS_FILE` | `"credentials.json"` | |
| `GMAIL_TOKEN_FILE` | `"gmail_token.json"` | |
| `LOG_FILE` | `"eod_processor.log"` | |

### `gmail_reader.py`

- `get_gmail_service()` → authenticated Gmail API service object
  - First run: opens browser for OAuth2 consent, saves token
  - Subsequent runs: loads token, auto-refreshes if expired
- `fetch_unread_emails(service)` → `list[dict]` of email dicts (see Data Flow)
  - Query: `is:unread has:attachment`
  - Skips emails with no PDF attachments
  - Converts urlsafe base64 → standard base64
- `mark_as_read(service, message_id)` → removes UNREAD label

### `claude_parser.py`

- `parse_eod_email(email_data)` → `dict | None`
  - Builds multimodal message: text label + document block per PDF, extraction prompt as final text
  - Handles markdown code fences in response
  - Returns `None` on JSON parse failure

### `fabric_writer.py`

- `write_eod_data(parsed_data)` → `str`: `"success"`, `"skipped"`, or `"error"`
  - Main entry point; orchestrates lookup, delete-if-exists, insert, commit/rollback
  - If duplicate found: deletes existing transactions (by `summary_id`) then summary (by `report_date` + `store_id`), logs info, then re-inserts
  - All within one transaction — atomic delete + re-insert
  - Returns `"success"` after insert+commit, `"error"` on any failure (rollback)
  - `main.py` marks email as read when result is `"success"`
- `_get_connection()` → pyodbc connection with Azure AD token auth
- `_get_store_id(cursor, sender_email)` → `int | None`
- `_get_method_id(cursor, method_code)` → `int | None` (nullable, don't fail)
- `_delete_existing(cursor, report_date, store_id)` → deletes `Fact_EOD_Transaction` then `Fact_EOD_Summary` for given date+store
- `_insert_summary(cursor, data, store_id)` → `summary_id` (via `SELECT @@IDENTITY`)
- `_insert_transactions(cursor, transactions, summary_id, store_id, report_date)` → None
- `_processed_at` set by Python: `datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")`

### `main.py`

- Configures logging: format `%(asctime)s [%(levelname)s] %(name)s: %(message)s`, file + stdout handlers
- Logs `===` separator at start/end of run
- For each email: parse → write → mark as read (conditional)
- Logs per-email: sender, subject, parse result, write result
- Logs final summary: success / failed / skipped counts

---

## Database Schema

Target: `ownd` schema in `gold_warehouse` on Microsoft Fabric.

Tables `Dim_Store`, `Dim_PaymentMethod`, `Fact_EOD_Summary`, `Fact_EOD_Transaction` already exist. See PRD for full column definitions.

Fabric constraints:
- `IDENTITY` columns: `BIGINT`, no seed/increment
- No `NVARCHAR` — use `VARCHAR`
- `DATETIME2` must specify precision: `DATETIME2(6)`
- No `DEFAULT` constraints
- Use `SELECT @@IDENTITY` (not `SCOPE_IDENTITY()` or `OUTPUT INSERTED`)

---

## File Structure

```
D:\Source Codes\ai_coding\OWNDAYS AU Sales Data Extraction\
├── main.py
├── config.py
├── gmail_reader.py
├── claude_parser.py
├── fabric_writer.py
├── requirements.txt
├── .env.example          # template (contents: ANTHROPIC_API_KEY=, AZURE_CLIENT_SECRET=)
├── credentials.json      # Gmail OAuth2 (user-provided, gitignored)
├── gmail_token.json      # auto-generated (gitignored)
└── docs/
    └── EOD_Processor_PRD.md
```

---

## Dependencies (`requirements.txt`)

```
anthropic
google-auth
google-auth-oauthlib
google-auth-httplib2
google-api-python-client
pyodbc
azure-identity
```
