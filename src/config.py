import json
from pathlib import Path

from dotenv import dotenv_values

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_env = dotenv_values(_PROJECT_ROOT / ".env")

# AI Provider
AI_PROVIDER = (_env.get("AI_PROVIDER") or "anthropic").lower()

# Anthropic
ANTHROPIC_API_KEY = _env.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = _env.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# Azure OpenAI
AZURE_OPENAI_ENDPOINT = _env.get("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = _env.get("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_DEPLOYMENT = _env.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

# Microsoft Graph (app-only / client-credentials)
GRAPH_TENANT_ID = _env.get("GRAPH_TENANT_ID")
GRAPH_CLIENT_ID = _env.get("GRAPH_CLIENT_ID")
GRAPH_MAILBOX = _env.get("GRAPH_MAILBOX")  # central mailbox UPN/SMTP the job reads from
# Credential: certificate (production) takes precedence over client secret (dev) when present.
GRAPH_CERT_THUMBPRINT = _env.get("GRAPH_CERT_THUMBPRINT")
GRAPH_CERT_KEY_FILE = (
    str(_PROJECT_ROOT / _env["GRAPH_CERT_KEY_FILE"]) if _env.get("GRAPH_CERT_KEY_FILE") else None
)
GRAPH_CLIENT_SECRET = _env.get("GRAPH_CLIENT_SECRET")  # dev only

# Timezone used to derive send_date (local calendar day) from Graph UTC datetimes.
REPORT_TIMEZONE = _env.get("REPORT_TIMEZONE", "Australia/Sydney")

# Logging
LOG_FILE = str(_PROJECT_ROOT / _env.get("LOG_FILE", "eod_processor.log"))

# SMTP Notification
SMTP_SERVER = _env.get("SMTP_SERVER")
SMTP_PORT = int(_env.get("SMTP_PORT") or "25")
SMTP_FROM_EMAIL = _env.get("SMTP_FROM_EMAIL")
SMTP_FROM_NAME = _env.get("SMTP_FROM_NAME", "EOD Processor")
SMTP_TO_SUCCESS = _env.get("SMTP_TO_SUCCESS", "")
SMTP_TO_ERROR = _env.get("SMTP_TO_ERROR", "")

# JSON Output
JSON_OUTPUT_DIR = str(_PROJECT_ROOT / _env.get("JSON_OUTPUT_DIR", "output"))
REGISTER_ID = int(_env.get("REGISTER_ID") or "1")

# Store email → store code mapping (JSON object in .env)
# e.g. {"sydney@owndays.com.au": "OWND01", "owndaysburwood@gmail.com": "OWND02"}
_store_map_raw = _env.get("STORE_MAP", "{}")
try:
    STORE_MAP = json.loads(_store_map_raw)
except json.JSONDecodeError:
    raise RuntimeError(f"STORE_MAP is not valid JSON: {_store_map_raw}")

# Fallback store identification when the sender address is not in STORE_MAP — e.g. when
# reports are routed through the au.owndays.eod.report distribution list and the From
# header is rewritten (DMARC) to the list address. Maps a store-name keyword to a store
# code; matched (case-insensitively) against the From display name, then the email body.
# e.g. {"sydney": "OWND01", "burwood": "OWND02", "hurstville": "OWND03", "chatswood": "OWND04"}
_store_name_map_raw = _env.get("STORE_NAME_MAP", "{}")
try:
    STORE_NAME_MAP = json.loads(_store_name_map_raw)
except json.JSONDecodeError:
    raise RuntimeError(f"STORE_NAME_MAP is not valid JSON: {_store_name_map_raw}")

# Distribution-list / forwarder addresses through which store EOD reports may arrive
# (the From header is rewritten to these for DMARC-protected domains). Comma-separated.
# Used only to widen the backfill sender filter so rerouted mail is still fetched; the
# actual store is then resolved via resolve_store_code(). Normal (unread) mode is
# unfiltered and does not need this.
STORE_FORWARDER_ADDRESSES = [
    s.strip() for s in (_env.get("STORE_FORWARDER_ADDRESSES", "") or "").split(",") if s.strip()
]

# SFTP (FTP_* names kept for backward compatibility)
FTP_HOST = _env.get("FTP_HOST")
FTP_PORT = int(_env.get("FTP_PORT") or "22")
FTP_USERNAME = _env.get("FTP_USERNAME")
FTP_PASSWORD = _env.get("FTP_PASSWORD")
FTP_DIRECTORY = _env.get("FTP_DIRECTORY", "/")

# Validate required settings
_required = {}
if AI_PROVIDER == "anthropic":
    _required["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY
elif AI_PROVIDER == "azure":
    _required["AZURE_OPENAI_ENDPOINT"] = AZURE_OPENAI_ENDPOINT
    _required["AZURE_OPENAI_API_KEY"] = AZURE_OPENAI_API_KEY
else:
    raise RuntimeError(f"AI_PROVIDER must be 'anthropic' or 'azure', got '{AI_PROVIDER}'")
# Microsoft Graph mail access is always required.
_required["GRAPH_TENANT_ID"] = GRAPH_TENANT_ID
_required["GRAPH_CLIENT_ID"] = GRAPH_CLIENT_ID
_required["GRAPH_MAILBOX"] = GRAPH_MAILBOX

_missing = [k for k, v in _required.items() if not v]
if _missing:
    raise RuntimeError(f"Missing required settings in .env: {', '.join(_missing)}")

# Exactly one Graph credential path must be configured.
_has_cert = bool(GRAPH_CERT_THUMBPRINT and GRAPH_CERT_KEY_FILE)
_has_secret = bool(GRAPH_CLIENT_SECRET)
if not (_has_cert or _has_secret):
    raise RuntimeError(
        "Missing Graph credential: set GRAPH_CERT_THUMBPRINT + GRAPH_CERT_KEY_FILE "
        "(production) or GRAPH_CLIENT_SECRET (development)"
    )

if not STORE_MAP:
    raise RuntimeError("STORE_MAP must contain at least one email→store code mapping")


def resolve_store_code(sender_email, sender_name="", body=""):
    """Resolve a store code for an EOD email, with a fallback for rerouted mail.

    Resolution order:
      1. Exact sender address match against STORE_MAP (deterministic; used when the
         original store From is preserved — direct sends and most list-forwarded mail).
      2. Fallback for when the distribution list rewrites From to the list address:
         match a STORE_NAME_MAP keyword (case-insensitive substring) against the From
         display name first, then the email body. If exactly one store matches in a
         given text it wins; if several different stores match (ambiguous) that text is
         skipped rather than guessed.

    Returns the store code, or ``None`` if no confident match (caller should skip + alert).
    """
    # Exact sender match (case-insensitive — email addresses are not case-sensitive).
    low_sender = (sender_email or "").lower()
    for addr, c in STORE_MAP.items():
        if addr.lower() == low_sender:
            return c

    for text in (sender_name or "", body or ""):
        low = text.lower()
        matched = {c for kw, c in STORE_NAME_MAP.items() if kw.lower() in low}
        if len(matched) == 1:
            return next(iter(matched))

    return None
