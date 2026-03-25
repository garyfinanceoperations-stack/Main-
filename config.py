"""Configuration loader for the Polymarket LP Bot."""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class BotConfig:
    # API
    private_key: str = os.getenv("PRIVATE_KEY", "")
    clob_api_url: str = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
    gamma_api_url: str = os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com")
    chain_id: int = int(os.getenv("CHAIN_ID", "137"))

    # Risk limits
    max_exposure_per_market: float = float(os.getenv("MAX_EXPOSURE_PER_MARKET", "120"))
    max_loss_per_position: float = float(os.getenv("MAX_LOSS_PER_POSITION", "10"))
    emergency_loss_threshold: float = float(os.getenv("EMERGENCY_LOSS_THRESHOLD", "25"))
    portfolio_stop_loss: float = float(os.getenv("PORTFOLIO_STOP_LOSS", "80"))

    # Market filters
    min_reward_pool: float = float(os.getenv("MIN_REWARD_POOL", "20"))
    max_orderbook_depth: float = float(os.getenv("MAX_ORDERBOOK_DEPTH", "120"))
    max_spread_gap: float = float(os.getenv("MAX_SPREAD_GAP", "5")) / 100  # convert cents to decimal
    min_reward_share_target: float = float(os.getenv("MIN_REWARD_SHARE_TARGET", "20")) / 100
    max_active_markets: int = int(os.getenv("MAX_ACTIVE_MARKETS", "5"))

    # Order parameters
    order_size: float = float(os.getenv("ORDER_SIZE", "10"))
    num_price_levels: int = int(os.getenv("NUM_PRICE_LEVELS", "2"))
    min_edge: float = float(os.getenv("MIN_EDGE", "0.5")) / 100  # cents to decimal
    max_edge: float = float(os.getenv("MAX_EDGE", "2.5")) / 100

    # Timing
    scan_interval: int = int(os.getenv("SCAN_INTERVAL", "60"))

    def validate(self) -> list[str]:
        """Return list of validation errors, empty if config is valid."""
        errors = []
        if not self.private_key or self.private_key == "your_private_key_here":
            errors.append("PRIVATE_KEY is not set in .env")
        if self.max_exposure_per_market <= 0:
            errors.append("MAX_EXPOSURE_PER_MARKET must be positive")
        if self.max_loss_per_position <= 0:
            errors.append("MAX_LOSS_PER_POSITION must be positive")
        if self.order_size <= 0:
            errors.append("ORDER_SIZE must be positive")
        if self.order_size * self.num_price_levels * 2 > self.max_exposure_per_market:
            errors.append(
                f"Total order size ({self.order_size * self.num_price_levels * 2}) "
                f"exceeds MAX_EXPOSURE_PER_MARKET ({self.max_exposure_per_market})"
            )
        return errors
