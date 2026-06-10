"""
main.py — Entry point for the OWNDAYS EOD Report Processor.

Orchestrates: Gmail fetch → Claude parse → JSON write → FTP upload → mark as read.

Backfill mode (--from / --to YYYY-MM-DD):
    Fetches emails in the given date range from configured store senders,
    writes JSON locally only. Skips SFTP upload, Gmail mark-as-read,
    dedup, and email notifications.
"""

import argparse
import logging
import smtplib
import sys
import traceback
from datetime import datetime, timedelta
from email.mime.text import MIMEText

import config
import gmail_reader
import claude_parser
import json_writer
import ftp_uploader

logger = logging.getLogger(__name__)


def deduplicate_emails(emails):
    """Drop duplicate emails, keeping the first occurrence of each store/day.

    An email is a duplicate only if BOTH its sender address and its send_date
    match an already-seen email. Gmail returns newest-first, so the first
    occurrence of a (sender, send_date) pair wins. Two different days' reports
    from the same store are NOT duplicates and are both kept.

    Args:
        emails (list[dict]): Email dicts with ``sender_email`` and ``send_date``.

    Returns:
        tuple[list[dict], list[str]]: (unique_emails, duplicate_message_ids).
    """
    seen = set()
    unique_emails = []
    duplicate_ids = []
    for email_data in emails:
        sender = email_data.get("sender_email", "unknown")
        key = (sender, email_data.get("send_date"))
        if key in seen:
            duplicate_ids.append(email_data["message_id"])
            logger.info(
                "Duplicate email from=%s date=%s subject=%s — keeping first occurrence",
                sender, email_data.get("send_date"), email_data.get("subject"),
            )
        else:
            seen.add(key)
            unique_emails.append(email_data)
    return unique_emails, duplicate_ids


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


def send_notification(subject, body_html, recipients):
    """Send an HTML email notification via SMTP.

    recipients: comma-separated email string (e.g. "a@x.com,b@x.com")
    """
    logger = logging.getLogger(__name__)
    if not config.SMTP_SERVER or not recipients:
        logger.warning("SMTP not configured or no recipients — skipping notification")
        return

    to_list = [addr.strip() for addr in recipients.split(",") if addr.strip()]
    if not to_list:
        return

    msg = MIMEText(body_html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{config.SMTP_FROM_NAME} <{config.SMTP_FROM_EMAIL}>"
    msg["To"] = ", ".join(to_list)

    try:
        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT, timeout=10) as server:
            server.sendmail(config.SMTP_FROM_EMAIL, to_list, msg.as_string())
        logger.info("Notification email sent to %s", ", ".join(to_list))
    except Exception:
        logger.exception("Failed to send notification email")


