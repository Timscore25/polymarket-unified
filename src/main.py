from __future__ import annotations

import asyncio
import signal
import time
from typing import Optional

from src.config import Settings, get_settings
from src.core.orderbook import MultiTokenOrderBook
from src.core.rest_client import RestClient
from src.core.websocket import WebSocketManager
from src.execution.order_manager import OrderManager
from src.risk.inventory import InventoryManager
from src.risk.manager import RiskManager
from src.strategies.market_maker import MarketMaker
from src.strategies.arbitrage import Arbitrage
from src.utils.logging import setup_logging, get_logger
from src.utils.metrics import MetricsCollector
from src.utils.simulator import get_simulator, reset_simulator

logger = get_logger(__name__)


class TradingSystem:
    """Main trading system orchestrator."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._running = False

        # Core components
        self.orderbooks = MultiTokenOrderBook()
        self.rest_client = RestClient(settings)
        self.ws_manager = WebSocketManager(settings, self.orderbooks)
        self.order_manager = OrderManager(settings)

        # Risk management
        self.inventory_manager = InventoryManager(
            settings.max_exposure_usd,
            settings.min_exposure_usd
        )
        self.risk_manager = RiskManager(settings, self.inventory_manager)

        # Strategies
        self.market_maker = MarketMaker(settings, self.orderbooks, self.risk_manager)
        self.arbitrage = Arbitrage(settings, self.orderbooks, self.risk_manager, self.order_manager)

        # Metrics
        self.metrics = MetricsCollector()

        # Market tracking
        self._markets: dict[str, dict] = {}  # timeframe -> market_info

    async def start(self) -> None:
        """Start the trading system."""
        self._running = True

        logger.info(
            "Starting trading system",
            dry_run=self.settings.dry_run,
            market_type=self.settings.market_type,
            timeframes=self.settings.timeframe_list,
        )

        if self.settings.dry_run:
            logger.warning("DRY RUN MODE - No real orders will be placed")
            logger.info("Simulated P&L tracking enabled")
            reset_simulator()  # Fresh simulator for this run

        # Discover markets
        await self._discover_markets()

        if not self._markets:
            logger.error("No markets found - exiting")
            return

        # Subscribe to WebSocket feeds
        await self._setup_websocket()

        # Start main loop
        await self._run_loop()

    async def _discover_markets(self) -> None:
        """Discover current markets for configured timeframes."""
        self._markets = await self.rest_client.discover_btc_markets(
            self.settings.timeframe_list
        )

        # Add markets to strategies
        for timeframe, market in self._markets.items():
            # Extract token IDs from clobTokenIds (JSON string format)
            import json
            clob_tokens_str = market.get("clobTokenIds", "[]")
            try:
                clob_tokens = json.loads(clob_tokens_str) if isinstance(clob_tokens_str, str) else clob_tokens_str
            except json.JSONDecodeError:
                clob_tokens = []

            if len(clob_tokens) >= 2:
                # First token is "Up" (YES), second is "Down" (NO)
                market["yes_token_id"] = clob_tokens[0]
                market["no_token_id"] = clob_tokens[1]
                market["up_token_id"] = clob_tokens[0]
                market["down_token_id"] = clob_tokens[1]
                logger.info("Token IDs extracted",
                           up_token=clob_tokens[0][:20] + "...",
                           down_token=clob_tokens[1][:20] + "...")

            # Add to market maker
            self.market_maker.add_market(market.get("id", ""), market)

            logger.info(
                f"Market configured",
                timeframe=timeframe,
                market_id=market.get("id"),
                question=market.get("question", "")[:50],
            )

        # Set up arbitrage pairs if we have both 5m and 15m
        if "5m" in self._markets and "15m" in self._markets:
            self.arbitrage.add_market_pair(
                self._markets["5m"],
                self._markets["15m"]
            )

    async def _setup_websocket(self) -> None:
        """Set up WebSocket subscriptions."""
        try:
            await self.ws_manager.connect()

            # Collect all token IDs to subscribe
            token_ids = []
            for market in self._markets.values():
                token_ids.append(market.get("yes_token_id", ""))
                token_ids.append(market.get("no_token_id", ""))

            token_ids = [t for t in token_ids if t]

            if token_ids:
                logger.info("Subscribing to tokens", count=len(token_ids))
                for tid in token_ids:
                    logger.info("Token ID", token=tid[:30] + "..." if len(tid) > 30 else tid)
                await self.ws_manager.subscribe(token_ids)
            else:
                logger.warning("No token IDs to subscribe to!")

            # Register event handlers
            self.ws_manager.on("book_update", self._on_book_update)
            self.ws_manager.on("trade", self._on_trade)

        except Exception as e:
            logger.error("WebSocket setup failed", error=str(e))

    async def _on_book_update(self, data: dict) -> None:
        """Handle order book updates."""
        # Order book is automatically updated by WebSocketManager
        pass

    async def _on_trade(self, data: dict) -> None:
        """Handle trade events (fills)."""
        # In real implementation, update inventory on fills
        pass

    async def _run_loop(self) -> None:
        """Main trading loop."""
        tick_interval = self.settings.tick_interval_ms / 1000

        # Start WebSocket listener in background
        ws_task = asyncio.create_task(self.ws_manager.listen())

        try:
            while self._running:
                tick_start = time.time()

                await self._tick()

                # Calculate sleep time
                elapsed = time.time() - tick_start
                sleep_time = max(0, tick_interval - elapsed)
                await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            logger.info("Trading loop cancelled")
        finally:
            # Properly cancel and await the WebSocket task
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass  # Expected when task is cancelled
            await self._cleanup()

    async def _tick(self) -> None:
        """Single tick of the trading loop."""
        try:
            # Periodic status log (every 10 seconds)
            tick_count = getattr(self, '_tick_count', 0) + 1
            self._tick_count = tick_count
            if tick_count % 100 == 0:  # Every ~10s at 100ms tick
                # Show order book status with TRADEABLE liquidity for each market
                for timeframe, market in self._markets.items():
                    yes_token = market.get("yes_token_id", "")
                    no_token = market.get("no_token_id", "")
                    if yes_token and no_token:
                        yes_book = self.orderbooks.get_or_create(yes_token)
                        no_book = self.orderbooks.get_or_create(no_token)

                        # Get TRADEABLE prices (filter dust at 0.10/0.90)
                        yes_bid, yes_bid_sz = yes_book.get_tradeable_bid(min_price=0.10, min_size=10)
                        yes_ask, yes_ask_sz = yes_book.get_tradeable_ask(max_price=0.90, min_size=10)
                        no_bid, no_bid_sz = no_book.get_tradeable_bid(min_price=0.10, min_size=10)
                        no_ask, no_ask_sz = no_book.get_tradeable_ask(max_price=0.90, min_size=10)

                        # Calculate tradeable spread
                        has_yes_liq = yes_bid > 0 and yes_ask < 1.0
                        has_no_liq = no_bid > 0 and no_ask < 1.0
                        spread_pct = ((yes_ask - yes_bid) / ((yes_ask + yes_bid) / 2) * 100) if has_yes_liq else 0

                        logger.info(
                            "LIQUIDITY STATUS",
                            timeframe=timeframe,
                            yes_bid=f"{yes_bid:.2f}" if yes_bid > 0 else "NONE",
                            yes_ask=f"{yes_ask:.2f}" if yes_ask < 1.0 else "NONE",
                            yes_spread=f"{spread_pct:.1f}%" if has_yes_liq else "N/A",
                            tradeable="YES" if has_yes_liq and spread_pct < 20 else "NO",
                            raw_bid=f"{yes_book.best_bid:.2f}",
                            raw_ask=f"{yes_book.best_ask:.2f}",
                        )

            # Print P&L report every 30 seconds in dry run mode
            if self.settings.dry_run and tick_count % 300 == 0:
                get_simulator().print_report()

            # 1. Check for arbitrage opportunities first (time-sensitive)
            if self.settings.arb_enabled:
                arb_signal = await self.arbitrage.check_opportunity()
                if arb_signal:
                    self.metrics.record_arb_opportunity()
                    if await self.arbitrage.execute(arb_signal):
                        self.metrics.record_arb_executed()
                    return  # Skip MM this tick if arb detected

            # 2. Run market maker
            if self.settings.mm_enabled:
                mm_signal = await self.market_maker.check_opportunity()
                if mm_signal:
                    await self.market_maker.execute(mm_signal)

            # 3. Update metrics
            self.metrics.update_exposure(self.inventory_manager.total_exposure())
            self.metrics.update_pnl(
                self.inventory_manager.total_realized_pnl(),
                0  # Would need current prices for unrealized
            )

        except Exception as e:
            logger.error("Tick error", error=str(e))

    async def _cleanup(self) -> None:
        """Clean up resources."""
        logger.info("Cleaning up...")

        # Print final P&L report in dry run mode
        if self.settings.dry_run:
            logger.info("=== FINAL SIMULATION RESULTS ===")
            get_simulator().print_report()

        await self.market_maker.cleanup()
        await self.arbitrage.cleanup()
        await self.ws_manager.close()
        await self.rest_client.close()

        logger.info("Cleanup complete")

    def stop(self) -> None:
        """Stop the trading system."""
        self._running = False
        logger.info("Stop signal received")

    def print_status(self) -> None:
        """Print current status."""
        metrics = self.metrics.get_metrics()
        risk = self.risk_manager.get_risk_metrics()

        logger.info(
            "Status",
            uptime=f"{metrics['uptime_seconds']:.0f}s",
            orders=metrics["orders"],
            arb=metrics["arbitrage"],
            pnl=metrics["pnl"],
            exposure=metrics["exposure"],
        )


async def main():
    """Entry point."""
    settings = get_settings()
    setup_logging(settings.log_level)

    system = TradingSystem(settings)

    # Set up signal handlers
    loop = asyncio.get_event_loop()

    def handle_signal():
        logger.info("Shutdown signal received")
        system.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            pass  # Windows doesn't support this

    try:
        await system.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    finally:
        logger.info("Exiting")


if __name__ == "__main__":
    asyncio.run(main())
