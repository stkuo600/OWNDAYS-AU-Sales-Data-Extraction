"""
fabric_writer.py — Writes parsed EOD data to Microsoft Fabric Warehouse.

Uses Azure AD Service Principal (ClientSecretCredential) for token-based
authentication via pyodbc + ODBC Driver 18 for SQL Server.
"""

import struct
import logging
from datetime import datetime, timezone

import pyodbc
from azure.identity import ClientSecretCredential

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _get_connection():
    """Open and return a pyodbc connection to the Fabric Warehouse using
    Azure AD token authentication.  autocommit is disabled so the caller
    owns the transaction lifecycle."""

    credential = ClientSecretCredential(
        config.AZURE_TENANT_ID,
        config.AZURE_CLIENT_ID,
        config.AZURE_CLIENT_SECRET,
    )
    token = credential.get_token("https://database.windows.net/.default")
    token_bytes = token.token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)

    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={config.FABRIC_SERVER},1433;"
        f"Database={config.FABRIC_DATABASE};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    conn = pyodbc.connect(conn_str, attrs_before={1256: token_struct})
    conn.autocommit = False
    return conn


# ---------------------------------------------------------------------------
# Dimension lookups
# ---------------------------------------------------------------------------

def _get_store_id(cursor, sender_email):
    """Return store_id (int) for the given sender_email, or None if not found."""
    cursor.execute(
        f"SELECT store_id FROM {config.FABRIC_SCHEMA}.Dim_Store WHERE sender_email = ?",
        sender_email,
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _get_method_id(cursor, method_code):
    """Return method_id for the given method_code, or None if not found.
    Nullable — callers must handle a None return gracefully."""
    if not method_code:
        return None
    cursor.execute(
        f"SELECT method_id FROM {config.FABRIC_SCHEMA}.Dim_PaymentMethod WHERE method_code = ?",
        method_code,
    )
    row = cursor.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Delete (atomic replace)
# ---------------------------------------------------------------------------

def _delete_existing(cursor, report_date, store_id):
    """Delete any existing Fact_EOD_Summary rows (and their child
    Fact_EOD_Transaction rows) for the given report_date + store_id.

    Returns the number of summary rows deleted (0 if none existed)."""

    # Collect existing summary_ids for this date + store
    cursor.execute(
        f"""
        SELECT summary_id
        FROM {config.FABRIC_SCHEMA}.Fact_EOD_Summary
        WHERE report_date = ? AND store_id = ?
        """,
        report_date,
        store_id,
    )
    rows = cursor.fetchall()
    summary_ids = [r[0] for r in rows]

    if not summary_ids:
        return 0

    # Build a parameterised IN clause
    placeholders = ", ".join("?" for _ in summary_ids)

    # Delete child transactions first (referential integrity)
    cursor.execute(
        f"""
        DELETE FROM {config.FABRIC_SCHEMA}.Fact_EOD_Transaction
        WHERE summary_id IN ({placeholders})
        """,
        *summary_ids,
    )
    tx_deleted = cursor.rowcount

    # Delete parent summaries
    cursor.execute(
        f"""
        DELETE FROM {config.FABRIC_SCHEMA}.Fact_EOD_Summary
        WHERE summary_id IN ({placeholders})
        """,
        *summary_ids,
    )
    summary_deleted = cursor.rowcount

    logger.info(
        "Deleted %d existing summary row(s) and %d transaction row(s) "
        "for report_date=%s store_id=%s",
        summary_deleted,
        tx_deleted,
        report_date,
        store_id,
    )
    return summary_deleted


# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------

def _insert_summary(cursor, data, store_id, processed_at):
    """INSERT one row into Fact_EOD_Summary and return the generated summary_id."""

    report_date = data.get("report_date")

    cursor.execute(
        f"""
        INSERT INTO {config.FABRIC_SCHEMA}.Fact_EOD_Summary
            (report_date, store_id, banking_no, total_inc_gst, total_tax,
             total_exc_gst, transaction_count, target_exc_gst, consultation,
             no_customers, daily_comment, customer_feedback, sender_email,
             _processed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        report_date,
        store_id,
        data.get("banking_no"),
        data.get("total_inc_gst"),
        data.get("total_tax"),
        data.get("total_exc_gst"),
        data.get("transaction_count"),
        data.get("target_exc_gst"),
        data.get("consultation"),
        data.get("no_customers"),
        data.get("daily_comment"),
        data.get("customer_feedback"),
        data.get("sender_email"),
        processed_at,
    )

    # Fabric doesn't support @@IDENTITY or OUTPUT — retrieve by unique key
    cursor.execute(
        f"SELECT summary_id FROM {config.FABRIC_SCHEMA}.Fact_EOD_Summary "
        f"WHERE report_date = ? AND store_id = ? AND _processed_at = ?",
        report_date, store_id, processed_at,
    )
    summary_id = cursor.fetchone()[0]
    logger.info("Inserted Fact_EOD_Summary summary_id=%s", summary_id)
    return summary_id


def _insert_transactions(cursor, transactions, summary_id, store_id, report_date, processed_at):
    """INSERT each transaction dict into Fact_EOD_Transaction."""
    count = 0

    for tx in transactions:
        method_code = tx.get("payment_method") or tx.get("method_code")
        method_id = _get_method_id(cursor, method_code)

        cursor.execute(
            f"""
            INSERT INTO {config.FABRIC_SCHEMA}.Fact_EOD_Transaction
                (summary_id, report_date, store_id, receipt_no, method_id,
                 method_code, customer_name, customer_id, amount_inc_tax,
                 tax, amount_exc_tax, _processed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            summary_id,
            report_date,
            store_id,
            tx.get("receipt_no") or None,
            method_id,
            method_code,
            tx.get("customer_name"),
            tx.get("customer_id") or None,
            tx.get("amount_inc_tax"),
            tx.get("tax"),
            tx.get("amount_exc_tax"),
            processed_at,
        )
        count += 1

    logger.info(
        "Inserted %d transaction row(s) for summary_id=%s", count, summary_id
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def write_eod_data(parsed_data):
    """Main entry point: persist one email's parsed EOD data to Fabric.

    Args:
        parsed_data (dict): Output from claude_parser — contains summary
            fields plus a 'transactions' list.

    Returns:
        str: "success", "skipped", or "error"
    """

    conn = None
    try:
        conn = _get_connection()
        cursor = conn.cursor()

        # 1. Resolve store
        sender_email = parsed_data.get("sender_email")
        store_id = _get_store_id(cursor, sender_email)
        if store_id is None:
            logger.warning(
                "sender_email '%s' not found in Dim_Store — skipping", sender_email
            )
            conn.close()
            return "skipped"

        report_date = parsed_data.get("report_date")
        logger.info(
            "Writing EOD data: sender_email=%s store_id=%s report_date=%s",
            sender_email,
            store_id,
            report_date,
        )

        # 2. Atomic replace — delete any pre-existing rows for this date+store
        _delete_existing(cursor, report_date, store_id)

        # 3. Insert summary + transactions with a single timestamp
        processed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        summary_id = _insert_summary(cursor, parsed_data, store_id, processed_at)

        # 4. Insert transactions
        transactions = parsed_data.get("transactions") or []
        _insert_transactions(cursor, transactions, summary_id, store_id, report_date, processed_at)

        # 5. Commit and clean up
        conn.commit()
        conn.close()
        logger.info(
            "write_eod_data SUCCESS: summary_id=%s store_id=%s report_date=%s",
            summary_id,
            store_id,
            report_date,
        )
        return "success"

    except Exception:
        logger.exception(
            "write_eod_data FAILED for sender_email=%s — rolling back",
            parsed_data.get("sender_email") if parsed_data else "unknown",
        )
        if conn:
            try:
                conn.rollback()
            except Exception:
                logger.exception("Rollback also failed")
            try:
                conn.close()
            except Exception:
                pass
        return "error"
