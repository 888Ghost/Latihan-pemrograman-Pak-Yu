#!/usr/bin/env python3
"""
generate_credentials.py — v7 (CORRECT EIP-712 + V2 headers)
────────────────────────────────────────────────────────────────
Root cause semua error sebelumnya:
  v1-v6: Sign pakai EIP-191 personal_sign → SALAH
  V2 Polymarket memakai EIP-712 dengan domain ClobAuthDomain
  Header juga berubah: POLY-ADDRESS → POLY_ADDRESS (underscore)

Referensi resmi:
  https://docs.polymarket.com/developers/CLOB/authentication
  https://github.com/Polymarket/py-clob-client-v2

INSTALL (cukup sekali):
  pip install py-clob-client-v2

JALANKAN (Windows):
  set POLY_PRIVATE_KEY=0x...
  set POLY_FUNDER=0x...
  python generate_credentials.py
────────────────────────────────────────────────────────────────
"""
import os, sys, time, json, warnings
import requests
from eth_account import Account

try:
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
except: pass

CLOB = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon


def validate(pk, funder):
    if not pk: sys.exit("ERROR: set POLY_PRIVATE_KEY=0x...")
    if not funder: sys.exit("ERROR: set POLY_FUNDER=0x...")
    if not pk.startswith("0x"): pk = "0x" + pk
    if len(pk) != 66: sys.exit(f"ERROR: private key harus 66 karakter (kamu: {len(pk)})")
    if not funder.startswith("0x") or len(funder) != 42:
        sys.exit(f"ERROR: wallet address harus 42 karakter (kamu: {len(funder)})")
    return pk, funder


def make_eip712_l1_sig(account, nonce=0):
    """
    EIP-712 signature untuk L1 auth Polymarket V2.
    Domain: ClobAuthDomain v1 chainId=137
    Struct: ClobAuth(address, timestamp, nonce, message)
    Sumber: docs.polymarket.com/developers/CLOB/authentication
    """
    ts = str(int(time.time()))

    domain = {
        "name":    "ClobAuthDomain",
        "version": "1",
        "chainId": CHAIN_ID,
    }
    types = {
        "ClobAuth": [
            {"name": "address",   "type": "address"},
            {"name": "timestamp", "type": "string"},
            {"name": "nonce",     "type": "uint256"},
            {"name": "message",   "type": "string"},
        ]
    }
    value = {
        "address":   account.address,
        "timestamp": ts,
        "nonce":     nonce,
        "message":   "This message attests that I control the given wallet",
    }

    signed = account.sign_typed_data(
        domain_data=domain,
        message_types=types,
        message_data=value,
    )
    sig = signed.signature.hex()
    if not sig.startswith("0x"): sig = "0x" + sig
    return ts, sig


def l1_headers(account, nonce=0):
    """
    V2 L1 headers — semua pakai underscore (bukan hyphen seperti V1).
    POLY_ADDRESS / POLY_SIGNATURE / POLY_TIMESTAMP / POLY_NONCE
    """
    ts, sig = make_eip712_l1_sig(account, nonce)
    return {
        "POLY_ADDRESS":   account.address,
        "POLY_SIGNATURE": sig,
        "POLY_TIMESTAMP": ts,
        "POLY_NONCE":     str(nonce),
        "Content-Type":   "application/json",
        "Accept":         "application/json",
        "User-Agent":     "Mozilla/5.0",
    }


def try_derive(account):
    """GET /auth/derive-api-key — ambil credentials yang sudah ada."""
    hdrs = l1_headers(account, nonce=0)
    r = requests.get(f"{CLOB}/auth/derive-api-key",
                     headers=hdrs, verify=False, timeout=25)
    txt = r.text.strip()
    if r.status_code == 200 and txt:
        return r.json()
    raise Exception(f"HTTP {r.status_code}: {txt[:200]}")


def try_create(account):
    """POST /auth/api-key — buat credentials baru."""
    hdrs = l1_headers(account, nonce=0)
    r = requests.post(f"{CLOB}/auth/api-key",
                      headers=hdrs, verify=False, timeout=25)
    txt = r.text.strip()
    if r.status_code in (200, 201) and txt:
        return r.json()
    raise Exception(f"HTTP {r.status_code}: {txt[:200]}")


