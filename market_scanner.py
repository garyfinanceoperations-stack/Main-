"""Market scanner - finds eligible markets for LP based on reward pools, spreads, and volume."""

import time
import requests
from dataclasses import dataclass, field
from config import BotConfig
from logger import log


@dataclass
class MarketInfo:
    """Represents a scanned market that passes our filters."""
    condition_id: str
    question: str
    token_yes: str
    token_no: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    midpoint: float
    spread: float
    reward_pool: float  # daily rewards in USD
    max_spread: float   # max spread for reward eligibility
    min_size: float     # min order size for reward eligibility
    volume_24h: float
    orderbook_depth_yes: float  # total $ on yes book
    orderbook_depth_no: float   # total $ on no book
    our_share_estimate: float   # estimated % of rewards we'd capture
    neg_risk: bool = False
    active: bool = True
    outcomes: list = field(default_factory=lambda: ["Yes", "No"])


class MarketScanner:
    """Scans Polymarket for eligible LP markets."""

    def __init__(self, config: BotConfig):
        self.config = config
        self.clob_url = config.clob_api_url
        self.gamma_url = config.gamma_api_url
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def get_all_markets(self) -> list[dict]:
        """Fetch all active markets from the Gamma API (richer data than CLOB)."""
        markets = []
        offset = 0
        limit = 100

        while True:
            try:
                resp = self._session.get(
                    f"{self.gamma_url}/markets",
                    params={
                        "limit": limit,
                        "offset": offset,
                        "active": True,
                        "closed": False,
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                markets.extend(batch)
                offset += limit
                if len(batch) < limit:
                    break
                time.sleep(0.2)  # rate limiting
            except Exception as e:
                log.error(f"Error fetching markets at offset {offset}: {e}")
                break

        log.info(f"Fetched {len(markets)} active markets from Gamma API")
        return markets

    def get_rewards_markets(self) -> list[dict]:
        """Fetch markets that have active reward pools."""
        # Try multiple endpoints - Polymarket API evolves frequently

        # Attempt 1: CLOB /markets with rewards included
        try:
            resp = self._session.get(
                f"{self.clob_url}/markets",
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                data = data.get("data", data.get("markets", []))
            if isinstance(data, list) and data:
                rewarded = [m for m in data if self._has_rewards(m)]
                if rewarded:
                    log.info(f"Found {len(rewarded)} reward markets via CLOB /markets")
                    return rewarded
        except Exception as e:
            log.debug(f"CLOB /markets: {e}")

        # Attempt 2: CLOB /rewards/markets (legacy)
        try:
            resp = self._session.get(
                f"{self.clob_url}/rewards/markets",
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if data:
                log.info(f"Found {len(data)} reward markets via CLOB /rewards/markets")
                return data
        except Exception as e:
            log.debug(f"CLOB /rewards/markets: {e}")

        # Attempt 3: Gamma API - most reliable fallback
        try:
            resp = self._session.get(
                f"{self.gamma_url}/markets",
                params={
                    "active": True,
                    "closed": False,
                    "limit": 200,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            rewarded = [m for m in data if self._has_rewards(m)]
            if rewarded:
                log.info(f"Found {len(rewarded)} reward markets via Gamma API")
                return rewarded
            # If no rewarded markets found, return all active (user can filter)
            log.warning("No reward fields found - returning all active markets")
            return data
        except Exception as e:
            log.error(f"Gamma API also failed: {e}")

        # Attempt 4: Gamma API events endpoint
        try:
            resp = self._session.get(
                f"{self.gamma_url}/events",
                params={"active": True, "closed": False, "limit": 100},
                timeout=30,
            )
            resp.raise_for_status()
            events = resp.json()
            markets = []
            for event in events:
                for m in event.get("markets", []):
                    m["question"] = m.get("question", event.get("title", ""))
                    markets.append(m)
            if markets:
                log.info(f"Found {len(markets)} markets via Gamma /events")
                return markets
        except Exception as e:
            log.error(f"All market fetch attempts failed: {e}")

        return []

    def _has_rewards(self, market: dict) -> bool:
        """Check if a market dictionary indicates it has active rewards."""
        # CLOB API nests under "rewards" object with "rates" inside
        rewards_obj = market.get("rewards", {})
        if isinstance(rewards_obj, dict):
            rates = rewards_obj.get("rates")
            if rates:
                try:
                    # rates can be a list of dicts or a single value
                    if isinstance(rates, list):
                        return any(float(r.get("rewards_daily_rate", 0)) > 0 for r in rates)
                    return float(rates) > 0
                except (ValueError, TypeError):
                    pass

        # Gamma API / flat field fallbacks
        for key in ["rewardsDaily", "rewards_daily_rate",
                     "rewardsDailyRate", "liquidityRewards", "clobRewards"]:
            val = market.get(key)
            if val:
                try:
                    if isinstance(val, dict):
                        return bool(val)
                    return float(val) > 0
                except (ValueError, TypeError):
                    continue
        return False

    def _get_reward_amount(self, market: dict) -> float:
        """Extract the daily reward amount from a market dict."""
        # CLOB API: nested rewards.rates structure
        rewards_obj = market.get("rewards", {})
        if isinstance(rewards_obj, dict):
            rates = rewards_obj.get("rates")
            if rates:
                try:
                    if isinstance(rates, list):
                        return sum(float(r.get("rewards_daily_rate", 0)) for r in rates)
                    return float(rates)
                except (ValueError, TypeError):
                    pass

        # Gamma API flat fields
        for key in ["rewardsDaily", "rewards_daily_rate",
                     "rewardsDailyRate", "liquidityRewards", "rewardsAmount"]:
            val = market.get(key)
            if val:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue
        return 0.0

    def _get_max_spread(self, market: dict) -> float:
        """Get the max spread for reward eligibility."""
        # CLOB API nested
        rewards_obj = market.get("rewards", {})
        if isinstance(rewards_obj, dict):
            for key in ["max_spread", "maxSpread"]:
                val = rewards_obj.get(key)
                if val:
                    try:
                        return float(val)
                    except (ValueError, TypeError):
                        pass

        # Flat field fallbacks
        for key in ["rewardsMaxSpread", "rewards_max_spread",
                     "maxIncentiveSpread", "max_incentive_spread"]:
            val = market.get(key)
            if val:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue
        return 0.03  # default 3 cents

    def _get_min_size(self, market: dict) -> float:
        """Get the minimum order size for reward eligibility."""
        # CLOB API nested
        rewards_obj = market.get("rewards", {})
        if isinstance(rewards_obj, dict):
            for key in ["min_size", "minSize"]:
                val = rewards_obj.get(key)
                if val:
                    try:
                        return float(val)
                    except (ValueError, TypeError):
                        pass

        # Flat field fallbacks
        for key in ["rewardsMinSize", "rewards_min_size",
                     "minIncentiveSize", "min_incentive_size"]:
            val = market.get(key)
            if val:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue
        return 5.0  # default $5

    def get_orderbook(self, token_id: str) -> dict:
        """Fetch the order book for a token."""
        try:
            resp = self._session.get(
                f"{self.clob_url}/book",
                params={"token_id": token_id},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.debug(f"Error fetching book for {token_id[:16]}...: {e}")
            return {"bids": [], "asks": []}

    def get_midpoint(self, token_id: str) -> float:
        """Get the midpoint price for a token."""
        try:
            resp = self._session.get(
                f"{self.clob_url}/midpoint",
                params={"token_id": token_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("mid", 0.5))
        except Exception:
            return 0.5

    def get_spread(self, token_id: str) -> float:
        """Get the spread for a token."""
        try:
            resp = self._session.get(
                f"{self.clob_url}/spread",
                params={"token_id": token_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("spread", 0))
        except Exception:
            return 1.0  # assume wide spread if can't fetch

    def _calculate_book_depth(self, book: dict, side: str) -> float:
        """Calculate total USD depth on one side of the book."""
        total = 0.0
        orders = book.get(side, [])
        for order in orders:
            price = float(order.get("price", 0))
            size = float(order.get("size", 0))
            total += price * size
        return total

    def _check_thin_book(self, book: dict) -> bool:
        """Check if the order book has thin liquidity (< $50 per price level)."""
        for side in ["bids", "asks"]:
            orders = book.get(side, [])
            # Group by price level and check each is under our threshold
            price_levels = {}
            for order in orders:
                price = order.get("price", "0")
                size = float(order.get("size", 0))
                price_levels[price] = price_levels.get(price, 0) + size * float(price)

            for _price, dollar_value in price_levels.items():
                if dollar_value > self.config.max_orderbook_depth:
                    return False  # too thick, dominated by bigger players
        return True

    def _estimate_reward_share(self, market: dict, book_yes: dict, book_no: dict) -> float:
        """Estimate what share of rewards we'd capture with our order sizes."""
        # Calculate existing Q scores from the book
        max_spread = self._get_max_spread(market)
        midpoint = 0.5  # will be refined

        # Get midpoint from book
        yes_bids = book_yes.get("bids", [])
        yes_asks = book_yes.get("asks", [])
        if yes_bids and yes_asks:
            best_bid = float(yes_bids[0]["price"])
            best_ask = float(yes_asks[0]["price"])
            midpoint = (best_bid + best_ask) / 2

        # Calculate existing total Q score from all orders in the book
        existing_q = 0.0
        for side_orders in [yes_bids, yes_asks]:
            for order in side_orders:
                price = float(order["price"])
                size = float(order["size"])
                order_spread = abs(price - midpoint)
                if order_spread <= max_spread:
                    score = ((max_spread - order_spread) / max_spread) ** 2 * size
                    existing_q += score

        # Estimate our Q score
        our_q = 0.0
        for level in range(self.config.num_price_levels):
            edge = self.config.min_edge + (self.config.max_edge - self.config.min_edge) * level / max(1, self.config.num_price_levels - 1)
            if edge <= max_spread:
                score = ((max_spread - edge) / max_spread) ** 2 * self.config.order_size
                our_q += score * 2  # both sides

        total_q = existing_q + our_q
        if total_q == 0:
            return 1.0  # empty book, we'd get all rewards
        return our_q / total_q

    def scan_for_opportunities(self) -> list[MarketInfo]:
        """Main scan: find markets meeting all our criteria."""
        eligible = []

        # Get markets with rewards
        all_markets = self.get_rewards_markets()
        if not all_markets:
            log.warning("No reward markets found, trying full market list")
            all_markets = self.get_all_markets()

        log.info(f"Scanning {len(all_markets)} markets for LP opportunities...")

        for market in all_markets:
            try:
                # Extract reward amount
                reward_amount = self._get_reward_amount(market)
                if reward_amount < self.config.min_reward_pool:
                    continue

                # Get token IDs
                tokens = market.get("clobTokenIds", market.get("clob_token_ids", []))
                if not tokens or len(tokens) < 2:
                    continue

                token_yes = tokens[0]
                token_no = tokens[1]
                condition_id = market.get("conditionId", market.get("condition_id", ""))

                # Get order books for both sides
                book_yes = self.get_orderbook(token_yes)
                book_no = self.get_orderbook(token_no)
                time.sleep(0.15)  # rate limiting

                # Check thin book condition
                if not self._check_thin_book(book_yes) or not self._check_thin_book(book_no):
                    log.debug(f"Skipping {condition_id[:16]}: book too thick")
                    continue

                # Get best bid/ask for YES
                yes_bids = book_yes.get("bids", [])
                yes_asks = book_yes.get("asks", [])
                if not yes_bids or not yes_asks:
                    log.debug(f"Skipping {condition_id[:16]}: empty YES book")
                    continue

                yes_bid = float(yes_bids[0]["price"])
                yes_ask = float(yes_asks[0]["price"])

                # Get best bid/ask for NO
                no_bids = book_no.get("bids", [])
                no_asks = book_no.get("asks", [])
                if not no_bids or not no_asks:
                    log.debug(f"Skipping {condition_id[:16]}: empty NO book")
                    continue

                no_bid = float(no_bids[0]["price"])
                no_ask = float(no_asks[0]["price"])

                # Calculate spread
                yes_spread = yes_ask - yes_bid
                no_spread = no_ask - no_bid
                avg_spread = (yes_spread + no_spread) / 2

                # Filter: spread must be < MAX_SPREAD_GAP (5 cents)
                if avg_spread > self.config.max_spread_gap:
                    log.debug(f"Skipping {condition_id[:16]}: spread too wide ({avg_spread:.4f})")
                    continue

                midpoint = (yes_bid + yes_ask) / 2

                # Calculate book depth
                depth_yes = self._calculate_book_depth(book_yes, "bids") + self._calculate_book_depth(book_yes, "asks")
                depth_no = self._calculate_book_depth(book_no, "bids") + self._calculate_book_depth(book_no, "asks")

                # Estimate our reward share
                share = self._estimate_reward_share(market, book_yes, book_no)

                # Filter: we want at least MIN_REWARD_SHARE_TARGET
                if share < self.config.min_reward_share_target:
                    log.debug(f"Skipping {condition_id[:16]}: reward share too low ({share:.1%})")
                    continue

                volume = float(market.get("volume", market.get("volume24hr", 0)) or 0)

                info = MarketInfo(
                    condition_id=condition_id,
                    question=market.get("question", market.get("title", "Unknown"))[:100],
                    token_yes=token_yes,
                    token_no=token_no,
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    no_bid=no_bid,
                    no_ask=no_ask,
                    midpoint=midpoint,
                    spread=avg_spread,
                    reward_pool=reward_amount,
                    max_spread=self._get_max_spread(market),
                    min_size=self._get_min_size(market),
                    volume_24h=volume,
                    orderbook_depth_yes=depth_yes,
                    orderbook_depth_no=depth_no,
                    our_share_estimate=share,
                    neg_risk=bool(market.get("negRisk", market.get("neg_risk", False))),
                )
                eligible.append(info)
                log.info(
                    f"ELIGIBLE: {info.question[:60]} | "
                    f"Reward: ${info.reward_pool:.2f}/day | "
                    f"Spread: {info.spread:.4f} | "
                    f"Our share: {info.our_share_estimate:.1%}"
                )

            except Exception as e:
                log.debug(f"Error processing market: {e}")
                continue

        # Sort by estimated reward share * reward pool (expected daily earnings)
        eligible.sort(
            key=lambda m: m.our_share_estimate * m.reward_pool,
            reverse=True,
        )

        log.info(f"Found {len(eligible)} eligible markets for LP")
        return eligible
