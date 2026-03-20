import os

# Anthropic
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# Azure AD Service Principal
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID", "6935b5fa-b1fc-433f-91d1-74905254de17")
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "254aedc1-a707-4768-b552-b9f07c28eafb")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET")

# Fabric Warehouse
FABRIC_SERVER = os.environ.get(
    "FABRIC_SERVER",
    "7k2tk2p4we7uheorosifevg6c4-5mvpeldfy2pepno5srpjp3k564.datawarehouse.fabric.microsoft.com",
)
FABRIC_DATABASE = os.environ.get("FABRIC_DATABASE", "gold_warehouse")
FABRIC_SCHEMA = os.environ.get("FABRIC_SCHEMA", "ownd")

# Gmail
GMAIL_CREDENTIALS_FILE = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "gmail_token.json")

# Logging
LOG_FILE = os.environ.get("LOG_FILE", "eod_processor.log")

# Validate required secrets
if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY environment variable is required")
if not AZURE_CLIENT_SECRET:
    raise RuntimeError("AZURE_CLIENT_SECRET environment variable is required")
