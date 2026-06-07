#!/usr/bin/env python3
"""
generate_credentials.py — v3 (CLOB V2 compatible)
────────────────────────────────────────────────────────────────
Polymarket migrasi ke CLOB V2 pada April 28, 2026.
Script ini menggunakan py-clob-client-v2 (BUKAN py-clob-client).

INSTALL (sekali saja):
  pip install py-clob-client-v2

JALANKAN (Windows):
  set POLY_PRIVATE_KEY=0x...private_key_ETH_dari_bitget...
  set POLY_FUNDER=0x...wallet_address_ETH...
  python generate_credentials.py

JALANKAN (Mac/Linux):
  export POLY_PRIVATE_KEY=0x...private_key_ETH_dari_bitget...
  export POLY_FUNDER=0x...wallet_address_ETH...
  python3 generate_credentials.py

PRIVATE KEY: Pilih ETH di Bitget Wallet (sama untuk Polygon)
WALLET ADDRESS: Copy dari jaringan Ethereum atau Polygon (identik)
────────────────────────────────────────────────────────────────
"""
import os, sys, time


def validate_inputs(pk, funder):
    if not pk:
        print("ERROR: POLY_PRIVATE_KEY belum di-set.")
        print("  Windows: set POLY_PRIVATE_KEY=0x...")
        print("  Mac:     export POLY_PRIVATE_KEY=0x...")
        sys.exit(1)
    if not funder:
        print("ERROR: POLY_FUNDER belum di-set.")
        print("  Windows: set POLY_FUNDER=0x...")
        print("  Mac:     export POLY_FUNDER=0x...")
        sys.exit(1)
    if not pk.startswith("0x"):
        pk = "0x" + pk
    if len(pk) != 66:
        print(f"ERROR: Private key harus 66 karakter (0x + 64 hex).")
        print(f"  Panjang kamu: {len(pk)} karakter")
        print("  Pastikan copy SELURUH private key dari Bitget Wallet.")
        sys.exit(1)
    if not funder.startswith("0x") or len(funder) != 42:
        print(f"ERROR: Wallet address harus 42 karakter (0x + 40 hex).")
        print(f"  Panjang kamu: {len(funder)} karakter")
        sys.exit(1)
    return pk, funder


def main():
    pk     = os.environ.get("POLY_PRIVATE_KEY", "").strip()
    funder = os.environ.get("POLY_FUNDER", "").strip()
    pk, funder = validate_inputs(pk, funder)

    print(f"\nWallet: {funder[:8]}...{funder[-4:]}")
    print("Menghubungi Polymarket CLOB V2...\n")

    # ── Coba py-clob-client-v2 ─────────────────────────────────────
    v2_ok = False
    try:
        import importlib
        # Nama modul py-clob-client-v2 bisa bervariasi
        for mod_name in ["py_clob_client_v2", "clob_client_v2", "polymarket_clob_v2"]:
            try:
                mod = importlib.import_module(f"{mod_name}.client")
                const_mod = importlib.import_module(f"{mod_name}.constants")
                ClobClient = mod.ClobClient
                POLYGON = const_mod.POLYGON
                v2_ok = True
                print(f"Menggunakan SDK: {mod_name}")
                break
            except ImportError:
                continue
    except Exception:
        pass

    if not v2_ok:
        # Fallback: coba py-clob-client (V1 SDK, mungkin masih bisa untuk auth)
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.constants import POLYGON
            print("Menggunakan SDK: py-clob-client (v1 fallback)")
        except ImportError:
            print("ERROR: Library tidak ditemukan.")
            print()
            print("Install salah satu:")
            print("  pip install py-clob-client-v2   ← DIREKOMENDASIKAN untuk V2")
            print("  pip install py-clob-client      ← fallback V1")
            sys.exit(1)

    # ── Generate credentials ────────────────────────────────────────
    try:
        client = ClobClient(
            host           = "https://clob.polymarket.com",
            key            = pk,
            chain_id       = POLYGON,
            signature_type = 0,      # 0 = EOA (off-chain, TIDAK butuh MATIC)
            funder         = funder,
        )

        print("Membuat API credentials (off-chain, tidak perlu MATIC)...")
        creds = client.create_or_derive_api_creds()

        api_key    = getattr(creds, "api_key", None)    or getattr(creds, "apiKey", None)
        api_secret = getattr(creds, "api_secret", None) or getattr(creds, "secret", None)
        api_pass   = getattr(creds, "api_passphrase", None) or getattr(creds, "passphrase", None)

        if not api_key:
            print(f"ERROR: Respons tidak terduga: {creds}")
            sys.exit(1)

        print("\n" + "="*65)
        print("  BERHASIL — Simpan ke GitHub Secrets:")
        print("="*65)
        print(f"CLOB_API_KEY        = {api_key}")
        print(f"CLOB_API_SECRET     = {api_secret}")
        print(f"CLOB_API_PASSPHRASE = {api_pass}")
        print("="*65)
        print()
        print("Juga tambahkan ke GitHub Secrets:")
        print(f"POLY_PRIVATE_KEY    = {pk[:10]}...{pk[-4:]}")
        print(f"POLY_FUNDER         = {funder}")
        print(f"TELEGRAM_TOKEN      = (dari @BotFather)")
        print(f"TELEGRAM_CHAT_ID    = (dari @userinfobot)")
        print(f"BANKROLL            = 20")
        print(f"DRY_RUN             = true")
        print()
        print("Setelah semua masuk GitHub Secrets → hapus file credentials lokal.")

    except Exception as e:
        err = str(e)
        print(f"\nERROR: {err}\n")

        if "Request exception" in err or "connection" in err.lower() or "timeout" in err.lower():
            print("DIAGNOSIS: Masalah KONEKSI, bukan masalah wallet/MATIC.")
            print()
            print("Solusi:")
            print("1. Buka clob.polymarket.com di browser — bisa diakses?")
            print("2. Matikan VPN jika aktif, atau nyalakan jika diblokir ISP")
            print("3. Coba hotspot HP vs WiFi")
            print("4. Tunggu 5 menit, coba lagi")
        elif "Invalid" in err or "unauthorized" in err.lower():
            print("DIAGNOSIS: Private key atau address tidak cocok.")
            print("Pastikan POLY_PRIVATE_KEY dan POLY_FUNDER dari wallet YANG SAMA.")
        else:
            print("Coba:")
            print("1. pip install --upgrade py-clob-client-v2")
            print("2. Jalankan ulang script ini")


if __name__ == "__main__":
    main()
