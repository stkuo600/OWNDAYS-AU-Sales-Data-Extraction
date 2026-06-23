# OWNDAYS AU Sales Data Extraction

## Overview

Automated EOD (End of Day) Report Processor for OWNDAYS optical retail stores in Australia. Reads daily sales report emails from Gmail, extracts structured data from Banking Transaction Report PDFs using Claude AI, outputs JSON files locally and uploads them to an SFTP server.

## Architecture

Linear batch pipeline — 5 procedural modules orchestrated by `main.py`:

```
Gmail API (OAuth2) → gmail_reader → claude_parser → json_writer → ftp_uploader → SFTP Server
```

No classes, no retry logic. Unread emails serve as the natural retry queue — emails are only marked as read after successful JSON write and SFTP upload.

## Project Structure

```
├── src/
│   ├── config.py           # All settings from .env via dotenv_values()
│   ├── main.py             # Entry point and orchestrator
│   ├── gmail_reader.py     # Gmail API: fetch unread emails with PDF attachments
│   ├── claude_parser.py    # Claude API: extract structured data from BankingTransactionReport PDFs
│   ├── json_writer.py      # Transform parsed data → JSON files saved locally
│   └── ftp_uploader.py     # Upload JSON files to SFTP server (despite the name)
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
- **python-dotenv** — config from `.env` file via `dotenv_values()` (no `os.environ`)
- **paramiko** — SFTP (SSH) upload. Host key trust-on-first-use via `AutoAddPolicy`, cached to `~/.ssh/known_hosts`

## Key Conventions

### Configuration

All config lives in `.env`, read by `config.py` using `dotenv_values()`. No `os.environ.get()`. File paths (credentials, token, log) resolve relative to project root.

### Store Mapping

Store email → store code mapping is configured via `STORE_MAP` in `.env` as a JSON object:
```
STORE_MAP={"sydney@owndays.com.au": "OWND01", "owndaysburwood@gmail.com": "OWND02"}
```

### JSON Output Format

One JSON file per store per day: `AU_Owndays_{StoreCode}_{YYYYMMDD}_{HHmmss}.json`

Each file is an array of transaction objects:
```json
[
    {
        "PartnerTransaction_ID": "137976",
        "StoreCode": "OWND03",
        "TxDate": "2026-03-19T00:00:00",
        "Register_ID": 1,
        "LineItems": [{"LineNumber": 1, "Item": {"Type": "Product", "ProductCodeType": "PLU_ColorSize", "ProductCode": "Dummy", "ColorDesc": "NA", "SizeDesc": "NA", "Qty": 1, "RetailPrice": 200.00, "SoldPrice": 181.82}}],
        "Payment": [{"PaymentMethodCode": "VISA", "Amount": 181.82}],
        "Tax": {"TaxAmount": 18.18, "TaxIncludedInLineItem": true}
    }
]
```

RetailPrice, SoldPrice, and Payment Amount all use Amount (Inc Tax). TaxAmount uses Tax from the Banking Transaction Report.

### Error Handling

- Parse failure → email stays unread, counted as failed
- JSON write failure → email stays unread, counted as failed
- SFTP upload failure → email stays unread, counted as failed
- Store not found in `STORE_MAP` → email stays unread, counted as skipped
- Gmail fetch failure → abort run, send error notification
- SMTP notification sent on completion (success or failure)

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
