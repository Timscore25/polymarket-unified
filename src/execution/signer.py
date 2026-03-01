from __future__ import annotations

import time
from typing import Optional

from eth_account import Account
from eth_account.messages import encode_defunct


class OrderSigner:
    """EIP-191 order signer for Polymarket."""

    def __init__(self, private_key: str):
        self._private_key = private_key
        self._account = Account.from_key(private_key) if private_key else None

    @property
    def address(self) -> str:
        """Get the signer's address."""
        return self._account.address if self._account else ""

    def sign_order(self, order: dict) -> str:
        """
        Sign an order using EIP-191.

        Args:
            order: Order dict with market, side, size, price, time, salt

        Returns:
            Hex signature string
        """
        if not self._account:
            return ""

        # Create order hash
        message = self._create_order_message(order)
        message_hash = encode_defunct(text=message)

        # Sign
        signed = self._account.sign_message(message_hash)
        return signed.signature.hex()

    def _create_order_message(self, order: dict) -> str:
        """Create the message to sign."""
        parts = [
            str(order.get("market", "")),
            str(order.get("side", "")),
            str(order.get("size", "")),
            str(order.get("price", "")),
            str(order.get("time", "")),
            str(order.get("salt", "")),
        ]
        return ":".join(parts)

    def sign_auth_message(self, timestamp: int, nonce: str) -> str:
        """Sign an authentication message."""
        if not self._account:
            return ""

        message = f"{timestamp}:{nonce}"
        message_hash = encode_defunct(text=message)
        signed = self._account.sign_message(message_hash)
        return signed.signature.hex()

    @staticmethod
    def generate_salt() -> str:
        """Generate a unique salt for orders."""
        return str(int(time.time() * 1000000))
