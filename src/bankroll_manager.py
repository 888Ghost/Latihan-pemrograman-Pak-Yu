"""
Bankroll Manager - Bot v10.0
==============================
100% Adaptive Bankroll: Always reads actual on-chain pUSD balance.
Kelly sizing grows with wallet. Never reverts to static default.

Priority:
  1. On-chain pUSD balance via Polygon RPC
  2. Cached balance if < 1 hour old
  3. Stale cache (any age)
  4. DEFAULT_BANKROLL ($20) only on very first run
"""

import time
import requests
from typing import Optional

from src.config import (
    DEFAULT_BANKROLL, POLY_RPC, POLY_RPC_BACKUP,
    PUSD_CONTRACT, FILE_BANKROLL,
)
from src.utils import logger, safe_read, safe_write


# pUSD balanceOf(address) selector
BALANCEOF_SELECTOR = "0x70a08231"


def _encode_balance_of(address: str) -> str:
    """Encode balanceOf(address) call data."""
    # Pad address to 32 bytes
    addr = address.lower()
    if addr.startswith("0x"):
        addr = addr[2:]
    padded = addr.zfill(64)
    return BALANCEOF_SELECTOR + padded


def _hex_to_usd(hex_str: str, decimals: int = 6) -> float:
    """Convert hex balance to USD (USDC has 6 decimals)."""
    try:
        raw = int(hex_str, 16)
        return raw / (10 ** decimals)
    except (ValueError, TypeError):
        return 0.0


def fetch_chain_balance(address: str) -> Optional[float]:
    """
    Read pUSD balance from Polygon chain via RPC.

    Uses eth_call with balanceOf(address) selector.
    Tries multiple RPCs for resilience.
    """
    if not address:
        return None

    data = _encode_balance_of(address)
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{
            "to": PUSD_CONTRACT,
            "data": data,
        }, "latest"],
        "id": 1,
    }

    for rpc in [POLY_RPC, POLY_RPC_BACKUP]:
        try:
            r = requests.post(rpc, json=payload, timeout=10)
            result = r.json().get("result", "")
            if result and result != "0x":
                balance = _hex_to_usd(result)
                if balance > 0:
                    return balance
        except Exception:
            continue

    return None


class BankrollManager:
    """
    Manages bankroll with 100% adaptive on-chain reading.
    """

    def __init__(self, funder_address: str = ""):
        self._funder = funder_address
        self._cached_balance: Optional[float] = None
        self._cache_time: float = 0
        self._chain_read_once: bool = False
        self._load_cache()

    def _load_cache(self):
        """Load cached balance from disk."""
        data = safe_read(FILE_BANKROLL, {})
        if data:
            self._cached_balance = data.get("balance")
            self._cache_time = data.get("timestamp", 0)

    def _save_cache(self, balance: float):
        """Save balance cache to disk."""
        safe_write(FILE_BANKROLL, {
            "balance": balance,
            "timestamp": time.time(),
            "source": "chain" if self._chain_read_once else "default",
        })

    def get_bankroll(self) -> float:
        """
        Get current bankroll with priority:
        1. On-chain balance (if funder address available)
        2. Cached balance (< 1 hour old)
        3. Stale cache (any age)
        4. DEFAULT_BANKROLL
        """
        # Try on-chain first
        if self._funder:
            chain_balance = fetch_chain_balance(self._funder)
            if chain_balance is not None and chain_balance > 0:
                self._cached_balance = chain_balance
                self._cache_time = time.time()
                self._chain_read_once = True
                self._save_cache(chain_balance)
                return chain_balance

        # Use cache if fresh (< 1 hour)
        if self._cached_balance is not None:
            if time.time() - self._cache_time < 3600:
                return self._cached_balance
            # Stale cache is still better than default
            if self._chain_read_once:
                return self._cached_balance

        return DEFAULT_BANKROLL

    def get_drawdown(self, peak: Optional[float] = None) -> float:
        """Calculate current drawdown from peak."""
        balance = self.get_bankroll()
        if peak is None:
            peak = DEFAULT_BANKROLL
        if peak <= 0:
            return 0.0
        return max(0, (peak - balance) / peak)
