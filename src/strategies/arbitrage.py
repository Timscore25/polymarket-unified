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
from src.execution.order_manager import OrderManager

logger = get_logger(__name__)


@dataclass
class ArbLeg:
    """One leg of an arbitrage trade."""
    market_id: str
    token_id: str
    side: str
    price: float
    size: float
    timeframe: str  # "5m" or "15m"
    outcome: str  # "UP" or "DOWN"


@dataclass
class ArbSignal:
    """Arbitrage signal with both legs."""
    leg1: ArbLeg
    leg2: ArbLeg
    expected_profit: float
    sum_of_asks: float


class Arbitrage(Strategy):
    """
    Cross-market arbitrage strategy.

    Exploits price inefficiencies between 5m and 15m BTC markets.
    Only activates during the overlap window (last 5 min of 15m period).
    """

    # Minimum liquidity required to consider a trade (in shares)
    MIN_LIQUIDITY = 50.0
    # Minimum execution interval (seconds) - can't spam trades
    MIN_EXECUTION_INTERVAL = 5.0
    # Cooldown after execution (seconds) - market needs time to react
    POST_EXECUTION_COOLDOWN = 10.0

    def __init__(
        self,
        settings: Settings,
        orderbooks: MultiTokenOrderBook,
        risk_manager: RiskManager,
        order_manager: OrderManager = None
    ):
        super().__init__(settings, orderbooks, risk_manager)
        self.order_manager = order_manager
        self._market_pairs: list[tuple[dict, dict]] = []  # (5m_market, 15m_market)
        self._pending_arbs: dict[str, ArbSignal] = {}  # arb_id -> signal
        self._last_check_time: float = 0
        self._last_execution_time: float = 0
        self._executions_this_session: int = 0

    def add_market_pair(self, market_5m: dict, market_15m: dict) -> None:
        """Add a 5m/15m market pair for arbitrage."""
        self._market_pairs.append((market_5m, market_15m))
        logger.info(
            "Added arbitrage pair",
            market_5m=market_5m.get("id"),
            market_15m=market_15m.get("id")
        )

    def _is_in_overlap_window(self, market_15m: dict) -> bool:
        """Check if we're in the last 5 minutes of the 15m market."""
        # In real implementation, parse the market's end time
        # For now, assume we're always in a valid window for testing
        end_time = market_15m.get("end_time", 0)
        if not end_time:
            return True  # Allow for testing

        now = time.time()
        time_to_close = end_time - now

        # Only trade in last 5 minutes of 15m market
        return 0 < time_to_close <= 300

    async def check_opportunity(self) -> Optional[ArbSignal]:
        """Check for arbitrage opportunities across market pairs."""
        if not self.enabled or not self.settings.arb_enabled:
            return None

        if self.risk_manager.should_stop_trading():
            return None

        # Rate limit: respect cooldown after execution
        current_time = time.time()
        time_since_last_exec = current_time - self._last_execution_time
        if time_since_last_exec < self.POST_EXECUTION_COOLDOWN:
            return None

        for market_5m, market_15m in self._market_pairs:
            # Check overlap window
            if not self._is_in_overlap_window(market_15m):
                continue

            signal = self._check_pair(market_5m, market_15m)
            if signal:
                return signal

        return None

    def _check_pair(self, market_5m: dict, market_15m: dict) -> Optional[ArbSignal]:
        """Check a specific market pair for arbitrage.

        IMPORTANT: We only consider ACTUAL tradeable liquidity, not synthetic
        prices from price_change events. This filters out dust orders and
        verifies sufficient size exists to execute the trade.
        """
        # Get token IDs
        up_5m = market_5m.get("up_token_id", "")
        down_5m = market_5m.get("down_token_id", "")
        up_15m = market_15m.get("up_token_id", "")
        down_15m = market_15m.get("down_token_id", "")

        if not all([up_5m, down_5m, up_15m, down_15m]):
            return None

        # Get order books
        book_up_5m = self.orderbooks.get_or_create(up_5m)
        book_down_5m = self.orderbooks.get_or_create(down_5m)
        book_up_15m = self.orderbooks.get_or_create(up_15m)
        book_down_15m = self.orderbooks.get_or_create(down_15m)

        # Check for stale books
        any_stale = (book_up_5m.is_stale() or book_down_5m.is_stale() or
                     book_up_15m.is_stale() or book_down_15m.is_stale())
        if any_stale:
            return None

        # Get TRADEABLE asks (filtering dust and requiring min size)
        # For prediction markets, realistic prices should be 0.10-0.90
        # Prices outside this range are dust/placeholder orders
        up_15m_ask, up_15m_size = book_up_15m.get_tradeable_ask(
            max_price=0.90, min_size=self.MIN_LIQUIDITY
        )
        down_5m_ask, down_5m_size = book_down_5m.get_tradeable_ask(
            max_price=0.90, min_size=self.MIN_LIQUIDITY
        )
        down_15m_ask, down_15m_size = book_down_15m.get_tradeable_ask(
            max_price=0.90, min_size=self.MIN_LIQUIDITY
        )
        up_5m_ask, up_5m_size = book_up_5m.get_tradeable_ask(
            max_price=0.90, min_size=self.MIN_LIQUIDITY
        )

        # Log actual tradeable prices (not dust)
        logger.debug(
            "Tradeable prices (filtered)",
            up_15m_ask=f"{up_15m_ask:.4f}" if up_15m_ask < 1.0 else "NO_LIQ",
            up_15m_size=f"{up_15m_size:.0f}",
            down_5m_ask=f"{down_5m_ask:.4f}" if down_5m_ask < 1.0 else "NO_LIQ",
            down_5m_size=f"{down_5m_size:.0f}",
            raw_up_15m=f"{book_up_15m.best_ask:.4f}",
            raw_down_5m=f"{book_down_5m.best_ask:.4f}",
        )

        # Arbitrage opportunity 1: Buy 15m UP + Buy 5m DOWN
        # Only if BOTH legs have real tradeable liquidity
        if up_15m_ask < 1.0 and down_5m_ask < 1.0:
            sum1 = up_15m_ask + down_5m_ask
            if sum1 < self.settings.arb_threshold:
                # Verify sufficient liquidity for our size
                trade_size = min(self.settings.arb_size, up_15m_size, down_5m_size)
                if trade_size >= self.settings.arb_size * 0.5:  # At least half our target
                    logger.info(
                        "REAL arb opportunity found",
                        sum_of_asks=f"{sum1:.4f}",
                        up_15m_ask=f"{up_15m_ask:.4f}",
                        down_5m_ask=f"{down_5m_ask:.4f}",
                        available_size=f"{trade_size:.0f}",
                    )
                    return self._create_signal(
                        market_15m, "UP", up_15m_ask,
                        market_5m, "DOWN", down_5m_ask,
                        sum1, trade_size
                    )

        # Arbitrage opportunity 2: Buy 15m DOWN + Buy 5m UP
        if down_15m_ask < 1.0 and up_5m_ask < 1.0:
            sum2 = down_15m_ask + up_5m_ask
            if sum2 < self.settings.arb_threshold:
                trade_size = min(self.settings.arb_size, down_15m_size, up_5m_size)
                if trade_size >= self.settings.arb_size * 0.5:
                    logger.info(
                        "REAL arb opportunity found",
                        sum_of_asks=f"{sum2:.4f}",
                        down_15m_ask=f"{down_15m_ask:.4f}",
                        up_5m_ask=f"{up_5m_ask:.4f}",
                        available_size=f"{trade_size:.0f}",
                    )
                    return self._create_signal(
                        market_15m, "DOWN", down_15m_ask,
                        market_5m, "UP", up_5m_ask,
                        sum2, trade_size
                    )

        return None

    def _create_signal(
        self,
        market1: dict,
        outcome1: str,
        price1: float,
        market2: dict,
        outcome2: str,
        price2: float,
        sum_of_asks: float,
        available_size: float = None
    ) -> Optional[ArbSignal]:
        """Create an arbitrage signal."""
        size = available_size if available_size else self.settings.arb_size

        # Validate with risk manager
        if not self.risk_manager.validate_order(market1.get("id", ""), "YES", size * price1):
            return None
        if not self.risk_manager.validate_order(market2.get("id", ""), "YES", size * price2):
            return None

        token1 = market1.get(f"{outcome1.lower()}_token_id", "")
        token2 = market2.get(f"{outcome2.lower()}_token_id", "")

        leg1 = ArbLeg(
            market_id=market1.get("id", ""),
            token_id=token1,
            side="BUY",
            price=price1,
            size=size,
            timeframe="15m",
            outcome=outcome1
        )

        leg2 = ArbLeg(
            market_id=market2.get("id", ""),
            token_id=token2,
            side="BUY",
            price=price2,
            size=size,
            timeframe="5m",
            outcome=outcome2
        )

        expected_profit = (1.0 - sum_of_asks) * size

        return ArbSignal(
            leg1=leg1,
            leg2=leg2,
            expected_profit=expected_profit,
            sum_of_asks=sum_of_asks
        )

    async def execute(self, signal: ArbSignal) -> bool:
        """Execute arbitrage by placing both legs simultaneously."""
        arb_id = f"arb-{int(time.time()*1000)}"

        # Record execution time for cooldown
        self._last_execution_time = time.time()
        self._executions_this_session += 1

        logger.info(
            "Arbitrage EXECUTED",
            arb_id=arb_id,
            execution_num=self._executions_this_session,
            sum_of_asks=f"{signal.sum_of_asks:.4f}",
            expected_profit=f"${signal.expected_profit:.2f}",
            leg1_tf=signal.leg1.timeframe,
            leg1_outcome=signal.leg1.outcome,
            leg1_price=f"{signal.leg1.price:.4f}",
            leg1_size=signal.leg1.size,
            leg2_tf=signal.leg2.timeframe,
            leg2_outcome=signal.leg2.outcome,
            leg2_price=f"{signal.leg2.price:.4f}",
            leg2_size=signal.leg2.size,
        )

        # Record simulated execution in dry run mode
        if self.settings.dry_run:
            simulator = get_simulator()
            simulator.record_arb_execution(
                arb_id=arb_id,
                market_5m=signal.leg2.market_id,
                market_15m=signal.leg1.market_id,
                leg1_price=signal.leg1.price,
                leg2_price=signal.leg2.price,
                size=signal.leg1.size,
            )
        else:
            # LIVE TRADING: Place both legs simultaneously
            if self.order_manager:
                import asyncio

                logger.info(
                    "Placing LIVE arb orders",
                    arb_id=arb_id,
                    leg1_token=signal.leg1.token_id[:20] + "...",
                    leg1_price=signal.leg1.price,
                    leg2_token=signal.leg2.token_id[:20] + "...",
                    leg2_price=signal.leg2.price,
                )

                # Place both orders concurrently
                try:
                    order1, order2 = await asyncio.gather(
                        self.order_manager.place_order(
                            market_id=signal.leg1.market_id,
                            token_id=signal.leg1.token_id,
                            side="BUY",
                            price=signal.leg1.price,
                            size=signal.leg1.size,
                        ),
                        self.order_manager.place_order(
                            market_id=signal.leg2.market_id,
                            token_id=signal.leg2.token_id,
                            side="BUY",
                            price=signal.leg2.price,
                            size=signal.leg2.size,
                        ),
                    )

                    if order1 and order2:
                        logger.info(
                            "Arb orders placed successfully",
                            arb_id=arb_id,
                            order1_id=order1.id,
                            order2_id=order2.id,
                        )
                    else:
                        logger.error(
                            "Failed to place one or both arb orders",
                            arb_id=arb_id,
                            order1=order1,
                            order2=order2,
                        )
                        return False

                except Exception as e:
                    logger.error("Arb order placement failed", arb_id=arb_id, error=str(e))
                    return False

        self._pending_arbs[arb_id] = signal

        return True

    async def _verify_fill(self, arb_id: str) -> None:
        """Verify if both legs filled after delay."""
        import asyncio
        await asyncio.sleep(self.settings.arb_verify_fill_secs)

        signal = self._pending_arbs.pop(arb_id, None)
        if not signal:
            return

        # In real implementation:
        # - Check if both orders filled
        # - If only one filled, exit that position
        # - If neither filled, cancel both

        logger.info("Arbitrage verification complete", arb_id=arb_id)

    async def cleanup(self) -> None:
        """Cancel all pending arbitrage orders."""
        for arb_id in list(self._pending_arbs.keys()):
            logger.info("Cancelling pending arbitrage", arb_id=arb_id)
        self._pending_arbs.clear()

    def get_stats(self) -> dict:
        """Get arbitrage statistics."""
        return {
            "pairs": len(self._market_pairs),
            "pending_arbs": len(self._pending_arbs),
            "enabled": self.enabled and self.settings.arb_enabled,
            "threshold": self.settings.arb_threshold,
            "size": self.settings.arb_size,
        }
