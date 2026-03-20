"""
claude_parser.py

Sends EOD report email data (body + PDF attachments) to Claude and returns
the extracted structured data as a Python dict.
"""

import json
import logging
import re

import anthropic

import config

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT_TEMPLATE = """\
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
{email_body}"""


def parse_eod_email(email_data):
    """Parse an EOD report email using Claude.

    Args:
        email_data (dict): Must contain:
            - "attachments": list of dicts with "filename" and "data_base64" keys
            - "body": plain-text email body (str)
            - "sender_email": sender email address (str)
            - "sender_name": sender display name (str)
            - "message_id": unique message identifier (str)

    Returns:
        dict: Extracted data enriched with sender_email, sender_name, and
              message_id metadata, or None on any failure.
    """
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        # Build the content list
        content = []

        for attachment in email_data.get("attachments", []):
            # Label each PDF so Claude knows which file follows
            content.append({
                "type": "text",
                "text": f"PDF: {attachment['filename']}",
            })
            # Send the raw PDF bytes as a base64-encoded document block
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": attachment["data_base64"],
                },
            })

        # Append the extraction prompt with the email body substituted in
        extraction_prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
            email_body=email_data.get("body", "")
        )
        content.append({"type": "text", "text": extraction_prompt})

        logger.info(
            "Sending EOD email to Claude (model=%s, attachments=%d)",
            config.CLAUDE_MODEL,
            len(email_data.get("attachments", [])),
        )

        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": content}],
        )

        raw_text = response.content[0].text

        # Strip markdown code fences if Claude wraps the JSON anyway
        cleaned_text = re.sub(
            r"^```(?:json)?\s*\n?|\n?```\s*$", "", raw_text.strip()
        )

        try:
            parsed = json.loads(cleaned_text)
        except json.JSONDecodeError as exc:
            logger.error(
                "Failed to parse Claude JSON response: %s\nRaw response: %s",
                exc,
                raw_text,
            )
            return None

        # Attach email metadata so downstream consumers have full context
        parsed["sender_email"] = email_data.get("sender_email")
        parsed["sender_name"] = email_data.get("sender_name")
        parsed["message_id"] = email_data.get("message_id")

        logger.info(
            "Successfully parsed EOD email (message_id=%s)", parsed.get("message_id")
        )
        return parsed

    except Exception as exc:  # Catches Anthropic API errors and any other unexpected errors
        logger.error("Unexpected error in parse_eod_email: %s", exc, exc_info=True)
        return None