def _parse_args():
    parser = argparse.ArgumentParser(description="OWNDAYS EOD Processor")
    parser.add_argument("--from", dest="from_date",
                        help="Backfill start date (YYYY-MM-DD, inclusive)")
    parser.add_argument("--to", dest="to_date",
                        help="Backfill end date (YYYY-MM-DD, inclusive)")
    return parser.parse_args()


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    args = _parse_args()
    backfill = bool(args.from_date or args.to_date)
    if backfill and not (args.from_date and args.to_date):
        logger.error("--from and --to must both be provided for backfill mode")
        sys.exit(2)

    logger.info("=" * 60)
    if backfill:
        logger.info("EOD Processor — BACKFILL run %s to %s (local JSON only)",
                    args.from_date, args.to_date)
    else:
        logger.info("EOD Processor — starting run")
    logger.info("=" * 60)

    success_count = 0
    failed_count = 0
    skipped_count = 0
    errors = []
    store_results = []  # (store_name, report_date, total_exc_gst, txn_count, status)

    try:
        service = gmail_reader.get_gmail_service()
        if backfill:
            # Gmail 'before:' is exclusive — add one day so the end date is inclusive.
            after = datetime.strptime(args.from_date, "%Y-%m-%d").strftime("%Y/%m/%d")
            before = (datetime.strptime(args.to_date, "%Y-%m-%d")
                      + timedelta(days=1)).strftime("%Y/%m/%d")
            emails = gmail_reader.fetch_emails_in_date_range(
                service, after, before, list(config.STORE_MAP.keys())
            )
            logger.info("Found %d emails in date range", len(emails))
        else:
            emails = gmail_reader.fetch_unread_emails(service)
            logger.info("Found %d unread emails with PDF attachments", len(emails))
    except Exception:
        logger.exception("Failed to fetch emails from Gmail")
        send_notification(
            "[EOD Processor] ERROR — Gmail fetch failed",
            f"<pre>{traceback.format_exc()}</pre>",
            config.SMTP_TO_ERROR,
        )
        return

    # Deduplicate: keep the latest email per store per day (sender, send_date).
    # Gmail returns newest first, so the first occurrence wins.
    emails, duplicate_ids = deduplicate_emails(emails)

    if backfill:
        # Backfill writes local JSON only — leave Gmail state untouched, so
        # duplicates are simply skipped rather than marked as read.
        if duplicate_ids:
            logger.info("Skipping %d duplicate email(s)", len(duplicate_ids))
        logger.info("Processing %d unique email(s) in backfill mode", len(emails))
    else:
        if duplicate_ids:
            logger.info("Marking %d duplicate email(s) as read", len(duplicate_ids))
            for dup_id in duplicate_ids:
                try:
                    gmail_reader.mark_as_read(service, dup_id)
                except Exception:
                    logger.exception("Failed to mark duplicate %s as read", dup_id)

        logger.info("Processing %d unique email(s) after deduplication", len(emails))

    for email_data in emails:
        sender = email_data.get("sender_email", "unknown")
        subject = email_data.get("subject", "no subject")
        message_id = email_data["message_id"]
        logger.info("Processing email from=%s subject=%s", sender, subject)

        # Parse with Claude
        parsed = claude_parser.parse_eod_email(email_data)
        if parsed is None:
            logger.error("Failed to parse email from=%s — skipping", sender)
            errors.append(f"Parse failed: {sender} — {subject}")
            store_results.append((subject, "", "", "", "PARSE FAILED"))
            failed_count += 1
            continue

        store_code = config.STORE_MAP.get(sender, "Unknown")
        report_date = parsed.get("report_date", "")
        total_exc_tax = parsed.get("total_exc_tax", 0)
        txn_count = len(parsed.get("transactions", []))

        logger.info("Parsed: date=%s store=%s transactions=%d",
                     report_date, store_code, txn_count)

        # Write JSON file
        result, file_path = json_writer.write_json(parsed)

        if result == "skipped":
            logger.warning("Skipped email from=%s — store not found in STORE_MAP, NOT marked as read", sender)
            errors.append(f"Skipped (store not found): {sender} — {subject}")
            store_results.append((store_code, report_date, total_exc_tax, txn_count, "SKIPPED"))
            skipped_count += 1
            continue

        if result == "error":
            logger.error("Failed to write JSON for email from=%s — NOT marked as read", sender)
            errors.append(f"JSON write failed: {sender} — {subject}")
            store_results.append((store_code, report_date, total_exc_tax, txn_count, "FAILED"))
            failed_count += 1
            continue

        # Upload to FTP (skipped in backfill mode)
        if config.FTP_HOST and not backfill:
            ftp_ok = ftp_uploader.upload_file(file_path)
            if not ftp_ok:
                logger.error("FTP upload failed for %s — email NOT marked as read", file_path)
                errors.append(f"FTP upload failed: {sender} — {subject}")
                store_results.append((store_code, report_date, total_exc_tax, txn_count, "FTP FAILED"))
                failed_count += 1
                continue

        # Mark as read (skipped in backfill mode — leave Gmail state untouched)
        if not backfill:
            try:
                gmail_reader.mark_as_read(service, message_id)
                logger.info("Success — email marked as read")
                store_results.append((store_code, report_date, total_exc_tax, txn_count, "OK"))
            except Exception:
                logger.exception("Data written but failed to mark email as read: message_id=%s", message_id)
                errors.append(f"Mark-as-read failed (data written): {sender} — {subject}")
                store_results.append((store_code, report_date, total_exc_tax, txn_count, "OK*"))
        else:
            logger.info("Success — JSON written (Gmail untouched)")
            store_results.append((store_code, report_date, total_exc_tax, txn_count, "OK"))
        success_count += 1

    logger.info("=" * 60)
    logger.info("EOD Processor — run complete: success=%d failed=%d skipped=%d",
                success_count, failed_count, skipped_count)
    logger.info("=" * 60)

    # Build HTML notification
    total = success_count + failed_count + skipped_count

    def _build_html(title_color, summary_extra=""):
        ok_sales = sum(s[2] for s in store_results if s[4] == "OK" and isinstance(s[2], (int, float)))
        rows_html = ""
        for name, date, sales, txns, status in store_results:
            sales_str = f"${sales:,.2f}" if isinstance(sales, (int, float)) and sales is not None else ""
            status_color = "#2e7d32" if status == "OK" else "#c62828"
            rows_html += (
                f"<tr>"
                f"<td style='padding:6px 12px;border-bottom:1px solid #e0e0e0'>{name}</td>"
                f"<td style='padding:6px 12px;border-bottom:1px solid #e0e0e0'>{date}</td>"
                f"<td style='padding:6px 12px;border-bottom:1px solid #e0e0e0;text-align:right'>{sales_str}</td>"
                f"<td style='padding:6px 12px;border-bottom:1px solid #e0e0e0;text-align:center'>{txns}</td>"
                f"<td style='padding:6px 12px;border-bottom:1px solid #e0e0e0;color:{status_color};font-weight:bold'>{status}</td>"
                f"</tr>"
            )
        return (
            f"<div style='font-family:Segoe UI,Arial,sans-serif;max-width:640px;margin:0 auto'>"
            f"<div style='background:{title_color};color:#fff;padding:16px 20px;border-radius:6px 6px 0 0'>"
            f"<h2 style='margin:0;font-size:18px'>EOD Processor Report</h2></div>"
            f"<div style='padding:20px;background:#fafafa;border:1px solid #e0e0e0;border-top:none;border-radius:0 0 6px 6px'>"
            f"<table style='margin-bottom:16px'>"
            f"<tr><td style='padding:2px 16px 2px 0;color:#666'>Success</td><td><b>{success_count}</b></td></tr>"
            f"<tr><td style='padding:2px 16px 2px 0;color:#666'>Failed</td><td><b>{failed_count}</b></td></tr>"
            f"<tr><td style='padding:2px 16px 2px 0;color:#666'>Skipped</td><td><b>{skipped_count}</b></td></tr>"
            f"<tr><td style='padding:2px 16px 2px 0;color:#666'>Total</td><td><b>{total}</b></td></tr>"
            f"</table>"
            f"<table style='width:100%;border-collapse:collapse;background:#fff;border:1px solid #e0e0e0;border-radius:4px'>"
            f"<thead><tr style='background:#f5f5f5'>"
            f"<th style='padding:8px 12px;text-align:left;border-bottom:2px solid #ccc'>Store</th>"
            f"<th style='padding:8px 12px;text-align:left;border-bottom:2px solid #ccc'>Date</th>"
            f"<th style='padding:8px 12px;text-align:right;border-bottom:2px solid #ccc'>Sales (Exc GST)</th>"
            f"<th style='padding:8px 12px;text-align:center;border-bottom:2px solid #ccc'>Txns</th>"
            f"<th style='padding:8px 12px;text-align:left;border-bottom:2px solid #ccc'>Status</th>"
            f"</tr></thead><tbody>{rows_html}</tbody>"
            f"<tfoot><tr style='background:#f5f5f5;font-weight:bold'>"
            f"<td style='padding:8px 12px;border-top:2px solid #ccc'>TOTAL</td>"
            f"<td style='padding:8px 12px;border-top:2px solid #ccc'></td>"
            f"<td style='padding:8px 12px;border-top:2px solid #ccc;text-align:right'>${ok_sales:,.2f}</td>"
            f"<td style='padding:8px 12px;border-top:2px solid #ccc'></td>"
            f"<td style='padding:8px 12px;border-top:2px solid #ccc'></td>"
            f"</tr></tfoot></table>"
            f"{summary_extra}"
            f"</div></div>"
        )

    # Send notification (skipped in backfill mode)
    if backfill:
        logger.info("Backfill mode — skipping email notification")
        return

    if failed_count > 0 or skipped_count > 0:
        error_items = "".join(f"<li>{e}</li>" for e in errors)
        extra = f"<div style='margin-top:16px;padding:12px;background:#fff3e0;border-left:4px solid #e65100;border-radius:4px'><b>Issues:</b><ul style='margin:8px 0 0'>{error_items}</ul></div>"
        html = _build_html("#e65100", extra)
        send_notification(
            f"[EOD Processor] COMPLETED WITH ISSUES — {failed_count} failed, {skipped_count} skipped",
            html, config.SMTP_TO_ERROR,
        )
        # Also send to success recipients if there were any successes
        if success_count > 0 and config.SMTP_TO_SUCCESS != config.SMTP_TO_ERROR:
            send_notification(
                f"[EOD Processor] COMPLETED WITH ISSUES — {failed_count} failed, {skipped_count} skipped",
                html, config.SMTP_TO_SUCCESS,
            )
    elif total > 0:
        send_notification(
            f"[EOD Processor] SUCCESS — {success_count} email(s) processed",
            _build_html("#2e7d32"), config.SMTP_TO_SUCCESS,
        )
    else:
        logger.info("No emails to process — no notification sent")


if __name__ == "__main__":
    main()
