"""process_missing_report.py — manually reprocess a missing EOD report PDF.

Use when a store's EOD email never arrived (so the normal Graph pipeline never
saw it) but you have the Banking Transaction Report PDF on hand. Runs the SAME
pipeline modules as main.py for a single local PDF, then archives the PDF so the
"did we file it away?" step can't be forgotten:

    parse (claude_parser) -> verify total -> write JSON (json_writer)
        -> upload SFTP (ftp_uploader) -> archive PDF

Usage:
    python scripts/process_missing_report.py "<path-to.pdf>" [options]

    --store OWND04     Force the store code (default: infer from the filename
                       via STORE_NAME_MAP, e.g. a name containing "chatswood").
    --no-upload        Parse + write JSON only; skip SFTP (dry run for review).
    --no-archive       Leave the PDF where it is (don't move into docs/check/archive).
    --archive-label X  Folder suffix under docs/check/archive/{today}-{X}
                       (default: "{store-name}-missing").

Exit code 0 only when every requested step succeeded.
"""

import argparse
import base64
import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

import config  # noqa: E402
import claude_parser  # noqa: E402
import json_writer  # noqa: E402
import ftp_uploader  # noqa: E402

ARCHIVE_ROOT = _PROJECT_ROOT / "docs" / "check" / "archive"


def _infer_store(pdf_name, override):
    """Return (store_code, store_name_keyword) for this PDF.

    Override wins. Otherwise match a STORE_NAME_MAP keyword (the lowercase store
    name) as a substring of the filename — the same keywords resolve_store_code
    uses, so behaviour matches the live pipeline.
    """
    low = pdf_name.lower()
    if override:
        # Reverse-lookup a friendly name for the archive label, if we have one.
        name = next((kw for kw, c in config.STORE_NAME_MAP.items() if c == override), override)
        return override, name
    matched = {kw: c for kw, c in config.STORE_NAME_MAP.items() if kw.lower() in low}
    if len(matched) == 1:
        kw, code = next(iter(matched.items()))
        return code, kw
    if not matched:
        sys.exit(f"ERROR: could not infer store from filename '{pdf_name}'. "
                 f"Pass --store explicitly (one of {sorted(set(config.STORE_NAME_MAP.values()))}).")
    sys.exit(f"ERROR: filename '{pdf_name}' matches multiple stores {matched}. Pass --store.")


def main():
    ap = argparse.ArgumentParser(description="Reprocess a single missing EOD report PDF.")
    ap.add_argument("pdf", help="Path to the Banking Transaction Report PDF.")
    ap.add_argument("--store", help="Force store code (e.g. OWND04). Default: infer from filename.")
    ap.add_argument("--no-upload", action="store_true", help="Skip SFTP upload (review JSON first).")
    ap.add_argument("--no-archive", action="store_true", help="Do not move the PDF into the archive.")
    ap.add_argument("--archive-label", help="Suffix for docs/check/archive/{today}-{label}.")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    if not pdf.is_file():
        sys.exit(f"ERROR: PDF not found: {pdf}")

    store_code, store_name = _infer_store(pdf.name, args.store)
    print(f">>> Store: {store_code} ({store_name})  |  PDF: {pdf.name}")

    # Feed the local PDF through the normal parser as if it were an email attachment.
    email_data = {
        "attachments": [{"filename": pdf.name, "data_base64": base64.b64encode(pdf.read_bytes()).decode("ascii")}],
        "body": f"{store_name} EOD report (manual reprocess of a missing report).",
        "sender_email": f"manual-{store_code}",
        "sender_name": store_name,
        "message_id": f"manual-{store_code}-{pdf.stem}",
    }

    print(f">>> Parsing via {config.AI_PROVIDER} ...")
    parsed = claude_parser.parse_eod_email(email_data)
    if parsed is None:
        sys.exit("ERROR: parse failed (see eod_processor.log).")

    report_date = parsed.get("report_date", "")
    txns = parsed.get("transactions", [])
    total_inc = parsed.get("total_inc_tax")
    sum_inc = round(sum(t.get("amount_inc_tax", 0) for t in txns), 2)
    print(f"    report_date={report_date}  rows={len(txns)}  "
          f"total_inc_tax={total_inc}  sum(rows)={sum_inc}")

    # Integrity gate: the extracted rows must reconcile to the report's own total.
    if total_inc is None or abs(sum_inc - round(float(total_inc), 2)) > 0.01:
        sys.exit(f"ERROR: rows sum to {sum_inc} but report total_inc_tax is {total_inc}. "
                 f"Extraction is incomplete — NOT writing/uploading. Inspect the PDF.")
    print("    OK - row total reconciles to report total.")

    result, file_path = json_writer.write_json(parsed, store_code)
    if result != "success":
        sys.exit(f"ERROR: json_writer returned '{result}'.")
    print(f">>> Wrote {file_path}")

    if args.no_upload:
        print(">>> --no-upload: skipping SFTP and archive (review the JSON, then rerun).")
        return 0

    print(f">>> Uploading to SFTP {config.FTP_HOST}{config.FTP_DIRECTORY} ...")
    if not ftp_uploader.upload_file(file_path):
        sys.exit("ERROR: SFTP upload failed (see eod_processor.log). PDF left in place.")
    print("    SFTP upload OK.")

    if args.no_archive:
        print(">>> --no-archive: leaving PDF in place.")
        return 0

    label = args.archive_label or f"{store_name}-missing"
    dest_dir = ARCHIVE_ROOT / f"{date.today():%Y-%m-%d}-{label}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / pdf.name
    pdf.replace(dest)
    print(f">>> Archived PDF -> {dest.relative_to(_PROJECT_ROOT)}")
    print("DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
