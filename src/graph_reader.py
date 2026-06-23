"""
graph_reader.py — Microsoft 365 (Microsoft Graph) integration for the OWNDAYS EOD Report Processor.

Replaces the former Gmail integration. Reads daily sales-report emails from ONE central
Exchange Online mailbox and distinguishes stores by SENDER address (see config.STORE_MAP).

Authentication is app-only (OAuth 2.0 client-credentials) — no browser, no user, no refresh
token. A certificate credential is preferred for production; a client secret is supported for
development. See docs/gmail-to-m365-migration-plan.md.

Public functions (mirror the old gmail_reader API; ``service`` is replaced by a bearer ``token``):
  - get_graph_token()                  : acquire an app-only Graph access token
  - fetch_unread_emails(token)         : unread emails that carry PDF attachments
  - fetch_emails_in_date_range(...)    : emails from given senders within a date range (backfill)
  - mark_as_read(token, message_id)    : set isRead=true and tag the 'EOD_Processed' category
"""

import logging
import time
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import msal
import requests

import config

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_SCOPES = ["https://graph.microsoft.com/.default"]  # the only legal scope for client-credentials

# Fields requested per message. 'body' is returned as plain text via the Prefer header below.
_MESSAGE_SELECT = "id,subject,from,sentDateTime,receivedDateTime,hasAttachments,body"
_PREFER_TEXT_BODY = 'outlook.body-content-type="text"'
_PAGE_SIZE = 50

# Transport-layer retry for throttling (429) and transient 5xx. This is NOT business retry —
# the "unread email = retry queue" model in main.py is unchanged.
_MAX_RETRIES = 4

_PROCESSED_CATEGORY = "EOD_Processed"

# Module-level MSAL app cache (one confidential-client app per process).
_msal_app = None


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def get_graph_token() -> str:
    """Acquire an app-only Microsoft Graph access token via client-credentials.

    Uses a certificate credential when ``GRAPH_CERT_THUMBPRINT`` + ``GRAPH_CERT_KEY_FILE``
    are configured, otherwise falls back to ``GRAPH_CLIENT_SECRET`` (development only).
    MSAL caches the token in memory, so calling this once per run is sufficient.

    Returns
    -------
    str
        A bearer access token for ``https://graph.microsoft.com``.
    """
    global _msal_app

    if _msal_app is None:
        if config.GRAPH_CERT_THUMBPRINT and config.GRAPH_CERT_KEY_FILE:
            with open(config.GRAPH_CERT_KEY_FILE, "r", encoding="utf-8") as key_file:
                private_key = key_file.read()
            client_credential = {
                "thumbprint": config.GRAPH_CERT_THUMBPRINT,
                "private_key": private_key,
            }
            logger.info("Initialising MSAL app with certificate credential.")
        else:
            client_credential = config.GRAPH_CLIENT_SECRET  # dev only
            logger.info("Initialising MSAL app with client secret (development credential).")

        _msal_app = msal.ConfidentialClientApplication(
            client_id=config.GRAPH_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{config.GRAPH_TENANT_ID}",
            client_credential=client_credential,
        )

    result = _msal_app.acquire_token_for_client(scopes=_SCOPES)
    if "access_token" not in result:
        raise RuntimeError(
            "Graph token acquisition failed: "
            f"{result.get('error')}: {result.get('error_description')}"
        )

    logger.info("Acquired Microsoft Graph app-only token.")
    return result["access_token"]


# ---------------------------------------------------------------------------
# HTTP helpers (with throttling-aware retry)
# ---------------------------------------------------------------------------

