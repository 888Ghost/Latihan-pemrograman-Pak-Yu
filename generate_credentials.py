#!/usr/bin/env python3
"""
generate_credentials.py
─────────────────────────────────────────────────────────────────────────
Jalankan SEKALI di laptop/PC lokal untuk generate CLOB L2 credentials.
Setelah selesai, simpan output ke GitHub Secrets.

Requirements (hanya untuk script ini, bukan untuk bot):
  pip install py-clob-client

Usage:
  export POLY_PRIVATE_KEY=0x...your_metamask_private_key...
  export POLY_FUNDER=0x...your_wallet_address...
  python generate_credentials.py

Output akan berisi 3 nilai yang perlu disimpan ke GitHub Secrets:
  CLOB_API_KEY
  CLOB_API_SECRET
  CLOB_API_PASSPHRASE
─────────────────────────────────────────────────────────────────────────
"""

import os
import sys


def main():
    private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    funder      = os.environ.get("POLY_FUNDER", "")

    if not private_key or not funder:
        print("ERROR: Set env vars terlebih dahulu:")
        print("  export POLY_PRIVATE_KEY=0x...")
        print("  export POLY_FUNDER=0x...")
        sys.exit(1)

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    print("Connecting to Polymarket CLOB...")

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
    except ImportError:
        print("\nERROR: py-clob-client not installed.")
        print("  pip install py-clob-client")
        sys.exit(1)

    try:
        client = ClobClient(
            host           = "https://clob.polymarket.com",
            key            = private_key,
            chain_id       = POLYGON,
            signature_type = 2,
            funder         = funder,
        )
        creds = client.create_or_derive_api_creds()

        print("\n" + "="*60)
        print("  SUCCESS — Save these to GitHub Secrets:")
        print("="*60)
        print(f"CLOB_API_KEY        = {creds.api_key}")
        print(f"CLOB_API_SECRET     = {creds.api_secret}")
        print(f"CLOB_API_PASSPHRASE = {creds.api_passphrase}")
        print("="*60)
        print(f"\nAlso needed in GitHub Secrets:")
        print(f"POLY_PRIVATE_KEY    = {private_key[:10]}...{private_key[-4:]}")
        print(f"POLY_FUNDER         = {funder}")
        print(f"TELEGRAM_TOKEN      = (dari @BotFather)")
        print(f"TELEGRAM_CHAT_ID    = (dari @userinfobot)")
        print(f"BANKROLL            = 100")
        print("\nCredentials ini berlaku sampai di-revoke.")
        print("Simpan dengan aman — jangan commit ke repo.")

    except Exception as e:
        print(f"\nERROR: {e}")
        print("Pastikan wallet memiliki MATIC untuk gas (Polygon network).")
        sys.exit(1)


if __name__ == "__main__":
    main()
