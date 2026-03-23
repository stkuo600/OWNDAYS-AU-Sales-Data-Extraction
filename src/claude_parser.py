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

The email contains PDF attachments. Find the **Payment Detail by Payment Type** PDF — it has columns: Date, Ref-No, Name, Tax Paid, Payment (Inc Tax)$, Consult $, Frame $, Lens $, CL $, Sundry $, Misc $. Rows are grouped under "Payment Type: XXX" section headers. Ignore other PDFs (e.g. "Banking Transaction Report", "BulkBillingSummaryReport", "DailyTallyReport", "ScannedDocument").

Extract the following and return as a single JSON object (no markdown, no preamble, ONLY valid JSON):

From the email body text below:
- "report_date": the report date in YYYY-MM-DD format
- "store_name": the store name
- "target_exc_gst": daily target excluding GST (numeric, no $ or commas)
- "consultation": consultation count or value (numeric)
- "no_customers": number of customers (integer)
- "daily_comment": any daily comment text (string, empty string if none)
- "customer_feedback": any customer feedback text (string, empty string if none)

From the Payment Detail by Payment Type PDF:
- "total_inc_gst": grand total Payment (Inc Tax) from the last row of the last page (numeric, no $ or commas)
- "total_tax": grand total Tax Paid from the last row of the last page (numeric, no $ or commas)
- "total_exc_gst": derived as total_inc_gst - total_tax (numeric)
- "total_consult": grand total Consult $ (numeric)
- "total_frame": grand total Frame $ (numeric)
- "total_lens": grand total Lens $ (numeric)
- "total_cl": grand total CL $ (numeric)
- "total_sundry": grand total Sundry $ (numeric)
- "total_misc": grand total Misc $ (numeric)
- "transaction_count": number of individual transaction rows (exclude per-payment-type subtotal rows and the grand total row)
- "transactions": array of objects, one per transaction row (exclude subtotal and grand total rows):
  - "receipt_no": Ref-No column (string, empty string if none e.g. for DDEP/Medicare rows)
  - "payment_method": the Payment Type section header the row belongs to, exactly as shown (e.g. "MASTER", "HC", "DDEP", "VISA", "EFTPOS", "CASH", "zMP")
  - "customer_name": Name column — the name portion only, without the ID number (string)
  - "customer_id": the number after the last dash in the Name column (string, empty string if none e.g. for Medicare)
  - "amount_inc_tax": Payment (Inc Tax)$ column (numeric, no $ or commas)
  - "tax": Tax Paid column (numeric, no $ or commas)
  - "amount_exc_tax": derived as amount_inc_tax - tax (numeric)
  - "consult": Consult $ column (numeric)
  - "frame": Frame $ column (numeric)
  - "lens": Lens $ column (numeric)
  - "cl": CL $ column (numeric)
  - "sundry": Sundry $ column (numeric)
  - "misc": Misc $ column (numeric)

Remove $ signs and commas from ALL numeric values. Return numbers as numbers, not strings.

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
