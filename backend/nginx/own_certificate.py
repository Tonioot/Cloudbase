"""A wildcard certificate the user brings along (for the base domain), instead of one certificate per app.

It is checked before it is kept: a readable certificate and key that belong together, not expired, and covering
`*.<base domain>`. The files live in ~/.cloudbase/certs/base-<fingerprint>/. The fingerprint is in the path on
purpose: a replaced certificate has new paths, so the nginx configs change and are rewritten and reloaded.
nginx reads them as root, so the key stays readable by the Cloudbase user only (chmod 600).
"""
import hashlib
import os
import re
import shutil
from datetime import datetime, timezone

CERTS_DIR = os.path.expanduser("~/.cloudbase/certs")
_MAX_PEM = 64 * 1024
_DIR_RE = re.compile(r"^(base|db)-[0-9a-f]{12}$")


class CertificateProblem(ValueError):
    """The upload can't be used; the message says why."""


def _load(cert_pem: str, key_pem: str):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    for label, text in (("certificate", cert_pem), ("private key", key_pem)):
        if not (text or "").strip():
            raise CertificateProblem(f"Add the {label}")
        if len(text) > _MAX_PEM:
            raise CertificateProblem(f"The {label} is too large to be right")
    try:
        certs = x509.load_pem_x509_certificates(cert_pem.encode())
    except Exception:
        raise CertificateProblem("The certificate isn’t a PEM file (it should start with -----BEGIN CERTIFICATE-----)")
    if not certs:
        raise CertificateProblem("No certificate found in that file")
    try:
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    except TypeError:
        raise CertificateProblem("The private key has a password. nginx can’t ask for it: remove it first (openssl rsa -in key.pem -out key-nopass.pem)")
    except Exception:
        raise CertificateProblem("The private key isn’t a PEM file (it should start with -----BEGIN PRIVATE KEY----- or similar)")
    return certs, key


def _public_bytes(public_key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _names(cert) -> list[str]:
    from cryptography import x509
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return [n.lower() for n in san.get_values_for_type(x509.DNSName)]
    except x509.ExtensionNotFound:
        return []


def _expires(cert) -> datetime:
    try:
        return cert.not_valid_after_utc
    except AttributeError:                      # cryptography < 42
        return cert.not_valid_after.replace(tzinfo=timezone.utc)


def check(cert_pem: str, key_pem: str, base_domain: str, what: str = "an app is <name>.{domain}") -> dict:
    """The details of the certificate, or a CertificateProblem."""
    from cryptography.hazmat.primitives import serialization
    certs, key = _load(cert_pem, key_pem)
    leaf = certs[0]
    if _public_bytes(leaf.public_key()) != _public_bytes(key.public_key()):
        raise CertificateProblem("The private key doesn’t belong to this certificate"
                                 + (" (the first certificate in the file should be the one for your domain, then the intermediates)" if len(certs) > 1 else ""))
    expires = _expires(leaf)
    if expires <= datetime.now(timezone.utc):
        raise CertificateProblem(f"This certificate expired on {expires:%Y-%m-%d}")
    names = _names(leaf)
    wanted = f"*.{base_domain.strip().lower()}"
    if wanted not in names:
        shown = ", ".join(names[:6]) or "no names"
        raise CertificateProblem(f"The certificate has to cover {wanted} ({what.format(domain=base_domain)}). It covers: {shown}")
    return {
        "names": names,
        "expires_at": expires.isoformat(),
        "days_left": (expires - datetime.now(timezone.utc)).days,
        "fingerprint": hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest()[:12],
    }


def save(cert_pem: str, key_pem: str, base_domain: str, kind: str = "base") -> tuple[str, str, dict]:
    """Check and keep the certificate ("base": for the app subdomains, "db": for database hostnames, each with its
    own folders). Returns (cert path, key path, details)."""
    info = check(cert_pem, key_pem, base_domain,
                 "a database is <name>.{domain}" if kind == "db" else "an app is <name>.{domain}")
    folder = os.path.join(CERTS_DIR, f"{kind}-{info['fingerprint']}")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    cert_path, key_path = os.path.join(folder, "fullchain.pem"), os.path.join(folder, "privkey.pem")
    for path, text, mode in ((cert_path, cert_pem, 0o644), (key_path, key_pem, 0o600)):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(text.strip() + "\n")
        os.chmod(path, mode)
    return cert_path, key_path, info


def inspect_saved(cert_path: str) -> dict | None:
    """Expiry and names of a certificate kept by save() (None for any other path or an unreadable file)."""
    folder = os.path.dirname(cert_path or "")
    if os.path.dirname(folder) != CERTS_DIR or not _DIR_RE.match(os.path.basename(folder)):
        return None
    try:
        from cryptography import x509
        with open(cert_path, "rb") as f:
            leaf = x509.load_pem_x509_certificates(f.read())[0]
        expires = _expires(leaf)
        return {"names": _names(leaf), "expires_at": expires.isoformat(),
                "days_left": (expires - datetime.now(timezone.utc)).days}
    except Exception:
        return None


def forget_except(keep_cert_path: str | None, kind: str = "base") -> None:
    """Remove the stored certificates of this kind other than the one in use (after a replacement or when switching
    to automatic)."""
    keep = os.path.dirname(keep_cert_path) if keep_cert_path else None
    try:
        entries = os.listdir(CERTS_DIR)
    except OSError:
        return
    for entry in entries:
        path = os.path.join(CERTS_DIR, entry)
        if _DIR_RE.match(entry) and entry.startswith(f"{kind}-") and path != keep:
            shutil.rmtree(path, ignore_errors=True)
