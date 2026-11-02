"""
main.py — Entry point for the OWNDAYS EOD Report Processor.

Orchestrates: Graph fetch → Claude parse → JSON write → FTP upload → mark as read.

Backfill mode (--from / --to YYYY-MM-DD):
    Fetches emails in the given date range from configured store senders,
    writes JSON locally only. Skips SFTP upload, mark-as-read,
    dedup, and email notifications.
"""

import argparse
import logging
import smtplib
import sys
import traceback
from datetime import date
from email.mime.text import MIMEText

import config
import graph_reader
import claude_parser
import json_writer
import ftp_uploader

logger = logging.getLogger(__name__)

# A report older than this (relative to its email's send date) is treated as a misread date.
REPORT_DATE_MAX_AGE_DAYS = 14


def deduplicate_emails(emails):
    """Drop duplicate reports, keeping the first occurrence of each store/day.

    Runs AFTER parsing. An email is a duplicate only if BOTH its store and its
    parsed report_date match an already-seen email. The day is the report's own
    date, not the send date: a store that sends yesterday's report late (same
    send day as today's) must not have it dropped. Emails are ordered
    newest-first, so the first occurrence of a (store, report_date) pair wins.

    Args:
        emails (list[dict]): Email dicts with ``store_code`` (falls back to
            ``sender_email`` when unknown) and ``report_date``.

    Returns:
        tuple[list[dict], list[str]]: (unique_emails, duplicate_message_ids).
    """
    seen = set()
    unique_emails = []
    duplicate_ids = []
    for email_data in emails:
        store = email_data.get("store_code") or email_data.get("sender_email", "unknown")
        key = (store, email_data.get("report_date"))
        if key in seen:
            duplicate_ids.append(email_data["message_id"])
            logger.info(
                "Duplicate report store=%s date=%s subject=%s — keeping first occurrence",
                store, email_data.get("report_date"), email_data.get("subject"),
            )
        else:
            seen.add(key)
            unique_emails.append(email_data)
    return unique_emails, duplicate_ids


