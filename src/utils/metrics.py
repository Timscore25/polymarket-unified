from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class MetricsCollector:
    """Collects and exposes trading metrics."""

    # Counters
    orders_placed: int = 0
    orders_filled: int = 0
    orders_cancelled: int = 0
    arb_opportunities: int = 0
    arb_executed: int = 0

    # Gauges
    current_exposure: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    # Timing
    last_quote_latency_ms: float = 0.0
    avg_quote_latency_ms: float = 0.0
    _latency_samples: list[float] = field(default_factory=list)

    # State
    start_time: float = field(default_factory=time.time)

    def record_order_placed(self) -> None:
        """Record an order placed."""
        self.orders_placed += 1

    def record_order_filled(self) -> None:
        """Record an order filled."""
        self.orders_filled += 1

    def record_order_cancelled(self) -> None:
        """Record an order cancelled."""
        self.orders_cancelled += 1

    def record_arb_opportunity(self) -> None:
        """Record an arbitrage opportunity detected."""
        self.arb_opportunities += 1

    def record_arb_executed(self) -> None:
        """Record an arbitrage executed."""
        self.arb_executed += 1

    def record_latency(self, latency_ms: float) -> None:
        """Record quote latency."""
        self.last_quote_latency_ms = latency_ms
        self._latency_samples.append(latency_ms)

        # Keep last 100 samples
        if len(self._latency_samples) > 100:
            self._latency_samples = self._latency_samples[-100:]

        self.avg_quote_latency_ms = sum(self._latency_samples) / len(self._latency_samples)

    def update_exposure(self, exposure: float) -> None:
        """Update current exposure."""
        self.current_exposure = exposure

    def update_pnl(self, realized: float, unrealized: float) -> None:
        """Update PnL metrics."""
        self.realized_pnl = realized
        self.unrealized_pnl = unrealized

    def get_metrics(self) -> dict:
        """Get all metrics as dict."""
        uptime = time.time() - self.start_time

        return {
            "uptime_seconds": uptime,
            "orders": {
                "placed": self.orders_placed,
                "filled": self.orders_filled,
                "cancelled": self.orders_cancelled,
                "fill_rate": self.orders_filled / max(1, self.orders_placed),
            },
            "arbitrage": {
                "opportunities": self.arb_opportunities,
                "executed": self.arb_executed,
                "execution_rate": self.arb_executed / max(1, self.arb_opportunities),
            },
            "pnl": {
                "realized": self.realized_pnl,
                "unrealized": self.unrealized_pnl,
                "total": self.realized_pnl + self.unrealized_pnl,
            },
            "exposure": self.current_exposure,
            "latency": {
                "last_ms": self.last_quote_latency_ms,
                "avg_ms": self.avg_quote_latency_ms,
            },
        }

    def to_prometheus(self) -> str:
        """Export metrics in Prometheus format."""
        lines = []

        lines.append(f"polymarket_orders_placed_total {self.orders_placed}")
        lines.append(f"polymarket_orders_filled_total {self.orders_filled}")
        lines.append(f"polymarket_orders_cancelled_total {self.orders_cancelled}")
        lines.append(f"polymarket_arb_opportunities_total {self.arb_opportunities}")
        lines.append(f"polymarket_arb_executed_total {self.arb_executed}")
        lines.append(f"polymarket_exposure_usd {self.current_exposure}")
        lines.append(f"polymarket_realized_pnl_usd {self.realized_pnl}")
        lines.append(f"polymarket_unrealized_pnl_usd {self.unrealized_pnl}")
        lines.append(f"polymarket_quote_latency_ms {self.last_quote_latency_ms}")
        lines.append(f"polymarket_uptime_seconds {time.time() - self.start_time}")

        return "\n".join(lines)
