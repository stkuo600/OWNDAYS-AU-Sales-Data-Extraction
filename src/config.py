import re
from pathlib import Path

from dotenv import dotenv_values

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_env = dotenv_values(_PROJECT_ROOT / ".env")

# Anthropic
ANTHROPIC_API_KEY = _env.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = _env.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# Azure AD Service Principal
AZURE_TENANT_ID = _env.get("AZURE_TENANT_ID")
AZURE_CLIENT_ID = _env.get("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = _env.get("AZURE_CLIENT_SECRET")

# Fabric Warehouse
FABRIC_SERVER = _env.get("FABRIC_SERVER")
FABRIC_DATABASE = _env.get("FABRIC_DATABASE")
FABRIC_SCHEMA = _env.get("FABRIC_SCHEMA")

# Gmail (resolve paths relative to project root)
GMAIL_CREDENTIALS_FILE = str(_PROJECT_ROOT / _env.get("GMAIL_CREDENTIALS_FILE", "credentials.json"))
GMAIL_TOKEN_FILE = str(_PROJECT_ROOT / _env.get("GMAIL_TOKEN_FILE", "gmail_token.json"))

# Logging
LOG_FILE = str(_PROJECT_ROOT / _env.get("LOG_FILE", "eod_processor.log"))

# SMTP Notification
SMTP_SERVER = _env.get("SMTP_SERVER")
SMTP_PORT = int(_env.get("SMTP_PORT") or "25")
SMTP_FROM_EMAIL = _env.get("SMTP_FROM_EMAIL")
SMTP_FROM_NAME = _env.get("SMTP_FROM_NAME", "EOD Processor")
SMTP_TO_SUCCESS = _env.get("SMTP_TO_SUCCESS", "")
SMTP_TO_ERROR = _env.get("SMTP_TO_ERROR", "")

# Validate required settings
_required = {
    "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    "AZURE_CLIENT_SECRET": AZURE_CLIENT_SECRET,
    "AZURE_TENANT_ID": AZURE_TENANT_ID,
    "AZURE_CLIENT_ID": AZURE_CLIENT_ID,
    "FABRIC_SERVER": FABRIC_SERVER,
    "FABRIC_DATABASE": FABRIC_DATABASE,
    "FABRIC_SCHEMA": FABRIC_SCHEMA,
}
_missing = [k for k, v in _required.items() if not v]
if _missing:
    raise RuntimeError(f"Missing required settings in .env: {', '.join(_missing)}")

if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', FABRIC_SCHEMA):
    raise RuntimeError(f"FABRIC_SCHEMA '{FABRIC_SCHEMA}' is not a valid SQL identifier")