def _request(method: str, url: str, token: str, **kwargs) -> requests.Response:
    """Issue a Graph request, retrying on 429/5xx honouring Retry-After."""
    headers = kwargs.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {token}"

    last_resp = None
    for attempt in range(_MAX_RETRIES):
        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        last_resp = resp
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt < _MAX_RETRIES - 1:
                delay = int(resp.headers.get("Retry-After", 2 ** attempt))
                logger.warning(
                    "Graph %s %s -> HTTP %d; retrying in %ds (attempt %d/%d).",
                    method, url, resp.status_code, delay, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(delay)
                continue
        resp.raise_for_status()
        return resp

    # Retries exhausted on a throttling/5xx response.
    last_resp.raise_for_status()
    return last_resp


def _graph_get(token: str, url: str, headers: dict = None) -> dict:
    return _request("GET", url, token, headers=headers).json()


# ---------------------------------------------------------------------------
# Date / time helpers
# ---------------------------------------------------------------------------

def _utc_iso_to_local_date(iso_str: str) -> str:
    """Convert a Graph UTC datetime (e.g. '2026-06-04T09:15:23Z') to a local YYYY-MM-DD.

    Graph mail datetimes are UTC. The local date is computed in ``config.REPORT_TIMEZONE``
    (default Australia/Sydney) so the day attribution matches the previous Gmail behaviour,
    which derived send_date from the sender's local-time Date header.
    """
    if not iso_str:
        return ""
    base = iso_str[:19]  # YYYY-MM-DDTHH:MM:SS — drop any fractional seconds / 'Z'
    dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo(config.REPORT_TIMEZONE)).strftime("%Y-%m-%d")


def _local_date_to_utc_literal(date_str: str, add_days: int = 0) -> str:
    """Convert a local (REPORT_TIMEZONE) calendar date at midnight to a UTC OData literal.

    Used to build receivedDateTime range filters whose boundaries are local-day midnights.
    """
    local_midnight = (
        datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=ZoneInfo(config.REPORT_TIMEZONE))
        + timedelta(days=add_days)
    )
    return local_midnight.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Message / attachment fetching
# ---------------------------------------------------------------------------

def _mailbox_url(*segments: str) -> str:
    """Build a Graph URL rooted at the configured central mailbox."""
    mailbox = urllib.parse.quote(config.GRAPH_MAILBOX)
    path = "/".join(segments)
    return f"{_GRAPH_BASE}/users/{mailbox}/{path}"


def _list_messages(token: str, odata_filter: str) -> list:
    """List messages matching *odata_filter*, following @odata.nextLink pages.

    The body is requested as plain text via the Prefer header. The Prefer header is
    re-sent on every page (including nextLink follow-ups).
    """
    params = urllib.parse.urlencode({
        "$filter": odata_filter,
        "$select": _MESSAGE_SELECT,
        "$top": _PAGE_SIZE,
    })
    url = _mailbox_url("messages") + "?" + params
    prefer_headers = {"Prefer": _PREFER_TEXT_BODY}

    messages = []
    while url:
        payload = _graph_get(token, url, headers=prefer_headers)
        messages.extend(payload.get("value", []))
        url = payload.get("@odata.nextLink")  # absolute URL with params baked in — use as-is

    # Sort newest-first. We deliberately omit $orderby (combining it with the date-range
    # $filter risks Graph 'InefficientFilter' errors), so order it client-side. ISO 8601
    # datetimes sort lexically. main.py's dedup keeps the first occurrence per
    # (sender, send_date), so this preserves the prior "newest wins" behaviour.
    messages.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
    return messages


def _fetch_pdf_attachments(token: str, message_id: str) -> list:
    """Return PDF file-attachments for *message_id* as [{"filename", "data_base64"}].

    Graph contentBytes is already standard (RFC 4648) base64, so it is passed straight
    through to the AI API — no urlsafe conversion is needed (unlike Gmail).

    No $select is used: contentBytes exists only on the derived fileAttachment type, and
    selecting it on the polymorphic attachments collection raises a 400 BadRequest. The
    default projection already includes contentBytes for file attachments.
    """
    url = _mailbox_url("messages", urllib.parse.quote(message_id), "attachments")

    attachments = []
    while url:
        payload = _graph_get(token, url)
        for att in payload.get("value", []):
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            name = att.get("name") or "attachment.pdf"
            content_type = (att.get("contentType") or "").lower()
            is_pdf = content_type == "application/pdf" or name.lower().endswith(".pdf")
            if not is_pdf:
                continue
            content_bytes = att.get("contentBytes")
            if not content_bytes:
                logger.warning(
                    "PDF attachment '%s' on message %s has no contentBytes — skipping.",
                    name, message_id,
                )
                continue
            attachments.append({"filename": name, "data_base64": content_bytes})
        url = payload.get("@odata.nextLink")
    return attachments