def try_sdk(pk):
    """
    Coba py_clob_client_v2 SDK (NAMA PAKAI UNDERSCORE di PyPI).
    Install: pip install py_clob_client_v2
    """
    try:
        from py_clob_client_v2 import ClobClient
    except ImportError:
        raise Exception(
            "py_clob_client_v2 tidak terinstall.\n"
            "  Jalankan: pip install py_clob_client_v2\n"
            "  PENTING: gunakan UNDERSCORE, bukan hyphen!"
        )

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=CHAIN_ID,
        key=pk,
    )
    result = client.create_or_derive_api_key()
    if result:
        return result
    raise Exception("SDK mengembalikan None")


def parse(data):
    if isinstance(data, dict):
        k = data.get("key") or data.get("apiKey") or data.get("api_key") or ""
        s = data.get("secret") or data.get("apiSecret") or data.get("api_secret") or ""
        p = data.get("passphrase") or data.get("apiPassphrase") or data.get("api_passphrase") or ""
        return k, s, p
    # Beberapa SDK mengembalikan object
    try:
        return getattr(data,"key",""), getattr(data,"secret",""), getattr(data,"passphrase","")
    except: return "", "", ""


def main():
    pk     = os.environ.get("POLY_PRIVATE_KEY","").strip()
    funder = os.environ.get("POLY_FUNDER","").strip()
    pk, funder = validate(pk, funder)

    print(f"\nWallet : {funder[:8]}...{funder[-4:]}")
    print(f"Host   : {CLOB}")
    print(f"Auth   : EIP-712 ClobAuthDomain v1 (Polymarket V2 standard)\n")

    try:
        account = Account.from_key(pk)
    except Exception as e:
        sys.exit(f"ERROR: Private key tidak valid — {e}")

    if account.address.lower() != funder.lower():
        print(f"[!] Derived address : {account.address}")
        print(f"[!] POLY_FUNDER     : {funder}")
        print(f"[!] BERBEDA — pastikan private key dan wallet address dari wallet yang sama.\n")

    data = None
    errors = []

    # Metode 0: py-clob-client-v2 SDK (paling resmi)
    print("Metode 0: py-clob-client-v2 SDK...")
    try:
        data = try_sdk(pk)
        print("  Berhasil via SDK!\n")
    except Exception as e:
        errors.append(f"  SDK: {e}")
        print(f"  Tidak tersedia: {e}\n")

    # Metode 1: GET /auth/derive-api-key (EIP-712)
    if not data:
        print("Metode 1: GET /auth/derive-api-key (EIP-712)...")
        try:
            data = try_derive(account)
            print("  Berhasil!\n")
        except Exception as e:
            errors.append(f"  GET derive: {e}")
            print(f"  Gagal: {e}\n")

    # Metode 2: POST /auth/api-key (EIP-712)
    if not data:
        print("Metode 2: POST /auth/api-key (EIP-712)...")
        try:
            data = try_create(account)
            print("  Berhasil!\n")
        except Exception as e:
            errors.append(f"  POST create: {e}")
            print(f"  Gagal: {e}\n")

    if not data:
        print("="*65)
        print("  SEMUA METODE GAGAL")
        print("="*65)
        for e in errors: print(e)
        print()
        print("Langkah debug:")
        print("1. Install SDK: pip install py-clob-client-v2")
        print("2. Jalankan lagi — SDK sudah handle semua signing")
        print("3. Pastikan wallet sudah connect ke polymarket.com")
        print("   dan sudah accept Terms & Conditions")
        return

    api_key, secret, passph = parse(data)
    if not api_key:
        print(f"ERROR: Response tidak dikenal:\n{data}")
        return

    print("="*65)
    print("  BERHASIL — Masukkan ke GitHub Secrets:")
    print("="*65)
    print(f"CLOB_API_KEY        = {api_key}")
    print(f"CLOB_API_SECRET     = {secret}")
    print(f"CLOB_API_PASSPHRASE = {passph}")
    print("="*65)
    print()
    print("Tambahkan juga:")
    print(f"POLY_PRIVATE_KEY    = (private key kamu)")
    print(f"POLY_FUNDER         = {funder}")
    print(f"TELEGRAM_TOKEN      = (dari @BotFather)")
    print(f"TELEGRAM_CHAT_ID    = (dari @userinfobot)")
    print(f"BANKROLL            = 20")
    print(f"DRY_RUN             = true")
    print()
    print("Setelah masuk GitHub Secrets → hapus file ini dari laptop.")


if __name__ == "__main__":
    main()
