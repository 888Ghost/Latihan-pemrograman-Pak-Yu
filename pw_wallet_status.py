#!/usr/bin/env python3
"""Inspect Polymarket signer/funder/deposit wallet balances without exposing secrets.
pw_ version — uses same pUSD contract as pw_bot_v9_3.py.
"""
import os
import sys
import requests  # type: ignore

try:
    from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]
    load_dotenv()
except Exception:
    pass

# ── Token contract on Polygon ──
# pUSD (USDC.e proxy used by Polymarket CTF Exchange)
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

RPCS = ("https://polygon-bor-rpc.publicnode.com", "https://rpc.ankr.com/polygon",
        "https://polygon-rpc.com")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def short(addr: str) -> str:
    if "--full" in sys.argv:
        return addr or "(empty)"
    return f"{addr[:8]}...{addr[-4:]}" if addr else "(empty)"


def rpc_call(payload: dict) -> str:
    last = ""
    for rpc in RPCS:
        try:
            res = requests.post(rpc, json=payload, timeout=10)
            if res.status_code != 200:
                last = f"{rpc} HTTP {res.status_code}"
                continue
            data = res.json()
            if "error" in data:
                last = f"{rpc} {data['error']}"
                continue
            return data.get("result", "0x0")
        except Exception as exc:
            last = f"{rpc} {exc}"
    raise RuntimeError(last or "all RPCs failed")


def native_balance(addr: str) -> float:
    payload = {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [addr, "latest"], "id": 1}
    return int(rpc_call(payload), 16) / 1e18


def erc20_balance(token: str, addr: str) -> float:
    padded = addr.lower().replace("0x", "").zfill(64)
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": token, "data": f"0x70a08231{padded}"}, "latest"],
        "id": 1,
    }
    return int(rpc_call(payload), 16) / 1e6


def expected_deposit_wallet(private_key: str) -> str:
    try:
        from py_builder_relayer_client.client import RelayClient  # pyright: ignore[reportMissingImports]
    except ImportError:
        raise RuntimeError("py_builder_relayer_client not installed — pip install py-builder-relayer-client")

    client = RelayClient(
        relayer_url=env("RELAYER_URL", "https://relayer-v2.polymarket.com/"),
        chain_id=137,
        private_key=private_key,
        rpc_url=env("RPC_URL", "https://polygon-bor-rpc.publicnode.com"),
    )
    return client.get_expected_deposit_wallet()


def clob_collateral_balance(private_key: str, funder: str, sig_type: int) -> str:
    try:
        from py_clob_client_v2 import ClobClient  # pyright: ignore[reportMissingImports]
        from py_clob_client_v2.clob_types import ApiCreds, AssetType, BalanceAllowanceParams  # pyright: ignore[reportMissingImports]
    except ImportError:
        raise RuntimeError("py_clob_client_v2 not installed — pip install py-clob-client-v2")

    # Auto-derive API credentials if not provided (same as pw_bot_v9_3.py)
    creds = None
    if all(env(k) for k in ("CLOB_API_KEY", "CLOB_API_SECRET", "CLOB_API_PASSPHRASE")):
        creds = ApiCreds(env("CLOB_API_KEY"), env("CLOB_API_SECRET"), env("CLOB_API_PASSPHRASE"))
    client = ClobClient(
        "https://clob.polymarket.com",
        137,
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
    data = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
    )
    return str(data)


def main() -> None:
    pk = env("POLY_PRIVATE_KEY")
    funder = env("POLY_FUNDER")
    if not pk:
        raise SystemExit("POLY_PRIVATE_KEY is missing — set it in .env or GitHub Secrets")

    from eth_account import Account  # pyright: ignore[reportMissingImports]

    signer = Account.from_key(pk).address
    print(f"signer: {short(signer)}")
    print(f"POLY_FUNDER env: {short(funder)}")

    candidates = [("signer", signer)]
    if funder and funder.lower() != signer.lower():
        candidates.append(("POLY_FUNDER", funder))

    try:
        deposit = expected_deposit_wallet(pk)
        print(f"expected deposit wallet: {short(deposit)}")
        if all(deposit.lower() != addr.lower() for _, addr in candidates):
            candidates.append(("expected_deposit_wallet", deposit))
    except Exception as exc:
        print(f"expected deposit wallet: failed ({exc})")

    print()
    for label, addr in candidates:
        print(f"{label}: {short(addr)}")
        print(f"  POL/MATIC: {native_balance(addr):.6f}")
        print(f"  pUSD: ${erc20_balance(PUSD, addr):.6f}")

    print()
    print("── CLOB Collateral (4 secrets sufficient) ──")
    for sig_type, label in ((0, "EOA"), (1, "POLY_PROXY"), (3, "POLY_1271")):
        target = funder if funder else signer
        try:
            bal = clob_collateral_balance(pk, target, sig_type)
            print(f"CLOB collateral using {label} funder={short(target)}: {bal}")
        except Exception as exc:
            print(f"CLOB collateral using {label} funder={short(target)}: failed ({exc})")


if __name__ == "__main__":
    main()
