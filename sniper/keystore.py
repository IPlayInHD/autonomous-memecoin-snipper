"""Keystore CLI: `python -m sniper.keystore create|show-pubkey`."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from .execution.wallet import Wallet, create_keystore, load_keystore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sniper.keystore")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_create = sub.add_parser("create", help="encrypt a burner secret key")
    p_create.add_argument("--path", default="keystore.json")
    p_show = sub.add_parser("show-pubkey", help="print the wallet address")
    p_show.add_argument("--path", default="keystore.json")
    args = parser.parse_args(argv)

    if args.cmd == "create":
        if Path(args.path).exists():
            print(f"refusing to overwrite existing {args.path}", file=sys.stderr)
            return 1
        print("Paste the BURNER wallet's base58 secret key (input hidden).")
        print("Never a seed phrase. Never your main wallet.")
        secret = getpass.getpass("secret key: ")
        pw1 = getpass.getpass("new passphrase (min 8 chars): ")
        pw2 = getpass.getpass("repeat passphrase: ")
        if pw1 != pw2:
            print("passphrases do not match", file=sys.stderr)
            return 1
        create_keystore(args.path, secret, pw1)
        wallet = Wallet(load_keystore(args.path, pw1))
        print(f"keystore written to {args.path} (mode 0600)")
        print(f"wallet address: {wallet.pubkey}")
        return 0

    if args.cmd == "show-pubkey":
        pw = getpass.getpass("passphrase: ")
        wallet = Wallet(load_keystore(args.path, pw))
        print(wallet.pubkey)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
