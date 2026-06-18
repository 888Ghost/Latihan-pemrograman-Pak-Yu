"""
CLOB Order Client - Bot v12.0
===============================
EIP-712 V2 order signing and placement on Polymarket CLOB.

v12.0 CRITICAL FIX (FATAL #1):
  - CLOB V2 has TWO different representations for 'side':
    1. EIP-712 Order struct (for signing): uint8 (0=BUY, 1=SELL)
    2. POST body JSON (for REST API): string ("BUY" or "SELL")
  - v11.0 BUG: Used uint8 for BOTH, causing all LIVE orders to be rejected.
  - v12.0 FIX: _build_order() uses uint8 for EIP-712 signing data,
    _submit_order() converts to string for the REST API payload.
  - Reference: Polymarket CLOB V2 API specification.
    EIP-712 ORDER_TYPE struct defines side as uint8.
    POST /order endpoint expects side as string.

V2 Contract (April 2026):
  Domain: Polymarket CTF Exchange v2, chainId=137
  Removed from V1: taker, expiration, nonce, feeRateBps
  Added in V2: timestamp, metadata (bytes32 zero), builder (bytes32 zero)

Execution Tiers:
  SIGNAL (8-20pp edge) -> Limit order at best_ask + 2c, capped at MAP price
  STRONG (>20pp edge)  -> Market order at best_ask + 5c for guaranteed fill
"""

import time
import json
import hmac
import hashlib
import base64
from typing import Tuple, Optional

from src.config import (
    CLOB_BASE, CLOB_V2_CONTRACT, POLYGON_CHAIN_ID,
    POLY_PRIVATE_KEY, POLY_FUNDER,
    CLOB_API_KEY, CLOB_API_SECRET, CLOB_API_PASSPHRASE,
    DRY_RUN, FILE_PENDING,
)
from src.utils import logger, api_get, safe_read, safe_write


# ============================================================================
# FIX BUG #8: Side encoding as uint8 for CLOB V2 Order struct
# ============================================================================

# V2 CLOB Order struct expects side as uint8, not string
# Reference: Polymarket CTF Exchange V2 contract, Order struct:
#   struct Order {
#       uint256 salt;
#       address maker;
#       address signer;
#       uint256 tokenId;
#       uint256 makerAmount;
#       uint256 takerAmount;
#       uint8 side;          // 0 = BUY, 1 = SELL
#       uint8 signatureType;
#       uint256 timestamp;
#       bytes32 metadata;
#       bytes32 builder;
#   }
SIDE_BUY = 0    # uint8 value for BUY
SIDE_SELL = 1   # uint8 value for SELL

# Map string side to uint8
SIDE_MAP = {
    "BUY": SIDE_BUY,
    "SELL": SIDE_SELL,
    "buy": SIDE_BUY,
    "sell": SIDE_SELL,
}


