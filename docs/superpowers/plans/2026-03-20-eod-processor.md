# EOD Report Processor Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python batch script that reads EOD report emails from Gmail, extracts data from PDF attachments via Claude API, and writes results into Microsoft Fabric Warehouse.

**Architecture:** Linear pipeline of 4 procedural modules (`gmail_reader` → `claude_parser` → `fabric_writer`) orchestrated by `main.py`. Config via environment variables. No classes, no retry logic — unread emails serve as natural retry queue.

**Tech Stack:** Python 3.11+, Gmail API (OAuth2), Anthropic SDK (`claude-sonnet-4-6`), pyodbc + azure-identity (Fabric Warehouse), Windows Task Scheduler

**Spec:** `docs/superpowers/specs/2026-03-20-eod-processor-design.md`
**PRD:** `docs/EOD_Processor_PRD.md`

---

### Task 1: Project scaffolding — `requirements.txt`, `.env.example`, `config.py`

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `config.py`

- [ ] **Step 1: Create `requirements.txt`**

```
anthropic
google-auth
google-auth-oauthlib
google-auth-httplib2
google-api-python-client
pyodbc
azure-identity
```

- [ ] **Step 2: Create `.gitignore`**

```
credentials.json
gmail_token.json
.env
eod_processor.log
__pycache__/
```

- [ ] **Step 3: Create `.env.example`**

This is a reference template for setting OS environment variables (e.g. via Windows system settings or Task Scheduler). No `python-dotenv` is used.

```
# Required — set these as OS environment variables
ANTHROPIC_API_KEY=
AZURE_CLIENT_SECRET=
```

- [ ] **Step 4: Create `config.py`**

All configuration via `os.environ.get()`. Required secrets are validated at import time with clear error messages. Non-secret values have defaults from the PRD.

```python
import os

# Anthropic
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# Azure AD Service Principal
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID", "6935b5fa-b1fc-433f-91d1-74905254de17")
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "254aedc1-a707-4768-b552-b9f07c28eafb")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET")

# Fabric Warehouse
FABRIC_SERVER = os.environ.get(
    "FABRIC_SERVER",
    "7k2tk2p4we7uheorosifevg6c4-5mvpeldfy2pepno5srpjp3k564.datawarehouse.fabric.microsoft.com",
)
FABRIC_DATABASE = os.environ.get("FABRIC_DATABASE", "gold_warehouse")
FABRIC_SCHEMA = os.environ.get("FABRIC_SCHEMA", "ownd")

# Gmail
GMAIL_CREDENTIALS_FILE = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "gmail_token.json")

# Logging
LOG_FILE = os.environ.get("LOG_FILE", "eod_processor.log")

# Validate required secrets
if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY environment variable is required")
if not AZURE_CLIENT_SECRET:
    raise RuntimeError("AZURE_CLIENT_SECRET environment variable is required")
```

- [ ] **Step 5: Commit**

```bash
git add requirements.txt .gitignore .env.example config.py
git commit -m "feat: add project scaffolding — requirements, config, env template, gitignore"
```

---

### Task 2: Gmail reader — `gmail_reader.py`

**Files:**
- Create: `gmail_reader.py`

**Dependencies:** `config.py` (Task 1)

**Reference docs:**
- Gmail API Python quickstart: https://developers.google.com/gmail/api/quickstart/python
- Gmail API message format: messages have `payload.headers` for sender/subject, `payload.parts` for body/attachments
- Attachment data from Gmail is urlsafe base64 — must convert to standard base64 for Claude API

- [ ] **Step 1: Create `gmail_reader.py`**

Three functions:

**`get_gmail_service()`**
- Load token from `config.GMAIL_TOKEN_FILE` if exists
- If token missing or invalid: run `InstalledAppFlow` from `config.GMAIL_CREDENTIALS_FILE` with scope `https://www.googleapis.com/auth/gmail.modify`
- If token expired but has refresh token: auto-refresh
- Save updated token back to file
- Return `build("gmail", "v1", credentials=creds)`

