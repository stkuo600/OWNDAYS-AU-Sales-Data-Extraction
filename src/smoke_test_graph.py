"""
smoke_test_graph.py — read-only connectivity check for the Microsoft Graph migration.

Implements Phase 1 of docs/gmail-to-m365-migration-plan.md. Verifies, WITHOUT changing any
message state (no mark-as-read), that:
  1. The app-only credential works and a Graph token is acquired.
  2. The app can read the configured central mailbox (permission + RBAC scope are correct).
  3. The Prefer text-body header and from/subject/sentDateTime parsing work.
  4. PDF attachments come back as standard base64 (first bytes are the '%PDF' magic).

Run:
    python src/smoke_test_graph.py            # inspect the newest message with attachments
    python src/smoke_test_graph.py --unread   # restrict to unread + has-attachment messages

Exit code 0 on success, 1 on any failure.
"""

import argparse
import base64
import sys
import urllib.parse

import config
import graph_reader as g


def _print(label, value=""):
    print(f"  {label:<22} {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Graph connectivity smoke test (read-only)")
    parser.add_argument("--unread", action="store_true",
                        help="Restrict to unread messages that have attachments")
    args = parser.parse_args()

    cred_mode = "certificate" if (config.GRAPH_CERT_THUMBPRINT and config.GRAPH_CERT_KEY_FILE) else "client secret"
    print("=" * 60)
    print("Graph smoke test (READ-ONLY — no message is marked as read)")
    print("=" * 60)
    _print("Mailbox:", config.GRAPH_MAILBOX)
    _print("Tenant:", config.GRAPH_TENANT_ID)
    _print("Credential mode:", cred_mode)
    _print("Report timezone:", config.REPORT_TIMEZONE)
    print()

    # 1. Token ---------------------------------------------------------------
    print("[1] Acquiring app-only token ...")
    try:
        token = g.get_graph_token()
    except Exception as exc:
        print(f"    FAIL: {exc}")
        return 1
    print("    OK — token acquired.\n")

    # 2. List one message ----------------------------------------------------
    filt = "isRead eq false and hasAttachments eq true" if args.unread else "hasAttachments eq true"
    # No $orderby: combining it with $filter on a different property raises
    # InefficientFilter (graph_reader sorts client-side for the same reason).
    params = urllib.parse.urlencode({
        "$filter": filt,
        "$select": g._MESSAGE_SELECT,
        "$top": 1,
    })
    url = g._mailbox_url("messages") + "?" + params
    print(f"[2] GET /users/{{mailbox}}/messages  (filter: {filt})")
    try:
        payload = g._graph_get(token, url, headers={"Prefer": g._PREFER_TEXT_BODY})
    except Exception as exc:
        print(f"    FAIL: {exc}")
        return 1
    messages = payload.get("value", [])
    print(f"    OK — endpoint reachable, {len(messages)} message(s) returned.\n")

    if not messages:
        print("[3] No matching message to inspect — connectivity is fine, but send a test")
        print("    email with a PDF attachment to the mailbox to verify parsing.")
        return 0

    # 3. Inspect message fields ---------------------------------------------
    msg = messages[0]
    from_addr = (msg.get("from") or {}).get("emailAddress") or {}
    body = (msg.get("body") or {}).get("content", "")
    body_type = (msg.get("body") or {}).get("contentType", "?")
    print("[3] Newest matching message:")
    _print("from:", f'{from_addr.get("name", "")} <{from_addr.get("address", "")}>')
    _print("subject:", msg.get("subject", ""))
    _print("sentDateTime (UTC):", msg.get("sentDateTime", ""))
    _print("send_date (local):", g._utc_iso_to_local_date(msg.get("sentDateTime", "")))
    _print("body contentType:", body_type)
    _print("body length:", len(body))
    if body_type != "text":
        print("    WARN: body is not plain text — check the Prefer header handling.")
    print()

    # 4. Attachments ---------------------------------------------------------
    print("[4] PDF attachments:")
    try:
        attachments = g._fetch_pdf_attachments(token, msg["id"])
    except Exception as exc:
        print(f"    FAIL: {exc}")
        return 1
    if not attachments:
        print("    (no PDF attachments on this message)")
        return 0
    all_ok = True
    for att in attachments:
        try:
            head = base64.b64decode(att["data_base64"][:12])
            is_pdf = head[:4] == b"%PDF"
        except Exception:
            is_pdf = False
        all_ok = all_ok and is_pdf
        _print(att["filename"], "%PDF magic OK" if is_pdf else "NOT a valid PDF / bad base64")
    print()
    if all_ok:
        print("RESULT: ALL CHECKS PASSED")
        return 0
    print("RESULT: attachment validation FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
