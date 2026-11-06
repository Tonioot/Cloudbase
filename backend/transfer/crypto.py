"""Passphrase encryption for export files.

scrypt turns the passphrase into a 256-bit key; the data is sealed with AES-256-GCM in frames
so a large file (later: database dumps) never has to sit in memory as one block.

    header  = b"CBX1" | salt(16) | n_log2(1) | r(1) | p(1) | base_nonce(8)
    frame   = length(4, big endian) | AES-GCM ciphertext+tag
    nonce_i = base_nonce | i (4 bytes, big endian)
    aad_i   = header | 0x01 if this is the last frame else 0x00

The header is authenticated in every frame, frames cannot be reordered (counter nonce), and the
"last frame" flag makes a truncated file fail instead of silently decrypting to less data.
A wrong passphrase and a damaged file give the same error on purpose.
"""
import os
import struct
from typing import Iterable, Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"CBX1"
SALT_LEN = 16
BASE_NONCE_LEN = 8
HEADER_LEN = len(MAGIC) + SALT_LEN + 3 + BASE_NONCE_LEN
FRAME_SIZE = 1024 * 1024
MAX_SEALED_FRAME = 1024 * 1024 + 16        # a frame never exceeds FRAME_SIZE plus the GCM tag

# scrypt cost: ~32 MB of memory and ~0.1 s. A file may carry its own parameters (so the cost
# can be raised later), but never beyond these bounds: a crafted header must not be able to
# make the server burn gigabytes of memory.
N_LOG2, R, P = 15, 8, 1
MAX_N_LOG2, MAX_R, MAX_P = 20, 16, 4
MIN_PASSPHRASE_LENGTH = 12


class TransferCryptoError(ValueError):
    pass


BAD_FILE = "Wrong passphrase, or the file is damaged"


def check_passphrase(passphrase: str) -> None:
    if len(passphrase or "") < MIN_PASSPHRASE_LENGTH:
        raise TransferCryptoError(f"Use a passphrase of at least {MIN_PASSPHRASE_LENGTH} characters")


def _derive(passphrase: str, salt: bytes, n_log2: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=2 ** n_log2, r=r, p=p).derive(passphrase.encode("utf-8"))


def _nonce(base: bytes, index: int) -> bytes:
    return base + struct.pack(">I", index)


def encrypt_chunks(chunks: Iterable[bytes], passphrase: str) -> Iterator[bytes]:
    """Yield the encrypted file piece by piece: the header, then one frame per input chunk."""
    check_passphrase(passphrase)
    salt, base = os.urandom(SALT_LEN), os.urandom(BASE_NONCE_LEN)
    header = MAGIC + salt + bytes([N_LOG2, R, P]) + base
    aead = AESGCM(_derive(passphrase, salt, N_LOG2, R, P))
    yield header

    index, pending = 0, None
    for chunk in chunks:
        if pending is not None:
            yield _frame(aead, base, header, index, pending, last=False)
            index += 1
        pending = chunk
    # the last frame is flagged; an empty input still produces one (empty) frame
    yield _frame(aead, base, header, index, pending if pending is not None else b"", last=True)


def _frame(aead: AESGCM, base: bytes, header: bytes, index: int, data: bytes, last: bool) -> bytes:
    sealed = aead.encrypt(_nonce(base, index), data, header + (b"\x01" if last else b"\x00"))
    return struct.pack(">I", len(sealed)) + sealed


def _pieces(data: bytes) -> Iterator[bytes]:
    for i in range(0, len(data), FRAME_SIZE):
        yield data[i:i + FRAME_SIZE]


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    return b"".join(encrypt_chunks(_pieces(data), passphrase))


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    if len(blob) < HEADER_LEN or blob[:4] != MAGIC:
        raise TransferCryptoError("This is not a Cloudbase export file")
    header = blob[:HEADER_LEN]
    salt = header[4:4 + SALT_LEN]
    n_log2, r, p = header[4 + SALT_LEN], header[5 + SALT_LEN], header[6 + SALT_LEN]
    base = header[7 + SALT_LEN:]
    if not (10 <= n_log2 <= MAX_N_LOG2 and 1 <= r <= MAX_R and 1 <= p <= MAX_P):
        raise TransferCryptoError("This is not a Cloudbase export file")
    aead = AESGCM(_derive(passphrase or "", salt, n_log2, r, p))

    out, pos, index = [], HEADER_LEN, 0
    try:
        while True:
            if pos + 4 > len(blob):
                raise TransferCryptoError(BAD_FILE)      # ran out of data before a last frame
            (size,) = struct.unpack(">I", blob[pos:pos + 4])
            end = pos + 4 + size
            if end > len(blob) or size < 16 or size > MAX_SEALED_FRAME:
                raise TransferCryptoError(BAD_FILE)
            last = end == len(blob)
            out.append(aead.decrypt(_nonce(base, index), blob[pos + 4:end], header + (b"\x01" if last else b"\x00")))
            if last:
                return b"".join(out)
            pos, index = end, index + 1
    except InvalidTag:
        raise TransferCryptoError(BAD_FILE) from None


# ── files: the same format, streamed from and to disk (database dumps are large) ──
def encrypt_file(src_path: str, dst_path: str, passphrase: str) -> None:
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        for piece in encrypt_chunks(iter(lambda: src.read(FRAME_SIZE), b""), passphrase):
            dst.write(piece)


def decrypt_file(src_path: str, dst_path: str, passphrase: str) -> None:
    """Decrypt frame by frame so memory stays at about one frame whatever the file size.
    On any error the partial output is removed."""
    total = os.path.getsize(src_path)
    try:
        with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
            header = src.read(HEADER_LEN)
            if len(header) < HEADER_LEN or header[:4] != MAGIC:
                raise TransferCryptoError("This is not a Cloudbase export file")
            salt = header[4:4 + SALT_LEN]
            n_log2, r, p = header[4 + SALT_LEN], header[5 + SALT_LEN], header[6 + SALT_LEN]
            base = header[7 + SALT_LEN:]
            if not (10 <= n_log2 <= MAX_N_LOG2 and 1 <= r <= MAX_R and 1 <= p <= MAX_P):
                raise TransferCryptoError("This is not a Cloudbase export file")
            aead = AESGCM(_derive(passphrase or "", salt, n_log2, r, p))

            pos, index = HEADER_LEN, 0
            while True:
                raw = src.read(4)
                if len(raw) < 4:
                    raise TransferCryptoError(BAD_FILE)          # ran out of data before a last frame
                (size,) = struct.unpack(">I", raw)
                end = pos + 4 + size
                if end > total or size < 16 or size > MAX_SEALED_FRAME:
                    raise TransferCryptoError(BAD_FILE)
                last = end == total
                try:
                    dst.write(aead.decrypt(_nonce(base, index), src.read(size), header + (b"\x01" if last else b"\x00")))
                except InvalidTag:
                    raise TransferCryptoError(BAD_FILE) from None
                if last:
                    return
                pos, index = end, index + 1
    except BaseException:
        if os.path.exists(dst_path):
            os.remove(dst_path)
        raise

