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

    def __init__(self, config: BotConfig, dry_run: bool = False, spending_cap: float = 0):
        self.config = config
        self.dry_run = dry_run
        self.spending_cap = spending_cap  # 0 = no cap
        self.total_spent = 0.0  # tracks total USD committed to orders
        self.cap_reached = False
        self.running = False
        self.scanner = MarketScanner(config)
        self.risk = RiskManager(config)
        self.learner = Learner(config)
        self.orders: OrderManager = None  # initialized after validation

        # Active markets we're providing liquidity to
        self.active_markets: dict[str, dict] = {}  # condition_id -> market info
        # Markets we've committed capital to (survive cap check)
        self.funded_markets: set = set()

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

        # Check wallet balance and allowances
        self.orders.check_wallet_ready()

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

                # Reset open order tracking for clean re-quote
                exposure = self.risk.exposures.get(market.condition_id)
                if exposure:
                    exposure.total_open_orders_usd = 0.0

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

    def _update_spending(self):
        """Track total USD in open orders and check against spending cap.
        Uses the current active orders total — once we've placed our first
        batch of orders, we set cap_reached so the bot stops placing more.
        """
        if self.spending_cap <= 0 or not self.orders:
            return
        current_orders_usd = sum(
            info.get("cost_usd", info.get("price", 0) * info.get("size", 0))
            for info in self.orders.active_orders.values()
        )
        # Track the high-water mark of spending
        self.total_spent = max(self.total_spent, current_orders_usd)
        if self.total_spent >= self.spending_cap:
            self.cap_reached = True
            log.info(
                f"SPENDING CAP REACHED: ${self.total_spent:.2f} / ${self.spending_cap:.2f}. "
                f"No more orders will be placed. Monitoring positions and risk."
            )

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

        if self.spending_cap > 0:
            log.info(f"LIVE TEST MODE: max ${self.spending_cap:.2f} in orders, then monitor only")

        log.info("Bot is now running. Press Ctrl+C to stop.")
        log.info(f"Scan interval: {self.config.scan_interval}s")

        while self.running:
            try:
                cycle_start = time.time()

                # === Scanning for NEW markets (only if cap not reached) ===
                if not self.cap_reached and scan_counter % FULL_SCAN_EVERY == 0:
                    log.info("--- Full market scan ---")
                    markets = self.scan_and_select_markets()
                    if markets:
                        self.refresh_quotes(markets)
                        # Track these as funded markets so they get refreshed even after cap
                        for m in markets:
                            self.funded_markets.add(m.condition_id)
                        self._update_spending()
                elif self.cap_reached and scan_counter % 5 == 0:
                    log.info(f"Spending cap reached (${self.total_spent:.2f}/${self.spending_cap:.2f}) - no NEW markets, still refreshing existing")

                # === ALWAYS refresh existing markets for max Q score ===
                # Cancel+repost orders every cycle to track midpoint movement
                if scan_counter % FULL_SCAN_EVERY != 0:  # skip on full-scan cycles (already refreshed)
                    existing = [
                        info["market"]
                        for info in self.active_markets.values()
                    ]
                    if existing:
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


