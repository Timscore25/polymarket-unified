from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional
from collections import defaultdict

import websockets
import orjson

from src.config import Settings
from src.core.orderbook import MultiTokenOrderBook
from src.utils.logging import get_logger

logger = get_logger(__name__)


class WebSocketManager:
    """High-performance WebSocket manager for real-time market data."""

    def __init__(self, settings: Settings, orderbooks: MultiTokenOrderBook):
        self.settings = settings
        self.orderbooks = orderbooks
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._subscribed_tokens: set[str] = set()
        self._handlers: dict[str, list[Callable]] = defaultdict(list)
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0

    async def connect(self) -> None:
        """Connect to WebSocket server."""
        try:
            self._ws = await websockets.connect(
                self.settings.polymarket_ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            )
            self._running = True
            self._reconnect_delay = 1.0
            logger.info("WebSocket connected")
        except Exception as e:
            logger.error("WebSocket connection failed", error=str(e))
            raise

    async def close(self) -> None:
        """Close WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("WebSocket closed")

    async def subscribe(self, token_ids: list[str]) -> None:
        """Subscribe to order book updates for tokens."""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        new_tokens = [t for t in token_ids if t not in self._subscribed_tokens]

        if not new_tokens:
            return

        # Send single subscription message with all asset IDs
        # Format from Polymarket CLOB WebSocket API
        msg = orjson.dumps({
            "assets_ids": new_tokens,
            "type": "market"
        }).decode()

        await self._ws.send(msg)
        logger.info("Subscription message sent", assets_count=len(new_tokens))

        for token_id in new_tokens:
            self._subscribed_tokens.add(token_id)

    async def unsubscribe(self, token_ids: list[str]) -> None:
        """Unsubscribe from token updates."""
        if not self._ws:
            return

        for token_id in token_ids:
            if token_id in self._subscribed_tokens:
                msg = orjson.dumps({
                    "type": "unsubscribe",
                    "channel": "book",
                    "market": token_id,
                }).decode()

                await self._ws.send(msg)
                self._subscribed_tokens.discard(token_id)

    def on(self, event: str, handler: Callable) -> None:
        """Register event handler."""
        self._handlers[event].append(handler)

    async def _emit(self, event: str, data: Any) -> None:
        """Emit event to handlers."""
        for handler in self._handlers.get(event, []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    handler(data)
            except Exception as e:
                logger.error("Handler error", event=event, error=str(e))

    async def listen(self) -> None:
        """Main listen loop with auto-reconnect."""
        while self._running:
            try:
                if not self._ws:
                    await self.connect()

                async for message in self._ws:
                    await self._handle_message(message)

            except websockets.ConnectionClosed as e:
                logger.warning("WebSocket connection closed", code=e.code)
                self._ws = None

                if self._running:
                    await self._reconnect()

            except Exception as e:
                logger.error("WebSocket error", error=str(e))
                self._ws = None

                if self._running:
                    await self._reconnect()

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff."""
        logger.info("Reconnecting", delay=self._reconnect_delay)
        await asyncio.sleep(self._reconnect_delay)

        self._reconnect_delay = min(
            self._reconnect_delay * 2,
            self._max_reconnect_delay
        )

        try:
            await self.connect()

            # Resubscribe to all tokens
            tokens = list(self._subscribed_tokens)
            self._subscribed_tokens.clear()
            await self.subscribe(tokens)

        except Exception as e:
            logger.error("Reconnection failed", error=str(e))

    async def _handle_message(self, raw_message: str | bytes) -> None:
        """Handle incoming WebSocket message."""
        try:
            # Handle bytes or string
            if isinstance(raw_message, bytes):
                msg_str = raw_message.decode('utf-8', errors='ignore')
            else:
                msg_str = raw_message

            # Skip empty or non-JSON messages (pings, etc.)
            msg_str = msg_str.strip()
            if not msg_str or not msg_str.startswith('{'):
                return

            data = orjson.loads(msg_str.encode() if isinstance(msg_str, str) else msg_str)
            # Handle both "type" and "event_type" fields
            msg_type = data.get("event_type") or data.get("type", "")
            logger.debug("WS message received", msg_type=msg_type)

            if msg_type == "book":
                await self._handle_book_update(data)
            elif msg_type == "price_change":
                await self._handle_price_change(data)
            elif msg_type == "trade":
                await self._emit("trade", data)
            elif msg_type == "subscribed":
                logger.info("Subscription confirmed", assets=data.get("assets_ids"))
            elif msg_type == "error":
                logger.error("WebSocket error message", error=data.get("message"))

        except orjson.JSONDecodeError:
            # Silently ignore non-JSON messages (pings, binary frames, etc.)
            pass
        except Exception as e:
            logger.error("Message handling error", error=str(e))

    async def _handle_book_update(self, data: dict) -> None:
        """Handle order book update - hot path, optimized."""
        # Handle both "asset_id" (CLOB WS) and "market" field names
        token_id = data.get("asset_id") or data.get("market", "")
        if not token_id:
            return

        # Handle both "bids"/"asks" and "buys"/"sells" field names
        bids = data.get("bids") or data.get("buys", [])
        asks = data.get("asks") or data.get("sells", [])

        logger.debug(
            "Book update received",
            token_id=token_id[:20] + "..." if len(token_id) > 20 else token_id,
            bid_levels=len(bids),
            ask_levels=len(asks),
        )

        book = self.orderbooks.get_or_create(token_id)

        # Check if full snapshot or delta
        if bids or asks:
            # Convert to expected format [[price, size], ...]
            formatted_bids = []
            formatted_asks = []

            for b in bids:
                if isinstance(b, dict):
                    formatted_bids.append([b.get("price", 0), b.get("size", 0)])
                elif isinstance(b, (list, tuple)) and len(b) >= 2:
                    formatted_bids.append([b[0], b[1]])

            for a in asks:
                if isinstance(a, dict):
                    formatted_asks.append([a.get("price", 0), a.get("size", 0)])
                elif isinstance(a, (list, tuple)) and len(a) >= 2:
                    formatted_asks.append([a[0], a[1]])

            if formatted_bids or formatted_asks:
                book.update_from_snapshot(formatted_bids, formatted_asks)
                logger.info(
                    "Order book updated",
                    token_id=token_id[:20] + "...",
                    bids=len(formatted_bids),
                    asks=len(formatted_asks),
                    best_bid=f"{book.best_bid:.4f}" if book.best_bid else "N/A",
                    best_ask=f"{book.best_ask:.4f}" if book.best_ask < 1.0 else "N/A",
                )

        elif "changes" in data:
            # Incremental update
            for change in data["changes"]:
                side = change.get("side", "")
                price = float(change.get("price", 0))
                size = float(change.get("size", 0))
                book.update_level(side, price, size)

        await self._emit("book_update", {
            "token_id": token_id,
            "book": book,
        })

    async def _handle_price_change(self, data: dict) -> None:
        """Handle price change event.

        price_change events contain the CURRENT MARKET PRICES where trades
        actually happen. The raw order book often has stale/extreme orders
        (0.01 bid, 0.99 ask) that don't reflect real trading activity.

        We use price_change to update our view of tradeable prices.
        """
        price_changes = data.get("price_changes", [])

        for pc in price_changes:
            token_id = pc.get("asset_id", "")
            if not token_id:
                continue

            best_bid = pc.get("best_bid")
            best_ask = pc.get("best_ask")

            if best_bid is not None or best_ask is not None:
                book = self.orderbooks.get_or_create(token_id)

                # Update with price_change prices (real market prices)
                # Use moderate size - these represent where trades happen
                if best_bid is not None:
                    try:
                        bid_price = float(best_bid)
                        # Only update if it's a real price (not 0 or dust)
                        if 0.02 < bid_price < 0.98:
                            book.update_level("buy", bid_price, 100.0)
                    except (ValueError, TypeError):
                        pass

                if best_ask is not None:
                    try:
                        ask_price = float(best_ask)
                        # Only update if it's a real price (not 1.0 or dust)
                        if 0.02 < ask_price < 0.98:
                            book.update_level("sell", ask_price, 100.0)
                    except (ValueError, TypeError):
                        pass

        await self._emit("price_change", data)
