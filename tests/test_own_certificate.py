"""A wildcard certificate of your own for the base domain: checked before it is kept, stored under a path that changes with the certificate."""
import datetime
import os

import pytest

pytest.importorskip("cryptography")
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from nginx import own_certificate as oc


def make(names, days=90):
    key = rsa.generate_private_key(65537, 2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=100)).not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())


def test_a_matching_wildcard_is_accepted():
    cert, key = make(["*.apps.example.com"])
    assert oc.check(cert, key, "apps.example.com")["names"] == ["*.apps.example.com"]


@pytest.mark.parametrize("problem", ["other base", "expired", "other key", "not pem"])
def test_unusable_uploads_are_refused_with_a_reason(problem):
    cert, key = make(["*.apps.example.com"])
    args = {
        "other base": (cert, key, "example.com"),                      # *.example.com is needed, not *.apps.example.com
        "expired": (*make(["*.apps.example.com"], days=-1), "apps.example.com"),
        "other key": (cert, make(["x.example.com"])[1], "apps.example.com"),
        "not pem": ("nope", "nope", "apps.example.com"),
    }[problem]
    with pytest.raises(oc.CertificateProblem):
        oc.check(*args)


def test_saved_certificate_has_its_own_path_and_a_private_key(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "CERTS_DIR", str(tmp_path))
    first = oc.save(*make(["*.apps.example.com"]), "apps.example.com")
    second = oc.save(*make(["*.apps.example.com"]), "apps.example.com")
    assert first[0] != second[0]                                       # a replacement changes the nginx config, so nginx reloads
    if os.name != "nt":
        assert oct(os.stat(second[1]).st_mode & 0o777) == "0o600"
    assert oc.inspect_saved(second[0])["names"] == ["*.apps.example.com"]
    oc.forget_except(second[0])
    assert os.path.isdir(os.path.dirname(second[0])) and not os.path.isdir(os.path.dirname(first[0]))
    oc.forget_except(None)
    assert os.listdir(tmp_path) == []
