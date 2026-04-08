"""Risk manager - tracks positions, enforces limits, triggers emergency exits."""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional
from config import BotConfig
from logger import log


@dataclass
class Position:
    """A tracked position in a market."""
    condition_id: str
    token_id: str
    side: str  # "YES" or "NO"
    entry_price: float
    size: float  # number of shares
    cost_basis: float  # total USD spent
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    order_ids: list = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def current_value(self) -> float:
        return self.size * self.current_price

    @property
    def pnl(self) -> float:
        return self.current_value - self.cost_basis


@dataclass
class MarketExposure:
    """Aggregated exposure for a single market."""
    condition_id: str
    yes_position: Optional[Position] = None
    no_position: Optional[Position] = None
    total_open_orders_usd: float = 0.0
    realized_pnl: float = 0.0

    @property
    def total_exposure(self) -> float:
        total = self.total_open_orders_usd
        if self.yes_position:
            total += self.yes_position.cost_basis
        if self.no_position:
            total += self.no_position.cost_basis
        return total

    @property
    def total_unrealized_pnl(self) -> float:
        pnl = 0.0
        if self.yes_position:
            pnl += self.yes_position.pnl
        if self.no_position:
            pnl += self.no_position.pnl
        return pnl


class RiskManager:
    """Enforces risk limits and tracks all positions."""

    STATE_FILE = "state.json"

    def __init__(self, config: BotConfig):
        self.config = config
        self.exposures: dict[str, MarketExposure] = {}
        self.total_realized_pnl: float = 0.0
        self.total_rewards_earned: float = 0.0
        self.emergency_mode: bool = False
        self._load_state()

    # === State Persistence ===

    def _load_state(self):
        """Load persisted state from disk."""
        if not os.path.exists(self.STATE_FILE):
            return
        try:
            with open(self.STATE_FILE, "r") as f:
                data = json.load(f)
            self.total_realized_pnl = data.get("total_realized_pnl", 0.0)
            self.total_rewards_earned = data.get("total_rewards_earned", 0.0)

            for cid, exp_data in data.get("exposures", {}).items():
                exposure = MarketExposure(condition_id=cid)
                exposure.realized_pnl = exp_data.get("realized_pnl", 0.0)
                exposure.total_open_orders_usd = exp_data.get("total_open_orders_usd", 0.0)

                if exp_data.get("yes_position"):
                    exposure.yes_position = Position(**exp_data["yes_position"])
                if exp_data.get("no_position"):
                    exposure.no_position = Position(**exp_data["no_position"])

                self.exposures[cid] = exposure

            log.info(f"Loaded state: {len(self.exposures)} markets tracked, "
                     f"realized PnL: ${self.total_realized_pnl:.2f}")
        except Exception as e:
            log.error(f"Error loading state: {e}")

    def save_state(self):
        """Persist state to disk."""
        try:
            data = {
                "total_realized_pnl": self.total_realized_pnl,
                "total_rewards_earned": self.total_rewards_earned,
                "exposures": {},
            }
            for cid, exp in self.exposures.items():
                exp_data = {
                    "realized_pnl": exp.realized_pnl,
                    "total_open_orders_usd": exp.total_open_orders_usd,
                }
                if exp.yes_position:
                    exp_data["yes_position"] = asdict(exp.yes_position)
                if exp.no_position:
                    exp_data["no_position"] = asdict(exp.no_position)
                data["exposures"][cid] = exp_data

            with open(self.STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error(f"Error saving state: {e}")

    # === Pre-Trade Checks ===

    def can_place_order(self, condition_id: str, side: str, size_usd: float) -> tuple[bool, str]:
        """Check if placing an order is allowed under risk limits."""
        if self.emergency_mode and side == "BUY":
            return False, "EMERGENCY MODE ACTIVE - no new orders"

        # SELL orders always allowed — they reduce exposure, not increase it
        if side == "SELL":
            return True, "OK"

        exposure = self.exposures.get(condition_id, MarketExposure(condition_id=condition_id))

        # Check market-level exposure (with $0.50 tolerance for rounding)
        new_total = exposure.total_exposure + size_usd
        limit = self.config.max_exposure_per_market + 0.50
        if new_total > limit:
            return False, (
                f"Would exceed market exposure limit: "
                f"${new_total:.2f} > ${self.config.max_exposure_per_market:.2f}"
            )

        # Check total portfolio exposure across all markets
        total_portfolio = sum(e.total_exposure for e in self.exposures.values()) + size_usd
        max_portfolio = self.config.max_exposure_per_market * 10  # allow up to 10 markets
        if total_portfolio > max_portfolio:
            return False, f"Would exceed portfolio limit: ${total_portfolio:.2f}"

        return True, "OK"

    def register_pending_order(self, condition_id: str, cost_usd: float):
        """Track a pending order's cost so subsequent orders see it."""
        if condition_id not in self.exposures:
            self.exposures[condition_id] = MarketExposure(condition_id=condition_id)
        self.exposures[condition_id].total_open_orders_usd += cost_usd

    def unregister_pending_order(self, condition_id: str, cost_usd: float):
        """Remove a pending order's cost (e.g. on failure or cancel)."""
        exposure = self.exposures.get(condition_id)
        if exposure:
            exposure.total_open_orders_usd = max(0, exposure.total_open_orders_usd - cost_usd)

    # === Position Tracking ===

    def record_fill(self, condition_id: str, token_id: str, side: str,
                    price: float, size: float, order_id: str):
        """Record a filled order as a position."""
        if condition_id not in self.exposures:
            self.exposures[condition_id] = MarketExposure(condition_id=condition_id)

        exposure = self.exposures[condition_id]
        cost = price * size

        if side == "YES":
            if exposure.yes_position:
                # Add to existing position
                pos = exposure.yes_position
                pos.cost_basis += cost
                pos.size += size
                pos.entry_price = pos.cost_basis / pos.size if pos.size > 0 else price
                pos.order_ids.append(order_id)
            else:
                exposure.yes_position = Position(
                    condition_id=condition_id,
                    token_id=token_id,
                    side="YES",
                    entry_price=price,
                    size=size,
                    cost_basis=cost,
                    current_price=price,
                    order_ids=[order_id],
                )
        else:
            if exposure.no_position:
                pos = exposure.no_position
                pos.cost_basis += cost
                pos.size += size
                pos.entry_price = pos.cost_basis / pos.size if pos.size > 0 else price
                pos.order_ids.append(order_id)
            else:
                exposure.no_position = Position(
                    condition_id=condition_id,
                    token_id=token_id,
                    side="NO",
                    entry_price=price,
                    size=size,
                    cost_basis=cost,
                    current_price=price,
                    order_ids=[order_id],
                )

        log.info(f"FILL: {side} {size:.2f} shares @ ${price:.4f} "
                 f"(cost: ${cost:.2f}) in {condition_id[:16]}")
        self.save_state()

    def update_prices(self, condition_id: str, yes_price: float, no_price: float):
        """Update current prices for position PnL calculation."""
        exposure = self.exposures.get(condition_id)
        if not exposure:
            return

        if exposure.yes_position:
            exposure.yes_position.current_price = yes_price
            exposure.yes_position.unrealized_pnl = exposure.yes_position.pnl
        if exposure.no_position:
            exposure.no_position.current_price = no_price
            exposure.no_position.unrealized_pnl = exposure.no_position.pnl

    def record_close(self, condition_id: str, side: str, proceeds: float):
        """Record closing a position."""
        exposure = self.exposures.get(condition_id)
        if not exposure:
            return

        if side == "YES" and exposure.yes_position:
            pnl = proceeds - exposure.yes_position.cost_basis
            exposure.realized_pnl += pnl
            self.total_realized_pnl += pnl
            log.info(f"CLOSED YES in {condition_id[:16]}: PnL ${pnl:.2f}")
            exposure.yes_position = None
        elif side == "NO" and exposure.no_position:
            pnl = proceeds - exposure.no_position.cost_basis
            exposure.realized_pnl += pnl
            self.total_realized_pnl += pnl
            log.info(f"CLOSED NO in {condition_id[:16]}: PnL ${pnl:.2f}")
            exposure.no_position = None

        self.save_state()

    # === Risk Checks ===

    def check_positions(self) -> list[dict]:
        """
        Check all positions against risk limits.
        Returns list of actions needed: 'reduce' or 'emergency_exit'.
        """
        actions = []

        for cid, exposure in self.exposures.items():
            for side, position in [("YES", exposure.yes_position), ("NO", exposure.no_position)]:
                if not position:
                    continue

                loss = -position.pnl  # positive number means loss
                cost = position.cost_basis

                # 10% loss threshold — percentage-based, not flat dollar
                # e.g., $10 position → triggers at $1 loss, $25 → at $2.50
                loss_pct = loss / max(cost, 0.01)
                loss_limit_pct = 0.10  # 10% max loss before action

                # LEVEL 1: Loss exceeds 10% of position cost
                # -> Place SELL limit at loss floor, don't market-sell
                if loss_pct >= loss_limit_pct and cost > 0:
                    actions.append({
                        "action": "reduce",
                        "condition_id": cid,
                        "token_id": position.token_id,
                        "side": side,
                        "loss": loss,
                        "size": position.size,
                        "reason": f"Loss ${loss:.2f} ({loss_pct:.0%}) >= {loss_limit_pct:.0%} of ${cost:.2f}",
                    })
                    log.warning(
                        f"RISK: {side} in {cid[:16]} losing ${loss:.2f} ({loss_pct:.0%}) "
                        f"- will hold SELL order, not panic-sell"
                    )

                # LEVEL 2: Loss exceeds emergency threshold ($25 default)
                # -> Only for catastrophic losses (>25% of position)
                emergency_pct = 0.25
                if loss_pct >= emergency_pct and cost > 0:
                    actions.append({
                        "action": "emergency_exit",
                        "condition_id": cid,
                        "token_id": position.token_id,
                        "side": side,
                        "loss": loss,
                        "size": position.size,
                        "reason": f"EMERGENCY: Loss ${loss:.2f} ({loss_pct:.0%}) >= {emergency_pct:.0%}",
                    })
                    log.critical(
                        f"EMERGENCY: {side} in {cid[:16]} losing ${loss:.2f} ({loss_pct:.0%}) "
                        f"- attempting exit"
                    )

        # Check total portfolio loss
        total_unrealized = sum(
            e.total_unrealized_pnl for e in self.exposures.values()
        )
        if total_unrealized < -self.config.portfolio_stop_loss:
            self.emergency_mode = True
            log.critical(
                f"PORTFOLIO STOP-LOSS: Total unrealized PnL ${total_unrealized:.2f} "
                f"- ENTERING EMERGENCY MODE"
            )
            # Add emergency exit for all positions
            for cid, exposure in self.exposures.items():
                for side, position in [("YES", exposure.yes_position), ("NO", exposure.no_position)]:
                    if position and position.size > 0:
                        actions.append({
                            "action": "emergency_exit",
                            "condition_id": cid,
                            "token_id": position.token_id,
                            "side": side,
                            "loss": -position.pnl,
                            "size": position.size,
                            "reason": "PORTFOLIO STOP-LOSS TRIGGERED",
                        })

        return actions

    def get_status_report(self) -> str:
        """Generate a human-readable status report."""
        lines = [
            "=" * 60,
            "  POLYMARKET LP BOT - STATUS REPORT",
            "=" * 60,
            f"  Emergency Mode: {'YES - NO NEW ORDERS' if self.emergency_mode else 'No'}",
            f"  Markets Tracked: {len(self.exposures)}",
            f"  Total Realized PnL: ${self.total_realized_pnl:.2f}",
            f"  Total Rewards Earned: ${self.total_rewards_earned:.2f}",
            "-" * 60,
        ]

        total_exposure = 0.0
        total_unrealized = 0.0

        for cid, exp in self.exposures.items():
            if not exp.yes_position and not exp.no_position:
                continue

            lines.append(f"  Market: {cid[:20]}...")
            if exp.yes_position:
                p = exp.yes_position
                lines.append(
                    f"    YES: {p.size:.2f} shares @ ${p.entry_price:.4f} "
                    f"| Current: ${p.current_price:.4f} | PnL: ${p.pnl:.2f}"
                )
                total_exposure += p.cost_basis
                total_unrealized += p.pnl
            if exp.no_position:
                p = exp.no_position
                lines.append(
                    f"    NO:  {p.size:.2f} shares @ ${p.entry_price:.4f} "
                    f"| Current: ${p.current_price:.4f} | PnL: ${p.pnl:.2f}"
                )
                total_exposure += p.cost_basis
                total_unrealized += p.pnl

            lines.append(f"    Market Realized PnL: ${exp.realized_pnl:.2f}")

        lines.extend([
            "-" * 60,
            f"  Total Exposure: ${total_exposure:.2f}",
            f"  Total Unrealized PnL: ${total_unrealized:.2f}",
            f"  Net PnL (realized + unrealized): ${self.total_realized_pnl + total_unrealized:.2f}",
            "=" * 60,
        ])

        return "\n".join(lines)
