"""Quick test: fetch emails for a date, parse them, write JSON. Does NOT mark as read."""

import email.utils
import json
import logging
import sys

sys.path.insert(0, ".")

import config
import gmail_reader
import claude_parser
import json_writer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Store emails to filter by
STORE_EMAILS = set(config.STORE_MAP.keys())


def process_one(service, msg_id):
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}

    sender_name, sender_email = email.utils.parseaddr(headers.get("From", ""))
    subject = headers.get("Subject", "")

    if sender_email not in STORE_EMAILS:
        return

    body = gmail_reader._extract_text_body(payload)
    pdf_attachments = gmail_reader._extract_pdf_attachments(service, "me", msg_id, payload)

    if not pdf_attachments:
        print(f"  SKIP {sender_email} — no PDF attachments")
        return

    email_data = {
        "message_id": msg_id,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "subject": subject,
        "body": body,
        "attachments": pdf_attachments,
    }

    print(f"\n{'='*60}")
    print(f"From: {sender_email}")
    print(f"Subject: {subject}")
    print(f"Attachments: {[a['filename'] for a in pdf_attachments]}")

    # Parse
    parsed = claude_parser.parse_eod_email(email_data)
    if parsed is None:
        print("ERROR: Failed to parse email.")
        return

    print("--- Parsed data ---")
    summary = {k: v for k, v in parsed.items() if k != "transactions"}
    print(json.dumps(summary, indent=2))
    print(f"Transactions: {len(parsed.get('transactions', []))}")

    # Write JSON
    result, file_path = json_writer.write_json(parsed)
    print(f"Result: {result}")
    if file_path:
        print(f"File: {file_path}")


def main():
    # Default: yesterday. Override with command-line arg like "2026/03/31"
    target_date = "2026/03/31"
    if len(sys.argv) > 1:
        target_date = sys.argv[1]

    # Build date range query (Gmail after/before are exclusive/inclusive boundaries)
    # For "2026/03/31" we want after:2026/03/30 before:2026/04/01
    from datetime import datetime, timedelta
    dt = datetime.strptime(target_date, "%Y/%m/%d")
    after = (dt - timedelta(days=1)).strftime("%Y/%m/%d")
    before = (dt + timedelta(days=1)).strftime("%Y/%m/%d")

    query = f"has:attachment after:{after} before:{before}"
    print(f"Query: {query}")
    print(f"Looking for store emails: {STORE_EMAILS}")

    service = gmail_reader.get_gmail_service()

    response = service.users().messages().list(
        userId="me", q=query, maxResults=50
    ).execute()
    messages = response.get("messages", [])
    print(f"Found {len(messages)} emails with attachments for {target_date}")

    if not messages:
        print("No emails found.")
        return

    for m in messages:
        process_one(service, m["id"])

    print(f"\n{'='*60}")
    print("Done.")


if __name__ == "__main__":
    main()
