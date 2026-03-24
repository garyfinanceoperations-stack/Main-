# Polymarket LP Bot

Automated liquidity provider bot for Polymarket prediction markets. Targets low-volume markets with active reward pools to capture LP rewards while strictly managing downside risk.

## Strategy

- **Finds markets** with reward pools >= $30/day and thin order books (< $50/level)
- **Provides two-sided liquidity** on both YES and NO tokens
- **Targets 30%+ share** of the market's reward pool
- **Looks for tight spreads** (< 5 cents between YES and NO)
- **Caps exposure** at $200 per market, $10 max loss per position
- **Emergency exits** at $30 loss per position or $100 portfolio loss

## Safety Features

1. **Per-position stop-loss** ($10) - partial market sell into liquidity
2. **Emergency dump threshold** ($30) - full position exit
3. **Portfolio stop-loss** ($100) - cancels everything, exits all positions
4. **Exposure cap** ($200/market) - blocks new orders exceeding limit
5. **Order-size validation** - ensures total orders don't exceed caps
6. **Graceful shutdown** (Ctrl+C) - cancels all orders before stopping
7. **State persistence** - recovers position tracking across restarts
8. **Dry-run mode** - scan markets without placing orders

## Setup (Windows)

### 1. Install Python 3.11+

Download from [python.org](https://www.python.org/downloads/). During install, check **"Add Python to PATH"**.

### 2. Clone and install dependencies

```powershell
git clone <this-repo-url>
cd Main-
pip install -r requirements.txt
```

### 3. Configure

```powershell
copy .env.example .env
```

Edit `.env` and set your **PRIVATE_KEY** (the Ethereum private key for the wallet you want the bot to use, WITHOUT 0x prefix).

### 4. Fund your wallet

Your wallet needs:
- **USDC.e on Polygon** (the collateral token for Polymarket)
- **MATIC on Polygon** (for gas fees, ~0.5 MATIC should last a while)

### 5. Run

```powershell
# Dry run first (no orders placed, just scans)
python bot.py --dry-run

# Live run
python bot.py

# Check status
python bot.py --status

# Emergency: cancel all orders
python bot.py --cancel-all
```

## Configuration

All settings are in `.env`. Key parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_EXPOSURE_PER_MARKET` | 200 | Max USD per market |
| `MAX_LOSS_PER_POSITION` | 10 | Stop-loss trigger per position |
| `EMERGENCY_LOSS_THRESHOLD` | 30 | Full dump trigger |
| `MIN_REWARD_POOL` | 30 | Min daily rewards to consider market |
| `MAX_ORDERBOOK_DEPTH` | 50 | Max $ per price level (filters thick books) |
| `MAX_SPREAD_GAP` | 5 | Max spread in cents |
| `MIN_REWARD_SHARE_TARGET` | 30 | Min % of rewards we'd capture |
| `ORDER_SIZE` | 15 | USD per order per side per level |
| `NUM_PRICE_LEVELS` | 3 | Number of price levels each side |
| `SCAN_INTERVAL` | 60 | Seconds between cycles |

## What You Need Before Running

1. **Ethereum wallet** with private key
2. **USDC.e on Polygon** - deposit via bridge (e.g., https://wallet.polygon.technology/)
3. **MATIC for gas** - small amount on Polygon
4. **Polymarket account** - your wallet must have accepted Polymarket's ToS
5. **Python 3.11+** installed
6. **Stable internet** - the bot needs to stay connected

## Architecture

```
bot.py              - Main loop & orchestration
config.py           - Configuration from .env
market_scanner.py   - Finds eligible markets via Gamma + CLOB APIs
order_manager.py    - Places/cancels orders via py-clob-client
risk_manager.py     - Tracks positions, enforces limits, triggers exits
logger.py           - Console + file logging
```

## Risk Warnings

- This bot trades real money. Start with small amounts.
- Prediction markets can move sharply on news events.
- The $10 stop-loss may not execute exactly at $10 if there's insufficient liquidity.
- Always run `--dry-run` first to verify the bot finds suitable markets.
- Monitor the bot regularly - don't leave it unattended for extended periods initially.
