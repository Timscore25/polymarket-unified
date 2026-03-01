from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Position:
    """Position in a single token."""
    token_id: str
    size: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0

    def add(self, size: float, price: float) -> None:
        """Add to position."""
        if self.size == 0:
            self.avg_price = price
            self.size = size
        else:
            # Weighted average price
            total_cost = (self.size * self.avg_price) + (size * price)
            self.size += size
            if self.size != 0:
                self.avg_price = total_cost / self.size

    def reduce(self, size: float, price: float) -> float:
        """Reduce position and return realized PnL."""
        if size > abs(self.size):
            size = abs(self.size)

        # Calculate PnL based on position direction
        # Long (size > 0): profit when sell price > avg_price
        # Short (size < 0): profit when buy-back price < avg_price
        if self.size > 0:
            pnl = size * (price - self.avg_price)
            self.size -= size
        else:
            pnl = size * (self.avg_price - price)
            self.size += size  # Add because size is positive, self.size is negative

        self.realized_pnl += pnl

        if abs(self.size) < 0.0001:
            self.size = 0
            self.avg_price = 0

        return pnl

    @property
    def unrealized_pnl(self) -> float:
        """Calculate unrealized PnL (requires current price)."""
        return 0.0  # Need current price to calculate

    def get_unrealized_pnl(self, current_price: float) -> float:
        """Calculate unrealized PnL with current price."""
        return self.size * (current_price - self.avg_price)


@dataclass
class Inventory:
    """Inventory tracking for a market (YES/NO pair)."""
    market_id: str
    yes_position: Position = field(default_factory=lambda: Position(""))
    no_position: Position = field(default_factory=lambda: Position(""))

    def __post_init__(self):
        if not self.yes_position.token_id:
            self.yes_position = Position(f"{self.market_id}_yes")
        if not self.no_position.token_id:
            self.no_position = Position(f"{self.market_id}_no")

    @property
    def net_exposure_usd(self) -> float:
        """Net exposure: positive = long YES, negative = long NO."""
        return self.yes_position.size - self.no_position.size

    @property
    def total_size(self) -> float:
        """Total position size."""
        return abs(self.yes_position.size) + abs(self.no_position.size)

    @property
    def skew(self) -> float:
        """Position skew: 0 = balanced, 1 = fully one-sided."""
        total = self.total_size
        if total == 0:
            return 0.0
        return abs(self.net_exposure_usd) / total

    def is_balanced(self, max_skew: float = 0.3) -> bool:
        """Check if inventory is balanced."""
        return self.skew <= max_skew

    @property
    def total_realized_pnl(self) -> float:
        """Total realized PnL."""
        return self.yes_position.realized_pnl + self.no_position.realized_pnl


class InventoryManager:
    """Manages inventory across multiple markets."""

    def __init__(self, max_exposure_usd: float, min_exposure_usd: float):
        self.max_exposure_usd = max_exposure_usd
        self.min_exposure_usd = min_exposure_usd
        self._inventories: dict[str, Inventory] = {}

    def get_or_create(self, market_id: str) -> Inventory:
        """Get or create inventory for a market."""
        if market_id not in self._inventories:
            self._inventories[market_id] = Inventory(market_id=market_id)
        return self._inventories[market_id]

    def update_position(
        self,
        market_id: str,
        side: str,  # "YES" or "NO"
        delta: float,
        price: float
    ) -> None:
        """Update position after a fill."""
        inventory = self.get_or_create(market_id)

        if side.upper() == "YES":
            if delta > 0:
                inventory.yes_position.add(delta, price)
            else:
                inventory.yes_position.reduce(abs(delta), price)
        else:
            if delta > 0:
                inventory.no_position.add(delta, price)
            else:
                inventory.no_position.reduce(abs(delta), price)

        logger.debug(
            "Position updated",
            market_id=market_id,
            side=side,
            delta=delta,
            new_exposure=inventory.net_exposure_usd,
            skew=inventory.skew,
        )

    def total_exposure(self) -> float:
        """Get total exposure across all markets."""
        return sum(inv.net_exposure_usd for inv in self._inventories.values())

    def can_add_exposure(self, market_id: str, side: str, size_usd: float) -> bool:
        """Check if we can add exposure."""
        current = self.total_exposure()

        if side.upper() == "YES":
            new_exposure = current + size_usd
            return new_exposure <= self.max_exposure_usd
        else:
            new_exposure = current - size_usd
            return new_exposure >= self.min_exposure_usd

    def get_available_size(self, market_id: str, side: str, base_size: float) -> float:
        """Get available size considering exposure limits."""
        current = self.total_exposure()
        inventory = self.get_or_create(market_id)

        if side.upper() == "YES":
            max_available = self.max_exposure_usd - current
            # Reduce size if inventory is skewed toward YES
            if inventory.net_exposure_usd > 0:
                base_size *= 0.5
        else:
            max_available = abs(self.min_exposure_usd - current)
            # Reduce size if inventory is skewed toward NO
            if inventory.net_exposure_usd < 0:
                base_size *= 0.5

        return min(base_size, max(0, max_available))

    def get_all_positions(self) -> dict[str, Inventory]:
        """Get all inventories."""
        return self._inventories.copy()

    def total_realized_pnl(self) -> float:
        """Get total realized PnL across all markets."""
        return sum(inv.total_realized_pnl for inv in self._inventories.values())
