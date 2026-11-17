"""A self-signed certificate so mail and the device cookie are encrypted on the way to the phone.

The phone shows a one-time "connection is not private" page because no authority signed it; that is
expected. It stops people on the same Wi-Fi from reading the traffic. It does not prove to the phone
that the PC is really yours, so pair only on a network you trust (or use Tailscale).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import ssl
from pathlib import Path


def ensure_certificate(folder: Path, hosts: list[str]) -> tuple[Path, Path] | None:
    """Create (or reuse) cert.pem + key.pem covering `hosts`. None when `cryptography` is missing."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    cert_path, key_path, meta_path = folder / "cert.pem", folder / "key.pem", folder / "cert.json"
    wanted = sorted(set(hosts) | {"localhost", "127.0.0.1"})
    try:
        if cert_path.exists() and key_path.exists() and json.loads(meta_path.read_text())["hosts"] == wanted:
            return cert_path, key_path
    except (OSError, ValueError, KeyError):
        pass
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Titip on this PC")])
    names = []
    for host in wanted:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=397))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    meta_path.write_text(json.dumps({"hosts": wanted}), encoding="utf-8")
    return cert_path, key_path


def server_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert_path), str(key_path))
    return context
