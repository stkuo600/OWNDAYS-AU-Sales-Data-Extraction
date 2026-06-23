"""
generate_graph_cert.py — create a self-signed certificate + private key for the
Microsoft Graph app-only credential (production alternative to the client secret).

Writes to ./secrets/ (gitignored):
  - eod-mail-reader.cer       PEM public certificate — UPLOAD THIS to the Entra app
                              (App registrations -> Certificates & secrets -> Upload certificate)
  - eod-mail-reader-key.pem   PKCS#8 private key (unencrypted) — used by MSAL; keep host-local

Prints the SHA-1 thumbprint to put in GRAPH_CERT_THUMBPRINT.

Usage:  python scripts/generate_graph_cert.py [--years N] [--cn NAME]
"""

import argparse
import datetime
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_ROOT = Path(__file__).resolve().parent.parent
_SECRETS = _ROOT / "secrets"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=2, help="Validity in years (default 2)")
    ap.add_argument("--cn", default="OWNDAYS-EOD-Mail-Reader", help="Certificate common name")
    args = ap.parse_args()

    _SECRETS.mkdir(exist_ok=True)
    key_path = _SECRETS / "eod-mail-reader-key.pem"
    cer_path = _SECRETS / "eod-mail-reader.cer"

    if key_path.exists() or cer_path.exists():
        raise SystemExit(
            f"Refusing to overwrite existing cert files in {_SECRETS}. "
            "Delete them first if you really want to regenerate."
        )

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, args.cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365 * args.years))
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cer_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    thumbprint = cert.fingerprint(hashes.SHA1()).hex().upper()
    expires = cert.not_valid_after_utc.strftime("%Y-%m-%d")

    print("Certificate generated.")
    print(f"  Public cert (upload to Entra): {cer_path}")
    print(f"  Private key (host-local):      {key_path}")
    print(f"  Expires:                       {expires}")
    print()
    print(f"GRAPH_CERT_THUMBPRINT={thumbprint}")
    print(f"GRAPH_CERT_KEY_FILE=secrets/eod-mail-reader-key.pem")


if __name__ == "__main__":
    main()
