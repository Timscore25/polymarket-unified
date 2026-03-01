from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from src.config import Settings
from src.core.orderbook import MultiTokenOrderBook
from src.risk.manager import RiskManager


class Strategy(ABC):
    """Base class for trading strategies."""

    def __init__(
        self,
        settings: Settings,
        orderbooks: MultiTokenOrderBook,
        risk_manager: RiskManager
    ):
        self.settings = settings
        self.orderbooks = orderbooks
        self.risk_manager = risk_manager
        self._enabled = True

    @property
    def name(self) -> str:
        """Strategy name."""
        return self.__class__.__name__

    @property
    def enabled(self) -> bool:
        """Check if strategy is enabled."""
        return self._enabled

    def enable(self) -> None:
        """Enable the strategy."""
        self._enabled = True

    def disable(self) -> None:
        """Disable the strategy."""
        self._enabled = False

    @abstractmethod
    async def check_opportunity(self) -> Optional[Any]:
        """
        Check for trading opportunity.

        Returns:
            Signal object if opportunity found, None otherwise
        """
        pass

    @abstractmethod
    async def execute(self, signal: Any) -> bool:
        """
        Execute on a signal.

        Args:
            signal: The signal from check_opportunity

        Returns:
            True if execution was successful
        """
        pass

    @abstractmethod
    async def cleanup(self) -> None:
        """Clean up any open orders or state."""
        pass
