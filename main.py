#!/usr/bin/env python3
"""
Local entrypoint for the Polymarket bot.

Loads .env, derives CLOB API credentials when needed, then runs polymarket_v9_2.
"""
import os
import sys
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

CLOB_BASE = "https://clob.polymarket.com"
CHAIN_ID = 137
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
POLYGON_RPCS = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _is_true(name: str, default: str = "false") -> bool:
    return _env(name, default).lower() in ("1", "true", "yes", "y", "on")


def _rpc_call(payload: dict) -> str:
    last_error = ""
    for rpc in POLYGON_RPCS:
        try:
            r = requests.post(rpc, json=payload, timeout=10)
            if r.status_code != 200:
                last_error = f"{rpc} HTTP {r.status_code}"
                continue
            data = r.json()
            if "error" in data:
                last_error = f"{rpc} {data['error']}"
                continue
            return data.get("result", "0x0")
        except Exception as exc:
            last_error = f"{rpc} {exc}"
    raise RuntimeError(last_error or "all Polygon RPCs failed")


def read_pusd_balance(wallet: str) -> float:
    padded = wallet.lower().replace("0x", "").zfill(64)
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": PUSD, "data": f"0x70a08231{padded}"}, "latest"],
        "id": 1,
    }
    return int(_rpc_call(payload), 16) / 1e6


def preflight_live_balance() -> None:
    if _is_true("DRY_RUN", "true"):
        return
    wallet = _env("POLY_FUNDER")
    if not wallet.startswith("0x") or len(wallet) != 42:
        sys.exit("[main] Missing/invalid POLY_FUNDER. Live trading needs the funded wallet address.")
    sig_type = int(_env("POLY_SIGNATURE_TYPE", "0") or 0)
    if sig_type == 3:
        balance = read_clob_collateral_balance(wallet, sig_type)
        print(f"[main] CLOB collateral balance: ${balance:.2f}")
        if balance < 1.0:
            sys.exit(
                f"[main] BLOCKED: CLOB collateral balance ${balance:.2f} is below live order minimum $1.00."
            )
        return
    try:
        balance = read_pusd_balance(wallet)
    except Exception as exc:
        sys.exit(f"[main] Could not verify pUSD balance before live trading: {exc}")

    max_order = float(_env("MAX_ORDER_USDC", "1") or 1)
    min_required = min(max_order, 1.0)
    print(f"[main] pUSD balance: ${balance:.2f}")
    if balance < min_required:
        sys.exit(
            f"[main] BLOCKED: pUSD balance ${balance:.2f} is below live order minimum ${min_required:.2f}. "
            "Polymarket UI balance may be internal/not on this wallet; fund or wrap to pUSD first."
        )


def read_clob_collateral_balance(funder: str, sig_type: int) -> float:
    private_key = _env("POLY_PRIVATE_KEY")
    if not private_key:
        sys.exit("[main] Missing POLY_PRIVATE_KEY. Live trading needs signer private key.")
    try:
        from py_clob_client_v2 import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds, AssetType, BalanceAllowanceParams
    except ImportError as exc:
        sys.exit(f"[main] Missing dependency: {exc}. Run: pip install -r requirements_v9_2.txt")

    creds = None
    if all(_env(k) for k in ("CLOB_API_KEY", "CLOB_API_SECRET", "CLOB_API_PASSPHRASE")):
        creds = ApiCreds(_env("CLOB_API_KEY"), _env("CLOB_API_SECRET"), _env("CLOB_API_PASSPHRASE"))
    client = ClobClient(
        CLOB_BASE,
        CHAIN_ID,
        key=private_key,
        creds=creds,
        signature_type=sig_type,
        funder=funder,
        use_server_time=True,
        retry_on_error=True,
    )
    if creds is None:
        creds = client.create_or_derive_api_key()
        client.set_api_creds(creds)
        os.environ["CLOB_API_KEY"] = creds.api_key
        os.environ["CLOB_API_SECRET"] = creds.api_secret
        os.environ["CLOB_API_PASSPHRASE"] = creds.api_passphrase
    data = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
    )
    return int(data.get("balance", "0")) / 1e6


def ensure_clob_credentials() -> None:
    if all(_env(k) for k in ("CLOB_API_KEY", "CLOB_API_SECRET", "CLOB_API_PASSPHRASE")):
        print("[main] CLOB credentials loaded from env.")
        return

    if _is_true("DRY_RUN", "true"):
        print("[main] DRY_RUN=true; CLOB credentials are optional.")
        return

    private_key = _env("POLY_PRIVATE_KEY")
    funder = _env("POLY_FUNDER")
    if not private_key:
        sys.exit("[main] Missing POLY_PRIVATE_KEY. Set it in .env before live trading.")

    try:
        from eth_account import Account
        from py_clob_client_v2 import ClobClient
        from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2
    except ImportError as exc:
        sys.exit(f"[main] Missing dependency: {exc}. Run: pip install -r requirements_v9_2.txt")

    account = Account.from_key(private_key)
    if not funder:
        funder = account.address
        os.environ["POLY_FUNDER"] = funder

    sig_type_env = _env("POLY_SIGNATURE_TYPE")
    sig_type = SignatureTypeV2(int(sig_type_env)) if sig_type_env else (
        SignatureTypeV2.POLY_PROXY if funder.lower() != account.address.lower() else SignatureTypeV2.EOA
    )
    print("[main] CLOB credentials missing; deriving/creating via py-clob-client-v2...")
    client = ClobClient(
        CLOB_BASE,
        CHAIN_ID,
        key=private_key,
        signature_type=sig_type,
        funder=funder,
        use_server_time=True,
        retry_on_error=True,
    )
    creds = client.create_or_derive_api_key()
    os.environ["CLOB_API_KEY"] = creds.api_key
    os.environ["CLOB_API_SECRET"] = creds.api_secret
    os.environ["CLOB_API_PASSPHRASE"] = creds.api_passphrase
    print("[main] CLOB credentials ready in memory.")
    print("[main] Optional: paste them into .env/GitHub Secrets to skip derivation next run.")


def main() -> None:
    preflight_live_balance()
    ensure_clob_credentials()
    import polymarket_v9_2

    polymarket_v9_2.main()


if __name__ == "__main__":
    main()
