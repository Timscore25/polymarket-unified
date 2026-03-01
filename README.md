# Polymarket Unified Trading System

Combined trading system with **market making** and **cross-market arbitrage** strategies for Polymarket BTC 5m/15m prediction markets.

## Features

- **Market Making**: Spread farming with inventory-aware sizing
- **Arbitrage**: Cross-market price discrepancy detection (5m vs 15m)
- **3-Layer Risk Management**: Position size, exposure, and skew limits
- **Performance Optimized**: NumPy order books, orjson parsing, async everywhere
- **Dry Run Mode**: Test safely without executing real trades

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your credentials

# Run in dry-run mode (default)
python -m src.main
```

## Configuration

Key settings in `.env`:

| Setting | Default | Description |
|---------|---------|-------------|
| `DRY_RUN` | true | Simulate trades without executing |
| `MM_SPREAD_BPS` | 10 | Market making spread (0.1%) |
| `MM_DEFAULT_SIZE` | 100 | Order size in USD |
| `ARB_THRESHOLD` | 0.99 | Arbitrage trigger threshold |
| `MAX_EXPOSURE_USD` | 10000 | Maximum exposure limit |

## Architecture

```
src/
├── main.py              # Orchestrator
├── config.py            # Settings
├── core/
│   ├── orderbook.py     # NumPy-based order book
│   ├── websocket.py     # Real-time data
│   └── rest_client.py   # API client
├── strategies/
│   ├── market_maker.py  # Spread farming
│   └── arbitrage.py     # Cross-market arb
├── risk/
│   ├── manager.py       # 3-layer validation
│   └── inventory.py     # Position tracking
└── execution/
    ├── order_manager.py # Order lifecycle
    └── signer.py        # EIP-712 signing
```

## Strategies

### Market Making
- Places BUY orders on both YES and NO tokens
- Captures spread when both sides fill
- Automatically adjusts size based on inventory skew

### Arbitrage
- Monitors BTC 5m and 15m markets simultaneously
- Detects when sum of opposite asks < threshold
- Executes both legs simultaneously during overlap window

## Risk Management

1. **Position Size**: Max single order size
2. **Exposure Limits**: Total long/short caps
3. **Inventory Skew**: Prevents runaway positions

## Monitoring

Metrics available at `http://localhost:9306/metrics` when `METRICS_ENABLED=true`.

## Disclaimer

Use at your own risk. Trading involves risk of loss. Test thoroughly in dry-run mode before live trading.