**`fetch_unread_emails(service)`**
- Query: `is:unread has:attachment`
- For each message: get full message via `service.users().messages().get(userId="me", id=msg_id, format="full")`
- Extract from headers: `From` (parse into `sender_name` and `sender_email`), `Subject`
- Extract plain text body from `payload.parts` (look for `mimeType == "text/plain"`)
- Extract PDF attachments: iterate parts where `mimeType == "application/pdf"`, get attachment data via `service.users().messages().attachments().get()`
- Convert attachment data from urlsafe base64 to standard base64: `base64.b64encode(base64.urlsafe_b64decode(data)).decode("utf-8")`
- Skip emails with no PDF attachments (log debug)
- Return list of email dicts:
  ```python
  {
      "message_id": str,
      "sender_name": str,
      "sender_email": str,
      "subject": str,
      "body": str,
      "attachments": [{"filename": str, "data_base64": str}]
  }
  ```

**`mark_as_read(service, message_id)`**
- `service.users().messages().modify(userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]})`

**Important implementation details:**
- Parse `From` header: format is `"Name <email@example.com>"` — use `email.utils.parseaddr()`
- Body may be nested in `payload.parts` or directly in `payload.body` — handle both cases
- Some emails have multipart/alternative with text/plain + text/html — prefer text/plain
- Attachment `data` field may be empty in the message payload; must fetch separately via attachments().get() using `attachmentId`

- [ ] **Step 2: Commit**

```bash
git add gmail_reader.py
git commit -m "feat: add Gmail reader — fetch unread emails with PDF attachments"
```

---

### Task 3: Claude parser — `claude_parser.py`

**Files:**
- Create: `claude_parser.py`

**Dependencies:** `config.py` (Task 1)

**Reference:**
- Anthropic SDK: `anthropic.Anthropic()`, `client.messages.create()`
- PDF document blocks: `{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64_str}}`
- PRD sample data (for understanding the extraction prompt):
  ```
  Date        Receipt  Paid By  Patient/Payer              Amount(Inc Tax)  Tax    Amount(Exc Tax)
  19/03/2026  137977   MASTER   Poscai, Joanne (#52010)    177.00           7.10   169.90
  19/03/2026            DDEP    Medicare                    297.75           0.00   297.75
  Total Amount $                                            2,852.75         73.99  2,778.76
  ```
- DDEP/Medicare rows have no receipt_no and no customer_id

- [ ] **Step 1: Create `claude_parser.py`**

One public function:

**`parse_eod_email(email_data)`**
- Initialize `anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)`
- Build `content` list for the message:
  - For each PDF attachment in `email_data["attachments"]`:
    - Add text block: `{"type": "text", "text": f"PDF: {attachment['filename']}"}`
    - Add document block: `{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": attachment["data_base64"]}}`
  - Add final text block with the extraction prompt (see below)
- Call `client.messages.create(model=config.CLAUDE_MODEL, max_tokens=4096, messages=[{"role": "user", "content": content}])`
- Extract text response: `response.content[0].text`
- Strip markdown code fences if present: `re.sub(r"^```(?:json)?\s*\n?|\n?```\s*$", "", text.strip())`
- Parse JSON; on `json.JSONDecodeError`: log error, return `None`
- Add metadata from email_data: `sender_email`, `sender_name`, `message_id`
- Return the parsed dict

**Extraction prompt** (final text block):

