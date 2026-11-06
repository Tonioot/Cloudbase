"""The archive that holds the manifest and the dumps."""
import io
import os
import tarfile

import pytest

from transfer import archive


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
