"""
ftp_uploader.py — Uploads JSON files to an SFTP server via paramiko.

Despite the module/config name, this uses SFTP (SSH File Transfer Protocol),
not FTP or FTPS. The FTP_* config variable names are retained for historical
reasons.
"""

import logging
from pathlib import Path

import paramiko

import config

logger = logging.getLogger(__name__)


def upload_file(local_path):
    """Upload a local file to the configured SFTP server.

    Uses AutoAddPolicy: unknown host keys are trusted on first connect and
    cached to ~/.ssh/known_hosts for subsequent runs.

    Args:
        local_path (str): Path to the local file to upload.

    Returns:
        bool: True on success, False on failure.
    """
    if not config.FTP_HOST:
        logger.warning("FTP_HOST not configured — skipping upload")
        return False

    local = Path(local_path)
    remote_filename = local.name
    remote_dir = config.FTP_DIRECTORY or "/"
    remote_path = (
        f"{remote_dir.rstrip('/')}/{remote_filename}"
        if remote_dir != "/"
        else f"/{remote_filename}"
    )

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    sftp = None
    try:
        logger.info("Connecting to SFTP %s:%d", config.FTP_HOST, config.FTP_PORT)
        client.connect(
            hostname=config.FTP_HOST,
            port=config.FTP_PORT,
            username=config.FTP_USERNAME or "",
            password=config.FTP_PASSWORD or "",
            allow_agent=False,
            look_for_keys=False,
        )
        sftp = client.open_sftp()

        sftp.put(str(local), remote_path)
        logger.info("Uploaded %s to SFTP %s", remote_filename, remote_path)
        return True

    except Exception:
        logger.exception("Failed to upload %s to SFTP", local_path)
        return False

    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        try:
            client.close()
        except Exception:
            pass