```
You are extracting data from an End of Day (EOD) report email for an optical retail store.

The email contains PDF attachments. Find the **Banking Transaction Report** PDF — it has columns: Date, Receipt, Paid By, Patient/Payer, Amount Inc Tax, Tax, Amount Exc Tax. Ignore other PDFs (e.g. "Payment Detail by Payment Type").

Extract the following and return as a single JSON object (no markdown, no preamble, ONLY valid JSON):

From the email body text below:
- "report_date": the report date in YYYY-MM-DD format
- "store_name": the store name
- "target_exc_gst": daily target excluding GST (numeric, no $ or commas)
- "consultation": consultation count or value (numeric)
- "no_customers": number of customers (integer)
- "daily_comment": any daily comment text (string, empty string if none)
- "customer_feedback": any customer feedback text (string, empty string if none)

From the Banking Transaction Report PDF:
- "banking_no": the banking number from the PDF header
- "total_inc_gst": total amount including tax from the Total row (numeric, no $ or commas)
- "total_tax": total tax from the Total row (numeric, no $ or commas)
- "total_exc_gst": total amount excluding tax from the Total row (numeric, no $ or commas)
- "transaction_count": number of individual transaction rows (exclude sub-total and total rows)
- "transactions": array of objects, one per transaction row (exclude sub-total/total rows):
  - "receipt_no": receipt number (string, empty string if none e.g. for DDEP rows)
  - "payment_method": the Paid By code exactly as shown (e.g. "MASTER", "HC", "DDEP", "VISA", "EFTPOS", "CASH")
  - "customer_name": patient/payer name without the ID portion (string)
  - "customer_id": the number after # without the # symbol (string, empty string if none)
  - "amount_inc_tax": amount including tax (numeric, no $ or commas)
  - "tax": tax amount (numeric, no $ or commas)
  - "amount_exc_tax": amount excluding tax (numeric, no $ or commas)

Remove $ signs and commas from ALL numeric values. Return numbers as numbers, not strings.

EMAIL BODY:
{email_body}
```

The `{email_body}` placeholder is replaced with `email_data["body"]` at runtime using an f-string or `.format()`.

- [ ] **Step 2: Commit**

```bash
git add claude_parser.py
git commit -m "feat: add Claude parser — extract structured data from EOD email PDFs"
```

---

### Task 4: Fabric writer — `fabric_writer.py`

**Files:**
- Create: `fabric_writer.py`

**Dependencies:** `config.py` (Task 1)

**Reference:**
- Azure AD token auth pattern from PRD (see spec lines 204-221)
- Fabric Warehouse constraints: use `SELECT @@IDENTITY`, `BIGINT` identity, `VARCHAR` not `NVARCHAR`, `DATETIME2(6)`
- `_processed_at` set by Python: `datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")`
- All DB operations within a single transaction (autocommit=False)

- [ ] **Step 1: Create `fabric_writer.py` with connection and lookup helpers**

```python
import logging
import struct
from datetime import datetime

import pyodbc
from azure.identity import ClientSecretCredential

import config

logger = logging.getLogger(__name__)


def _get_connection():
    """Get pyodbc connection to Fabric Warehouse using Azure AD Service Principal."""
    credential = ClientSecretCredential(
        config.AZURE_TENANT_ID, config.AZURE_CLIENT_ID, config.AZURE_CLIENT_SECRET
    )
    token = credential.get_token("https://database.windows.net/.default")
    token_bytes = token.token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)

    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={config.FABRIC_SERVER},1433;"
        f"Database={config.FABRIC_DATABASE};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    conn = pyodbc.connect(conn_str, attrs_before={1256: token_struct})
    conn.autocommit = False
    return conn


def _get_store_id(cursor, sender_email):
    """Look up store_id from Dim_Store by sender_email. Returns int or None."""
    cursor.execute(
        f"SELECT store_id FROM {config.FABRIC_SCHEMA}.Dim_Store WHERE sender_email = ?",
        sender_email,
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _get_method_id(cursor, method_code):
    """Look up method_id from Dim_PaymentMethod by method_code. Returns int or None."""
    cursor.execute(
        f"SELECT method_id FROM {config.FABRIC_SCHEMA}.Dim_PaymentMethod WHERE method_code = ?",
        method_code,
    )
    row = cursor.fetchone()
    return row[0] if row else None
```

- [ ] **Step 2: Add `_delete_existing` function**

Deletes child transactions first, then parent summary. Uses the summary_id to link.

