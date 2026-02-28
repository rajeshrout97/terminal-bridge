"""TLS certificate generation for Terminal Bridge.

Generates self-signed certificates for LAN WebSocket connections (wss://).
"""

from __future__ import annotations

import datetime
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from terminal_bridge.config import get_certs_dir


def generate_self_signed_cert(
    hostname: str = "localhost",
    cert_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Generate a self-signed TLS certificate and private key.

    Returns (cert_path, key_path).
    """
    if cert_dir is None:
        cert_dir = get_certs_dir()

    cert_path = cert_dir / "server.crt"
    key_path = cert_dir / "server.key"

    # Generate RSA key
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # Build certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Terminal Bridge"),
    ])

    now = datetime.datetime.now(datetime.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.DNSName(hostname),
                x509.DNSName("*.local"),
                x509.IPAddress(
                    __import__("ipaddress").IPv4Address("127.0.0.1")
                ),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    # Write key
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)

    # Write cert
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    return cert_path, key_path


def get_ssl_context_server(
    cert_path: Path | None = None,
    key_path: Path | None = None,
) -> ssl.SSLContext:
    """Create an SSL context for the WebSocket server."""
    if cert_path is None or key_path is None:
        certs_dir = get_certs_dir()
        cert_path = cert_path or certs_dir / "server.crt"
        key_path = key_path or certs_dir / "server.key"

    if not cert_path.exists() or not key_path.exists():
        cert_path, key_path = generate_self_signed_cert()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def get_ssl_context_client() -> ssl.SSLContext:
    """Create an SSL context for the WebSocket client (allows self-signed certs)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # Accept self-signed certs on LAN
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx

