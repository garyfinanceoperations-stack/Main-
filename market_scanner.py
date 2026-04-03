"""Market scanner - finds eligible markets for LP based on reward pools, spreads, and volume."""

import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    orderbook_depth_yes: float  # total $ on yes book near mid
    orderbook_depth_no: float   # total $ on no book near mid
    our_share_estimate: float   # estimated % of rewards we'd capture
    neg_risk: bool = False
    active: bool = True
    outcomes: list = field(default_factory=lambda: ["Yes", "No"])
    is_fallback: bool = False  # True = high volume market outside normal range


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
        """
        Fetch markets with ACTIVE liquidity rewards.

        Strategy:
        1. Gamma API /markets (fast, paginated with offset) — get all active markets
        2. For each market with tokens, fetch CLOB /markets/{condition_id} to get
           authoritative reward data (rewards.rates, max_spread, min_size)
        3. Only keep markets where rewards.rates is populated (= on rewards page)

        This avoids the slow/huge CLOB /markets bulk endpoint.
        """
        # Step 1: Fetch all active markets from Gamma (fast)
        gamma_markets = {}
        offset = 0
        while offset < 800:
            try:
                resp = self._session.get(
                    f"{self.gamma_url}/markets",
                    params={"active": True, "closed": False, "limit": 100, "offset": offset},
                    timeout=30,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                for m in batch:
                    cid = m.get("conditionId", m.get("condition_id", ""))
                    if cid:
                        gamma_markets[cid] = m
                if len(batch) < 100:
                    break
                offset += 100
                time.sleep(0.15)
            except Exception as e:
                log.warning(f"Gamma /markets at offset {offset}: {e}")
                break

        log.info(f"Gamma API: {len(gamma_markets)} active markets")

        if not gamma_markets:
            log.error("Failed to fetch any markets from Gamma API")
            return []

        # Step 2: Filter to markets with tokens, then check CLOB for rewards
        candidates = []
        for cid, m in gamma_markets.items():
            tokens = self._get_tokens(m)
            if tokens:
                candidates.append((cid, m))

        log.info(f"Markets with tokens: {len(candidates)} — checking CLOB for reward data...")

        # Fetch CLOB reward data in parallel batches
        reward_markets = []
        checked = 0

        def _check_rewards(item):
            cid, gamma_m = item
            try:
                resp = self._session.get(
                    f"{self.clob_url}/markets/{cid}",
                    timeout=15,
                )
                if resp.status_code == 200:
                    clob_data = resp.json()
                    rewards = clob_data.get("rewards", {})
                    if isinstance(rewards, dict) and rewards.get("rates"):
                        # Merge: gamma for metadata, clob for rewards
                        merged = {**gamma_m, **clob_data}
                        merged["rewards"] = rewards
                        # Preserve gamma question/volume fields
                        for key in ["question", "volume", "volume24hr"]:
                            if key in gamma_m and gamma_m[key]:
                                merged[key] = gamma_m[key]
                        return merged
            except Exception:
                pass
            return None

        BATCH_SIZE = 12
        for i in range(0, len(candidates), BATCH_SIZE):
            batch = candidates[i:i + BATCH_SIZE]
            with ThreadPoolExecutor(max_workers=BATCH_SIZE) as pool:
                futures = [pool.submit(_check_rewards, item) for item in batch]
                for f in as_completed(futures):
                    try:
                        result = f.result()
                        if result:
                            reward_markets.append(result)
                    except Exception:
                        pass
            checked += len(batch)
            if checked % 100 == 0 or i + BATCH_SIZE >= len(candidates):
                log.info(f"  ...checked {checked}/{len(candidates)} markets, "
                         f"found {len(reward_markets)} with rewards...")

        log.info(f"CLOB reward check: {len(reward_markets)} markets with active rewards "
                 f"(checked {checked} markets)")

        if not reward_markets:
            log.warning("No reward markets found via CLOB individual lookups. "
                        "Falling back to Gamma clobRewards field.")
            # Fallback: use Gamma's clobRewards field
            for cid, m in gamma_markets.items():
                clob_rewards = m.get("clobRewards", [])
                if isinstance(clob_rewards, list) and clob_rewards:
                    for entry in clob_rewards:
                        if isinstance(entry, dict):
                            rate = entry.get("rewardsDailyRate", 0)
                            try:
                                if float(rate) > 0:
                                    reward_markets.append(m)
                                    break
                            except (ValueError, TypeError):
                                pass
            log.info(f"Gamma clobRewards fallback: {len(reward_markets)} markets")

        log.info(f"Total reward markets to scan: {len(reward_markets)}")
        return reward_markets

    def _has_rewards(self, market: dict) -> bool:
        """Check if a market has active rewards. CLOB rewards.rates is authoritative."""
        # CLOB API: rewards.rates is the authoritative source (matches rewards page)
        rewards_obj = market.get("rewards", {})
        if isinstance(rewards_obj, dict):
            rates = rewards_obj.get("rates")
            if rates:
                # rates can be a list of dicts with rewards_daily_rate
                if isinstance(rates, list):
                    return any(
                        float(r.get("rewards_daily_rate", r.get("rewardsDailyRate", 0))) > 0
                        for r in rates if isinstance(r, dict)
                    )
                try:
                    return float(rates) > 0
                except (ValueError, TypeError):
                    return bool(rates)  # non-empty = has rewards
        return False

    def _get_reward_amount(self, market: dict) -> float:
        """Extract the daily reward amount from a market dict.
        CLOB API rewards.rates is the authoritative source (matches rewards page)."""
        total = 0.0

        # PRIMARY: CLOB API rewards.rates (this is what the rewards page shows)
        rewards_obj = market.get("rewards", {})
        if isinstance(rewards_obj, dict):
            rates = rewards_obj.get("rates")
            if rates:
                if isinstance(rates, list):
                    for r in rates:
                        if isinstance(r, dict):
                            rate = r.get("rewards_daily_rate", r.get("rewardsDailyRate", 0))
                            try:
                                total += float(rate)
                            except (ValueError, TypeError):
                                pass
                else:
                    try:
                        total += float(rates)
                    except (ValueError, TypeError):
                        pass
        if total > 0:
            return total

        # FALLBACK: Gamma API clobRewards array
        clob_rewards = market.get("clobRewards", [])
        if isinstance(clob_rewards, list):
            for entry in clob_rewards:
                if isinstance(entry, dict):
                    rate = entry.get("rewardsDailyRate", 0)
                    try:
                        total += float(rate)
                    except (ValueError, TypeError):
                        pass
        if total > 0:
            return total

        return 0.0

    def _get_max_spread(self, market: dict) -> float:
        """Get the max spread for reward eligibility. CLOB rewards data is authoritative."""
        # PRIMARY: CLOB API rewards.max_spread
        rewards_obj = market.get("rewards", {})
        if isinstance(rewards_obj, dict):
            for key in ["max_spread", "maxSpread"]:
                val = rewards_obj.get(key)
                if val:
                    try:
                        v = float(val)
                        if v > 0:
                            return v / 100 if v > 1 else v
                    except (ValueError, TypeError):
                        pass

        # FALLBACK: Gamma API flat field
        val = market.get("rewardsMaxSpread")
        if val:
            try:
                v = float(val)
                if v > 0:
                    return v / 100 if v > 1 else v
            except (ValueError, TypeError):
                pass

        return 0.035  # default 3.5 cents

    def _get_min_size(self, market: dict) -> float:
        """Get the minimum order size for reward eligibility. CLOB rewards data is authoritative."""
        # PRIMARY: CLOB API rewards.min_size
        rewards_obj = market.get("rewards", {})
        if isinstance(rewards_obj, dict):
            for key in ["min_size", "minSize"]:
                val = rewards_obj.get(key)
                if val:
                    try:
                        return float(val)
                    except (ValueError, TypeError):
                        pass

        # FALLBACK: Gamma API flat field
        for key in ["rewardsMinSize", "rewards_min_size"]:
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

    def _check_thin_book(self, book: dict, midpoint: float = 0.5) -> bool:
        """
        Check if the order book has thin liquidity near the midpoint.
        Only checks orders within 10 cents of midpoint - extreme prices
        (like $0.001 or $0.999) are irrelevant for LP competition.
        """
        for side in ["bids", "asks"]:
            orders = book.get(side, [])
            price_levels = {}
            for order in orders:
                price = float(order.get("price", "0"))
                size = float(order.get("size", 0))
                # Only count orders within 10 cents of midpoint
                if abs(price - midpoint) > 0.10:
                    continue
                key = str(round(price, 4))
                price_levels[key] = price_levels.get(key, 0) + size * price

            for _price, dollar_value in price_levels.items():
                if dollar_value > self.config.max_orderbook_depth:
                    return False  # too thick near midpoint
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

    def _get_tokens(self, market: dict) -> list[str]:
        """Extract token IDs from a market dict, handling all API formats."""
        tokens = market.get("clobTokenIds", market.get("clob_token_ids", []))
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except (json.JSONDecodeError, TypeError):
                tokens = []
        if not tokens or (isinstance(tokens, list) and len(tokens) < 2):
            token_objs = market.get("tokens", [])
            if isinstance(token_objs, list) and len(token_objs) >= 2:
                tokens = []
                for t in token_objs:
                    if isinstance(t, dict):
                        tokens.append(t.get("token_id", t.get("tokenId", "")))
                    elif isinstance(t, str):
                        tokens.append(t)
        return tokens if isinstance(tokens, list) and len(tokens) >= 2 else []

    def _get_depth_near_mid(self, book: dict, mid: float) -> float:
        """Get max USD depth at any single price level within 15c of midpoint."""
        max_depth = 0.0
        for side in ["bids", "asks"]:
            levels = {}
            for order in book.get(side, []):
                price = float(order.get("price", "0"))
                size = float(order.get("size", 0))
                if abs(price - mid) > 0.15:
                    continue
                key = str(round(price, 4))
                levels[key] = levels.get(key, 0) + size * price
            for d in levels.values():
                max_depth = max(max_depth, d)
        return max_depth

    def _get_total_shares_near_mid(self, book: dict, mid: float) -> float:
        """Get total shares within 15c of midpoint."""
        total = 0.0
        for side in ["bids", "asks"]:
            for order in book.get(side, []):
                price = float(order.get("price", "0"))
                size = float(order.get("size", 0))
                if abs(price - mid) > 0.15:
                    continue
                total += size
        return total

    def _get_best_bid_ask_near_mid(self, book: dict, mid: float) -> tuple[float, float]:
        """Get best bid and ask within 15c of midpoint. Returns (bid, ask) or (0, 0)."""
        best_bid = 0.0
        best_ask = 0.0
        for order in book.get("bids", []):
            price = float(order.get("price", "0"))
            if abs(price - mid) <= 0.15 and price > best_bid:
                best_bid = price
        for order in book.get("asks", []):
            price = float(order.get("price", "0"))
            if abs(price - mid) <= 0.15 and (best_ask == 0 or price < best_ask):
                best_ask = price
        return best_bid, best_ask

    def scan_for_opportunities(self) -> list[MarketInfo]:
        """
        Main scan with clear rules:
        1. Midpoint must be 0.40-0.60 (max 60/40 split)
        2. Depth near midpoint < 500 shares OR < $150/level
        3. Spread between 2-6 cents (or empty book = we set our own)
        4. If gap > 2c, we post 1c better than best order
        5. Must have rewards >= $20/day (or assume $20 if unknown)
        6. FALLBACK: thick book but high volume = post 1c better with min shares
        """
        eligible = []

        all_markets = self.get_rewards_markets()
        if not all_markets:
            log.warning("No reward markets found, trying full market list")
            all_markets = self.get_all_markets()

        # Pre-filter: only keep markets with rewards and valid tokens (no API calls needed)
        candidates = []
        stats = {
            "no_tokens": 0, "no_reward": 0, "bad_midpoint": 0, "too_thick": 0,
            "bad_spread": 0, "low_share": 0, "checked": 0,
            "fallback": 0, "total": len(all_markets),
        }

        for market in all_markets:
            tokens = self._get_tokens(market)
            if not tokens:
                stats["no_tokens"] += 1
                continue
            reward_amount = self._get_reward_amount(market)
            if reward_amount <= 0:
                stats["no_reward"] += 1
                continue
            # Skip markets where min shares requirement exceeds our budget
            # min_size is in shares; at ~$0.50/share, check if we can afford it
            min_shares = self._get_min_size(market)
            min_usd_needed = min_shares * 0.50  # approximate cost at midpoint
            if min_usd_needed > self.config.max_exposure_per_market:
                stats["no_reward"] += 1  # reuse counter
                log.debug(f"Skipping {market.get('question', '?')[:40]}: "
                          f"min_size={min_shares:.0f} shares (~${min_usd_needed:.0f}) > budget")
                continue
            candidates.append((market, tokens, reward_amount))

        log.info(
            f"Pre-filter: {len(candidates)} markets with rewards "
            f"(skipped {stats['no_tokens']} no tokens, {stats['no_reward']} no reward) "
            f"out of {stats['total']} total"
        )

        # Fetch order books in parallel batches for speed
        def _fetch_books(item):
            market, tokens, reward = item
            book_yes = self.get_orderbook(tokens[0])
            book_no = self.get_orderbook(tokens[1])
            return (market, tokens, reward, book_yes, book_no)

        fetched = []
        BATCH_SIZE = 8
        for i in range(0, len(candidates), BATCH_SIZE):
            batch = candidates[i:i + BATCH_SIZE]
            with ThreadPoolExecutor(max_workers=BATCH_SIZE) as pool:
                futures = [pool.submit(_fetch_books, item) for item in batch]
                for f in as_completed(futures):
                    try:
                        fetched.append(f.result())
                    except Exception:
                        pass
            stats["checked"] += len(batch)
            if stats["checked"] % 50 == 0 or i + BATCH_SIZE >= len(candidates):
                log.info(f"  ...fetched {stats['checked']}/{len(candidates)} books...")

        log.info(f"Scanning {len(fetched)} markets with book data...")

        for market, tokens, reward_amount, book_yes, book_no in fetched:
            try:
                token_yes = tokens[0]
                token_no = tokens[1]
                condition_id = market.get("conditionId", market.get("condition_id", ""))
                question = market.get("question", market.get("title", "Unknown"))[:80]

                # === RULE 1: Determine midpoint ===
                yes_bids = book_yes.get("bids", [])
                yes_asks = book_yes.get("asks", [])

                # Find the REAL midpoint — where orders actually cluster
                # Many markets have orders only at extremes (0.001/0.999)
                # with a huge empty gap in the middle — that's our opportunity
                real_yes_bid = 0.0
                real_yes_ask = 0.0

                # Find highest bid that's > 0.10 (ignore dust bids at 0.001)
                for b in yes_bids:
                    p = float(b["price"])
                    if p >= 0.10:
                        real_yes_bid = p
                        break
                # Find lowest ask that's < 0.90 (ignore asks at 0.999)
                for a in yes_asks:
                    p = float(a["price"])
                    if p <= 0.90:
                        real_yes_ask = p
                        break

                # Determine midpoint and whether the book center is empty
                center_is_empty = False
                if real_yes_bid > 0 and real_yes_ask > 0:
                    mid = (real_yes_bid + real_yes_ask) / 2
                    spread = real_yes_ask - real_yes_bid
                elif real_yes_bid > 0:
                    mid = real_yes_bid + 0.02
                    spread = 0.04
                    center_is_empty = True
                elif real_yes_ask > 0:
                    mid = real_yes_ask - 0.02
                    spread = 0.04
                    center_is_empty = True
                else:
                    # No orders near center at all — fully empty
                    mid = 0.5
                    spread = 0.0
                    center_is_empty = True

                if mid < 0.40 or mid > 0.60:
                    stats["bad_midpoint"] += 1
                    continue

                no_mid = 1.0 - mid

                # === RULE 2: Depth check - < 500 shares OR < $150/level near mid ===
                depth_yes_usd = self._get_depth_near_mid(book_yes, mid)
                depth_no_usd = self._get_depth_near_mid(book_no, no_mid)
                max_depth_usd = max(depth_yes_usd, depth_no_usd)

                shares_yes = self._get_total_shares_near_mid(book_yes, mid)
                shares_no = self._get_total_shares_near_mid(book_no, no_mid)
                max_shares = max(shares_yes, shares_no)

                is_thin = max_depth_usd < 150.0 or max_shares < 500

                # If center is empty, it's always thin where we'd place orders
                if center_is_empty:
                    is_thin = True

                # === RULE 3: Spread and best bid/ask ===
                yes_best_bid = real_yes_bid if real_yes_bid > 0 else 0.0
                yes_best_ask = real_yes_ask if real_yes_ask > 0 else 0.0

                # Find NO side best bid/ask near mid too
                no_best_bid = 0.0
                no_best_ask = 0.0
                for b in book_no.get("bids", []):
                    p = float(b["price"])
                    if p >= 0.10:
                        no_best_bid = p
                        break
                for a in book_no.get("asks", []):
                    p = float(a["price"])
                    if p <= 0.90:
                        no_best_ask = p
                        break

                # Set defaults for empty/center-empty books
                book_is_empty = center_is_empty and yes_best_bid == 0 and yes_best_ask == 0
                if book_is_empty or center_is_empty:
                    if yes_best_bid == 0:
                        yes_best_bid = mid - 0.02
                    if yes_best_ask == 0:
                        yes_best_ask = mid + 0.02
                    if no_best_bid == 0:
                        no_best_bid = no_mid - 0.02
                    if no_best_ask == 0:
                        no_best_ask = no_mid + 0.02

                # Spread must be >= 2c and <= 6c for primary targets
                # Center-empty books are always OK (we define the spread)
                spread_ok = center_is_empty or (0.02 <= spread <= 0.06)

                # === RULE 5: Reward share estimate ===
                if center_is_empty:
                    share = 1.0  # we'd be the only LP near the center
                else:
                    share = self._estimate_reward_share(market, book_yes, book_no)

                volume = float(market.get("volume", market.get("volume24hr", 0)) or 0)

                # === DECISION: Primary target or fallback? ===
                is_fallback = False

                if center_is_empty:
                    # Empty center = PRIMARY — we'd be the only LP
                    pass
                elif is_thin and spread_ok:
                    # PRIMARY TARGET: thin book, good spread
                    pass
                elif is_thin and not spread_ok:
                    # Thin book, spread > 6c — post 1c better than best
                    is_fallback = True
                    stats["fallback"] += 1
                elif not is_thin and volume > 0:
                    # FALLBACK: thick book but has volume
                    is_fallback = True
                    stats["fallback"] += 1
                else:
                    stats["too_thick"] += 1
                    continue

                # Log what we found
                tag = "FALLBACK" if is_fallback else ("EMPTY" if book_is_empty else "PRIMARY")
                if len(eligible) < 20:
                    log.info(
                        f"  [{tag}] mid:{mid:.2f} | spread:{spread:.3f} | "
                        f"depth:${max_depth_usd:.0f}/{max_shares:.0f}sh | "
                        f"share:{share:.0%} | reward:${reward_amount:.0f} | "
                        f"{question[:50]}"
                    )

                info = MarketInfo(
                    condition_id=condition_id,
                    question=question[:100],
                    token_yes=token_yes,
                    token_no=token_no,
                    yes_bid=yes_best_bid,
                    yes_ask=yes_best_ask,
                    no_bid=no_best_bid,
                    no_ask=no_best_ask,
                    midpoint=mid,
                    spread=spread,
                    reward_pool=reward_amount,
                    max_spread=self._get_max_spread(market),
                    min_size=self._get_min_size(market),
                    volume_24h=volume,
                    orderbook_depth_yes=depth_yes_usd,
                    orderbook_depth_no=depth_no_usd,
                    our_share_estimate=share,
                    neg_risk=bool(market.get("negRisk", market.get("neg_risk", False))),
                    is_fallback=is_fallback,
                )
                eligible.append(info)

            except Exception as e:
                log.debug(f"Error processing market: {e}")
                continue

        # Sort: primary targets first (by share * reward), then fallbacks
        eligible.sort(
            key=lambda m: (0 if m.is_fallback else 1, m.our_share_estimate * m.reward_pool),
            reverse=True,
        )

        log.info(
            f"Scan: {len(eligible)} eligible ({stats['fallback']} fallback) | "
            f"Skipped: {stats['no_tokens']} no tokens, "
            f"{stats['no_reward']} no/low reward, "
            f"{stats['bad_midpoint']} bad midpoint, "
            f"{stats['too_thick']} too thick, "
            f"{stats['bad_spread']} bad spread | "
            f"Checked {stats['checked']} books"
        )
        return eligible
