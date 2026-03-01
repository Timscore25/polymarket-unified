from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Any
from collections import defaultdict

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from src.config import Settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Order:
    """Represents an order."""
    id: str
    market_id: str
    token_id: str
    side: str
    price: float
    size: float
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    filled_size: float = 0.0


class OrderManager:
    """Manages order lifecycle using official py-clob-client."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._clob_client: Optional[ClobClient] = None

        self._orders: dict[str, Order] = {}  # order_id -> Order
        self._market_orders: dict[str, list[str]] = defaultdict(list)  # market_id -> order_ids
        self._pending_cancels: set[str] = set()

    def _get_client(self) -> ClobClient:
        """Get or create the CLOB client with Level 2 authentication."""
        if self._clob_client is None:
            # First create L1 client to derive credentials
            client = ClobClient(
                host=self.settings.polymarket_api_url,
                chain_id=137,  # Polygon mainnet
                key=self.settings.private_key,
            )

            # Derive API credentials for L2 access
            try:
                creds = client.derive_api_key()
                logger.info(
                    "API credentials derived",
                    api_key=creds.api_key[:20] + "..."
                )

                # Create L2 client with credentials
                self._clob_client = ClobClient(
                    host=self.settings.polymarket_api_url,
                    chain_id=137,
                    key=self.settings.private_key,
                    creds=creds,
                )
            except Exception as e:
                logger.warning(
                    "Failed to derive L2 credentials, using L1",
                    error=str(e)
                )
                self._clob_client = client

            logger.info(
                "CLOB client initialized",
                address=self._clob_client.get_address()
            )
        return self._clob_client

    @property
    def address(self) -> str:
        """Get the signer's address."""
        return self._get_client().get_address()

    async def place_order(
        self,
        market_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float
    ) -> Optional[Order]:
        """
        Place a new order using official py-clob-client.

        Returns:
            Order object if successful, None otherwise
        """
        if self.settings.dry_run:
            timestamp = int(time.time() * 1000)
            order_id = f"dry-run-{timestamp}"
            order = Order(
                id=order_id,
                market_id=market_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                status="simulated"
            )
            self._orders[order_id] = order
            self._market_orders[market_id].append(order_id)
            logger.info("[DRY RUN] Would place order",
                       token_id=token_id[:20]+"...", side=side, price=price, size=size)
            return order

        try:
            client = self._get_client()

            # Convert side to py-clob-client constants
            order_side = BUY if side.upper() == "BUY" else SELL

            # Build order args
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=order_side,
            )

            # Create and sign the order
            signed_order = client.create_order(order_args)

            # Post the order
            result = client.post_order(signed_order, OrderType.GTC)

            order_id = result.get("orderID", f"local-{int(time.time()*1000)}")
            order = Order(
                id=order_id,
                market_id=market_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                status="open"
            )

            self._orders[order_id] = order
            self._market_orders[market_id].append(order_id)

            logger.info(
                "Order placed successfully",
                order_id=order_id,
                market_id=market_id,
                side=side,
                price=price,
                size=size
            )

            return order

        except Exception as e:
            logger.error("Order placement failed", error=str(e))
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order."""
        if order_id in self._pending_cancels:
            return True

        self._pending_cancels.add(order_id)

        if self.settings.dry_run:
            logger.info("[DRY RUN] Would cancel order", order_id=order_id)
            self._pending_cancels.discard(order_id)
            return True

        try:
            client = self._get_client()
            result = client.cancel(order_id)

            success = result.get("canceled", False) if isinstance(result, dict) else bool(result)

            if success:
                order = self._orders.get(order_id)
                if order:
                    order.status = "cancelled"

            self._pending_cancels.discard(order_id)
            return success

        except Exception as e:
            logger.error("Order cancel failed", order_id=order_id, error=str(e))
            self._pending_cancels.discard(order_id)
            return False

    async def cancel_market_orders(self, market_id: str) -> int:
        """Cancel all orders for a market."""
        order_ids = self._market_orders.get(market_id, [])
        order_ids = [oid for oid in order_ids if oid not in self._pending_cancels]

        if not order_ids:
            return 0

        cancelled = 0
        for order_id in order_ids:
            if await self.cancel_order(order_id):
                cancelled += 1
        return cancelled

    async def cancel_stale_orders(self, market_id: str) -> int:
        """Cancel orders older than lifetime threshold."""
        current_time = time.time()
        lifetime_secs = self.settings.mm_order_lifetime_ms / 1000

        order_ids = self._market_orders.get(market_id, [])
        stale_ids = []

        for oid in order_ids:
            order = self._orders.get(oid)
            if order and order.status == "open":
                age = current_time - order.created_at
                if age > lifetime_secs:
                    stale_ids.append(oid)

        if stale_ids:
            logger.debug("Cancelling stale orders", count=len(stale_ids), market_id=market_id)
            cancelled = 0
            for oid in stale_ids:
                if await self.cancel_order(oid):
                    cancelled += 1
            return cancelled

        return 0

    def get_open_orders(self, market_id: Optional[str] = None) -> list[Order]:
        """Get all open orders, optionally filtered by market."""
        orders = []
        for order in self._orders.values():
            if order.status == "open":
                if market_id is None or order.market_id == market_id:
                    orders.append(order)
        return orders

    def get_order(self, order_id: str) -> Optional[Order]:
        """Get a specific order."""
        return self._orders.get(order_id)

    def cleanup_filled_orders(self) -> None:
        """Remove filled/cancelled orders from tracking."""
        to_remove = [
            oid for oid, order in self._orders.items()
            if order.status in ("filled", "cancelled")
        ]

        for oid in to_remove:
            order = self._orders.pop(oid, None)
            if order:
                self._market_orders[order.market_id] = [
                    o for o in self._market_orders[order.market_id] if o != oid
                ]

    def get_stats(self) -> dict:
        """Get order manager statistics."""
        open_count = sum(1 for o in self._orders.values() if o.status == "open")
        return {
            "total_orders": len(self._orders),
            "open_orders": open_count,
            "pending_cancels": len(self._pending_cancels),
            "markets": list(self._market_orders.keys()),
        }