def validate_parsed(parsed, send_date=None):
    """Return an error string if a parse result has no usable data, else None.

    A parse can "succeed" yet contain nothing — e.g. the store forgot to attach
    BankingTransactionReport.pdf, so the model returns an empty report_date and
    no transactions. Treat that as a failure so the email stays unprocessed
    and the run notification flags it.

    When *send_date* is given, report_date must fall within the
    REPORT_DATE_MAX_AGE_DAYS before it. This catches day/month swaps: the
    2026-10-03 reports were read as 2026-03-10 and filed under March.
    """
    report_date = parsed.get("report_date")
    if not report_date:
        return "no report date (BankingTransactionReport.pdf missing?)"
    if not parsed.get("transactions"):
        return "no transactions (BankingTransactionReport.pdf missing?)"
    if send_date:
        try:
            age = (date.fromisoformat(send_date) - date.fromisoformat(report_date)).days
        except ValueError:
            return f"unreadable report date {report_date!r}"
        if not 0 <= age <= REPORT_DATE_MAX_AGE_DAYS:
            return (f"report date {report_date} is implausible for an email sent "
                    f"{send_date} (day/month swapped?)")
    return None


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
    parser.add_argument("--live", action="store_true",
                        help="In backfill mode, also run side effects (SFTP upload + "
                             "mark-as-read). Use for a scoped, controlled production run.")
    return parser.parse_args()


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    args = _parse_args()
    backfill = bool(args.from_date or args.to_date)
    if backfill and not (args.from_date and args.to_date):
        logger.error("--from and --to must both be provided for backfill mode")
        sys.exit(2)
    if args.live and not backfill:
        logger.error("--live only applies to backfill mode (use with --from/--to)")
        sys.exit(2)

    # Side effects (SFTP upload + mark-as-read) run in normal mode always, and in
    # backfill mode only when --live is given (a scoped, controlled production run).
    side_effects = (not backfill) or args.live

    logger.info("=" * 60)
    if backfill:
        logger.info("EOD Processor — BACKFILL run %s to %s (%s)",
                    args.from_date, args.to_date,
                    "LIVE: SFTP + mark-as-read" if args.live else "local JSON only")
    else:
        logger.info("EOD Processor — starting run")
    logger.info("=" * 60)

    success_count = 0
    failed_count = 0
    skipped_count = 0
    errors = []
    store_results = []  # (store_name, report_date, total_exc_gst, txn_count, status)

    try:
        token = graph_reader.get_graph_token()
        if backfill:
            # from_date/to_date are inclusive local dates; graph_reader handles the
            # exclusive upper bound and timezone conversion.
            emails = graph_reader.fetch_emails_in_date_range(
                token, args.from_date, args.to_date,
                list(config.STORE_MAP.keys()) + config.STORE_FORWARDER_ADDRESSES,
            )
            logger.info("Found %d emails in date range", len(emails))
        else:
            emails = graph_reader.fetch_unprocessed_emails(token)
            logger.info("Found %d unprocessed emails with PDF attachments", len(emails))
    except Exception:
        logger.exception("Failed to fetch emails from Microsoft Graph")
        send_notification(
            "[EOD Processor] ERROR — Graph fetch failed",
            f"<pre>{traceback.format_exc()}</pre>",
            config.SMTP_TO_ERROR,
        )
        return

    # Parse every email first: dedup needs the report date, which only the parse knows.
    parsed_emails = []
    for email_data in emails:
        sender = email_data.get("sender_email", "unknown")
        subject = email_data.get("subject", "no subject")
        logger.info("Processing email from=%s subject=%s", sender, subject)

        # Parse with Claude
        parsed = claude_parser.parse_eod_email(email_data)
        if parsed is None:
            logger.error("Failed to parse email from=%s — skipping", sender)
            errors.append(f"Parse failed: {sender} — {subject}")
            store_results.append((subject, "", "", "", "PARSE FAILED"))
            failed_count += 1
            continue

        store_code = config.resolve_store_code(
            sender, email_data.get("sender_name", ""), email_data.get("body", "")
        )
        logger.info("Parsed: date=%s store=%s transactions=%d",
                     parsed.get("report_date", ""), store_code or "Unknown",
                     len(parsed.get("transactions") or []))

        problem = validate_parsed(parsed, email_data.get("send_date"))
        if problem:
            logger.error("Unusable parse for email from=%s subject=%s: %s — NOT marked as read",
                         sender, subject, problem)
            errors.append(f"Parse empty: {sender} — {subject} ({problem})")
            store_results.append((store_code or subject, parsed.get("report_date", ""), "", 0, "NO DATA"))
            failed_count += 1
            continue

        email_data["parsed"] = parsed
        email_data["store_code"] = store_code
        email_data["report_date"] = parsed["report_date"]
        parsed_emails.append(email_data)

    # Deduplicate: keep the latest email per (store, report_date).
    # graph_reader returns messages newest-first, so the first occurrence wins.
    emails, duplicate_ids = deduplicate_emails(parsed_emails)

    if not side_effects:
        # Local JSON only — leave mailbox state untouched, so duplicates are
        # simply skipped rather than marked as read.
        if duplicate_ids:
            logger.info("Skipping %d duplicate email(s)", len(duplicate_ids))
        logger.info("Processing %d unique email(s) in backfill mode", len(emails))
    else:
        if duplicate_ids:
            logger.info("Marking %d duplicate email(s) as read", len(duplicate_ids))
            for dup_id in duplicate_ids:
                try:
                    graph_reader.mark_as_read(token, dup_id)
                except Exception:
                    logger.exception("Failed to mark duplicate %s as read", dup_id)

        logger.info("Processing %d unique email(s) after deduplication", len(emails))

    for email_data in emails:
        sender = email_data.get("sender_email", "unknown")
        subject = email_data.get("subject", "no subject")
        message_id = email_data["message_id"]
        parsed = email_data["parsed"]
        store_code = email_data["store_code"]
        store_label = store_code or "Unknown"
        report_date = email_data["report_date"]
        total_exc_tax = parsed.get("total_exc_tax", 0)
        txn_count = len(parsed["transactions"])

        # Write JSON file
        result, file_path = json_writer.write_json(parsed, store_code)

        if result == "skipped":
            logger.warning("Skipped email from=%s — store not found in STORE_MAP, NOT marked as read", sender)
            errors.append(f"Skipped (store not found): {sender} — {subject}")
            store_results.append((store_label, report_date, total_exc_tax, txn_count, "SKIPPED"))
            skipped_count += 1
            continue

        if result == "error":
            logger.error("Failed to write JSON for email from=%s — NOT marked as read", sender)
            errors.append(f"JSON write failed: {sender} — {subject}")
            store_results.append((store_label, report_date, total_exc_tax, txn_count, "FAILED"))
            failed_count += 1
            continue

        # Upload to SFTP (skipped unless side effects are enabled)
        if config.FTP_HOST and side_effects:
            ftp_ok = ftp_uploader.upload_file(file_path)
            if not ftp_ok:
                logger.error("FTP upload failed for %s — email NOT marked as read", file_path)
                errors.append(f"FTP upload failed: {sender} — {subject}")
                store_results.append((store_label, report_date, total_exc_tax, txn_count, "FTP FAILED"))
                failed_count += 1
                continue

        # Mark as read (skipped unless side effects are enabled — leave mailbox untouched)
        if side_effects:
            try:
                graph_reader.mark_as_read(token, message_id)
                logger.info("Success — email marked as read")
                store_results.append((store_label, report_date, total_exc_tax, txn_count, "OK"))
            except Exception:
                logger.exception("Data written but failed to mark email as read: message_id=%s", message_id)
                errors.append(f"Mark-as-read failed (data written): {sender} — {subject}")
                store_results.append((store_label, report_date, total_exc_tax, txn_count, "OK*"))
        else:
            logger.info("Success — JSON written (mailbox untouched)")
            store_results.append((store_label, report_date, total_exc_tax, txn_count, "OK"))
        success_count += 1

    logger.info("=" * 60)
    logger.info("EOD Processor — run complete: success=%d failed=%d skipped=%d",
                success_count, failed_count, skipped_count)
    logger.info("=" * 60)

    # Certificate expiry check — warn ahead of time so the cert is rotated before it lapses.
    cert_days = graph_reader.cert_days_until_expiry()
    cert_warning_html = ""
    if cert_days is not None and cert_days <= config.GRAPH_CERT_WARN_DAYS:
        warn_msg = (f"Graph certificate expires in {cert_days} day(s) — rotate it soon "
                    f"(scripts/generate_graph_cert.py; see migration plan §2/§4).")
        logger.warning(warn_msg)
        cert_warning_html = (
            "<div style='margin-top:16px;padding:12px;background:#fff3e0;"
            "border-left:4px solid #e65100;border-radius:4px'>"
            f"<b>⚠ Certificate expiry:</b> {warn_msg}</div>"
        )
    elif cert_days is not None:
        logger.info("Graph certificate expires in %d day(s).", cert_days)

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
            f"{cert_warning_html}"
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
