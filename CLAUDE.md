# OWNDAYS AU Sales Data Extraction

## Overview

Automated EOD (End of Day) Report Processor for OWNDAYS optical retail stores in Australia. Reads daily sales report emails from Gmail, extracts structured data from PDF attachments using Claude AI, and writes results to Microsoft Fabric Warehouse.

## Architecture

Linear batch pipeline — 4 procedural modules orchestrated by `main.py`:

```
Gmail API (OAuth2) → gmail_reader → claude_parser → fabric_writer → Fabric Warehouse
```

No classes, no retry logic. Unread emails serve as the natural retry queue — emails are only marked as read after successful write to Fabric.

## Project Structure

```
├── src/
│   ├── config.py           # All settings from .env via dotenv_values()
│   ├── main.py             # Entry point and orchestrator
│   ├── gmail_reader.py     # Gmail API: fetch unread emails with PDF attachments
│   ├── claude_parser.py    # Claude API: extract structured data from email + PDFs
│   └── fabric_writer.py    # Fabric Warehouse: write summary + transactions
├── docs/                   # PRD, design spec, implementation plan
├── .env                    # Secrets and config (gitignored)
├── .env.example            # Template for .env
├── requirements.txt        # Python dependencies
├── credentials.json        # Gmail OAuth2 client credentials (gitignored)
└── gmail_token.json        # Gmail refresh token, auto-generated (gitignored)
```

## Running

```bash
pip install -r requirements.txt
python src/main.py
```

First run opens a browser for Gmail OAuth2 consent. Subsequent runs are non-interactive.

## Tech Stack

- **Python 3.12+**
- **Gmail API** — OAuth2 with offline refresh token (`gmail.modify` scope)
- **Anthropic SDK** — Claude claude-sonnet-4-6 for PDF data extraction
- **pyodbc + azure-identity** — Fabric Warehouse via Azure AD Service Principal
- **python-dotenv** — config from `.env` file via `dotenv_values()` (no `os.environ`)

## Key Conventions

### Configuration

All config lives in `.env`, read by `config.py` using `dotenv_values()`. No `os.environ.get()`. File paths (credentials, token, log) resolve relative to project root.

### Error Handling

- Parse failure → email stays unread, counted as failed
- Write failure → rollback, email stays unread, counted as failed
- Store not found in `Dim_Store` → email stays unread, counted as skipped
- Gmail fetch failure → abort run, send error notification
- SMTP notification sent on completion (success or failure)

### Fabric Warehouse Specifics

- Schema: `ownd`
- Tables: `Dim_Store`, `Dim_PaymentMethod`, `Fact_EOD_Summary`, `Fact_EOD_Transaction`
- Identity columns are auto-generated BIGINT — do NOT insert explicit values
- `@@IDENTITY`, `SCOPE_IDENTITY()`, and `OUTPUT INSERTED` are all unsupported
- Retrieve generated IDs by querying back with unique key (report_date + store_id + _processed_at)
- Use `VARCHAR` not `NVARCHAR`, `DATETIME2(6)` for timestamps
- Duplicate handling: atomic delete-and-reinsert (transactions first, then summary)

### Gmail Data

- Attachment data from Gmail is urlsafe base64 — must convert to standard base64 for Claude API
- Email body: prefer `text/plain` over `text/html`, recursively handle multipart structures
- Pagination: always handle `nextPageToken` when listing messages

### Logging

- Format: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`
- File handler: DEBUG level, stdout handler: INFO level
- Log file: `eod_processor.log` in project root

## Stores

| Store | Email |
|-------|-------|
| Sydney | sydney@owndays.com.au |
| Burwood | owndaysburwood@gmail.com |
| Hurstville | westfieldhurstville@owndays.com |
| Chatswood | chatswood@owndays.com.au |
