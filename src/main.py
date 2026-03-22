"""
main.py — Entry point for the OWNDAYS EOD Report Processor.

Orchestrates: Gmail fetch → Claude parse → Fabric write → mark as read.
"""

import logging
import smtplib
import sys
import traceback
from email.mime.text import MIMEText

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


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("EOD Processor — starting run")
    logger.info("=" * 60)

    success_count = 0
    failed_count = 0
    skipped_count = 0
    errors = []
    store_results = []  # (store_name, report_date, total_exc_gst, txn_count, status)

    try:
        service = gmail_reader.get_gmail_service()
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

        store_name = parsed.get("store_name", "Unknown")
        report_date = parsed.get("report_date", "")
        total_exc_gst = parsed.get("total_exc_gst", 0)
        txn_count = len(parsed.get("transactions", []))

        logger.info("Parsed: date=%s store=%s transactions=%d",
                     report_date, store_name, txn_count)

        # Write to Fabric
        result = fabric_writer.write_eod_data(parsed)

        if result == "success":
            try:
                gmail_reader.mark_as_read(service, message_id)
                logger.info("Success — email marked as read")
                store_results.append((store_name, report_date, total_exc_gst, txn_count, "OK"))
            except Exception:
                logger.exception("Data written but failed to mark email as read: message_id=%s", message_id)
                errors.append(f"Mark-as-read failed (data written): {sender} — {subject}")
                store_results.append((store_name, report_date, total_exc_gst, txn_count, "OK*"))
            success_count += 1
        elif result == "skipped":
            logger.warning("Skipped email from=%s — store not found, NOT marked as read", sender)
            errors.append(f"Skipped (store not found): {sender} — {subject}")
            store_results.append((store_name, report_date, total_exc_gst, txn_count, "SKIPPED"))
            skipped_count += 1
        else:
            logger.error("Failed to write data for email from=%s — NOT marked as read", sender)
            errors.append(f"Write failed: {sender} — {subject}")
            store_results.append((store_name, report_date, total_exc_gst, txn_count, "FAILED"))
            failed_count += 1

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

    # Send notification
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
