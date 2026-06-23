"""
json_writer.py — Transforms parsed EOD data to JSON and saves locally.

Output format: array of transaction objects with PartnerTransaction_ID,
StoreCode, TxDate, Register_ID, LineItems, and Payment.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)


def _transform_transactions(parsed_data, store_code):
    """Transform parsed transaction data to the target JSON format.

    Args:
        parsed_data (dict): Output from claude_parser.
        store_code (str): Store code from STORE_MAP.

    Returns:
        list[dict]: Array of transaction objects in target format.
    """
    report_date = parsed_data.get("report_date", "")
    transactions = parsed_data.get("transactions", [])

    # DDEP rows (Medicare direct deposits) have no receipt number, so each would
    # otherwise become a separate entry with an empty PartnerTransaction_ID.
    # Collapse all DDEP rows into a single aggregated transaction.
    ddep_txs = [t for t in transactions if t.get("payment_method", "") == "DDEP"]
    non_ddep_txs = [t for t in transactions if t.get("payment_method", "") != "DDEP"]

    if ddep_txs:
        aggregated_ddep = {
            "receipt_no": "",
            "payment_method": "DDEP",
            "customer_name": "Medicare",
            "customer_id": "",
            "amount_inc_tax": sum(t.get("amount_inc_tax", 0) for t in ddep_txs),
            "tax": sum(t.get("tax", 0) for t in ddep_txs),
            "amount_exc_tax": sum(t.get("amount_exc_tax", 0) for t in ddep_txs),
        }
        non_ddep_txs.append(aggregated_ddep)

    output = []

    for tx in non_ddep_txs:
        receipt_no = tx.get("receipt_no", "")
        amount_inc_tax = tx.get("amount_inc_tax", 0)
        amount_exc_tax = tx.get("amount_exc_tax", 0)
        tax = tx.get("tax", 0)
        payment_method = tx.get("payment_method", "")

        # Returns have negative amounts — use Qty -1 with positive prices
        is_return = amount_inc_tax < 0
        qty = -1 if is_return else 1
        price = abs(amount_inc_tax)
        tax_amount = abs(tax)
        payment_amount = amount_inc_tax

        # Build TxDate from report_date (set time to 00:00:00)
        tx_date = f"{report_date}T00:00:00" if report_date else ""

        entry = {
            "PartnerTransaction_ID": receipt_no,
            "StoreCode": store_code,
            "TxDate": tx_date,
            "Register_ID": config.REGISTER_ID,
            "LineItems": [
                {
                    "LineNumber": 1,
                    "Item": {
                        "Type": "Product",
                        "ProductCodeType": "PLU_ColorSize",
                        "ProductCode": "Dummy",
                        "ColorDesc": "NA",
                        "SizeDesc": "NA",
                        "Qty": qty,
                        "RetailPrice": price,
                        "SoldPrice": price,
                    },
                }
            ],
            "Payment": [
                {
                    "PaymentMethodCode": payment_method,
                    "Amount": payment_amount,
                }
            ],
            "Tax": {
                "TaxAmount": tax_amount,
                "TaxIncludedInLineItem": True,
            },
        }
        output.append(entry)

    return output


def write_json(parsed_data, store_code):
    """Transform parsed data and write to a local JSON file.

    Args:
        parsed_data (dict): Output from claude_parser.
        store_code (str | None): Resolved store code (see config.resolve_store_code).
            ``None`` means the store could not be identified — the email is skipped.

    Returns:
        tuple: (status, file_path) where status is "success", "skipped", or "error"
               and file_path is the path to the written file (or None).
    """
    try:
        if not store_code:
            logger.warning(
                "Could not resolve store for sender_email '%s' — skipping",
                parsed_data.get("sender_email", ""),
            )
            return "skipped", None

        report_date = parsed_data.get("report_date", "")
        transactions = _transform_transactions(parsed_data, store_code)

        # Build filename: AU_Owndays_{YYYYMMDD}_{HHmmss}.json
        now = datetime.now()
        date_part = report_date.replace("-", "") if report_date else now.strftime("%Y%m%d")
        time_part = now.strftime("%H%M%S")
        filename = f"AU_Owndays_{date_part}_{time_part}.json"

        # Ensure output directory exists (organized by date)
        output_dir = Path(config.JSON_OUTPUT_DIR) / date_part
        output_dir.mkdir(parents=True, exist_ok=True)

        file_path = output_dir / filename
        file_path.write_text(
            json.dumps(transactions, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )

        logger.info(
            "Wrote %d transactions to %s", len(transactions), file_path
        )
        return "success", str(file_path)

    except Exception:
        logger.exception("Failed to write JSON for sender_email=%s", parsed_data.get("sender_email"))
        return "error", None
