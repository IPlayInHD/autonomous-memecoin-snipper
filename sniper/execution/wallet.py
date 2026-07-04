"""Burner-wallet key handling.

Rules (non-negotiable):
- Preferred source: an encrypted keystore file (scrypt + Fernet) unlocked with
  a passphrase at startup. Even a burner shouldn't sit in plaintext.
- Fallback: SNIPER_PRIVATE_KEY env var (base58 secret key). Logged as a warning.
- NEVER a seed phrase. Never logged, never committed (.gitignore covers both
  keystore.json and .env). __repr__ redacts.
- Paper/observe modes never load a key at all.
"""

from __future__ import annotations

import base64
import getpass
import json
import logging
import os
from pathlib import Path
from typing import Optional

import base58
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

log = logging.getLogger(__name__)

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def create_keystore(path: str, secret_key_b58: str, passphrase: str) -> None:
    if len(passphrase) < 8:
        raise ValueError("passphrase must be at least 8 characters")
    raw = base58.b58decode(secret_key_b58.strip())
    if len(raw) not in (32, 64):
        raise ValueError("expected a base58 32- or 64-byte secret key "
                         "(NOT a seed phrase)")
    salt = os.urandom(16)
    token = Fernet(_derive_key(passphrase, salt)).encrypt(raw)
    payload = {
        "version": 1, "kdf": "scrypt",
        "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P,
        "salt": base64.b64encode(salt).decode(),
        "ciphertext": token.decode(),
    }
    p = Path(path)
    p.write_text(json.dumps(payload, indent=2))
    p.chmod(0o600)


def load_keystore(path: str, passphrase: str) -> bytes:
    payload = json.loads(Path(path).read_text())
    salt = base64.b64decode(payload["salt"])
    try:
        return Fernet(_derive_key(passphrase, salt)).decrypt(
            payload["ciphertext"].encode())
    except InvalidToken as exc:
        raise ValueError("wrong passphrase or corrupted keystore") from exc


class Wallet:
    """Holds the solders Keypair. Import of solders is lazy so paper mode and
    the test suite never need it."""

    def __init__(self, secret_bytes: bytes):
        from solders.keypair import Keypair  # lazy: only real modes reach here
        if len(secret_bytes) == 64:
            self._kp = Keypair.from_bytes(secret_bytes)
        elif len(secret_bytes) == 32:
            self._kp = Keypair.from_seed(secret_bytes)
        else:
            raise ValueError("secret key must be 32 or 64 bytes")

    @property
    def pubkey(self) -> str:
        return str(self._kp.pubkey())

    def sign_versioned_tx_b64(self, tx_b64: str) -> str:
        """Sign a base64 VersionedTransaction (as returned by Jupiter)."""
        from solders.transaction import VersionedTransaction
        raw = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(tx.message, [self._kp])
        return base64.b64encode(bytes(signed)).decode()

    def __repr__(self) -> str:  # never leak key material
        return f"Wallet(pubkey={self.pubkey})"


def load_wallet(env: Optional[dict[str, str]] = None,
                interactive: bool = True) -> Wallet:
    env = env if env is not None else dict(os.environ)
    keystore_path = env.get("SNIPER_KEYSTORE_PATH", "keystore.json")
    if Path(keystore_path).exists():
        passphrase = env.get("SNIPER_KEYSTORE_PASSPHRASE") or (
            getpass.getpass("keystore passphrase: ") if interactive else "")
        if not passphrase:
            raise ValueError("keystore present but no passphrase provided")
        log.info("wallet loaded from encrypted keystore %s", keystore_path)
        return Wallet(load_keystore(keystore_path, passphrase))
    pk = env.get("SNIPER_PRIVATE_KEY")
    if pk:
        log.warning("wallet loaded from PLAINTEXT env var — prefer the encrypted "
                    "keystore: python -m sniper.keystore create")
        return Wallet(base58.b58decode(pk.strip()))
    raise ValueError(
        "no wallet found: create one with `python -m sniper.keystore create` "
        "(paper mode needs no wallet)")
