from __future__ import annotations

import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

FEATURE_COLS: list[str] = [
    # Trajectory
    "displacement_pct",
    "abs_displacement_pct",
    "direction_consistency",
    "cross_count",
    "time_above_pct",
    "velocity",
    "acceleration",
    "max_displacement_pct",
    "min_displacement_pct",
    "distance_from_low",
    # Market
    "mid_price",
    "spread",
    "depth_imbalance",
    "market_move_speed",
    # Confirmation
    "spot_confirms",
    "spot_displacement_pct",
    "spot_trend_1m",
    "chainlink_spot_divergence",
    # Timing
    "elapsed_seconds",
    "remaining_seconds",
    "time_of_day_hour",
    "day_of_week",
    # Derived
    "displacement_per_cross",
    "momentum_score",
    "market_disagreement",
]


def _add_derived(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Timing from snapshot_time
    ts = pd.to_datetime(df["snapshot_time"], utc=True, errors="coerce")
    df["time_of_day_hour"] = ts.dt.hour + ts.dt.minute / 60.0
    df["day_of_week"] = ts.dt.dayofweek  # 0=Mon … 6=Sun

    # chainlink_spot_divergence: how far chainlink and spot disagree
    df["chainlink_spot_divergence"] = (
        df["displacement_pct"] - df["spot_displacement_pct"]
    ).abs()

    # displacement_per_cross: momentum per directional flip
    df["displacement_per_cross"] = df["displacement_pct"] / (df["cross_count"] + 1)

    # momentum_score: velocity weighted by direction consistency
    df["momentum_score"] = df["velocity"] * df["direction_consistency"]

    # market_disagreement: how much BTC move differs from market pricing
    # mid_price is P(YES); a 1% up move → implies ~P(YES) > 0.5
    # We proxy market-implied move as (mid_price - 0.5) * 2% (rough heuristic)
    market_implied_move = (df["mid_price"] - 0.5) * 0.02
    df["market_disagreement"] = (df["displacement_pct"] - market_implied_move).abs()

    return df


def build_feature_matrix(
    df: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Build (X, y, feature_names) from a raw windows DataFrame.

    Requires settlement_outcome to be non-null.
    """
    df = df[df["settlement_outcome"].notna()].copy()
    if df.empty:
        return np.empty((0, len(FEATURE_COLS))), np.empty(0, dtype=int), list(FEATURE_COLS)

    df = _add_derived(df)

    missing = [c for c in FEATURE_COLS if c not in df.columns]
    if missing:
        log.warning("Missing columns filled with 0: %s", missing)
        for c in missing:
            df[c] = 0.0

    X = df[FEATURE_COLS].fillna(0.0).to_numpy(dtype=float)
    y = (df["settlement_outcome"] == "YES").astype(int).to_numpy()
    return X, y, list(FEATURE_COLS)


async def get_feature_matrix(
    db_path: str,
    min_date: str | None = None,
    max_date: str | None = None,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Async entry-point used by train.py."""
    from kalbot.data.db import Database

    db = Database(db_path)
    df = await db.get_training_data(
        limit=200_000,
        only_settled=True,
        min_date=min_date,
        max_date=max_date,
    )
    if df.empty:
        log.warning("No settled windows found for date range %s – %s", min_date, max_date)
        return np.empty((0, len(FEATURE_COLS))), np.empty(0, dtype=int), list(FEATURE_COLS)

    return build_feature_matrix(df)
