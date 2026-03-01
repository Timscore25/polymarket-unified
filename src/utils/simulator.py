"""Simulated P&L tracking for dry run mode."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class SimulatedTrade:
    """Record of a simulated trade."""
    timestamp: float
    market_id: str
    token_id: str
    side: str  # "BUY" or "SELL"
    outcome: str  # "YES" or "NO"
    price: float
    size: float
    trade_type: str  # "MM" or "ARB"


@dataclass
class SimulatedPosition:
    """Simulated position in a token."""
    token_id: str
    outcome: str
    size: float = 0.0
    avg_price: float = 0.0
    cost_basis: float = 0.0

    def add(self, size: float, price: float) -> None:
        """Add to position."""
        new_cost = size * price
        self.cost_basis += new_cost
        self.size += size
        if self.size > 0:
            self.avg_price = self.cost_basis / self.size


@dataclass
class PnLReport:
    """P&L summary report."""
    total_trades: int
    mm_trades: int
    arb_trades: int
    mm_spread_captured: float  # From completed MM round-trips
    arb_profit: float  # From completed arb trades
    total_realized_pnl: float
    total_cost: float  # Money spent on positions
    positions_value: float  # Current value of positions
    unrealized_pnl: float
    total_pnl: float
    runtime_seconds: float
    pnl_per_minute: float


class SimulatedTracker:
    """
    Tracks simulated trades and calculates P&L for dry run mode.

    For Market Making:
    - When both YES and NO quotes fill, we capture the spread
    - Spread profit = 1.0 - (yes_price + no_price)

    For Arbitrage:
    - When both legs execute, we lock in the price difference
    - Arb profit = 1.0 - (leg1_ask + leg2_ask)
    """

    def __init__(self):
        self.trades: list[SimulatedTrade] = []
        self.positions: dict[str, SimulatedPosition] = {}  # token_id -> position
        self.mm_round_trips: list[dict] = []  # Completed MM trades (both sides filled)
        self.arb_completions: list[dict] = []  # Completed arb trades
        self.start_time = time.time()

        # Pending MM quotes waiting for match
        self._pending_mm: dict[str, dict] = {}  # market_id -> {yes: quote, no: quote}

        # Running totals
        self.total_cost = 0.0
        self.mm_spread_captured = 0.0
        self.arb_profit = 0.0

    def record_mm_fill(
        self,
        market_id: str,
        token_id: str,
        outcome: str,
        price: float,
        size: float,
        current_market_price: float,
    ) -> Optional[float]:
        """
        Record a simulated MM fill.

        Simulates fill if market price would execute our quote:
        - BUY fills if market ask <= our bid

        Returns spread captured if both sides of MM filled, None otherwise.
        """
        # Check if our quote would fill
        # For BUY orders, we fill if market price is at or below our price
        # In reality we'd check the order book, but for simulation we use a simple model

        # Record the trade
        trade = SimulatedTrade(
            timestamp=time.time(),
            market_id=market_id,
            token_id=token_id,
            side="BUY",
            outcome=outcome,
            price=price,
            size=size,
            trade_type="MM",
        )
        self.trades.append(trade)

        # Update position
        if token_id not in self.positions:
            self.positions[token_id] = SimulatedPosition(token_id=token_id, outcome=outcome)
        self.positions[token_id].add(size, price)
        self.total_cost += size * price

        # Track pending MM for this market
        if market_id not in self._pending_mm:
            self._pending_mm[market_id] = {}

        self._pending_mm[market_id][outcome] = {
            "price": price,
            "size": size,
            "timestamp": time.time(),
        }

        # Check if we have both sides - complete round trip
        pending = self._pending_mm[market_id]
        if "YES" in pending and "NO" in pending:
            yes_price = pending["YES"]["price"]
            no_price = pending["NO"]["price"]
            min_size = min(pending["YES"]["size"], pending["NO"]["size"])

            # Spread captured = what we'd pay vs guaranteed $1 payout
            # We buy YES at yes_price, NO at no_price
            # One will pay $1, total cost = yes_price + no_price
            # Profit per share = 1.0 - (yes_price + no_price)
            spread = 1.0 - (yes_price + no_price)
            profit = spread * min_size

            if profit > 0:
                self.mm_spread_captured += profit
                self.mm_round_trips.append({
                    "market_id": market_id,
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "size": min_size,
                    "spread": spread,
                    "profit": profit,
                    "timestamp": time.time(),
                })

                logger.info(
                    "MM round-trip completed",
                    market_id=market_id,
                    yes_price=f"{yes_price:.4f}",
                    no_price=f"{no_price:.4f}",
                    spread_bps=f"{spread * 10000:.1f}",
                    profit=f"${profit:.4f}",
                )

            # Clear pending
            self._pending_mm[market_id] = {}
            return profit

        return None

    def record_arb_execution(
        self,
        arb_id: str,
        market_5m: str,
        market_15m: str,
        leg1_price: float,
        leg2_price: float,
        size: float,
    ) -> float:
        """
        Record a simulated arbitrage execution.

        Returns profit from the arb.
        """
        # Arb profit = 1.0 - sum of both legs
        total_cost = leg1_price + leg2_price
        profit_per_share = 1.0 - total_cost
        profit = profit_per_share * size

        self.arb_profit += profit
        self.total_cost += total_cost * size

        self.arb_completions.append({
            "arb_id": arb_id,
            "market_5m": market_5m,
            "market_15m": market_15m,
            "leg1_price": leg1_price,
            "leg2_price": leg2_price,
            "size": size,
            "profit": profit,
            "timestamp": time.time(),
        })

        logger.info(
            "Arb execution simulated",
            arb_id=arb_id,
            leg1=f"{leg1_price:.4f}",
            leg2=f"{leg2_price:.4f}",
            total_cost=f"{total_cost:.4f}",
            profit=f"${profit:.4f}",
        )

        return profit

    def get_report(self) -> PnLReport:
        """Generate P&L report."""
        runtime = time.time() - self.start_time

        # Calculate position values (assume current price = avg price for simplicity)
        positions_value = sum(p.size * p.avg_price for p in self.positions.values())

        total_realized = self.mm_spread_captured + self.arb_profit
        unrealized = positions_value - self.total_cost  # Simplified
        total_pnl = total_realized

        pnl_per_minute = (total_pnl / runtime * 60) if runtime > 0 else 0

        return PnLReport(
            total_trades=len(self.trades),
            mm_trades=len([t for t in self.trades if t.trade_type == "MM"]),
            arb_trades=len(self.arb_completions) * 2,  # 2 legs per arb
            mm_spread_captured=self.mm_spread_captured,
            arb_profit=self.arb_profit,
            total_realized_pnl=total_realized,
            total_cost=self.total_cost,
            positions_value=positions_value,
            unrealized_pnl=unrealized,
            total_pnl=total_pnl,
            runtime_seconds=runtime,
            pnl_per_minute=pnl_per_minute,
        )

    def print_report(self) -> None:
        """Print formatted P&L report."""
        report = self.get_report()

        logger.info(
            "=== SIMULATED P&L REPORT ===",
            runtime=f"{report.runtime_seconds:.1f}s",
            total_trades=report.total_trades,
        )
        logger.info(
            "Market Making",
            round_trips=len(self.mm_round_trips),
            spread_captured=f"${report.mm_spread_captured:.4f}",
        )
        logger.info(
            "Arbitrage",
            executions=len(self.arb_completions),
            profit=f"${report.arb_profit:.4f}",
        )
        logger.info(
            "TOTAL P&L",
            realized=f"${report.total_realized_pnl:.4f}",
            pnl_per_minute=f"${report.pnl_per_minute:.4f}/min",
            projected_hourly=f"${report.pnl_per_minute * 60:.2f}/hr",
        )


# Global simulator instance for dry run
_simulator: Optional[SimulatedTracker] = None


def get_simulator() -> SimulatedTracker:
    """Get or create the global simulator instance."""
    global _simulator
    if _simulator is None:
        _simulator = SimulatedTracker()
    return _simulator


def reset_simulator() -> SimulatedTracker:
    """Reset and return a new simulator instance."""
    global _simulator
    _simulator = SimulatedTracker()
    return _simulator
