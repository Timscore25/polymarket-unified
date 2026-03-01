from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Any

from src.config import Settings
from src.core.orderbook import MultiTokenOrderBook, OrderBook
from src.risk.manager import RiskManager
from src.strategies.base import Strategy
from src.utils.logging import get_logger
from src.utils.simulator import get_simulator

logger = get_logger(__name__)


@dataclass
class Quote:
    """A quote to be placed."""
    market_id: str
    token_id: str
    side: str  # "BUY" or "SELL"
    price: float
    size: float
    outcome: str  # "YES" or "NO"


@dataclass
class MMSignal:
    """Market making signal with quotes to place."""
    market_id: str
    yes_quote: Optional[Quote]
    no_quote: Optional[Quote]


class MarketMaker(Strategy):
    """
    Spread farming market maker strategy.

    Places passive limit orders on both sides to capture the spread.
    """

    def __init__(
        self,
        settings: Settings,
        orderbooks: MultiTokenOrderBook,
        risk_manager: RiskManager
    ):
        super().__init__(settings, orderbooks, risk_manager)
        self._markets: dict[str, dict] = {}  # market_id -> market info
        self._last_quote_time: dict[str, float] = {}
        self._open_orders: dict[str, list[str]] = {}  # market_id -> order_ids

    def add_market(self, market_id: str, market_info: dict) -> None:
        """Add a market to make."""
        self._markets[market_id] = market_info
        self._last_quote_time[market_id] = 0
        self._open_orders[market_id] = []
        logger.info("Added market to MM", market_id=market_id)

    def remove_market(self, market_id: str) -> None:
        """Remove a market."""
        self._markets.pop(market_id, None)
        self._last_quote_time.pop(market_id, None)
        self._open_orders.pop(market_id, None)

    async def check_opportunity(self) -> Optional[MMSignal]:
        """Check if we should refresh quotes on any market."""
        if not self.enabled or not self.settings.mm_enabled:
            return None

        current_time = time.time() * 1000

        for market_id, market_info in self._markets.items():
            last_quote = self._last_quote_time.get(market_id, 0)

            if current_time - last_quote < self.settings.mm_refresh_ms:
                continue

            # Check if we should stop trading
            if self.risk_manager.should_stop_trading():
                logger.warning("Risk limit reached - skipping MM")
                continue

            signal = self._generate_signal(market_id, market_info)
            if signal and (signal.yes_quote or signal.no_quote):
                self._last_quote_time[market_id] = current_time
                return signal

        return None

    def _generate_signal(self, market_id: str, market_info: dict) -> Optional[MMSignal]:
        """Generate quotes for a market."""
        yes_token = market_info.get("yes_token_id", "")
        no_token = market_info.get("no_token_id", "")

        if not yes_token or not no_token:
            logger.debug("Missing token IDs", market_id=market_id)
            return None

        yes_book = self.orderbooks.get_or_create(yes_token)
        no_book = self.orderbooks.get_or_create(no_token)

        # Check if books are fresh
        if yes_book.is_stale() or no_book.is_stale():
            logger.debug(
                "Order books stale",
                market_id=market_id,
                yes_last_update=yes_book.last_update,
                no_last_update=no_book.last_update,
                yes_bid_count=yes_book.bid_count,
                no_bid_count=no_book.bid_count,
            )
            return None

        # Get TRADEABLE prices (filter dust orders)
        # For prediction markets, realistic prices should be 0.10-0.90
        # Prices outside this range (0.03, 0.97) are dust/placeholder orders
        yes_bid, yes_bid_size = yes_book.get_tradeable_bid(min_price=0.10, min_size=10.0)
        yes_ask, yes_ask_size = yes_book.get_tradeable_ask(max_price=0.90, min_size=10.0)

        # Check if there's actual tradeable liquidity
        if yes_bid == 0 or yes_ask >= 1.0:
            logger.debug(
                "No tradeable liquidity",
                market_id=market_id,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                raw_bid=yes_book.best_bid,
                raw_ask=yes_book.best_ask,
            )
            return None

        # Log what tradeable prices we found vs raw
        logger.info(
            "Tradeable liquidity found",
            market_id=market_id,
            tradeable_bid=f"{yes_bid:.4f}",
            tradeable_bid_size=f"{yes_bid_size:.0f}",
            tradeable_ask=f"{yes_ask:.4f}",
            tradeable_ask_size=f"{yes_ask_size:.0f}",
            raw_bid=f"{yes_book.best_bid:.4f}",
            raw_ask=f"{yes_book.best_ask:.4f}",
        )

        # Calculate mid price from TRADEABLE levels (not dust)
        mid = (yes_bid + yes_ask) / 2.0
        if mid == 0 or mid < 0.05 or mid > 0.95:
            logger.debug("Mid price out of range", mid=mid)
            return None

        spread_bps = self.settings.mm_spread_bps
        spread = spread_bps / 10000

        bid_price = mid * (1 - spread)
        ask_price = mid * (1 + spread)

        # Ensure prices are valid
        bid_price = max(0.01, min(0.99, bid_price))
        ask_price = max(0.01, min(0.99, ask_price))

        # Validate spread after clamping - bid must be < ask
        if bid_price >= ask_price:
            logger.warning(
                "Invalid spread after clamping",
                bid=bid_price, ask=ask_price, mid=mid
            )
            return None

        # Get risk-adjusted sizes
        yes_size = self.risk_manager.get_adjusted_size(
            market_id, "YES", self.settings.mm_default_size
        )
        no_size = self.risk_manager.get_adjusted_size(
            market_id, "NO", self.settings.mm_default_size
        )

        yes_quote = None
        no_quote = None

        # Generate YES quote (buy at bid)
        if yes_size > 0:
            validation = self.risk_manager.validate_order(
                market_id, "YES", yes_size * bid_price
            )
            if validation:
                yes_quote = Quote(
                    market_id=market_id,
                    token_id=yes_token,
                    side="BUY",
                    price=bid_price,
                    size=yes_size,
                    outcome="YES"
                )

        # Generate NO quote (buy at 1 - ask = bid for NO)
        if no_size > 0:
            no_price = 1 - ask_price
            validation = self.risk_manager.validate_order(
                market_id, "NO", no_size * no_price
            )
            if validation:
                no_quote = Quote(
                    market_id=market_id,
                    token_id=no_token,
                    side="BUY",
                    price=no_price,
                    size=no_size,
                    outcome="NO"
                )

        return MMSignal(
            market_id=market_id,
            yes_quote=yes_quote,
            no_quote=no_quote
        )

    async def execute(self, signal: MMSignal) -> bool:
        """Execute market making signal by placing quotes."""
        # In real implementation, this would:
        # 1. Cancel stale orders
        # 2. Place new quotes via OrderManager

        orders_placed = []
        simulator = get_simulator()

        if signal.yes_quote:
            logger.info(
                "MM quote generated",
                market_id=signal.market_id,
                outcome="YES",
                side=signal.yes_quote.side,
                price=f"{signal.yes_quote.price:.4f}",
                size=signal.yes_quote.size,
            )
            orders_placed.append(signal.yes_quote)

            # Record simulated fill in dry run mode
            if self.settings.dry_run:
                yes_book = self.orderbooks.get_or_create(signal.yes_quote.token_id)
                simulator.record_mm_fill(
                    market_id=signal.market_id,
                    token_id=signal.yes_quote.token_id,
                    outcome="YES",
                    price=signal.yes_quote.price,
                    size=signal.yes_quote.size,
                    current_market_price=yes_book.best_ask,
                )

        if signal.no_quote:
            logger.info(
                "MM quote generated",
                market_id=signal.market_id,
                outcome="NO",
                side=signal.no_quote.side,
                price=f"{signal.no_quote.price:.4f}",
                size=signal.no_quote.size,
            )
            orders_placed.append(signal.no_quote)

            # Record simulated fill in dry run mode
            if self.settings.dry_run:
                no_book = self.orderbooks.get_or_create(signal.no_quote.token_id)
                simulator.record_mm_fill(
                    market_id=signal.market_id,
                    token_id=signal.no_quote.token_id,
                    outcome="NO",
                    price=signal.no_quote.price,
                    size=signal.no_quote.size,
                    current_market_price=no_book.best_ask,
                )

        return len(orders_placed) > 0

    async def cleanup(self) -> None:
        """Cancel all open orders."""
        for market_id, order_ids in self._open_orders.items():
            if order_ids:
                logger.info("Cancelling MM orders", market_id=market_id, count=len(order_ids))
        self._open_orders.clear()

    def get_stats(self) -> dict:
        """Get market making statistics."""
        return {
            "markets": list(self._markets.keys()),
            "enabled": self.enabled and self.settings.mm_enabled,
            "spread_bps": self.settings.mm_spread_bps,
            "default_size": self.settings.mm_default_size,
        }
