"""Encrypting big files frame by frame, and the archive that holds the manifest and the dumps."""
import io
import os
import tarfile

import pytest

from transfer import archive, crypto

PASS = "correct horse battery"


def test_file_round_trip_many_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "FRAME_SIZE", 1000)
    src, enc, out = tmp_path / "plain", tmp_path / "enc", tmp_path / "out"
    data = os.urandom(10_500)
    src.write_bytes(data)
    crypto.encrypt_file(str(src), str(enc), PASS)
    crypto.decrypt_file(str(enc), str(out), PASS)
    assert out.read_bytes() == data
    assert data[:50] not in enc.read_bytes()


def test_file_and_bytes_use_the_same_format(tmp_path):
    src, enc = tmp_path / "plain", tmp_path / "enc"
    src.write_bytes(b"same format")
    crypto.encrypt_file(str(src), str(enc), PASS)
    assert crypto.decrypt_bytes(enc.read_bytes(), PASS) == b"same format"
    enc.write_bytes(crypto.encrypt_bytes(b"and the other way", PASS))
    crypto.decrypt_file(str(enc), str(tmp_path / "out"), PASS)
    assert (tmp_path / "out").read_bytes() == b"and the other way"


def test_empty_file(tmp_path):
    src, enc, out = tmp_path / "plain", tmp_path / "enc", tmp_path / "out"
    src.write_bytes(b"")
    crypto.encrypt_file(str(src), str(enc), PASS)
    crypto.decrypt_file(str(enc), str(out), PASS)
    assert out.read_bytes() == b""


@pytest.mark.parametrize("damage", ["flip", "truncate", "wrong_passphrase", "not_an_export", "huge_frame"])
def test_file_decryption_fails_cleanly_and_leaves_nothing(tmp_path, monkeypatch, damage):
    monkeypatch.setattr(crypto, "FRAME_SIZE", 100)
    src, enc, out = tmp_path / "plain", tmp_path / "enc", tmp_path / "out"
    src.write_bytes(b"A" * 450)
    crypto.encrypt_file(str(src), str(enc), PASS)
    blob = bytearray(enc.read_bytes())
    passphrase = PASS
    if damage == "flip":
        blob[-3] ^= 1
    elif damage == "truncate":
        blob = blob[:len(blob) - (4 + 100 + 16)]
    elif damage == "wrong_passphrase":
        passphrase = PASS + "!"
    elif damage == "not_an_export":
        blob = bytearray(b"hello, definitely not an export file at all, no")
    elif damage == "huge_frame":
        blob[crypto.HEADER_LEN:crypto.HEADER_LEN + 4] = (2 ** 31).to_bytes(4, "big")   # claims a 2 GB frame
    enc.write_bytes(bytes(blob))
    with pytest.raises(crypto.TransferCryptoError):
        crypto.decrypt_file(str(enc), str(out), passphrase)
    assert not out.exists()


def test_archive_round_trip(tmp_path):
    dump = tmp_path / "db.dump"
    dump.write_bytes(b"PGDMP" + os.urandom(5000))
    path = str(tmp_path / "bundle.tar")
    archive.write(path, {"format": "x", "apps": []}, {"data/0.dump": str(dump)})
    with archive.open_archive(path) as tar:
        assert archive.read_manifest(tar) == {"format": "x", "apps": []}
        assert archive.open_data(tar, "data/0.dump").read() == dump.read_bytes()


def test_manifest_of_bytes(tmp_path):
    path = str(tmp_path / "b.tar")
    archive.write(path, {"hello": "world"})
    assert archive.manifest_of_bytes(open(path, "rb").read()) == {"hello": "world"}


def test_archive_never_reads_outside_its_fixed_names(tmp_path):
    path = str(tmp_path / "evil.tar")
    with tarfile.open(path, "w") as tar:
        for name in ("manifest.json", "../../etc/passwd", "/abs/path"):
            data = b"{}"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("data/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    with archive.open_archive(path) as tar:
        for bad in ("../../etc/passwd", "/abs/path", "data/../../x", "manifest.json"):
            with pytest.raises(archive.ArchiveError):
                archive.open_data(tar, bad)
        with pytest.raises(archive.ArchiveError):
            archive.open_data(tar, "data/link")          # a symlink is not a file
        with pytest.raises(archive.ArchiveError):
            archive.open_data(tar, "data/missing")


def test_a_damaged_archive_is_reported_not_crashed_on(tmp_path):
    path = tmp_path / "junk.tar"
    path.write_bytes(b"this is not a tar file" * 100)
    with pytest.raises(archive.ArchiveError):
        with archive.open_archive(str(path)) as tar:
            archive.read_manifest(tar)


def test_an_archive_without_a_manifest(tmp_path):
    path = str(tmp_path / "nomanifest.tar")
    with tarfile.open(path, "w") as tar:
        info = tarfile.TarInfo("data/0.dump")
        info.size = 0
        tar.addfile(info, io.BytesIO(b""))
    with archive.open_archive(path) as tar:
        with pytest.raises(archive.ArchiveError, match="no manifest"):
            archive.read_manifest(tar)
