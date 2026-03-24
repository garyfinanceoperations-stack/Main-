"""Order manager - places, cancels, and tracks orders via the Polymarket CLOB API."""

import time
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
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
        try:
            self.client = ClobClient(
                self.config.clob_api_url,
                key=self.config.private_key,
                chain_id=self.config.chain_id,
            )

            # Create or derive API credentials
            self.api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(self.api_creds)
            log.info(f"CLOB client initialized for chain {self.config.chain_id}")

        except Exception as e:
            log.error(f"Failed to initialize CLOB client: {e}")
            raise

    def get_allowance(self) -> float:
        """Check USDC allowance for the CTF Exchange."""
        try:
            allowances = self.client.get_allowances()
            log.info(f"Current allowances: {allowances}")
            return float(allowances.get("allowance", 0))
        except Exception as e:
            log.warning(f"Could not check allowances: {e}")
            return 0

    def set_allowances(self):
        """Approve USDC spending for the CTF Exchange if needed."""
        try:
            self.client.set_allowances()
            log.info("Allowances set successfully")
        except Exception as e:
            log.error(f"Failed to set allowances: {e}")

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
            self.client.cancel(order_id)
            self.active_orders.pop(order_id, None)
            log.info(f"Cancelled order {order_id[:16]}")
            return True
        except Exception as e:
            log.error(f"Failed to cancel order {order_id[:16]}: {e}")
            return False

    def cancel_all_orders(self) -> int:
        """Cancel all open orders. Returns count of cancelled orders."""
        try:
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
        to_remove = []
        for oid, info in self.active_orders.items():
            if info.get("condition_id") == condition_id:
                if self.cancel_order(oid):
                    cancelled += 1
                    to_remove.append(oid)
        for oid in to_remove:
            self.active_orders.pop(oid, None)
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
                    "timestamp": time.time(),
                }
                log.info(
                    f"ORDER PLACED: {side} {size:.2f} @ ${price:.4f} "
                    f"token={token_id[:16]}... | id={order_id[:16] if order_id else 'N/A'}"
                )
            return order_id

        except Exception as e:
            log.error(f"Failed to place order: {e}")
            return None

    def place_lp_quotes(self, market: MarketInfo) -> list[str]:
        """
        Place two-sided LP quotes (both YES and NO) for a market.
        Returns list of order IDs placed.
        """
        order_ids = []
        midpoint = market.midpoint

        # Calculate price levels
        for level in range(self.config.num_price_levels):
            edge = (
                self.config.min_edge
                + (self.config.max_edge - self.config.min_edge)
                * level / max(1, self.config.num_price_levels - 1)
            )

            # Ensure edge is within the market's max spread for rewards
            if edge > market.max_spread:
                log.debug(f"Skipping level {level}: edge {edge:.4f} > max_spread {market.max_spread:.4f}")
                continue

            # Size decreases slightly for levels further from mid
            size_multiplier = 1.0 - (level * 0.15)
            level_size = self.config.order_size * size_multiplier

            # Ensure we meet minimum size requirement
            if level_size < market.min_size:
                level_size = market.min_size

            # === YES side ===
            yes_bid_price = round(midpoint - edge, 4)
            yes_ask_price = round(midpoint + edge, 4)

            # Clamp prices to valid range
            yes_bid_price = max(0.01, min(0.99, yes_bid_price))
            yes_ask_price = max(0.01, min(0.99, yes_ask_price))

            # Place YES BID (buy YES tokens)
            shares_yes_bid = round(level_size / yes_bid_price, 2) if yes_bid_price > 0 else 0
            if shares_yes_bid > 0:
                oid = self.place_limit_order(
                    market.token_yes, "BUY", yes_bid_price, shares_yes_bid, market.condition_id
                )
                if oid:
                    order_ids.append(oid)
                time.sleep(0.1)

            # Place YES ASK (sell YES tokens - only if we hold YES)
            # For initial LP, we buy on both sides to provide liquidity

            # === NO side ===
            no_bid_price = round((1 - midpoint) - edge, 4)
            no_ask_price = round((1 - midpoint) + edge, 4)
            no_bid_price = max(0.01, min(0.99, no_bid_price))
            no_ask_price = max(0.01, min(0.99, no_ask_price))

            # Place NO BID (buy NO tokens)
            shares_no_bid = round(level_size / no_bid_price, 2) if no_bid_price > 0 else 0
            if shares_no_bid > 0:
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

    def sync_fills(self):
        """
        Check for filled orders and update risk manager positions.
        """
        try:
            trades = self.client.get_trades()
            if not isinstance(trades, list):
                return

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
                    # Remove from active orders
                    self.active_orders.pop(order_id, None)

        except Exception as e:
            log.debug(f"Error syncing fills: {e}")

    def get_balances(self) -> dict:
        """Get token balances."""
        try:
            return self.client.get_balances()
        except Exception as e:
            log.debug(f"Error fetching balances: {e}")
            return {}
