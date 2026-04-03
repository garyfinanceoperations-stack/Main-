"""Order manager - places, cancels, and tracks orders via the Polymarket CLOB API."""

import time
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL

from config import BotConfig
from market_scanner import MarketInfo
from risk_manager import RiskManager
from logger import log


class OrderManager:
    """Manages order lifecycle on Polymarket CLOB."""

    def __init__(self, config: BotConfig, risk_manager: RiskManager):
        self.config = config
        self.risk = risk_manager
        self.client: ClobClient = None
        self.api_creds = None
        self.active_orders: dict[str, dict] = {}  # order_id -> order info
        self._initialize_client()

    def _initialize_client(self):
        """Initialize the CLOB client with authentication."""
        # Build client kwargs
        kwargs = {
            "host": self.config.clob_api_url,
            "key": self.config.private_key,
            "chain_id": self.config.chain_id,
        }

        # Add proxy wallet support if configured
        if self.config.funder:
            kwargs["funder"] = self.config.funder
            log.info(f"Using proxy wallet (funder): {self.config.funder}")

        if self.config.signature_type > 0:
            kwargs["signature_type"] = self.config.signature_type
            sig_names = {0: "EOA", 1: "Poly Proxy", 2: "Gnosis Safe"}
            log.info(f"Signature type: {sig_names.get(self.config.signature_type, self.config.signature_type)}")

        self.client = ClobClient(**kwargs)

        # Create or derive API credentials with retry
        max_retries = 4
        for attempt in range(1, max_retries + 1):
            try:
                self.api_creds = self.client.create_or_derive_api_creds()
                self.client.set_api_creds(self.api_creds)
                log.info(f"CLOB client initialized for chain {self.config.chain_id}")
                return
            except Exception as e:
                wait = 2 ** attempt  # 2, 4, 8, 16 seconds
                log.warning(f"API connection attempt {attempt}/{max_retries} failed: {e}")
                if attempt < max_retries:
                    log.info(f"Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    log.error(f"Failed to connect after {max_retries} attempts. Check your internet and try again.")
                    raise

    def get_balance_and_allowance(self) -> dict:
        """Check USDC balance and allowance for the CTF Exchange."""
        try:
            if hasattr(self.client, "get_balance_allowance"):
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                result = self.client.get_balance_allowance(params)
                log.info(f"Balance/allowance: {result}")
                return result if isinstance(result, dict) else {}
        except Exception as e:
            log.warning(f"get_balance_allowance() error: {e}")
        return {}

    def update_balance_and_allowance(self) -> bool:
        """Approve USDC spending for the CTF Exchange."""
        try:
            if hasattr(self.client, "update_balance_allowance"):
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                result = self.client.update_balance_allowance(params)
                log.info(f"update_balance_allowance() returned: {result}")
                return True
        except Exception as e:
            log.warning(f"update_balance_allowance() error: {e}")
        return False

    def check_wallet_ready(self) -> bool:
        """Check wallet balance and allowance, log results for debugging."""
        # Step 1: Check current balance and allowance
        log.info("Checking wallet balance and allowance...")
        ba = self.get_balance_and_allowance()

        # Step 2: Try to update/approve allowance
        if not ba or float(ba.get("allowance", 0)) == 0:
            log.info("Setting allowance approval for CTF Exchange...")
            self.update_balance_and_allowance()
            # Re-check after approval
            ba = self.get_balance_and_allowance()

        if ba:
            log.info(f"Wallet status: {ba}")
        else:
            log.info(
                "Could not read balance/allowance (normal for some setups). "
                "If you can trade on polymarket.com, your funds should work."
            )

        log.info("Wallet check complete — will attempt to place orders.")
        return True

    def get_open_orders(self) -> list[dict]:
        """Fetch all open orders for our account."""
        try:
            orders = self.client.get_orders()
            return orders if isinstance(orders, list) else []
        except Exception as e:
            log.error(f"Error fetching open orders: {e}")
            return []

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order."""
        try:
            info = self.active_orders.get(order_id)
            self.client.cancel(order_id)
            if info:
                # Unregister the order cost from risk tracking
                cost = info.get("cost_usd", info.get("price", 0) * info.get("size", 0))
                self.risk.unregister_pending_order(info["condition_id"], cost)
            self.active_orders.pop(order_id, None)
            log.info(f"Cancelled order {order_id[:16]}")
            return True
        except Exception as e:
            log.error(f"Failed to cancel order {order_id[:16]}: {e}")
            return False

    def cancel_all_orders(self) -> int:
        """Cancel all open orders. Returns count of cancelled orders."""
        try:
            # Unregister all tracked order costs
            for oid, info in self.active_orders.items():
                cost = info.get("cost_usd", info.get("price", 0) * info.get("size", 0))
                self.risk.unregister_pending_order(info["condition_id"], cost)
            self.client.cancel_all()
            count = len(self.active_orders)
            self.active_orders.clear()
            log.info(f"Cancelled all orders ({count} tracked)")
            return count
        except Exception as e:
            log.error(f"Failed to cancel all orders: {e}")
            return 0

    def cancel_market_orders(self, condition_id: str) -> int:
        """Cancel all orders for a specific market."""
        cancelled = 0
        to_cancel = [
            (oid, info) for oid, info in self.active_orders.items()
            if info.get("condition_id") == condition_id
        ]
        for oid, info in to_cancel:
            try:
                self.client.cancel(oid)
                cost = info.get("cost_usd", info.get("price", 0) * info.get("size", 0))
                self.risk.unregister_pending_order(condition_id, cost)
                self.active_orders.pop(oid, None)
                cancelled += 1
            except Exception as e:
                log.error(f"Failed to cancel order {oid[:16]}: {e}")
        return cancelled

    def place_limit_order(self, token_id: str, side: str, price: float,
                          size: float, condition_id: str) -> str | None:
        """
        Place a single limit order.
        side: BUY or SELL
        Returns order_id or None on failure.
        """
        # Pre-trade risk check
        cost_usd = price * size
        ok, reason = self.risk.can_place_order(condition_id, side, cost_usd)
        if not ok:
            log.warning(f"Order blocked by risk manager: {reason}")
            return None

        # Register pending cost so the next order in the same cycle sees it
        self.risk.register_pending_order(condition_id, cost_usd)

        try:
            order_args = OrderArgs(
                price=price,
                size=size,
                side=BUY if side == "BUY" else SELL,
                token_id=token_id,
            )
            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order, OrderType.GTC)

            order_id = None
            if isinstance(resp, dict):
                order_id = resp.get("orderID", resp.get("id"))
                if resp.get("success") is False:
                    log.warning(f"Order rejected: {resp.get('errorMsg', 'unknown')}")
                    # Unregister since order didn't go through
                    self.risk.unregister_pending_order(condition_id, cost_usd)
                    return None
            elif isinstance(resp, str):
                order_id = resp

            if order_id:
                self.active_orders[order_id] = {
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "side": side,
                    "price": price,
                    "size": size,
                    "cost_usd": cost_usd,
                    "timestamp": time.time(),
                }
                log.info(
                    f"ORDER PLACED: {side} {size:.2f} @ ${price:.4f} "
                    f"(${cost_usd:.2f}) token={token_id[:16]}... | id={order_id[:16] if order_id else 'N/A'}"
                )
            else:
                # No order_id returned - unregister
                self.risk.unregister_pending_order(condition_id, cost_usd)
            return order_id

        except Exception as e:
            log.error(f"Failed to place order: {e}")
            # Unregister since order failed
            self.risk.unregister_pending_order(condition_id, cost_usd)
            return None

    def place_lp_quotes(self, market: MarketInfo,
                        learned_params: dict | None = None) -> list[str]:
        """
        Place two-sided LP quotes (both YES and NO) for a market.

        Strategy:
        - Normal markets: place at midpoint ± edge
        - Fallback/wide-spread markets: post 1c better than best existing order
        - Always maintain >= 2c gap between YES and NO prices

        learned_params can override min_edge, max_edge, order_size_multiplier.
        Returns list of order IDs placed.
        """
        order_ids = []
        midpoint = market.midpoint
        lp = learned_params or {}

        min_edge = lp.get("min_edge", self.config.min_edge)
        max_edge = lp.get("max_edge", self.config.max_edge)
        size_mult = lp.get("order_size_multiplier", 1.0)

        # For fallback markets or wide spreads: post 1c better than best order
        use_undercut = market.is_fallback or market.spread > 0.06

        # Calculate price levels
        for level in range(self.config.num_price_levels):
            edge = (
                min_edge
                + (max_edge - min_edge)
                * level / max(1, self.config.num_price_levels - 1)
            )

            # Ensure edge is within the market's max spread for rewards
            if edge > market.max_spread:
                log.debug(f"Skipping level {level}: edge {edge:.4f} > max_spread {market.max_spread:.4f}")
                continue

            # Size: ensure we meet min_size (in shares) for reward eligibility
            # but never exceed exposure cap per side
            max_per_side = self.config.max_exposure_per_market / 2

            # === YES side ===
            if use_undercut and market.yes_bid > 0:
                # Post 1 cent better than the current best bid
                # But only if the bid is within 15c of midpoint (ignore dust bids)
                if abs(market.yes_bid - midpoint) <= 0.15:
                    yes_bid_price = round(market.yes_bid + 0.01, 4)
                else:
                    yes_bid_price = round(midpoint - edge, 4)
            else:
                yes_bid_price = round(midpoint - edge, 4)

            yes_ask_price = round(midpoint + edge, 4)

            # Clamp prices to valid range
            yes_bid_price = max(0.01, min(0.99, yes_bid_price))
            yes_ask_price = max(0.01, min(0.99, yes_ask_price))

            # Calculate YES shares: at least min_size, capped by exposure limit
            shares_yes_bid = max(market.min_size, round(self.config.order_size / yes_bid_price, 2)) if yes_bid_price > 0 else 0
            yes_cost = shares_yes_bid * yes_bid_price
            # Cap by exposure limit
            if yes_cost > max_per_side:
                shares_yes_bid = round(max_per_side / yes_bid_price, 2)
                yes_cost = shares_yes_bid * yes_bid_price
            # Skip if we can't meet min shares within budget
            if shares_yes_bid < market.min_size:
                log.warning(f"  YES: can't meet {market.min_size:.0f} min shares within ${max_per_side:.0f} budget, skipping")
                shares_yes_bid = 0

            if shares_yes_bid > 0:
                log.info(f"  YES: {shares_yes_bid:.0f} shares @ ${yes_bid_price:.4f} = ${yes_cost:.2f} "
                         f"(min: {market.min_size:.0f} shares)")
                oid = self.place_limit_order(
                    market.token_yes, "BUY", yes_bid_price, shares_yes_bid, market.condition_id
                )
                if oid:
                    order_ids.append(oid)
                time.sleep(0.1)

            # === NO side ===
            no_mid = 1 - midpoint
            if use_undercut and market.no_bid > 0:
                # Only undercut if bid is within 15c of NO midpoint (ignore dust bids)
                if abs(market.no_bid - no_mid) <= 0.15:
                    no_bid_price = round(market.no_bid + 0.01, 4)
                else:
                    no_bid_price = round(no_mid - edge, 4)
            else:
                no_bid_price = round(no_mid - edge, 4)
            no_ask_price = round((1 - midpoint) + edge, 4)
            no_bid_price = max(0.01, min(0.99, no_bid_price))
            no_ask_price = max(0.01, min(0.99, no_ask_price))

            # Safety: YES bid + NO bid must be <= $0.98 (2c gap minimum)
            if yes_bid_price + no_bid_price > 0.98:
                # Scale both down equally to maintain gap
                excess = (yes_bid_price + no_bid_price) - 0.98
                yes_bid_price = round(yes_bid_price - excess / 2, 4)
                no_bid_price = round(no_bid_price - excess / 2, 4)
                log.debug(f"Adjusted prices for 2c gap: YES={yes_bid_price} NO={no_bid_price}")

            # Calculate NO shares: at least min_size, capped by exposure limit
            shares_no_bid = max(market.min_size, round(self.config.order_size / no_bid_price, 2)) if no_bid_price > 0 else 0
            no_cost = shares_no_bid * no_bid_price
            # Cap by exposure limit
            if no_cost > max_per_side:
                shares_no_bid = round(max_per_side / no_bid_price, 2)
                no_cost = shares_no_bid * no_bid_price
            # Skip if we can't meet min shares within budget
            if shares_no_bid < market.min_size:
                log.warning(f"  NO: can't meet {market.min_size:.0f} min shares within ${max_per_side:.0f} budget, skipping")
                shares_no_bid = 0

            if shares_no_bid > 0:
                log.info(f"  NO:  {shares_no_bid:.0f} shares @ ${no_bid_price:.4f} = ${no_cost:.2f} "
                         f"(min: {market.min_size:.0f} shares)")
                oid = self.place_limit_order(
                    market.token_no, "BUY", no_bid_price, shares_no_bid, market.condition_id
                )
                if oid:
                    order_ids.append(oid)
                time.sleep(0.1)

        log.info(
            f"Placed {len(order_ids)} LP orders for market {market.condition_id[:16]} "
            f"({market.question[:40]})"
        )
        return order_ids

    def market_sell(self, token_id: str, size: float, condition_id: str,
                    partial: bool = True) -> str | None:
        """
        Market sell a position. If partial=True, only sell enough to cap losses.
        Uses FOK (fill-or-kill) at best available price.
        """
        try:
            # Get current best bid to determine executable price
            book = self.client.get_order_book(token_id)
            bids = book.get("bids", []) if isinstance(book, dict) else []

            if not bids:
                log.warning(f"No bids available to sell {token_id[:16]} - no liquidity!")
                return None

            # Find how much liquidity is available
            available_liquidity = 0.0
            sell_price = 0.0
            for bid in bids:
                bid_price = float(bid["price"])
                bid_size = float(bid["size"])
                available_liquidity += bid_size
                if available_liquidity >= size:
                    sell_price = bid_price
                    break
                sell_price = bid_price

            if available_liquidity < size and partial:
                # Only sell what liquidity allows if partial
                actual_size = min(size, available_liquidity * 0.9)  # 90% of available
                log.warning(
                    f"Partial sell: only {actual_size:.2f} of {size:.2f} "
                    f"(liquidity: {available_liquidity:.2f})"
                )
                size = actual_size

            if size <= 0 or sell_price <= 0:
                log.warning("Cannot execute market sell - no valid price/size")
                return None

            # Place aggressive sell slightly below best bid for fast fill
            sell_price = round(sell_price * 0.995, 4)  # 0.5% discount for urgency

            order_args = OrderArgs(
                price=sell_price,
                size=round(size, 2),
                side=SELL,
                token_id=token_id,
            )
            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order, OrderType.GTC)

            order_id = None
            if isinstance(resp, dict):
                order_id = resp.get("orderID", resp.get("id"))
            elif isinstance(resp, str):
                order_id = resp

            log.warning(
                f"MARKET SELL: {size:.2f} shares of {token_id[:16]} @ ${sell_price:.4f}"
            )
            return order_id

        except Exception as e:
            log.error(f"Market sell failed for {token_id[:16]}: {e}")
            return None

    def emergency_dump(self, token_id: str, size: float, condition_id: str) -> str | None:
        """
        Emergency full position dump - sells at any available price.
        Only called when losses exceed emergency threshold.
        """
        log.critical(f"EMERGENCY DUMP: {size:.2f} shares of {token_id[:16]}")
        return self.market_sell(token_id, size, condition_id, partial=False)

    def sync_fills(self) -> list[dict]:
        """
        Check for filled orders and update risk manager positions.
        Returns list of fill dicts for the learner.
        """
        fills = []
        try:
            trades = self.client.get_trades()
            if not isinstance(trades, list):
                return fills

            for trade in trades:
                order_id = trade.get("order_id", trade.get("orderID", ""))
                if order_id in self.active_orders:
                    info = self.active_orders[order_id]
                    fill_price = float(trade.get("price", info["price"]))
                    fill_size = float(trade.get("size", info["size"]))

                    side = "YES" if info["token_id"] == info.get("token_yes") else "NO"
                    # Determine side from our tracking
                    if info["side"] == "BUY":
                        self.risk.record_fill(
                            info["condition_id"],
                            info["token_id"],
                            side,
                            fill_price,
                            fill_size,
                            order_id,
                        )
                        fills.append({
                            "condition_id": info["condition_id"],
                            "side": side,
                            "price": fill_price,
                            "size": fill_size,
                            "edge": abs(fill_price - 0.5),  # approximate edge
                        })
                    # Remove from active orders
                    self.active_orders.pop(order_id, None)

        except Exception as e:
            log.debug(f"Error syncing fills: {e}")

        return fills

    def get_balances(self) -> dict:
        """Get token balances."""
        try:
            return self.client.get_balances()
        except Exception as e:
            log.debug(f"Error fetching balances: {e}")
            return {}
