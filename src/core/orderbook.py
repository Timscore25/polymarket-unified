from __future__ import annotations

import numpy as np
from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class OrderBook:
    """High-performance order book using numpy arrays."""

    token_id: str
    max_levels: int = 50

    # Pre-allocated numpy arrays for speed
    bid_prices: np.ndarray = field(default_factory=lambda: np.zeros(50, dtype=np.float64))
    bid_sizes: np.ndarray = field(default_factory=lambda: np.zeros(50, dtype=np.float64))
    ask_prices: np.ndarray = field(default_factory=lambda: np.zeros(50, dtype=np.float64))
    ask_sizes: np.ndarray = field(default_factory=lambda: np.zeros(50, dtype=np.float64))

    bid_count: int = 0
    ask_count: int = 0
    last_update: float = 0.0

    def __post_init__(self):
        self.bid_prices = np.zeros(self.max_levels, dtype=np.float64)
        self.bid_sizes = np.zeros(self.max_levels, dtype=np.float64)
        self.ask_prices = np.zeros(self.max_levels, dtype=np.float64)
        self.ask_sizes = np.zeros(self.max_levels, dtype=np.float64)

    def update_from_snapshot(self, bids: list[list], asks: list[list]) -> None:
        """Update order book from full snapshot. Fast path using numpy."""
        self.bid_count = min(len(bids), self.max_levels)
        self.ask_count = min(len(asks), self.max_levels)

        # Clear arrays
        self.bid_prices.fill(0)
        self.bid_sizes.fill(0)
        self.ask_prices.fill(0)
        self.ask_sizes.fill(0)

        # Fill from snapshot
        for i, (price, size) in enumerate(bids[:self.max_levels]):
            self.bid_prices[i] = float(price)
            self.bid_sizes[i] = float(size)

        for i, (price, size) in enumerate(asks[:self.max_levels]):
            self.ask_prices[i] = float(price)
            self.ask_sizes[i] = float(size)

        self.last_update = time.time()

    def update_level(self, side: str, price: float, size: float) -> None:
        """Update single price level. Used for incremental updates."""
        if side == "buy":
            self._update_bid_level(price, size)
        else:
            self._update_ask_level(price, size)
        self.last_update = time.time()

    def _update_bid_level(self, price: float, size: float) -> None:
        """Update or insert bid level, maintaining sorted order (descending)."""
        if size == 0:
            # Remove level - find and shift
            indices = np.where(np.abs(self.bid_prices - price) < 1e-9)[0]
            if len(indices) > 0:
                idx = indices[0]
                # Shift elements left to fill the gap
                if idx < self.max_levels - 1:
                    self.bid_prices[idx:self.max_levels-1] = self.bid_prices[idx+1:self.max_levels]
                    self.bid_sizes[idx:self.max_levels-1] = self.bid_sizes[idx+1:self.max_levels]
                self.bid_prices[self.max_levels-1] = 0
                self.bid_sizes[self.max_levels-1] = 0
                self.bid_count = max(0, self.bid_count - 1)
            return

        # Find position for price (bids sorted descending)
        for i in range(self.max_levels):
            if abs(self.bid_prices[i] - price) < 1e-9:
                # Update existing level
                self.bid_sizes[i] = size
                return
            if self.bid_prices[i] < price or self.bid_prices[i] == 0:
                # Insert here - shift elements right first (if room)
                if i < self.max_levels - 1:
                    self.bid_prices[i+1:self.max_levels] = self.bid_prices[i:self.max_levels-1].copy()
                    self.bid_sizes[i+1:self.max_levels] = self.bid_sizes[i:self.max_levels-1].copy()
                self.bid_prices[i] = price
                self.bid_sizes[i] = size
                self.bid_count = min(self.bid_count + 1, self.max_levels)
                return

    def _update_ask_level(self, price: float, size: float) -> None:
        """Update or insert ask level, maintaining sorted order (ascending)."""
        if size == 0:
            # Remove level - find and shift
            indices = np.where(np.abs(self.ask_prices - price) < 1e-9)[0]
            if len(indices) > 0:
                idx = indices[0]
                # Shift elements left to fill the gap
                if idx < self.max_levels - 1:
                    self.ask_prices[idx:self.max_levels-1] = self.ask_prices[idx+1:self.max_levels]
                    self.ask_sizes[idx:self.max_levels-1] = self.ask_sizes[idx+1:self.max_levels]
                self.ask_prices[self.max_levels-1] = 0
                self.ask_sizes[self.max_levels-1] = 0
                self.ask_count = max(0, self.ask_count - 1)
            return

        # Find position for price (asks sorted ascending, 0 = empty slot)
        for i in range(self.max_levels):
            if abs(self.ask_prices[i] - price) < 1e-9:
                # Update existing level
                self.ask_sizes[i] = size
                return
            if self.ask_prices[i] == 0 or self.ask_prices[i] > price:
                # Insert here - shift elements right first (if room)
                if i < self.max_levels - 1:
                    self.ask_prices[i+1:self.max_levels] = self.ask_prices[i:self.max_levels-1].copy()
                    self.ask_sizes[i+1:self.max_levels] = self.ask_sizes[i:self.max_levels-1].copy()
                self.ask_prices[i] = price
                self.ask_sizes[i] = size
                self.ask_count = min(self.ask_count + 1, self.max_levels)
                return

    @property
    def best_bid(self) -> float:
        """Get best bid price. O(1)."""
        return self.bid_prices[0] if self.bid_count > 0 else 0.0

    @property
    def best_ask(self) -> float:
        """Get best ask price. O(1)."""
        return self.ask_prices[0] if self.ask_count > 0 else 1.0

    @property
    def best_bid_size(self) -> float:
        """Get best bid size. O(1)."""
        return self.bid_sizes[0] if self.bid_count > 0 else 0.0

    @property
    def best_ask_size(self) -> float:
        """Get best ask size. O(1)."""
        return self.ask_sizes[0] if self.ask_count > 0 else 0.0

    def get_tradeable_bid(self, min_price: float = 0.02, min_size: float = 10.0) -> tuple[float, float]:
        """Get best tradeable bid (filtering dust orders).

        Returns (price, size) tuple. Returns (0, 0) if no tradeable bid.
        Dust orders are those with price <= min_price or size < min_size.
        """
        for i in range(self.bid_count):
            price = self.bid_prices[i]
            size = self.bid_sizes[i]
            if price > min_price and size >= min_size:
                return (price, size)
        return (0.0, 0.0)

    def get_tradeable_ask(self, max_price: float = 0.98, min_size: float = 10.0) -> tuple[float, float]:
        """Get best tradeable ask (filtering dust orders).

        Returns (price, size) tuple. Returns (1.0, 0) if no tradeable ask.
        Dust orders are those with price >= max_price or size < min_size.
        """
        for i in range(self.ask_count):
            price = self.ask_prices[i]
            size = self.ask_sizes[i]
            if price > 0 and price < max_price and size >= min_size:
                return (price, size)
        return (1.0, 0.0)

    def get_available_liquidity(self, side: str, target_price: float, min_size: float = 10.0) -> float:
        """Get total available liquidity at or better than target price.

        For buys (asks): sum sizes where ask_price <= target_price
        For sells (bids): sum sizes where bid_price >= target_price
        """
        total = 0.0
        if side == "buy":
            for i in range(self.ask_count):
                if self.ask_prices[i] > 0 and self.ask_prices[i] <= target_price:
                    if self.ask_sizes[i] >= min_size:
                        total += self.ask_sizes[i]
        else:
            for i in range(self.bid_count):
                if self.bid_prices[i] >= target_price:
                    if self.bid_sizes[i] >= min_size:
                        total += self.bid_sizes[i]
        return total

    @property
    def mid_price(self) -> float:
        """Calculate mid price."""
        if self.best_bid == 0 or self.best_ask == 1.0:
            return 0.0
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float:
        """Calculate spread."""
        return self.best_ask - self.best_bid

    @property
    def spread_bps(self) -> float:
        """Calculate spread in basis points."""
        mid = self.mid_price
        if mid == 0:
            return 0.0
        return (self.spread / mid) * 10000

    def get_depth(self, side: str, depth: int = 5) -> list[tuple[float, float]]:
        """Get order book depth for a side."""
        if side == "buy":
            return [(self.bid_prices[i], self.bid_sizes[i])
                    for i in range(min(depth, self.bid_count))]
        else:
            return [(self.ask_prices[i], self.ask_sizes[i])
                    for i in range(min(depth, self.ask_count))]

    def is_stale(self, max_age_secs: float = 5.0) -> bool:
        """Check if order book data is stale."""
        return (time.time() - self.last_update) > max_age_secs


@dataclass
class MultiTokenOrderBook:
    """Manages order books for multiple tokens (YES/NO)."""

    books: dict[str, OrderBook] = field(default_factory=dict)

    def get_or_create(self, token_id: str, max_levels: int = 50) -> OrderBook:
        """Get or create order book for token."""
        if token_id not in self.books:
            self.books[token_id] = OrderBook(token_id=token_id, max_levels=max_levels)
        return self.books[token_id]

    def update(self, token_id: str, bids: list, asks: list) -> None:
        """Update order book for token."""
        book = self.get_or_create(token_id)
        book.update_from_snapshot(bids, asks)

    def get_yes_no_books(self, yes_token: str, no_token: str) -> tuple[OrderBook, OrderBook]:
        """Get YES and NO order books."""
        return self.get_or_create(yes_token), self.get_or_create(no_token)
