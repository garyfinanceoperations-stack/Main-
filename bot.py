"""
Polymarket LP Bot - Main entry point.

Provides two-sided liquidity on low-volume Polymarket markets
to capture liquidity rewards while strictly managing risk.

Usage:
    python bot.py              # Run the bot
    python bot.py --dry-run    # Scan markets without placing orders
    python bot.py --status     # Show current position status
    python bot.py --cancel-all # Cancel all open orders and exit
"""

import argparse
import signal
import sys
import time
import traceback
from config import BotConfig
from logger import log
from market_scanner import MarketScanner
from risk_manager import RiskManager
from order_manager import OrderManager
from learner import Learner


class PolymarketLPBot:
    """Main bot orchestrator."""

    def __init__(self, config: BotConfig, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.running = False
        self.scanner = MarketScanner(config)
        self.risk = RiskManager(config)
        self.learner = Learner(config)
        self.orders: OrderManager = None  # initialized after validation

        # Active markets we're providing liquidity to
        self.active_markets: dict[str, dict] = {}  # condition_id -> market info

        # Graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        log.warning(f"Received signal {signum} - initiating graceful shutdown...")
        self.running = False

    def initialize(self) -> bool:
        """Initialize the bot - validate config, set up client, check allowances."""
        log.info("=" * 60)
        log.info("  POLYMARKET LP BOT - INITIALIZING")
        log.info("=" * 60)

        # Validate config
        errors = self.config.validate()
        if errors:
            for err in errors:
                log.error(f"Config error: {err}")
            return False

        if self.dry_run:
            log.info("DRY RUN MODE - no orders will be placed")
            return True

        # Initialize order manager (connects to CLOB API)
        try:
            self.orders = OrderManager(self.config, self.risk)
        except Exception as e:
            log.error(f"Failed to initialize order manager: {e}")
            return False

        # Check and set allowances
        try:
            self.orders.set_allowances()
        except Exception as e:
            log.warning(f"Allowance setup issue (may already be set): {e}")

        log.info("Bot initialized successfully")
        return True

    def scan_and_select_markets(self) -> list:
        """Scan for eligible markets and select the best ones."""
        markets = self.scanner.scan_for_opportunities()

        if not markets:
            log.info("No eligible markets found this scan")
            return []

        # Filter out blacklisted markets
        before_count = len(markets)
        markets = [m for m in markets if not self.learner.is_blacklisted(m.condition_id)]
        if len(markets) < before_count:
            log.info(f"Learner filtered out {before_count - len(markets)} blacklisted markets")

        # Re-score using learner (combines reward estimate + historical performance)
        for m in markets:
            m.our_share_estimate = self.learner.score_market(
                m.condition_id, m.reward_pool, m.our_share_estimate,
                m.spread, m.orderbook_depth_yes + m.orderbook_depth_no,
            )

        # Sort by learned score (stored in our_share_estimate for compatibility)
        markets.sort(key=lambda m: m.our_share_estimate, reverse=True)

        # Limit to configured max markets
        max_markets = min(self.config.max_active_markets, len(markets))
        selected = markets[:max_markets]

        for m in selected:
            # Record market entry for learning
            self.learner.record_market_entry(
                m.condition_id, m.question, m.spread,
                m.reward_pool, m.orderbook_depth_yes + m.orderbook_depth_no,
                m.volume_24h, m.our_share_estimate,
            )
            log.info(
                f"Selected: {m.question[:50]} | "
                f"Reward: ${m.reward_pool:.2f}/day | "
                f"Score: {m.our_share_estimate:.2f} | "
                f"Blacklisted: No"
            )

        return selected

    def refresh_quotes(self, markets: list):
        """Cancel old orders and place fresh quotes for all active markets."""
        if self.dry_run or not self.orders:
            return

        # Apply learned parameter adjustments
        learned = self.learner.get_adjusted_params()
        if learned:
            log.debug(f"Applying learned params: {learned}")

        for market in markets:
            try:
                # Cancel existing orders for this market
                self.orders.cancel_market_orders(market.condition_id)
                time.sleep(0.2)

                # Place fresh two-sided quotes with learned adjustments
                order_ids = self.orders.place_lp_quotes(
                    market, learned_params=learned
                )

                if order_ids:
                    self.active_markets[market.condition_id] = {
                        "market": market,
                        "order_ids": order_ids,
                        "last_refresh": time.time(),
                    }

            except Exception as e:
                log.error(f"Error refreshing quotes for {market.condition_id[:16]}: {e}")

    def check_risk_and_act(self):
        """Run risk checks and execute any required actions."""
        if self.dry_run or not self.orders:
            return

        # Update prices for all tracked positions
        for cid, info in self.active_markets.items():
            market = info["market"]
            try:
                yes_mid = self.scanner.get_midpoint(market.token_yes)
                no_mid = self.scanner.get_midpoint(market.token_no)
                self.risk.update_prices(cid, yes_mid, no_mid)
            except Exception:
                pass

        # Sync any fills and feed to learner
        try:
            fills = self.orders.sync_fills()
            if fills:
                for fill in fills:
                    self.learner.record_fill(
                        fill["condition_id"], fill["side"],
                        fill["price"], fill["size"],
                        fill.get("edge", self.config.min_edge),
                    )
        except Exception:
            pass

        # Check for risk actions
        actions = self.risk.check_positions()

        for action in actions:
            try:
                if action["action"] == "reduce":
                    # Partial sell to cap losses at ~$10
                    log.warning(f"RISK REDUCTION: {action['reason']}")
                    # Cancel orders first
                    self.orders.cancel_market_orders(action["condition_id"])
                    time.sleep(0.3)
                    # Market sell the position
                    self.orders.market_sell(
                        action["token_id"],
                        action["size"],
                        action["condition_id"],
                        partial=True,
                    )
                    # Record the close
                    self.risk.record_close(
                        action["condition_id"],
                        action["side"],
                        0,  # actual proceeds will be updated on fill
                    )
                    # Feed loss to learner
                    self.learner.record_trade_result(
                        action["condition_id"], -action["loss"]
                    )

                elif action["action"] == "emergency_exit":
                    # Full dump
                    log.critical(f"EMERGENCY EXIT: {action['reason']}")
                    self.orders.cancel_market_orders(action["condition_id"])
                    time.sleep(0.3)
                    self.orders.emergency_dump(
                        action["token_id"],
                        action["size"],
                        action["condition_id"],
                    )
                    self.risk.record_close(
                        action["condition_id"],
                        action["side"],
                        0,
                    )
                    # Feed loss to learner
                    self.learner.record_trade_result(
                        action["condition_id"], -action["loss"]
                    )
                    # Remove from active markets
                    self.active_markets.pop(action["condition_id"], None)

            except Exception as e:
                log.error(f"Error executing risk action: {e}")
                traceback.print_exc()

    def run(self):
        """Main bot loop."""
        if not self.initialize():
            log.error("Initialization failed - exiting")
            return

        self.running = True
        scan_counter = 0
        FULL_SCAN_EVERY = 10  # full market re-scan every 10 cycles

        log.info("Bot is now running. Press Ctrl+C to stop.")
        log.info(f"Scan interval: {self.config.scan_interval}s")

        while self.running:
            try:
                cycle_start = time.time()

                # Full market scan periodically
                if scan_counter % FULL_SCAN_EVERY == 0:
                    log.info("--- Full market scan ---")
                    markets = self.scan_and_select_markets()
                    if markets:
                        self.refresh_quotes(markets)
                else:
                    # Quick refresh: just update quotes for existing markets
                    existing = [
                        info["market"]
                        for info in self.active_markets.values()
                    ]
                    if existing:
                        # Re-fetch book data for existing markets
                        refreshed = []
                        for m in existing:
                            book_yes = self.scanner.get_orderbook(m.token_yes)
                            book_no = self.scanner.get_orderbook(m.token_no)
                            yes_bids = book_yes.get("bids", [])
                            yes_asks = book_yes.get("asks", [])
                            if yes_bids and yes_asks:
                                m.yes_bid = float(yes_bids[0]["price"])
                                m.yes_ask = float(yes_asks[0]["price"])
                                m.midpoint = (m.yes_bid + m.yes_ask) / 2
                                m.spread = m.yes_ask - m.yes_bid
                            refreshed.append(m)
                            time.sleep(0.1)
                        self.refresh_quotes(refreshed)

                # Always run risk checks
                self.check_risk_and_act()

                # Status report every 5 cycles
                if scan_counter % 5 == 0:
                    log.info(self.risk.get_status_report())

                # Learning report every 20 cycles
                if scan_counter % 20 == 0 and scan_counter > 0:
                    log.info(self.learner.get_learning_report())

                scan_counter += 1

                # Sleep until next cycle
                elapsed = time.time() - cycle_start
                sleep_time = max(1, self.config.scan_interval - elapsed)
                log.debug(f"Cycle completed in {elapsed:.1f}s, sleeping {sleep_time:.1f}s")

                # Interruptible sleep
                sleep_end = time.time() + sleep_time
                while time.time() < sleep_end and self.running:
                    time.sleep(1)

            except KeyboardInterrupt:
                self.running = False
            except Exception as e:
                log.error(f"Error in main loop: {e}")
                traceback.print_exc()
                time.sleep(5)  # back off on errors

        # Graceful shutdown
        self.shutdown()

    def shutdown(self):
        """Graceful shutdown - cancel all orders, save state, record learning."""
        log.warning("Shutting down bot...")

        if self.orders and not self.dry_run:
            log.info("Cancelling all open orders...")
            self.orders.cancel_all_orders()

        # Record session-end learning
        market_pnl = {}
        for cid, exp in self.risk.exposures.items():
            market_pnl[cid] = exp.realized_pnl + exp.total_unrealized_pnl
        self.learner.record_session_end(market_pnl)

        self.risk.save_state()
        self.learner.save()
        log.info("State and learning data saved. Bot stopped.")
        log.info(self.risk.get_status_report())
        log.info(self.learner.get_learning_report())


def main():
    parser = argparse.ArgumentParser(description="Polymarket LP Bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan markets without placing orders")
    parser.add_argument("--status", action="store_true",
                        help="Show current position status and exit")
    parser.add_argument("--cancel-all", action="store_true",
                        help="Cancel all open orders and exit")
    parser.add_argument("--learning", action="store_true",
                        help="Show learning engine status and exit")
    parser.add_argument("--unblacklist", type=str, default=None,
                        help="Remove a market from the blacklist (condition_id)")
    args = parser.parse_args()

    config = BotConfig()

    if args.learning:
        learner = Learner(config)
        print(learner.get_learning_report())
        return

    if args.unblacklist:
        learner = Learner(config)
        learner.unblacklist(args.unblacklist)
        print(f"Unblacklisted market {args.unblacklist}")
        return

    if args.status:
        risk = RiskManager(config)
        print(risk.get_status_report())
        return

    if args.cancel_all:
        errors = config.validate()
        if errors:
            for err in errors:
                log.error(f"Config error: {err}")
            return
        risk = RiskManager(config)
        orders = OrderManager(config, risk)
        orders.cancel_all_orders()
        log.info("All orders cancelled.")
        return

    bot = PolymarketLPBot(config, dry_run=args.dry_run)
    bot.run()


if __name__ == "__main__":
    main()