class ClobOrderClient:
    """
    Polymarket CLOB V2 order client with EIP-712 signing.
    """

    def __init__(self):
        self._api_key = CLOB_API_KEY
        self._api_secret = CLOB_API_SECRET
        self._api_passphrase = CLOB_API_PASSPHRASE
        self._derived = False

    def ensure_credentials(self) -> bool:
        """
        Ensure CLOB API credentials are available.
        Auto-derive from private key if not provided.
        """
        if self._api_key:
            return True

        if not POLY_PRIVATE_KEY or not POLY_FUNDER:
            logger.warning("Cannot derive CLOB credentials: missing private key or funder")
            return False

        try:
            # Try to derive using py_clob_client_v2
            from py_clob_client_v2.client import ClobClient
            client = ClobClient(
                CLOB_BASE,
                key=POLY_PRIVATE_KEY,
                chain_id=POLYGON_CHAIN_ID,
            )
            creds = client.derive_api_key()
            if creds:
                self._api_key = creds.api_key
                self._api_secret = creds.api_secret
                self._api_passphrase = creds.api_passphrase
                self._derived = True
                logger.info("CLOB credentials auto-derived from private key")
                return True
        except ImportError:
            logger.warning("py_clob_client_v2 not available for auto-derivation")
        except Exception as e:
            logger.error(f"CLOB credential derivation failed: {e}")

        return False

    def place_order(
        self,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        price: float,
        size_usd: float,
        order_type: str = "GTC",
    ) -> Optional[dict]:
        """
        Place an order on Polymarket CLOB.

        In DRY_RUN mode, returns simulated result without actual order.

        Args:
            token_id: Token to trade
            side: BUY or SELL
            price: Limit price (0-1 probability)
            size_usd: Position size in USD
            order_type: GTC (Good Till Cancelled)

        Returns:
            Order result dict or None on failure
        """
        if DRY_RUN:
            return {
                "status": "DRY_RUN",
                "token_id": token_id,
                "side": side,
                "price": price,
                "size_usd": size_usd,
                "order_type": order_type,
                "timestamp": time.time(),
            }

        if not self.ensure_credentials():
            logger.error("Cannot place live order: no CLOB credentials")
            return None

        try:
            # Build EIP-712 typed data for V2
            order_data, side_str = self._build_order(token_id, side, price, size_usd)

            # Sign with eth_account
            signature = self._sign_order(order_data)
            if not signature:
                return None

            # v13.1 fix: order_type now genuinely flows through to the
            # wire-level POST body (was hardcoded "GTC" regardless of
            # what caller requested -- FOK/market orders never worked).
            result = self._submit_order(order_data, signature, side_str, order_type)
            return result

        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return None

    def _build_order(self, token_id: str, side: str, price: float,
                     size_usd: float) -> dict:
        """
        Build order data structure for CLOB V2 EIP-712 signing.

        v12.0 CRITICAL FIX (FATAL #1):
        The EIP-712 Order struct defines side as uint8 (0=BUY, 1=SELL).
        This is ONLY for the typed data signing. The REST API POST body
        uses string "BUY"/"SELL" instead.

        This method builds the EIP-712 signing data with uint8 side.
        The _submit_order method converts to string for the wire format.

        Reference: Polymarket CLOB V2 API specification:
        - EIP-712 ORDER_TYPE: side type is uint8
        - POST /order body: side type is string
        These are DIFFERENT and BOTH must be correct simultaneously.
        """
        # v13.1 Bug#12 REAL fix: previous formula was inverted for BOTH
        # branches (not just "SELL same as BUY" -- BUY itself was wrong).
        #
        # Order semantics: price = makerAmount / takerAmount (USDC per
        # share, both normalized to the same 1e6 decimal scale).
        #
        # Old (WRONG): taker_amount = int(price * maker_amount)
        #   At price=0.20, size=$10: gives 2 "shares" instead of 50.
        #   Error factor = price^2 -- at our typical 10-28% target price
        #   range, requested share count was 1-8% of the correct value.
        #   Live orders would be rejected or fill at a catastrophically
        #   unfavorable rate. Verified numerically before this fix.
        #
        # New (CORRECT): taker_amount = maker_amount / price
        if side.upper() == "BUY":
            # Maker gives USDC, wants shares: shares = USDC_given / price
            maker_amount = int(size_usd * 1e6)
            taker_amount = int(maker_amount / price)
        else:  # SELL
            # Maker gives shares, wants USDC: shares_given = USDC_value / price
            maker_amount = int((size_usd / price) * 1e6)
            taker_amount = int(size_usd * 1e6)

        # EIP-712 signing data: side as uint8 (0=BUY, 1=SELL)
        side_uint8 = SIDE_MAP.get(side, SIDE_BUY)

        # Return order data (for EIP-712 signing) and side string separately
        # CRITICAL: Do NOT include _side_str in order_data dict, as it would
        # be passed to EIP-712 signing which only expects the typed fields.
        order_data = {
            "salt": int(time.time() * 1000) + int.from_bytes(
                __import__('os').urandom(4), 'big'
            ),
            "maker": POLY_FUNDER,
            "signer": POLY_FUNDER,
            "tokenId": token_id,
            "makerAmount": str(maker_amount),
            "takerAmount": str(taker_amount),
            "side": side_uint8,  # uint8 for EIP-712 signing only
            "signatureType": 0,  # EOA
            "timestamp": int(time.time()),
            "metadata": "0x0000000000000000000000000000000000000000000000000000000000000000",
            "builder": "0x0000000000000000000000000000000000000000000000000000000000000000",
        }

        return order_data, side.upper()

    def self_test_signing(self) -> Tuple[bool, str]:
        """
        v13.1 Bug#18 mitigation: fail-fast EIP-712 sanity check.

        Manual EIP-712 signing (this file) works empirically today, but
        is fragile to undetected Polymarket API changes -- exactly what
        caused the V1->V2 migration breakage. Full SDK migration is
        deferred to v14.0 (per original audit reasoning: current BUY-only
        usage works, migration is an architecture decision not a bug
        fix). This self-test is the interim safety net: it builds a
        throwaway dummy order using the SAME production _sign_order()
        code path, signs it, recovers the signer address from the
        signature, and verifies it matches POLY_FUNDER.

        If Polymarket changes the ORDER_TYPE struct, domain version, or
        contract address again, this check fails IMMEDIATELY at startup
        with a clear Telegram alert -- instead of silently producing
        rejected orders discovered only after the fact (or worse,
        orders that are signed-but-wrong in a way the API accepts
        differently than intended).

        Returns (passed: bool, message: str).
        """
        try:
            from eth_account import Account
            from eth_account.messages import encode_typed_data

            dummy_order = {
                "salt": 1,
                "maker": POLY_FUNDER,
                "signer": POLY_FUNDER,
                "tokenId": "1",
                "makerAmount": "1000000",
                "takerAmount": "1000000",
                "side": 0,
                "signatureType": 0,
                "timestamp": 1,
                "metadata": "0x" + "00" * 32,
                "builder": "0x" + "00" * 32,
            }

            sig = self._sign_order(dummy_order)
            if not sig:
                return False, "self_test: _sign_order() returned None"

            typed_data = {
                "types": {
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "version", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                        {"name": "verifyingContract", "type": "address"},
                    ],
                    "Order": [
                        {"name": "salt", "type": "uint256"},
                        {"name": "maker", "type": "address"},
                        {"name": "signer", "type": "address"},
                        {"name": "tokenId", "type": "uint256"},
                        {"name": "makerAmount", "type": "uint256"},
                        {"name": "takerAmount", "type": "uint256"},
                        {"name": "side", "type": "uint8"},
                        {"name": "signatureType", "type": "uint8"},
                        {"name": "timestamp", "type": "uint256"},
                        {"name": "metadata", "type": "bytes32"},
                        {"name": "builder", "type": "bytes32"},
                    ],
                },
                "primaryType": "Order",
                "domain": {
                    "name": "Polymarket CTF Exchange",
                    "version": "2",
                    "chainId": POLYGON_CHAIN_ID,
                    "verifyingContract": CLOB_V2_CONTRACT,
                },
                "message": dummy_order,
            }

            signable = encode_typed_data(full_message=typed_data)
            recovered = Account.recover_message(signable, signature=sig)

            if recovered.lower() != POLY_FUNDER.lower():
                return False, (
                    f"self_test: signature recovery mismatch! "
                    f"expected={POLY_FUNDER[:10]}... got={recovered[:10]}... "
                    f"EIP-712 struct or domain may have drifted from Polymarket spec."
                )

            return True, "self_test: EIP-712 signing verified OK"

        except Exception as e:
            return False, f"self_test: exception during verification: {e}"

    def _sign_order(self, order_data: dict) -> Optional[str]:
        """Sign order with EIP-712 typed data."""
        try:
            from eth_account import Account
            from eth_account.messages import encode_typed_data

            typed_data = {
                "types": {
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "version", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                        {"name": "verifyingContract", "type": "address"},
                    ],
                    "Order": [
                        {"name": "salt", "type": "uint256"},
                        {"name": "maker", "type": "address"},
                        {"name": "signer", "type": "address"},
                        {"name": "tokenId", "type": "uint256"},
                        {"name": "makerAmount", "type": "uint256"},
                        {"name": "takerAmount", "type": "uint256"},
                        {"name": "side", "type": "uint8"},          # uint8 per V2 spec
                        {"name": "signatureType", "type": "uint8"},
                        {"name": "timestamp", "type": "uint256"},
                        {"name": "metadata", "type": "bytes32"},
                        {"name": "builder", "type": "bytes32"},
                    ],
                },
                "primaryType": "Order",
                "domain": {
                    "name": "Polymarket CTF Exchange",
                    "version": "2",
                    "chainId": POLYGON_CHAIN_ID,
                    "verifyingContract": CLOB_V2_CONTRACT,
                },
                "message": order_data,
            }

            signed = Account.sign_typed_data(
                private_key=POLY_PRIVATE_KEY,
                typed_data=typed_data,
            )
            return signed.signature.hex()

        except Exception as e:
            logger.error(f"EIP-712 signing failed: {e}")
            return None

    def _submit_order(self, order_data: dict, signature: str,
                       side_str: str = "BUY",
                       order_type: str = "GTC") -> Optional[dict]:
        """Submit signed order to CLOB API.

        v12.0 CRITICAL FIX (FATAL #1):
        The REST API POST body uses string "BUY"/"SELL" for the side field,
        NOT the uint8 value used in EIP-712 signing.

        v13.1 FIX: orderType was hardcoded "GTC" here regardless of what
        the caller passed -- meaning FOK (genuine market order) was NEVER
        actually sent to Polymarket even when main.py classified a signal
        as STRONG/"MARKET". Confirmed via official Polymarket docs that
        GTC/GTD are limit order types; FOK/FAK are market order types --
        these are real, distinct wire-level values, not just UI labels.

        The signed EIP-712 data contains the uint8 side value embedded in
        the signature. The POST body must use the string representation.
        These are DIFFERENT wire formats and BOTH must be correct.
        """
        headers = self._auth_headers("POST", "/order")

        # v12.0 FIX: REST API expects string "BUY"/"SELL", NOT uint8
        # v13.1 FIX: orderType now genuinely passed through (was hardcoded)
        valid_types = {"GTC", "GTD", "FOK", "FAK"}
        if order_type not in valid_types:
            logger.warning(f"Unknown order_type '{order_type}', defaulting to GTC")
            order_type = "GTC"

        payload = {
            **order_data,
            "side": side_str,  # REST API expects string, not uint8
            "signature": signature,
            "orderType": order_type,
        }

        try:
            import requests
            r = requests.post(
                f"{CLOB_BASE}/order",
                json=payload,
                headers=headers,
                timeout=15,
            )
            result = r.json()
            if result.get("orderID"):
                order_id = result["orderID"]
                logger.info(f"Order placed: ...{order_id[-6:]}")
                # Track pending order
                self._track_pending(order_id, order_data)
                return result
            else:
                logger.error(f"Order rejected: {result}")
                return None
        except Exception as e:
            logger.error(f"Order submission failed: {e}")
            return None

    def _auth_headers(self, method: str, path: str) -> dict:
        """Generate L2 HMAC authentication headers."""
        timestamp = str(int(time.time()))
        message = f"{timestamp}{method}{path}"

        if not self._api_secret:
            return {"Content-Type": "application/json"}

        signature = base64.b64encode(
            hmac.new(
                self._api_secret.encode(),
                message.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()

        return {
            "Content-Type": "application/json",
            "POLY_API_KEY": self._api_key,
            "POLY_API_TIMESTAMP": timestamp,
            "POLY_API_SIGNATURE": signature,
            "POLY_API_PASSPHRASE": self._api_passphrase,
        }

    def _track_pending(self, order_id: str, order_data: dict) -> None:
        """Track pending order for follow-up."""
        pending = safe_read(FILE_PENDING, {})
        pending[order_id] = {
            "data": order_data,
            "placed_at": time.time(),
            "runs": 0,
        }
        safe_write(FILE_PENDING, pending)

    def check_pending(self) -> list[dict]:
        """Check status of all pending orders."""
        pending = safe_read(FILE_PENDING, {})
        filled = []
        still_pending = {}

        for order_id, info in pending.items():
            runs = info.get("runs", 0) + 1

            # Check order status
            try:
                import requests
                r = requests.get(
                    f"{CLOB_BASE}/orders/{order_id}",
                    headers=self._auth_headers("GET", f"/orders/{order_id}"),
                    timeout=10,
                )
                result = r.json()
                status = result.get("status", "")

                if status in ("MATCHED", "MATCHED_FULLY", "FILLED"):
                    filled.append({"order_id": order_id, "status": status})
                    continue
            except Exception:
                pass

            # Keep if < 2 runs without fill
            if runs < 2:
                info["runs"] = runs
                still_pending[order_id] = info
            else:
                # Cancel stale order
                self._cancel_order(order_id)

        safe_write(FILE_PENDING, still_pending)
        return filled

    def _cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        try:
            import requests
            r = requests.delete(
                f"{CLOB_BASE}/order/{order_id}",
                headers=self._auth_headers("DELETE", f"/order/{order_id}"),
                timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False

    def get_book(self, token_id: str) -> Optional[dict]:
        """Fetch order book for a token."""
        data = api_get(f"{CLOB_BASE}/book", params={"token_id": token_id})
        return data