```python
def _delete_existing(cursor, report_date, store_id):
    """Delete existing EOD data for the given date and store (transactions first, then summary)."""
    # Find existing summary_id(s)
    cursor.execute(
        f"SELECT summary_id FROM {config.FABRIC_SCHEMA}.Fact_EOD_Summary "
        f"WHERE report_date = ? AND store_id = ?",
        report_date, store_id,
    )
    rows = cursor.fetchall()
    if not rows:
        return 0

    count = len(rows)
    for row in rows:
        summary_id = row[0]
        cursor.execute(
            f"DELETE FROM {config.FABRIC_SCHEMA}.Fact_EOD_Transaction WHERE summary_id = ?",
            summary_id,
        )
        cursor.execute(
            f"DELETE FROM {config.FABRIC_SCHEMA}.Fact_EOD_Summary WHERE summary_id = ?",
            summary_id,
        )

    logger.info("Deleted %d existing summary record(s) for date=%s store_id=%s", count, report_date, store_id)
    return count
```

- [ ] **Step 3: Add `_insert_summary` function**

```python
def _insert_summary(cursor, data, store_id):
    """Insert a row into Fact_EOD_Summary. Returns the new summary_id."""
    processed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        f"""INSERT INTO {config.FABRIC_SCHEMA}.Fact_EOD_Summary
            (report_date, store_id, banking_no, total_inc_gst, total_tax, total_exc_gst,
             transaction_count, target_exc_gst, consultation, no_customers,
             daily_comment, customer_feedback, sender_email, _processed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        data["report_date"],
        store_id,
        data.get("banking_no"),
        data.get("total_inc_gst"),
        data.get("total_tax"),
        data.get("total_exc_gst"),
        data.get("transaction_count"),
        data.get("target_exc_gst"),
        data.get("consultation"),
        data.get("no_customers"),
        data.get("daily_comment"),
        data.get("customer_feedback"),
        data.get("sender_email"),
        processed_at,
    )
    cursor.execute("SELECT @@IDENTITY")
    summary_id = cursor.fetchone()[0]
    logger.info("Inserted summary_id=%s", summary_id)
    return summary_id
```

- [ ] **Step 4: Add `_insert_transactions` function**

```python
def _insert_transactions(cursor, transactions, summary_id, store_id, report_date):
    """Insert transaction rows into Fact_EOD_Transaction."""
    processed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for txn in transactions:
        method_code = txn.get("payment_method", "")
        method_id = _get_method_id(cursor, method_code) if method_code else None

        cursor.execute(
            f"""INSERT INTO {config.FABRIC_SCHEMA}.Fact_EOD_Transaction
                (summary_id, report_date, store_id, receipt_no, method_id, method_code,
                 customer_name, customer_id, amount_inc_tax, tax, amount_exc_tax, _processed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            summary_id,
            report_date,
            store_id,
            txn.get("receipt_no"),
            method_id,
            method_code,
            txn.get("customer_name"),
            txn.get("customer_id"),
            txn.get("amount_inc_tax"),
            txn.get("tax"),
            txn.get("amount_exc_tax"),
            processed_at,
        )
    logger.info("Inserted %d transactions for summary_id=%s", len(transactions), summary_id)
```

- [ ] **Step 5: Add `write_eod_data` main entry point**

```python
def write_eod_data(parsed_data):
    """
    Write parsed EOD data to Fabric Warehouse.
    Returns "success", "skipped", or "error".
    "skipped" means store not found in Dim_Store.
    """
    try:
        conn = _get_connection()
        cursor = conn.cursor()

        # Look up store
        sender_email = parsed_data.get("sender_email", "")
        store_id = _get_store_id(cursor, sender_email)
        if store_id is None:
            logger.warning("Store not found for sender_email=%s — skipping", sender_email)
            conn.close()
            return "skipped"

        report_date = parsed_data["report_date"]

        # Delete existing data if any (atomic replace)
        _delete_existing(cursor, report_date, store_id)

        # Insert new data
        summary_id = _insert_summary(cursor, parsed_data, store_id)
        _insert_transactions(
            cursor, parsed_data.get("transactions", []), summary_id, store_id, report_date
        )

        conn.commit()
        logger.info("Successfully wrote EOD data for date=%s store_id=%s", report_date, store_id)
        conn.close()
        return "success"

    except Exception:
        logger.exception("Failed to write EOD data to Fabric")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return "error"
```

