"""The content of an export file.

A tar archive with `manifest.json` and one `data/<n>.dump` member per database dump. It is never
extracted to disk by name: members are read by their exact, fixed name, so a hand-made archive
cannot write anywhere or overwrite anything.
"""
import io
import json
import tarfile
from typing import IO, Optional

MANIFEST = "manifest.json"
DATA_PREFIX = "data/"
MAX_MANIFEST_BYTES = 32 * 1024 * 1024


class ArchiveError(ValueError):
    pass


def write(path: str, manifest: dict, files: Optional[dict[str, str]] = None) -> None:
    """files: member name (data/…) -> path of the file to put there."""
    body = json.dumps(manifest).encode("utf-8")
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo(MANIFEST)
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
        for name, src in (files or {}).items():
            if not name.startswith(DATA_PREFIX) or ".." in name:
                raise ArchiveError(f"bad member name {name!r}")
            tar.add(src, arcname=name, recursive=False)


def open_archive(path: str) -> tarfile.TarFile:
    try:
        return tarfile.open(path, "r:")
    except (tarfile.TarError, OSError) as exc:
        raise ArchiveError("The export file is damaged") from exc


def read_manifest(tar: tarfile.TarFile) -> dict:
    try:
        member = tar.getmember(MANIFEST)
        if not member.isfile() or member.size > MAX_MANIFEST_BYTES:
            raise ArchiveError("The export file has no usable manifest")
        return json.loads(tar.extractfile(member).read().decode("utf-8"))
    except KeyError:
        raise ArchiveError("The export file has no manifest") from None
    except (ValueError, tarfile.TarError) as exc:
        raise ArchiveError("The export file is damaged") from exc


def open_data(tar: tarfile.TarFile, name: str) -> IO[bytes]:
    if not name.startswith(DATA_PREFIX) or ".." in name:
        raise ArchiveError(f"bad member name {name!r}")
    try:
        member = tar.getmember(name)
    except KeyError:
        raise ArchiveError(f"The export file is missing {name}") from None
    if not member.isfile():
        raise ArchiveError(f"{name} is not a file")
    return tar.extractfile(member)


def manifest_of_bytes(plain: bytes) -> dict:
    """For tests and small tools: the manifest of an in-memory archive."""
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:") as tar:
        return read_manifest(tar)
