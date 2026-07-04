"""Encrypted keystore: roundtrip, wrong passphrase, hygiene."""

import json
import os

import base58
import pytest

from sniper.execution.wallet import create_keystore, load_keystore


SECRET64 = bytes(range(64))
SECRET_B58 = base58.b58encode(SECRET64).decode()


def test_roundtrip(tmp_path):
    path = str(tmp_path / "keystore.json")
    create_keystore(path, SECRET_B58, "correct horse battery")
    assert load_keystore(path, "correct horse battery") == SECRET64


def test_wrong_passphrase_rejected(tmp_path):
    path = str(tmp_path / "keystore.json")
    create_keystore(path, SECRET_B58, "correct horse battery")
    with pytest.raises(ValueError, match="passphrase"):
        load_keystore(path, "wrong")


def test_key_not_stored_in_plaintext(tmp_path):
    path = str(tmp_path / "keystore.json")
    create_keystore(path, SECRET_B58, "correct horse battery")
    raw = (tmp_path / "keystore.json").read_bytes()
    assert SECRET_B58.encode() not in raw
    assert SECRET64 not in raw
    payload = json.loads(raw)
    assert payload["kdf"] == "scrypt"


def test_file_permissions_restricted(tmp_path):
    path = tmp_path / "keystore.json"
    create_keystore(str(path), SECRET_B58, "correct horse battery")
    assert (path.stat().st_mode & 0o777) == 0o600


def test_short_passphrase_rejected(tmp_path):
    with pytest.raises(ValueError, match="8 characters"):
        create_keystore(str(tmp_path / "k.json"), SECRET_B58, "short")


def test_seed_phrase_like_input_rejected(tmp_path):
    """Anything that isn't a 32/64-byte base58 key is refused — a seed phrase
    will fail decode or length and never be written to disk."""
    with pytest.raises(Exception):
        create_keystore(str(tmp_path / "k.json"),
                        "correct horse battery staple mnemonic words here",
                        "longpassphrase")


def test_wallet_repr_redacts(tmp_path):
    solders = pytest.importorskip("solders")
    from sniper.execution.wallet import Wallet
    wallet = Wallet(os.urandom(32))
    assert "pubkey=" in repr(wallet)
    # no 32/64-byte secret material should appear in the repr
    assert len(repr(wallet)) < 120
