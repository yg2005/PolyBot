from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class OrderState(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"


class TradeSide(str, Enum):
    YES = "YES"
    NO = "NO"


@dataclass
class WindowSnapshot:
    window_id: str
    market_id: str
    strike_price: float

    window_open_time: datetime
    window_close_time: datetime

    open_price: float
    snapshot_price: float
    close_price: float | None

    displacement_pct: float
    abs_displacement_pct: float
    direction: int  # 1 = up, -1 = down, 0 = flat

    direction_consistency: float
    cross_count: int

    time_above_pct: float
    time_below_pct: float

    max_displacement_pct: float
    min_displacement_pct: float

    velocity: float
    acceleration: float
    distance_from_low: float

    spot_price: float
    spot_displacement_pct: float
    spot_trend_1m: float
    spot_confirms: bool
    spot_source: str

    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    spread: float
    mid_price: float
    bid_depth_usd: float
    ask_depth_usd: float
    depth_imbalance: float
    market_move_speed: float

    elapsed_seconds: int
    remaining_seconds: int
    snapshot_time: datetime

    settlement_outcome: str | None = None
    settlement_price: float | None = None

    traded: bool = False
    trade_side: str | None = None
    trade_entry_price: float | None = None
    trade_fill_price: float | None = None
    trade_pnl: float | None = None

    rule_signal: str | None = None
    model_prob: float | None = None


@dataclass
class ScorerResult:
    signal: str          # "YES", "NO", or "PASS"
    confidence: float    # 0.0–1.0
    edge_estimate: float
    reasoning: str
    features_used: dict = field(default_factory=dict)


@dataclass
class DecisionResult:
    action: str                    # "TRADE" or "PASS"
    side: str | None
    target_price: float | None
    size_usd: float | None
    strategy: str | None           # "maker", "taker", "adaptive"
    pass_reason: str | None
    scorer_result: ScorerResult
