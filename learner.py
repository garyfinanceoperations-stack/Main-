"""
Adaptive learner - tracks market performance history and auto-adjusts strategy.

Learns which markets, spreads, and conditions produce profits vs losses,
then uses that history to improve future market selection and parameter tuning.
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from config import BotConfig
from logger import log


@dataclass
class MarketRecord:
    """Historical performance record for a single market."""
    condition_id: str
    question: str = ""
    # Lifetime stats
    total_fills: int = 0
    total_rewards_earned: float = 0.0
    total_realized_pnl: float = 0.0
    total_adverse_fills: int = 0  # fills that resulted in loss
    total_profitable_fills: int = 0
    # Session tracking
    sessions_active: int = 0  # how many bot sessions we've been in this market
    sessions_profitable: int = 0
    # Condition snapshots at entry (averaged over sessions)
    avg_spread_at_entry: float = 0.0
    avg_reward_pool: float = 0.0
    avg_book_depth: float = 0.0
    avg_volume: float = 0.0
    avg_our_share: float = 0.0
    # Timing
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    # Computed
    blacklisted: bool = False
    blacklist_reason: str = ""
    score: float = 0.0  # computed learning score

    @property
    def net_pnl(self) -> float:
        return self.total_realized_pnl + self.total_rewards_earned

    @property
    def win_rate(self) -> float:
        total = self.total_profitable_fills + self.total_adverse_fills
        if total == 0:
            return 0.5  # no data, assume neutral
        return self.total_profitable_fills / total

    @property
    def reward_per_session(self) -> float:
        if self.sessions_active == 0:
            return 0.0
        return self.total_rewards_earned / self.sessions_active

    @property
    def pnl_per_session(self) -> float:
        if self.sessions_active == 0:
            return 0.0
        return self.net_pnl / self.sessions_active


@dataclass
class EdgePerformance:
    """Tracks how different edge values perform."""
    edge_value: float
    fills_at_edge: int = 0
    pnl_at_edge: float = 0.0
    adverse_fills: int = 0

    @property
    def avg_pnl_per_fill(self) -> float:
        if self.fills_at_edge == 0:
            return 0.0
        return self.pnl_at_edge / self.fills_at_edge


@dataclass
class LearningSnapshot:
    """A point-in-time snapshot used for time-series learning."""
    timestamp: float
    total_markets_active: int
    total_pnl: float
    total_rewards: float
    avg_spread: float
    avg_fill_rate: float


class Learner:
    """
    Adaptive learning engine that improves market selection and parameters
    based on historical performance.
    """

    DATA_FILE = "learning_data.json"
    MIN_SESSIONS_FOR_SCORING = 3  # need this many sessions before trusting scores
    BLACKLIST_LOSS_THRESHOLD = -20.0  # auto-blacklist after this much net loss
    BLACKLIST_MIN_SESSIONS = 2  # don't blacklist until we've tried it enough

    def __init__(self, config: BotConfig):
        self.config = config
        self.markets: dict[str, MarketRecord] = {}
        self.edge_stats: dict[str, EdgePerformance] = {}  # edge_bucket -> stats
        self.snapshots: list[dict] = []
        self.session_start: float = time.time()
        self.session_markets: set[str] = set()  # markets active this session

        # Learned parameter adjustments
        self.learned_min_edge: float | None = None
        self.learned_max_edge: float | None = None
        self.learned_order_size_multiplier: float = 1.0

        self._load()

    # === Persistence ===

    def _load(self):
        """Load learning data from disk."""
        if not os.path.exists(self.DATA_FILE):
            log.info("No learning data found - starting fresh")
            return

        try:
            with open(self.DATA_FILE, "r") as f:
                data = json.load(f)

            for cid, rec_data in data.get("markets", {}).items():
                self.markets[cid] = MarketRecord(**rec_data)

            for edge_key, edge_data in data.get("edge_stats", {}).items():
                self.edge_stats[edge_key] = EdgePerformance(**edge_data)

            self.snapshots = data.get("snapshots", [])

            # Load learned parameters
            learned = data.get("learned_params", {})
            self.learned_min_edge = learned.get("min_edge")
            self.learned_max_edge = learned.get("max_edge")
            self.learned_order_size_multiplier = learned.get("order_size_multiplier", 1.0)

            blacklisted = sum(1 for m in self.markets.values() if m.blacklisted)
            log.info(
                f"Loaded learning data: {len(self.markets)} markets tracked, "
                f"{blacklisted} blacklisted, "
                f"{len(self.edge_stats)} edge buckets"
            )

        except Exception as e:
            log.error(f"Error loading learning data: {e}")

    def save(self):
        """Persist learning data to disk."""
        try:
            data = {
                "markets": {cid: asdict(rec) for cid, rec in self.markets.items()},
                "edge_stats": {k: asdict(v) for k, v in self.edge_stats.items()},
                "snapshots": self.snapshots[-500:],  # keep last 500 snapshots
                "learned_params": {
                    "min_edge": self.learned_min_edge,
                    "max_edge": self.learned_max_edge,
                    "order_size_multiplier": self.learned_order_size_multiplier,
                },
            }

            with open(self.DATA_FILE, "w") as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            log.error(f"Error saving learning data: {e}")

    # === Event Recording ===

    def record_market_entry(self, condition_id: str, question: str,
                            spread: float, reward_pool: float,
                            book_depth: float, volume: float,
                            our_share: float):
        """Record when we start providing liquidity to a market."""
        if condition_id not in self.markets:
            self.markets[condition_id] = MarketRecord(
                condition_id=condition_id,
                question=question,
            )

        rec = self.markets[condition_id]
        rec.last_seen = time.time()
        rec.question = question

        # Track this session
        if condition_id not in self.session_markets:
            self.session_markets.add(condition_id)
            rec.sessions_active += 1

            # Running average of conditions at entry
            n = rec.sessions_active
            rec.avg_spread_at_entry = ((n - 1) * rec.avg_spread_at_entry + spread) / n
            rec.avg_reward_pool = ((n - 1) * rec.avg_reward_pool + reward_pool) / n
            rec.avg_book_depth = ((n - 1) * rec.avg_book_depth + book_depth) / n
            rec.avg_volume = ((n - 1) * rec.avg_volume + volume) / n
            rec.avg_our_share = ((n - 1) * rec.avg_our_share + our_share) / n

    def record_fill(self, condition_id: str, side: str, price: float,
                    size: float, edge_used: float):
        """Record a fill event for learning."""
        if condition_id not in self.markets:
            self.markets[condition_id] = MarketRecord(condition_id=condition_id)

        rec = self.markets[condition_id]
        rec.total_fills += 1

        # Track edge performance
        edge_bucket = f"{round(edge_used * 100, 1)}c"  # e.g. "0.5c", "1.0c"
        if edge_bucket not in self.edge_stats:
            self.edge_stats[edge_bucket] = EdgePerformance(edge_value=edge_used)
        self.edge_stats[edge_bucket].fills_at_edge += 1

    def record_trade_result(self, condition_id: str, pnl: float, edge_used: float = 0.0):
        """Record the P&L result of a closed position."""
        if condition_id not in self.markets:
            self.markets[condition_id] = MarketRecord(condition_id=condition_id)

        rec = self.markets[condition_id]
        rec.total_realized_pnl += pnl

        if pnl >= 0:
            rec.total_profitable_fills += 1
        else:
            rec.total_adverse_fills += 1

        # Track edge P&L
        if edge_used > 0:
            edge_bucket = f"{round(edge_used * 100, 1)}c"
            if edge_bucket in self.edge_stats:
                self.edge_stats[edge_bucket].pnl_at_edge += pnl
                if pnl < 0:
                    self.edge_stats[edge_bucket].adverse_fills += 1

        # Check auto-blacklist
        self._check_auto_blacklist(condition_id)
        self.save()

    def record_rewards(self, condition_id: str, amount: float):
        """Record rewards earned from a market."""
        if condition_id not in self.markets:
            self.markets[condition_id] = MarketRecord(condition_id=condition_id)

        self.markets[condition_id].total_rewards_earned += amount
        self.save()

    def record_session_end(self, market_pnl: dict[str, float]):
        """Record end-of-session stats for learning."""
        for cid, pnl in market_pnl.items():
            if cid in self.markets:
                if pnl > 0:
                    self.markets[cid].sessions_profitable += 1

        # Take a snapshot
        total_pnl = sum(m.net_pnl for m in self.markets.values())
        total_rewards = sum(m.total_rewards_earned for m in self.markets.values())
        active = len(self.session_markets)
        avg_spread = 0.0
        if self.session_markets:
            spreads = [
                self.markets[cid].avg_spread_at_entry
                for cid in self.session_markets
                if cid in self.markets
            ]
            avg_spread = sum(spreads) / len(spreads) if spreads else 0.0

        self.snapshots.append({
            "timestamp": time.time(),
            "total_markets_active": active,
            "total_pnl": total_pnl,
            "total_rewards": total_rewards,
            "avg_spread": avg_spread,
        })

        # Run parameter optimization
        self._optimize_parameters()
        self.save()

    # === Auto-Blacklisting ===

    def _check_auto_blacklist(self, condition_id: str):
        """Auto-blacklist markets that consistently lose money."""
        rec = self.markets.get(condition_id)
        if not rec or rec.blacklisted:
            return

        # Don't blacklist until we've given it a fair chance
        if rec.sessions_active < self.BLACKLIST_MIN_SESSIONS:
            return

        # Blacklist if net P&L is deeply negative
        if rec.net_pnl < self.BLACKLIST_LOSS_THRESHOLD:
            rec.blacklisted = True
            rec.blacklist_reason = (
                f"Auto-blacklisted: net PnL ${rec.net_pnl:.2f} over "
                f"{rec.sessions_active} sessions"
            )
            log.warning(
                f"LEARNER: Blacklisted market {condition_id[:16]} "
                f"({rec.question[:40]}) - {rec.blacklist_reason}"
            )

        # Blacklist if win rate is terrible with enough data
        if rec.total_fills >= 10 and rec.win_rate < 0.2:
            rec.blacklisted = True
            rec.blacklist_reason = (
                f"Auto-blacklisted: win rate {rec.win_rate:.0%} over "
                f"{rec.total_fills} fills"
            )
            log.warning(
                f"LEARNER: Blacklisted market {condition_id[:16]} "
                f"({rec.question[:40]}) - {rec.blacklist_reason}"
            )

    def is_blacklisted(self, condition_id: str) -> bool:
        """Check if a market is blacklisted."""
        rec = self.markets.get(condition_id)
        return rec.blacklisted if rec else False

    def unblacklist(self, condition_id: str):
        """Manually remove a market from the blacklist."""
        rec = self.markets.get(condition_id)
        if rec:
            rec.blacklisted = False
            rec.blacklist_reason = ""
            log.info(f"LEARNER: Unblacklisted market {condition_id[:16]}")
            self.save()

    # === Market Scoring ===

    def score_market(self, condition_id: str, reward_pool: float,
                     our_share: float, spread: float,
                     book_depth: float) -> float:
        """
        Score a market for selection, combining:
        1. Base expected value (reward * share)
        2. Historical performance bonus/penalty
        3. Similarity to historically profitable markets

        Returns a float score (higher = better). Base score is reward * share.
        """
        base_score = reward_pool * our_share

        rec = self.markets.get(condition_id)
        if not rec or rec.sessions_active < self.MIN_SESSIONS_FOR_SCORING:
            # Not enough data - use base score + similarity bonus
            similarity = self._similarity_score(spread, reward_pool, book_depth)
            return base_score * (1.0 + similarity * 0.3)

        # Historical performance multiplier
        # Range: 0.3x (terrible) to 2.0x (great)
        perf_multiplier = 1.0

        # Factor 1: PnL per session (most important)
        pnl_per = rec.pnl_per_session
        if pnl_per > 5:
            perf_multiplier += 0.5
        elif pnl_per > 0:
            perf_multiplier += 0.2
        elif pnl_per > -5:
            perf_multiplier -= 0.2
        else:
            perf_multiplier -= 0.5

        # Factor 2: Win rate
        wr = rec.win_rate
        if wr > 0.6:
            perf_multiplier += 0.3
        elif wr < 0.3:
            perf_multiplier -= 0.3

        # Factor 3: Reward consistency
        if rec.reward_per_session > 2:
            perf_multiplier += 0.2

        # Clamp
        perf_multiplier = max(0.3, min(2.0, perf_multiplier))

        score = base_score * perf_multiplier
        rec.score = score
        return score

    def _similarity_score(self, spread: float, reward_pool: float,
                          book_depth: float) -> float:
        """
        Score how similar a new market's conditions are to historically
        profitable markets. Returns -1 to 1 (negative = similar to losers).
        """
        profitable = [
            m for m in self.markets.values()
            if m.sessions_active >= self.MIN_SESSIONS_FOR_SCORING and m.net_pnl > 0
        ]
        losing = [
            m for m in self.markets.values()
            if m.sessions_active >= self.MIN_SESSIONS_FOR_SCORING and m.net_pnl < 0
        ]

        if not profitable and not losing:
            return 0.0  # no data yet

        def avg_distance(markets: list[MarketRecord]) -> float:
            if not markets:
                return float("inf")
            total = 0.0
            for m in markets:
                # Normalized differences
                spread_diff = abs(spread - m.avg_spread_at_entry) / max(spread, 0.001)
                reward_diff = abs(reward_pool - m.avg_reward_pool) / max(reward_pool, 1)
                depth_diff = abs(book_depth - m.avg_book_depth) / max(book_depth, 1)
                total += (spread_diff + reward_diff + depth_diff) / 3
            return total / len(markets)

        dist_to_winners = avg_distance(profitable)
        dist_to_losers = avg_distance(losing)

        if dist_to_winners == float("inf") and dist_to_losers == float("inf"):
            return 0.0

        # Closer to winners = positive score
        if dist_to_losers == 0:
            return -0.5
        if dist_to_winners == 0:
            return 0.5

        ratio = dist_to_losers / (dist_to_winners + dist_to_losers)
        return (ratio - 0.5) * 2  # scale to -1..1

    # === Parameter Optimization ===

    def _optimize_parameters(self):
        """Use historical edge performance to suggest better edge values."""
        if not self.edge_stats:
            return

        # Find edge buckets with positive avg P&L per fill
        good_edges = [
            e for e in self.edge_stats.values()
            if e.fills_at_edge >= 5 and e.avg_pnl_per_fill > 0
        ]
        bad_edges = [
            e for e in self.edge_stats.values()
            if e.fills_at_edge >= 5 and e.avg_pnl_per_fill < 0
        ]

        if good_edges:
            # Suggest shifting toward profitable edge range
            best_edge = max(good_edges, key=lambda e: e.avg_pnl_per_fill)
            worst_edge = min(
                (e for e in self.edge_stats.values() if e.fills_at_edge >= 5),
                key=lambda e: e.avg_pnl_per_fill,
                default=None,
            )

            if best_edge.edge_value != self.learned_min_edge:
                self.learned_min_edge = best_edge.edge_value
                log.info(
                    f"LEARNER: Optimal min edge → {best_edge.edge_value*100:.1f}c "
                    f"(avg PnL/fill: ${best_edge.avg_pnl_per_fill:.3f})"
                )

            if worst_edge and worst_edge.avg_pnl_per_fill < -0.5:
                # If the worst edge is very bad, avoid going that wide
                if worst_edge.edge_value > best_edge.edge_value:
                    self.learned_max_edge = worst_edge.edge_value * 0.8
                    log.info(
                        f"LEARNER: Narrowing max edge → {self.learned_max_edge*100:.1f}c "
                        f"(avoiding ${worst_edge.avg_pnl_per_fill:.3f}/fill at "
                        f"{worst_edge.edge_value*100:.1f}c)"
                    )

        # Order size optimization based on overall performance
        total_sessions = sum(m.sessions_active for m in self.markets.values())
        if total_sessions >= 10:
            profitable_sessions = sum(m.sessions_profitable for m in self.markets.values())
            session_win_rate = profitable_sessions / total_sessions

            if session_win_rate > 0.6:
                # Winning consistently - can scale up slightly
                self.learned_order_size_multiplier = min(1.5, self.learned_order_size_multiplier + 0.1)
                log.info(
                    f"LEARNER: Scaling up order size → {self.learned_order_size_multiplier:.1f}x "
                    f"(session win rate: {session_win_rate:.0%})"
                )
            elif session_win_rate < 0.35:
                # Losing too often - scale down
                self.learned_order_size_multiplier = max(0.5, self.learned_order_size_multiplier - 0.1)
                log.info(
                    f"LEARNER: Scaling down order size → {self.learned_order_size_multiplier:.1f}x "
                    f"(session win rate: {session_win_rate:.0%})"
                )

    def get_adjusted_params(self) -> dict:
        """
        Return parameter adjustments the bot should use.
        Only overrides values where we have enough data to be confident.
        """
        params = {}

        if self.learned_min_edge is not None:
            params["min_edge"] = self.learned_min_edge

        if self.learned_max_edge is not None:
            params["max_edge"] = self.learned_max_edge

        if self.learned_order_size_multiplier != 1.0:
            params["order_size_multiplier"] = self.learned_order_size_multiplier

        return params

    # === Reporting ===

    def get_learning_report(self) -> str:
        """Generate a human-readable learning status report."""
        lines = [
            "=" * 60,
            "  LEARNING ENGINE - STATUS",
            "=" * 60,
        ]

        total_markets = len(self.markets)
        blacklisted = sum(1 for m in self.markets.values() if m.blacklisted)
        scored = [m for m in self.markets.values()
                  if m.sessions_active >= self.MIN_SESSIONS_FOR_SCORING]

        lines.append(f"  Markets tracked: {total_markets}")
        lines.append(f"  Blacklisted: {blacklisted}")
        lines.append(f"  Scored (enough data): {len(scored)}")

        # Top performers
        if scored:
            lines.append("")
            lines.append("  TOP PERFORMERS:")
            top = sorted(scored, key=lambda m: m.pnl_per_session, reverse=True)[:5]
            for m in top:
                lines.append(
                    f"    {m.question[:45]:45s} | "
                    f"PnL/session: ${m.pnl_per_session:+.2f} | "
                    f"Win: {m.win_rate:.0%} | "
                    f"Sessions: {m.sessions_active}"
                )

            lines.append("")
            lines.append("  WORST PERFORMERS:")
            bottom = sorted(scored, key=lambda m: m.pnl_per_session)[:5]
            for m in bottom:
                status = " [BLACKLISTED]" if m.blacklisted else ""
                lines.append(
                    f"    {m.question[:45]:45s} | "
                    f"PnL/session: ${m.pnl_per_session:+.2f} | "
                    f"Win: {m.win_rate:.0%}{status}"
                )

        # Edge analysis
        if self.edge_stats:
            lines.append("")
            lines.append("  EDGE ANALYSIS:")
            for key in sorted(self.edge_stats.keys()):
                e = self.edge_stats[key]
                if e.fills_at_edge >= 3:
                    lines.append(
                        f"    Edge {key:>5s}: {e.fills_at_edge:4d} fills | "
                        f"Avg PnL/fill: ${e.avg_pnl_per_fill:+.3f} | "
                        f"Adverse: {e.adverse_fills}"
                    )

        # Learned parameters
        params = self.get_adjusted_params()
        if params:
            lines.append("")
            lines.append("  LEARNED ADJUSTMENTS:")
            if "min_edge" in params:
                lines.append(f"    Min edge: {params['min_edge']*100:.1f}c "
                             f"(default: {self.config.min_edge*100:.1f}c)")
            if "max_edge" in params:
                lines.append(f"    Max edge: {params['max_edge']*100:.1f}c "
                             f"(default: {self.config.max_edge*100:.1f}c)")
            if "order_size_multiplier" in params:
                lines.append(f"    Order size multiplier: {params['order_size_multiplier']:.1f}x")

        # Blacklisted markets
        bl_markets = [m for m in self.markets.values() if m.blacklisted]
        if bl_markets:
            lines.append("")
            lines.append("  BLACKLISTED MARKETS:")
            for m in bl_markets:
                lines.append(f"    {m.question[:50]} - {m.blacklist_reason}")

        lines.append("=" * 60)
        return "\n".join(lines)
