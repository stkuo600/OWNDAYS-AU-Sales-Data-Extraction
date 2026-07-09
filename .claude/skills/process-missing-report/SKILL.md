---
name: process-missing-report
description: Use when a store's EOD email never arrived but you have the Banking Transaction Report PDF on hand and need to get that day's sales onto the SFTP server manually (missing report, forgot to send, late report, backfill a single store/day). Covers parse -> JSON -> SFTP -> archive the PDF.
---

# Process a Missing EOD Report

## Overview

When a store forgets to send (or the email never arrives), the normal Graph
pipeline never sees that day's report, so nothing reaches the SFTP server. This
skill reprocesses a single **Banking Transaction Report PDF** through the exact
same pipeline modules `main.py` uses, then **archives the PDF** — the step people
most often forget.

## One command

```bash
python scripts/process_missing_report.py "<path-to.pdf>"
```

The script does all five steps in order and stops on the first failure:

1. **Parse** the PDF (`claude_parser`) — store is inferred from the filename
   (e.g. a name containing "Chatswood" → OWND04 via `STORE_NAME_MAP`).
2. **Reconcile** — extracted rows must sum to the report's own `total_inc_tax`,
   else it aborts without writing/uploading (guards against a truncated extract).
3. **Write JSON** (`json_writer`) → `output/{YYYYMMDD}/AU_Owndays_*.json`.
4. **Upload** to SFTP (`ftp_uploader`).
5. **Archive** the PDF → `docs/check/archive/{today}-{store}-missing/`.

## Recommended flow

```bash
# 1. Review first — parse + JSON only, no SFTP, no archive:
python scripts/process_missing_report.py "docs/check/missing emails/Chatswood ... 06-27.pdf" --no-upload

# 2. Looks right? Run for real (uploads + archives):
python scripts/process_missing_report.py "docs/check/missing emails/Chatswood ... 06-27.pdf"
```

## Options

| Flag | Use |
|------|-----|
| `--store OWND04` | Force the store code if the filename doesn't name the store. |
| `--no-upload` | Parse + write JSON only (also skips archive). Dry run for review. |
| `--no-archive` | Upload but leave the PDF where it is. |
| `--archive-label X` | Folder suffix: `docs/check/archive/{today}-X`. |

## Notes

- Run from the project root (the script resolves `src/` and `.env` relative to it).
- One JSON per store per day. Re-running uploads a **new** file (new timestamp) —
  don't run twice for the same PDF or you'll create a duplicate on SFTP.
- If the late email later arrives and the auto-pipeline processes it, that day may
  be uploaded twice. If that's a concern, mark/skip that email so it isn't
  reprocessed (work queue is the `EOD_Processed` category, not read/unread).
- Filename → store keywords live in `STORE_NAME_MAP` in `.env`.
- Archive convention follows existing folders, e.g. `2026-06-26-chatswood-missing`.
```
