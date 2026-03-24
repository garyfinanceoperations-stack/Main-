# Risk & Loss Analysis - 30-Day Projection

## How Losses Happen in LP

When you provide two-sided liquidity, you lose money in three ways:

1. **Adverse selection** - Smart traders fill your order on the correct side, leaving you holding the wrong side. This is the most common loss.
2. **Market resolution** - A market resolves and one side goes to $0. If you're holding that side, you lose your cost basis.
3. **Spread slippage on exit** - When stop-losses trigger, you market sell into thin liquidity at a discount.

**The hedge:** When both YES and NO fill symmetrically, your net exposure is near zero (YES + NO = $1.00). The bot targets tight spreads specifically so that paired fills are close to break-even.

---

## Loss Model Assumptions

| Factor | Value | Notes |
|--------|-------|-------|
| Adverse fill rate | 30-40% of fills | Not all fills are paired; smart money picks sides |
| Avg loss per adverse fill | $1-3 | On a $10 order with 5c max spread |
| Stop-loss hit rate | 1-3 per week per market | $10 cap per position |
| Emergency dump rate | 0-1 per month total | $25 cap, rare if stop-losses work |
| Market resolution risk | ~5% of active markets/month | Lose remaining position value |
| Spread slippage on exit | 5-15% of position | Thin books = worse exits |

---

## $300 Budget - 30 Day Projection

**Config:** $10 orders, 2 levels, 5 markets, $40/market deployed

### Best Case (low volatility, good market selection)
| | Daily | Monthly |
|--|-------|---------|
| Rewards earned | $6-8 | $180-240 |
| Adverse fill losses | -$2 | -$60 |
| Stop-losses triggered | 0-1/week | -$20 |
| Emergency dumps | 0 | $0 |
| **Net P&L** | | **+$100 to +$160** |

### Average Case (typical conditions)
| | Daily | Monthly |
|--|-------|---------|
| Rewards earned | $4-6 | $120-180 |
| Adverse fill losses | -$5 | -$150 |
| Stop-losses triggered | 1-2/week | -$50 |
| Emergency dumps | 0-1 | -$15 |
| **Net P&L** | | **-$35 to +$35** |

### Worst Case (high volatility, news-driven markets)
| | Daily | Monthly |
|--|-------|---------|
| Rewards earned | $3-4 | $90-120 |
| Adverse fill losses | -$10 | -$300 |
| Stop-losses triggered | 3-5/week | -$150 |
| Emergency dumps | 2-3 | -$60 |
| Portfolio stop-loss | 1 | -$80 cap |
| **Net P&L** | | **-$80 (capped by portfolio stop)** |

**$300 Bottom line:** Your maximum theoretical loss is **$80** (portfolio stop-loss). Realistic worst month is **-$40 to -$80**. Average month is roughly **break-even**. The bot needs 2-3 months of average conditions to become consistently profitable as you learn which markets work best.

---

## $1,000 Budget - 30 Day Projection

**Config:** $20 orders, 3 levels, 7 markets, $200/market

### Best Case
| | Daily | Monthly |
|--|-------|---------|
| Rewards earned | $18-25 | $540-750 |
| Adverse fill losses | -$6 | -$180 |
| Stop-losses triggered | 1-2/week | -$50 |
| **Net P&L** | | **+$310 to +$520** |

### Average Case
| | Daily | Monthly |
|--|-------|---------|
| Rewards earned | $12-18 | $360-540 |
| Adverse fill losses | -$12 | -$360 |
| Stop-losses triggered | 2-3/week | -$100 |
| Emergency dumps | 0-1 | -$25 |
| **Net P&L** | | **-$125 to +$155** |

### Worst Case
| | Daily | Monthly |
|--|-------|---------|
| Rewards earned | $8-12 | $240-360 |
| All loss channels | | -$400 |
| Portfolio stop-loss | | -$200 cap |
| **Net P&L** | | **-$200 (capped)** |

---

## $3,500 Budget - 30 Day Projection (target: $50/day)

**Config:** $40 orders, 3 levels, 12 markets, $400/market

### Best Case
| | Daily | Monthly |
|--|-------|---------|
| Rewards earned | $55-80 | $1,650-2,400 |
| Adverse fill losses | -$15 | -$450 |
| Stop-losses triggered | 2-3/week | -$120 |
| **Net P&L** | | **+$1,080 to +$1,830** |

### Average Case
| | Daily | Monthly |
|--|-------|---------|
| Rewards earned | $35-55 | $1,050-1,650 |
| Adverse fill losses | -$30 | -$900 |
| Stop-losses triggered | 3-5/week | -$200 |
| Emergency dumps | 1-2 | -$50 |
| **Net P&L** | | **-$100 to +$500** |

### Worst Case
| | Daily | Monthly |
|--|-------|---------|
| Rewards earned | $25-35 | $750-1,050 |
| All loss channels | | -$1,200 |
| Portfolio stop-loss | | -$700 cap |
| **Net P&L** | | **-$700 (capped)** |

---

## $5,000 Budget - 30 Day Projection (consistent $50/day target)

**Config:** $50 orders, 3 levels, 15 markets, $500/market

### Best Case
| | Daily | Monthly |
|--|-------|---------|
| Rewards earned | $70-100 | $2,100-3,000 |
| All losses | | -$600 |
| **Net P&L** | | **+$1,500 to +$2,400** |

### Average Case
| | Daily | Monthly |
|--|-------|---------|
| Rewards earned | $50-70 | $1,500-2,100 |
| All losses | | -$1,200 |
| **Net P&L** | | **+$300 to +$900** |

### Worst Case
| | Daily | Monthly |
|--|-------|---------|
| Rewards earned | $35-50 | $1,050-1,500 |
| All losses | | -$2,000 |
| Portfolio stop-loss | | -$1,000 cap |
| **Net P&L** | | **-$1,000 (capped)** |

---

## Key Risk Factors That Shift Outcomes

| Factor | Helps | Hurts |
|--------|-------|-------|
| Low-volume markets | Higher reward share | Less exit liquidity |
| Tight spreads | Better Q scores | More adverse selection |
| News/election events | Higher reward pools | Violent price moves |
| Competition | - | Dilutes reward share |
| Bot uptime | More reward minutes | - |
| Multiple markets | Diversification | More to monitor |

## Protection Summary

| Safeguard | Trigger | Max damage |
|-----------|---------|------------|
| Per-position stop | $10 loss | $10 per position |
| Emergency dump | $25 loss | $25 per position |
| Portfolio stop | $80 total loss ($300 budget) | 27% of capital |
| Graceful shutdown | Ctrl+C or crash | Cancels all orders |
| Thin-book filter | >$50/level | Avoids unwinnable competition |
| Reward share filter | <30% share | Avoids low-yield markets |

## The Honest Truth

- **Month 1 at $300:** Expect break-even to slight loss while learning. The bot finds markets but you'll need to observe which ones actually pay.
- **Months 2-3:** With tuning (blacklisting bad markets, adjusting edge), should trend positive.
- **To reliably net $50/day:** You need $3,500-5,000 and 12+ good markets running simultaneously.
- **Absolute worst case at $300:** You lose $80 and the bot stops itself. That's 27% of capital — painful but not catastrophic.
