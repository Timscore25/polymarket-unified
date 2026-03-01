from __future__ import annotations

import asyncio
import time
from typing import Any, Optional
from datetime import datetime, timezone

import httpx
import orjson

from src.config import Settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Rate limit retry configuration
MAX_RETRIES = 3
RETRY_DELAY_SECS = 1.0


def _safe_json_parse(content: bytes, default: Any = None) -> Any:
    """Safely parse JSON, returning default on error."""
    if not content:
        return default
    try:
        return orjson.loads(content)
    except (orjson.JSONDecodeError, ValueError) as e:
        logger.warning("JSON parse error", error=str(e))
        return default


class RestClient:
    """High-performance async REST client for Polymarket APIs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Optional[httpx.AsyncClient] = None
        self._gamma_client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client with connection pooling."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.polymarket_api_url,
                timeout=httpx.Timeout(30.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def _get_gamma_client(self) -> httpx.AsyncClient:
        """Get or create Gamma API client."""
        if self._gamma_client is None:
            self._gamma_client = httpx.AsyncClient(
                base_url=self.settings.gamma_api_url,
                timeout=httpx.Timeout(30.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._gamma_client

    async def close(self) -> None:
        """Close HTTP clients."""
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._gamma_client:
            await self._gamma_client.aclose()
            self._gamma_client = None

    # --- Market Discovery (Gamma API) ---

    async def discover_btc_markets(self, timeframes: list[str]) -> dict[str, dict]:
        """Discover current BTC up/down markets for given timeframes."""
        markets = {}

        for tf in timeframes:
            market = await self._find_current_market(self.settings.market_type, tf)
            if market:
                markets[tf] = market
                logger.info(f"Discovered {tf} market", market_id=market.get("id"),
                           question=market.get("question"))

        return markets

    async def _find_current_market(self, asset: str, timeframe: str) -> Optional[dict]:
        """Find the current active market for asset and timeframe."""
        client = await self._get_gamma_client()

        # Calculate current time window
        now = datetime.now(timezone.utc)

        # Determine interval in seconds
        if timeframe == "5m":
            interval_secs = 300
        elif timeframe == "15m":
            interval_secs = 900
        elif timeframe == "1h":
            interval_secs = 3600
        else:
            interval_secs = 300

        # Round to current interval
        timestamp = int(now.timestamp())
        current_window = (timestamp // interval_secs) * interval_secs

        # Build slug pattern
        slug = f"{asset}-updown-{timeframe}-{current_window}"

        try:
            response = await client.get("/markets", params={
                "slug": slug,
                "active": "true",
                "closed": "false",
            })
            response.raise_for_status()

            data = orjson.loads(response.content)
            if data and len(data) > 0:
                return data[0]

            # Try previous window if current not found
            prev_window = current_window - interval_secs
            slug = f"{asset}-updown-{timeframe}-{prev_window}"

            response = await client.get("/markets", params={
                "slug": slug,
                "active": "true",
                "closed": "false",
            })
            response.raise_for_status()

            data = orjson.loads(response.content)
            if data and len(data) > 0:
                return data[0]

            return None

        except Exception as e:
            logger.error(f"Market discovery failed", error=str(e), asset=asset, timeframe=timeframe)
            return None

    async def get_market_info(self, market_id: str) -> Optional[dict]:
        """Get detailed market information."""
        client = await self._get_gamma_client()

        try:
            response = await client.get(f"/markets/{market_id}")
            response.raise_for_status()
            return orjson.loads(response.content)
        except Exception as e:
            logger.error("Failed to get market info", market_id=market_id, error=str(e))
            return None

    # --- Order Book (CLOB API) ---

    async def get_orderbook(self, token_id: str) -> dict:
        """Get order book for a token."""
        client = await self._get_client()

        for attempt in range(MAX_RETRIES):
            try:
                response = await client.get("/book", params={"token_id": token_id})

                # Handle rate limiting
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", RETRY_DELAY_SECS))
                    logger.warning("Rate limited, retrying", retry_after=retry_after)
                    await asyncio.sleep(retry_after)
                    continue

                response.raise_for_status()
                return _safe_json_parse(response.content, {"bids": [], "asks": []})
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY_SECS * (attempt + 1))
                    continue
                logger.error("Failed to get orderbook", token_id=token_id, error=str(e))
            return {"bids": [], "asks": []}

    async def get_price(self, token_id: str) -> dict:
        """Get current price for a token."""
        client = await self._get_client()

        try:
            response = await client.get("/price", params={"token_id": token_id})
            response.raise_for_status()
            return orjson.loads(response.content)
        except Exception as e:
            logger.error("Failed to get price", token_id=token_id, error=str(e))
            return {}

    # --- Orders (CLOB API) ---

    async def place_order(self, order: dict) -> dict:
        """Place an order."""
        if self.settings.dry_run:
            logger.info("[DRY RUN] Would place order", order=order)
            return {"id": f"dry-run-{int(time.time()*1000)}", "status": "simulated"}

        client = await self._get_client()

        try:
            response = await client.post("/order", json=order)
            response.raise_for_status()
            result = orjson.loads(response.content)
            logger.info("Order placed", order_id=result.get("id"))
            return result
        except Exception as e:
            logger.error("Order placement failed", error=str(e), order=order)
            raise

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order."""
        if self.settings.dry_run:
            logger.info("[DRY RUN] Would cancel order", order_id=order_id)
            return True

        client = await self._get_client()

        try:
            response = await client.delete(f"/order/{order_id}")
            response.raise_for_status()
            logger.info("Order cancelled", order_id=order_id)
            return True
        except Exception as e:
            logger.error("Order cancellation failed", order_id=order_id, error=str(e))
            return False

    async def cancel_orders_batch(self, order_ids: list[str]) -> int:
        """Cancel multiple orders in batch."""
        if self.settings.dry_run:
            logger.info("[DRY RUN] Would cancel orders", count=len(order_ids))
            return len(order_ids)

        client = await self._get_client()

        try:
            response = await client.post("/orders/cancel", json={"orderIds": order_ids})
            response.raise_for_status()
            logger.info("Batch orders cancelled", count=len(order_ids))
            return len(order_ids)
        except Exception as e:
            logger.error("Batch cancel failed", error=str(e))
            return 0

    async def get_open_orders(self, maker: str, market_id: Optional[str] = None) -> list[dict]:
        """Get open orders for an address."""
        client = await self._get_client()

        params = {"maker": maker}
        if market_id:
            params["market"] = market_id

        try:
            response = await client.get("/open-orders", params=params)
            response.raise_for_status()
            return orjson.loads(response.content)
        except Exception as e:
            logger.error("Failed to get open orders", error=str(e))
            return []

    async def get_order_status(self, order_id: str) -> Optional[dict]:
        """Get status of a specific order."""
        client = await self._get_client()

        try:
            response = await client.get(f"/order/{order_id}")
            response.raise_for_status()
            return orjson.loads(response.content)
        except Exception as e:
            logger.error("Failed to get order status", order_id=order_id, error=str(e))
            return None