def _fetch_emails(token: str, odata_filter: str, sender_filter: set = None) -> list:
    """Fetch messages matching *odata_filter*, returning PDF-bearing email dicts.

    When *sender_filter* is provided, messages whose sender is not in the set are dropped
    BEFORE the (per-message) attachment fetch. Filtering senders client-side avoids the
    Graph 'InefficientFilter' errors that combining sender + date $filter clauses can raise.

    Returns
    -------
    list[dict]
        Each item has the shape::

            {
                "message_id":   str,
                "sender_name":  str,
                "sender_email": str,
                "subject":      str,
                "send_date":    str,  # YYYY-MM-DD in REPORT_TIMEZONE
                "body":         str,
                "attachments":  [{"filename": str, "data_base64": str}],
            }
    """
    logger.info("Listing Graph messages with $filter: %s", odata_filter)
    messages = _list_messages(token, odata_filter)
    logger.info("Found %d message(s) matching filter.", len(messages))

    # Email addresses are case-insensitive — normalise the filter for comparison.
    sender_filter_lc = {s.lower() for s in sender_filter} if sender_filter is not None else None

    emails = []
    for message in messages:
        msg_id = message["id"]

        from_addr = (message.get("from") or {}).get("emailAddress") or {}
        sender_email = from_addr.get("address", "")
        sender_name = from_addr.get("name", "")

        if sender_filter_lc is not None and sender_email.lower() not in sender_filter_lc:
            logger.debug(
                "Message %s from <%s> not in sender filter — skipping.", msg_id, sender_email
            )
            continue

        subject = message.get("subject", "")
        send_date = _utc_iso_to_local_date(message.get("sentDateTime", ""))
        body = (message.get("body") or {}).get("content", "")

        pdf_attachments = _fetch_pdf_attachments(token, msg_id)
        if not pdf_attachments:
            logger.debug(
                "Message %s ('%s') has no PDF attachments — skipping.", msg_id, subject
            )
            continue

        emails.append({
            "message_id": msg_id,
            "sender_name": sender_name,
            "sender_email": sender_email,
            "subject": subject,
            "send_date": send_date,
            "body": body,
            "attachments": pdf_attachments,
        })
        logger.info(
            "Collected message %s from <%s> with %d PDF attachment(s): '%s'.",
            msg_id, sender_email, len(pdf_attachments), subject,
        )

    logger.info(
        "Returning %d email(s) with PDF attachments out of %d matching message(s).",
        len(emails), len(messages),
    )
    return emails


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_unread_emails(token: str) -> list:
    """Fetch unread emails that contain at least one PDF attachment."""
    return _fetch_emails(token, "isRead eq false and hasAttachments eq true")


def fetch_emails_in_date_range(token: str, from_date: str, to_date: str, senders) -> list:
    """Fetch emails from *senders* within an inclusive local-date range (backfill mode).

    Parameters
    ----------
    from_date, to_date:
        Inclusive calendar dates ``YYYY-MM-DD`` interpreted in ``config.REPORT_TIMEZONE``.
    senders:
        Iterable of sender email addresses to scope the result (matched client-side).

    The Graph $filter scopes the server query to the date range and attachment presence;
    sender matching is applied client-side (see :func:`_fetch_emails`).
    """
    after_utc = _local_date_to_utc_literal(from_date)
    before_utc = _local_date_to_utc_literal(to_date, add_days=1)  # exclusive upper bound
    odata_filter = (
        f"receivedDateTime ge {after_utc} and receivedDateTime lt {before_utc} "
        "and hasAttachments eq true"
    )
    return _fetch_emails(token, odata_filter, sender_filter=set(senders))


def mark_as_read(token: str, message_id: str) -> None:
    """Set isRead=true and add the 'EOD_Processed' category on *message_id*.

    Equivalent to the old Gmail behaviour (remove UNREAD + add EOD_Processed label), but
    in a single PATCH with no label-id lookup. The category need not pre-exist.
    """
    url = _mailbox_url("messages", urllib.parse.quote(message_id))
    body = {"isRead": True, "categories": [_PROCESSED_CATEGORY]}
    _request("PATCH", url, token, json=body)
    logger.info("Marked message %s as read and categorised %s.", message_id, _PROCESSED_CATEGORY)
