"""Process a local PDF file directly (no Gmail) and write the output JSON.

Usage:
    python src/process_pdf.py "docs/check/20260601/BankingTransactionReport (52).pdf" [sender_email]

If sender_email is omitted, the first store in STORE_MAP is used.
"""

import base64
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, ".")

import config
import claude_parser
import json_writer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def main():
    if len(sys.argv) < 2:
        print('Usage: python src/process_pdf.py "<pdf_path>" [sender_email]')
        return

    pdf_path = Path(sys.argv[1])
    if not pdf_path.is_file():
        print(f"File not found: {pdf_path}")
        return

    sender_email = sys.argv[2] if len(sys.argv) > 2 else next(iter(config.STORE_MAP))
    store_code = config.STORE_MAP.get(sender_email, "<unknown>")

    data_base64 = base64.b64encode(pdf_path.read_bytes()).decode("ascii")

    email_data = {
        "message_id": pdf_path.name,
        "sender_name": "",
        "sender_email": sender_email,
        "subject": "",
        "body": "",
        "attachments": [{"filename": pdf_path.name, "data_base64": data_base64}],
    }

    print(f"PDF: {pdf_path.name}")
    print(f"Store: {sender_email} -> {store_code}")

    parsed = claude_parser.parse_eod_email(email_data)
    if parsed is None:
        print("ERROR: Failed to parse PDF.")
        return

    summary = {k: v for k, v in parsed.items() if k != "transactions"}
    print("--- Parsed summary ---")
    print(json.dumps(summary, indent=2))
    print(f"Transactions (raw rows): {len(parsed.get('transactions', []))}")

    result, file_path = json_writer.write_json(parsed)
    print(f"Result: {result}")
    if file_path:
        print(f"File: {file_path}")


if __name__ == "__main__":
    main()
