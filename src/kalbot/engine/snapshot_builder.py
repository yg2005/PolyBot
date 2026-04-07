from __future__ import annotations

import re
from datetime import datetime, timezone

from ..feeds.polymarket import MarketInfo, OrderbookSnapshot
from ..feeds.spot_feed import SpotFeed
from ..types import WindowSnapshot
from .window_tracker import WindowTracker


def parse_strike(question: str) -> float | None:
    m = re.search(r'\$([0-9,]+(?:\.[0-9]+)?)', question)
    return float(m.group(1).replace(",", "")) if m else None


def build_snapshot(
    market: MarketInfo,
    elapsed_bucket: int,
    tracker: WindowTracker,
    chainlink_price: float,
    spot: SpotFeed | None,
    spot_price: float,
    spot_source: str,
    ob: OrderbookSnapshot | None,
) -> WindowSnapshot | None:
    features = tracker.get_features(chainlink_price)
    if features is None:
        return None

    now = datetime.now(timezone.utc)
    end = market.end_date.replace(tzinfo=timezone.utc) if market.end_date.tzinfo is None else market.end_date
    remaining = max(0, int((end - now).total_seconds()))

    # Recover open_price: current = open * (1 + disp/100) → open = current / (1 + disp/100)
    denom = 1.0 + features.displacement_pct / 100.0
    open_price = chainlink_price / denom if denom != 0 else chainlink_price

    strike = parse_strike(market.question) or open_price
    spot_val = spot_price or chainlink_price
    spot_disp = (spot_val - open_price) / open_price * 100.0 if open_price else 0.0
    spot_trend = spot.trend_1m(spot_val, now) if spot else 0.0
    spot_confirms = (not spot.is_stale) if spot else False

    yes_bid = ob.yes_bid if ob else 0.5
    yes_ask = ob.yes_ask if ob else 0.5
    no_bid = ob.no_bid if ob else 0.5
    no_ask = ob.no_ask if ob else 0.5
    spread = ob.spread if ob else 0.0
    mid = ob.mid_price if ob else 0.5
    bid_dep = ob.bid_depth_usd if ob else 0.0
    ask_dep = ob.ask_depth_usd if ob else 0.0
    depth_imb = (bid_dep - ask_dep) / (bid_dep + ask_dep + 1e-9)
    mspeed = features.abs_displacement_pct / features.elapsed_seconds * 60.0 if features.elapsed_seconds > 0 else 0.0

    return WindowSnapshot(
        window_id=f"{market.condition_id}_{elapsed_bucket}",
        market_id=market.market_id,
        strike_price=strike,
        window_open_time=now,
        window_close_time=end,
        open_price=open_price,
        snapshot_price=chainlink_price,
        close_price=None,
        displacement_pct=features.displacement_pct,
        abs_displacement_pct=features.abs_displacement_pct,
        direction=features.direction,
        direction_consistency=features.direction_consistency,
        cross_count=features.cross_count,
        time_above_pct=features.time_above_pct,
        time_below_pct=1.0 - features.time_above_pct,
        max_displacement_pct=features.max_displacement_pct,
        min_displacement_pct=features.min_displacement_pct,
        velocity=features.velocity,
        acceleration=features.acceleration,
        distance_from_low=features.distance_from_low,
        spot_price=spot_val,
        spot_displacement_pct=spot_disp,
        spot_trend_1m=spot_trend,
        spot_confirms=spot_confirms,
        spot_source=spot_source,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        spread=spread,
        mid_price=mid,
        bid_depth_usd=bid_dep,
        ask_depth_usd=ask_dep,
        depth_imbalance=depth_imb,
        market_move_speed=mspeed,
        elapsed_seconds=int(features.elapsed_seconds),
        remaining_seconds=remaining,
        snapshot_time=now,
    )
