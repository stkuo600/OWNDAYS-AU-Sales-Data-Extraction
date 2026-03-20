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


def send_notification(subject, body):
    """Send an email notification via SMTP."""
    logger = logging.getLogger(__name__)
    if not config.SMTP_SERVER or not config.SMTP_TO_EMAIL:
        logger.warning("SMTP not configured — skipping notification")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{config.SMTP_FROM_NAME} <{config.SMTP_FROM_EMAIL}>"
    msg["To"] = config.SMTP_TO_EMAIL

    try:
        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT, timeout=10) as server:
            server.sendmail(config.SMTP_FROM_EMAIL, [config.SMTP_TO_EMAIL], msg.as_string())
        logger.info("Notification email sent to %s", config.SMTP_TO_EMAIL)
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

    try:
        service = gmail_reader.get_gmail_service()
        emails = gmail_reader.fetch_unread_emails(service)
        logger.info("Found %d unread emails with PDF attachments", len(emails))
    except Exception:
        logger.exception("Failed to fetch emails from Gmail")
        send_notification(
            "[EOD Processor] ERROR — Gmail fetch failed",
            f"The EOD Processor failed to fetch emails from Gmail.\n\n{traceback.format_exc()}",
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
            errors.append(f"Skipped (store not found): {sender} — {subject}")
            skipped_count += 1
        else:
            logger.error("Failed to write data for email from=%s — NOT marked as read", sender)
            errors.append(f"Write failed: {sender} — {subject}")
            failed_count += 1

    logger.info("=" * 60)
    logger.info("EOD Processor — run complete: success=%d failed=%d skipped=%d",
                success_count, failed_count, skipped_count)
    logger.info("=" * 60)

    # Send notification
    total = success_count + failed_count + skipped_count
    if failed_count > 0 or skipped_count > 0:
        error_detail = "\n".join(f"  - {e}" for e in errors)
        send_notification(
            f"[EOD Processor] COMPLETED WITH ISSUES — {failed_count} failed, {skipped_count} skipped",
            f"EOD Processor run complete.\n\n"
            f"  Success: {success_count}\n"
            f"  Failed:  {failed_count}\n"
            f"  Skipped: {skipped_count}\n"
            f"  Total:   {total}\n\n"
            f"Issues:\n{error_detail}",
        )
    elif total > 0:
        send_notification(
            f"[EOD Processor] SUCCESS — {success_count} email(s) processed",
            f"EOD Processor run complete.\n\n"
            f"  Success: {success_count}\n"
            f"  Total:   {total}\n\n"
            f"All emails processed and written to Fabric Warehouse.",
        )
    else:
        logger.info("No emails to process — no notification sent")


if __name__ == "__main__":
    main()
