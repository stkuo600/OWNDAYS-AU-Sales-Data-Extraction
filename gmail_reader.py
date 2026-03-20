"""
gmail_reader.py — Gmail integration for the OWNDAYS EOD Report Processor.

Provides three public functions:
  - get_gmail_service()        : authenticate and return a Gmail API service object
  - fetch_unread_emails()      : retrieve unread emails that carry PDF attachments
  - mark_as_read()             : remove the UNREAD label from a message
"""

import base64
import email.utils
import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import config

logger = logging.getLogger(__name__)

# OAuth 2.0 scope — gmail.modify allows reading messages and changing labels.
_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def get_gmail_service():
    """Authenticate with the Gmail API and return a service object.

    Token is loaded from ``config.GMAIL_TOKEN_FILE`` when it exists.  If the
    file is absent, or the token is invalid/expired without a refresh token,
    the OAuth installed-app flow is launched interactively.  An expired token
    that still holds a refresh token is silently refreshed.  The (possibly
    updated) credentials are always persisted back to disk before returning.

    Returns
    -------
    googleapiclient.discovery.Resource
        An authorised Gmail v1 service object.
    """
    creds = None

    # --- Load existing token from disk ---
    if os.path.exists(config.GMAIL_TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(
                config.GMAIL_TOKEN_FILE, _SCOPES
            )
            logger.debug("Loaded Gmail token from %s", config.GMAIL_TOKEN_FILE)
        except Exception:
            logger.warning(
                "Could not load token from %s; will re-authenticate.",
                config.GMAIL_TOKEN_FILE,
                exc_info=True,
            )
            creds = None

    # --- Refresh or run the interactive flow ---
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Gmail token expired — refreshing automatically.")
            creds.refresh(Request())
        else:
            logger.info(
                "No valid Gmail token found — launching OAuth flow using %s.",
                config.GMAIL_CREDENTIALS_FILE,
            )
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GMAIL_CREDENTIALS_FILE, _SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Persist updated credentials so the next run is silent.
        with open(config.GMAIL_TOKEN_FILE, "w", encoding="utf-8") as token_file:
            token_file.write(creds.to_json())
        logger.debug("Saved updated Gmail token to %s", config.GMAIL_TOKEN_FILE)

    service = build("gmail", "v1", credentials=creds)
    logger.info("Gmail service initialised successfully.")
    return service


# ---------------------------------------------------------------------------
# Email fetching helpers
# ---------------------------------------------------------------------------

def _decode_body(part) -> str:
    """Return the decoded UTF-8 string from a message part's body data."""
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    raw_bytes = base64.urlsafe_b64decode(data + "==")  # pad defensively
    return raw_bytes.decode("utf-8", errors="replace")


def _extract_text_body(payload: dict) -> str:
    """Recursively walk *payload* and return the first text/plain body found.

    Handles:
    - Simple messages whose body sits directly in ``payload.body``
    - ``multipart/alternative`` (text/plain + text/html) — prefers text/plain
    - Arbitrarily nested multipart structures
    """
    mime_type = payload.get("mimeType", "")

    # Leaf part — text/plain
    if mime_type == "text/plain":
        return _decode_body(payload)

    # Leaf part — not text/plain (e.g. text/html), skip
    if not mime_type.startswith("multipart/"):
        return ""

    # Multipart — recurse into sub-parts; collect text/plain first
    parts = payload.get("parts", [])
    plain_text = ""
    fallback_text = ""

    for part in parts:
        part_mime = part.get("mimeType", "")
        if part_mime == "text/plain":
            result = _decode_body(part)
            if result:
                plain_text = result
                break  # prefer the first text/plain found
        elif part_mime.startswith("multipart/"):
            result = _extract_text_body(part)
            if result and not plain_text:
                fallback_text = result
        elif part_mime == "text/html" and not fallback_text:
            # Keep html as a last resort but don't return it — just note it.
            pass

    return plain_text or fallback_text


def _extract_pdf_attachments(service, user_id: str, message_id: str, payload: dict) -> list:
    """Return a list of PDF attachment dicts for *message_id*.

    Each dict has the shape::

        {"filename": str, "data_base64": str}

    where ``data_base64`` is standard (non-urlsafe) base64-encoded PDF bytes.

    Parameters
    ----------
    service:
        Authorised Gmail API service object.
    user_id:
        Gmail user ID (typically ``"me"``).
    message_id:
        The Gmail message ID owning these parts.
    payload:
        The ``payload`` field of the full message resource.
    """
    attachments = []
    parts = payload.get("parts", [])

    for part in parts:
        mime_type = part.get("mimeType", "")

        # Recurse into nested multipart containers.
        if mime_type.startswith("multipart/"):
            attachments.extend(
                _extract_pdf_attachments(service, user_id, message_id, part)
            )
            continue

        if mime_type != "application/pdf":
            continue

        filename = part.get("filename") or "attachment.pdf"
        attachment_id = part.get("body", {}).get("attachmentId")
        if not attachment_id:
            logger.warning(
                "PDF part in message %s has no attachmentId — skipping.", message_id
            )
            continue

        logger.debug(
            "Downloading attachment '%s' (id=%s) from message %s.",
            filename,
            attachment_id,
            message_id,
        )
        response = (
            service.users()
            .messages()
            .attachments()
            .get(userId=user_id, messageId=message_id, id=attachment_id)
            .execute()
        )

        # Gmail returns urlsafe base64; convert to standard base64.
        urlsafe_data = response.get("data", "")
        standard_b64 = base64.b64encode(
            base64.urlsafe_b64decode(urlsafe_data)
        ).decode("utf-8")

        attachments.append({"filename": filename, "data_base64": standard_b64})

    return attachments


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_unread_emails(service) -> list:
    """Fetch unread emails that contain at least one PDF attachment.

    Queries Gmail for messages matching ``is:unread has:attachment``, then
    retrieves each message in full to extract headers, body text, and PDF
    attachments.  Messages with no PDF attachments are skipped (logged at
    DEBUG level).

    Parameters
    ----------
    service:
        Authorised Gmail API service object (from :func:`get_gmail_service`).

    Returns
    -------
    list[dict]
        Each item has the shape::

            {
                "message_id":   str,
                "sender_name":  str,
                "sender_email": str,
                "subject":      str,
                "body":         str,
                "attachments":  [{"filename": str, "data_base64": str}],
            }
    """
    user_id = "me"
    query = "is:unread has:attachment"
    logger.info("Searching Gmail with query: '%s'", query)

    results = (
        service.users()
        .messages()
        .list(userId=user_id, q=query)
        .execute()
    )

    messages = results.get("messages", [])
    logger.info("Found %d message(s) matching query.", len(messages))

    emails = []

    for msg_stub in messages:
        msg_id = msg_stub["id"]

        # Fetch the full message.
        message = (
            service.users()
            .messages()
            .get(userId=user_id, id=msg_id, format="full")
            .execute()
        )

        payload = message.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}

        # --- Parse sender ---
        raw_from = headers.get("From", "")
        sender_name, sender_email = email.utils.parseaddr(raw_from)

        # --- Subject ---
        subject = headers.get("Subject", "")

        # --- Body ---
        body = _extract_text_body(payload)

        # --- PDF attachments ---
        pdf_attachments = _extract_pdf_attachments(service, user_id, msg_id, payload)

        if not pdf_attachments:
            logger.debug(
                "Message %s ('%s') has no PDF attachments — skipping.", msg_id, subject
            )
            continue

        email_dict = {
            "message_id": msg_id,
            "sender_name": sender_name,
            "sender_email": sender_email,
            "subject": subject,
            "body": body,
            "attachments": pdf_attachments,
        }

        logger.info(
            "Collected message %s from <%s> with %d PDF attachment(s): '%s'.",
            msg_id,
            sender_email,
            len(pdf_attachments),
            subject,
        )
        emails.append(email_dict)

    logger.info(
        "Returning %d email(s) with PDF attachments out of %d unread message(s).",
        len(emails),
        len(messages),
    )
    return emails


def mark_as_read(service, message_id: str) -> None:
    """Remove the UNREAD label from the specified Gmail message.

    Parameters
    ----------
    service:
        Authorised Gmail API service object (from :func:`get_gmail_service`).
    message_id:
        The Gmail message ID to mark as read.
    """
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()
    logger.info("Marked message %s as read.", message_id)
