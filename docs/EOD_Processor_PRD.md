# PRD: EOD Report Processor

## Overview

Build a Python automation script that runs nightly on a Windows machine (`C:\Users\stkuo\`), reads EOD (End of Day) report emails from Gmail, uses Claude API to extract data from PDF attachments, and writes the results into a Microsoft Fabric Warehouse via Azure AD Service Principal authentication.

---

## Problem Statement

An optical retail chain (Owndays Australia) sends daily EOD report emails from each store. Each email contains PDF attachments including a **Banking Transaction Report** with individual sales transactions. Currently this data is not centralised. The goal is to automatically parse these emails and load the data into Microsoft Fabric for reporting.

---

## Technical Stack

| Component | Technology |
|---|---|
| Language | Python 3.11+ |
| Email | Gmail API (OAuth2) |
| PDF parsing | Claude API (`claude-sonnet-4-5-20250929`) |
| Database | Microsoft Fabric Warehouse (SQL endpoint via pyodbc + Azure AD token) |
| Scheduler | Windows Task Scheduler |
| Auth (Fabric) | Azure AD Service Principal |
| Auth (Gmail) | OAuth2 with offline refresh token |

---

## Project Structure

```
C:\Users\stkuo\eod_processor\
├── main.py               # Orchestrator — entry point
├── config.py             # All config/secrets
├── gmail_reader.py       # Gmail API: fetch unread emails + PDF attachments
├── claude_parser.py      # Claude API: extract structured data from email + PDFs
├── fabric_writer.py      # Fabric Warehouse: write Summary + Transactions
├── requirements.txt      # Python dependencies
├── credentials.json      # Gmail OAuth2 client credentials (from Google Cloud Console)
└── gmail_token.json      # Auto-generated after first OAuth2 login
```

---

## Configuration (`config.py`)

```python
# Anthropic
ANTHROPIC_API_KEY = "YOUR_KEY"
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

# Azure AD Service Principal
AZURE_TENANT_ID = "6935b5fa-b1fc-433f-91d1-74905254de17"
AZURE_CLIENT_ID = "254aedc1-a707-4768-b552-b9f07c28eafb"
AZURE_CLIENT_SECRET = "YOUR_SECRET"

# Fabric Warehouse
FABRIC_SERVER = "7k2tk2p4we7uheorosifevg6c4-5mvpeldfy2pepno5srpjp3k564.datawarehouse.fabric.microsoft.com"
FABRIC_DATABASE = "gold_warehouse"
FABRIC_SCHEMA = "ownd"

# Gmail
GMAIL_CREDENTIALS_FILE = "credentials.json"
GMAIL_TOKEN_FILE = "gmail_token.json"

# Logging
LOG_FILE = "eod_processor.log"
```

---

## Database Schema (already created in Fabric)

Schema: `ownd` in database `gold_warehouse`

### `ownd.Dim_Store`
| Column | Type | Notes |
|---|---|---|
| store_id | BIGINT IDENTITY | PK |
| store_code | VARCHAR(20) | |
| store_name | VARCHAR(100) | |
| city | VARCHAR(50) | |
| is_active | BIT | |
| created_at | DATETIME2(6) | |
| sender_email | VARCHAR(200) | Used to match incoming emails to store |

**Existing data:**
| store_id | store_code | store_name | sender_email |
|---|---|---|---|
| 2882303761517117441 | SYD | Owndays Sydney | sydney@owndays.com.au |
| 2882303761517117442 | BWD | Owndays Burwood | owndaysburwood@gmail.com |
| 2882303761517117443 | HVL | OWNDAYS Westfield Hurstville | westfieldhurstville@owndays.com |
| 2882303761517117444 | CWD | Owndays Chatswood | chatswood@owndays.com.au |

### `ownd.Dim_PaymentMethod`
| Column | Type |
|---|---|
| method_id | BIGINT IDENTITY |
| method_code | VARCHAR(20) |
| method_name | VARCHAR(50) |
| method_type | VARCHAR(30) |
| is_active | BIT |

**Existing data:** HC, MASTER, VISA, EFTPOS, DDEP, zMP, CASH

### `ownd.Fact_EOD_Summary`
| Column | Type | Notes |
|---|---|---|
| summary_id | BIGINT IDENTITY | PK |
| report_date | DATE | NOT NULL |
| store_id | BIGINT | FK → Dim_Store |
| banking_no | VARCHAR(20) | From PDF header |
| total_inc_gst | DECIMAL(12,2) | |
| total_tax | DECIMAL(12,2) | |
| total_exc_gst | DECIMAL(12,2) | |
| transaction_count | INT | |
| target_exc_gst | DECIMAL(12,2) | From email body |
| consultation | DECIMAL(12,2) | From email body |
| no_customers | INT | From email body |
| daily_comment | VARCHAR(2000) | From email body |
| customer_feedback | VARCHAR(2000) | From email body |
| sender_email | VARCHAR(200) | |
| _processed_at | DATETIME2(6) | Set by Python, not DB default |

### `ownd.Fact_EOD_Transaction`
| Column | Type | Notes |
|---|---|---|
| transaction_id | BIGINT IDENTITY | PK |
| summary_id | BIGINT | FK → Fact_EOD_Summary |
| report_date | DATE | NOT NULL |
| store_id | BIGINT | NOT NULL |
| receipt_no | VARCHAR(20) | |
| method_id | BIGINT | FK → Dim_PaymentMethod (nullable) |
| method_code | VARCHAR(20) | Raw code from PDF, always populated |
| customer_name | VARCHAR(200) | |
| customer_id | VARCHAR(20) | ID number from PDF (without #) |
| amount_inc_tax | DECIMAL(12,2) | |
| tax | DECIMAL(12,2) | |
| amount_exc_tax | DECIMAL(12,2) | |
| _processed_at | DATETIME2(6) | Set by Python |

> ⚠️ Fabric Warehouse constraints:
> - `IDENTITY` columns must be `BIGINT` with no seed/increment specified
> - No `NVARCHAR` — use `VARCHAR`
> - `DATETIME2` must specify precision e.g. `DATETIME2(6)`
> - No `DEFAULT` constraints supported
> - PKs use `PRIMARY KEY NONCLUSTERED (col) NOT ENFORCED`
> - Use `SELECT @@IDENTITY` to retrieve last inserted identity (not `SCOPE_IDENTITY()` or `OUTPUT INSERTED`)

---

## Module Specifications

### `main.py`
Entry point. Orchestrates the full pipeline:
1. Initialise logging (to file + stdout)
2. Connect to Gmail, fetch unread emails with PDF attachments
3. For each email:
   a. Call Claude to parse email body + PDFs
   b. Write parsed data to Fabric (Summary then Transactions)
   c. If write succeeds → mark email as read
4. Log summary: success / failed / skipped counts

### `gmail_reader.py`
- Use `google-auth`, `google-auth-oauthlib`, `google-api-python-client`
- OAuth2 scopes: `https://www.googleapis.com/auth/gmail.modify`
- On first run: open browser for user consent, save token to `gmail_token.json`
- On subsequent runs: auto-refresh token silently
- Query: `is:unread has:attachment`
- For each email: extract sender name, sender email, subject, plain text body
- Download all PDF attachments as base64 (standard base64, not urlsafe — Claude API requires standard)
- Skip emails with no PDF attachments
- `mark_as_read(service, message_id)`: remove UNREAD label

### `claude_parser.py`
- Use `anthropic` Python SDK
- Build multimodal message: for each PDF, add a text label then a `document` block with `type: base64`, `media_type: application/pdf`
- Append the extraction prompt as the final text block
- Parse the JSON response (handle markdown code fences if present)
- Return a dict with all extracted fields plus `sender_email`, `sender_name`, `message_id` from email metadata

**Extraction prompt instructs Claude to:**
- Find the **Banking Transaction Report** PDF (has columns: Date, Receipt, Paid By, Patient/Payer, Amount Inc Tax, Tax, Amount Exc Tax)
- Ignore other PDFs (e.g. Payment Detail by Payment Type)
- From email body: `report_date` (YYYY-MM-DD), `store_name`, `target_exc_gst`, `consultation`, `no_customers`, `daily_comment`, `customer_feedback`
- From Banking Transaction Report PDF: `banking_no`, `total_inc_gst`, `total_tax`, `total_exc_gst`, `transaction_count` (exclude sub-total/total rows), `transactions` array
- Each transaction: `receipt_no`, `payment_method`, `customer_name`, `customer_id` (number after #, without #), `amount_inc_tax`, `tax`, `amount_exc_tax`
- Remove $ and commas from all numeric values
- Return ONLY valid JSON, no markdown, no preamble

**Sample Banking Transaction Report data (for reference):**
```
Date        Receipt  Paid By  Patient/Payer              Amount(Inc Tax)  Tax    Amount(Exc Tax)
19/03/2026  137977   MASTER   Poscai, Joanne (#52010)    177.00           7.10   169.90
19/03/2026  137978   HC       Pocsai, Ashleigh M (#52669) 200.00          7.81   192.19
19/03/2026            DDEP    Medicare                    297.75           0.00   297.75
Total Amount $                                            2,852.75         73.99  2,778.76
```

Note: DDEP/Medicare rows have no receipt_no and no customer_id.

### `fabric_writer.py`
- Use `pyodbc` + `azure-identity` (`ClientSecretCredential`)
- **Azure AD token auth pattern:**
  ```python
  from azure.identity import ClientSecretCredential
  import struct, pyodbc

  credential = ClientSecretCredential(tenant_id, client_id, client_secret)
  token = credential.get_token("https://database.windows.net/.default")
  token_bytes = token.token.encode("utf-16-le")
  token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)

  conn_str = (
      "Driver={ODBC Driver 18 for SQL Server};"
      f"Server={FABRIC_SERVER},1433;"
      f"Database={FABRIC_DATABASE};"
      "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
  )
  conn = pyodbc.connect(conn_str, attrs_before={1256: token_struct})
  ```
- `get_store_id(cursor, sender_email)` → query `ownd.Dim_Store WHERE sender_email = ?`
- `get_method_id(cursor, method_code)` → query `ownd.Dim_PaymentMethod WHERE method_code = ?` (nullable, don't fail if not found)
- `check_duplicate(cursor, report_date, store_id)` → return True if record already exists in `Fact_EOD_Summary`
- `insert_summary(cursor, data, store_id)` → INSERT, then `SELECT @@IDENTITY` to get `summary_id`
- `insert_transactions(cursor, transactions, summary_id, store_id, report_date)` → loop INSERT each row
- `write_eod_data(parsed_data)` → main entry: get store_id → check duplicate → insert summary → insert transactions → commit. On any exception: rollback, log error, return False
- `_processed_at` is always set by Python: `datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")`

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| Email has no PDF | Skip silently, log debug |
| Claude returns invalid JSON | Log error, count as failed, do NOT mark as read |
| Store email not in Dim_Store | Log warning, skip email, do NOT mark as read |
| Duplicate (same store + date) | Log warning, skip, mark as read (already processed) |
| Fabric INSERT fails | Rollback transaction, log error, do NOT mark as read |
| Gmail token expired | Auto-refresh via google-auth |
| Fabric token expired | azure-identity handles refresh automatically |

---

## Logging

- Format: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`
- Handlers: file (`eod_processor.log`) + stdout
- Log start/end of each run with `===` separator
- Log per-email: sender, subject, parse result, write result
- Log final summary: success / failed / skipped counts

---

## Requirements (`requirements.txt`)

```
anthropic
google-auth
google-auth-oauthlib
google-auth-httplib2
google-api-python-client
pyodbc
azure-identity
```

---

## Installation & Setup

### Prerequisites
- Python 3.11+
- ODBC Driver 18 for SQL Server installed on Windows
- `credentials.json` from Google Cloud Console (Gmail OAuth2 Desktop app credentials)

### Setup steps
```cmd
cd C:\Users\stkuo\eod_processor
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### First run (Gmail OAuth2 consent)
```cmd
python main.py
```
Browser will open for Gmail OAuth2 consent. After approval, `gmail_token.json` is saved. Subsequent runs are silent.

---

## Success Criteria

- [ ] Script runs without error on `python main.py`
- [ ] Correctly identifies Banking Transaction Report PDF vs other PDFs
- [ ] All 10 transactions from sample report are inserted correctly
- [ ] Duplicate runs on same day are safely skipped
- [ ] Unknown store emails are skipped without crashing
- [ ] Email is only marked as read after successful Fabric write
- [ ] Log file clearly shows what happened each run

---

## Out of Scope

- Payment Detail by Payment Type PDF (Frame/Lens/CL breakdown) — future phase
- Web dashboard or reporting UI
- Email sending / alerting on failure
- Multi-environment (dev/prod) config management
- Unit tests