- [ ] **Step 6: Commit**

```bash
git add fabric_writer.py
git commit -m "feat: add Fabric writer — delete-and-reinsert EOD data to warehouse"
```

---

### Task 5: Main orchestrator — `main.py`

**Files:**
- Create: `main.py`

**Dependencies:** All previous tasks (1-4)

- [ ] **Step 1: Create `main.py`**

```python
import logging
import sys

import config
import gmail_reader
import claude_parser
import fabric_writer


def setup_logging():
    """Configure logging to file and stdout."""
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # File handler
    fh = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Stdout handler
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(formatter)
    logger.addHandler(sh)


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("EOD Processor — starting run")
    logger.info("=" * 60)

    success_count = 0
    failed_count = 0
    skipped_count = 0

    try:
        service = gmail_reader.get_gmail_service()
        emails = gmail_reader.fetch_unread_emails(service)
        logger.info("Found %d unread emails with PDF attachments", len(emails))
    except Exception:
        logger.exception("Failed to fetch emails from Gmail")
        return

    for email_data in emails:
        sender = email_data.get("sender_email", "unknown")
        subject = email_data.get("subject", "no subject")
        message_id = email_data["message_id"]
        logger.info("Processing email from=%s subject=%s", sender, subject)

        # Parse with Claude
        parsed = claude_parser.parse_eod_email(email_data)
        if parsed is None:
            logger.error("Failed to parse email from=%s — skipping", sender)
            failed_count += 1
            continue

        logger.info("Parsed: date=%s store=%s transactions=%d",
                     parsed.get("report_date"), parsed.get("store_name"),
                     len(parsed.get("transactions", [])))

        # Write to Fabric
        result = fabric_writer.write_eod_data(parsed)

        if result == "success":
            gmail_reader.mark_as_read(service, message_id)
            logger.info("Success — email marked as read")
            success_count += 1
        elif result == "skipped":
            logger.warning("Skipped email from=%s — store not found, NOT marked as read", sender)
            skipped_count += 1
        else:
            logger.error("Failed to write data for email from=%s — NOT marked as read", sender)
            failed_count += 1

    logger.info("=" * 60)
    logger.info("EOD Processor — run complete: success=%d failed=%d skipped=%d",
                success_count, failed_count, skipped_count)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add main.py
git commit -m "feat: add main orchestrator — full EOD processing pipeline"
```

---

### Task 6: Manual end-to-end verification

**Dependencies:** All previous tasks

- [ ] **Step 1: Set environment variables**

Ensure `ANTHROPIC_API_KEY` and `AZURE_CLIENT_SECRET` are set in the environment.
Ensure `credentials.json` (Gmail OAuth2) is in the project directory.

- [ ] **Step 2: Install dependencies**

```bash
pip install -r requirements.txt
```

- [ ] **Step 3: Run the script**

```bash
python main.py
```

First run will open browser for Gmail OAuth2 consent. After approval, `gmail_token.json` is created.

- [ ] **Step 4: Verify results**

Check `eod_processor.log` for:
- Emails fetched count
- Per-email parse results (date, store, transaction count)
- Successful writes to Fabric
- Final summary line

Check Fabric Warehouse:
```sql
SELECT * FROM ownd.Fact_EOD_Summary ORDER BY _processed_at DESC;
SELECT * FROM ownd.Fact_EOD_Transaction ORDER BY _processed_at DESC;
```

- [ ] **Step 5: Test idempotency — run again**

```bash
python main.py
```

Verify: same emails are re-processed (delete + re-insert), data is correct, no duplicates.
