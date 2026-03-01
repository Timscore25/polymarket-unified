from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from src.config import Settings
from src.risk.inventory import InventoryManager
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ValidationResult:
    """Result of order validation."""
    is_valid: bool
    reason: str = "OK"

    def __bool__(self) -> bool:
        return self.is_valid


class RiskManager:
    """
    3-layer risk management system.

    Layer 1: Position size limits
    Layer 2: Exposure limits
    Layer 3: Inventory skew limits
    """

    def __init__(self, settings: Settings, inventory_manager: InventoryManager):
        self.settings = settings
        self.inventory = inventory_manager

    def validate_order(
        self,
        market_id: str,
        side: str,
        size_usd: float
    ) -> ValidationResult:
        """
        Validate an order against all risk checks.

        Args:
            market_id: Market identifier
            side: "YES" or "NO"
            size_usd: Order size in USD

        Returns:
            ValidationResult with is_valid flag and reason
        """
        # Layer 1: Position size limit
        result = self._check_position_size(size_usd)
        if not result:
            return result

        # Layer 2: Exposure limits
        result = self._check_exposure_limits(side, size_usd)
        if not result:
            return result

        # Layer 3: Inventory skew
        result = self._check_inventory_skew(market_id)
        if not result:
            return result

        return ValidationResult(True)

    def _check_position_size(self, size_usd: float) -> ValidationResult:
        """Layer 1: Check position size limit."""
        if size_usd > self.settings.max_position_size_usd:
            logger.warning(
                "Position size exceeded",
                size=size_usd,
                limit=self.settings.max_position_size_usd
            )
            return ValidationResult(
                False,
                f"Position size ${size_usd:.2f} exceeds limit ${self.settings.max_position_size_usd:.2f}"
            )
        return ValidationResult(True)

    def _check_exposure_limits(self, side: str, size_usd: float) -> ValidationResult:
        """Layer 2: Check exposure limits."""
        current_exposure = self.inventory.total_exposure()

        if side.upper() == "YES":
            new_exposure = current_exposure + size_usd
            if new_exposure > self.settings.max_exposure_usd:
                logger.warning(
                    "Exposure limit exceeded",
                    current=current_exposure,
                    proposed=new_exposure,
                    limit=self.settings.max_exposure_usd
                )
                return ValidationResult(
                    False,
                    f"Would exceed max exposure ${self.settings.max_exposure_usd:.2f}"
                )
        else:
            new_exposure = current_exposure - size_usd
            if new_exposure < self.settings.min_exposure_usd:
                logger.warning(
                    "Exposure limit exceeded",
                    current=current_exposure,
                    proposed=new_exposure,
                    limit=self.settings.min_exposure_usd
                )
                return ValidationResult(
                    False,
                    f"Would exceed min exposure ${self.settings.min_exposure_usd:.2f}"
                )

        return ValidationResult(True)

    def _check_inventory_skew(self, market_id: str) -> ValidationResult:
        """Layer 3: Check inventory skew."""
        inventory = self.inventory.get_or_create(market_id)
        skew = inventory.skew

        if skew > self.settings.max_skew:
            logger.warning(
                "Inventory skew exceeded",
                skew=skew,
                limit=self.settings.max_skew,
                market_id=market_id
            )
            return ValidationResult(
                False,
                f"Inventory skew {skew:.2%} exceeds limit {self.settings.max_skew:.2%}"
            )

        return ValidationResult(True)

    def should_reduce_exposure(self, market_id: str) -> bool:
        """Check if we should reduce exposure in this market."""
        inventory = self.inventory.get_or_create(market_id)
        return inventory.skew > self.settings.max_skew * 0.8

    def should_stop_trading(self) -> bool:
        """Check if we should stop trading entirely."""
        exposure = abs(self.inventory.total_exposure())
        max_exposure = abs(self.settings.max_exposure_usd)

        if exposure > max_exposure * 0.95:
            logger.warning(
                "Near exposure limit - stopping trading",
                exposure=exposure,
                max=max_exposure
            )
            return True

        return False

    def get_adjusted_size(
        self,
        market_id: str,
        side: str,
        base_size: float
    ) -> float:
        """
        Get risk-adjusted order size.

        Reduces size based on:
        - Current exposure
        - Inventory skew
        - Proximity to limits
        """
        # Start with inventory manager's available size
        size = self.inventory.get_available_size(market_id, side, base_size)

        # Further reduce if near limits
        exposure_ratio = abs(self.inventory.total_exposure()) / abs(self.settings.max_exposure_usd)

        if exposure_ratio > 0.8:
            # Scale down linearly as we approach limit
            scale = 1.0 - (exposure_ratio - 0.8) / 0.2
            size *= max(0.1, scale)

        # Apply position size limit
        size = min(size, self.settings.max_position_size_usd)

        return max(0, size)

    def get_risk_metrics(self) -> dict:
        """Get current risk metrics for monitoring."""
        return {
            "total_exposure": self.inventory.total_exposure(),
            "max_exposure": self.settings.max_exposure_usd,
            "exposure_utilization": abs(self.inventory.total_exposure()) / abs(self.settings.max_exposure_usd),
            "total_realized_pnl": self.inventory.total_realized_pnl(),
            "positions": {
                market_id: {
                    "yes_size": inv.yes_position.size,
                    "no_size": inv.no_position.size,
                    "net_exposure": inv.net_exposure_usd,
                    "skew": inv.skew,
                }
                for market_id, inv in self.inventory.get_all_positions().items()
            }
        }
