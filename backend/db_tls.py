"""TLS for public database access (primary only).

Cloudbase keeps its own small CA in ~/.cloudbase/db-tls and issues one server
certificate for the primary's public address. Clients can encrypt without any
setup (sslmode=require and friends), or download the CA to also verify the server.
"""

import datetime
import ipaddress
import logging
import os
import ssl
import threading
from typing import Optional

from database import DATA_DIR

log = logging.getLogger("cloudbase.db-tls")

TLS_DIR = os.path.join(DATA_DIR, "db-tls")
CA_CERT = os.path.join(TLS_DIR, "ca.crt")
CA_KEY = os.path.join(TLS_DIR, "ca.key")
SERVER_CERT = os.path.join(TLS_DIR, "server.crt")
SERVER_KEY = os.path.join(TLS_DIR, "server.key")

_lock = threading.Lock()
_contexts: dict[tuple, ssl.SSLContext] = {}


def _write_private(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _name(common_name: str):
    from cryptography.x509.oid import NameOID
    from cryptography import x509
    return x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Cloudbase"),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])


def _ensure_ca():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if os.path.exists(CA_CERT) and os.path.exists(CA_KEY):
        with open(CA_KEY, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        with open(CA_CERT, "rb") as f:
            return x509.load_pem_x509_certificate(f.read()), key

    os.makedirs(TLS_DIR, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("Cloudbase Database CA"))
        .issuer_name(_name("Cloudbase Database CA"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                     content_commitment=False, key_encipherment=False, data_encipherment=False,
                                     key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
        .sign(key, hashes.SHA256())
    )
    _write_private(CA_KEY, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    with open(CA_CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    log.info("[db-tls] created database CA")
    return cert, key


def _server_cert_matches(hosts: tuple[str, ...]) -> bool:
    from cryptography import x509
    if not (os.path.exists(SERVER_CERT) and os.path.exists(SERVER_KEY)):
        return False
    with open(SERVER_CERT, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    if cert.not_valid_after_utc < datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30):
        return False
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
    except x509.ExtensionNotFound:
        return False
    present = {str(v) for v in san.get_values_for_type(x509.IPAddress)} | set(san.get_values_for_type(x509.DNSName))
    return set(hosts) <= present


def _issue_server_cert(hosts: tuple[str, ...]) -> None:
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    ca_cert, ca_key = _ensure_ca()
    key = ec.generate_private_key(ec.SECP256R1())
    names = []
    for h in hosts:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            names.append(x509.DNSName(h))
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(hosts[0] if hosts else "cloudbase"))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, key_agreement=True, key_cert_sign=False, crl_sign=False,
                                     content_commitment=False, key_encipherment=False, data_encipherment=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        # Strict verifiers (Python 3.13+, recent clients) require the key identifiers.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_private(SERVER_KEY, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption()))
    with open(SERVER_CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
        f.write(ca_cert.public_bytes(serialization.Encoding.PEM))  # full chain
    log.info("[db-tls] issued server certificate for %s", ", ".join(hosts))


def server_context(hosts: tuple[str, ...], alpn: Optional[str] = None) -> ssl.SSLContext:
    """TLS context for the public listeners, valid for `hosts` (IPs or names)."""
    hosts = tuple(sorted({h for h in hosts if h})) or ("localhost",)
    with _lock:
        key = (hosts, alpn)
        if key in _contexts:
            return _contexts[key]
        if not _server_cert_matches(hosts):
            _issue_server_cert(hosts)
            _contexts.clear()
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(SERVER_CERT, SERVER_KEY)
        if alpn:
            ctx.set_alpn_protocols([alpn])
        _contexts[key] = ctx
        return ctx


def ca_pem() -> bytes:
    with _lock:
        _ensure_ca()
    with open(CA_CERT, "rb") as f:
        return f.read()