def run_simulation(config: BotConfig):
    """
    Full simulation: uses real market data but simulates order placement.
    Tests the entire pipeline — scanning, reward detection, market selection,
    price calculation, risk checks, and spending caps — with zero real money.
    """
    scanner = MarketScanner(config)
    risk = RiskManager(config)
    learner = Learner(config)

    # === STEP 1: Scan for markets with rewards ===
    log.info("=" * 60)
    log.info("  STEP 1: Scanning for reward-eligible markets")
    log.info("=" * 60)
    markets = scanner.scan_for_opportunities()

    if not markets:
        log.error("No eligible markets found. Check VPN and DNS settings.")
        return

    # Filter through learner
    markets = [m for m in markets if not learner.is_blacklisted(m.condition_id)]
    for m in markets:
        m.our_share_estimate = learner.score_market(
            m.condition_id, m.reward_pool, m.our_share_estimate,
            m.spread, m.orderbook_depth_yes + m.orderbook_depth_no,
        )
    markets.sort(key=lambda m: m.our_share_estimate, reverse=True)
    selected = markets[:config.max_active_markets]

    log.info("")
    log.info("=" * 60)
    log.info(f"  STEP 2: Selected {len(selected)} market(s) for LP")
    log.info("=" * 60)

    for i, m in enumerate(selected):
        log.info(f"  [{i+1}] {m.question}")
        log.info(f"      Condition: {m.condition_id[:24]}...")
        log.info(f"      Reward: ${m.reward_pool:.2f}/day | Score: {m.our_share_estimate:.2%}")  # .2f shows sub-$1 rates
        log.info(f"      Midpoint: {m.midpoint:.4f} | Spread: {m.spread:.4f}")
        log.info(f"      YES bid/ask: {m.yes_bid:.4f}/{m.yes_ask:.4f}")
        log.info(f"      NO  bid/ask: {m.no_bid:.4f}/{m.no_ask:.4f}")
        log.info(f"      Depth YES: ${m.orderbook_depth_yes:.0f} | NO: ${m.orderbook_depth_no:.0f}")
        log.info(f"      Max spread for rewards: {m.max_spread:.4f} | Min size: {m.min_size:.0f}")
        log.info(f"      Fallback market: {'YES' if m.is_fallback else 'NO'}")
        log.info("")

    # === STEP 3: Simulate order placement ===
    log.info("=" * 60)
    log.info("  STEP 3: Simulating order placement")
    log.info("=" * 60)

    total_simulated = 0.0
    sim_orders = []
    learned = learner.get_adjusted_params() or {}
    min_edge = learned.get("min_edge", config.min_edge)
    max_edge = learned.get("max_edge", config.max_edge)

    for m in selected:
        midpoint = m.midpoint
        use_undercut = m.is_fallback or m.spread > 0.06

        for level in range(config.num_price_levels):
            edge = min_edge + (max_edge - min_edge) * level / max(1, config.num_price_levels - 1)
            if edge > m.max_spread:
                log.info(f"      Skipping level {level}: edge {edge:.4f} > max_spread {m.max_spread:.4f}")
                continue

            max_per_side = config.max_exposure_per_market / 2

            # YES side price
            if use_undercut and m.yes_bid > 0 and abs(m.yes_bid - midpoint) <= 0.15:
                yes_price = round(m.yes_bid + 0.01, 4)
                yes_method = "undercut"
            else:
                yes_price = round(midpoint - edge, 4)
                yes_method = "midpoint-edge"
            yes_price = max(0.01, min(0.99, yes_price))

            # YES shares: at least min_size, capped by exposure
            yes_shares = max(m.min_size, round(config.order_size / yes_price, 2)) if yes_price > 0 else 0
            yes_cost = yes_shares * yes_price
            if yes_cost > max_per_side:
                yes_shares = round(max_per_side / yes_price, 2)
                yes_cost = yes_shares * yes_price
            yes_meets_min = yes_shares >= m.min_size

            # NO side price
            no_mid = 1 - midpoint
            if use_undercut and m.no_bid > 0 and abs(m.no_bid - no_mid) <= 0.15:
                no_price = round(m.no_bid + 0.01, 4)
                no_method = "undercut"
            else:
                no_price = round(no_mid - edge, 4)
                no_method = "midpoint-edge"
            no_price = max(0.01, min(0.99, no_price))

            # NO shares: at least min_size, capped by exposure
            no_shares = max(m.min_size, round(config.order_size / no_price, 2)) if no_price > 0 else 0
            no_cost = no_shares * no_price
            if no_cost > max_per_side:
                no_shares = round(max_per_side / no_price, 2)
                no_cost = no_shares * no_price
            no_meets_min = no_shares >= m.min_size

            # Safety check: YES bid + NO bid <= 0.98
            if yes_price + no_price > 0.98:
                excess = (yes_price + no_price) - 0.98
                yes_price = round(yes_price - excess / 2, 4)
                no_price = round(no_price - excess / 2, 4)

            # Risk check simulation
            ok_yes, reason_yes = risk.can_place_order(m.condition_id, "BUY", yes_cost)
            ok_no, reason_no = risk.can_place_order(m.condition_id, "BUY", no_cost)

            log.info(f"  Market: {m.question[:50]}")
            log.info(f"    YES BUY: {yes_shares:.0f} shares @ ${yes_price:.4f} = ${yes_cost:.2f} "
                     f"[{yes_method}] {'MEETS MIN' if yes_meets_min else 'BELOW MIN'} "
                     f"{'OK' if ok_yes else f'BLOCKED: {reason_yes}'}")
            log.info(f"    NO  BUY: {no_shares:.0f} shares @ ${no_price:.4f} = ${no_cost:.2f} "
                     f"[{no_method}] {'MEETS MIN' if no_meets_min else 'BELOW MIN'} "
                     f"{'OK' if ok_no else f'BLOCKED: {reason_no}'}")

            if ok_yes and yes_meets_min:
                risk.register_pending_order(m.condition_id, yes_cost)
                total_simulated += yes_cost
                sim_orders.append(("YES", yes_price, yes_shares, yes_cost, m.question[:40]))
            elif not yes_meets_min:
                log.info(f"    ^ YES skipped: {yes_shares:.0f} < {m.min_size:.0f} min shares")
            if ok_no and no_meets_min:
                risk.register_pending_order(m.condition_id, no_cost)
                total_simulated += no_cost
                sim_orders.append(("NO", no_price, no_shares, no_cost, m.question[:40]))
            elif not no_meets_min:
                log.info(f"    ^ NO skipped: {no_shares:.0f} < {m.min_size:.0f} min shares")

    # === STEP 4: Summary ===
    log.info("")
    log.info("=" * 60)
    log.info("  SIMULATION SUMMARY")
    log.info("=" * 60)
    log.info(f"  Markets scanned: {len(markets)} eligible")
    log.info(f"  Markets selected: {len(selected)}")
    log.info(f"  Orders simulated: {len(sim_orders)}")
    log.info(f"  Total USD committed: ${total_simulated:.2f}")
    log.info("")

    if sim_orders:
        log.info("  Simulated orders:")
        for side, price, shares, cost, q in sim_orders:
            log.info(f"    {side:3s} BUY {shares:>8.2f} @ ${price:.4f} = ${cost:.2f}  |  {q}")
    else:
        log.info("  No orders would be placed (all blocked by risk manager)")

    # Check spending cap
    spending_cap = 50.0
    if total_simulated >= spending_cap:
        log.info(f"\n  Spending cap would trigger at ${spending_cap:.2f} "
                 f"(total: ${total_simulated:.2f}) - bot would stop placing orders")
    else:
        log.info(f"\n  Under spending cap: ${total_simulated:.2f} / ${spending_cap:.2f}")

    # Check if orders would score for rewards
    log.info("")
    log.info("  Reward eligibility check:")
    for m in selected:
        yes_mid = m.midpoint
        no_mid = 1.0 - m.midpoint
        for side, price, shares, cost, q in sim_orders:
            # Use the correct midpoint for each side
            mid = yes_mid if side == "YES" else no_mid
            spread_from_mid = abs(price - mid)
            within_max = spread_from_mid <= m.max_spread
            meets_min = shares >= m.min_size
            log.info(f"    {side} @ ${price:.4f}: spread_from_mid={spread_from_mid:.4f} "
                     f"{'<=' if within_max else '>'} max_spread={m.max_spread:.4f} "
                     f"{'OK' if within_max else 'NO REWARD'} | "
                     f"size={shares:.0f} {'>=':s} min={m.min_size:.0f} "
                     f"{'OK' if meets_min else 'TOO SMALL'}")

    log.info("")
    log.info("=" * 60)
    log.info("  SIMULATION COMPLETE - No real orders placed")
    log.info("  Run with --live-test to place real orders ($10 cap)")
    log.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Polymarket LP Bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan markets without placing orders")
    parser.add_argument("--status", action="store_true",
                        help="Show current position status and exit")
    parser.add_argument("--cancel-all", action="store_true",
                        help="Cancel all open orders and exit")
    parser.add_argument("--test-order", action="store_true",
                        help="Quick test: find first eligible market and try to place one order")
    parser.add_argument("--live-test", action="store_true",
                        help="Live test: place up to $10 in orders, then stop placing new ones. "
                             "Keeps running to monitor fills and manage risk.")
    parser.add_argument("--simulate", action="store_true",
                        help="Full simulation: scan real markets, calculate prices, "
                             "simulate order placement with no real money. "
                             "Tests the entire pipeline without touching your wallet.")
    parser.add_argument("--learning", action="store_true",
                        help="Show learning engine status and exit")
    parser.add_argument("--unblacklist", type=str, default=None,
                        help="Remove a market from the blacklist (condition_id)")
    args = parser.parse_args()

    config = BotConfig()

    if args.test_order:
        errors = config.validate()
        if errors:
            for err in errors:
                log.error(f"Config error: {err}")
            return
        log.info("=== QUICK ORDER TEST ===")
        risk = RiskManager(config)
        orders = OrderManager(config, risk)
        orders.check_wallet_ready()

        # Find first eligible market quickly (check only 50 markets)
        scanner = MarketScanner(config)
        log.info("Scanning first 50 markets for a quick test...")
        import requests
        try:
            resp = requests.get(f"{config.gamma_api_url}/markets", params={
                "active": "true", "closed": "false", "limit": 50
            }, timeout=10)
            markets_data = resp.json()
        except Exception as e:
            log.error(f"Failed to fetch markets: {e}")
            return

        # Find one with valid tokens
        test_market = None
        for m in markets_data:
            tokens = m.get("clobTokenIds")
            if not tokens:
                continue
            import json as _json
            if isinstance(tokens, str):
                try:
                    tokens = _json.loads(tokens)
                except:
                    continue
            if len(tokens) >= 2:
                cid = m.get("conditionId", m.get("condition_id", ""))
                if cid:
                    test_market = {"condition_id": cid, "tokens": tokens,
                                   "question": m.get("question", "?")}
                    break

        if not test_market:
            log.error("Could not find a test market")
            return

        log.info(f"Test market: {test_market['question'][:60]}")
        log.info(f"Token YES: {test_market['tokens'][0][:20]}...")
        log.info(f"Token NO:  {test_market['tokens'][1][:20]}...")

        # Try to place a tiny $1 BUY order at $0.10 (will sit on book, unlikely to fill)
        token_id = test_market["tokens"][0]
        test_price = 0.10
        test_size = 10.0  # 10 shares at $0.10 = $1
        log.info(f"Placing test order: BUY 10 shares YES @ $0.10 (=$1.00)")
        oid = orders.place_limit_order(token_id, "BUY", test_price, test_size,
                                       test_market["condition_id"])
        if oid:
            log.info(f"ORDER SUCCESS! ID: {oid}")
            log.info("Cancelling test order...")
            orders.cancel_order(oid)
            log.info("Test complete - orders are working!")
        else:
            log.error("Order failed - check errors above")
        return

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
        # Cancel all via API (catches orders the bot may not be tracking)
        try:
            orders.client.cancel_all()
            log.info("All open orders cancelled via API.")
        except Exception as e:
            log.error(f"cancel_all error: {e}")
        orders.active_orders.clear()
        return

    if args.simulate:
        log.info("=" * 60)
        log.info("  SIMULATION MODE - NO REAL MONEY")
        log.info("=" * 60)
        config.order_size = 10.0
        config.num_price_levels = 1
        config.max_active_markets = 3
        config.max_exposure_per_market = 50.0
        run_simulation(config)
        return

    if args.live_test:
        # Override config for safe $50 test
        config.order_size = 10.0
        config.num_price_levels = 1
        config.max_active_markets = 1
        config.max_exposure_per_market = 50.0
        config.max_loss_per_position = 5.0
        config.emergency_loss_threshold = 10.0
        config.portfolio_stop_loss = 25.0
        log.info("LIVE TEST: $50 cap | 1 market | $5 max loss | $25 stop-loss")
        bot = PolymarketLPBot(config, spending_cap=50.0)
    else:
        bot = PolymarketLPBot(config, dry_run=args.dry_run)
    bot.run()


if __name__ == "__main__":
    main()
