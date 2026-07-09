"""
claude_parser.py — AI-powered EOD report extraction.

Supports two providers (configured via AI_PROVIDER in .env):
  - "anthropic": Claude API (Anthropic SDK)
  - "azure": Azure OpenAI GPT-4o (OpenAI SDK)
"""

import json
import logging
import re

import config

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT_TEMPLATE = """\
You are extracting data from an End of Day (EOD) report email for an optical retail store.

The email contains PDF attachments. Find the **Banking Transaction Report** PDF — it has columns: Date, Receipt, Paid By, Patient / Payer, Amount (Inc Tax) $, Tax $, Amount (Exc Tax) $. Rows are grouped by payment method sections. Ignore other PDFs (e.g. "PaymentDetailByPaymentType", "BulkBillingSummaryReport", "DailyTallyReport", "ScannedDocument").

Extract the following and return as a single JSON object (no markdown, no preamble, ONLY valid JSON):

- "report_date": the report date from the PDF Period field in YYYY-MM-DD format
- "total_inc_tax": Total Amount (Inc Tax) from the last row (numeric, no $ or commas)
- "total_tax": Total Tax from the last row (numeric, no $ or commas)
- "total_exc_tax": Total Amount (Exc Tax) from the last row (numeric, no $ or commas)
- "transaction_count": number of individual transaction rows (exclude Sub Total rows and the Total Amount row)
- "transactions": array of objects, one per transaction row (exclude Sub Total and Total Amount rows):
  - "receipt_no": Receipt column (string, empty string if none e.g. for DDEP/Medicare rows)
  - "payment_method": Paid By column exactly as shown (e.g. "MASTER", "HC", "DDEP", "VISA", "EFTPOS", "CASH", "zMP", "Afterpay", "AX")
  - "customer_name": Patient / Payer column — the name portion only, without the ID in parentheses (string)
  - "customer_id": the number in parentheses after # in the Patient / Payer column (string, empty string if none e.g. for Medicare)
  - "amount_inc_tax": Amount (Inc Tax) $ column (numeric, no $ or commas)
  - "tax": Tax $ column (numeric, no $ or commas)
  - "amount_exc_tax": Amount (Exc Tax) $ column (numeric, no $ or commas)

Remove $ signs and commas from ALL numeric values. Return numbers as numbers, not strings.

CRITICAL — column alignment when Receipt is empty:
"DDEP" is ALWAYS a payment method, NEVER a receipt number. When you see a row like
  "24/05/2026 DDEP Medicare 198.75 0.00 198.75"
the Receipt column is empty and the columns map as:
  receipt_no="", payment_method="DDEP", customer_name="Medicare", customer_id="",
  amount_inc_tax=198.75, tax=0, amount_exc_tax=198.75
Do NOT shift "DDEP" left into receipt_no. Receipt numbers are always all-digit values like "142139".

CRITICAL — receipt_no is empty ONLY for DDEP/Medicare rows:
A DDEP (Medicare direct deposit) row is the ONLY row type printed with no Receipt.
EVERY other row — VISA, EFTPOS, HC, MASTER, AX, zBUPA, CASH, Afterpay, zMP, etc. —
ALWAYS has an all-digit Receipt number (e.g. "145258") in the Receipt column, and you
MUST copy it verbatim into receipt_no. Never output receipt_no="" for a non-DDEP row.
Watch the row directly ABOVE a DDEP/Medicare row especially: it is a normal
receipt-bearing row (e.g. an EFTPOS payment) and must keep its own receipt number —
do NOT let the adjacent empty-receipt DDEP row cause you to blank it out. If any
non-DDEP row looks like it has no receipt, re-read that row; the number is there.

EMAIL BODY:
{email_body}"""


def _call_anthropic(content):
    """Send extraction request via Anthropic Claude API."""
    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    logger.info("Sending to Claude (model=%s)", config.CLAUDE_MODEL)

    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=16384,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


def _call_azure_openai(content):
    """Send extraction request via Azure OpenAI Responses API (v1)."""
    from openai import OpenAI

    # Azure Foundry v1 endpoint — use OpenAI client directly with base_url
    # Endpoint format: https://<resource>.services.ai.azure.com/api/projects/<project>/openai/v1/responses
    # Base URL for SDK: strip /responses to get the v1 base
    base_url = config.AZURE_OPENAI_ENDPOINT
    if base_url.endswith("/responses"):
        base_url = base_url[: -len("/responses")]

    client = OpenAI(
        base_url=base_url,
        api_key=config.AZURE_OPENAI_API_KEY,
    )
    logger.info("Sending to Azure OpenAI (deployment=%s)", config.AZURE_OPENAI_DEPLOYMENT)

    # Convert Anthropic content format to OpenAI Responses API format
    openai_content = []
    for block in content:
        if block["type"] == "text":
            openai_content.append({"type": "input_text", "text": block["text"]})
        elif block["type"] == "document":
            data = block["source"]["data"]
            openai_content.append({
                "type": "input_file",
                "filename": "attachment.pdf",
                "file_data": f"data:application/pdf;base64,{data}",
            })

    response = client.responses.create(
        model=config.AZURE_OPENAI_DEPLOYMENT,
        input=[{"role": "user", "content": openai_content}],
        max_output_tokens=16384,
    )
    return response.output_text


def parse_eod_email(email_data):
    """Parse an EOD report email using the configured AI provider.

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
        # Build the content list (Anthropic format — converted for Azure in _call_azure_openai)
        content = []

        for attachment in email_data.get("attachments", []):
            content.append({
                "type": "text",
                "text": f"PDF: {attachment['filename']}",
            })
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": attachment["data_base64"],
                },
            })

        extraction_prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
            email_body=email_data.get("body", "")
        )
        content.append({"type": "text", "text": extraction_prompt})

        logger.info(
            "Parsing EOD email (provider=%s, attachments=%d)",
            config.AI_PROVIDER,
            len(email_data.get("attachments", [])),
        )

        # Call the configured provider
        if config.AI_PROVIDER == "azure":
            raw_text = _call_azure_openai(content)
        else:
            raw_text = _call_anthropic(content)

        # Strip markdown code fences if the model wraps the JSON
        cleaned_text = re.sub(
            r"^```(?:json)?\s*\n?|\n?```\s*$", "", raw_text.strip()
        )

        try:
            parsed = json.loads(cleaned_text)
        except json.JSONDecodeError as exc:
            logger.error(
                "Failed to parse JSON response: %s\nRaw response: %s",
                exc,
                raw_text,
            )
            return None

        # Attach email metadata
        parsed["sender_email"] = email_data.get("sender_email")
        parsed["sender_name"] = email_data.get("sender_name")
        parsed["message_id"] = email_data.get("message_id")

        logger.info(
            "Successfully parsed EOD email (message_id=%s)", parsed.get("message_id")
        )
        return parsed

    except Exception as exc:
        logger.error("Unexpected error in parse_eod_email: %s", exc, exc_info=True)
        return None
