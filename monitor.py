#!/usr/bin/env python3
"""
Polymarket Liquidity Monitor

Monitors BTC 5m/15m markets and alerts when tradeable liquidity appears.
Run with: python monitor.py
"""

import asyncio
import time
import os
import sys
import logging
from datetime import datetime

# Suppress all library logs
logging.basicConfig(level=logging.CRITICAL)
os.environ['STRUCTLOG_LEVEL'] = 'CRITICAL'

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Configure structlog to be silent
import structlog
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL),
)

from src.config import Settings
from src.core.orderbook import MultiTokenOrderBook
from src.core.websocket import WebSocketManager
from src.core.rest_client import RestClient
import json

# Alert thresholds
MAX_SPREAD_PCT = 20.0
MIN_LIQUIDITY_SIZE = 50.0
MIN_PRICE = 0.10
MAX_PRICE = 0.90

# Colors
G = "\033[92m"  # Green
R = "\033[91m"  # Red
Y = "\033[93m"  # Yellow
C = "\033[96m"  # Cyan
B = "\033[1m"   # Bold
X = "\033[0m"   # Reset


def beep():
    """Alert sound."""
    try:
        os.system('afplay /System/Library/Sounds/Glass.aiff 2>/dev/null &')
    except:
        print("\a")


def clear_line():
    """Clear current line."""
    print("\033[2K\033[1G", end="")


class Monitor:
    def __init__(self):
        self.settings = Settings()
        self.settings.dry_run = True
        self.orderbooks = MultiTokenOrderBook()
        self.ws = WebSocketManager(self.settings, self.orderbooks)
        self.rest = RestClient(self.settings)
        self.markets = {}
        self.last_alert = 0

    async def run(self):
        print(f"\n{C}{B}=== POLYMARKET LIQUIDITY MONITOR ==={X}")
        print(f"{C}Alerts when spread < {MAX_SPREAD_PCT}% with real liquidity{X}\n")

        # Find markets
        print(f"Finding markets...")
        self.markets = await self.rest.discover_btc_markets(['5m', '15m'])

        if not self.markets:
            print(f"{R}No markets found!{X}")
            return

        for tf, m in self.markets.items():
            tokens = json.loads(m.get("clobTokenIds", "[]"))
            if len(tokens) >= 2:
                m["up_token_id"] = tokens[0]
                m["down_token_id"] = tokens[1]
            print(f"  {tf}: {m.get('question', '')[:45]}...")

        # Connect WebSocket
        print(f"\nConnecting...")
        await self.ws.connect()

        token_ids = []
        for m in self.markets.values():
            token_ids.extend([m.get("up_token_id", ""), m.get("down_token_id", "")])
        token_ids = [t for t in token_ids if t]
        await self.ws.subscribe(token_ids)

        print(f"{G}Connected! Monitoring...{X}")
        print(f"{Y}Press Ctrl+C to stop{X}\n")

        # Start listener
        ws_task = asyncio.create_task(self.ws.listen())

        try:
            while True:
                await self.check()
                await asyncio.sleep(5)
        except KeyboardInterrupt:
            print(f"\n{Y}Stopping...{X}")
        finally:
            ws_task.cancel()
            await self.ws.close()
            await self.rest.close()

    async def check(self):
        now = datetime.now().strftime("%H:%M:%S")
        lines = [f"{C}[{now}]{X}"]

        any_tradeable = False
        arb_sum = None

        for tf, m in self.markets.items():
            up = m.get("up_token_id", "")
            down = m.get("down_token_id", "")
            if not up:
                continue

            book = self.orderbooks.get_or_create(up)
            down_book = self.orderbooks.get_or_create(down)

            bid, bid_sz = book.get_tradeable_bid(MIN_PRICE, MIN_LIQUIDITY_SIZE)
            ask, ask_sz = book.get_tradeable_ask(MAX_PRICE, MIN_LIQUIDITY_SIZE)
            down_ask, _ = down_book.get_tradeable_ask(MAX_PRICE, MIN_LIQUIDITY_SIZE)

            has_liq = bid > 0 and ask < 1.0
            spread = ((ask - bid) / ((ask + bid) / 2) * 100) if has_liq else 0
            tradeable = has_liq and spread < MAX_SPREAD_PCT

            if tradeable:
                status = f"{G}{B}TRADEABLE{X}"
                any_tradeable = True
            elif has_liq:
                status = f"{Y}wide spread{X}"
            else:
                status = f"{R}no liquidity{X}"

            bid_s = f"{bid:.2f}" if bid > 0 else "----"
            ask_s = f"{ask:.2f}" if ask < 1.0 else "----"
            spread_s = f"{spread:.0f}%" if has_liq else "N/A"

            lines.append(f"  {tf:4s} | bid:{bid_s} ask:{ask_s} | spread:{spread_s:>4s} | {status}")

            # Track for arb check
            if tf == "15m":
                arb_sum = ask  # 15m UP ask
            elif tf == "5m" and arb_sum is not None:
                arb_sum += down_ask  # + 5m DOWN ask
                if arb_sum < 0.99 and arb_sum > 0:
                    profit = (1.0 - arb_sum) * 100
                    lines.append(f"  {G}{B}>>> ARB: sum={arb_sum:.2f} profit={profit:.1f}%{X}")
                    any_tradeable = True

        # Print status
        print("\n".join(lines))

        # Alert
        if any_tradeable and (time.time() - self.last_alert > 30):
            self.last_alert = time.time()
            beep()
            print(f"\n{G}{B}*** OPPORTUNITY DETECTED ***{X}\n")


async def main():
    m = Monitor()
    await m.run()


if __name__ == "__main__":
    asyncio.run(main())
