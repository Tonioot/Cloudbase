import struct
import time

import pytest

from transfer import crypto

PASS = "correct horse battery"


def test_round_trip():
    data = b'{"apps": []} with unicode \xc3\xa9\xe2\x82\xac'
    assert crypto.decrypt_bytes(crypto.encrypt_bytes(data, PASS), PASS) == data


def test_empty_input_round_trips():
    assert crypto.decrypt_bytes(crypto.encrypt_bytes(b"", PASS), PASS) == b""


def test_the_plaintext_is_not_in_the_file():
    secret = b"SUPER-SECRET-VALUE-12345"
    assert secret not in crypto.encrypt_bytes(b"x" * 50 + secret, PASS)


def test_two_encryptions_of_the_same_data_differ():
    assert crypto.encrypt_bytes(b"same", PASS) != crypto.encrypt_bytes(b"same", PASS)


def test_many_frames(monkeypatch):
    monkeypatch.setattr(crypto, "FRAME_SIZE", 16)
    data = bytes(range(256)) * 3
    blob = crypto.encrypt_bytes(data, PASS)
    assert crypto.decrypt_bytes(blob, PASS) == data


def test_wrong_passphrase_is_rejected():
    blob = crypto.encrypt_bytes(b"data", PASS)
    with pytest.raises(crypto.TransferCryptoError, match="Wrong passphrase"):
        crypto.decrypt_bytes(blob, PASS + "x")


def test_a_flipped_bit_is_rejected():
    blob = bytearray(crypto.encrypt_bytes(b"data" * 10, PASS))
    blob[-5] ^= 1
    with pytest.raises(crypto.TransferCryptoError):
        crypto.decrypt_bytes(bytes(blob), PASS)


def test_a_modified_header_is_rejected():
    blob = bytearray(crypto.encrypt_bytes(b"data", PASS))
    blob[6] ^= 1                      # inside the salt: different key, and the header is authenticated
    with pytest.raises(crypto.TransferCryptoError):
        crypto.decrypt_bytes(bytes(blob), PASS)


def test_truncation_is_detected(monkeypatch):
    monkeypatch.setattr(crypto, "FRAME_SIZE", 16)
    blob = crypto.encrypt_bytes(b"A" * 64, PASS)            # 4 data frames
    # cut off the final frame: the earlier frames are valid but none of them is marked "last"
    last_start = len(blob) - (4 + 16 + 16)
    with pytest.raises(crypto.TransferCryptoError):
        crypto.decrypt_bytes(blob[:last_start], PASS)


def test_frames_cannot_be_reordered(monkeypatch):
    monkeypatch.setattr(crypto, "FRAME_SIZE", 16)
    blob = crypto.encrypt_bytes(b"A" * 16 + b"B" * 16 + b"C" * 16, PASS)
    header, rest = blob[:crypto.HEADER_LEN], blob[crypto.HEADER_LEN:]
    size = 4 + 16 + 16
    frames = [rest[i:i + size] for i in range(0, len(rest), size)]
    swapped = header + frames[1] + frames[0] + frames[2]
    with pytest.raises(crypto.TransferCryptoError):
        crypto.decrypt_bytes(swapped, PASS)


@pytest.mark.parametrize("blob", [b"", b"nope", b"CBX1", b"hello world, this is not an export" * 3])
def test_other_files_are_rejected(blob):
    with pytest.raises(crypto.TransferCryptoError):
        crypto.decrypt_bytes(blob, PASS)


def test_a_crafted_header_cannot_demand_huge_memory():
    """A header asking for scrypt n=2**30 must be refused before any key is derived."""
    good = crypto.encrypt_bytes(b"x", PASS)
    evil = bytearray(good)
    evil[4 + crypto.SALT_LEN] = 30
    started = time.time()
    with pytest.raises(crypto.TransferCryptoError):
        crypto.decrypt_bytes(bytes(evil), PASS)
    assert time.time() - started < 1


def test_short_passphrases_are_refused():
    with pytest.raises(crypto.TransferCryptoError, match="at least"):
        crypto.encrypt_bytes(b"x", "short")
    with pytest.raises(crypto.TransferCryptoError):
        crypto.check_passphrase("")


def test_frame_layout_matches_the_documentation():
    blob = crypto.encrypt_bytes(b"abc", PASS)
    assert blob[:4] == b"CBX1"
    (size,) = struct.unpack(">I", blob[crypto.HEADER_LEN:crypto.HEADER_LEN + 4])
    assert size == 3 + 16                                   # data + GCM tag
    assert len(blob) == crypto.HEADER_LEN + 4 + size
